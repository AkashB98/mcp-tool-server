# helios-mcp-server

**The unglamorous glue every AI team ships: take an internal REST API and expose it to agents as MCP tools, resources, and prompts — with schemas, error mapping, tracing, and evals proving the agent picks the right tool with the right arguments.**

Every forward-deployed AI team ends up building exactly this layer. An agent that freestyles raw HTTP calls against your internal API is a billing incident waiting to happen: no validation, no auth story, no observability, and no proof it does the right thing. This project is the disciplined version — a [Model Context Protocol](https://modelcontextprotocol.io/) server wrapping a fictional smart-home store API ("Helios Home"), plus a demo agent client and a golden-task eval harness that scores tool selection, argument correctness, and task completion.

## What it proves

- **API-to-agent integration** — wrapping existing REST semantics as typed MCP tools with JSON Schema validation, instead of letting agents improvise HTTP.
- **Production concerns, not just the happy path** — auth injection, rate limiting (token bucket), atomic order validation, and REST errors mapped to actionable MCP errors with hints the agent can act on.
- **Evaluation discipline** — 7 golden tasks scored on tool choice, exact argument match, answer correctness, *and* side effects verified from tool observations (never from answer text). Includes an anti-hallucination task: the agent must say "not found", never invent a price.
- **Observability** — every tool call traced to JSONL (tool, args, latency, ok/error).

## Quickstart

```bash
# stdlib only — no dependencies to install
python demo.py                          # watch the agent solve 3 tasks live

python agent_client.py "Buy 2 of the cheapest in-stock smart lock for customer C-103."

python -m unittest discover -s tests    # 74 hermetic tests
python evals/run_evals.py               # 7 golden tasks → evals/eval_report.json
```

Use it from any MCP-compatible client (stdio transport):

```bash
python mcp_server.py --trace trace.jsonl
```

`claude_desktop_config.example.json` shows the client config snippet.

## Architecture

```
┌──────────────┐   stdio JSON-RPC 2.0   ┌───────────────────┐
│ demo agent   │ ◄───────────────────► │ helios-mcp-server │
│ (scripted    │   initialize/tools/    │  8 tools · 3      │
│  policy, or  │   resources/prompts    │  resources · 2    │
│  LLM policy) │                        │  prompts · tracing│
└──────────────┘                        └────────┬──────────┘
                                                 │ in-process (status, body)
                                        ┌────────▼──────────┐
                                        │  HeliosRestApi    │
                                        │  fictional store  │
                                        │  21 products ·    │
                                        │  deterministic    │
                                        └───────────────────┘
```

- **`helios_api.py`** — the fictional REST API, simulated in-process as `(status_code, body)` tuples: products, inventory, customers, orders, tickets. Seeded fixtures (seed 42) make every run bit-identical. `HttpRestApi` adapts the same surface to a real HTTP backend (`HELIOS_API_BASE_URL` + `HELIOS_API_KEY`).
- **`mcp_server.py`** — the MCP server: `initialize`, `tools/list`, `tools/call` (schema-validated), `resources/list`/`read`, `prompts/list`/`get`, per-call tracing, and REST→MCP error mapping (`404 → "check the ID spelling"`, `409 → "tell the user what's available"`, `429 → "back off and retry"`).
- **`agent_client.py`** — the demo agent: spawns the server as a subprocess and runs an observe → act → answer loop. `ScriptedPolicy` is the deterministic default (what the evals use); set `HELIOS_AGENT_LLM=1` with `HELIOS_LLM_BASE_URL` / `HELIOS_LLM_API_KEY` / `HELIOS_LLM_MODEL` to swap in an OpenAI-compatible model as the decision-maker — same loop, same schemas.

## Tools, resources, prompts

| Tool | What it does |
|---|---|
| `search_products` | keyword + category + in-stock search |
| `get_product` | one product by ID |
| `check_inventory` | warehouse units + low-stock flag |
| `place_order` | atomic order creation (all-or-nothing stock reservation) |
| `get_order` / `list_orders` | order lookup, newest-first, filterable |
| `get_customer` | customer lookup |
| `open_ticket` | support ticket with priority |

| Resource | What it gives |
|---|---|
| `inventory://low-stock` | products under 5 units, with reorder suggestions |
| `orders://recent` | 10 most recent orders |
| `customers://{id}/orders` | one customer's history |

| Prompt | What it renders |
|---|---|
| `reorder-plan` | restock-planning prompt seeded with live low-stock data |
| `order-status-check` | customer-facing status summary grounded in real order JSON |

## Evals

`python evals/run_evals.py` — 7/7 passing (report committed at `evals/eval_report.json`):

| Task | Scored on |
|---|---|
| stock-check | right tool, exact `product_id` arg, answer cites 17 units |
| cheapest-lock-order | `search_products` → cheapest → `place_order`; order actually created, total $238.00 |
| order-status | finds C-104's most recent order (ORD-5005), reports `processing` |
| low-stock-report | resource read; all 4 low-stock IDs in the answer |
| open-ticket | ticket actually created for the right customer |
| unknown-product | **anti-hallucination**: says "not found", quotes no price, creates nothing |
| cheapest-bulbs-order | multi-step: cheapest in-stock bulb → order 3 → $72.00 |

Each task runs against a **fresh server** (fresh fixtures), so state never leaks between tasks. Run twice, diff the report — it's identical.

## Dev loop: what the tests caught

The evals and tests caught 3 real bugs mid-build (this is the point of the harness):

1. **Quantity regex read digits inside customer IDs** — "Customer C-106 wants 3 bulbs" parsed quantity as **106** and the order failed on insufficient stock. Fixed with a lookbehind so `\d+` never matches inside IDs.
2. **`get_order` returns the bare order object, not `{"order": …}`** — the agent's status answer silently fell back to "couldn't retrieve that order" on the by-ID path (the by-customer path had accidentally masked it via a fallback default). Now the policy handles the API's real shape.
3. **Action-tuple arity** — `("answer", text)` vs `("call", tool, args)` unpacked rigidly in the driver; the driver now unpacks flexibly.

74/74 hermetic unit tests pass (`python -m unittest discover -s tests`). No network, no clock, no randomness anywhere in the suite.

## Sample data & plugging in a real API

Everything here is fictional: Helios Home, its 21 products, 6 customers, 8 orders — all invented, seeded, and deterministic. Nothing personal, no real credentials. The demo API key is literally `"helios-demo-key"`.

To wrap a real API: implement the `HeliosRestApi` method surface (or point `HttpRestApi` at your base URL) — the MCP layer, agent, and evals don't change. That's the whole point of the seam.

## File map

```
helios_api.py          fictional REST API (simulated) + HttpRestApi adapter
mcp_server.py          MCP server over stdio JSON-RPC
agent_client.py        demo agent (scripted policy + LLM policy hook)
demo.py                3-task live showcase
evals/
  golden_tasks.json    7 tasks with expected tools/args/answers/side effects
  run_evals.py         the harness
  eval_report.json     committed results (7/7)
tests/                 74 hermetic unit tests
claude_desktop_config.example.json   client config snippet
```

## License

MIT — see `LICENSE`.
