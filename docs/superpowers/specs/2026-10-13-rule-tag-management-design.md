# Rule and tag management extension

The operator identified the remaining gap in the already approved Monarch extension. Extend the existing single-tenant OAuth endpoint with native rule update/delete/reorder and tag rename/delete/removal from an exact transaction. Keep the URL and connector identity.

## Behavior

Rule update uses a typed partial change object. Omitted fields preserve the stored rule; explicit null/empty values clear only the requested field. Fetch every field in Monarch's current TransactionRuleFields fragment before editing, including legacy merchant criteria, owner/business criteria/actions, goals, notification/reviewer flags and splits. The unassignNeedsReviewByUserAction output flag is not an input field: explicit null reviewer encodes unassign, omission encodes no reviewer action, and an ID encodes assignment; readback verifies the difference. Serialize output entities to native IDs (merchant action uses its name), remove GraphQL typenames, and resend all actions because Monarch clears omitted actions. Refuse an incomplete snapshot.

A read-only update preview returns full before/after native inputs and a 15-minute one-use ID. It describes policy changes, not a historical transaction simulation. Update consumes that ID, checks a semantic snapshot for external edits, and defaults historical application off. Creation and editing previews are not interchangeable. Unknown transport outcomes never automatically retry.

Delete a single rule by exact ID; verify absence rather than trusting Monarch's unreliable deleted flag. Move one rule to a validated zero-based position in the complete ordered list; verify the entire resulting ID sequence. Serialize rule writes inside this process. Never infer success merely from an accepted response.

Rename a tag by exact ID, preserving its color and order, refusing a duplicate name. Delete a tag account-wide, explicitly describing that removal affects every tagged transaction and potentially native rule references; verify absence. Removing specific tag IDs from a transaction preserves every other tag and reads the exact transaction, without pending-to-posted redirection. Serialize catalog operations and transaction tag edits.

## Evidence and limits

Primary source: https://static.monarch.com/static/js/main.b128d232.js (Monarch's current web client), plus read-only authenticated field queries and skipped GraphQL mutation schema validation. Verified native schemas for rule update/delete/reorder and tag update/delete. Existing set_transaction_tags handles removal. Current web rule fragment and mutation catalogue contain no native enable/disable field/action. Do not simulate native disable with a made-up flag, deleting rules, or sentinel criteria. This remains a documented platform limitation.

No actual financial mutation is authorized for verification without an operator-selected target. Synthetic behavioral tests, live schema checks with @skip(if:true), authenticated reads and production tool discovery are the acceptance evidence.
