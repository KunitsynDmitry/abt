# ABT — Agent Building Tool

Inspired by dbt. Compiles declarative `.prompt` files + YAML schemas into LangGraph agents with SQLite persistence.

## Key Commands

```bash
pip install -e .                        # Install in editable mode
python -c "from abt.cli import cli; cli(['init', 'name'], standalone_mode=False)"
python -c "from abt.cli import cli; cli(['compile'], standalone_mode=False)"
python -c "from abt.cli import cli; cli(['run', '-v'], standalone_mode=False)"
python -c "from abt.cli import cli; cli(['test'], standalone_mode=False)"
python example_project/target/compiled_graph.py   # Standalone generated code
```

Run tests:
```bash
python tests/test_integration.py
python tests/test_phase4_smoke.py
python tests/test_phase5_smoke.py
python tests/test_generated_python.py
python tests/test_nested_subgraphs.py
```

## Architecture (three levels)

| Level | Scope | Mechanism |
|-------|-------|-----------|
| Folder | Transitions between files | LangGraph edge (SEQUENTIAL/REQUIRE_ALL/REQUIRE_ANY) |
| File (.prompt) | LLM + tool loop | One LangGraph node |
| CTE block | Steps within file | Internal routing (on_fail_route, allowed_tools) |

## Core Design Decisions (do NOT change without discussion)

1. **One file = one node.** Compiler does NOT create separate graph nodes per `{{ source() }}`. Tool calls run inside the node as `llm.bind_tools()` loop.
2. **SELECT for context passing.** Nodes don't auto-inherit state. Each node explicitly does `SELECT ... FROM {{ ref('prev_node') }} WHERE ...` to pick what it needs.
3. **Folder name = routing.** `require_all/` = AND gate, `require_any__meta/` = OR gate. Plain name = sequential.
4. **YAML schemas → Pydantic via `create_model()`.** Not metaclass magic.
5. **SQLite is the source of truth** for checkpoints, traces, and tool cache.

## File map

```
abt/
├── models/         # Pydantic data contracts (6 files)
│   ├── config.py   # AbtProjectConfig, ProjectPaths
│   ├── schema.py   # SchemaField, SchemaModel, FieldConstraint
│   ├── source.py   # SourceDefinition, SourceTable, ToolType enum
│   ├── prompt.py   # CTEBlock, ParsedPrompt, PromptConfig
│   ├── node.py     # CompiledNode
│   └── graph.py    # SubgraphDef, RoutingType, GraphStructure
├── compiler/       # Parse → compile pipeline
│   ├── factory.py          # pydantic.create_model() from SchemaModel
│   ├── schema_parser.py    # schema.yml → {name: PydanticClass}
│   ├── source_parser.py    # sources.yml → {name: SourceDefinition}
│   ├── jinja_env.py        # Jinja2 + ref/source/config/var/env_var
│   ├── cte_parser.py       # WITH...AS extraction, SELECT parsing
│   ├── prompt_compiler.py  # .prompt → ParsedPrompt (full pipeline)
│   ├── folder_parser.py    # Dir tree → SubgraphDef with routing
│   └── graph_builder.py    # SubgraphDef+CompiledNode → StateGraph + Python codegen
├── runtime/        # Execution layer
│   ├── db.py              # DatabaseManager: SQLite DDL + trace/cache CRUD
│   ├── tool_table.py      # SourceDefinition → callable Python tools
│   ├── mcp_client.py      # McpConnection + McpManager — persistent MCP stdio
│   ├── node_runner.py     # CTE loop, retries, ref resolution, output mapping
│   └── executor.py        # GraphExecutor: topological sort, execution, traces
├── cli.py          # Click CLI: init, compile, run, test
├── project.py      # ProjectLoader: reads abt_project.yml, discovers files
└── exceptions.py   # Custom exception hierarchy
```

## What's built (v0.3.2 — nested subgraph compilation)

- [x] Full CLI (init, compile, run, test) — all wired to real pipeline
- [x] YAML schema → dynamic Pydantic with enum/constraint validation
- [x] Source/tool registration (REST, MCP stubs, Python function)
- [x] Jinja env with ref(), source(), config(), var(), env_var()
- [x] CTE parser (multi-line, single-line, tool-step detection)
- [x] Folder routing parser (require_all, require_any, metadata, prompt_root-aware node keys)
- [x] Graph builder + dependency resolution + Python codegen (routing-aware: `_flatten_blocks()` + `_wire_blocks()`)
- [x] SQLite persistence (agent_runs, llm_traces, tool_results, node_executions) — thread-safe with `check_same_thread=False` + `threading.Lock()`
- [x] Node runner with CTE execution, retries, SELECT-ref resolution
- [x] **Real LangGraph StateGraph** — `GraphExecutor.execute()` builds `StateGraph(AbtState)`, wires edges via `_flatten_tree()` + `_wire_blocks()`, uses `Annotated` reducers for dict merge / list concat. `execute_sequential()` kept as fallback.
- [x] **Parallel execution for require_all** — fan-out to all children, fan-in via LangGraph's natural AND-gate (multiple incoming edges)
- [x] **OR-gate for require_any** — fan-out to all children, collector node picks first success, all-fail → error
- [x] **Real LLM calls via OpenAI-compatible API (DeepSeek)** — `_execute_llm_cte` calls `openai.OpenAI` client, logs traces via `db.log_llm_call`, parses JSON from response
- [x] **Project-level model defaults** — `models.default` in `abt_project.yml` sets fallback model/temperature/max_tokens; `{{ config(...) }}` in `.prompt` overrides per-file; `llm_factory` in code overrides everything
- [x] Example project (inventory agent: 5 prompts, 4 schemas, 2 sources)
- [x] Generated Python code runs standalone with correct execution order
- [x] **Nested subgraph compilation** — folders with REQUIRE_ALL/REQUIRE_ANY become compiled LangGraph StateGraphs. `_flatten_tree` produces recursive blocks (parallel/any with children). `_build_blocks_in_graph` recursively builds child StateGraphs, compiles them, adds as nodes in parent. SEQUENTIAL folders stay inline. 3-level nesting tested.
- [x] All 5 test suites pass (integration, phase4, phase5, generated_python, nested_subgraphs)
- [x] Mock LLM factory in tests (no real API key needed for CI)

## What's NOT built (next priorities)

- [x] **require_any with collector node** — true OR-gate: fan-out, collector picks first success, all-fail → error
- [x] **Nested subgraph compilation** — folders → compiled LangGraph StateGraphs, added as nodes via `sg.add_node(name, compiled_subgraph)`. SEQUENTIAL stays inline, REQUIRE_ALL/REQUIRE_ANY become nested blocks with children. Deep nesting (3+ levels) works.
- [ ] MCP client in tool_table (currently returns stub)
- [ ] Token streaming (abt run --stream)
- [ ] Manifest file
- [ ] Incremental compilation
- [ ] dbt-style selectors

## LLM Setup

The runtime uses OpenAI-compatible API. Default: DeepSeek.

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."          # Required
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"  # Optional, this is the default
```

Per-project model in `abt_project.yml`:
```yaml
models:
  default:
    provider: deepseek       # or openai, anthropic, etc.
    model: deepseek-chat     # model name passed to the API
    temperature: 0.7
    max_tokens: 4096
```

Per-file override in `.prompt`:
```
{{ config(model="deepseek-reasoner", temperature=0.1) }}
```

Code-level override: pass `llm_factory=my_factory` to `GraphExecutor` or `NodeRunner`.

## The user

A dbt analytics engineer exploring AI agent development. Deep understanding of dbt's declarative patterns. Wants to apply dbt's elegance to the agent space. Values clean architecture and conceptual clarity over feature bloat. Uses DeepSeek API (OpenAI-compatible) as the LLM backend. Speaks Russian; respond in Russian.

## Current state (2026-06-04)

v0.3.2 — Nested subgraph compilation done. REQUIRE_ALL/REQUIRE_ANY folders compile to real LangGraph StateGraphs, added to parent via `sg.add_node(name, compiled)`. `_flatten_tree` produces recursive blocks (parallel/any with children). `_build_blocks_in_graph` recursively builds and compiles. SEQUENTIAL stays inline for backward compatibility. Generated Python code mirrors the same recursive logic. All 5 test suites pass. 3-level deep nesting tested.

### Nested subgraph architecture (v0.3.2)

```
require_all/                    → compiled as LangGraph StateGraph
  sequential_child/             → flattened inline (SEQUENTIAL)
    step_a.prompt → node
    step_b.prompt → node
  check_stock.prompt → node
```

Each non-sequential folder becomes its own StateGraph, handles internal routing (AND/OR gate), and is added as a single node in the parent. `_block_entry_names`/`_block_exit_names` return `[block["name"]]` — the subgraph looks like one node to its parent.

## Key files for the example project

```
example_project/
├── abt_project.yml
├── schemas/inventory_schema.yml    # 4 models: stock_check, demand_forecast, inventory_analysis, fallback_result
├── sources/apis.yml                # warehouse_api (REST) + demand_forecast_mcp (MCP)
├── prompts/
│   ├── require_all/                # AND gate: both must complete
│   │   ├── check_stock.prompt
│   │   └── check_demand.prompt
│   ├── require_any__fast/          # OR gate, metadata: tag=fast
│   │   ├── fallback_a.prompt
│   │   └── fallback_b.prompt
│   └── decide.prompt               # Depends on ref('require_all/check_stock'), ref('require_all/check_demand')
└── target/
    └── compiled_graph.py           # Auto-generated standalone Python
```
