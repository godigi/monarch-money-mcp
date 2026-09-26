# Monarch Money MCP fork

This is the hardened, single-tenant fork for vps2. Read README.md before changing the server.

- The only data route is OAuth-protected `/mcp`; `/health` returns no account data.
- No REST routes, static API key, password login, or token-return route may be restored without an explicit security review.
- `MONARCH_TOKEN_FILE` and `GITHUB_CLIENT_SECRET_FILE` point to host-mounted secret files. Never print or commit their contents.
- The two write tools are `update_transaction` and `set_budget_amount`; a live test needs an operator-approved record change.
- Run `.venv/bin/python -m unittest test_security -q` and `.venv/bin/python test_github_allowlist.py` before shipping. Run `./verify_server.sh BASE_URL` after deployment.
