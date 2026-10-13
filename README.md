# Monarch Money MCP for vps2

This fork is a single-tenant Monarch Money MCP server for the operator's Claude and ChatGPT connectors. It uses the [upstream FastMCP server](https://github.com/richardadonnell/monarch-money-mcp) at commit `77789519c09ac539be7e2540bf4fe3369884de23`, with a smaller security surface:

- `/mcp` requires GitHub OAuth and admits only `GITHUB_ALLOWED_USER`.
- `/health` is public and returns only `{"status":"ok"}`.
- OAuth discovery, registration, and callback routes are public as required by the protocol. There is no REST API, static API key, password login, or token-return endpoint.
- The Monarch browser cookie and GitHub OAuth client secret are read from mounted files. Their contents never belong in Git or Coolify environment variables.

The MCP tools include reads, transaction updates, budgets, tag creation/addition, native rule previews/creation and receipt attachments. These write tools edit Monarch records; they do not transfer money. The Monarch session cookie itself has full account privilege. Removing MCP write tools would reduce accidental assistant edits, but would not make a stolen session read-only.

## Deployment

Use a Git-connected **Coolify Application** built from this repository's Dockerfile. Set the internal service port to 8000 and the domain to `https://monarch-mcp.briansagency.com`. Do not publish a Docker host port. Keep Cloudflare proxying the hostname. Ordinary Cloudflare Access cannot sit in front of this route because it would intercept the MCP OAuth handshake.

Configure runtime variables (not build variables):

| Name | Value |
| --- | --- |
| `MONARCH_COOKIE_FILE` | `/run/secrets/monarch.cookie` |
| `GITHUB_CLIENT_SECRET_FILE` | `/run/secrets/github-oauth.secret` |
| `GITHUB_CLIENT_ID` | GitHub OAuth App client ID |
| `GITHUB_ALLOWED_USER` | `godigi` |
| `PUBLIC_BASE_URL` | `https://monarch-mcp.briansagency.com` |
| `FASTMCP_HOME` | `/data` |

Create a **GitHub OAuth App** under the allowed account in GitHub's Developer settings. Its callback URL must be exactly `https://monarch-mcp.briansagency.com/auth/callback`. This is distinct from ChatGPT's custom app setup: ChatGPT's `https://chatgpt.com/connector/oauth/...` URL is a return address for the MCP client and is allowlisted in this server. The GitHub OAuth App client ID and secret belong on the VPS, not in ChatGPT's manual OAuth client fields. Mount the existing host files into the application:

| Host file | Container path |
| --- | --- |
| `/root/credentials/apps/monarch.cookie` | `/run/secrets/monarch.cookie` |
| `/root/credentials/apps/monarch-github-oauth.secret` | `/run/secrets/github-oauth.secret` |

The container runs as UID/GID 10001. On the VPS, make each source file root-owned, group 10001, mode 0440, and keep the parent directory root-only. Use Coolify **Host File Mount**, not a managed File Mount, so Coolify does not copy the contents into its resource configuration. Mount a persistent named volume at `/data` for FastMCP client registration and OAuth state. Back up that volume or reauthorize connected clients after loss.

Monarch's current browser requests use a `Cookie` header rather than an `Authorization: Token` header. Capture a fresh `graphql` request after signing in; copy **only the Cookie header value** to the host file over SSH, never the whole cURL request and never into chat or shell history. The value must contain `session_id=` and `csrftoken=`. This session cannot renew itself; sign in and replace the cookie file, then redeploy when Monarch calls reject it. Signing out invalidates the captured browser session. The library's GraphQL transport is forced to verify TLS certificates, and its response logger is held at WARNING so account payloads do not enter container logs.

## Verification

Run `./verify_server.sh https://monarch-mcp.briansagency.com` from outside the VPS. It requires unauthenticated MCP requests to get 401 with an OAuth challenge, removed REST routes to return 404, and OAuth metadata to identify this MCP resource. It does not print financial data or secrets.

Then add `https://monarch-mcp.briansagency.com/mcp` as a Claude custom connector or ChatGPT custom MCP app and authenticate through GitHub. In ChatGPT's New App form, include `/mcp` in **Server URL**. DCR and CIMD appeared unavailable while this server returned HTTP 503, including for OAuth discovery. After deployment and public discovery verification, refresh the form and select **Dynamic Client Registration (DCR)** if available. If it remains unavailable, investigate before creating the app. Leave ChatGPT's manual OAuth client ID and secret empty; they are not the upstream GitHub OAuth App credentials. FastMCP publishes a DCR endpoint. The ChatGPT callback shown during initial setup, `https://chatgpt.com/connector/oauth/R3V1cbYg3KfX`, and its stable callback are both allowlisted exactly. Read accounts to prove the Monarch cookie works. A live write test requires a specific transaction or budget change approved by the operator, followed by a read-back. A successful deployment status alone is not proof that authentication or Monarch access works.

[OpenAI's current custom MCP plugin guidance](https://developers.openai.com/api/docs/guides/custom-mcp-server) describes read and write tools and workspace action controls. The former statement here that Pro was limited to read/fetch permissions is historical and should not be used to determine current account access. Refresh the existing plugin's tools after deploying an extension; the same endpoint and OAuth login remain in use.

## Tags, rules and receipts

`get_transaction_details` reads the exact requested record without redirecting a pending transaction to its posted counterpart. `get_transaction_tags` lists names/IDs; `create_transaction_tag` reuses an existing name case-insensitively. `add_transaction_tags` preserves the tags it reads and verifies the resulting set. Writes from this server are serialized per transaction. Monarch's set-tags operation replaces the set: simultaneous edits through Monarch's own UI or another integration can still race, so avoid editing the same record elsewhere during an additive write.

`get_transaction_rules` lists native rules. `preview_transaction_rule` accepts a `rule` object and explicit `start_date`/`end_date` (YYYY-MM-DD, at most 366 days and 5,000 transactions). Conditions support merchant or original statement text (`text_operator`: `contains`/`eq`), account/category IDs and expense/income amount comparisons (`eq`/`gt`/`lt`/`between`, positive magnitudes). Actions support `category_id`, `merchant_rename`, `add_tag_ids`, `hide_from_reports` and `review_status`. Different conditions are ANDed. Preview excludes pending transactions, includes counts and up to 100 matching records, and issues a 15-minute `preview_id`. Show the preview to the operator, then call `create_transaction_rule(preview_id)`. The ID is consumed on creation. By default the native rule applies to future transactions only. Explicit `apply_to_existing=true` applies to **all matching history**, not just the preview's dates. The preview estimates this rule alone; it does not simulate interactions with other rules or future bank arrivals. Inspect the native rules before retrying an uncertain creation.

`preview_transaction_rule_update(rule_id, changes)` previews an edit to an existing native rule. Omitted fields stay unchanged; explicit null clears the named field and empty ID lists clear that restriction/action. The typed `changes` object accepts merchant/statement text, account/category lists, an `amount_criteria` object (`operator`, `is_expense`, `value` or `lower`/`upper`), category/merchant/tag actions, visibility and review status. Multiple existing text conditions and advanced owner/business/goal/split actions are retained when omitted. Legacy matching is preserved by domain: changing merchant matching does not erase original-statement matching. Show the full before/after policy preview, then call `update_transaction_rule(preview_id)`. IDs expire after 15 minutes, are single-use, and reject a rule changed since preview. Historical application defaults off; opting in affects all matching history. This edit preview does not count historical matching transactions. It is separate from creation previews.

`delete_transaction_rule(rule_id)` removes one exact native rule, leaving already changed transactions intact. `reorder_transaction_rule(rule_id, new_order)` moves one rule to a zero-based position (0 first) and verifies the entire sequence. Reordering can change future rule results. **Native rule enable/disable is not supported by Monarch's verified current schema**; no disable tool is advertised and no delete/recreate or impossible-match workaround is substituted. There is no bulk rule deletion.

`rename_transaction_tag(tag_id, name)` preserves the existing tag ID, color and order and refuses another tag's name. `delete_transaction_tag(tag_id)` deletes the tag **account-wide**, affecting all assignments and potentially native rule references; inspect those references first. `remove_transaction_tags(transaction_id, tag_ids)` removes only the selected tags from one exact transaction, preserving its other tags. This server serializes tag catalog operations and tag edits, but simultaneous edits in Monarch or other integrations can still race. Rule snapshot checks likewise cannot provide an atomic compare-and-swap against outside integrations. If a write has `verified=false`, inspect the native record before repeating; the tools never automatically retry uncertain mutations.

`upload_transaction_receipt` accepts a transaction ID and a [ChatGPT file input](https://developers.openai.com/plugins/reference#file-apis). The tool declares `openai/fileParams: ["file"]`; the file object requires `download_url` and `file_id`, with optional `mime_type` and `file_name`. Confirm the target record first. The VPS downloads the file in memory, validates its content signature and extension, uses Monarch's signed Cloudinary upload instructions, links the asset, and checks that the attachment appears. JPEG, PNG, WebP and PDF are accepted, up to 10 MiB, with a conservative three-attachment limit. Downloads reject non-HTTPS URLs, literal IP addresses, private DNS answers and unsafe redirects. Neither the Monarch cookie nor connector OAuth token is sent to the file host or Cloudinary. Files and signed URLs are not logged or written to disk.

This adds a native attachment to an **existing** transaction. It does not invoke Monarch's receipt-scanning/OCR import, automatically match a receipt, create a transaction or alter its amount. If the result says the write was accepted but readback failed, inspect Monarch before repeating.

### Claude Code local files

Keep the remote Monarch MCP connection for reads, tags and rules. Add the local receipt helper so Claude Code can use a Mac path without treating it as a VPS path:

```bash
claude mcp add --scope user --transport http monarch-money https://monarch-mcp.briansagency.com/mcp
claude mcp add --scope user --transport stdio monarch-receipts -- uv run --script /absolute/path/to/monarch-money-mcp/scripts/local_receipt_mcp.py
uv run --script /absolute/path/to/monarch-money-mcp/scripts/local_receipt_mcp.py --login
```

The helper's `upload_transaction_receipt(transaction_id, file_path)` reads the selected local file and transfers bytes through the remote OAuth-protected `upload_transaction_receipt_bytes` tool. Receipt data need not pass through the model's conversation. The first connection may open GitHub sign-in; its OAuth cache is encrypted under `~/.config/monarch-receipts`, with a mode-0600 local key and mode-0700 parent directory. Loss of that cache requires reauthorization. The key and ciphertext live on the same Mac, so directory permissions, rather than encryption alone, are the local access boundary. Restart Claude Code to discover newly configured MCP servers.

## Local tests

With Python 3.12 and uv:

```bash
uv venv --python python3.12 .venv
uv pip install --python .venv/bin/python -r requirements.lock
.venv/bin/python -m unittest discover -p 'test_*.py' -q
.venv/bin/python test_github_allowlist.py
```

The tests use synthetic secrets and do not contact Monarch.
