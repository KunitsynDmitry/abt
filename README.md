# ABT (Agent Building Tool)

> Declarative agent framework inspired by dbt. Compile `.prompt` files + YAML schemas into LangGraph agents with SQLite persistence.

## Quick Start

```bash
pip install -e .
abt init my_agent
cd my_agent
abt compile       # → target/compiled_graph.py
abt run           # execute with SQLite tracing
abt test          # validate schemas and sources
python target/compiled_graph.py  # standalone execution
```

## Philosophy

dbt taught us: **declarative > imperative**. abt applies this to agents:

| dbt concept | abt equivalent |
|-------------|---------------|
| `.sql` model | `.prompt` file |
| `{{ ref('model') }}` | `{{ ref('node') }}` — dependency |
| `{{ source('src', 'table') }}` | `{{ source('api', 'tool') }}` — tool binding |
| `{{ config(...) }}` | LLM params, retries, routing |
| `schema.yml` | Pydantic model definition |
| `sources.yml` | Tool/API registration |
| DAG | LangGraph StateGraph |
| `dbt run` | `abt run` |
| `dbt test` | `abt test` |
| CTE (`WITH ... AS`) | Micro-steps within a node |

## Three Levels of Control

| Level | Scope | Mechanism |
|-------|-------|-----------|
| **Folder** | Transitions between files | LangGraph edge (SEQUENTIAL / REQUIRE_ALL / REQUIRE_ANY) |
| **File (.prompt)** | One LLM + tool loop | One LangGraph node |
| **CTE block** | Steps within a file | Internal routing (on_fail_route, allowed_tools, retries) |

Everything inside a file is the LLM's responsibility. Everything outside is the compiler's + LangGraph's.

## Project Structure

```
my_agent/
├── abt_project.yml          # Project config, vars, model defaults
├── schemas/
│   └── my_schema.yml        # Pydantic models (output contracts)
├── sources/
│   └── apis.yml             # Tool/API registrations (REST, MCP, Python)
├── prompts/
│   ├── require_all/         # AND gate — all children must complete
│   │   ├── check_x.prompt
│   │   └── check_y.prompt
│   ├── require_any__fast/   # OR gate — first success wins (metadata: tag=fast)
│   │   ├── fallback_a.prompt
│   │   └── fallback_b.prompt
│   └── decide.prompt        # Depends on results from require_all/*
├── macros/
│   └── helpers.jinja        # Shared Jinja macros
└── target/
    └── compiled_graph.py    # Generated standalone Python
```

## .prompt File Syntax

```sql
{{ config(temperature=0.3, max_tool_calls=5, output_schema="inventory_analysis") }}

You are an inventory decision agent.

WITH stock_data AS (
    -- Tool CTE: detected by SELECT FROM source
    SELECT * FROM {{ source('warehouse_api', 'current_stock') }}
    WHERE product_id = '{{ product_id }}'
),

analyze AS (
    -- LLM CTE: reasoning step
    Analyze stock levels. Items below {{ var('low_stock_threshold') }} need reorder.
)

SELECT
    stock_status,            -- str: OK | LOW | CRITICAL
    total_order_cost,        -- float
    priority                 -- str: low | medium | high
FROM analyze
```

- `{{ config(...) }}` — file-level settings (model, temperature, retries, on_fail_route)
- `{{ ref('node_name') }}` — dependency on another prompt's output
- `{{ source('src', 'table') }}` — bind a tool to this node
- `{{ var('name') }}` — project variable from abt_project.yml
- `{{ env_var('NAME') }}` — environment variable
- `WITH name AS (...)` — CTE block (tool step or LLM step)
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

Compiles to a Pydantic BaseModel with full validation. `{{ ref('inventory_analysis') }}` in a prompt injects the JSON schema.

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

Supports `rest_api`, `mcp_server`, `python_function`, `graphql`.

## SQLite Observability

Every run writes to 4 tables:

- **agent_runs** — top-level execution (run_id, status, final_state)
- **node_executions** — per-node tracking (started_at, completed_at, retries, errors)
- **llm_traces** — full LLM request/response audit (messages, tool_calls, tokens)
- **tool_results** — cached tool call results (dedup across retries)

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

## Architecture (source code)

```
abt/
├── models/        # Pydantic data contracts
│   ├── config.py  # AbtProjectConfig, ProjectPaths
│   ├── schema.py  # SchemaField, SchemaModel (YAML ↔ Pydantic bridge)
│   ├── source.py  # SourceDefinition, SourceTable, ToolType
│   ├── prompt.py  # CTEBlock, ParsedPrompt, PromptConfig
│   ├── node.py    # CompiledNode (resolved, ready for graph)
│   └── graph.py   # SubgraphDef, RoutingType, GraphStructure
├── compiler/      # Parse → compile pipeline
│   ├── factory.py         # pydantic.create_model() wrapper
│   ├── schema_parser.py   # schema.yml → Pydantic model dict
│   ├── source_parser.py   # sources.yml → SourceDefinition dict
│   ├── jinja_env.py       # Jinja2 with ref(), source(), config()
│   ├── cte_parser.py      # WITH...AS extraction, SELECT parsing
│   ├── prompt_compiler.py # .prompt file → ParsedPrompt
│   ├── folder_parser.py   # Directory tree → SubgraphDef tree
│   └── graph_builder.py   # Tree + nodes → LangGraph StateGraph + Python codegen
├── runtime/       # Execution layer
│   ├── db.py             # SQLite DDL, tracing, caching
│   ├── tool_table.py     # SourceDefinition → callable tools
│   ├── node_runner.py    # CTE execution loop with retries
│   └── executor.py       # Graph runner, topological sort
├── cli.py         # Click CLI: init, compile, run, test
├── project.py     # ProjectLoader
└── exceptions.py  # Error hierarchy
```

## Key Design Decisions

1. **One file = one node.** Compiler does NOT create micro-graphs per tool. Tool calls happen inside the node as `llm.bind_tools()` loop. (Decision from user + senior model analysis.)

2. **Explicit SELECT for context passing.** No giant auto-inherited BaseState. Each node picks what it needs from previous outputs via `{{ ref() }}` in CTE blocks.

3. **CTE-level tool control.** Individual CTE blocks can restrict which tools are available via `{{ config(allowed_tools=[...]) }}`.

4. **Folder name encodes routing.** `require_all/` → AND gate, `require_any__<meta>/` → OR gate. Plain names → sequential.

5. **YAML schemas → Pydantic at compile time.** Using `pydantic.create_model()`, not metaclass magic.

6. **SQLite is the source of truth.** Checkpoints, traces, and tool cache all in one DB. Queryable with SQL.

## What's Implemented (v0.3.2)

- [x] Full CLI (init, compile, run, test) — wired to real pipeline
- [x] YAML schema → dynamic Pydantic models with enum/constraint validation
- [x] Source/tool registration (REST, MCP stubs, Python function)
- [x] Jinja environment with ref(), source(), config(), var(), env_var()
- [x] CTE parser (multi-line, single-line, tool-step detection)
- [x] Folder routing parser (require_all, require_any, metadata extraction)
- [x] Graph builder with dependency resolution and Python code generation
- [x] SQLite persistence (4 tables, full tracing, thread-safe)
- [x] Node runner with CTE execution loop, retries, ref resolution
- [x] Real LangGraph StateGraph — GraphExecutor builds StateGraph, wires edges
- [x] **Parallel execution for require_all** — fan-out to children, fan-in via AND-gate
- [x] **OR-gate for require_any** — fan-out, collector picks first success, all-fail → error
- [x] **Real LLM calls** via OpenAI-compatible API (DeepSeek), with mock factory for tests
- [x] **Nested subgraph compilation** — REQUIRE_ALL/REQUIRE_ANY folders → compiled StateGraphs added as nodes. SEQUENTIAL stays inline. Deep nesting (3+ levels) supported.
- [x] Project-level model defaults — configurable per project, per file, per code
- [x] Example project (inventory agent, 5 prompts, 4 schemas, 2 sources)
- [x] Generated Python code runs standalone with recursive subgraph support
- [x] All 5 test suites pass (integration, phase4, phase5, generated_python, nested_subgraphs)

## What's Next

- [ ] MCP client implementation in tool_table.py (currently returns stub)
- [ ] Token streaming (`abt run --stream`)
- [ ] Manifest file (manifest.json)
- [ ] Incremental compilation (only recompile changed files)
- [ ] dbt-style dependency selectors (`--select +model_name+`)
- [ ] Web UI for trace exploration over SQLite

## Running Tests

```bash
python tests/test_integration.py          # End-to-end (example project)
python tests/test_phase4_smoke.py         # Compiler
python tests/test_phase5_smoke.py         # Runtime
python tests/test_generated_python.py     # Codegen
python tests/test_nested_subgraphs.py     # Nested subgraph compilation
```
