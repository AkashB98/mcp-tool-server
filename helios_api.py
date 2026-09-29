#!/usr/bin/env python3
"""Helios Home — fictional smart-home store REST API (simulated, deterministic).

This module implements the *semantics* of a REST API in-process: every method
returns a ``(status_code, body)`` tuple exactly as an HTTP service would, so the
MCP server, demo client, tests and evals all run hermetically with zero
network access.

Swap in a real backend with :class:`HttpRestApi`, which exposes the same
method names over HTTP (``HELIOS_API_BASE_URL`` + ``HELIOS_API_KEY``).

All data is fictional and seeded (default seed 42): two instances built with
the same seed are bit-identical, which is what makes the evals deterministic.
"""

from __future__ import annotations

import copy
import json
import time
import urllib.request
import urllib.error

DEMO_DATE = "2026-09-28"
LOW_STOCK_THRESHOLD = 5

# ---------------------------------------------------------------------------
# Fixtures (fictional catalog — every name, price and quantity is invented)
# ---------------------------------------------------------------------------

_PRODUCTS = [
    # (id, name, category, price_usd)
    ("HX-1001", "Aura Thermostat", "Smart Thermostats", 149.00),
    ("HX-1002", "Aura Thermostat Mini", "Smart Thermostats", 99.00),
    ("HX-1003", "Climate Hub Pro", "Smart Thermostats", 229.00),
    ("HX-1004", "Climate Room Sensor", "Smart Thermostats", 49.00),
    ("HX-1011", "Bolt Smart Lock", "Smart Locks", 179.00),
    ("HX-1012", "Bolt Smart Lock Plus", "Smart Locks", 249.00),
    ("HX-1013", "Latch Keypad Deadbolt", "Smart Locks", 129.00),
    ("HX-1014", "Latch Lever Handle", "Smart Locks", 119.00),
    ("HX-1021", "Glow A19 Bulb (4-pack)", "Smart Lighting", 39.00),
    ("HX-1022", "Glow BR30 Bulb (2-pack)", "Smart Lighting", 29.00),
    ("HX-1023", "Halo Light Strip 5m", "Smart Lighting", 59.00),
    ("HX-1024", "Halo Panel Light", "Smart Lighting", 79.00),
    ("HX-1025", "Glow Filament Bulb (2-pack)", "Smart Lighting", 24.00),
    ("HX-1031", "Sense Motion Sensor", "Sensors", 34.00),
    ("HX-1032", "Sense Door/Window Sensor (2-pack)", "Sensors", 44.00),
    ("HX-1033", "Sense Water Leak Sensor", "Sensors", 39.00),
    ("HX-1034", "Sense Air Quality Monitor", "Sensors", 99.00),
    ("HX-1041", "Vista Indoor Cam", "Cameras", 89.00),
    ("HX-1042", "Vista Outdoor Cam", "Cameras", 129.00),
    ("HX-1043", "Vista Doorbell Cam", "Cameras", 159.00),
    ("HX-1044", "Vista Floodlight Cam", "Cameras", 199.00),
]

_INVENTORY = {
    "HX-1001": 42, "HX-1002": 35, "HX-1003": 18, "HX-1004": 27,
    "HX-1011": 22, "HX-1012": 9, "HX-1013": 3, "HX-1014": 15,
    "HX-1021": 60, "HX-1022": 2, "HX-1023": 31, "HX-1024": 24,
    "HX-1025": 48, "HX-1031": 55, "HX-1032": 38, "HX-1033": 4,
    "HX-1034": 12, "HX-1041": 29, "HX-1042": 17, "HX-1043": 21,
    "HX-1044": 1,
}

_CUSTOMERS = [
    ("C-101", "Maya Chen", "maya.chen@example.com", "plus"),
    ("C-102", "Devon Park", "devon.park@example.com", "standard"),
    ("C-103", "Priya Nair", "priya.nair@example.com", "pro"),
    ("C-104", "Sam Okafor", "sam.okafor@example.com", "plus"),
    ("C-105", "Lena Ruiz", "lena.ruiz@example.com", "standard"),
    ("C-106", "Tom Becker", "tom.becker@example.com", "standard"),
]

# (order_id, customer_id, [(product_id, qty)], status, created_at)
_ORDERS = [
    ("ORD-5001", "C-104", [("HX-1021", 2)], "shipped", "2026-09-21"),
    ("ORD-5002", "C-101", [("HX-1001", 1)], "delivered", "2026-09-18"),
    ("ORD-5003", "C-103", [("HX-1042", 1), ("HX-1031", 2)], "processing", "2026-09-24"),
    ("ORD-5004", "C-102", [("HX-1011", 1)], "delivered", "2026-09-15"),
    ("ORD-5005", "C-104", [("HX-1023", 1)], "processing", "2026-09-26"),
    ("ORD-5006", "C-105", [("HX-1032", 1)], "shipped", "2026-09-22"),
    ("ORD-5007", "C-103", [("HX-1002", 1)], "delivered", "2026-09-12"),
    ("ORD-5008", "C-106", [("HX-1041", 2)], "processing", "2026-09-25"),
]

_TICKETS = [
    ("TCK-9001", "C-102", "Thermostat display flickers intermittently", "normal", "open"),
    ("TCK-9002", "C-105", "Lock jammed after firmware update", "high", "resolved"),
]


class ApiError(Exception):
    """Raised internally with an HTTP-style status and message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class HeliosRestApi:
    """In-process simulation of the Helios Home store REST API."""

    def __init__(self, seed: int = 42, api_key: str = "helios-demo-key",
                 require_auth: bool = True, rate_limit_per_min: int = 600,
                 clock=None):
        self.seed = seed
        self.api_key = api_key
        self.require_auth = require_auth
        self.rate_limit_per_min = rate_limit_per_min
        self._clock = clock or time.monotonic
        self._tokens = float(rate_limit_per_min)
        self._last_refill = self._clock()
        # Deep copies so each instance is independent.
        self.products = [
            {"id": pid, "name": name, "category": cat, "price_usd": price}
            for pid, name, cat, price in _PRODUCTS
        ]
        self.inventory = dict(_INVENTORY)
        self.customers = [
            {"id": cid, "name": name, "email": email, "tier": tier}
            for cid, name, email, tier in _CUSTOMERS
        ]
        self.orders = []
        for oid, cid, items, status, created in _ORDERS:
            self.orders.append(self._build_order(oid, cid, items, status, created))
        self.tickets = [
            {"id": tid, "customer_id": cid, "subject": subj,
             "priority": prio, "status": st}
            for tid, cid, subj, prio, st in _TICKETS
        ]
        self._next_order = 5009
        self._next_ticket = 9003

    # -- internals ------------------------------------------------------
    def _build_order(self, oid, customer_id, items, status, created_at):
        lines = []
        total = 0.0
        for pid, qty in items:
            unit = self._product_or_raise(pid)["price_usd"]
            lines.append({"product_id": pid, "quantity": qty,
                          "unit_price_usd": unit,
                          "line_total_usd": round(unit * qty, 2)})
            total += unit * qty
        return {"id": oid, "customer_id": customer_id, "items": lines,
                "status": status, "created_at": created_at,
                "total_usd": round(total, 2)}

    def _product_or_raise(self, pid):
        for p in self.products:
            if p["id"] == pid:
                return p
        raise ApiError(404, f"product '{pid}' not found")

    def _customer_or_raise(self, cid):
        for c in self.customers:
            if c["id"] == cid:
                return c
        raise ApiError(404, f"customer '{cid}' not found")

    def _order_or_raise(self, oid):
        for o in self.orders:
            if o["id"] == oid:
                return o
        raise ApiError(404, f"order '{oid}' not found")

    def _guard(self, provided_key):
        """Auth + rate limiting. Returns (status, body) on rejection, else None."""
        if self.require_auth and provided_key != self.api_key:
            return 401, {"error": "unauthorized",
                         "message": "missing or invalid API key"}
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(float(self.rate_limit_per_min),
                           self._tokens + elapsed * self.rate_limit_per_min / 60.0)
        self._last_refill = now
        if self._tokens < 1.0:
            return 429, {"error": "rate_limited",
                         "message": "too many requests — back off and retry"}
        self._tokens -= 1.0
        return None

    def _call(self, provided_key, fn, *args, **kwargs):
        rejected = self._guard(provided_key)
        if rejected:
            return rejected
        try:
            return 200, fn(*args, **kwargs)
        except ApiError as e:
            return e.status, {"error": "api_error", "message": e.message}

    # -- products -------------------------------------------------------
    def list_products(self, q=None, category=None, in_stock_only=False,
                      limit=20, api_key=None):
        def run():
            out = self.products
            if category:
                out = [p for p in out if p["category"].lower() == category.lower()]
            if q:
                ql = q.lower()
                out = [p for p in out
                       if ql in p["name"].lower() or ql in p["id"].lower()]
            if in_stock_only:
                out = [p for p in out if self.inventory.get(p["id"], 0) > 0]
            out = out[: max(1, min(limit, 100))]
            return {"products": copy.deepcopy(out), "count": len(out)}
        return self._call(api_key, run)

    def get_product(self, product_id, api_key=None):
        return self._call(api_key, lambda: copy.deepcopy(self._product_or_raise(product_id)))

    # -- inventory ------------------------------------------------------
    def get_inventory(self, product_id, api_key=None):
        def run():
            self._product_or_raise(product_id)  # 404 on unknown product
            units = self.inventory.get(product_id, 0)
            return {"product_id": product_id, "units": units,
                    "low_stock": units < LOW_STOCK_THRESHOLD}
        return self._call(api_key, run)

    def adjust_inventory(self, product_id, delta, reason="", api_key=None):
        def run():
            self._product_or_raise(product_id)
            if not isinstance(delta, int):
                raise ApiError(400, "'delta' must be an integer")
            new_units = self.inventory.get(product_id, 0) + delta
            if new_units < 0:
                raise ApiError(400, f"adjustment would take inventory negative "
                                    f"({self.inventory.get(product_id, 0)} + {delta})")
            self.inventory[product_id] = new_units
            return {"product_id": product_id, "units": new_units,
                    "low_stock": new_units < LOW_STOCK_THRESHOLD}
        return self._call(api_key, run)

    # -- customers ------------------------------------------------------
    def get_customer(self, customer_id, api_key=None):
        return self._call(api_key, lambda: copy.deepcopy(self._customer_or_raise(customer_id)))

    # -- orders ---------------------------------------------------------
    def list_orders(self, customer_id=None, status=None, limit=20, api_key=None):
        def run():
            out = sorted(self.orders, key=lambda o: o["created_at"], reverse=True)
            if customer_id:
                self._customer_or_raise(customer_id)
                out = [o for o in out if o["customer_id"] == customer_id]
            if status:
                out = [o for o in out if o["status"] == status]
            out = out[: max(1, min(limit, 100))]
            return {"orders": copy.deepcopy(out), "count": len(out)}
        return self._call(api_key, run)

    def get_order(self, order_id, api_key=None):
        return self._call(api_key, lambda: copy.deepcopy(self._order_or_raise(order_id)))

    def create_order(self, customer_id, items, api_key=None):
        def run():
            self._customer_or_raise(customer_id)
            if not items or not isinstance(items, list):
                raise ApiError(400, "'items' must be a non-empty list")
            # Validate everything BEFORE mutating (atomic).
            validated = []
            for it in items:
                pid = it.get("product_id")
                qty = it.get("quantity")
                if not pid or not isinstance(qty, int) or qty <= 0:
                    raise ApiError(400, "each item needs 'product_id' and a "
                                        "positive integer 'quantity'")
                self._product_or_raise(pid)
                have = self.inventory.get(pid, 0)
                if have < qty:
                    raise ApiError(409, f"insufficient stock for '{pid}': "
                                        f"need {qty}, have {have}")
                validated.append((pid, qty))
            for pid, qty in validated:
                self.inventory[pid] -= qty
            oid = f"ORD-{self._next_order}"
            self._next_order += 1
            order = self._build_order(oid, customer_id, validated,
                                     "processing", DEMO_DATE)
            self.orders.append(order)
            return {"order": copy.deepcopy(order)}
        status, body = self._call(api_key, run)
        return (201, body) if status == 200 else (status, body)

    def cancel_order(self, order_id, api_key=None):
        def run():
            order = self._order_or_raise(order_id)
            if order["status"] == "cancelled":
                raise ApiError(409, f"order '{order_id}' is already cancelled")
            if order["status"] in ("shipped", "delivered"):
                raise ApiError(409, f"order '{order_id}' is already "
                                    f"{order['status']} and cannot be cancelled")
            for line in order["items"]:
                self.inventory[line["product_id"]] += line["quantity"]
            order["status"] = "cancelled"
            return {"order": copy.deepcopy(order)}
        return self._call(api_key, run)

    # -- tickets --------------------------------------------------------
    def create_ticket(self, customer_id, subject, priority="normal", api_key=None):
        def run():
            self._customer_or_raise(customer_id)
            if not subject or not subject.strip():
                raise ApiError(400, "'subject' must be a non-empty string")
            if priority not in ("low", "normal", "high", "urgent"):
                raise ApiError(400, "'priority' must be low|normal|high|urgent")
            tid = f"TCK-{self._next_ticket}"
            self._next_ticket += 1
            ticket = {"id": tid, "customer_id": customer_id,
                      "subject": subject.strip(), "priority": priority,
                      "status": "open"}
            self.tickets.append(ticket)
            return {"ticket": copy.deepcopy(ticket)}
        status, body = self._call(api_key, run)
        return (201, body) if status == 200 else (status, body)

    def list_tickets(self, customer_id=None, status=None, api_key=None):
        def run():
            out = list(self.tickets)
            if customer_id:
                self._customer_or_raise(customer_id)
                out = [t for t in out if t["customer_id"] == customer_id]
            if status:
                out = [t for t in out if t["status"] == status]
            return {"tickets": copy.deepcopy(out), "count": len(out)}
        return self._call(api_key, run)


class HttpRestApi:
    """Adapter exposing the same method names over real HTTP.

    Configure with ``HELIOS_API_BASE_URL`` and ``HELIOS_API_KEY``. Every
    method returns the same ``(status_code, body)`` tuples as
    :class:`HeliosRestApi`. Included so the MCP server can wrap a live API
    later; the demo, tests and evals all use the simulated one.
    """

    def __init__(self, base_url, api_key, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json",
                     "X-API-Key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode() or "{}")
            except Exception:
                body = {"error": "http_error", "message": str(e)}
            return e.code, body

    # Same surface as HeliosRestApi (subset used by the MCP tools).
    def list_products(self, q=None, category=None, in_stock_only=False, limit=20):
        qs = f"?limit={limit}"
        if q:
            qs += f"&q={q}"
        if category:
            qs += f"&category={category}"
        if in_stock_only:
            qs += "&in_stock=true"
        return self._request("GET", "/products" + qs)

    def get_product(self, product_id):
        return self._request("GET", f"/products/{product_id}")

    def get_inventory(self, product_id):
        return self._request("GET", f"/inventory/{product_id}")

    def get_customer(self, customer_id):
        return self._request("GET", f"/customers/{customer_id}")

    def list_orders(self, customer_id=None, status=None, limit=20):
        qs = f"?limit={limit}"
        if customer_id:
            qs += f"&customer_id={customer_id}"
        if status:
            qs += f"&status={status}"
        return self._request("GET", "/orders" + qs)

    def get_order(self, order_id):
        return self._request("GET", f"/orders/{order_id}")

    def create_order(self, customer_id, items):
        return self._request("POST", "/orders",
                             {"customer_id": customer_id, "items": items})

    def create_ticket(self, customer_id, subject, priority="normal"):
        return self._request("POST", "/tickets",
                             {"customer_id": customer_id, "subject": subject,
                              "priority": priority})
