# Monarch Money MCP fork

This is the hardened, single-tenant fork, hosted on prod1 (Coolify app `r7baehvexg3gfuo7voy395o6`; moved from vps2 on 2026-10-15). Read README.md before changing the server.

- The only data route is OAuth-protected `/mcp`; `/health` returns no account data.
- No REST routes, static API key, password login, or token-return route may be restored without an explicit security review.
- `MONARCH_COOKIE_FILE` and `GITHUB_CLIENT_SECRET_FILE` point to host-mounted secret files. Never print or commit their contents. The old token-file instruction was incorrect for the current cookie deployment.
- Writes include transaction/budget changes, tag creation/addition, native rule creation and receipt attachments. A live financial test needs an operator-approved record change.
- Rule creation requires a complete preview ID; applying it to past transactions defaults off. Keep tag additions additive and use the exact transaction ID.
- Receipts use bounded in-memory file transfer; never restore arbitrary VPS file-path reads or log bytes, signed URLs or upload parameters.
- Run `.venv/bin/python -m unittest discover -p 'test_*.py' -q` and `.venv/bin/python test_github_allowlist.py` before shipping. Run `./verify_server.sh BASE_URL` after deployment.
