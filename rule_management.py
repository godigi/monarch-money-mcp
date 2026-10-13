"""Native rule and tag maintenance; previews preserve complete rule snapshots."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
from typing import Literal

from gql import gql
from pydantic import BaseModel, ConfigDict, Field, model_validator
from transaction_tools import checked_payload, require_id


class AmountChange(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    operator: Literal['eq', 'gt', 'lt', 'between']
    is_expense: bool = True
    value: float | None = Field(default=None, ge=0)
    lower: float | None = Field(default=None, ge=0)
    upper: float | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def valid_amount(self):
        if self.operator == 'between':
            if self.lower is None or self.upper is None or self.lower > self.upper or self.value is not None:
                raise ValueError('between requires ordered lower/upper only')
        elif self.value is None or self.lower is not None or self.upper is not None:
            raise ValueError('Amount comparison requires value only')
        return self

    def native(self):
        return {'operator': self.operator, 'isExpense': self.is_expense, 'value': self.value,
                'valueRange': {'lower': self.lower, 'upper': self.upper} if self.operator == 'between' else None}


class RuleChanges(BaseModel):
    """Omit unchanged fields. Explicit null clears a field; [] clears ID lists."""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    merchant_name: str | None = Field(default=None, min_length=1, max_length=500)
    original_statement: str | None = Field(default=None, min_length=1, max_length=500)
    text_operator: Literal['contains', 'eq'] = 'contains'
    account_ids: list[str] | None = Field(default=None, max_length=100)
    category_ids: list[str] | None = Field(default=None, max_length=100)
    amount_criteria: AmountChange | None = None
    category_id: str | None = Field(default=None, min_length=1, max_length=128)
    merchant_rename: str | None = Field(default=None, min_length=1, max_length=500)
    add_tag_ids: list[str] | None = Field(default=None, max_length=100)
    hide_from_reports: bool | None = None
    review_status: Literal['needs_review', 'reviewed'] | None = None

    @model_validator(mode='after')
    def valid_changes(self):
        supplied = self.model_fields_set
        if not supplied or supplied == {'text_operator'}:
            raise ValueError('Provide at least one rule field to change')
        if 'text_operator' in supplied and not ({'merchant_name', 'original_statement'} & supplied):
            raise ValueError('text_operator requires a new merchant_name or original_statement')
        for field in ('account_ids', 'category_ids', 'add_tag_ids'):
            if getattr(self, field) is not None:
                setattr(self, field, list(dict.fromkeys(require_id(v) for v in getattr(self, field))))
        if self.category_id is not None:
            self.category_id = require_id(self.category_id)
        return self


# Matches every editable field in Monarch's current TransactionRuleFields fragment.
LIST_FIELDS = ('merchantCriteria', 'merchantNameCriteria', 'originalStatementCriteria',
               'accountIds', 'categoryIds', 'criteriaOwnerUserIds', 'criteriaBusinessEntityIds')
BOOL_FIELDS = ('merchantCriteriaUseOriginalStatement', 'setHideFromReportsAction',
               'sendNotificationAction', 'setLinkToPaydownBudgetAction', 'criteriaOwnerIsJoint',
               'criteriaBusinessEntityIsUnassigned', 'actionSetOwnerIsJoint',
               'actionSetBusinessEntityIsUnassigned')
ENTITY_FIELDS = ('setCategoryAction', 'linkGoalAction', 'linkSavingsGoalAction',
                 'needsReviewByUserAction', 'actionSetOwner', 'actionSetBusinessEntity')
REQUIRED_FIELDS = {*LIST_FIELDS, *BOOL_FIELDS, *ENTITY_FIELDS, 'setMerchantAction',
                   'addTagsAction', 'amountCriteria', 'reviewStatusAction', 'splitTransactionsAction', 'unassignNeedsReviewByUserAction'}


def clean(value):
    if isinstance(value, dict): return {k: clean(v) for k, v in value.items() if k != '__typename'}
    if isinstance(value, list): return [clean(v) for v in value]
    return value


def native_snapshot(rule):
    if not REQUIRED_FIELDS.issubset(rule):
        raise ValueError('Rule snapshot is incomplete; refusing to lose unedited settings')
    result = {}
    for field in LIST_FIELDS:
        value = rule[field]
        if value is not None and not isinstance(value, list):
            raise ValueError('Rule snapshot is incomplete')
        result[field] = clean(value or [])
    for field in BOOL_FIELDS:
        value = rule[field]
        if value is not None and not isinstance(value, bool):
            raise ValueError('Rule snapshot is incomplete')
        result[field] = value if value is not None else False
    for field in ENTITY_FIELDS:
        value = rule[field]
        if value is not None and (not isinstance(value, dict) or not value.get('id')):
            raise ValueError('Rule snapshot is incomplete')
        result[field] = require_id(value['id']) if value else None
    merchant = rule['setMerchantAction']
    if merchant is not None and (not isinstance(merchant, dict) or not isinstance(merchant.get('name'), str) or not merchant['name']):
        raise ValueError('Rule snapshot is incomplete')
    result['setMerchantAction'] = merchant['name'] if merchant else None
    tags = rule['addTagsAction']
    if tags is not None and (not isinstance(tags, list) or any(not isinstance(t, dict) or not t.get('id') for t in tags)):
        raise ValueError('Rule snapshot is incomplete')
    result['addTagsAction'] = [require_id(t['id']) for t in tags or []]
    for field in ('amountCriteria', 'reviewStatusAction', 'splitTransactionsAction'):
        result[field] = clean(rule[field])
    unassign = rule['unassignNeedsReviewByUserAction']
    if not isinstance(unassign, bool):
        raise ValueError('Rule snapshot is incomplete')
    reviewer = result.get('needsReviewByUserAction')
    if result['reviewStatusAction'] != 'needs_review':
        if reviewer is not None or unassign:
            raise ValueError('Rule reviewer state cannot be preserved with this review status')
        result.pop('needsReviewByUserAction', None)
    elif unassign:
        if reviewer is not None:
            raise ValueError('Rule reviewer state is inconsistent')
        result['needsReviewByUserAction'] = None
    elif reviewer is None:
        # Omission is an inactive assignment; explicit null is the native unassign action.
        result.pop('needsReviewByUserAction', None)
    return result


def fingerprint(rule):
    value = {'input': native_snapshot(rule), 'order': rule.get('order'), 'unassignReviewer': rule['unassignNeedsReviewByUserAction']}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def changed_input(before, changes):
    after = copy.deepcopy(before)
    fields = changes.model_fields_set
    for field, native in (('merchant_name', 'merchantNameCriteria'), ('original_statement', 'originalStatementCriteria')):
        if field in fields:
            value = getattr(changes, field)
            after[native] = [{'operator': changes.text_operator, 'value': value}] if value is not None else []
    # Replacing merchant matching must clear the legacy condition, which otherwise ANDs back in.
    legacy_domain = 'original_statement' if before['merchantCriteriaUseOriginalStatement'] else 'merchant_name'
    if legacy_domain in fields:
        after['merchantCriteria'] = []
        after['merchantCriteriaUseOriginalStatement'] = False
    if 'amount_criteria' in fields:
        after['amountCriteria'] = changes.amount_criteria.native() if changes.amount_criteria else None
    for field, native in (('account_ids', 'accountIds'), ('category_ids', 'categoryIds'),
                          ('add_tag_ids', 'addTagsAction'), ('category_id', 'setCategoryAction'),
                          ('merchant_rename', 'setMerchantAction'), ('review_status', 'reviewStatusAction'),
                          ('hide_from_reports', 'setHideFromReportsAction')):
        if field in fields:
            value = getattr(changes, field)
            after[native] = value if value is not None else ([] if native in ('accountIds', 'categoryIds', 'addTagsAction') else False if native == 'setHideFromReportsAction' else None)
    if 'review_status' in fields and changes.review_status != 'needs_review':
        after.pop('needsReviewByUserAction', None)
    if not any(after.get(k) for k in (*LIST_FIELDS, 'amountCriteria', 'criteriaOwnerIsJoint', 'criteriaBusinessEntityIsUnassigned')):
        raise ValueError('An updated rule requires at least one matching condition')
    active_actions = (*ENTITY_FIELDS, 'setMerchantAction', 'addTagsAction', 'reviewStatusAction',
                      'splitTransactionsAction', 'setHideFromReportsAction', 'sendNotificationAction',
                      'setLinkToPaydownBudgetAction', 'actionSetOwnerIsJoint', 'actionSetBusinessEntityIsUnassigned')
    if not any(after.get(k) for k in active_actions):
        raise ValueError('An updated rule requires at least one action')
    return after


UPDATE_RULE = gql('''mutation Common_UpdateTransactionRuleMutationV2($input: UpdateTransactionRuleInput!) {
 updateTransactionRuleV2(input:$input) {transactionRule {id order} errors {message code fieldErrors {field messages}}}
}''')
DELETE_RULE = gql('''mutation Common_DeleteTransactionRule($id:ID!) {
 deleteTransactionRule(id:$id) {deleted errors {message code fieldErrors {field messages}}}
}''')
MOVE_RULE = gql('''mutation Web_UpdateRuleOrderMutation($id:ID!, $order:Int!) {
 updateTransactionRuleOrderV2(id:$id,order:$order) {transactionRules {id order}}
}''')
RENAME_TAG = gql('''mutation Common_UpdateTransactionTag($input:UpdateTransactionTagInput!) {
 updateTransactionTag(input:$input) {tag {id name color order} errors {message}}
}''')
DELETE_TAG = gql('''mutation Common_DeleteHouseholdTransactionTag($tagId:ID!) {
 deleteTransactionTag(tagId:$tagId) {errors {message code fieldErrors {field messages}}}
}''')


class RuleManagement:
    def __init__(self, transactions):
        self.tx = transactions
        self._previews = {}

    async def rules(self):
        rules = (await self.tx.get_transaction_rules())['rules']
        seen = set()
        for rule in rules:
            if not isinstance(rule, dict) or not rule.get('id') or str(rule['id']) in seen:
                raise ValueError('Rule list is incomplete or contains duplicate IDs')
            if type(rule.get('order')) is not int or rule['order'] < 0:
                raise ValueError('Rule order could not be read completely')
            seen.add(str(rule['id']))
        if len({r['order'] for r in rules}) != len(rules):
            raise ValueError('Rule order is ambiguous')
        return sorted(rules, key=lambda r:r['order'])

    async def find_rule(self, rule_id):
        return next((r for r in await self.rules() if str(r['id']) == rule_id), None)

    async def preview_transaction_rule_update(self, rule_id: str, changes: RuleChanges) -> dict:
        """Preview before/after policy for an existing native rule; no transactions are changed.

        Omitted changes preserve fields; null clears only that field, [] clears ID lists.
        Text edits replace legacy matching only in the same condition domain. Advanced actions
        are preserved. This is a policy preview, not a historical match simulation.
        Show it to the user before updating. The one-use ID expires in 15 minutes.
        """
        rule_id = require_id(rule_id)
        rule = await self.find_rule(rule_id)
        if rule is None: raise ValueError('Unknown rule ID; list rules first')
        before = native_snapshot(rule)
        after = changed_input(before, changes)
        if 'add_tag_ids' in changes.model_fields_set and changes.add_tag_ids:
            await self.tx.validate_tag_ids(changes.add_tag_ids)
        now = self.tx.clock()
        self._previews = {k:v for k,v in self._previews.items() if v['expires'] > now}
        if len(self._previews) >= 100: raise ValueError('Too many active previews; wait for expiration')
        preview_id = secrets.token_urlsafe(24)
        self._previews[preview_id] = {'expires':now+900, 'rule_id':rule_id, 'fingerprint':fingerprint(rule), 'after':after}
        return {'preview_id':preview_id, 'rule_id':rule_id, 'expires_in_seconds':900,
                'before':before, 'after':after, 'scope':'Future transactions by default. apply_to_existing=True affects ALL matching history; this preview does not estimate historical matches.'}

    async def update_transaction_rule(self, preview_id: str, apply_to_existing: bool = False) -> dict:
        """Update the existing rule from its user-reviewed update preview, preserving unedited fields.

        Defaults to future transactions only. apply_to_existing=True rewrites ALL matching
        history. Refuses a rule changed since preview; consumes the ID before attempting a
        write. An uncertain outcome must be inspected, never automatically retried.
        """
        async with self.tx._rule_write_lock:
            preview = self._previews.pop(preview_id, None)
            if preview is None or preview['expires'] <= self.tx.clock():
                raise ValueError('Update preview is missing, consumed or expired; preview again')
            rule_id = preview['rule_id']
            current = await self.find_rule(rule_id)
            if current is None or fingerprint(current) != preview['fingerprint']:
                raise ValueError('Rule changed since preview; read and preview it again')
            payload = checked_payload(await self.tx.api(self.tx.client.gql_call,
                operation='Common_UpdateTransactionRuleMutationV2', graphql_query=UPDATE_RULE,
                variables={'input':{**preview['after'], 'id':rule_id, 'applyToExistingTransactions':apply_to_existing}}), 'updateTransactionRuleV2')
            native = payload.get('transactionRule')
            if not isinstance(native, dict) or str(native.get('id')) != rule_id:
                raise ValueError('Update did not return the requested rule; inspect before repeating')
            try:
                readback = await self.find_rule(rule_id)
                verified = readback is not None and native_snapshot(readback) == preview['after']
            except ValueError: verified = False
            return {'rule_id':rule_id, 'updated':verified, 'write_accepted':True, 'verified':verified,
                    'apply_to_existing':apply_to_existing,
                    'message':'Rule updated and verified' if verified else 'Write accepted but readback did not confirm every setting; inspect before repeating'}

    async def delete_transaction_rule(self, rule_id: str) -> dict:
        """Delete one exact native rule. Already changed transactions remain unchanged.

        List rules and confirm the target first. Readback checks absence because Monarch's
        deleted flag is unreliable. No bulk deletion and no automatic retry.
        """
        rule_id = require_id(rule_id)
        async with self.tx._rule_write_lock:
            if await self.find_rule(rule_id) is None:
                return {'rule_id':rule_id, 'deleted':True, 'changed':False, 'verified':True}
            checked_payload(await self.tx.api(self.tx.client.gql_call, operation='Common_DeleteTransactionRule',
                graphql_query=DELETE_RULE, variables={'id':rule_id}), 'deleteTransactionRule')
            try: verified = await self.find_rule(rule_id) is None
            except ValueError: verified = False
            return {'rule_id':rule_id, 'deleted':verified, 'changed':verified, 'write_accepted':True, 'verified':verified,
                    'message':'Rule deletion verified' if verified else 'Deletion not confirmed; inspect rules before repeating'}

    async def reorder_transaction_rule(self, rule_id: str, new_order: int) -> dict:
        """Move one native rule to a zero-based position (0=first), preserving other relative order.

        Monarch applies rules in sequence, so this can change future outcomes. List all
        rules and review the desired position first. Verify the complete order after moving.
        """
        rule_id = require_id(rule_id)
        if type(new_order) is not int or new_order < 0: raise ValueError('new_order must be a nonnegative integer position')
        async with self.tx._rule_write_lock:
            rules = await self.rules()
            ids = [str(r['id']) for r in rules]
            if rule_id not in ids or new_order >= len(ids): raise ValueError('Unknown rule or position outside the complete rule list')
            current = ids.index(rule_id)
            if current == new_order: return {'rule_id':rule_id, 'rule_ids':ids, 'changed':False, 'verified':True}
            expected = ids.copy(); expected.remove(rule_id); expected.insert(new_order,rule_id)
            checked_payload(await self.tx.api(self.tx.client.gql_call, operation='Web_UpdateRuleOrderMutation',
                graphql_query=MOVE_RULE, variables={'id':rule_id,'order':new_order}), 'updateTransactionRuleOrderV2')
            try:
                after = [str(r['id']) for r in await self.rules()]
                verified = after == expected
            except ValueError: after,verified = None,False
            return {'rule_id':rule_id, 'rule_ids':after, 'expected_rule_ids':expected, 'changed':verified,
                    'write_accepted':True, 'verified':verified,
                    'message':'Rule order verified' if verified else 'Full rule order not confirmed; inspect before repeating'}

    async def rename_transaction_tag(self, tag_id: str, name: str) -> dict:
        """Rename an existing account-wide tag by ID, preserving its color and order.

        Existing transactions and rules keep referencing the same ID. Refuses a duplicate
        name. List tags and confirm the target first; inspect uncertain outcomes before retrying.
        """
        tag_id = require_id(tag_id)
        name = name.strip()
        if not name or len(name)>100: raise ValueError('Provide a tag name of 1–100 characters')
        async with self.tx._tag_creation_lock:
            tags = (await self.tx.get_transaction_tags())['tags']
            tag = next((t for t in tags if str(t['id'])==tag_id),None)
            if tag is None: raise ValueError('Unknown tag ID; list tags first')
            if any(str(t['id']) != tag_id and str(t.get('name','')).casefold()==name.casefold() for t in tags):
                raise ValueError('A different tag already has that name')
            color = tag.get('color')
            if not isinstance(color,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',color):
                raise ValueError('Tag color could not be read; refusing to overwrite it')
            if tag.get('name') == name: return {'tag_id':tag_id,'changed':False,'verified':True,'tag':tag}
            checked_payload(await self.tx.api(self.tx.client.gql_call, operation='Common_UpdateTransactionTag',
                graphql_query=RENAME_TAG,variables={'input':{'id':tag_id,'name':name,'color':color}}),'updateTransactionTag')
            try:
                after = next((t for t in (await self.tx.get_transaction_tags())['tags'] if str(t['id'])==tag_id),None)
                verified = bool(after and after.get('name')==name and after.get('color')==color and after.get('order')==tag.get('order'))
            except ValueError: after,verified = None,False
            return {'tag_id':tag_id,'tag':after,'changed':verified,'write_accepted':True,'verified':verified,
                    'message':'Tag rename verified' if verified else 'Tag rename not confirmed; inspect before repeating'}

    async def delete_transaction_tag(self, tag_id: str) -> dict:
        """Delete one tag account-wide, removing its assignment from every tagged transaction.

        This may also affect native rules that reference the tag. List tags/rules and confirm
        that account-wide deletion is intended. To untag one transaction use remove_transaction_tags.
        Checks tag absence; does not claim to restore or verify every historical rule/transaction.
        """
        async with self.tx._rule_write_lock:
            return await self._delete_transaction_tag(tag_id)

    async def _delete_transaction_tag(self, tag_id):
        tag_id = require_id(tag_id)
        async with self.tx._tag_creation_lock:
            tag = next((t for t in (await self.tx.get_transaction_tags())['tags'] if str(t['id'])==tag_id),None)
            if tag is None: return {'tag_id':tag_id,'deleted':True,'changed':False,'verified':True}
            checked_payload(await self.tx.api(self.tx.client.gql_call, operation='Common_DeleteHouseholdTransactionTag',
                graphql_query=DELETE_TAG,variables={'tagId':tag_id}),'deleteTransactionTag')
            try: verified = all(str(t['id'])!=tag_id for t in (await self.tx.get_transaction_tags())['tags'])
            except ValueError: verified = False
            return {'tag_id':tag_id,'deleted':verified,'changed':verified,'write_accepted':True,'verified':verified,
                    'transaction_count_before':tag.get('transactionCount'), 'scope':'Account-wide tag deletion; all transaction assignments and rule references may be affected.',
                    'message':'Tag absence verified' if verified else 'Tag deletion not confirmed; inspect before repeating'}

    async def remove_transaction_tags(self, transaction_id: str, tag_ids: list[str]) -> dict:
        """Remove only these tag IDs from the exact transaction, preserving all its other tags.

        Does not delete account-wide tags. Identify the transaction and list tag IDs first.
        Reads and verifies the exact transaction without pending-to-posted redirection.
        """
        transaction_id = require_id(transaction_id)
        async with self.tx._tag_creation_lock:
            wanted = await self.tx.validate_tag_ids(tag_ids)
            if not wanted: raise ValueError('Provide at least one tag ID to remove')
            async with self.tx.transaction_lock(transaction_id):
                txn = await self.tx.get_transaction_details(transaction_id)
                tags = txn.get('tags')
                if not isinstance(tags,list) or any(not isinstance(t,dict) or not t.get('id') for t in tags):
                    raise ValueError('Existing tags could not be read; refusing to replace them')
                existing = list(dict.fromkeys(str(t['id']) for t in tags))
                remaining = [t for t in existing if t not in wanted]
                if remaining==existing: return {'transaction_id':transaction_id,'tag_ids':remaining,'changed':False,'verified':True}
                checked_payload(await self.tx.api(self.tx.client.set_transaction_tags,
                    transaction_id=transaction_id,tag_ids=remaining),'setTransactionTags')
                try:
                    after = await self.tx.get_transaction_details(transaction_id)
                    read_tags = after.get('tags')
                    verified = isinstance(read_tags,list) and all(isinstance(t,dict) and t.get('id') for t in read_tags) and {str(t['id']) for t in read_tags}==set(remaining)
                except ValueError: verified = False
                return {'transaction_id':transaction_id,'tag_ids':remaining,'changed':verified,'write_accepted':True,'verified':verified,
                        'message':'Tag removal verified' if verified else 'Tag removal not confirmed; inspect before repeating'}


def register_management_tools(mcp, transactions):
    service = RuleManagement(transactions)
    for name in ('preview_transaction_rule_update','update_transaction_rule','delete_transaction_rule',
                 'reorder_transaction_rule','rename_transaction_tag','delete_transaction_tag','remove_transaction_tags'):
        read = name=='preview_transaction_rule_update'
        mcp.tool(getattr(service,name), name=name, annotations={'readOnlyHint':read,
            'destructiveHint':not read,'openWorldHint':False,'idempotentHint':read})
    return service
