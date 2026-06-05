# ABT — Agent Building Tool

Inspired by dbt. Compiles declarative `.prompt` files + YAML schemas into LangGraph agents with SQLite persistence.

> **v1.0.0** — Manifest.json is the single compilation artifact. Incremental: both compile (`abt compile --full-refresh`) and runtime (`abt run --refresh`) skip unchanged work.

## Key Commands

```bash
pip install -e .                        # Install in editable mode
python -c "from abt.cli import cli; cli(['init', 'name'], standalone_mode=False)"
python -c "from abt.cli import cli; cli(['compile'], standalone_mode=False)"
python -c "from abt.cli import cli; cli(['run', '-v'], standalone_mode=False)"
python -c "from abt.cli import cli; cli(['run', '--refresh'], standalone_mode=False)"
python -c "from abt.cli import cli; cli(['run', '--trigger', 'daily_check'], standalone_mode=False)"
python -c "from abt.cli import cli; cli(['test'], standalone_mode=False)"
python -c "from abt.cli import cli; cli(['serve', '--port', '8000'], standalone_mode=False)"
```

Run tests:
```bash
python tests/test_integration.py
python tests/test_phase4_smoke.py
python tests/test_phase5_smoke.py
python tests/test_nested_subgraphs.py
python tests/test_selectors.py
python tests/test_triggers.py
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
├── models/         # Pydantic data contracts (7 files)
│   ├── config.py   # AbtProjectConfig, ProjectPaths
│   ├── schema.py   # SchemaField, SchemaModel, FieldConstraint
│   ├── source.py   # SourceDefinition, SourceTable, ToolType enum
│   ├── prompt.py   # CTEBlock, ParsedPrompt, PromptConfig
│   ├── node.py     # CompiledNode
│   ├── graph.py    # SubgraphDef, RoutingType, GraphStructure
│   └── trigger.py  # TriggerType, TriggerInput, TriggerDefinition, TriggerFile
├── compiler/       # Parse → compile pipeline
│   ├── factory.py              # pydantic.create_model() from SchemaModel
│   ├── schema_parser.py        # schema.yml → {name: PydanticClass}
│   ├── source_parser.py        # sources.yml → {name: SourceDefinition}
│   ├── trigger_parser.py       # triggers.yml → {name: TriggerDefinition}
│   ├── jinja_env.py            # Jinja2 + ref/source/config/var/env_var
│   ├── cte_parser.py           # WITH...AS extraction, SELECT parsing
│   ├── prompt_compiler.py      # .prompt → ParsedPrompt (full pipeline)
│   ├── folder_parser.py        # Dir tree → SubgraphDef with routing
│   ├── graph_builder.py        # SubgraphDef+CompiledNode → StateGraph
│   ├── manifest_generator.py   # GraphStructure+AbtProjectConfig → manifest.json
│   ├── selector.py             # dbt-style node selection (+name+, tag:, path:, glob)
│   ├── fingerprint.py          # SHA256 file hashing for incremental compilation
│   └── cache_manager.py        # Loads prev manifest, detects staleness, merges cache
├── runtime/        # Execution layer
│   ├── db.py              # DatabaseManager: SQLite DDL + trace/cache CRUD
│   ├── tool_table.py      # SourceDefinition → callable Python tools
│   ├── mcp_client.py      # McpConnection + McpManager — persistent MCP stdio
│   ├── node_runner.py     # CTE loop, retries, ref resolution, output mapping, Pydantic validation
│   ├── test_runner.py     # .test.yml discovery, safe eval assertions, TestResult
│   ├── executor.py        # GraphExecutor: topological sort, execution, traces, dynamic routing
│   ├── trigger_manager.py # TriggerManager: resolve_input(), activate(), JSONPath resolution
│   └── server.py          # Starlette app factory: dynamic webhook routes + utility endpoints
├── cli.py          # Click CLI: init, compile, run, test, serve
├── project.py      # ProjectLoader: reads abt_project.yml, discovers files
└── exceptions.py   # Custom exception hierarchy
```

## What's built (v1.0.0)

- [x] Full CLI (init, compile, run, test, serve)
- [x] YAML schema → dynamic Pydantic with enum/constraint validation
- [x] Source/tool registration (REST, MCP, Python function)
- [x] Jinja env with ref(), source(), config(), var(), env_var()
- [x] CTE parser (multi-line, single-line, AS TOOL/AS LLM)
- [x] Folder routing parser (require_all, require_any, metadata, prompt_root-aware node keys)
- [x] Graph builder + dependency resolution (manifest.json is the single artifact)
- [x] SQLite persistence (agent_runs, llm_traces, tool_results, node_executions, node_cache)
- [x] Node runner with CTE execution, retries, SELECT-ref resolution
- [x] Real LangGraph StateGraph with Annotated reducers
- [x] Parallel execution (require_all) + OR-gate (require_any with collector)
- [x] Real LLM calls via OpenAI-compatible API (DeepSeek)
- [x] Project-level model defaults + per-file config overrides
- [x] Example project (inventory agent: 5 prompts, 4 schemas, 2 sources)
- [x] Nested subgraph compilation (3-level nesting tested)
- [x] MCP client with persistent stdio connections + SQLite caching
- [x] Native token streaming via LangGraph `get_stream_writer()` + `app.stream()`
- [x] Manifest file (manifest.json: 7 sections including file_hashes)
- [x] dbt-style selectors (`+name+`, `tag:`, `path:`, `source:`, glob, `--exclude`)
- [x] Incremental compilation — manifest.json as cache, SHA256 fingerprints, conservative invalidation, `--full-refresh`
- [x] Pydantic output validation — validates LLM output against `output_schema_type`, feeds errors back on retry
- [x] Compile-time ref() contracts — `GraphBuildError` on unresolved `ref('X')`
- [x] Explicit CTE types — `AS TOOL` / `AS LLM` syntax, `CTEBlock.cte_type` field
- [x] `abt test` — `.test.yml` data assertions with safe eval, `TestRunner`, 8 assertions
- [x] Dynamic routing — `route_on`/`route_when`/`route_default`, `add_conditional_edges`, compile-time target resolution
- [x] Human-in-the-loop — `interrupt()` approval gates via `approve_when`/`approve_message` in config
- [x] Triggers — declarative agent activation (schedule, webhook, message), `abt serve` with Starlette
- [x] Incremental execution — `node_cache` table, `_compute_node_inputs_hash()`, `--refresh` flag

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

## Current state (2026-06-05)

v1.0.0 — Manifest.json is the single compilation artifact. Both compile and runtime are incremental. All tests pass.

**Completed (previous sessions):**
- #1 Pydantic output validation
- #2 Compile-time ref() contracts
- #3 Explicit CTE types (AS TOOL / AS LLM)
- #4 `abt test`
- #5 Incremental execution
- #6 Remove codegen — `generate_python_code()` removed, `_subgraph_to_dict()` kept
- #7 Triggers + #7a Human-in-the-loop + #8b Dynamic routing
- #8c Native streaming — LangGraph `get_stream_writer()` + `app.stream()`
- Final cleanup — IMPROVEMENTS.md removed, README.md rewritten for v1.0.0, dead test references fixed
- IMPROVEMENTS.md removed (all done), README.md rewritten for v1.0.0

### New features detail

**Pydantic validation** (`node_runner.py:92-103`):
```
After CTE loop: schema_cls = node.output_schema_type
→ validated = schema_cls(**output) → output = validated.model_dump()
→ on failure: validation_feedback passed to _execute_llm_cte on next attempt
→ LLM sees "Previous output failed validation: ..." and self-corrects
```

**Compile-time ref() contracts** (`graph_builder.py:52-57`):
```
ref('nonexistent') → GraphBuildError at compile time
All refs validated: CTE blocks + system prompt
```

**Explicit CTE types** (`cte_parser.py:16-24`, `prompt_compiler.py:40-48`):
```sql
WITH fetch_inventory AS TOOL (...)   -- API call, forever cacheable
gap_analysis AS LLM (...)            -- LLM call, never cacheable
```
Legacy `AS (...)` without type → fallback to `detect_cte_type()`.

**abt test** (`runtime/test_runner.py`):
```yaml
# check_stock.test.yml
tests:
  - name: stock_not_negative
    assert: quantity_on_hand >= 0
  - name: location_not_empty
    assert: location is not null
```
```bash
abt test                    # All tests
abt test --select check_stock  # Specific node
```
Safe eval: restricted `__builtins__` (no `__import__`, `open`, etc).

**Dynamic routing** (`executor.py:263-290`):
```
{{ config(
    route_on="priority",
    route_when=["high:escalate", "medium:auto_order"],
    route_default="__END__"
) }}
```
At runtime: `state["node_outputs"][node_name]["priority"]` → value looked up in `route_map` → `add_conditional_edges` routes to target node or END. Failed nodes fall through to default/END.

**Human-in-the-loop** (`node_runner.py:105-143`, `executor.py:77-130`, `cli.py:142-168`):
```
{{ config(approve_when="total_order_cost > 5000", approve_message="Large order") }}
```
At runtime, after Pydantic validation, `approve_when` is evaluated as a Python expression against the output dict (safe eval, same builtins as `abt test`). If truthy → `interrupt()` pauses the graph. CLI shows output and prompts: approve (y) / reject (n) / edit (e). Reject returns error; edit opens $EDITOR for JSON modification. `MemorySaver` persists state across the pause. `resume()` continues with `Command(resume=decision)`.

Config fields:
- `approve_when: str = ""` — Python expression (e.g. `"total > 100 and priority == 'high'"`)
- `approve_message: str = ""` — optional custom prompt; defaults to "Approve output for 'node_name'?"

Flow: `execute()` → `GraphInterrupt` caught → return `{"__interrupt__": {...}}` → CLI approval loop → `resume(decision)` → final result. Nodes without `approve_when` run normally (zero overhead — `interrupt()` never called).

### Triggers (dbt exposures pattern) — v0.8.0

**Trigger types:** schedule (cron), webhook (HTTP), message (chat).

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

**Key files:** `models/trigger.py:1-55` (TriggerFile, TriggerDefinition, TriggerInput, TriggerType), `compiler/trigger_parser.py:1-29` (TriggerParser), `runtime/trigger_manager.py:1-70` (TriggerManager, `_resolve_jsonpath`), `runtime/server.py:1-62` (Starlette `create_app`), `cli.py:320-405` (serve command, `_start_scheduler`).

**CLI:**
```bash
abt serve                          # Start HTTP server + optional scheduler
abt serve --port 8080              # Custom port
abt serve --no-scheduler           # Webhooks only
abt run --trigger daily_check      # Simulate a trigger
abt run --trigger daily_check --input event.json  # With event data
```

**Architecture:**
1. `TriggerParser` reads `triggers/` directory (`.triggers.yml` files), returns `dict[str, TriggerDefinition]`
2. `TriggerManager.resolve_input()` merges: static → JSONPath mapping → mode shorthand
3. `TriggerManager.activate()` calls `executor.execute(initial_input)` — full graph run
4. `create_app()` builds Starlette routes per webhook trigger + utility routes (`/triggers`, `/trigger/{name}`, `/health`)
5. `_start_scheduler()` uses APScheduler (optional) for cron triggers
6. Manifest gets a `triggers` section; cache gets `__triggers__` global key

**JSONPath:** Hand-rolled `_resolve_jsonpath()` — `$.body.sku`, `$.query.token`, `$.text`. Returns `None` on missing path (key excluded from input).

**Input merge order:** `static` < `mapping` < `mode`. Mapping overrides static; mode is the final contextual signal.

### Incremental execution architecture (v0.9.0)

```
NodeRunner.make_node_function()
  ├── _compute_node_inputs_hash(node, state) → sha256 hex
  │   Inputs: system_prompt + config(model,temp,max_tokens) + refs + cte_content
  ├── db.get_cached_node_output(node_name, inputs_hash) → cached or None
  │   Cache hit → return immediately, skip all LLM calls
  │   Cache miss → execute CTE loop as normal
  └── db.cache_node_output(node_name, inputs_hash, output)
```

**Cache key:** `(node_name, inputs_hash)` — composite primary key.

**Skipped when:**
- `temperature != 0` — non-deterministic output, can't cache
- `--refresh` flag — user explicitly wants fresh execution

**CLI:**
```bash
abt run                  # Use cache when possible (temp=0 nodes)
abt run --refresh        # Force re-execute all nodes
```

**Key files:** `runtime/db.py:63-71` (node_cache DDL), `runtime/db.py:185-200` (get/cache methods), `runtime/node_runner.py:14` (import hashlib), `runtime/node_runner.py:188-205` (_compute_node_inputs_hash), `runtime/node_runner.py:67-72` (cache check in node_fn), `runtime/node_runner.py:161` (cache write), `runtime/executor.py:68` (_use_cache), `cli.py:81` (--refresh flag).

### Incremental compilation architecture (v0.5.0)

```
CacheManager(manifest.json)
  ├── load_previous_manifest() → prev dict or None
  ├── compute_hashes(loader, prompt_root) → {key: sha256}
  │   Global keys: __project__, __macros__, __schemas__, __sources__
  │   Prompt keys: qualified_name → hash
  └── detect_changes(loader, prompt_root, previous) → {
        full_rebuild: bool,
        reason: str | None,
        changed_qualified: set[str],
        unchanged_qualified: set[str],
        current_hashes: dict,
      }
```

**Invalidation rules:**

| Change | Action |
|--------|--------|
| `.prompt` file content | Recompile only that file |
| `.jinja` macro (any) | Full rebuild (macros shared) |
| `.yml` schema (any) | Full rebuild (schemas shared) |
| `.yml` source (any) | Full rebuild (sources shared) |
| `abt_project.yml` | Full rebuild |
| File added/removed | Full rebuild (structure changed) |

**Flow:**
1. Load previous `manifest.json` (if exists and not `--full-refresh`)
2. Compute SHA256 of all source files
3. Compare `file_hashes`: global keys first, then per-prompt
4. If global invalidation → `compile_all()` as usual
5. Otherwise → `compile_all(changed_files_only)` + `load_cached_prompts(unchanged)` from manifest nodes
6. Merge, rebuild folder tree, regenerate manifest + Python code

**Key files:** `fingerprint.py:1-14` (hash_file, hash_file_list), `cache_manager.py:1-140` (CacheManager), `manifest_generator.py:1-35` (generate_manifest with file_hashes param, load_manifest), `cli.py:195-305` (_compile_project helper).

### Nested subgraph architecture (v0.3.2)

```
require_all/                    → compiled as LangGraph StateGraph
  sequential_child/             → flattened inline (SEQUENTIAL)
    step_a.prompt → node
    step_b.prompt → node
  check_stock.prompt → node
```

Each non-sequential folder becomes its own StateGraph, handles internal routing (AND/OR gate), and is added as a single node in the parent. `_block_entry_names`/`_block_exit_names` return `[block["name"]]` — the subgraph looks like one node to its parent.

### Token streaming architecture (v1.0.0 — native via LangGraph)

Uses LangGraph's built-in `get_stream_writer()` + `app.stream()` instead of a custom 4-layer callback chain.

```
cli.py: stream loop over executor.execute_stream()
  → GraphExecutor.execute_stream(): app.stream(stream_mode=["updates", "custom"])
    → NodeRunner._execute_llm_cte(): get_stream_writer() → writer({...})
```

Event structure: `{"event": "cte_start"|"token"|"cte_end", "node": ..., "cte": ..., ...}`

Stream modes:
- `"custom"` → LLM token events emitted via `get_stream_writer()`
- `"updates"` → state changes (node outputs, errors) for final result accumulation

When `get_stream_writer()` is unavailable (non-stream context, e.g. `app.invoke()`), falls back to non-streaming LLM call automatically.

Key files: `executor.py:139-183` (execute_stream), `node_runner.py:276-308` (streaming path in _execute_llm_cte), `cli.py:139-158` (stream event loop).

### Manifest architecture (v0.5.0 — now includes file_hashes)

Generated by `generate_manifest(GraphStructure, AbtProjectConfig, file_hashes)` → `target/manifest.json`.

**Sections:**
| Section | Source | Key fields |
|---------|--------|------------|
| metadata | config + counts | project_name, version, generated_at, abt_version, node/source/schema counts, total_cte_blocks |
| file_hashes | CacheManager | __project__, __macros__, __schemas__, __sources__ (combined), + per-prompt qualified_name → SHA256 |
| nodes | CompiledNode + ParsedPrompt | qualified_name, file_path, config, system_prompt, cte_blocks[], output_columns, dependencies[], source_refs[], resolved_tools[], output_schema, on_fail_target |
| sources | SourceDefinition | type, description, config, tables[] |
| schemas | Pydantic model_cls | fields[] (name, type, description, required), json_schema (full JSON Schema) |
| graph | SubgraphDef + dep_graph | routing_tree (via `_subgraph_to_dict`), dependency_graph (sets→sorted lists), topological_order (Kahn's algorithm) |
| project | AbtProjectConfig | name, version, paths, models, vars |

**Key files:** `manifest_generator.py:1-160` (generate_manifest with file_hashes param, load_manifest, _serialize_node, _serialize_schema, _topological_sort), `cache_manager.py:1-140` (CacheManager.compute_hashes, _prompt_from_manifest_node).

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
    └── manifest.json               # Single compilation artifact (metadata, nodes, sources, schemas, graph, project)
```
