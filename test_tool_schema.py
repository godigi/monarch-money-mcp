import asyncio
import unittest

from test_security import server


class ToolSchemaTests(unittest.TestCase):
    def test_new_tools_are_registered_with_write_and_file_metadata(self):
        tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
        for name in ("add_transaction_tags", "create_transaction_tag", "create_transaction_rule",
                     "upload_transaction_receipt", "upload_transaction_receipt_bytes",
                     "update_transaction_rule", "delete_transaction_rule", "reorder_transaction_rule",
                     "rename_transaction_tag", "delete_transaction_tag", "remove_transaction_tags"):
            self.assertIn(name, tools)
            self.assertIs(tools[name].annotations.read_only_hint, False)
        self.assertTrue(tools["preview_transaction_rule_update"].annotations.read_only_hint)
        self.assertTrue(tools["delete_transaction_tag"].annotations.destructive_hint)
        receipt = tools["upload_transaction_receipt"].to_mcp_tool().model_dump(by_alias=True)
        self.assertEqual(receipt["_meta"]["openai/fileParams"], ["file"])
        schema = receipt["inputSchema"]
        file_schema = schema["properties"]["file"]
        self.assertEqual(set(file_schema["properties"]), {"download_url", "file_id", "mime_type", "file_name"})
        self.assertEqual(set(file_schema["required"]), {"download_url", "file_id"})


if __name__ == "__main__": unittest.main()
