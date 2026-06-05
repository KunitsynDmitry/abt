# ABT (Agent Building Tool)

> Declarative agent framework inspired by dbt. Compile `.prompt` files + YAML schemas into LangGraph agents with SQLite persistence.

## Quick Start

```bash
pip install -e .
abt init my_agent
cd my_agent
abt compile                  # → target/manifest.json
abt run                      # execute with SQLite tracing
abt run --refresh            # force re-execute all nodes
abt run --trigger daily_check  # simulate a trigger
abt run -v                   # stream LLM tokens
abt test                     # validate assertions
abt serve                    # HTTP server + scheduler
```

## Philosophy

dbt taught us: **declarative > imperative**. abt applies this to agents:

| dbt concept | abt equivalent |
|-------------|---------------|
| `.sql` model | `.prompt` file |
| `{{ ref('model') }}` | `{{ ref('node') }}` — dependency |
| `{{ source('src', 'table') }}` | `{{ source('api', 'tool') }}` — tool binding |
| `{{ config(...) }}` | LLM params, retries, routing, approval gates |
| `schema.yml` | Pydantic model definition |
| `sources.yml` | Tool/API registration |
| `exposures.yml` | Triggers (schedule, webhook, message) |
| DAG | LangGraph StateGraph |
| `dbt run` | `abt run` |
| `dbt test` | `abt test` |
| `--select` / `--exclude` | `--select +name+` / `--exclude` |
| CTE (`WITH ... AS`) | Micro-steps within a node |

## Three Levels of Control

| Level | Scope | Mechanism |
|-------|-------|-----------|
| **Folder** | Transitions between files | LangGraph edge (SEQUENTIAL / REQUIRE_ALL / REQUIRE_ANY) |
| **File (.prompt)** | One LLM + tool loop | One LangGraph node |
| **CTE block** | Steps within a file | Internal routing (on_fail_route, allowed_tools, AS TOOL/AS LLM) |

Everything inside a file is the LLM's responsibility. Everything outside is the compiler's + LangGraph's.

## Project Structure

```
my_agent/
├── abt_project.yml              # Project config, vars, model defaults
├── schemas/
│   └── my_schema.yml            # Pydantic models (output contracts)
├── sources/
│   └── apis.yml                 # Tool/API registrations (REST, MCP, Python)
├── triggers/
│   └── my_agent.triggers.yml    # Trigger definitions (schedule, webhook, message)
├── blueprints/                  # Reusable subgraphs (referenced via _folder_name)
│   └── approval/
│       └── request_approval.prompt
├── prompts/
│   ├── require_all/             # AND gate — all children must complete
│   │   ├── check_x.prompt
│   │   └── check_y.prompt
│   ├── require_any__fast/       # OR gate — runs all branches, first non-error wins (metadata: tag=fast)
│   │   ├── fallback_a.prompt
│   │   └── fallback_b.prompt
│   └── decide.prompt            # Depends on results from require_all/*
├── macros/
│   └── helpers.jinja            # Shared Jinja macros
└── target/
    └── manifest.json            # Single compilation artifact (7 sections)
```

## .prompt File Syntax

```sql
{{ config(temperature=0.3, max_tool_calls=5, output_schema="inventory_analysis",
          route_on="priority",
          route_when=["high:escalate", "medium:auto_order"],
          route_default="__END__",
          approve_when="total_order_cost > 5000") }}

You are an inventory decision agent.

WITH stock_data AS TOOL (
    -- Tool CTE: calls external API, forever cacheable
    SELECT * FROM {{ source('warehouse_api', 'current_stock') }}
    WHERE product_id = '{{ product_id }}'
),

analyze AS LLM (
    -- LLM CTE: reasoning step, never cacheable
    Analyze stock levels. Items below {{ var('low_stock_threshold') }} need reorder.
)

SELECT
    stock_status,            -- str: OK | LOW | CRITICAL
    total_order_cost,        -- float
    priority                 -- str: low | medium | high
FROM analyze
```

- `{{ config(...) }}` — model, temperature, retries, `output_schema`, `route_on`/`route_when`, `approve_when`
- `{{ ref('node_name') }}` — dependency on another prompt's output
- `{{ source('src', 'table') }}` — bind a tool to this node
- `{{ var('name') }}` — project variable from abt_project.yml
- `{{ env_var('NAME') }}` — environment variable
- `WITH name AS TOOL (...)` — tool CTE (API call, cacheable)
- `WITH name AS LLM (...)` — LLM CTE (reasoning step)
- `SELECT column1, column2 FROM last_cte` — output schema definition

## Schema YAML → Pydantic

```yaml
# schemas/inventory_schema.yml
version: 1
models:
  - name: inventory_analysis
    fields:
      - name: stock_status
        type: str
        constraints:
          enum: ["OK", "LOW", "CRITICAL"]
      - name: total_order_cost
        type: float
        constraints:
          ge: 0.0
```

Compiles to a Pydantic BaseModel with full validation. LLM output is validated at runtime — validation errors are fed back for self-correction on retry.

## Sources YAML → Tools

```yaml
# sources/apis.yml
version: 1
sources:
  - name: warehouse_api
    type: rest_api
    config:
      base_url: "https://warehouse.internal/api/v1"
    tables:
      - name: current_stock
        endpoint: "/inventory"
        method: GET
        params:
          product_id: {type: str, required: true}
```

Supports `rest_api`, `mcp_server`, `python_function`.

## Triggers (dbt exposures pattern)

```yaml
# triggers/inventory.triggers.yml
version: 1
agent: inventory_agent
triggers:
  - name: daily_check
    type: schedule
    schedule: "0 9 * * *"
    input:
      mode: full_scan
  - name: low_stock_alert
    type: webhook
    path: "/hooks/low-stock"
    method: POST
    input:
      mapping:
        product_id: "$.body.sku"
        current_qty: "$.body.quantity"
```

```bash
abt serve                          # Start HTTP server + cron scheduler
abt serve --port 8080 --no-scheduler  # Custom port, webhooks only
abt run --trigger daily_check      # Simulate a trigger from CLI
```

## Testing

```yaml
# check_stock.test.yml
tests:
  - name: stock_not_negative
    assert: quantity_on_hand >= 0
  - name: location_not_empty
    assert: location is not None
```

```bash
abt test                           # All tests
abt test --select check_stock      # Specific node
```

Safe eval with restricted `__builtins__` (no `__import__`, `open`, etc). Supports `==`, `!=`, `>`, `<`, `>=`, `<=`, `in`, `not in`, `is`, `is not`, `and`, `or`, `+`, `-`, `*`, `/`.

## SQLite Observability

Every run writes to 5 tables:

- **agent_runs** — top-level execution (run_id, status, final_state)
- **node_executions** — per-node tracking (started_at, completed_at, retries, errors)
- **llm_traces** — full LLM request/response audit (messages, tool_calls, tokens)
- **tool_results** — cached tool call results (dedup across retries)
- **node_cache** — incremental execution cache (node_name, inputs_hash, output)

Query with standard SQL: `SELECT * FROM llm_traces WHERE run_id = '...' ORDER BY created_at`

## Intentional Context Passing (SELECT, not inherit)

Nodes don't auto-inherit all previous state. Each node explicitly SELECTs what it needs:

```sql
WITH cleaned_data AS (
    SELECT article_id, competitor_price
    FROM {{ ref('market_research') }}
    WHERE confidence_score > 0.8
)
```

This prevents context bloat — the LLM only sees relevant, filtered data.

## Key Features (v1.0.0)

- **Incremental compilation** — SHA256 fingerprints, `manifest.json` as cache, `--full-refresh`
- **Incremental execution** — `node_cache` table, input hashing, `--refresh` flag
- **dbt-style selectors** — `+name+`, `tag:`, `path:`, `source:`, glob, `--exclude`
- **Native token streaming** — LangGraph `get_stream_writer()` + `app.stream()`
- **Pydantic output validation** — LLM output validated against schema, errors fed back on retry
- **Compile-time ref() contracts** — `GraphBuildError` on unresolved dependencies
- **Explicit CTE types** — `AS TOOL` (cacheable) vs `AS LLM` (never cached)
- **Dynamic routing** — `route_on`/`route_when`/`route_default` with conditional edges
- **Human-in-the-loop** — `approve_when`/`approve_message` with `interrupt()` gates
- **Nested subgraphs** — recursive compilation, 3+ levels of nesting
- **MCP client** — persistent stdio connections with SQLite caching

## Architecture (source code)

```
abt/
├── models/         # Pydantic data contracts (7 files)
│   ├── config.py   # AbtProjectConfig, ProjectPaths
│   ├── schema.py   # SchemaField, SchemaModel (YAML ↔ Pydantic bridge)
│   ├── source.py   # SourceDefinition, SourceTable, ToolType
│   ├── prompt.py   # CTEBlock, ParsedPrompt, PromptConfig
│   ├── node.py     # CompiledNode (resolved, ready for graph)
│   ├── graph.py    # SubgraphDef, RoutingType, GraphStructure
│   └── trigger.py  # TriggerType, TriggerInput, TriggerDefinition, TriggerFile
├── compiler/       # Parse → compile pipeline (12 files)
│   ├── factory.py              # pydantic.create_model() from SchemaModel
│   ├── schema_parser.py        # schema.yml → {name: PydanticClass}
│   ├── source_parser.py        # sources.yml → {name: SourceDefinition}
│   ├── trigger_parser.py       # triggers.yml → {name: TriggerDefinition}
│   ├── jinja_env.py            # Jinja2 + ref/source/config/var/env_var
│   ├── cte_parser.py           # WITH...AS extraction, SELECT parsing
│   ├── prompt_compiler.py      # .prompt → ParsedPrompt (full pipeline)
│   ├── folder_parser.py        # Dir tree → SubgraphDef with routing
│   ├── graph_builder.py        # SubgraphDef + CompiledNode → StateGraph
│   ├── manifest_generator.py   # GraphStructure → manifest.json (7 sections)
│   ├── selector.py             # dbt-style node selection (+name+, tag:, path:, glob)
│   ├── fingerprint.py          # SHA256 file hashing
│   └── cache_manager.py        # Loads prev manifest, detects staleness, merges cache
├── runtime/        # Execution layer (8 files)
│   ├── db.py              # SQLite DDL, tracing, caching (5 tables)
│   ├── tool_table.py      # SourceDefinition → callable Python tools
│   ├── mcp_client.py      # McpConnection + McpManager — persistent MCP stdio
│   ├── node_runner.py     # CTE loop, retries, ref resolution, Pydantic validation, streaming
│   ├── test_runner.py     # .test.yml discovery, safe eval assertions
│   ├── executor.py        # GraphExecutor: topological sort, execution, traces, dynamic routing
│   ├── trigger_manager.py # TriggerManager: resolve_input(), activate(), JSONPath resolution
│   └── server.py          # Starlette app factory: dynamic webhook routes + utility endpoints
├── cli.py          # Click CLI: init, compile, run, test, serve
├── project.py      # ProjectLoader: reads abt_project.yml, discovers files
└── exceptions.py   # Custom exception hierarchy
```

## Key Design Decisions

1. **One file = one node.** Compiler does NOT create separate graph nodes per `{{ source() }}`. Tool calls run inside the node as `llm.bind_tools()` loop.

2. **Explicit SELECT for context passing.** No giant auto-inherited BaseState. Each node picks what it needs from previous outputs via `{{ ref() }}` in CTE blocks.

3. **Folder name encodes routing.** `require_all/` → AND gate, `require_any__<meta>/` → OR gate. Plain names → sequential.

4. **YAML schemas → Pydantic at compile time.** Using `pydantic.create_model()`, not metaclass magic.

5. **SQLite is the source of truth.** Checkpoints, traces, tool cache, and node cache all in one DB. Queryable with SQL.

6. **Manifest.json is the single compilation artifact.** No generated Python code. Everything runtime needs is in manifest.json.

## LLM Setup

Uses OpenAI-compatible API. Default: DeepSeek.

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"   # Optional, this is the default
```

Per-project model in `abt_project.yml`:
```yaml
models:
  default:
    provider: deepseek
    model: deepseek-chat
    temperature: 0.7
    max_tokens: 4096
```

Per-file override: `{{ config(model="deepseek-reasoner", temperature=0.1) }}`

## Running Tests

```bash
python tests/test_integration.py          # End-to-end (example project)
python tests/test_phase5_smoke.py         # Runtime smoke test
python tests/test_nested_subgraphs.py     # Nested subgraph compilation
python tests/test_selectors.py            # dbt-style selectors
python tests/test_triggers.py             # Triggers + serve
```
