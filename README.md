# Monarch Money MCP for vps2

This fork is a single-tenant Monarch Money MCP server for the operator's Claude connector. It uses the [upstream FastMCP server](https://github.com/richardadonnell/monarch-money-mcp) at commit `77789519c09ac539be7e2540bf4fe3369884de23`, with a smaller security surface:

- `/mcp` requires GitHub OAuth and admits only `GITHUB_ALLOWED_USER`.
- `/health` is public and returns only `{"status":"ok"}`.
- OAuth discovery, registration, and callback routes are public as required by the protocol. There is no REST API, static API key, password login, or token-return endpoint.
- The Monarch browser session token and GitHub OAuth client secret are read from mounted files. Their contents never belong in Git or Coolify environment variables.

The MCP tools include reads plus `update_transaction` and `set_budget_amount`. These write tools edit Monarch records; they do not transfer money. The Monarch session token itself has full account privilege. Removing MCP write tools would reduce accidental assistant edits, but would not make a stolen token read-only.

## Deployment

Use a Git-connected **Coolify Application** built from this repository's Dockerfile. Set the internal service port to 8000 and the domain to `https://monarch-mcp.briansagency.com`. Do not publish a Docker host port. Keep Cloudflare proxying the hostname. Ordinary Cloudflare Access cannot sit in front of this route because it would intercept the MCP OAuth handshake.

Configure runtime variables (not build variables):

| Name | Value |
| --- | --- |
| `MONARCH_TOKEN_FILE` | `/run/secrets/monarch.token` |
| `GITHUB_CLIENT_SECRET_FILE` | `/run/secrets/github-oauth.secret` |
| `GITHUB_CLIENT_ID` | GitHub OAuth App client ID |
| `GITHUB_ALLOWED_USER` | `godigi` |
| `PUBLIC_BASE_URL` | `https://monarch-mcp.briansagency.com` |
| `FASTMCP_HOME` | `/data` |

Create a GitHub OAuth App under the allowed account. Its callback URL must be exactly `https://monarch-mcp.briansagency.com/auth/callback`. Mount the existing host files into the application:

| Host file | Container path |
| --- | --- |
| `/root/credentials/apps/monarch.token` | `/run/secrets/monarch.token` |
| `/root/credentials/apps/monarch-github-oauth.secret` | `/run/secrets/github-oauth.secret` |

The container runs as UID/GID 10001. On the VPS, make each source file root-owned, group 10001, mode 0440, and keep the parent directory root-only. Use Coolify **Host File Mount**, not a managed File Mount, so Coolify does not copy the contents into its resource configuration. Mount a persistent named volume at `/data` for FastMCP client registration and OAuth state. Back up that volume or reauthorize Claude after loss.

Never paste the Monarch token into chat or a terminal command that will be recorded in shell history. Transfer it from the Mac directly to the on-box path. A token-only deployment cannot renew an expired Monarch token; replace the file and restart the app when calls start returning 401.

## Verification

Run `./verify_server.sh https://monarch-mcp.briansagency.com` from outside the VPS. It requires unauthenticated MCP requests to get 401 with an OAuth challenge, removed REST routes to return 404, and OAuth metadata to identify this MCP resource. It does not print financial data or secrets.

Then add `https://monarch-mcp.briansagency.com/mcp` as a Claude custom connector and authenticate through GitHub. Read accounts to prove the Monarch token works. A live write test requires a specific transaction or budget change approved by the operator, followed by a read-back. A successful deployment status alone is not proof that authentication or Monarch access works.

## Local tests

With Python 3.12 and uv:

```bash
uv venv --python python3.12 .venv
uv pip install --python .venv/bin/python -r requirements.lock
.venv/bin/python -m unittest test_security -q
.venv/bin/python test_github_allowlist.py
```

The tests use synthetic secrets and do not contact Monarch.
