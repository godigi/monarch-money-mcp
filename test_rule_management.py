"""Native management contracts; external API replaced with synthetic household state."""
import asyncio
import copy
import unittest
from test_transaction_tools import FakeMonarch, direct
from transaction_tools import TransactionTools
try:
    from rule_management import RuleManagement, RuleChanges
except ImportError:
    RuleManagement = RuleChanges = None


def native_rule():
    return {
        'id': 'r1', 'order': 0,
        'merchantCriteriaUseOriginalStatement': True,
        'merchantCriteria': [{'operator': 'contains', 'value': 'ORIGINAL COFFEE'}],
        'merchantNameCriteria': None, 'originalStatementCriteria': None,
        'amountCriteria': {'operator': 'gt', 'isExpense': True, 'value': 5, 'valueRange': None},
        'accountIds': ['acc1'], 'categoryIds': None,
        'setCategoryAction': {'id': 'cat1', 'name': 'Coffee'},
        'setMerchantAction': {'id': 'merchant1', 'name': 'Coffee Shop'},
        'addTagsAction': [{'id': 'old', 'name': 'Existing'}],
        'setHideFromReportsAction': False, 'reviewStatusAction': 'needs_review',
        'sendNotificationAction': True, 'setLinkToPaydownBudgetAction': False,
        'criteriaOwnerIsJoint': False, 'criteriaOwnerUserIds': ['u1'],
        'criteriaBusinessEntityIds': ['b1'], 'criteriaBusinessEntityIsUnassigned': False,
        'actionSetOwnerIsJoint': False, 'actionSetOwner': {'id': 'u1', 'displayName': 'Owner'},
        'actionSetBusinessEntity': {'id': 'b1', 'name': 'Business'},
        'actionSetBusinessEntityIsUnassigned': False,
        'linkGoalAction': {'id': 'g1', 'name': 'Goal'},
        'linkSavingsGoalAction': {'id': 's1', 'name': 'Savings'},
        'needsReviewByUserAction': {'id': 'u1', 'displayName': 'Owner'},
        'unassignNeedsReviewByUserAction': False,
        'splitTransactionsAction': None,
    }


class ManagementBackend(FakeMonarch):
    def __init__(self):
        super().__init__()
        self.tags[0].update(color='#123456', order=0, transactionCount=8)
        self.tags[1].update(color='#abcdef', order=1, transactionCount=0)
        self.rules = [native_rule(), {**native_rule(), 'id': 'r2', 'order': 1}]
        self.writes = []
        self.ignore_write = False
        self.break_readback = False
        self.fail_write = False

    async def gql_call(self, operation, graphql_query, variables=None):
        if operation == 'GetTransactionRules':
            if self.break_readback and self.writes:
                raise RuntimeError('private upstream details')
            return {'transactionRules': copy.deepcopy(self.rules)}
        self.writes.append((operation, copy.deepcopy(variables)))
        if self.fail_write:
            raise RuntimeError('private credential-shaped details')
        if operation == 'Common_UpdateTransactionRuleMutationV2':
            data = variables['input']
            r = next(r for r in self.rules if r['id'] == data['id'])
            if not self.ignore_write:
                if 'needsReviewByUserAction' not in data:
                    r['needsReviewByUserAction'] = None
                    r['unassignNeedsReviewByUserAction'] = False
                for key, value in data.items():
                    if key in ('id', 'applyToExistingTransactions'): continue
                    if key == 'setMerchantAction': value = {'id': 'merchant1', 'name': value} if value else None
                    elif key == 'addTagsAction': value = [{'id': tag} for tag in value] if value else None
                    elif key in ('setCategoryAction', 'linkGoalAction', 'linkSavingsGoalAction',
                                 'needsReviewByUserAction', 'actionSetOwner', 'actionSetBusinessEntity'):
                        value = {'id': value} if value else None
                    r[key] = copy.deepcopy(value)
            return {'updateTransactionRuleV2': {'errors': [{'message': 'rejected'}] if self.reject else [], 'transactionRule': {'id': r['id'], 'order': r['order']}}}
        if operation == 'Common_DeleteTransactionRule':
            if not self.ignore_write and not self.reject:
                self.rules = [r for r in self.rules if r['id'] != variables['id']]
            return {'deleteTransactionRule': {'deleted': False, 'errors': [{'message': 'rejected'}] if self.reject else []}}
        if operation == 'Web_UpdateRuleOrderMutation':
            if not self.ignore_write:
                r = next(r for r in self.rules if r['id'] == variables['id'])
                self.rules.remove(r); self.rules.insert(variables['order'], r)
                for i, r in enumerate(self.rules): r['order'] = i
            return {'updateTransactionRuleOrderV2': {'transactionRules': [{'id': r['id'], 'order': r['order']} for r in self.rules]}}
        if operation == 'Common_UpdateTransactionTag':
            data = variables['input']; tag = next(t for t in self.tags if t['id'] == data['id'])
            if not self.ignore_write: tag.update(data)
            return {'updateTransactionTag': {'tag': copy.deepcopy(tag), 'errors': [{'message': 'rejected'}] if self.reject else []}}
        if operation == 'Common_DeleteHouseholdTransactionTag':
            if not self.ignore_write and not self.reject: self.tags = [t for t in self.tags if t['id'] != variables['tagId']]
            return {'deleteTransactionTag': {'errors': [{'message': 'rejected'}] if self.reject else []}}
        return await super().gql_call(operation, graphql_query, variables)


class RuleManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(RuleManagement, 'Rule management has not been implemented')
        self.backend = ManagementBackend()
        self.clock = 100
        self.tx = TransactionTools(self.backend, direct, clock=lambda: self.clock)
        self.svc = RuleManagement(self.tx)

    async def preview(self, **changes):
        return await self.svc.preview_transaction_rule_update('r1', RuleChanges(**changes))

    async def test_partial_update_preserves_legacy_and_all_advanced_actions(self):
        p = await self.preview(category_id='cat2')
        self.assertEqual(p['before']['setCategoryAction'], 'cat1')
        self.assertEqual(p['after']['setCategoryAction'], 'cat2')
        self.assertEqual(self.backend.writes, [])
        result = await self.svc.update_transaction_rule(p['preview_id'])
        data = self.backend.writes[0][1]['input']
        self.assertFalse(data['applyToExistingTransactions'])
        self.assertEqual(data['merchantCriteria'], [{'operator': 'contains', 'value': 'ORIGINAL COFFEE'}])
        self.assertEqual(data['setMerchantAction'], 'Coffee Shop')
        for key, value in {'linkGoalAction': 'g1', 'linkSavingsGoalAction': 's1', 'needsReviewByUserAction': 'u1', 'actionSetOwner': 'u1', 'actionSetBusinessEntity': 'b1', 'sendNotificationAction': True, 'addTagsAction': ['old']}.items():
            self.assertEqual(data[key], value)
        self.assertTrue(result['verified'])

    async def test_update_clears_only_explicit_fields(self):
        p = await self.preview(merchant_rename=None, add_tag_ids=[], hide_from_reports=False)
        self.assertIsNone(p['after']['setMerchantAction'])
        self.assertEqual(p['after']['addTagsAction'], [])
        self.assertEqual(p['after']['linkGoalAction'], 'g1')
        r = await self.svc.update_transaction_rule(p['preview_id'])
        self.assertTrue(r['verified'])

    async def test_changing_merchant_replaces_legacy_condition(self):
        self.backend.rules[0]['merchantCriteriaUseOriginalStatement'] = False
        p = await self.preview(merchant_name='New Shop', text_operator='eq')
        self.assertEqual(p['after']['merchantCriteria'], [])
        self.assertEqual(p['after']['merchantNameCriteria'], [{'operator': 'eq', 'value': 'New Shop'}])
        self.assertEqual(p['after']['accountIds'], ['acc1'])

    async def test_merchant_update_preserves_legacy_original_statement(self):
        p = await self.preview(merchant_name='New Shop')
        self.assertEqual(p['after']['merchantCriteria'], [{'operator': 'contains', 'value': 'ORIGINAL COFFEE'}])
        self.assertTrue(p['after']['merchantCriteriaUseOriginalStatement'])

    async def test_statement_update_or_clear_replaces_legacy_statement_only(self):
        for value in ('NEW ORIGINAL', None):
            p = await self.preview(original_statement=value)
            self.assertEqual(p['after']['merchantCriteria'], [])
            self.assertFalse(p['after']['merchantCriteriaUseOriginalStatement'])
            self.backend.rules[0]['merchantCriteriaUseOriginalStatement'] = False
            p = await self.preview(original_statement=value)
            self.assertEqual(p['after']['merchantCriteria'], [{'operator': 'contains', 'value': 'ORIGINAL COFFEE'}])
            self.backend.rules[0]['merchantCriteriaUseOriginalStatement'] = True

    async def test_structured_amount_and_null_clear(self):
        p = await self.preview(amount_criteria={'operator': 'between', 'is_expense': False, 'lower': 10, 'upper': 20})
        self.assertEqual(p['after']['amountCriteria'], {'operator': 'between', 'isExpense': False, 'value': None, 'valueRange': {'lower': 10.0, 'upper': 20.0}})
        p = await self.preview(amount_criteria=None)
        self.assertIsNone(p['after']['amountCriteria'])

    async def test_output_only_reviewer_flag_never_sent_in_mutation(self):
        self.backend.rules[0]['unassignNeedsReviewByUserAction'] = True
        self.backend.rules[0]['needsReviewByUserAction'] = None
        p = await self.preview(category_id='cat2')
        self.assertNotIn('unassignNeedsReviewByUserAction', p['after'])
        await self.svc.update_transaction_rule(p['preview_id'])
        self.assertNotIn('unassignNeedsReviewByUserAction', self.backend.writes[0][1]['input'])

    async def test_inactive_reviewer_omitted_but_explicit_unassign_sent_as_null(self):
        self.backend.rules[0]['needsReviewByUserAction'] = None
        p = await self.preview(category_id='cat2')
        self.assertNotIn('needsReviewByUserAction', p['after'])
        self.backend.rules[0]['unassignNeedsReviewByUserAction'] = True
        p = await self.preview(category_id='cat2')
        self.assertIn('needsReviewByUserAction', p['after'])
        self.assertIsNone(p['after']['needsReviewByUserAction'])

    async def test_reviewer_flag_change_cannot_report_verified(self):
        self.backend.rules[0]['needsReviewByUserAction'] = None
        original = self.backend.gql_call
        async def changed_reviewer(*args, **kwargs):
            result = await original(*args, **kwargs)
            if kwargs.get('operation') == 'Common_UpdateTransactionRuleMutationV2':
                self.backend.rules[0]['unassignNeedsReviewByUserAction'] = True
            return result
        self.backend.gql_call = changed_reviewer
        p = await self.preview(category_id='cat2')
        result = await self.svc.update_transaction_rule(p['preview_id'])
        self.assertFalse(result['verified'])

    async def test_split_action_is_preserved_without_graphql_typenames(self):
        self.backend.rules[0]['splitTransactionsAction'] = {'amountType': 'percentage', '__typename': 'SplitAction', 'splitsInfo': [{'categoryId': 'cat1', 'merchantName': 'Coffee', 'amount': 100, 'goalId': None, 'savingsGoalId': None, 'tags': ['old'], 'hideFromReports': False, 'reviewStatus': None, 'needsReviewByUserId': None, 'ownerUserId': None, 'ownerIsJoint': False, 'businessEntityId': None, 'businessEntityIsUnassigned': False, '__typename': 'SplitInfo'}]}
        p = await self.preview(category_id='cat2')
        self.assertNotIn('__typename', str(p['after']))
        self.assertEqual(p['after']['splitTransactionsAction']['splitsInfo'][0]['amount'], 100)

    async def test_clearing_last_action_refuses_update(self):
        r = self.backend.rules[0]
        r.update(setMerchantAction=None, addTagsAction=None, reviewStatusAction=None,
                 sendNotificationAction=False, linkGoalAction=None, linkSavingsGoalAction=None,
                 needsReviewByUserAction=None, actionSetOwner=None, actionSetBusinessEntity=None)
        with self.assertRaisesRegex(ValueError, 'action'):
            await self.preview(category_id=None)
        self.assertEqual(self.backend.writes, [])

    async def test_notification_only_action_is_preserved(self):
        r = self.backend.rules[0]
        r.update(setMerchantAction=None, addTagsAction=None, reviewStatusAction=None,
                 linkGoalAction=None, linkSavingsGoalAction=None,
                 needsReviewByUserAction=None, actionSetOwner=None, actionSetBusinessEntity=None)
        p = await self.preview(category_id=None)
        self.assertTrue(p['after']['sendNotificationAction'])

    async def test_reviewed_clears_assigned_reviewer(self):
        p = await self.preview(review_status='reviewed')
        self.assertIsNone(p['after'].get('needsReviewByUserAction'))

    async def test_empty_unknown_or_invalid_changes_refused(self):
        for changes in ({}, {'text_operator': 'eq'}, {'category_id': ' '}, {'add_tag_ids': ['unknown']}, {'amount_criteria': {'operator': 'between', 'lower': 20, 'upper': 10}}, {'amount_criteria': {'operator': 'gt'}}, {'made_up': True}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError): await self.preview(**changes)
        self.assertEqual(self.backend.writes, [])

    async def test_unknown_rule_and_incomplete_snapshot_refused(self):
        with self.assertRaises(ValueError):
            await self.svc.preview_transaction_rule_update('missing', RuleChanges(category_id='cat2'))
        del self.backend.rules[0]['linkSavingsGoalAction']
        with self.assertRaisesRegex(ValueError, 'incomplete'): await self.preview(category_id='cat2')
        self.assertEqual(self.backend.writes, [])

    async def test_stale_preview_refuses_write_and_is_consumed(self):
        p = await self.preview(category_id='cat2')
        self.backend.rules[0]['sendNotificationAction'] = False
        with self.assertRaisesRegex(ValueError, 'changed'): await self.svc.update_transaction_rule(p['preview_id'])
        with self.assertRaises(ValueError): await self.svc.update_transaction_rule(p['preview_id'])
        self.assertEqual(self.backend.writes, [])

    async def test_expired_and_reused_previews_refused(self):
        p = await self.preview(category_id='cat2'); self.clock += 901
        with self.assertRaises(ValueError): await self.svc.update_transaction_rule(p['preview_id'])
        p = await self.preview(category_id='cat2'); await self.svc.update_transaction_rule(p['preview_id'])
        with self.assertRaises(ValueError): await self.svc.update_transaction_rule(p['preview_id'])
        self.assertEqual(len(self.backend.writes), 1)

    async def test_create_and_update_previews_are_not_interchangeable(self):
        from transaction_tools import RuleSpec
        c = await self.tx.preview_transaction_rule(RuleSpec(merchant_name='Coffee', category_id='cat1'), '2026-10-01', '2026-10-03')
        p = await self.preview(category_id='cat2')
        with self.assertRaises(ValueError): await self.svc.update_transaction_rule(c['preview_id'])
        with self.assertRaises(ValueError): await self.tx.create_transaction_rule(p['preview_id'])
        self.assertEqual(self.backend.writes, [])

    async def test_update_uncertain_write_not_retried_or_reused(self):
        p = await self.preview(category_id='cat2'); self.backend.fail_write = True
        with self.assertRaisesRegex(ValueError, 'write may have completed'): await self.svc.update_transaction_rule(p['preview_id'])
        with self.assertRaises(ValueError): await self.svc.update_transaction_rule(p['preview_id'])
        self.assertEqual(len(self.backend.writes), 1)

    async def test_update_rejected_not_reported_as_success(self):
        p = await self.preview(category_id='cat2'); self.backend.reject = True
        with self.assertRaisesRegex(ValueError, 'rejected'): await self.svc.update_transaction_rule(p['preview_id'])

    async def test_update_ignored_or_unreadable_is_unverified(self):
        for flag in ('ignore_write', 'break_readback'):
            with self.subTest(flag=flag):
                self.setUp(); p = await self.preview(category_id='cat2'); setattr(self.backend, flag, True)
                result = await self.svc.update_transaction_rule(p['preview_id'])
                self.assertFalse(result['verified'])
                self.assertIn('inspect', result['message'].lower())

    async def test_delete_uses_absence_not_unreliable_deleted_flag(self):
        r = await self.svc.delete_transaction_rule('r1')
        self.assertTrue(r['verified']); self.assertTrue(r['deleted'])
        self.assertEqual([r['id'] for r in self.backend.rules], ['r2'])

    async def test_delete_ignored_is_not_deleted(self):
        self.backend.ignore_write = True
        r = await self.svc.delete_transaction_rule('r1')
        self.assertFalse(r['verified']); self.assertFalse(r['deleted'])

    async def test_delete_missing_is_idempotent_and_rejection_is_error(self):
        r = await self.svc.delete_transaction_rule('missing')
        self.assertFalse(r['changed']); self.assertEqual(self.backend.writes, [])
        self.backend.reject = True
        with self.assertRaises(ValueError): await self.svc.delete_transaction_rule('r1')

    async def test_reorder_checks_entire_sequence_and_sorted_native_orders(self):
        self.backend.rules.reverse()
        r = await self.svc.reorder_transaction_rule('r2', 0)
        self.assertTrue(r['verified']); self.assertEqual(r['rule_ids'], ['r2', 'r1'])
        self.assertEqual(self.backend.writes[0][1], {'id': 'r2', 'order': 0})

    async def test_reorder_invalid_missing_duplicate_and_noninteger_refused(self):
        for pos in (-1, 2, True, 0.5):
            with self.subTest(pos=pos):
                with self.assertRaises(ValueError): await self.svc.reorder_transaction_rule('r1', pos)
        with self.assertRaises(ValueError): await self.svc.reorder_transaction_rule('missing', 0)
        self.backend.rules[1]['id'] = 'r1'
        with self.assertRaises(ValueError): await self.svc.reorder_transaction_rule('r1', 0)
        self.assertEqual(self.backend.writes, [])

    async def test_reorder_noop_and_ignored_write(self):
        r = await self.svc.reorder_transaction_rule('r1', 0)
        self.assertFalse(r['changed']); self.assertEqual(self.backend.writes, [])
        self.backend.ignore_write = True
        r = await self.svc.reorder_transaction_rule('r2', 0)
        self.assertFalse(r['verified'])

    async def test_rename_preserves_color_order_and_verifies(self):
        r = await self.svc.rename_transaction_tag('old', ' Renamed ')
        self.assertTrue(r['verified'])
        self.assertEqual(self.backend.writes[0][1]['input'], {'id': 'old', 'name': 'Renamed', 'color': '#123456'})
        self.assertEqual(self.backend.tags[0]['order'], 0)

    async def test_rename_missing_collision_empty_and_unreadable_color_refused(self):
        for tag, name in [('missing', 'New'), ('old', ' business '), ('old', ' '), ('old', 'x'*101)]:
            with self.subTest(tag=tag,name=name):
                with self.assertRaises(ValueError): await self.svc.rename_transaction_tag(tag,name)
        del self.backend.tags[0]['color']
        with self.assertRaises(ValueError): await self.svc.rename_transaction_tag('old','New')
        self.assertEqual(self.backend.writes, [])

    async def test_rename_noop_and_ignored_write(self):
        r = await self.svc.rename_transaction_tag('old', 'Existing')
        self.assertFalse(r['changed']); self.assertEqual(self.backend.writes, [])
        self.backend.ignore_write = True
        r = await self.svc.rename_transaction_tag('old', 'Changed')
        self.assertFalse(r['verified'])

    async def test_tag_delete_is_accountwide_and_verified(self):
        r = await self.svc.delete_transaction_tag('old')
        self.assertTrue(r['verified']); self.assertTrue(r['deleted'])
        self.assertEqual(r['transaction_count_before'], 8)
        self.assertIn('account', r['scope'].lower())

    async def test_tag_delete_noop_rejection_and_ignored_write(self):
        r = await self.svc.delete_transaction_tag('missing')
        self.assertFalse(r['changed']); self.assertEqual(self.backend.writes, [])
        self.backend.reject = True
        with self.assertRaises(ValueError): await self.svc.delete_transaction_tag('old')
        self.backend.reject = False; self.backend.ignore_write = True
        r = await self.svc.delete_transaction_tag('old')
        self.assertFalse(r['verified']); self.assertFalse(r['deleted'])

    async def test_remove_only_requested_tags_exact_transaction(self):
        self.backend.txn['tags'] = [{'id': 'old'}, {'id': 'new'}]
        r = await self.svc.remove_transaction_tags('tx1', ['old','old'])
        self.assertEqual(r['tag_ids'], ['new']); self.assertTrue(r['verified'])
        self.assertIs(self.backend.redirect_posted, False)

    async def test_remove_noop_unknown_wrong_and_incomplete_refused(self):
        r = await self.svc.remove_transaction_tags('tx1', ['new'])
        self.assertFalse(r['changed'])
        for tx, ids in [('wrong',['old']), ('tx1',['unknown']), ('tx1',[])]:
            with self.assertRaises(ValueError): await self.svc.remove_transaction_tags(tx,ids)
        self.backend.txn['tags'] = None
        with self.assertRaises(ValueError): await self.svc.remove_transaction_tags('tx1',['old'])

    async def test_concurrent_add_and_remove_preserve_requested_effects(self):
        await asyncio.gather(self.tx.add_transaction_tags('tx1',['new']), self.svc.remove_transaction_tags('tx1',['old']))
        self.assertEqual(self.backend.txn['tags'], [{'id':'new'}])

    async def test_concurrent_update_preview_can_only_write_once(self):
        p = await self.preview(category_id='cat2')
        results = await asyncio.gather(self.svc.update_transaction_rule(p['preview_id']), self.svc.update_transaction_rule(p['preview_id']), return_exceptions=True)
        self.assertEqual(sum(isinstance(r,dict) for r in results), 1)
        self.assertEqual(len(self.backend.writes), 1)

if __name__ == '__main__': unittest.main()
