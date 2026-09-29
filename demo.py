#!/usr/bin/env python3
"""End-to-end showcase: the demo agent solving three tasks live.

Each task spawns a fresh server (fresh fixtures), so the demo is
deterministic — run it twice, get the same transcript.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_client import solve  # noqa: E402

SHOWCASE = [
    "How many units of HX-1042 (Vista Outdoor Cam) are in stock?",
    "Buy 2 of the cheapest in-stock smart lock for customer C-103.",
    "Which products are low on stock (fewer than 5 units)?",
]


def main():
    print("=" * 70)
    print("helios-mcp-server — live demo")
    print("A demo agent driving 8 MCP tools + resources over stdio JSON-RPC.")
    print("=" * 70)
    for task in SHOWCASE:
        print(f"\n>>> Task: {task}")
        result = solve(task, verbose=True)
        print(f"<<< Answer: {result['answer']}")
        print(f"    ({len(result['steps'])} tool step(s))")
    print("\nDone. Try your own:  python agent_client.py \"<your task>\"")


if __name__ == "__main__":
    main()
