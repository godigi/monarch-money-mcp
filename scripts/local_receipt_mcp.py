#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["fastmcp==4.0.0b3", "py-key-value-aio[filetree]==0.4.6", "cryptography==50.0.1"]
# ///
"""Claude Code stdio helper: transfer a selected local receipt to remote Monarch MCP."""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastmcp import Client, FastMCP
from fastmcp.client.auth import OAuth
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

ENDPOINT = "https://monarch-mcp.briansagency.com/mcp"
MAX_BYTES = 10 * 1024 * 1024


def encrypted_store(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    key_path = directory / "encryption.key"
    if not key_path.exists():
        # Publish a completely written key without replacing a concurrent winner.
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".key-", delete=False) as key_file:
            temporary = Path(key_file.name)
            try:
                key_file.write(Fernet.generate_key())
                key_file.flush()
                os.fsync(key_file.fileno())
                try:
                    os.link(temporary, key_path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
    key_path.chmod(0o600)
    return FernetEncryptionWrapper(FileTreeStore(data_directory=directory / "tokens"),
                                   fernet=Fernet(key_path.read_bytes()))


class RemoteReceiptBridge:
    def __init__(self, endpoint=ENDPOINT, *, client_factory=Client, state_dir=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Remote Monarch MCP must use an HTTPS URL without embedded credentials")
        self.endpoint, self.client_factory = endpoint, client_factory
        self.state_dir = state_dir or Path.home() / ".config" / "monarch-receipts"
        self._oauth = None

    def connect(self):
        if self._oauth is None:
            self._oauth = OAuth(self.endpoint, token_storage=encrypted_store(self.state_dir),
                                client_name="Monarch local receipt helper")
        return self.client_factory(self.endpoint, auth=self._oauth, timeout=180)

    async def upload_transaction_receipt(self, transaction_id: str, file_path: str) -> dict:
        """Attach an explicitly selected LOCAL JPEG/PNG/WebP/PDF receipt (max 10 MiB).

        Use the remote Monarch connector to identify the exact transaction first. Confirm
        the intended file and transaction with the user before calling this write tool.
        The file is read here on the Mac and transferred to the existing OAuth-protected
        VPS connector; its contents do not need to be copied into conversation. This adds
        a real transaction attachment, without OCR or a new transaction. Inspect the target
        before repeating an uncertain upload. First use opens GitHub OAuth if login is needed.
        """
        if not transaction_id.strip() or len(transaction_id) > 128:
            raise ValueError("Provide the exact Monarch transaction ID")
        path = Path(file_path).expanduser()
        if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}:
            raise ValueError("Select an existing local JPEG, PNG, WebP or PDF file")
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Receipt exceeds 10 MiB")
        with path.open("rb") as file:
            data = file.read(MAX_BYTES + 1)
        if not data or len(data) > MAX_BYTES:
            raise ValueError("Receipt must be nonempty and at most 10 MiB")
        try:
            async with self.connect() as remote:
                result = await remote.call_tool("upload_transaction_receipt_bytes", {
                    "transaction_id": transaction_id.strip(), "filename": path.name,
                    "data_base64": base64.b64encode(data).decode("ascii"),
                })
                if not isinstance(result.data, dict):
                    raise ValueError("Remote server did not return an attachment result")
                return result.data
        except Exception:
            # Underlying HTTP exceptions can include OAuth headers, temporary URLs or file data.
            raise ValueError("Remote upload did not confirm a result. Check authentication and the transaction before repeating.") from None

    async def login(self):
        async with self.connect() as remote:
            names = {tool.name for tool in await remote.list_tools()}
            print("Monarch OAuth connected; receipt upload tool " + ("available" if "upload_transaction_receipt_bytes" in names else "not yet deployed"))


def build_server(bridge):
    mcp = FastMCP("Monarch local receipts")
    mcp.tool(bridge.upload_transaction_receipt, annotations={
        "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False})
    return mcp


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING)
    # HTTP debug logging can contain the base64 file payload and bearer token.
    for name in ("httpx", "httpcore", "mcp", "fastmcp"):
        logging.getLogger(name).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=ENDPOINT)
    parser.add_argument("--login", action="store_true", help="Sign in through GitHub without uploading a file")
    args = parser.parse_args()
    bridge = RemoteReceiptBridge(args.server)
    if args.login:
        asyncio.run(bridge.login())
    else:
        build_server(bridge).run(transport="stdio", show_banner=False)


if __name__ == "__main__": main()
