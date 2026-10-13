# Monarch transaction tools implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add tags, native rules and real receipt attachments to the current Monarch connector.
**Architecture:** Focused tag/rule and attachment modules register tools on the existing FastMCP server and use its secure Monarch call path. A local helper bridges Claude Code file paths to the remote byte upload tool.
**Tech Stack:** Python 3.12, pinned FastMCP 4.0.0b3, monarchmoney, aiohttp, Pydantic.
**Spec:** ../specs/2026-10-13-transaction-tools-design.md

## Global constraints

- Preserve mounted cookie and GitHub OAuth allowlisting; add no unauthenticated financial route.
- Files: JPEG/PNG/WebP/PDF, at most 10 MiB; never log contents, credentials or signed URLs.
- Creation requires a complete rule preview; applying to past transactions defaults false.
- No production financial mutation without a specific operator-approved record.

## Review focus

- Tag reads returning the wrong transaction must never overwrite another transaction's tags.
- Preview scans missing pages or exceeding 5,000 records must not issue a creation ID.
- Failed/uncertain writes must not be retried automatically.
- DNS rebinding, redirects to private IPs and compressed oversized downloads must fail before sending account secrets.
- OpenAI file metadata must declare all four properties and require only file_id/download_url.

### Task 1: Tag and rule tools

Files: create transaction_tools.py and test_transaction_tools.py; modify server.py and CI workflow.
Interfaces: register_transaction_tools(mcp, mm, call) registers get_transaction_details/get_transaction_tags/create_transaction_tag/add_transaction_tags/get_transaction_rules/preview_transaction_rule/create_transaction_rule.

- [x] Write tests for additive tags, exact IDs, upstream errors, rule matching, pagination, preview expiry and default non-retroactive creation.
- [x] Run tests and observe failure because registration/module is absent.
- [x] Implement validation, native GraphQL queries, transaction locks, preview tokens and readback.
- [x] Run the new tests and full existing suites.

### Task 2: Receipt uploads

Files: create attachments.py and test_attachments.py; modify server.py and Dockerfile.
Interfaces: register_attachment_tools(mcp, mm, call, transaction_lock) registers upload_transaction_receipt(file, transaction_id) and upload_transaction_receipt_bytes(transaction_id, data_base64, filename).

- [x] Write tests for signatures and size limits, safe URL resolution, upload/link/readback, malformed upload parameters and rejected payloads.
- [x] Observe failing tests before implementation.
- [x] Implement bounded downloads, signed Cloudinary upload and attachment linking.
- [x] Verify schema metadata through actual FastMCP tool discovery and run all suites.

### Task 3: Claude Code helper and deployment

Files: create scripts/local_receipt_mcp.py and its tests; update README/CLAUDE.md, CI, and infra runbook/history in isolated infra worktree.
Interfaces: local upload_transaction_receipt(transaction_id, file_path) reads a bounded local file and invokes remote upload_transaction_receipt_bytes through FastMCP OAuth.

- [x] Test local path/size checks and byte-transfer contract without a real financial write.
- [x] Implement helper and install/configure it for local Claude Code.
- [x] Review complete diff, run full suites, and build/verify container boundary.
- [x] Commit/push through the workspace's commit mechanism, deploy existing Coolify app, verify running tools and read queries. Infra documentation and worktree cleanup are recorded in CHANGELOG item 345.

## Execution rulings and ledger

The user's 'let's do it' authorizes the chat description and inline execution. No additional plan-method choice is necessary under the developer autonomy instructions. This file is the execution ledger; update checkboxes and verification evidence here. Full baseline: security 19/19 and GitHub allowlist 7/7 passed on 2026-10-13.

Verification 2026-10-13: full unit discovery 46/46; GitHub allowlist 7/7. Live read-only validation: 9 tags, 28 native rules, exact transaction/detail shapes accepted. Upload-info credentials were obtained without uploading or linking anything; current path is /v1_1/monarch-money/image/upload/ and the trailing slash is covered by a regression test. Schema introspection was refused, so native mutation execution remains unverified until an operator approves a specific live record. The container preflight build succeeded; repeat after final review fixes before deployment.

Review ruling: normalize file URLs with yarl before validating hostnames; otherwise Unicode digits could become a loopback literal after validation and bypass the connection resolver. The reviewer reproduced this offline, then the regression test failed before the fix and passed after it.

Final pre-deployment evidence: full discovery 48/48 and allowlist 7/7. Fresh reviewer confirmed three resolved findings (Unicode IP normalization, atomic OAuth cache key publication, concurrent same-name tag creation). Rebuilt image on VPS with synthetic secrets and no networking: UID 10001, health 200, anonymous MCP 401, 20 runtime tools, fileParams metadata and required download_url/file_id verified. No financial write performed.

Local client setup: monarch-money (remote HTTP) and monarch-receipts (local stdio) added to the operator's user-level Claude Code config. Actual local tool discovery finds upload_transaction_receipt. First OAuth sign-in revealed FileTreeStore treating server-URL keys as nested paths; a real TokenStorageAdapter regression reproduced it. Explicit AlwaysHashStrategy fixes keys; 49 tests pass. OAuth sign-in then completed using the encrypted cache, without uploading a file. Production core deploy hqpp75gs4ma4l9u7kl9kqha2 is in progress from source cc7e657.

Deployment verified: Coolify hqpp75gs4ma4l9u7kl9kqha2 finished with core cc7e657; container healthy, runtime hashes match, external boundary 8/8. Authenticated remote client discovers 20 tools, reads 9 tags/28 rules, and previews 113 transactions with zero matches without a native write. Both BetterStack monitors up. ChatGPT app refreshed and its dialog visibly lists the new actions (Write7/Read13). Local helper fix CI passed; restart/reconnect Claude Code to load its new user MCP entries. Actual native financial writes remain unverified until specifically authorized; the deployed extension is available for normal approved use.
