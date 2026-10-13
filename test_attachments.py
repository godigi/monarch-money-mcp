import base64
import copy
import socket
import unittest
from unittest.mock import patch

from test_transaction_tools import FakeMonarch, direct
from transaction_tools import TransactionTools
try:
    from attachments import ReceiptUploads, validate_file, safe_url, PublicResolver, download_file
except ImportError:
    ReceiptUploads = None

PNG = b"\x89PNG\r\n\x1a\n" + b"image-fixture"


class ReceiptMonarch(FakeMonarch):
    async def gql_call(self, operation, graphql_query, variables=None):
        if operation == "Common_GetTransactionAttachmentUploadInfo":
            return {"getTransactionAttachmentUploadInfo": {"info": {
                "path": "/v1_1/monarch-test/auto/upload",
                "requestParams": {"timestamp": "123", "signature": "signed", "api_key": "public-key"}}}}
        if operation == "Common_AddTransactionAttachment":
            if self.reject:
                return {"addTransactionAttachment": {"errors": [{"message": "denied"}]}}
            attachment = {"id": "att1", **variables["input"]}
            self.txn["attachments"].append(attachment)
            return {"addTransactionAttachment": {"errors": [], "attachment": attachment}}
        return await super().gql_call(operation, graphql_query, variables)


class Response:
    def __init__(self, chunks=(), status=200, headers=None, json_data=None):
        self.status, self.headers = status, headers or {}
        self.content_length = int(self.headers["Content-Length"]) if "Content-Length" in self.headers else None
        self.content = self
        self.chunks, self.json_data = chunks, json_data
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def iter_chunked(self, size):
        for chunk in self.chunks: yield chunk
    async def json(self): return self.json_data


class Session:
    def __init__(self, responses): self.responses, self.requests = responses, []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)
    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(ReceiptUploads, "Receipt uploads have not been implemented")
        self.backend = ReceiptMonarch()
        self.transactions = TransactionTools(self.backend, direct)
        self.service = ReceiptUploads(self.transactions)

    async def test_file_signature_size_and_filename_are_checked(self):
        self.assertEqual(validate_file(PNG, "receipt.png"), "image/png")
        for data, name in [(b"not-an-image", "receipt.png"), (PNG, "../secret.png"),
                           (PNG, "receipt.pdf"), (PNG + b"a" * (10 * 1024 * 1024), "big.png")]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_file(data, name)

    async def test_upload_links_native_attachment_and_verifies_it(self):
        session = Session([Response(json_data={"public_id": "monarch-test/upload1"})])
        with patch("attachments.aiohttp.ClientSession", return_value=session):
            result = await self.service.upload_transaction_receipt_bytes("tx1", base64.b64encode(PNG).decode(), "receipt.png")
        self.assertTrue(result["verified"])
        self.assertEqual(result["attachment_id"], "att1")
        self.assertEqual(self.backend.txn["attachments"][0]["sizeBytes"], len(PNG))
        self.assertEqual(session.requests[0][0], "https://api.cloudinary.com/v1_1/monarch-test/auto/upload")
        self.assertNotIn("headers", session.requests[0][1])
        self.assertIs(session.requests[0][1]["allow_redirects"], False)

    async def test_invalid_base64_and_attachment_limit_refuse_native_change(self):
        with self.assertRaises(ValueError):
            await self.service.upload_transaction_receipt_bytes("tx1", "not base64", "receipt.png")
        self.backend.txn["attachments"] = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        before = copy.deepcopy(self.backend.txn)
        with self.assertRaises(ValueError):
            await self.service.upload_transaction_receipt_bytes("tx1", base64.b64encode(PNG).decode(), "receipt.png")
        self.assertEqual(self.backend.txn, before)

    async def test_rejected_link_is_not_reported_as_success(self):
        self.backend.reject = True
        session = Session([Response(json_data={"public_id": "asset"})])
        with patch("attachments.aiohttp.ClientSession", return_value=session), self.assertRaisesRegex(ValueError, "rejected"):
            await self.service.upload_transaction_receipt_bytes("tx1", base64.b64encode(PNG).decode(), "receipt.png")
        self.assertEqual(self.backend.txn["attachments"], [])

    async def test_live_upload_path_with_trailing_slash_is_accepted(self):
        from attachments import upload_asset
        info = {"path": "/v1_1/monarch-money/image/upload/", "requestParams": {
            "timestamp": "1", "signature": "signed", "api_key": "public-key"}}
        session = Session([Response(json_data={"public_id": "asset"})])
        with patch("attachments.aiohttp.ClientSession", return_value=session):
            self.assertEqual(await upload_asset(info, PNG, "receipt.png", "image/png"), "asset")

    async def test_url_rejects_literal_addresses_credentials_and_http(self):
        for url in ("http://files.example.com/a", "https://127.0.0.1/a", "https://[::1]/a",
                    "https://u:p@files.example.com/a", "https://localhost/a", "https://files.example.com:8000/a"):
            with self.subTest(url=url), self.assertRaises(ValueError): safe_url(url)
        self.assertEqual(safe_url("https://files.example.com/a?sig=signed"), "https://files.example.com/a?sig=signed")

    async def test_unicode_ip_hostname_is_rejected_before_connector_can_bypass_resolver(self):
        from yarl import URL
        url = "https://１２７.０.０.１/receipt.png"
        self.assertEqual(URL(url).raw_host, "127.0.0.1")
        with self.assertRaises(ValueError):
            safe_url(url)

    async def test_connection_resolver_rejects_private_dns_answer(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
        loop = __import__("asyncio").get_running_loop()
        with patch.object(loop, "getaddrinfo", return_value=private), self.assertRaises(ValueError):
            await PublicResolver().resolve("files.example.com", 443)

    async def test_download_stops_oversized_stream_and_private_redirect(self):
        for response in (Response(chunks=[b"a" * (10 * 1024 * 1024), b"b"]),
                         Response(status=302, headers={"Location": "https://127.0.0.1/secret"})):
            session = Session([response])
            with patch("attachments.aiohttp.ClientSession", return_value=session), self.assertRaises(ValueError):
                await download_file("https://files.example.com/a")
            self.assertEqual(len(session.requests), 1)

    async def test_download_follows_public_redirect_without_forwarding_credentials(self):
        session = Session([Response(status=302, headers={"Location": "https://cdn.example.com/image"}), Response(chunks=[PNG])])
        with patch("attachments.aiohttp.ClientSession", return_value=session):
            self.assertEqual(await download_file("https://files.example.com/a"), PNG)
        self.assertEqual(len(session.requests), 2)
        self.assertTrue(all("headers" not in kwargs for _, kwargs in session.requests))


if __name__ == "__main__": unittest.main()
