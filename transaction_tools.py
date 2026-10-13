"""Narrow transaction writes and required previews for native Monarch rules."""
from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Literal

from gql import gql
from pydantic import BaseModel, ConfigDict, Field, model_validator


def meaningful_errors(value):
    if isinstance(value, dict):
        return any(meaningful_errors(v) for k, v in value.items() if k != "__typename")
    if isinstance(value, list):
        return any(meaningful_errors(v) for v in value)
    return value not in (None, "", False)


def checked_payload(data, key):
    payload = data.get(key) if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("Monarch returned an incomplete response")
    if meaningful_errors(payload.get("errors")):
        raise ValueError("Monarch rejected the requested change; no success was confirmed")
    return payload


def require_id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("A nonempty Monarch ID is required")
    return value.strip()


class RuleSpec(BaseModel):
    """Conditions are ANDed; original_statement/merchant_name use case-insensitive matching."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    merchant_name: str | None = Field(default=None, min_length=1, max_length=500)
    original_statement: str | None = Field(default=None, min_length=1, max_length=500)
    text_operator: Literal["contains", "eq"] = "contains"
    account_ids: list[str] = Field(default_factory=list, max_length=100)
    category_ids: list[str] = Field(default_factory=list, max_length=100)
    amount_operator: Literal["eq", "gt", "lt", "between"] | None = None
    amount_value: float | None = Field(default=None, ge=0)
    amount_lower: float | None = Field(default=None, ge=0)
    amount_upper: float | None = Field(default=None, ge=0)
    amount_is_expense: bool = True
    category_id: str | None = Field(default=None, min_length=1, max_length=128)
    merchant_rename: str | None = Field(default=None, min_length=1, max_length=500)
    add_tag_ids: list[str] = Field(default_factory=list, max_length=100)
    hide_from_reports: bool | None = None
    review_status: Literal["needs_review", "reviewed"] | None = None

    @model_validator(mode="after")
    def validate_rule(self):
        for name in ("account_ids", "category_ids", "add_tag_ids"):
            values = [require_id(v) for v in getattr(self, name)]
            setattr(self, name, list(dict.fromkeys(values)))
        if not (self.merchant_name or self.original_statement or self.account_ids
                or self.category_ids or self.amount_operator):
            raise ValueError("A rule requires at least one matching condition")
        if not (self.category_id or self.merchant_rename or self.add_tag_ids
                or self.hide_from_reports is not None or self.review_status):
            raise ValueError("A rule requires at least one action")
        if self.amount_operator == "between":
            if self.amount_lower is None or self.amount_upper is None or self.amount_lower > self.amount_upper:
                raise ValueError("between requires ordered amount_lower and amount_upper")
            if self.amount_value is not None:
                raise ValueError("between does not accept amount_value")
        elif self.amount_operator:
            if self.amount_value is None or self.amount_lower is not None or self.amount_upper is not None:
                raise ValueError("Amount comparison requires only amount_value")
        elif any(v is not None for v in (self.amount_value, self.amount_lower, self.amount_upper)):
            raise ValueError("Amount values require amount_operator")
        return self

    def native_input(self):
        result = {}
        for field, native in (("merchant_name", "merchantNameCriteria"),
                              ("original_statement", "originalStatementCriteria")):
            value = getattr(self, field)
            if value:
                result[native] = [{"operator": self.text_operator, "value": value}]
        for field, native in (("account_ids", "accountIds"), ("category_ids", "categoryIds"),
                              ("category_id", "setCategoryAction"), ("merchant_rename", "setMerchantAction"),
                              ("add_tag_ids", "addTagsAction"), ("hide_from_reports", "setHideFromReportsAction"),
                              ("review_status", "reviewStatusAction")):
            value = getattr(self, field)
            if value is not None and value != []:
                result[native] = value
        if self.amount_operator:
            amount = {"operator": self.amount_operator, "isExpense": self.amount_is_expense}
            if self.amount_operator == "between":
                amount["valueRange"] = {"lower": self.amount_lower, "upper": self.amount_upper}
            else:
                amount["value"] = self.amount_value
            result["amountCriteria"] = amount
        return result

    def matches(self, txn):
        if not isinstance(txn.get("pending"), bool):
            raise ValueError("Preview transaction is missing pending status")
        if txn["pending"]:
            return False
        for field, value in (("merchant", self.merchant_name), ("plaidName", self.original_statement)):
            if value:
                raw = txn.get(field)
                if field == "merchant":
                    raw = raw.get("name") if isinstance(raw, dict) else None
                if not isinstance(raw, str):
                    raise ValueError("Preview transaction is missing matching text")
                matches = value.casefold() in raw.casefold() if self.text_operator == "contains" else value.casefold() == raw.casefold()
                if not matches:
                    return False
        for field, ids in (("account", self.account_ids), ("category", self.category_ids)):
            if ids:
                entity = txn.get(field)
                if not isinstance(entity, dict) or not entity.get("id"):
                    raise ValueError("Preview transaction is missing account/category ID")
                if str(entity["id"]) not in ids:
                    return False
        if self.amount_operator:
            try:
                amount = Decimal(str(txn["amount"]))
                if not amount.is_finite():
                    raise ValueError()
            except (KeyError, ValueError, ArithmeticError):
                raise ValueError("Preview transaction is missing a valid amount") from None
            if (amount < 0) != self.amount_is_expense:
                return False
            amount = abs(amount)
            if self.amount_operator == "between":
                return Decimal(str(self.amount_lower)) <= amount <= Decimal(str(self.amount_upper))
            value = Decimal(str(self.amount_value))
            return {"eq": amount == value, "gt": amount > value, "lt": amount < value}[self.amount_operator]
        return True


RULES_QUERY = gql("""
query GetTransactionRules {
 transactionRules {
  id order merchantCriteriaUseOriginalStatement
  merchantCriteria {operator value}
  merchantNameCriteria {operator value}
  originalStatementCriteria {operator value}
  amountCriteria {operator isExpense value valueRange {lower upper}}
  accountIds categoryIds criteriaOwnerIsJoint criteriaOwnerUserIds
  criteriaBusinessEntityIds criteriaBusinessEntityIsUnassigned
  setCategoryAction {id name} setMerchantAction {id name} addTagsAction {id name}
  linkGoalAction {id name} linkSavingsGoalAction {id name}
  needsReviewByUserAction {id displayName} unassignNeedsReviewByUserAction
  setHideFromReportsAction reviewStatusAction sendNotificationAction setLinkToPaydownBudgetAction
  actionSetOwnerIsJoint actionSetOwner {id displayName}
  actionSetBusinessEntity {id name} actionSetBusinessEntityIsUnassigned
  splitTransactionsAction {
   amountType splitsInfo {
    categoryId merchantName amount goalId savingsGoalId tags hideFromReports
    reviewStatus needsReviewByUserId ownerUserId ownerIsJoint businessEntityId businessEntityIsUnassigned
   }
  }
 }
}
""")
CREATE_RULE = gql("""
mutation Common_CreateTransactionRuleMutationV2($input: CreateTransactionRuleInput!) {
  createTransactionRuleV2(input: $input) {
    transactionRule { id order }
    errors { message code fieldErrors { field messages } }
  }
}
""")


class TransactionTools:
    def __init__(self, client, call, *, clock=time.monotonic):
        self.client, self.call, self.clock = client, call, clock
        self._locks = {}
        self._previews = {}
        self._tag_creation_lock = asyncio.Lock()
        self._rule_write_lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction_lock(self, transaction_id):
        lock, count = self._locks.get(transaction_id, (asyncio.Lock(), 0))
        self._locks[transaction_id] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            _, count = self._locks[transaction_id]
            if count == 1:
                del self._locks[transaction_id]
            else:
                self._locks[transaction_id] = (lock, count - 1)

    async def api(self, fn, **kwargs):
        try:
            return await self.call(fn, **kwargs)
        except Exception:
            raise ValueError("Monarch request failed; check the session and retry reads only. A write may have completed; inspect before repeating.") from None

    async def get_transaction_details(self, transaction_id: str) -> dict:
        """Read the exact transaction, including tags/attachments, without pending-to-posted redirection."""
        transaction_id = require_id(transaction_id)
        data = await self.api(self.client.get_transaction_details, transaction_id=transaction_id, redirect_posted=False)
        txn = data.get("getTransaction") if isinstance(data, dict) else None
        if not isinstance(txn, dict) or str(txn.get("id")) != transaction_id:
            raise ValueError("Monarch did not return the exact requested transaction")
        return txn

    async def get_transaction_tags(self) -> dict:
        """List available tag IDs, names and colors."""
        data = await self.api(self.client.get_transaction_tags)
        tags = data.get("householdTransactionTags") if isinstance(data, dict) else None
        if not isinstance(tags, list) or any(not isinstance(t, dict) or not t.get("id") for t in tags):
            raise ValueError("Monarch returned an incomplete tag list")
        return {"tags": tags}

    async def create_transaction_tag(self, name: str, color: str = "#6366f1") -> dict:
        """Create a named tag; reuse a tag with the same name rather than duplicate it."""
        async with self._tag_creation_lock:
            return await self._create_transaction_tag(name, color)

    async def _create_transaction_tag(self, name, color):
        import re
        name = name.strip()
        if not name or len(name) > 100 or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Tag name must be 1–100 characters and color a six-digit hex code")
        tags = (await self.get_transaction_tags())["tags"]
        for tag in tags:
            if str(tag.get("name", "")).casefold() == name.casefold():
                return {"created": False, "tag": tag}
        payload = checked_payload(await self.api(self.client.create_transaction_tag, name=name, color=color), "createTransactionTag")
        tag = payload.get("tag")
        # The base library calls this field transactionTag on some versions.
        if not isinstance(tag, dict):
            tag = payload.get("transactionTag")
        if not isinstance(tag, dict) or not tag.get("id"):
            raise ValueError("Tag write did not return an ID; inspect before repeating")
        return {"created": True, "tag": tag}

    async def validate_tag_ids(self, tag_ids):
        if not isinstance(tag_ids, list) or len(tag_ids) > 100:
            raise ValueError("Provide at most 100 tag IDs")
        wanted = list(dict.fromkeys(require_id(v) for v in tag_ids))
        known = {str(t["id"]) for t in (await self.get_transaction_tags())["tags"]}
        if not set(wanted) <= known:
            raise ValueError("Unknown tag ID; list available tags first")
        return wanted

    async def add_transaction_tags(self, transaction_id: str, tag_ids: list[str]) -> dict:
        """Add tag IDs while preserving existing tags. Read/verify the exact transaction."""
        async with self._tag_creation_lock:
            return await self._add_transaction_tags(transaction_id, tag_ids)

    async def _add_transaction_tags(self, transaction_id, tag_ids):
        transaction_id = require_id(transaction_id)
        wanted = await self.validate_tag_ids(tag_ids)
        if not wanted:
            raise ValueError("Provide at least one tag ID to add")
        async with self.transaction_lock(transaction_id):
            txn = await self.get_transaction_details(transaction_id)
            existing = txn.get("tags")
            if not isinstance(existing, list) or any(not isinstance(t, dict) or not t.get("id") for t in existing):
                raise ValueError("Existing tags could not be read; refusing to replace them")
            merged = list(dict.fromkeys([str(t["id"]) for t in existing] + wanted))
            if set(merged) == {str(t["id"]) for t in existing}:
                return {"transaction_id": transaction_id, "tag_ids": merged, "changed": False, "verified": True}
            checked_payload(await self.api(self.client.set_transaction_tags, transaction_id=transaction_id, tag_ids=merged), "setTransactionTags")
            try:
                after = await self.get_transaction_details(transaction_id)
                verified = {str(t["id"]) for t in after["tags"]} == set(merged)
            except (ValueError, KeyError, TypeError):
                verified = False
            return {"transaction_id": transaction_id, "tag_ids": merged, "changed": True, "verified": verified,
                    "message": "Tags added" if verified else "Write accepted but readback did not confirm the tag set; inspect before repeating"}

    async def get_transaction_rules(self) -> dict:
        """List native Monarch rules in order, with conditions and actions."""
        data = await self.api(self.client.gql_call, operation="GetTransactionRules", graphql_query=RULES_QUERY)
        rules = data.get("transactionRules") if isinstance(data, dict) else None
        if not isinstance(rules, list):
            raise ValueError("Monarch did not return a rule list")
        return {"rules": rules}

    async def preview_transaction_rule(self, rule: RuleSpec, start_date: str, end_date: str) -> dict:
        """Preview one proposed rule against a complete date window (max 366 days/5000 transactions).

        This estimates this rule alone, excluding pending transactions; other rules and future
        arrivals may change the result. Show this preview to the user before creating the rule.
        A complete preview produces a creation ID valid for 15 minutes, lost on server restart.
        """
        try:
            start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except ValueError:
            raise ValueError("Use YYYY-MM-DD preview dates") from None
        if start.isoformat() != start_date or end.isoformat() != end_date:
            raise ValueError("Use YYYY-MM-DD preview dates")
        if start > end or (end - start).days > 366:
            raise ValueError("Preview window must be ordered and no longer than 366 days")
        if rule.add_tag_ids:
            await self.validate_tag_ids(rule.add_tag_ids)
        records, seen, total = [], set(), None
        while total is None or len(records) < total:
            data = await self.api(self.client.get_transactions, start_date=start_date, end_date=end_date,
                                  offset=len(records), limit=500)
            page = data.get("allTransactions") if isinstance(data, dict) else None
            if not isinstance(page, dict) or not isinstance(page.get("totalCount"), int):
                raise ValueError("Preview could not confirm all pages")
            if total is None:
                total = page["totalCount"]
            if total < 0 or total > 5000 or page["totalCount"] != total:
                raise ValueError("Preview exceeds 5000 records or changed during scan; narrow the dates and retry")
            rows = page.get("results")
            if not isinstance(rows, list) or (not rows and len(records) < total):
                raise ValueError("Preview ended before all records were read")
            for row in rows:
                row_id = require_id(row.get("id")) if isinstance(row, dict) else None
                if not row_id or row_id in seen:
                    raise ValueError("Preview encountered a duplicate or incomplete transaction")
                seen.add(row_id)
                records.append(row)
            if len(records) > total:
                raise ValueError("Preview record count changed during scan")
        matches = [row for row in records if rule.matches(row)]
        now = self.clock()
        self._previews = {key: value for key, value in self._previews.items() if value[0] > now}
        if len(self._previews) >= 100:
            raise ValueError("Too many active previews; wait for one to expire")
        preview_id = secrets.token_urlsafe(24)
        self._previews[preview_id] = (now + 900, rule.native_input())
        return {"preview_id": preview_id, "expires_in_seconds": 900, "start_date": start_date, "end_date": end_date,
                "scanned_count": len(records), "match_count": len(matches), "matches": matches[:100],
                "matches_shown": min(100, len(matches)), "actions": rule.native_input(),
                "scope": "This date window only. Applying to existing transactions when creating the rule affects ALL matching history, not only these dates."}

    async def create_transaction_rule(self, preview_id: str, apply_to_existing: bool = False) -> dict:
        """Create the exact native rule from a user-reviewed preview ID. Default: future transactions only.

        apply_to_existing=True affects ALL matching history, beyond the preview's dates.
        IDs are consumed before the mutation; inspect rules before retrying an uncertain write.
        """
        async with self._rule_write_lock:
            return await self._create_transaction_rule(preview_id, apply_to_existing)

    async def _create_transaction_rule(self, preview_id, apply_to_existing):
        preview = self._previews.pop(preview_id, None)
        if preview is None or preview[0] <= self.clock():
            raise ValueError("Preview is missing, consumed or expired; preview again before creating")
        data = await self.api(self.client.gql_call, operation="Common_CreateTransactionRuleMutationV2",
                              graphql_query=CREATE_RULE,
                              variables={"input": {**preview[1], "applyToExistingTransactions": apply_to_existing}})
        payload = checked_payload(data, "createTransactionRuleV2")
        native = payload.get("transactionRule")
        if not isinstance(native, dict) or not native.get("id"):
            raise ValueError("Rule write did not return an ID; inspect native rules before repeating")
        try:
            rules = (await self.get_transaction_rules())["rules"]
            verified = any(str(r.get("id")) == str(native["id"]) for r in rules)
        except ValueError:
            verified = False
        return {"created": True, "rule_id": native["id"], "apply_to_existing": apply_to_existing,
                "verified": verified, "message": "Rule created" if verified else "Rule created but readback unavailable; inspect before repeating"}


def register_transaction_tools(mcp, client, call):
    service = TransactionTools(client, call)
    reads = ("get_transaction_details", "get_transaction_tags", "get_transaction_rules", "preview_transaction_rule")
    for name in reads + ("create_transaction_tag", "add_transaction_tags", "create_transaction_rule"):
        mcp.tool(getattr(service, name), name=name, annotations={
            "readOnlyHint": name in reads, "destructiveHint": name == "create_transaction_rule",
            "openWorldHint": False, "idempotentHint": name != "create_transaction_rule",
        })
    return service
