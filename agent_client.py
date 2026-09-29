#!/usr/bin/env python3
"""Demo agent client for helios-mcp-server.

Spawns the MCP server as a subprocess, speaks the JSON-RPC protocol over
stdio, and solves natural-language tasks with a small observe→act→answer loop.

Two policies:
- ``ScriptedPolicy`` (default): a deterministic, rule-based planner. It picks
  tools from the task text, executes them, and composes the final answer from
  real tool observations. No LLM, no network — this is what the evals use.
- ``LLMPolicy``: set ``HELIOS_AGENT_LLM=1`` (plus ``HELIOS_LLM_BASE_URL`` /
  ``HELIOS_LLM_API_KEY`` / ``HELIOS_LLM_MODEL``) to let an OpenAI-compatible
  chat model choose each action. The loop, tool schemas and history format are
  identical; only the decision-maker changes.

The anti-hallucination contract both policies obey: the final answer may only
cite IDs, prices and quantities that appeared in tool observations. The evals
check this.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

SERVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mcp_server.py")
DEFAULT_API_KEY = os.environ.get("HELIOS_API_KEY", "helios-demo-key")

CATEGORY_KEYWORDS = {
    "thermostat": "Smart Thermostats",
    "lock": "Smart Locks",
    "bulb": "Smart Lighting", "light": "Smart Lighting", "lamp": "Smart Lighting",
    "sensor": "Sensors",
    "cam": "Cameras", "camera": "Cameras", "doorbell": "Cameras",
}


class MCPClient:
    """Speaks to the MCP server over stdio (newline-delimited JSON-RPC)."""

    def __init__(self, server_path=SERVER_PATH, api_key=DEFAULT_API_KEY,
                 extra_args=()):
        self._id = 0
        self.proc = subprocess.Popen(
            [sys.executable, server_path, "--api-key", api_key, *extra_args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)

    def _request(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read()
            raise RuntimeError(f"server closed the pipe. stderr: {err[-2000:]}")
        resp = json.loads(line)
        if "error" in resp:
            raise RuntimeError(f"MCP error {resp['error']}")
        return resp["result"]

    def initialize(self):
        return self._request("initialize",
                             {"protocolVersion": "2024-11-05",
                              "clientInfo": {"name": "helios-demo-agent",
                                             "version": "1.0.0"}})

    def list_tools(self):
        return self._request("tools/list")["tools"]

    def call_tool(self, name, arguments):
        return self._request("tools/call",
                             {"name": name, "arguments": arguments})

    def read_resource(self, uri):
        return self._request("resources/read", {"uri": uri})["contents"][0]

    def get_prompt(self, name, arguments=None):
        return self._request("prompts/get",
                             {"name": name, "arguments": arguments or {}})

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


# ---------------------------------------------------------------------------
# Policies: decide the next action given the task + history.
# An action is ("call", tool_name, args), ("resource", uri, None),
# or ("answer", text).
# ---------------------------------------------------------------------------

def _find_ids(text, prefix):
    return re.findall(r"\b" + re.escape(prefix) + r"-\d+\b", text)


def _find_qty(text):
    # Lookbehind keeps us from reading the digits inside IDs like C-106.
    m = re.search(r"(?<![A-Za-z0-9-])(\d+)\s+(?:of\s+the\s+)?(?:cheapest\s+)?",
                  text, re.I)
    return int(m.group(1)) if m else 1


class ScriptedPolicy:
    """Deterministic planner: keyword task routing + observation-driven args."""

    def __init__(self, tools):
        self.tools = {t["name"]: t for t in tools}

    def _category(self, text):
        tl = text.lower()
        for kw, cat in CATEGORY_KEYWORDS.items():
            if kw in tl:
                return cat
        return None

    def next_action(self, task, history):
        """history: list of {"tool","args","result"} dicts."""
        tl = task.lower()
        done = {(h["tool"], json.dumps(h["args"], sort_keys=True)) for h in history}
        results = {h["tool"]: h["result"] for h in history}

        def payload(tool):
            r = results.get(tool, {})
            for c in r.get("content", []):
                if c.get("type") == "text" and not r.get("isError"):
                    try:
                        return json.loads(c["text"])
                    except Exception:
                        return {}
            return {}

        # -- route: product not found / price of unknown product ------------
        pids = _find_ids(task, "HX")
        if pids and ("price" in tl or "stock" in tl or "unit" in tl):
            pid = pids[0]
            key = ("check_inventory", json.dumps({"product_id": pid}, sort_keys=True))
            if key not in done:
                return ("call", "check_inventory", {"product_id": pid})
            inv = payload("check_inventory")
            if not inv:  # error case: observation carried the failure
                last = history[-1]["result"]
                err_text = last["content"][0]["text"]
                return ("answer", f"I couldn't find product {pid}: {err_text} "
                                  f"I don't have pricing for it, so I can't quote one.")
            units = inv.get("units")
            return ("answer", f"{pid} has {units} units in stock.")

        # -- route: cheapest X + order --------------------------------------
        if "cheapest" in tl and ("order" in tl or "buy" in tl or "place" in tl):
            cat = self._category(task)
            cids = _find_ids(task, "C")
            qty = _find_qty(task)
            if not cids:
                return ("answer", "I need a customer ID (e.g. C-103) to place an order.")
            skey = ("search_products", json.dumps(
                {"category": cat, "in_stock_only": True, "limit": 50}, sort_keys=True))
            if skey not in done:
                return ("call", "search_products",
                        {"category": cat, "in_stock_only": True, "limit": 50})
            prods = payload("search_products").get("products", [])
            if not prods:
                return ("answer", f"No in-stock products found in {cat}.")
            cheapest = min(prods, key=lambda p: p["price_usd"])
            return ("call", "place_order",
                    {"customer_id": cids[0],
                     "items": [{"product_id": cheapest["id"],
                                "quantity": qty}]})
            # (the loop ends after place_order: see _terminal below)

        # -- route: order status --------------------------------------------
        if "status" in tl and "order" in tl:
            oids = _find_ids(task, "ORD")
            if oids:
                key = ("get_order", json.dumps({"order_id": oids[0]}, sort_keys=True))
                if key not in done:
                    return ("call", "get_order", {"order_id": oids[0]})
                # NOTE: the get_order tool returns the bare order object
                # (not {"order": …}) — the API's shape, not ours.
                return ("answer", self._describe_order(payload("get_order")))
            cids = _find_ids(task, "C")
            if cids:
                lkey = ("list_orders", json.dumps(
                    {"customer_id": cids[0], "limit": 5}, sort_keys=True))
                if lkey not in done:
                    return ("call", "list_orders",
                            {"customer_id": cids[0], "limit": 5})
                orders = payload("list_orders").get("orders", [])
                if not orders:
                    return ("answer", f"No orders found for {cids[0]}.")
                newest = orders[0]
                gkey = ("get_order", json.dumps({"order_id": newest["id"]},
                                                sort_keys=True))
                if gkey not in done:
                    return ("call", "get_order", {"order_id": newest["id"]})
                return ("answer", self._describe_order(
                    payload("get_order") or newest))
            return ("answer", "I need an order ID (ORD-…) or customer ID (C-…) "
                               "to check an order status.")

        # -- route: low stock -----------------------------------------------
        if "low" in tl and "stock" in tl:
            rkey = "resource:inventory://low-stock"
            seen = [h for h in history if h["tool"] == rkey]
            if not seen:
                return ("resource", "inventory://low-stock", None)
            try:
                data = json.loads(seen[-1]["result"]["content"][0]["text"])
            except Exception:
                data = {}
            lines = [f"- {i['product_id']} {i['name']}: {i['units']} units"
                     for i in data.get("low_stock", [])]
            return ("answer",
                    f"Products low on stock (under {data.get('threshold', 5)} "
                    f"units):\n" + ("\n".join(lines) if lines else "(none)"))

        # -- route: open ticket ---------------------------------------------
        if "ticket" in tl:
            cids = _find_ids(task, "C")
            if not cids:
                return ("answer", "I need a customer ID (e.g. C-102) to open a ticket.")
            subject = self._extract_subject(task)
            tkey = ("open_ticket", json.dumps(
                {"customer_id": cids[0], "subject": subject}, sort_keys=True))
            if tkey not in done:
                return ("call", "open_ticket",
                        {"customer_id": cids[0], "subject": subject})
            ticket = payload("open_ticket").get("ticket", {})
            return ("answer", f"Opened ticket {ticket.get('id')} for "
                              f"{cids[0]}: \"{ticket.get('subject')}\" "
                              f"(priority {ticket.get('priority')}).")

        return ("answer", "I'm not sure how to handle that task with the "
                          "available tools.")

    # -- helpers ---------------------------------------------------------
    def _terminal(self, history):
        """After a place_order call the task is done — render the answer."""
        if history and history[-1]["tool"] == "place_order":
            r = history[-1]["result"]
            if r.get("isError"):
                return ("answer", f"The order failed: {r['content'][0]['text']}")
            order = json.loads(r["content"][0]["text"])["order"]
            items = ", ".join(f"{l['quantity']}× {l['product_id']} @ "
                              f"${l['unit_price_usd']:.2f}"
                              for l in order["items"])
            return ("answer", f"Order {order['id']} placed for "
                              f"{order['customer_id']}: {items}. "
                              f"Total ${order['total_usd']:.2f}.")
        return None

    def _describe_order(self, order):
        if not order:
            return "I couldn't retrieve that order."
        items = ", ".join(f"{l['quantity']}× {l['product_id']}"
                          for l in order.get("items", []))
        return (f"Order {order.get('id')} ({order.get('customer_id')}) is "
                f"**{order.get('status')}**: {items} — "
                f"total ${order.get('total_usd', 0):.2f}.")

    def _describe_low_stock(self, history, done):
        # Kept for documentation; the live path is the ("resource", …) action.
        return ("answer", "Use the inventory://low-stock resource for the "
                          "current low-stock list.")

    @staticmethod
    def _extract_subject(task):
        m = re.search(r"ticket[^:]*:\s*[\"']?(.+?)[\"']?\s*$", task, re.I | re.S)
        if m:
            return m.group(1).strip().strip("\"'")
        m = re.search(r"about\s+(.+?)\s*$", task, re.I)
        return m.group(1).strip() if m else "General support request"


class LLMPolicy:
    """Let an OpenAI-compatible chat model choose each action.

    Enable with HELIOS_AGENT_LLM=1. Reads HELIOS_LLM_BASE_URL,
    HELIOS_LLM_API_KEY, HELIOS_LLM_MODEL. Not used by tests/evals (they use
    ScriptedPolicy) — this is the documented upgrade path to a real brain.
    """

    SYSTEM = ("You are an agent operating a store's MCP tools. Reply with ONE "
              "JSON object: either {\"action\": \"call\", \"tool\": NAME, "
              "\"arguments\": {...}} or {\"action\": \"answer\", \"text\": "
              "\"...\"}. Only cite IDs, prices and quantities that appeared "
              "in tool observations — never invent them. If a tool errors, "
              "explain the error and suggest a fix instead of retrying blindly.")

    def __init__(self, tools):
        import urllib.request  # local import: never needed unless enabled
        self._urllib = urllib.request
        self.tools = tools
        self.base_url = os.environ["HELIOS_LLM_BASE_URL"].rstrip("/")
        self.api_key = os.environ["HELIOS_LLM_API_KEY"]
        self.model = os.environ.get("HELIOS_LLM_MODEL", "gpt-4o-mini")

    def next_action(self, task, history):
        tool_desc = "\n".join(
            f"- {t['name']}: {t['description']} "
            f"args={json.dumps(t['inputSchema'])}" for t in self.tools)
        convo = [{"role": "system", "content": self.SYSTEM},
                 {"role": "user",
                  "content": f"Tools:\n{tool_desc}\n\nTask: {task}"}]
        for h in history:
            convo.append({"role": "assistant",
                          "content": json.dumps({"action": "call",
                                                 "tool": h["tool"],
                                                 "arguments": h["args"]})})
            convo.append({"role": "user",
                          "content": f"Observation: {json.dumps(h['result'])}"})
        body = json.dumps({"model": self.model, "messages": convo,
                           "temperature": 0, "response_format": {"type": "json_object"}})
        req = self._urllib.Request(
            self.base_url + "/chat/completions", data=body.encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with self._urllib.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        decision = json.loads(data["choices"][0]["message"]["content"])
        if decision.get("action") == "call":
            return ("call", decision["tool"], decision.get("arguments", {}))
        return ("answer", decision.get("text", ""))


# ---------------------------------------------------------------------------
# Driver: the observe → act → answer loop
# ---------------------------------------------------------------------------

MAX_STEPS = 8

def solve(task, policy=None, client=None, verbose=False, keep_results=False):
    """Run one task. Returns {"task","steps","answer"}.

    With keep_results=True, each step also carries the full tool "result"
    (used by evals to verify side effects from observations, not answer text).
    """
    own_client = client is None
    client = client or MCPClient()
    try:
        client.initialize()
        tools = client.list_tools()
        policy = policy or (LLMPolicy(tools) if os.environ.get("HELIOS_AGENT_LLM")
                            else ScriptedPolicy(tools))
        history, steps = [], []
        for _ in range(MAX_STEPS):
            # Terminal check for scripted multi-step flows (order placed, etc.)
            if isinstance(policy, ScriptedPolicy):
                term = policy._terminal(history)
                if term:
                    return {"task": task, "steps": steps, "answer": term[1]}
            action = policy.next_action(task, history)
            kind = action[0]
            a = action[1] if len(action) > 1 else None
            b = action[2] if len(action) > 2 else None
            if kind == "answer":
                return {"task": task, "steps": steps, "answer": a}
            if kind == "resource":
                content = client.read_resource(a)
                result = {"content": [{"type": "text", "text": content["text"]}],
                          "isError": False}
                history.append({"tool": f"resource:{a}", "args": {},
                                "result": result})
                step = {"tool": f"resource:{a}", "args": {}, "ok": True}
                if keep_results:
                    step["result"] = result
                steps.append(step)
                if verbose:
                    print(f"  → read {a} OK")
                continue
            result = client.call_tool(a, b)
            history.append({"tool": a, "args": b, "result": result})
            step = {"tool": a, "args": b,
                    "ok": not result.get("isError", False)}
            if keep_results:
                step["result"] = result
            steps.append(step)
            if verbose:
                print(f"  → {a}({json.dumps(b)}) "
                      f"{'OK' if steps[-1]['ok'] else 'ERROR'}")
            if result.get("isError"):
                # Surface tool errors to the answer instead of looping forever.
                err_text = result["content"][0]["text"]
                hint = result.get("_hint", "")
                if isinstance(policy, ScriptedPolicy):
                    # Let the scripted policy render the failure gracefully.
                    graceful = policy.next_action(task, history)
                    if graceful[0] == "answer":
                        return {"task": task, "steps": steps,
                                "answer": graceful[1]}
                return {"task": task, "steps": steps,
                        "answer": f"The task failed at tool '{a}': {err_text} "
                                  f"{hint}".strip()}
        return {"task": task, "steps": steps,
                "answer": "I ran out of steps before finishing."}
    finally:
        if own_client:
            client.close()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Helios demo agent client")
    ap.add_argument("task", nargs="?", default=None,
                    help="task text; omit for the built-in showcase")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    tasks = [a.task] if a.task else [
        "How many units of HX-1042 (Vista Outdoor Cam) are in stock?",
        "Buy 2 of the cheapest in-stock smart lock for customer C-103.",
        "What is the status of customer C-104's most recent order?",
    ]
    for t in tasks:
        print(f"\nTask: {t}")
        r = solve(t, verbose=a.verbose)
        print(f"Answer: {r['answer']}")
        print(f"(steps: {len(r['steps'])})")


if __name__ == "__main__":
    main()
