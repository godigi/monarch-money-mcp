"""In-memory receipt transfer and native Monarch transaction attachments."""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import socket
from pathlib import PurePath
from typing import NotRequired, TypedDict
from urllib.parse import urljoin, urlsplit

import aiohttp
from gql import gql
from yarl import URL

from transaction_tools import checked_payload, require_id

MAX_FILE_BYTES = 10 * 1024 * 1024


class OpenAIFile(TypedDict):
    download_url: str
    file_id: str
    mime_type: NotRequired[str]
    file_name: NotRequired[str]


def validate_file(data: bytes, filename: str) -> str:
    if not isinstance(data, bytes) or not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("Receipt must be nonempty and at most 10 MiB")
    if (not isinstance(filename, str) or not filename or len(filename) > 200
            or PurePath(filename).name != filename or "\\" in filename
            or any(ord(c) < 32 for c in filename)):
        raise ValueError("Provide a simple receipt filename without a path")
    extension = PurePath(filename).suffix.lower()
    if data.startswith(b"\x89PNG\r\n\x1a\n") and extension == ".png":
        return "image/png"
    if data.startswith(b"\xff\xd8\xff") and extension in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and extension == ".webp":
        return "image/webp"
    if data.startswith(b"%PDF-") and extension == ".pdf":
        return "application/pdf"
    raise ValueError("Receipt contents and extension must identify JPEG, PNG, WebP or PDF")


def safe_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError("Invalid file download URL")
    try:
        # aiohttp normalizes IDNA before connecting; inspect that same canonical
        # hostname so Unicode spellings of IPs cannot bypass the DNS resolver.
        url = str(URL(url))
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid file download URL") from None
    if (parsed.scheme != "https" or not host or "." not in host or parsed.username
            or parsed.password or parsed.fragment or port not in (None, 443)
            or any(ord(c) < 33 for c in url)):
        raise ValueError("File URL must use HTTPS on a public hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return url
    raise ValueError("Literal IP addresses are not accepted for file downloads")


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate the addresses actually handed to the connector (prevents DNS rebinding)."""
    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, family=family)
        results = []
        for af, _, proto, _, address in records:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or (ip.version == 6 and ip.ipv4_mapped and not ip.ipv4_mapped.is_global):
                raise ValueError("File hostname resolves to a nonpublic address")
            results.append({"hostname": host, "host": address[0], "port": port,
                            "family": af, "proto": proto, "flags": socket.AI_NUMERICHOST})
        if not results:
            raise ValueError("File hostname could not be resolved")
        return results

    async def close(self):
        pass


async def download_file(url: str) -> bytes:
    url = safe_url(url)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    try:
        async with aiohttp.ClientSession(connector=connector, trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(), timeout=aiohttp.ClientTimeout(total=45)) as session:
            for redirects in range(4):
                url = safe_url(url)
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        target = response.headers.get("Location")
                        if not target or redirects == 3:
                            raise ValueError("File download redirected too many times")
                        url = safe_url(urljoin(url, target))
                        continue
                    if response.status != 200:
                        raise ValueError("File download failed; the temporary link may have expired")
                    if response.content_length and response.content_length > MAX_FILE_BYTES:
                        raise ValueError("Receipt exceeds 10 MiB")
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise ValueError("Receipt exceeds 10 MiB")
                        chunks.append(chunk)
                    return b"".join(chunks)
    except ValueError:
        raise
    except Exception:
        raise ValueError("File download failed; attach the file again to obtain a fresh link") from None
    finally:
        # Also closes the real connector when a test substitutes the network session.
        await connector.close()
    raise ValueError("File download did not complete")


UPLOAD_INFO = gql("""
mutation Common_GetTransactionAttachmentUploadInfo($transactionId: UUID!) {
  getTransactionAttachmentUploadInfo(transactionId: $transactionId) {
    info { path requestParams { timestamp folder signature api_key upload_preset } }
  }
}
""")
LINK_ATTACHMENT = gql("""
mutation Common_AddTransactionAttachment($input: TransactionAddAttachmentMutationInput!) {
  addTransactionAttachment(input: $input) {
    attachment { id publicId extension sizeBytes filename }
    errors { message code }
  }
}
""")


async def upload_asset(info, data, filename, content_type):
    path, params = info.get("path"), info.get("requestParams")
    if not isinstance(path, str) or not isinstance(params, dict):
        raise ValueError("Monarch returned invalid upload instructions")
    url = path if path.startswith("https://") else "https://api.cloudinary.com" + path
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "api.cloudinary.com"
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment or not parsed.path.startswith("/v1_1/")
            or not parsed.path.rstrip("/").endswith("/upload")):
        raise ValueError("Monarch upload instructions did not target the expected file service")
    if not params.get("signature") or not params.get("api_key") or not params.get("timestamp"):
        raise ValueError("Monarch upload instructions are missing signature fields")
    form = aiohttp.FormData()
    for key in ("timestamp", "folder", "signature", "api_key", "upload_preset"):
        value = params.get(key)
        if value is not None:
            form.add_field(key, str(value))
    form.add_field("file", data, filename=filename, content_type=content_type)
    try:
        # Independent session: no Monarch Cookie/CSRF or connector OAuth header is sent here.
        async with aiohttp.ClientSession(trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.post(url, data=form, allow_redirects=False) as response:
                if response.status not in {200, 201}:
                    raise ValueError("File service rejected the upload; no attachment link was created")
                result = await response.json()
    except ValueError:
        raise
    except Exception:
        raise ValueError("File upload failed; no attachment link was confirmed") from None
    if not isinstance(result, dict) or not isinstance(result.get("public_id"), str) or not result["public_id"]:
        raise ValueError("File service did not return an uploaded asset ID")
    return result["public_id"]


class ReceiptUploads:
    def __init__(self, transactions):
        self.transactions = transactions

    async def upload(self, transaction_id, data, filename):
        transaction_id = require_id(transaction_id)
        content_type = validate_file(data, filename)
        service = self.transactions
        async with service.transaction_lock(transaction_id):
            txn = await service.get_transaction_details(transaction_id)
            attachments = txn.get("attachments")
            if not isinstance(attachments, list):
                raise ValueError("Could not read existing attachments")
            if len(attachments) >= 3:
                raise ValueError("Transaction already has three attachments; remove one in Monarch first")
            info_data = await service.api(service.client.gql_call,
                operation="Common_GetTransactionAttachmentUploadInfo", graphql_query=UPLOAD_INFO,
                variables={"transactionId": transaction_id})
            info = checked_payload(info_data, "getTransactionAttachmentUploadInfo").get("info")
            if not isinstance(info, dict):
                raise ValueError("Monarch did not return upload instructions")
            public_id = await upload_asset(info, data, filename, content_type)
            payload = checked_payload(await service.api(service.client.gql_call,
                operation="Common_AddTransactionAttachment", graphql_query=LINK_ATTACHMENT,
                variables={"input": {"transactionId": transaction_id, "publicId": public_id,
                    "filename": PurePath(filename).stem, "extension": PurePath(filename).suffix[1:].lower(),
                    "sizeBytes": len(data)}}), "addTransactionAttachment")
            attachment = payload.get("attachment")
            if not isinstance(attachment, dict) or not attachment.get("id"):
                raise ValueError("Attachment link did not return an ID; inspect before repeating")
            try:
                after = await service.get_transaction_details(transaction_id)
                verified = any(str(a.get("id")) == str(attachment["id"]) for a in after["attachments"])
            except (ValueError, KeyError, TypeError):
                verified = False
            return {"transaction_id": transaction_id, "attachment_id": attachment["id"],
                    "filename": filename, "size_bytes": len(data), "verified": verified,
                    "message": "Receipt attached" if verified else "Attachment linked but readback unavailable; inspect before repeating"}

    async def upload_transaction_receipt(self, transaction_id: str, file: OpenAIFile) -> dict:
        """Attach the user-selected ChatGPT receipt to the exact existing transaction.

        Uses ChatGPT file input (temporary download URL), accepts JPEG/PNG/WebP/PDF <=10 MiB.
        This adds an actual attachment, without invoking receipt-scanning/OCR or creating a
        transaction. Confirm the transaction with the user first. Do not repeat an uncertain upload.
        """
        require_id(transaction_id)
        if not file.get("file_id"):
            raise ValueError("Select or attach a file in ChatGPT first")
        data = await download_file(file["download_url"])
        filename = file.get("file_name")
        if not filename:
            # OpenAI may omit the name; infer an extension from validated bytes.
            if data.startswith(b"\x89PNG\r\n\x1a\n"): filename = "receipt.png"
            elif data.startswith(b"\xff\xd8\xff"): filename = "receipt.jpg"
            elif data.startswith(b"RIFF") and data[8:12] == b"WEBP": filename = "receipt.webp"
            elif data.startswith(b"%PDF-"): filename = "receipt.pdf"
            else: raise ValueError("Unsupported receipt file")
        return await self.upload(transaction_id, data, filename)

    async def upload_transaction_receipt_bytes(self, transaction_id: str, data_base64: str, filename: str) -> dict:
        """Attach bytes supplied by the local Claude Code helper. Do not manufacture receipt data.

        JPEG/PNG/WebP/PDF <=10 MiB. A client filesystem path cannot be read by this VPS.
        The helper encodes the selected local file without placing its contents in conversation.
        Confirm the target transaction first; do not repeat an uncertain upload.
        """
        if not isinstance(data_base64, str) or len(data_base64) > ((MAX_FILE_BYTES + 2) // 3) * 4:
            raise ValueError("Encoded receipt exceeds 10 MiB")
        try:
            data = base64.b64decode(data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("Receipt bytes must be valid base64") from None
        return await self.upload(transaction_id, data, filename)


def register_attachment_tools(mcp, transactions):
    service = ReceiptUploads(transactions)
    for name in ("upload_transaction_receipt", "upload_transaction_receipt_bytes"):
        mcp.tool(getattr(service, name), name=name,
                 meta={"openai/fileParams": ["file"]} if name == "upload_transaction_receipt" else None,
                 annotations={"readOnlyHint": False, "destructiveHint": False,
                              "idempotentHint": False, "openWorldHint": False})
    return service
