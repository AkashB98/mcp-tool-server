#!/usr/bin/env python3
"""helios-mcp-server — wrap the Helios Home store API as MCP tools/resources/prompts.

Speaks newline-delimited JSON-RPC 2.0 over stdio, following the Model Context
Protocol surface (initialize / tools / resources / prompts) closely enough that
any MCP-compatible client can drive it. This is the layer every FDE team ends
up building: take an internal REST API and expose it to agents with schemas,
validation, error mapping and tracing — instead of letting the agent freestyle
HTTP calls.

Usage:
    python mcp_server.py [--api-key KEY] [--no-auth] [--seed 42]
                         [--rate-limit 600] [--trace trace.jsonl]
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time

from helios_api import HeliosRestApi, LOW_STOCK_THRESHOLD

SERVER_NAME = "helios-mcp-server"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# Tool definitions: name -> (description, input_schema, handler)
# Handlers receive (api, args, api_key) and return a JSON-serializable result.
# They raise ToolError to produce a mapped MCP error response.
# ---------------------------------------------------------------------------

class ToolError(Exception):
    """A tool failure with an actionable hint for the calling agent."""

    def __init__(self, message: str, hint: str = "", code: int = -32000):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code


def _raise_for_status(status, body, context):
    if status == 404:
        raise ToolError(body.get("message", "not found"),
                        hint=f"{context}: check the ID spelling — use "
                             f"search_products or list_orders to find valid IDs.",
                        code=-32001)
    if status == 409:
        raise ToolError(body.get("message", "conflict"),
                        hint=f"{context}: the request conflicts with current "
                             f"state (e.g. not enough stock). Tell the user what "
                             f"is available instead of retrying blindly.",
                        code=-32002)
    if status == 429:
        raise ToolError("rate limit exceeded",
                        hint=f"{context}: back off for a few seconds and retry "
                             f"once; do not hammer the tool.",
                        code=-32003)
    if status in (400, 401):
        raise ToolError(body.get("message", "bad request"),
                        hint=f"{context}: fix the arguments and retry — "
                             f"re-read the tool's input schema.",
                        code=-32004)
    if status >= 400:
        raise ToolError(body.get("message", f"API error {status}"),
                        hint=context, code=-32000)


def _api(api, method, key, context, **kwargs):
    status, body = getattr(api, method)(api_key=key, **kwargs)
    _raise_for_status(status, body, context)
    return body


def _bind_key(api, key):
    # The simulated API takes the key per-call; stash the server's key on the
    # instance so tool handlers stay uniform.
    api._api_key_for_mcp = key
    return api


TOOLS = {}

def tool(name, description, schema):
    def deco(fn):
        TOOLS[name] = {"description": description,
                       "inputSchema": schema, "handler": fn}
        return fn
    return deco


def _schema(props, required=()):
    return {"type": "object", "properties": props,
            "required": list(required), "additionalProperties": False}


@tool("search_products",
      "Search the product catalog by keyword and/or category.",
      _schema({"query": {"type": "string",
                         "description": "keyword matched against name and product ID"},
               "category": {"type": "string",
                            "description": "one of: Smart Thermostats, Smart Locks, "
                                           "Smart Lighting, Sensors, Cameras"},
               "in_stock_only": {"type": "boolean"},
               "limit": {"type": "integer", "minimum": 1, "maximum": 50}}))
def _t_search(api, args, key):
    return _api(api, "list_products", key, "search_products",
                q=args.get("query"), category=args.get("category"),
                in_stock_only=args.get("in_stock_only", False),
                limit=args.get("limit", 20))


@tool("get_product",
      "Get full details for one product by ID (e.g. HX-1042).",
      _schema({"product_id": {"type": "string"}}, required=("product_id",)))
def _t_get_product(api, args, key):
    return _api(api, "get_product", key, "get_product", product_id=args["product_id"])


@tool("check_inventory",
      "Check current warehouse stock for a product ID.",
      _schema({"product_id": {"type": "string"}}, required=("product_id",)))
def _t_inventory(api, args, key):
    return _api(api, "get_inventory", key, "check_inventory",
                product_id=args["product_id"])


@tool("place_order",
      "Place an order for a customer. Validates stock atomically — either the "
      "whole order succeeds or nothing is reserved.",
      _schema({"customer_id": {"type": "string"},
               "items": {"type": "array", "minItems": 1, "items": _schema(
                   {"product_id": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1}},
                   required=("product_id", "quantity"))}},
              required=("customer_id", "items")))
def _t_place_order(api, args, key):
    return _api(api, "create_order", key, "place_order",
                customer_id=args["customer_id"], items=args["items"])


@tool("get_order",
      "Get one order by ID (e.g. ORD-5005): status, line items, totals.",
      _schema({"order_id": {"type": "string"}}, required=("order_id",)))
def _t_get_order(api, args, key):
    return _api(api, "get_order", key, "get_order", order_id=args["order_id"])


@tool("list_orders",
      "List orders, newest first. Filter by customer and/or status.",
      _schema({"customer_id": {"type": "string"},
               "status": {"type": "string",
                          "enum": ["processing", "shipped", "delivered",
                                   "cancelled"]},
               "limit": {"type": "integer", "minimum": 1, "maximum": 50}}))
def _t_list_orders(api, args, key):
    return _api(api, "list_orders", key, "list_orders",
                customer_id=args.get("customer_id"), status=args.get("status"),
                limit=args.get("limit", 20))


@tool("get_customer",
      "Look up a customer by ID (e.g. C-104).",
      _schema({"customer_id": {"type": "string"}}, required=("customer_id",)))
def _t_get_customer(api, args, key):
    return _api(api, "get_customer", key, "get_customer",
                customer_id=args["customer_id"])


@tool("open_ticket",
      "Open a support ticket for a customer.",
      _schema({"customer_id": {"type": "string"},
               "subject": {"type": "string"},
               "priority": {"type": "string",
                            "enum": ["low", "normal", "high", "urgent"]}},
              required=("customer_id", "subject")))
def _t_open_ticket(api, args, key):
    return _api(api, "create_ticket", key, "open_ticket",
                customer_id=args["customer_id"], subject=args["subject"],
                priority=args.get("priority", "normal"))


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

def _low_stock_resource(api, key):
    items = []
    for p in api.products:
        units = api.inventory.get(p["id"], 0)
        if units < LOW_STOCK_THRESHOLD:
            items.append({"product_id": p["id"], "name": p["name"],
                          "units": units,
                          "reorder_suggestion": max(0, 20 - units)})
    return {"threshold": LOW_STOCK_THRESHOLD, "low_stock": items,
            "count": len(items)}


def list_resources(api, key):
    return {"resources": [
        {"uri": "inventory://low-stock",
         "name": "Low-stock products",
         "description": f"Products with fewer than {LOW_STOCK_THRESHOLD} "
                        f"units in the warehouse.",
         "mimeType": "application/json"},
        {"uri": "orders://recent",
         "name": "Recent orders",
         "description": "The 10 most recent orders across all customers.",
         "mimeType": "application/json"},
        {"uriTemplate": "customers://{customer_id}/orders",
         "name": "Customer order history",
         "description": "All orders for one customer, newest first.",
         "mimeType": "application/json"},
    ]}


def read_resource(api, key, uri):
    if uri == "inventory://low-stock":
        return {"uri": uri, "mimeType": "application/json",
                "text": json.dumps(_low_stock_resource(api, key), indent=2)}
    if uri == "orders://recent":
        status, body = api.list_orders(limit=10, api_key=key)
        _raise_for_status(status, body, "orders://recent")
        return {"uri": uri, "mimeType": "application/json",
                "text": json.dumps(body, indent=2)}
    m = re.fullmatch(r"customers://([A-Za-z0-9-]+)/orders", uri)
    if m:
        status, body = api.list_orders(customer_id=m.group(1), api_key=key)
        _raise_for_status(status, body, uri)
        return {"uri": uri, "mimeType": "application/json",
                "text": json.dumps(body, indent=2)}
    raise ToolError(f"unknown resource '{uri}'",
                    hint="Available: inventory://low-stock, orders://recent, "
                         "customers://{customer_id}/orders.",
                    code=-32001)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PROMPTS = {
    "reorder-plan": {
        "description": "Draft a restock plan from the current low-stock list.",
        "arguments": [{"name": "threshold", "required": False,
                       "description": "override the low-stock threshold"}],
    },
    "order-status-check": {
        "description": "Summarize one order's status for a customer message.",
        "arguments": [{"name": "order_id", "required": True}],
    },
}


def get_prompt(api, key, name, args):
    args = args or {}
    if name == "reorder-plan":
        data = _low_stock_resource(api, key)
        lines = [f"- {i['product_id']} {i['name']}: {i['units']} units "
                 f"(suggest reordering {i['reorder_suggestion']})"
                 for i in data["low_stock"]] or ["(nothing is low on stock)"]
        return {"description": PROMPTS[name]["description"], "messages": [
            {"role": "user", "content": {"type": "text", "text":
             "You are a warehouse planner for Helios Home. Draft a restock "
             "plan from this low-stock snapshot (units < "
             f"{data['threshold']}). Prioritize items with 0-2 units, suggest "
             "order quantities, and flag anything that risks a stockout this "
             "week.\n\nLow-stock snapshot:\n" + "\n".join(lines)}}]}
    if name == "order-status-check":
        oid = args.get("order_id")
        if not oid:
            raise ToolError("order_status-check needs 'order_id'",
                            hint="Pass the order ID, e.g. {'order_id': 'ORD-5005'}.",
                            code=-32602)
        status, body = api.get_order(oid, api_key=key)
        _raise_for_status(status, body, "order-status-check")
        return {"description": PROMPTS[name]["description"], "messages": [
            {"role": "user", "content": {"type": "text", "text":
             "You are a support agent for Helios Home. Write a short, friendly "
             "status update for the customer based on this order JSON. "
             "Include the order ID, current status, items and total. Do not "
             "invent details that are not in the JSON.\n\nOrder:\n"
             + json.dumps(body, indent=2)}}]}
    raise ToolError(f"unknown prompt '{name}'",
                    hint="Available prompts: reorder-plan, order-status-check.",
                    code=-32602)


# ---------------------------------------------------------------------------
# Minimal JSON-Schema validation (enough for our tool schemas)
# ---------------------------------------------------------------------------

def _validate(schema, value, path="args"):
    errors = []
    stype = schema.get("type")
    if stype == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected object"]
        for r in schema.get("required", ()):
            if r not in value:
                errors.append(f"{path}: missing required field '{r}'")
        for k, v in value.items():
            if k in schema.get("properties", {}):
                errors += _validate(schema["properties"][k], v, f"{path}.{k}")
            elif not schema.get("additionalProperties", True):
                errors.append(f"{path}: unexpected field '{k}'")
    elif stype == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: need at least {schema['minItems']} items")
        for i, v in enumerate(value):
            errors += _validate(schema.get("items", {}), v, f"{path}[{i}]")
    elif stype == "string":
        if not isinstance(value, str):
            errors.append(f"{path}: expected string")
        elif "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: '{value}' not in {schema['enum']}")
    elif stype == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"{path}: expected integer")
        else:
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"{path}: {value} < minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                errors.append(f"{path}: {value} > maximum {schema['maximum']}")
    elif stype == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append(f"{path}: expected number")
    elif stype == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{path}: expected boolean")
    return errors


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------

class ServerContext:
    def __init__(self, api, api_key, trace_path=None):
        self.api = api
        self.api_key = api_key
        self.trace_path = trace_path
        self._trace_fh = None

    def trace(self, entry):
        if not self.trace_path:
            return
        if self._trace_fh is None:
            self._trace_fh = open(self.trace_path, "a", encoding="utf-8")
        self._trace_fh.write(json.dumps(entry) + "\n")
        self._trace_fh.flush()

    def close(self):
        if self._trace_fh is not None:
            self._trace_fh.close()
            self._trace_fh = None


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message, data=None):
    e = {"jsonrpc": "2.0", "id": rid,
         "error": {"code": code, "message": message}}
    if data is not None:
        e["error"]["data"] = data
    return e


def handle_request(req, ctx):
    """Pure dispatch: (request dict, ServerContext) -> response dict or None."""
    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or "method" not in req:
        return _err(req.get("id") if isinstance(req, dict) else None,
                    -32600, "Invalid Request")
    method, rid, params = req["method"], req.get("id"), req.get("params") or {}
    is_notification = "id" not in req

    def respond(payload):
        return None if is_notification else payload

    if method == "initialize":
        return respond(_ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}}}))
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return respond(_ok(rid, {"tools": [
            {"name": n, "description": t["description"],
             "inputSchema": t["inputSchema"]} for n, t in TOOLS.items()]}))
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return respond(_err(rid, -32602, f"unknown tool '{name}'",
                                {"hint": f"Available tools: {sorted(TOOLS)}"}))
        schema_errors = _validate(TOOLS[name]["inputSchema"], args)
        if schema_errors:
            return respond(_err(rid, -32602, "arguments failed schema validation",
                                {"hint": "; ".join(schema_errors)}))
        t0 = time.monotonic()
        try:
            result = TOOLS[name]["handler"](ctx.api, args, ctx.api_key)
            ms = (time.monotonic() - t0) * 1000
            ctx.trace({"ts": time.time(), "tool": name, "args": args,
                       "ms": round(ms, 2), "ok": True})
            return respond(_ok(rid, {
                "content": [{"type": "text",
                             "text": json.dumps(result, indent=2)}],
                "isError": False}))
        except ToolError as e:
            ms = (time.monotonic() - t0) * 1000
            ctx.trace({"ts": time.time(), "tool": name, "args": args,
                       "ms": round(ms, 2), "ok": False, "error": e.message})
            return respond(_ok(rid, {
                "content": [{"type": "text",
                             "text": f"ERROR: {e.message}"}],
                "isError": True,
                "_hint": e.hint, "_code": e.code}))
    if method == "resources/list":
        return respond(_ok(rid, list_resources(ctx.api, ctx.api_key)))
    if method == "resources/read":
        uri = params.get("uri", "")
        try:
            return respond(_ok(rid, {"contents": [
                read_resource(ctx.api, ctx.api_key, uri)]}))
        except ToolError as e:
            return respond(_err(rid, e.code, e.message, {"hint": e.hint}))
    if method == "prompts/list":
        return respond(_ok(rid, {"prompts": [
            {"name": n, **{k: v for k, v in p.items()}}
            for n, p in PROMPTS.items()]}))
    if method == "prompts/get":
        try:
            return respond(_ok(rid, get_prompt(
                ctx.api, ctx.api_key, params.get("name"),
                params.get("arguments"))))
        except ToolError as e:
            return respond(_err(rid, e.code, e.message, {"hint": e.hint}))
    return respond(_err(rid, -32601, f"method not found: {method}"))


def serve(ctx):
    inp, out = sys.stdin, sys.stdout
    try:
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                out.write(json.dumps(_err(None, -32700, "Parse error")) + "\n")
                out.flush()
                continue
            resp = handle_request(req, ctx)
            if resp is not None:
                out.write(json.dumps(resp) + "\n")
                out.flush()
    finally:
        ctx.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Helios Home MCP server (stdio)")
    ap.add_argument("--api-key", default=os.environ.get("HELIOS_API_KEY",
                                                        "helios-demo-key"))
    ap.add_argument("--no-auth", action="store_true",
                    help="disable API-key auth (demo convenience)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--rate-limit", type=int, default=600,
                    help="simulated API rate limit per minute")
    ap.add_argument("--trace", default=None,
                    help="append per-tool-call trace entries as JSONL")
    a = ap.parse_args(argv)
    api = _bind_key(HeliosRestApi(seed=a.seed, api_key=a.api_key,
                                  require_auth=not a.no_auth,
                                  rate_limit_per_min=a.rate_limit),
                    a.api_key)
    serve(ServerContext(api, a.api_key, trace_path=a.trace))


if __name__ == "__main__":
    main()
