"""Tests for the fictional Helios Home REST API layer."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helios_api import HeliosRestApi, HttpRestApi, DEMO_DATE  # noqa: E402

KEY = "helios-demo-key"


def api(**kw):
    kw.setdefault("api_key", KEY)
    return HeliosRestApi(**kw)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestFixtures(unittest.TestCase):
    def test_deterministic_fixtures(self):
        a, b = api(require_auth=False), api(require_auth=False)
        self.assertEqual(a.products, b.products)
        self.assertEqual(a.inventory, b.inventory)
        self.assertEqual(a.orders, b.orders)

    def test_catalog_shape(self):
        a = api(require_auth=False)
        self.assertEqual(len(a.products), 21)
        cats = {p["category"] for p in a.products}
        self.assertEqual(cats, {"Smart Thermostats", "Smart Locks",
                                "Smart Lighting", "Sensors", "Cameras"})

    def test_instances_are_independent(self):
        a, b = api(require_auth=False), api(require_auth=False)
        a.inventory["HX-1042"] = 0
        self.assertNotEqual(a.inventory["HX-1042"], b.inventory["HX-1042"])


class TestProducts(unittest.TestCase):
    def test_search_keyword(self):
        s, body = api().list_products(q="bulb", api_key=KEY)
        self.assertEqual(s, 200)
        ids = {p["id"] for p in body["products"]}
        self.assertTrue({"HX-1021", "HX-1022", "HX-1025"} <= ids)

    def test_search_category(self):
        s, body = api().list_products(category="Cameras", api_key=KEY)
        self.assertEqual(s, 200)
        self.assertTrue(all(p["category"] == "Cameras"
                            for p in body["products"]))

    def test_search_in_stock_only(self):
        a = api()
        a.inventory["HX-1022"] = 0
        s, body = a.list_products(category="Smart Lighting", in_stock_only=True,
                                  api_key=KEY)
        self.assertEqual(s, 200)
        self.assertNotIn("HX-1022", {p["id"] for p in body["products"]})

    def test_search_limit(self):
        s, body = api().list_products(limit=3, api_key=KEY)
        self.assertEqual(body["count"], 3)

    def test_get_product(self):
        s, body = api().get_product("HX-1042", api_key=KEY)
        self.assertEqual(s, 200)
        self.assertEqual(body["price_usd"], 129.00)

    def test_get_product_404(self):
        s, body = api().get_product("HX-9999", api_key=KEY)
        self.assertEqual(s, 404)
        self.assertIn("not found", body["message"])


class TestInventory(unittest.TestCase):
    def test_get_inventory(self):
        s, body = api().get_inventory("HX-1042", api_key=KEY)
        self.assertEqual((s, body["units"]), (200, 17))
        self.assertFalse(body["low_stock"])

    def test_low_stock_flag(self):
        s, body = api().get_inventory("HX-1044", api_key=KEY)
        self.assertEqual(s, 200)
        self.assertTrue(body["low_stock"])

    def test_adjust_up_and_down(self):
        a = api()
        s, body = a.adjust_inventory("HX-1041", 5, reason="restock", api_key=KEY)
        self.assertEqual(body["units"], 34)
        s, body = a.adjust_inventory("HX-1041", -4, api_key=KEY)
        self.assertEqual(body["units"], 30)

    def test_adjust_below_zero_rejected(self):
        s, body = api().adjust_inventory("HX-1044", -5, api_key=KEY)
        self.assertEqual(s, 400)

    def test_adjust_non_integer_rejected(self):
        s, body = api().adjust_inventory("HX-1041", 1.5, api_key=KEY)
        self.assertEqual(s, 400)

    def test_inventory_unknown_product_404(self):
        s, _ = api().get_inventory("HX-9999", api_key=KEY)
        self.assertEqual(s, 404)


class TestOrders(unittest.TestCase):
    def test_create_order_happy_path(self):
        a = api()
        before = a.inventory["HX-1025"]
        s, body = a.create_order("C-103",
                                 [{"product_id": "HX-1025", "quantity": 3}],
                                 api_key=KEY)
        self.assertEqual(s, 201)
        order = body["order"]
        self.assertEqual(order["id"], "ORD-5009")
        self.assertEqual(order["status"], "processing")
        self.assertEqual(order["total_usd"], 72.00)
        self.assertEqual(order["created_at"], DEMO_DATE)
        self.assertEqual(a.inventory["HX-1025"], before - 3)

    def test_create_order_atomic_on_insufficient_stock(self):
        a = api()
        before = dict(a.inventory)
        s, body = a.create_order(
            "C-103", [{"product_id": "HX-1025", "quantity": 1},
                      {"product_id": "HX-1044", "quantity": 50}], api_key=KEY)
        self.assertEqual(s, 409)
        self.assertIn("insufficient stock", body["message"])
        self.assertEqual(a.inventory, before)  # nothing reserved

    def test_create_order_validation(self):
        a = api()
        for items in ([], [{"product_id": "HX-1025", "quantity": 0}],
                      [{"product_id": "HX-1025"}]):
            s, _ = a.create_order("C-103", items, api_key=KEY)
            self.assertEqual(s, 400, items)
        s, _ = a.create_order("C-999", [{"product_id": "HX-1025",
                                        "quantity": 1}], api_key=KEY)
        self.assertEqual(s, 404)
        s, _ = a.create_order("C-103", [{"product_id": "HX-9999",
                                        "quantity": 1}], api_key=KEY)
        self.assertEqual(s, 404)

    def test_cancel_restores_stock(self):
        a = api()
        s, body = a.create_order("C-103",
                                 [{"product_id": "HX-1025", "quantity": 2}],
                                 api_key=KEY)
        oid, before = body["order"]["id"], a.inventory["HX-1025"]
        s, body = a.cancel_order(oid, api_key=KEY)
        self.assertEqual(s, 200)
        self.assertEqual(body["order"]["status"], "cancelled")
        self.assertEqual(a.inventory["HX-1025"], before + 2)

    def test_cancel_shipped_rejected(self):
        s, body = api().cancel_order("ORD-5001", api_key=KEY)
        self.assertEqual(s, 409)

    def test_cancel_twice_rejected(self):
        a = api()
        s, body = a.create_order("C-103",
                                 [{"product_id": "HX-1025", "quantity": 1}],
                                 api_key=KEY)
        oid = body["order"]["id"]
        a.cancel_order(oid, api_key=KEY)
        s, _ = a.cancel_order(oid, api_key=KEY)
        self.assertEqual(s, 409)

    def test_list_orders_newest_first_and_filters(self):
        s, body = api().list_orders(api_key=KEY)
        dates = [o["created_at"] for o in body["orders"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        s, body = api().list_orders(customer_id="C-104", api_key=KEY)
        self.assertTrue(all(o["customer_id"] == "C-104"
                            for o in body["orders"]))
        self.assertEqual(body["orders"][0]["id"], "ORD-5005")
        s, body = api().list_orders(status="delivered", api_key=KEY)
        self.assertTrue(all(o["status"] == "delivered"
                            for o in body["orders"]))

    def test_get_order_404(self):
        s, _ = api().get_order("ORD-9999", api_key=KEY)
        self.assertEqual(s, 404)


class TestTickets(unittest.TestCase):
    def test_create_ticket(self):
        s, body = api().create_ticket("C-102", "Bulb flickers", priority="high",
                                      api_key=KEY)
        self.assertEqual(s, 201)
        self.assertEqual(body["ticket"]["id"], "TCK-9003")
        self.assertEqual(body["ticket"]["priority"], "high")

    def test_create_ticket_validation(self):
        s, _ = api().create_ticket("C-102", "   ", api_key=KEY)
        self.assertEqual(s, 400)
        s, _ = api().create_ticket("C-102", "x", priority="ASAP", api_key=KEY)
        self.assertEqual(s, 400)
        s, _ = api().create_ticket("C-999", "x", api_key=KEY)
        self.assertEqual(s, 404)


class TestAuthAndRateLimit(unittest.TestCase):
    def test_wrong_key_401(self):
        s, body = api().list_products(api_key="wrong-key")
        self.assertEqual(s, 401)

    def test_no_auth_mode(self):
        s, _ = api(require_auth=False).list_products(api_key=None)
        self.assertEqual(s, 200)

    def test_rate_limit_429_then_refill(self):
        clock = FakeClock()
        a = api(rate_limit_per_min=2, clock=clock)
        self.assertEqual(a.list_products(api_key=KEY)[0], 200)
        self.assertEqual(a.list_products(api_key=KEY)[0], 200)
        s, body = a.list_products(api_key=KEY)
        self.assertEqual(s, 429)
        self.assertIn("back off", body["message"])
        clock.t += 61.0  # a full minute passes: bucket refills
        self.assertEqual(a.list_products(api_key=KEY)[0], 200)

    def test_http_adapter_shape(self):
        h = HttpRestApi("https://api.example.com", "k")
        self.assertTrue(hasattr(h, "create_order"))
        self.assertTrue(hasattr(h, "list_products"))


if __name__ == "__main__":
    unittest.main()
