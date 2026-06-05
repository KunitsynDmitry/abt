# ABT — Agent Building Tool

Inspired by dbt. Compiles declarative `.prompt` files + YAML schemas into LangGraph agents with SQLite persistence.

> **CONTINUE:** v0.7.0 — #1-#5 + #7a done. Next is **#5 Incremental execution** (runtime cache).
> Incremental: `abt compile` skips unchanged files. `--full-refresh` forces rebuild. See `abt/compiler/cache_manager.py`.
> **IMPROVEMENTS:** See [IMPROVEMENTS.md](IMPROVEMENTS.md) — architectural review & prioritized roadmap (7 items, 2026-06-05).

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
│   ├── factory.py              # pydantic.create_model() from SchemaModel
│   ├── schema_parser.py        # schema.yml → {name: PydanticClass}
│   ├── source_parser.py        # sources.yml → {name: SourceDefinition}
│   ├── jinja_env.py            # Jinja2 + ref/source/config/var/env_var
│   ├── cte_parser.py           # WITH...AS extraction, SELECT parsing
│   ├── prompt_compiler.py      # .prompt → ParsedPrompt (full pipeline)
│   ├── folder_parser.py        # Dir tree → SubgraphDef with routing
│   ├── graph_builder.py        # SubgraphDef+CompiledNode → StateGraph + Python codegen
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
│   └── executor.py        # GraphExecutor: topological sort, execution, traces, dynamic routing
├── cli.py          # Click CLI: init, compile, run, test
├── project.py      # ProjectLoader: reads abt_project.yml, discovers files
└── exceptions.py   # Custom exception hierarchy
```

## What's built (v0.5.0 — Incremental compilation)

- [x] Full CLI (init, compile, run, test) — all wired to real pipeline
- [x] YAML schema → dynamic Pydantic with enum/constraint validation
- [x] Source/tool registration (REST, MCP, Python function)
- [x] Jinja env with ref(), source(), config(), var(), env_var()
- [x] CTE parser (multi-line, single-line, tool-step detection)
- [x] Folder routing parser (require_all, require_any, metadata, prompt_root-aware node keys)
- [x] Graph builder + dependency resolution + Python codegen (routing-aware: `_flatten_blocks()` + `_wire_blocks()`)
- [x] SQLite persistence (agent_runs, llm_traces, tool_results, node_executions)
- [x] Node runner with CTE execution, retries, SELECT-ref resolution
- [x] Real LangGraph StateGraph with Annotated reducers
- [x] Parallel execution (require_all) + OR-gate (require_any with collector)
- [x] Real LLM calls via OpenAI-compatible API (DeepSeek)
- [x] Project-level model defaults + per-file config overrides
- [x] Example project (inventory agent: 5 prompts, 4 schemas, 2 sources)
- [x] Nested subgraph compilation (3-level nesting tested)
- [x] MCP client with persistent stdio connections + SQLite caching
- [x] Token streaming (callback pattern: CLI → Executor → NodeRunner → LLM)
- [x] Manifest file (manifest.json: 7 sections including file_hashes)
- [x] dbt-style selectors (`+name+`, `tag:`, `path:`, `source:`, glob, `--exclude`)
- [x] **Incremental compilation** — manifest.json as cache, SHA256 fingerprints, conservative invalidation, `--full-refresh`
- [x] **Pydantic output validation** — `node_runner.py` validates LLM output against `output_schema_type`, feeds errors back on retry
- [x] **Compile-time ref() contracts** — `graph_builder.py` raises `GraphBuildError` on unresolved `ref('X')`
- [x] **Explicit CTE types** — `AS TOOL` / `AS LLM` syntax, `CTEBlock.cte_type` field, backward-compatible legacy detection
- [x] **`abt test`** — `.test.yml` data assertions with safe eval, `TestRunner`, 3 example test files (8 assertions)
- [x] **Dynamic routing** — `route_on`/`route_when`/`route_default` in config, `add_conditional_edges` in executor, compile-time target resolution

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

v0.7.0 — 6 improvements from IMPROVEMENTS.md done. `manifest.json` doubles as compilation cache. All 6 test suites pass (24+ tests).

**Completed (this session):**
- #7a Human-in-the-loop — `interrupt()` approval gates via `approve_when`/`approve_message` in config

**Completed (previous):**
- #1 Pydantic output validation — `node_runner.py:92-103`, validates against `output_schema_type`, feeds errors back on retry
- #2 Compile-time ref() contracts — `graph_builder.py:52-57`, `GraphBuildError` on unresolved `ref('X')`
- #3 Explicit CTE types — `AS TOOL` / `AS LLM` syntax, `CTEBlock.cte_type`, backward-compatible
- #4 `abt test` — `.test.yml` data assertions, `TestRunner` with safe eval, 8 assertions in example project
- #5 Dynamic routing — `route_on`/`route_when`/`route_default`, `add_conditional_edges`, compile-time target resolution

**Next: #5 Incremental execution** (runtime cache, 3-4h).
**Remaining:** #7c Native streaming, #6 Remove codegen.

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

### Token streaming architecture (v0.3.4)

Callback pattern: `CLI → GraphExecutor → NodeRunner → _execute_llm_cte`.

```
cli.py: _stream_printer(node, cte, delta, event)
  → GraphExecutor.__init__(stream_callback=...)
    → NodeRunner.__init__(stream_callback=...)
      → _execute_llm_cte(stream_callback=...)
```

Callback signature: `(node_name, cte_name, delta, event) -> None`
Events: `"cte_start"` (delta=""), `"token"` (delta=chunk), `"cte_end"` (delta=full content)

When `stream_callback is None` → original non-streaming `create()` path, zero overhead.
When set → `create(stream=True)`, iterate chunks, accumulate for SQLite, callback per event.

Key files: `node_runner.py:146,195-228`, `executor.py:53,59,111,210`, `cli.py:168-177`.

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
    ├── compiled_graph.py           # Auto-generated standalone Python
    └── manifest.json               # Compiled artifact (metadata, nodes, sources, schemas, graph, project)
```
