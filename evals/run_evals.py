#!/usr/bin/env python3
"""Golden-task evals for helios-mcp-server + demo agent.

Spawns a FRESH server per task (fresh fixtures), solves the task with the
scripted policy, and scores:
  - tools:      expected tools appear in order (as a subsequence of steps)
  - args:       key arguments match exactly on at least one call
  - answer:     expected substrings present, forbidden substrings absent
  - side_effect: orders/tickets actually created, totals correct
                 (verified from tool observations, never from answer text)

Writes evals/eval_report.json. Fully deterministic: run it twice, diff it.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from agent_client import MCPClient, solve  # noqa: E402


def _is_subsequence(needles, haystack):
    it = iter(haystack)
    return all(any(n == h for h in it) for n in needles)


def _created(steps, tool_name, key):
    """Orders/tickets created during the run, from tool observations."""
    out = []
    for s in steps:
        if s["tool"] != tool_name:
            continue
        r = s.get("result", {})
        if r.get("isError"):
            continue
        try:
            data = json.loads(r["content"][0]["text"])
        except Exception:
            continue
        if key in data:
            out.append(data[key])
    return out


def run_task(task_def):
    client = MCPClient()
    try:
        result = solve(task_def["task"], client=client, keep_results=True)
    finally:
        client.close()
    expect = task_def["expect"]
    tool_names = [s["tool"] for s in result["steps"]]
    checks = {}

    # 1. tool selection (subsequence, in order)
    checks["tools"] = _is_subsequence(expect.get("tools", []), tool_names)

    # 2. argument correctness (subset match on at least one call per tool)
    arg_ok, arg_detail = True, {}
    for tname, want_args in expect.get("arg_checks", {}).items():
        calls = [s for s in result["steps"] if s["tool"] == tname]
        hit = any(all(s["args"].get(k) == v for k, v in want_args.items())
                  for s in calls)
        arg_detail[tname] = hit
        arg_ok = arg_ok and hit
    checks["args"] = arg_ok
    checks["arg_detail"] = arg_detail

    # 3. answer content
    ans_l = result["answer"].lower()
    checks["answer_contains"] = all(s.lower() in ans_l
                                    for s in expect.get("answer_contains", []))
    checks["answer_not_contains"] = not any(
        s.lower() in ans_l for s in expect.get("answer_not_contains", []))

    # 4. side effects, from observations
    se = expect.get("side_effect", {})
    se_ok = True
    if "new_orders" in se or "order_total" in se or "product" in se:
        orders = _created(result["steps"], "place_order", "order")
        se_ok = se_ok and len(orders) == se.get("new_orders", len(orders))
        if "order_total" in se and orders:
            se_ok = se_ok and abs(orders[0]["total_usd"] - se["order_total"]) < 0.005
        if "product" in se and orders:
            pids = {l["product_id"] for o in orders for l in o["items"]}
            se_ok = se_ok and se["product"] in pids
    if "new_tickets" in se:
        tickets = _created(result["steps"], "open_ticket", "ticket")
        se_ok = se_ok and len(tickets) == se["new_tickets"]
    checks["side_effect"] = se_ok

    passed = all(v for k, v in checks.items() if k != "arg_detail")
    return {"id": task_def["id"], "task": task_def["task"],
            "pass": passed, "checks": checks,
            "steps": [{"tool": s["tool"], "args": s["args"], "ok": s["ok"]}
                      for s in result["steps"]],
            "answer": result["answer"]}


def main():
    with open(os.path.join(HERE, "golden_tasks.json")) as f:
        tasks = json.load(f)
    results = [run_task(t) for t in tasks]
    report = {"tasks": results,
              "summary": {"total": len(results),
                          "passed": sum(1 for r in results if r["pass"]),
                          "failed": sum(1 for r in results if not r["pass"])}}
    out = os.path.join(HERE, "eval_report.json")
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"{report['summary']['passed']}/{report['summary']['total']} "
          f"golden tasks passed → {out}")
    for r in results:
        mark = "PASS" if r["pass"] else "FAIL"
        bad = [k for k, v in r["checks"].items()
               if k != "arg_detail" and not v]
        print(f"  [{mark}] {r['id']}" + (f"  failed: {bad}" if bad else ""))
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
