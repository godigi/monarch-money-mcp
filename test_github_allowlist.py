"""
Self-check for the GitHub login allowlist in server.py.

Run:  python test_github_allowlist.py

No network, no GitHub account, no Monarch account needed. The parent verifier's
verify_token is replaced with a fake that returns a canned AccessToken, so the
real https://api.github.com/user call never happens.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

_test_dir = tempfile.TemporaryDirectory()
_cookie = Path(_test_dir.name) / "monarch.cookie"
_github = Path(_test_dir.name) / "github.secret"
_cookie.write_text("session_id=test-session; csrftoken=test-csrf\n")
_github.write_text("test-secret\n")
os.environ["MONARCH_COOKIE_FILE"] = str(_cookie)
os.environ["GITHUB_CLIENT_SECRET_FILE"] = str(_github)
os.environ["GITHUB_CLIENT_ID"] = "Ov23liTEST"
os.environ["GITHUB_ALLOWED_USER"] = "godigi"
os.environ["PUBLIC_BASE_URL"] = "https://monarch-mcp.briansagency.com"
os.environ["FASTMCP_HOME"] = _test_dir.name

import server  # noqa: E402
from fastmcp.server.auth import AccessToken, OAuthProxy  # noqa: E402
from fastmcp.server.auth.providers.github import GitHubTokenVerifier  # noqa: E402


def _stub_parent(claims: dict[str, object] | None) -> None:
    """Replace GitHubTokenVerifier.verify_token with a canned response."""

    async def _verify(self, token: str) -> AccessToken | None:  # noqa: ANN001
        if claims is None:
            return None
        return AccessToken(
            token=token,
            client_id="12345",
            scopes=["user"],
            claims=claims,
        )

    GitHubTokenVerifier.verify_token = _verify  # type: ignore[method-assign]


def test_allowed_login_passes() -> None:
    _stub_parent({"login": "richardadonnell", "sub": "12345"})
    v = server.AllowlistedGitHubTokenVerifier(allowed_login="richardadonnell")
    result = asyncio.run(v.verify_token("tok"))
    assert result is not None, "the allowed login must be admitted"
    assert result.claims["login"] == "richardadonnell"


def test_other_login_rejected() -> None:
    _stub_parent({"login": "someone-else", "sub": "99999"})
    v = server.AllowlistedGitHubTokenVerifier(allowed_login="richardadonnell")
    assert asyncio.run(v.verify_token("tok")) is None, (
        "a non-allowlisted GitHub login must be refused"
    )


def test_login_match_is_case_insensitive() -> None:
    # GitHub usernames are case-insensitive; RichardADonnell is the same account.
    _stub_parent({"login": "RichardADonnell", "sub": "12345"})
    v = server.AllowlistedGitHubTokenVerifier(allowed_login="richardadonnell")
    assert asyncio.run(v.verify_token("tok")) is not None


def test_parent_rejection_propagates() -> None:
    _stub_parent(None)
    v = server.AllowlistedGitHubTokenVerifier(allowed_login="richardadonnell")
    assert asyncio.run(v.verify_token("tok")) is None


def test_missing_login_claim_rejected() -> None:
    _stub_parent({"sub": "12345"})  # no "login" key at all
    v = server.AllowlistedGitHubTokenVerifier(allowed_login="richardadonnell")
    assert asyncio.run(v.verify_token("tok")) is None, (
        "absent login claim must fail closed, not match"
    )


def test_empty_allowlist_refuses_to_construct() -> None:
    try:
        server.AllowlistedGitHubTokenVerifier(allowed_login="")
    except ValueError:
        return
    raise AssertionError("an empty allowed_login must raise, never match everyone")



def test_build_auth_requires_oauth() -> None:
    assert isinstance(server._build_auth(), OAuthProxy)


if __name__ == "__main__":
    test_allowed_login_passes()
    test_other_login_rejected()
    test_login_match_is_case_insensitive()
    test_parent_rejection_propagates()
    test_missing_login_claim_rejected()
    test_empty_allowlist_refuses_to_construct()
    test_build_auth_requires_oauth()
    print("OK github allowlist: 7/7")
