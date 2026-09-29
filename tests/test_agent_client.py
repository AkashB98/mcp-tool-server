"""Tests for the demo agent client: protocol round-trip + scripted policy."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_client import MCPClient, ScriptedPolicy, solve  # noqa: E402


class TestClientProtocol(unittest.TestCase):
    def test_round_trip_over_real_subprocess(self):
        c = MCPClient()
        try:
            init = c.initialize()
            self.assertEqual(init["serverInfo"]["name"], "helios-mcp-server")
            tools = c.list_tools()
            self.assertEqual(len(tools), 8)
            res = c.call_tool("check_inventory", {"product_id": "HX-1042"})
            self.assertFalse(res["isError"])
            data = json.loads(res["content"][0]["text"])
            self.assertEqual(data["units"], 17)
        finally:
            c.close()

    def test_close_terminates_subprocess(self):
        c = MCPClient()
        c.initialize()
        c.close()
        self.assertIsNotNone(c.proc.poll())

    def test_resource_read_over_subprocess(self):
        c = MCPClient()
        try:
            c.initialize()
            content = c.read_resource("inventory://low-stock")
            data = json.loads(content["text"])
            self.assertEqual(data["count"], 4)
        finally:
            c.close()


class TestScriptedPolicy(unittest.TestCase):
    def solve(self, task):
        return solve(task)

    def test_stock_check(self):
        r = self.solve("How many units of HX-1042 (Vista Outdoor Cam) are in stock?")
        self.assertIn("17", r["answer"])
        self.assertIn("HX-1042", r["answer"])
        self.assertEqual([s["tool"] for s in r["steps"]], ["check_inventory"])

    def test_cheapest_lock_order(self):
        r = self.solve("Buy 2 of the cheapest in-stock smart lock for customer C-103.")
        self.assertEqual([s["tool"] for s in r["steps"]],
                         ["search_products", "place_order"])
        self.assertIn("HX-1014", r["answer"])
        self.assertIn("238.00", r["answer"])
        self.assertIn("ORD-", r["answer"])

    def test_cheapest_bulbs_order(self):
        r = self.solve("Customer C-106 wants 3 smart bulbs. Find the cheapest "
                       "in-stock option and place the order.")
        self.assertIn("HX-1025", r["answer"])
        self.assertIn("72.00", r["answer"])

    def test_order_status_by_customer(self):
        r = self.solve("What is the status of customer C-104's most recent order?")
        self.assertIn("ORD-5005", r["answer"])
        self.assertIn("processing", r["answer"])

    def test_order_status_by_id(self):
        r = self.solve("What is the status of order ORD-5002?")
        self.assertIn("delivered", r["answer"])

    def test_low_stock_report(self):
        r = self.solve("Which products are low on stock (fewer than 5 units)?")
        for pid in ("HX-1013", "HX-1022", "HX-1033", "HX-1044"):
            self.assertIn(pid, r["answer"])
        self.assertEqual(r["steps"][0]["tool"], "resource:inventory://low-stock")

    def test_open_ticket(self):
        r = self.solve('Open a support ticket for customer C-102: '
                       '"Doorbell camera offline after storm".')
        self.assertIn("TCK-", r["answer"])
        self.assertIn("C-102", r["answer"])

    def test_unknown_product_no_hallucination(self):
        r = self.solve("What is the price of product HX-9999?")
        self.assertIn("not found", r["answer"].lower())
        self.assertNotIn("$", r["answer"])
        # no side effects: no order/ticket-creating tools were called
        tools = [s["tool"] for s in r["steps"]]
        self.assertNotIn("place_order", tools)
        self.assertNotIn("open_ticket", tools)

    def test_missing_customer_id_asks(self):
        r = self.solve("Buy 1 of the cheapest in-stock sensor.")
        self.assertIn("customer ID", r["answer"])

    def test_each_task_gets_fresh_state(self):
        # Two identical order tasks must mint different order IDs only if the
        # server were shared — with fresh servers both mint ORD-5009.
        r1 = self.solve("Buy 1 of the cheapest in-stock smart lock for customer C-103.")
        r2 = self.solve("Buy 1 of the cheapest in-stock smart lock for customer C-103.")
        self.assertIn("ORD-5009", r1["answer"])
        self.assertIn("ORD-5009", r2["answer"])


class FakePolicy:
    """A scripted stand-in proving the driver honors the policy interface."""

    def __init__(self, actions):
        self.actions = list(actions)

    def next_action(self, task, history):
        return self.actions.pop(0)


class TestPolicyInterface(unittest.TestCase):
    def test_driver_honors_injected_policy(self):
        c = MCPClient()
        try:
            policy = FakePolicy([
                ("call", "check_inventory", {"product_id": "HX-1041"}),
                ("answer", "HX-1041 has stock, per the injected policy."),
            ])
            r = solve("anything", policy=policy, client=c)
            self.assertEqual(r["answer"],
                             "HX-1041 has stock, per the injected policy.")
            self.assertEqual(r["steps"][0]["tool"], "check_inventory")
            self.assertTrue(r["steps"][0]["ok"])
        finally:
            c.close()

    def test_driver_surfaces_tool_errors(self):
        c = MCPClient()
        try:
            policy = FakePolicy([
                ("call", "get_product", {"product_id": "HX-9999"}),
            ])
            r = solve("anything", policy=policy, client=c)
            self.assertIn("get_product", r["answer"])
            self.assertFalse(r["steps"][0]["ok"])
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
