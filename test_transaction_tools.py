"""Behavioral contracts for writes and complete rule previews (synthetic data)."""
import copy
import unittest

try:
    from transaction_tools import TransactionTools, RuleSpec
except ImportError:
    TransactionTools = RuleSpec = None


class FakeMonarch:
    def __init__(self):
        self.txn = {"id": "tx1", "tags": [{"id": "old"}], "attachments": []}
        self.tags = [{"id": "old", "name": "Existing"}, {"id": "new", "name": "Business"}]
        self.records = [
            {"id": "tx1", "pending": False, "date": "2026-10-01", "amount": -20,
             "plaidName": "COFFEE SHOP 42", "merchant": {"name": "Coffee Shop"},
             "account": {"id": "acc1"}, "category": {"id": "cat1"}},
            {"id": "tx2", "pending": True, "date": "2026-10-02", "amount": -20,
             "plaidName": "COFFEE SHOP 42", "merchant": {"name": "Coffee Shop"},
             "account": {"id": "acc1"}, "category": {"id": "cat1"}},
            {"id": "tx3", "pending": False, "date": "2026-10-03", "amount": 20,
             "plaidName": "COFFEE SHOP 42", "merchant": {"name": "Coffee Shop"},
             "account": {"id": "acc1"}, "category": {"id": "cat1"}},
        ]
        self.rules = []
        self.redirect_posted = None
        self.reject = False
        self.force_empty_page = False
        self.total_override = None

    async def get_transaction_details(self, transaction_id, redirect_posted=True):
        self.redirect_posted = redirect_posted
        return {"getTransaction": copy.deepcopy(self.txn)}

    async def get_transaction_tags(self):
        return {"householdTransactionTags": copy.deepcopy(self.tags)}

    async def set_transaction_tags(self, transaction_id, tag_ids):
        if self.reject:
            return {"setTransactionTags": {"errors": [{"message": "Rejected", "code": "bad"}]}}
        self.txn["tags"] = [{"id": tag_id} for tag_id in tag_ids]
        return {"setTransactionTags": {"errors": [], "transaction": copy.deepcopy(self.txn)}}

    async def create_transaction_tag(self, name, color):
        tag = {"id": "created", "name": name, "color": color}
        self.tags.append(tag)
        return {"createTransactionTag": {"errors": [], "tag": tag}}

    async def get_transactions(self, offset=0, limit=500, **kwargs):
        # Serve at most two records to force pagination even for a larger limit.
        return {"allTransactions": {
            "totalCount": self.total_override if self.total_override is not None else len(self.records),
            "results": [] if self.force_empty_page else copy.deepcopy(self.records[offset:offset + 2]),
        }}

    async def gql_call(self, operation, graphql_query, variables=None):
        if operation == "GetTransactionRules":
            return {"transactionRules": copy.deepcopy(self.rules)}
        if operation == "Common_CreateTransactionRuleMutationV2":
            data = copy.deepcopy(variables["input"])
            rule = {"id": "rule1", "order": 1, "input": data}
            self.rules.append(rule)
            return {"createTransactionRuleV2": {"errors": [], "transactionRule": rule}}
        raise AssertionError(operation)


async def direct(fn, *args, **kwargs):
    return await fn(*args, **kwargs)


class TransactionToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(TransactionTools, "Transaction tools have not been implemented")
        self.backend = FakeMonarch()
        self.clock = 100.0
        self.service = TransactionTools(self.backend, direct, clock=lambda: self.clock)

    async def test_add_tags_preserves_existing_and_reads_exact_transaction(self):
        result = await self.service.add_transaction_tags("tx1", ["new", "new"])
        self.assertEqual({tag["id"] for tag in self.backend.txn["tags"]}, {"old", "new"})
        self.assertTrue(result["verified"])
        self.assertIs(self.backend.redirect_posted, False)

    async def test_wrong_transaction_or_unknown_tag_refuses_write(self):
        for transaction_id, tags in [("wrong", ["new"]), ("tx1", ["unknown"])]:
            with self.subTest(transaction_id=transaction_id):
                with self.assertRaises(ValueError):
                    await self.service.add_transaction_tags(transaction_id, tags)
                self.assertEqual(self.backend.txn["tags"], [{"id": "old"}])

    async def test_upstream_rejection_is_not_success(self):
        self.backend.reject = True
        with self.assertRaisesRegex(ValueError, "rejected"):
            await self.service.add_transaction_tags("tx1", ["new"])
        self.assertEqual(self.backend.txn["tags"], [{"id": "old"}])

    async def test_create_tag_reuses_existing_name_case_insensitively(self):
        result = await self.service.create_transaction_tag(" business ", "#123456")
        self.assertEqual(result["tag"]["id"], "new")
        self.assertEqual(len(self.backend.tags), 2)

    async def test_preview_matches_case_insensitive_statement_amount_and_account(self):
        spec = RuleSpec(original_statement="coffee shop", account_ids=["acc1"],
                        amount_operator="eq", amount_value=20, category_id="cat2")
        result = await self.service.preview_transaction_rule(spec, "2026-10-01", "2026-10-31")
        self.assertEqual(result["scanned_count"], 3)
        self.assertEqual(result["match_count"], 1)
        self.assertEqual([row["id"] for row in result["matches"]], ["tx1"])
        self.assertTrue(result["preview_id"])

    async def test_rule_creation_uses_preview_and_defaults_future_only_and_consumes_token(self):
        spec = RuleSpec(merchant_name="coffee", category_id="cat2", add_tag_ids=["new"])
        preview = await self.service.preview_transaction_rule(spec, "2026-10-01", "2026-10-31")
        result = await self.service.create_transaction_rule(preview["preview_id"])
        self.assertEqual(result["rule_id"], "rule1")
        self.assertIs(self.backend.rules[0]["input"]["applyToExistingTransactions"], False)
        self.assertEqual(self.backend.rules[0]["input"]["setCategoryAction"], "cat2")
        with self.assertRaises(ValueError):
            await self.service.create_transaction_rule(preview["preview_id"])
        self.assertEqual(len(self.backend.rules), 1)

    async def test_expired_preview_cannot_create(self):
        preview = await self.service.preview_transaction_rule(
            RuleSpec(merchant_name="coffee", category_id="cat2"), "2026-10-01", "2026-10-31")
        self.clock += 901
        with self.assertRaisesRegex(ValueError, "expired"):
            await self.service.create_transaction_rule(preview["preview_id"])
        self.assertEqual(self.backend.rules, [])

    async def test_incomplete_or_oversized_preview_cannot_issue_creation_token(self):
        for empty, total in [(True, 3), (False, 5001)]:
            self.backend.force_empty_page = empty
            self.backend.total_override = total
            with self.assertRaises(ValueError):
                await self.service.preview_transaction_rule(
                    RuleSpec(merchant_name="coffee", category_id="cat2"), "2026-10-01", "2026-10-31")
            self.assertEqual(self.backend.rules, [])

    async def test_rule_requires_condition_and_action_and_valid_dates(self):
        for values in ({"category_id": "cat2"}, {"merchant_name": "coffee"},
                       {"merchant_name": "coffee", "category_id": "cat2", "amount_operator": "eq"}):
            with self.assertRaises(ValueError):
                RuleSpec(**values)
        with self.assertRaises(ValueError):
            await self.service.preview_transaction_rule(
                RuleSpec(merchant_name="coffee", category_id="cat2"), "2026-10-31", "2026-10-01")

    async def test_compact_date_and_duplicate_pages_cannot_issue_preview(self):
        rule = RuleSpec(merchant_name="coffee", category_id="cat2")
        with self.assertRaises(ValueError):
            await self.service.preview_transaction_rule(rule, "20261001", "20261031")
        self.backend.records[2] = copy.deepcopy(self.backend.records[0])
        with self.assertRaises(ValueError):
            await self.service.preview_transaction_rule(rule, "2026-10-01", "2026-10-31")

    async def test_amount_range_and_exact_text_use_native_semantics(self):
        row = self.backend.records[0]
        self.assertTrue(RuleSpec(merchant_name="coffee shop", text_operator="eq", amount_operator="between",
                                amount_lower=20, amount_upper=30, category_id="cat2").matches(row))
        self.assertFalse(RuleSpec(original_statement="coffee shop", text_operator="eq", category_id="cat2").matches(row))
        self.assertFalse(RuleSpec(amount_operator="eq", amount_value=20, amount_is_expense=False, category_id="cat2").matches(row))

    async def test_concurrent_tag_additions_keep_both_updates(self):
        import asyncio
        self.backend.tags.append({"id": "second", "name": "Second"})
        await asyncio.gather(self.service.add_transaction_tags("tx1", ["new"]),
                             self.service.add_transaction_tags("tx1", ["second"]))
        self.assertEqual({tag["id"] for tag in self.backend.txn["tags"]}, {"old", "new", "second"})

    async def test_concurrent_creation_of_same_name_reuses_one_native_tag(self):
        import asyncio
        original = self.backend.get_transaction_tags
        async def delayed_tags():
            snapshot = await original()
            await asyncio.sleep(0)
            return snapshot
        self.backend.get_transaction_tags = delayed_tags
        await asyncio.gather(self.service.create_transaction_tag("Trip"),
                             self.service.create_transaction_tag("trip"))
        self.assertEqual(len([t for t in self.backend.tags if t["name"].casefold() == "trip"]), 1)

    async def test_malformed_tag_read_and_uncertain_rule_write_do_not_retry(self):
        self.backend.txn["tags"] = None
        with self.assertRaises(ValueError):
            await self.service.add_transaction_tags("tx1", ["new"])
        preview = await self.service.preview_transaction_rule(
            RuleSpec(merchant_name="coffee", category_id="cat2"), "2026-10-01", "2026-10-31")
        original = self.backend.gql_call
        async def fail_write(**kwargs):
            if kwargs["operation"] == "Common_CreateTransactionRuleMutationV2":
                await original(**kwargs)
                raise RuntimeError("request body may contain private data")
            return await original(**kwargs)
        self.backend.gql_call = fail_write
        with self.assertRaisesRegex(ValueError, "inspect"):
            await self.service.create_transaction_rule(preview["preview_id"])
        with self.assertRaisesRegex(ValueError, "consumed"):
            await self.service.create_transaction_rule(preview["preview_id"])
        self.assertEqual(len(self.backend.rules), 1)


if __name__ == "__main__":
    unittest.main()
