"""Security boundary checks for the vps2 Monarch deployment."""

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from gql.transport.exceptions import TransportServerError
from starlette.testclient import TestClient

_test_secrets = tempfile.TemporaryDirectory()
_token_path = Path(_test_secrets.name) / "monarch.token"
_github_path = Path(_test_secrets.name) / "github.secret"
_token_path.write_text("test-token\n")
_github_path.write_text("test-secret\n")
os.environ["MONARCH_TOKEN_FILE"] = str(_token_path)
os.environ["GITHUB_CLIENT_SECRET_FILE"] = str(_github_path)
os.environ["GITHUB_CLIENT_ID"] = "Ov23liTEST"
os.environ["GITHUB_CLIENT_SECRET"] = "legacy-test-secret"
os.environ["GITHUB_ALLOWED_USER"] = "godigi"
os.environ["PUBLIC_BASE_URL"] = "https://monarch-mcp.briansagency.com"
os.environ["FASTMCP_HOME"] = _test_secrets.name
os.environ.setdefault("MCP_API_KEY", "test-key")

import server  # noqa: E402


class SecretFileTests(unittest.TestCase):
    def test_reads_token_from_file_without_exposing_it_in_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monarch.token"
            path.write_text("browser-token\n")
            with patch.dict(os.environ, {"MONARCH_TOKEN_FILE": str(path)}):
                self.assertEqual(
                    server.read_secret("Monarch token", "MONARCH_TOKEN_FILE"),
                    "browser-token",
                )

    def test_missing_file_fails_closed(self):
        with patch.dict(os.environ, {"MONARCH_TOKEN_FILE": "/missing/monarch.token"}):
            with self.assertRaisesRegex(RuntimeError, "Monarch token"):
                server.read_secret("Monarch token", "MONARCH_TOKEN_FILE")

    def test_blank_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monarch.token"
            path.write_text("\n")
            with patch.dict(os.environ, {"MONARCH_TOKEN_FILE": str(path)}):
                with self.assertRaisesRegex(RuntimeError, "Monarch token"):
                    server.read_secret("Monarch token", "MONARCH_TOKEN_FILE")


class StartupTests(unittest.TestCase):
    def _import_server(self, overrides):
        env = os.environ.copy()
        for key in (
            "MONARCH_TOKEN",
            "MONARCH_TOKEN_FILE",
            "MONARCH_EMAIL",
            "MONARCH_PASSWORD",
            "MONARCH_MFA_SECRET",
            "MCP_API_KEY",
            "GITHUB_CLIENT_ID",
            "GITHUB_CLIENT_SECRET",
            "GITHUB_CLIENT_SECRET_FILE",
            "GITHUB_ALLOWED_USER",
            "PUBLIC_BASE_URL",
        ):
            env.pop(key, None)
        env.update(overrides)
        return subprocess.run(
            [sys.executable, "-c", "import server"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_missing_monarch_file_refuses_startup_even_with_legacy_env_token(self):
        result = self._import_server({"MONARCH_TOKEN": "legacy-token", "MCP_API_KEY": "test-key"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MONARCH_TOKEN_FILE", result.stderr)

    def test_missing_github_secret_file_refuses_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "monarch.token"
            token.write_text("browser-token\n")
            result = self._import_server({
                "MONARCH_TOKEN_FILE": str(token),
                "MCP_API_KEY": "test-key",
                "GITHUB_CLIENT_ID": "Ov23liTEST",
                "GITHUB_CLIENT_SECRET": "legacy-secret",
                "GITHUB_ALLOWED_USER": "godigi",
                "PUBLIC_BASE_URL": "https://monarch-mcp.briansagency.com",
            })
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("GITHUB_CLIENT_SECRET_FILE", result.stderr)

    def test_valid_host_files_boot_without_password_or_static_key(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "monarch.token"
            github = Path(directory) / "github.secret"
            token.write_text("browser-token\n")
            github.write_text("oauth-secret\n")
            result = self._import_server({
                "MONARCH_TOKEN_FILE": str(token),
                "GITHUB_CLIENT_SECRET_FILE": str(github),
                "GITHUB_CLIENT_ID": "Ov23liTEST",
                "GITHUB_ALLOWED_USER": "godigi",
                "PUBLIC_BASE_URL": "https://monarch-mcp.briansagency.com",
                "FASTMCP_HOME": directory,
            })
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_blank_github_client_id_refuses_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "monarch.token"
            github = Path(directory) / "github.secret"
            token.write_text("browser-token\n")
            github.write_text("oauth-secret\n")
            result = self._import_server({
                "MONARCH_TOKEN_FILE": str(token),
                "GITHUB_CLIENT_SECRET_FILE": str(github),
                "GITHUB_CLIENT_ID": "",
                "GITHUB_ALLOWED_USER": "godigi",
                "PUBLIC_BASE_URL": "https://monarch-mcp.briansagency.com",
                "MCP_API_KEY": "test-key",
            })
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("GITHUB_CLIENT_ID", result.stderr)

    def test_public_base_url_requires_https_and_excludes_mcp_path(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "monarch.token"
            github = Path(directory) / "github.secret"
            token.write_text("browser-token\n")
            github.write_text("oauth-secret\n")
            base = {
                "MONARCH_TOKEN_FILE": str(token),
                "GITHUB_CLIENT_SECRET_FILE": str(github),
                "GITHUB_CLIENT_ID": "Ov23liTEST",
                "GITHUB_ALLOWED_USER": "godigi",
            }
            for bad_url in (
                "http://monarch-mcp.briansagency.com",
                "https://monarch-mcp.briansagency.com/mcp",
            ):
                with self.subTest(url=bad_url):
                    result = self._import_server({**base, "PUBLIC_BASE_URL": bad_url})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("PUBLIC_BASE_URL", result.stderr)


class RouteTests(unittest.TestCase):
    def test_chatgpt_callback_can_register_but_an_unrelated_redirect_cannot(self):
        def payload(redirect_uri):
            return {
                "client_name": "ChatGPT Monarch MCP",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }

        with TestClient(server.app) as client:
            allowed = client.post(
                "/register",
                json=payload("https://chatgpt.com/connector/oauth/R3V1cbYg3KfX"),
            )
            denied = client.post(
                "/register",
                json=payload("https://example.com/steal-code"),
            )
        self.assertEqual(allowed.status_code, 201)
        self.assertEqual(denied.status_code, 400)

    def test_token_return_route_is_absent_even_with_legacy_key(self):
        with TestClient(server.app) as client:
            response = client.get("/api/token", headers={"Authorization": "Bearer test-key"})
        self.assertEqual(response.status_code, 404)

    def test_rest_write_route_is_absent(self):
        with TestClient(server.app) as client:
            response = client.post(
                "/api/transaction/txn_1",
                headers={"Authorization": "Bearer test-key"},
                json={"notes": "unwanted"},
            )
        self.assertEqual(response.status_code, 404)

    def test_static_key_cannot_authenticate_mcp(self):
        with TestClient(server.app) as client:
            response = client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer test-key",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
        self.assertEqual(response.status_code, 401)

    def test_expired_monarch_token_is_not_retried_with_a_password(self):
        calls = []

        async def expired():
            calls.append(1)
            raise TransportServerError("expired", code=401)

        with self.assertRaises(TransportServerError):
            asyncio.run(server._call(expired))
        self.assertEqual(len(calls), 1)

    def test_transaction_and_budget_writes_remain_available_to_authorized_clients(self):
        tools = asyncio.run(server.mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertIn("update_transaction", names)
        self.assertIn("set_budget_amount", names)


if __name__ == "__main__":
    unittest.main()
