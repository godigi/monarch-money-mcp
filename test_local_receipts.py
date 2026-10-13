import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from scripts.local_receipt_mcp import RemoteReceiptBridge, encrypted_store
except ImportError:
    RemoteReceiptBridge = None


class Remote:
    def __init__(self): self.upload = None
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def call_tool(self, name, arguments):
        self.upload = (name, arguments)
        return SimpleNamespace(data={"attachment_id": "att1", "verified": True})


class LocalReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(RemoteReceiptBridge, "Local receipt helper has not been implemented")

    async def test_local_file_is_transferred_as_bytes_without_server_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.pdf"
            path.write_bytes(b"%PDF-1.7\nfixture")
            remote = Remote()
            bridge = RemoteReceiptBridge(client_factory=lambda *args, **kw: remote,
                                         state_dir=Path(directory) / "state")
            result = await bridge.upload_transaction_receipt("tx1", str(path))
            self.assertEqual(result, {"attachment_id": "att1", "verified": True})
            self.assertEqual(remote.upload[0], "upload_transaction_receipt_bytes")
            self.assertEqual(base64.b64decode(remote.upload[1]["data_base64"]), path.read_bytes())
            self.assertEqual(remote.upload[1]["filename"], "receipt.pdf")
            self.assertNotIn("file_path", remote.upload[1])

    async def test_missing_unsupported_and_oversized_file_never_uploads(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Remote()
            bridge = RemoteReceiptBridge(client_factory=lambda *args, **kw: remote,
                                         state_dir=Path(directory) / "state")
            bad = Path(directory) / "secret.txt"
            bad.write_text("private")
            big = Path(directory) / "large.pdf"
            with big.open("wb") as stream: stream.truncate(10 * 1024 * 1024 + 1)
            for path in (Path(directory) / "missing.pdf", bad, big):
                with self.subTest(path=path.name), self.assertRaises(ValueError):
                    await bridge.upload_transaction_receipt("tx1", str(path))
            self.assertIsNone(remote.upload)

    async def test_oauth_cache_is_encrypted_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state"
            store = encrypted_store(path)
            await store.put(key="test", value={"token": "synthetic-private-token"}, collection="oauth")
            self.assertEqual(await store.get(key="test", collection="oauth"), {"token": "synthetic-private-token"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertEqual((path / "encryption.key").stat().st_mode & 0o777, 0o600)
            for file in path.rglob("*"):
                if file.is_file(): self.assertNotIn(b"synthetic-private-token", file.read_bytes())

    async def test_oauth_url_keys_can_be_persisted_by_real_token_adapter(self):
        from fastmcp.client.auth.oauth import TokenStorageAdapter
        from mcp.shared.auth import OAuthClientInformationFull
        with tempfile.TemporaryDirectory() as directory:
            adapter = TokenStorageAdapter(encrypted_store(Path(directory) / "state"),
                                          "https://monarch-mcp.briansagency.com/mcp")
            info = OAuthClientInformationFull(client_id="synthetic-client", redirect_uris=["http://localhost:12345/callback"])
            await adapter.set_client_info(info)
            self.assertEqual((await adapter.get_client_info()).client_id, "synthetic-client")

    async def test_concurrent_first_launch_never_observes_partial_key(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from cryptography.fernet import Fernet
        generated = threading.Event()
        release = threading.Event()
        original = Fernet.generate_key
        first = True
        def delayed_key():
            nonlocal first
            if first:
                first = False
                generated.set()
                release.wait(3)
            return original()
        with tempfile.TemporaryDirectory() as directory, ThreadPoolExecutor(max_workers=2) as executor:
            path = Path(directory) / "state"
            with patch("scripts.local_receipt_mcp.Fernet.generate_key", side_effect=delayed_key):
                worker = executor.submit(encrypted_store, path)
                self.assertTrue(generated.wait(2))
                try:
                    second = encrypted_store(path)
                finally:
                    release.set()
                first_store = worker.result(timeout=3)
            await second.put(key="test", value={"token": "shared-key-fixture"}, collection="oauth")
            self.assertEqual(await first_store.get(key="test", collection="oauth"), {"token": "shared-key-fixture"})


if __name__ == "__main__": unittest.main()
