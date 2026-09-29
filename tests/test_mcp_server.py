"""Tests for the MCP server: protocol, tools, resources, prompts, tracing."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helios_api import HeliosRestApi  # noqa: E402
import mcp_server  # noqa: E402

KEY = "helios-demo-key"


def ctx(**kw):
    kw.setdefault("api_key", KEY)
    api = HeliosRestApi(api_key=kw.pop("api_key"), require_auth=True)
    return mcp_server.ServerContext(api, KEY, **kw)


def call(method, params=None, rid=1, c=None):
    req = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        req["params"] = params
    return mcp_server.handle_request(req, c or ctx())


class TestProtocol(unittest.TestCase):
    def test_initialize(self):
        r = call("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(r["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(r["result"]["serverInfo"]["name"],
                         "helios-mcp-server")
        self.assertIn("tools", r["result"]["capabilities"])

    def test_notification_gets_no_response(self):
        req = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self.assertIsNone(mcp_server.handle_request(req, ctx()))

    def test_unknown_method(self):
        r = call("frobnicate/do")
        self.assertEqual(r["error"]["code"], -32601)

    def test_invalid_request(self):
        r = mcp_server.handle_request({"nope": True}, ctx())
        self.assertEqual(r["error"]["code"], -32600)

    def test_envelope_shape(self):
        r = call("tools/list")
        self.assertEqual(r["jsonrpc"], "2.0")
        self.assertEqual(r["id"], 1)
        self.assertIn("result", r)


class TestTools(unittest.TestCase):
    def test_list_has_eight_tools_with_schemas(self):
        r = call("tools/list")
        tools = r["result"]["tools"]
        self.assertEqual(len(tools), 8)
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"search_products", "get_product",
                                 "check_inventory", "place_order", "get_order",
                                 "list_orders", "get_customer", "open_ticket"})
        for t in tools:
            self.assertIn("inputSchema", t)
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_call_search_products(self):
        r = call("tools/call", {"name": "search_products",
                                "arguments": {"query": "bulb"}})
        res = r["result"]
        self.assertFalse(res["isError"])
        data = json.loads(res["content"][0]["text"])
        self.assertGreater(data["count"], 0)

    def test_call_unknown_tool(self):
        r = call("tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("Available tools", r["error"]["data"]["hint"])

    def test_call_schema_validation_missing_required(self):
        r = call("tools/call", {"name": "get_product", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("product_id", r["error"]["data"]["hint"])

    def test_call_schema_validation_wrong_type(self):
        r = call("tools/call", {"name": "search_products",
                                "arguments": {"limit": "many"}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_call_schema_rejects_extra_fields(self):
        r = call("tools/call", {"name": "get_product",
                                "arguments": {"product_id": "HX-1042",
                                              "sneaky": 1}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_call_404_mapped_with_hint(self):
        r = call("tools/call", {"name": "get_product",
                                "arguments": {"product_id": "HX-9999"}})
        res = r["result"]
        self.assertTrue(res["isError"])
        self.assertIn("not found", res["content"][0]["text"])
        self.assertIn("search_products", res["_hint"])
        self.assertEqual(res["_code"], -32001)

    def test_call_409_insufficient_stock(self):
        r = call("tools/call", {"name": "place_order",
                                "arguments": {"customer_id": "C-103",
                                              "items": [{"product_id": "HX-1044",
                                                         "quantity": 50}]}})
        res = r["result"]
        self.assertTrue(res["isError"])
        self.assertIn("insufficient stock", res["content"][0]["text"])
        self.assertEqual(res["_code"], -32002)

    def test_call_place_order_happy_path(self):
        c = ctx()
        r = mcp_server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "place_order",
                       "arguments": {"customer_id": "C-103",
                                     "items": [{"product_id": "HX-1025",
                                                "quantity": 2}]}}}, c)
        data = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(data["order"]["total_usd"], 48.00)
        # stock actually decremented on the server's API instance
        self.assertEqual(c.api.inventory["HX-1025"], 46)

    def test_call_enum_validation(self):
        r = call("tools/call", {"name": "list_orders",
                                "arguments": {"status": "teleported"}})
        self.assertEqual(r["error"]["code"], -32602)


class TestResources(unittest.TestCase):
    def test_list_resources(self):
        r = call("resources/list")
        uris = [x.get("uri", x.get("uriTemplate"))
                for x in r["result"]["resources"]]
        self.assertIn("inventory://low-stock", uris)
        self.assertIn("orders://recent", uris)
        self.assertIn("customers://{customer_id}/orders", uris)

    def test_low_stock_content(self):
        r = call("resources/read", {"uri": "inventory://low-stock"})
        data = json.loads(r["result"]["contents"][0]["text"])
        ids = {i["product_id"] for i in data["low_stock"]}
        self.assertEqual(ids, {"HX-1013", "HX-1022", "HX-1033", "HX-1044"})

    def test_recent_orders(self):
        r = call("resources/read", {"uri": "orders://recent"})
        data = json.loads(r["result"]["contents"][0]["text"])
        self.assertEqual(data["orders"][0]["id"], "ORD-5005")

    def test_customer_order_history(self):
        r = call("resources/read",
                 {"uri": "customers://C-103/orders"})
        data = json.loads(r["result"]["contents"][0]["text"])
        self.assertTrue(all(o["customer_id"] == "C-103"
                            for o in data["orders"]))
        self.assertEqual(data["count"], 2)

    def test_unknown_resource(self):
        r = call("resources/read", {"uri": "inventory://everything"})
        self.assertEqual(r["error"]["code"], -32001)

    def test_unknown_customer_history_404(self):
        r = call("resources/read", {"uri": "customers://C-999/orders"})
        self.assertEqual(r["error"]["code"], -32001)


class TestPrompts(unittest.TestCase):
    def test_list_prompts(self):
        r = call("prompts/list")
        names = {p["name"] for p in r["result"]["prompts"]}
        self.assertEqual(names, {"reorder-plan", "order-status-check"})

    def test_reorder_plan_embeds_low_stock(self):
        r = call("prompts/get", {"name": "reorder-plan", "arguments": {}})
        text = r["result"]["messages"][0]["content"]["text"]
        for pid in ("HX-1013", "HX-1022", "HX-1033", "HX-1044"):
            self.assertIn(pid, text)

    def test_order_status_check(self):
        r = call("prompts/get", {"name": "order-status-check",
                                 "arguments": {"order_id": "ORD-5005"}})
        text = r["result"]["messages"][0]["content"]["text"]
        self.assertIn("ORD-5005", text)
        self.assertIn("processing", text)

    def test_order_status_check_missing_arg(self):
        r = call("prompts/get", {"name": "order-status-check",
                                 "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_unknown_prompt(self):
        r = call("prompts/get", {"name": "nope", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)


class TestTracing(unittest.TestCase):
    def test_trace_file_written(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            c = ctx(trace_path=path)
            try:
                mcp_server.handle_request(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "check_inventory",
                               "arguments": {"product_id": "HX-1042"}}}, c)
                mcp_server.handle_request(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": "get_product",
                               "arguments": {"product_id": "HX-9999"}}}, c)
                with open(path) as fh:
                    lines = fh.read().strip().split("\n")
                self.assertEqual(len(lines), 2)
                ok_entry, err_entry = (json.loads(l) for l in lines)
                self.assertEqual((ok_entry["tool"], ok_entry["ok"]),
                                 ("check_inventory", True))
                self.assertEqual((err_entry["tool"], err_entry["ok"]),
                                 ("get_product", False))
                self.assertIn("ms", ok_entry)
            finally:
                c.close()


if __name__ == "__main__":
    unittest.main()
