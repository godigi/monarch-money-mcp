# Rule and tag management implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline; a fresh reviewer checks the completed branch.

**Goal:** Make existing native rules and tags manageable through the same connector.
**Architecture:** Add rule_management.py for typed changes, complete snapshots, native GraphQL operations and registration. Reuse TransactionTools API, catalog/transaction locks and a shared rule-write lock.
**Tech Stack:** Python 3.12, Pydantic, gql, FastMCP, unittest.
**Spec:** ../specs/2026-10-13-rule-tag-management-design.md

## Constraints and review focus

- Preserve all unedited native fields, especially legacy merchant matching and advanced actions.
- Preview IDs are one-use, expire after 900 seconds and reject concurrent/external edits.
- Readback failure is unverified, not success; uncertainty must never trigger retries.
- Complete rule ordering and unknown/duplicate IDs must be checked before reorder.
- Concurrent add/remove/rename/delete must use consistent lock ordering.
- No native disable action is fabricated; no live financial writes in validation.

## Tasks

- [x] Write test_rule_management.py first. Test partial update preservation, clearing, stale/expired/reused/wrong preview IDs, input validation, uncertainty, rejected mutations, delete readback, reorder full sequence, rename collision/color preservation, tag deletion scope, exact transaction removal and concurrent tag edits.
- [x] Run it and confirm missing capability failures against the baseline.
- [x] Add rule_management.py and complete native rule query; register tools and shared write locks in transaction_tools.py/server.py.
- [x] Run the full suite; include the new suite explicitly in CI. Review schemas and tool annotations.
- [x] Obtain fresh code review; fix significant findings with regression tests.
- [ ] Commit via Nimbalyst and push immediately, fast-forward source main, deploy the existing Coolify application. Verify image revision, OAuth guards, tool catalogue and authenticated read-only previews.
- [ ] Refresh the existing ChatGPT plugin; update operator docs, VPS_STATUS and append numbered CHANGELOG item. Commit/push infra docs; remove merged feature branches/worktrees.

## Execution ledger

Baseline: source main 3d818b9, 49 tests pass. Expanded native rule read returns 28 complete rules. Skipped mutation schema validation returns {}. Native disable is unsupported by the verified current web/API interface.
Ruling: treat the explicit list of missing actions as a correction to the approved extension; continue implementation without another scope survey. No actual account cleanup occurs during this work.

Review: three preservation defects fixed with red/green regressions; fresh reviewer reports no remaining blocker. Final suite 85 tests plus 7 allowlist checks pass. Synthetic no-network Docker image builds, runs UID10001, advertises27 tools, health200, anonymous MCP401. Native input schema validated with full snapshot; unassign reviewer flag proved output-only and excluded from input.
