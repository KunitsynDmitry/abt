# IMPROVEMENTS — Architectural critique & roadmap

Based on the 2026-06-05 review. Each section: problem → solution → effort. **8 of 10 items done** (v0.9.0).

---

## 1. Pydantic output validation (CRITICAL) — DONE 2026-06-05

**Problem:** `node_runner.py:245` does `json.loads(content)` and returns the dict as-is. `CompiledNode.output_schema_type` is loaded but never applied. LLM hallucinations (wrong keys, wrong types, missing fields) pass through silently.

**Solution:** After building the output dict in `node_runner.py:78-88`, validate against the schema:

```python
schema_cls = node.output_schema_type
if schema_cls and isinstance(output, dict):
    try:
        validated = schema_cls(**output)
        output = validated.model_dump()
    except Exception as e:
        # Retry with validation error in context, or fail to on_fail_target
```

If validation fails during a CTE step → retry with the pydantic error message in the next LLM prompt so the model can self-correct. If retries exhausted → `on_fail_target`.

**Files to change:** `node_runner.py` (~20 lines)

**Effort:** 30 minutes

---

## 2. Compile-time ref() contract checking — DONE 2026-06-05

**Problem:** `FROM {{ ref('require_all/check_stock') }}` in `decide.prompt` is never verified. If `check_stock.prompt` changes its SELECT columns (e.g. `quantity_on_hand` → `qty`), `decide.prompt` silently breaks at runtime because the LLM receives unexpected context keys.

**Solution:** In `graph_builder._compile_node()`, after `raw_dependencies` are collected, for each `ref_name`:
1. Resolve the target node (already implemented via `_resolve_prompt_ref`)
2. Check that the target exists — if not, **compile error**
3. Read `target.prompt.output_columns` — store as a contract on the edge
4. If the caller's CTE references specific columns from the ref (via WHERE-like syntax or column hints), verify those columns exist in the target's output_columns — mismatch → **compile error**

```python
# graph_builder.py, inside _compile_node or a new _check_ref_contracts method
for cte in prompt.cte_blocks:
    for ref_name in cte.model_refs:
        target = self._resolve_prompt_ref(ref_name, all_nodes)
        if target is None:
            raise CompileError(
                f"Node '{qualified_name}' references '{ref_name}' which does not exist"
            )
```

**Files to change:** `graph_builder.py` (~40 lines), new `exceptions.py` entry

**Effort:** 2-3 hours

---

## 3. Explicit CTE types: `AS LLM` vs `AS TOOL` — DONE 2026-06-05

**Problem:** CTE syntax looks uniform but semantics diverge completely. `detect_cte_type()` distinguishes tool-CTE from LLM-CTE by scanning for `__SOURCE__` in rendered text. The user writes the same `WITH name AS (...)` syntax and gets fundamentally different behavior (API call vs prompt text). This is a cognitive trap.

**Solution:** Make the distinction explicit in syntax:

```sql
WITH fetch_inventory AS TOOL (
    SELECT * FROM {{ source('warehouse_api', 'current_stock') }}
    WHERE product_id = '{{ product_id }}'
),

gap_analysis AS LLM (
    Compare stock_analysis against demand_analysis.
    If quantity_on_hand < predicted_demand, calculate the shortfall.
)
```

Changes:
- `CTE_START` regex in `cte_parser.py:17-19` adds `TOOL|LLM` alternative
- `CTEBlock` gets `cte_type: Literal["tool", "llm"]` field
- `detect_cte_type()` is replaced by direct parse of the AS keyword
- TOOL steps can be: executed in parallel, cached forever, run without LLM API key
- LLM steps: always need API, never cacheable, cost is predictable

**Files to change:** `cte_parser.py` (regex + parse logic, ~30 lines), `models/prompt.py` (CTEBlock field), `node_runner.py` (branch on cte_type instead of is_tool_step)

**Effort:** 1 hour

---

## 4. `abt test` — data assertions on node outputs — DONE 2026-06-05

**Problem:** No equivalent of `dbt test`. LLM outputs validated as JSON, but semantic correctness is unchecked. `quantity_on_hand` could be -5, `priority` could be "banana".

**Solution:** Test definitions in `.prompt` files or adjacent `.test.yml`:

```yaml
# check_stock.test.yml
tests:
  - name: stock_not_negative
    assert: quantity_on_hand >= 0
  - name: location_not_empty
    assert: location is not null
  - name: in_stock_consistent
    assert: (in_stock == True) == (quantity_on_hand > 0)
```

CLI:
```bash
abt test                    # Run all tests
abt test --select check_stock  # Tests for one node
```

Implementation: `NodeRunner` after successful validation, evaluates each assert in the output dict context. Failed assert → test failure with clear message. This is `eval(assertion, {}, output_dict)` with safe builtins only.

Optionally: re-run the node with a fixed input to get deterministic test output (use cached ref values).

**Files to change:** `cli.py` (test command enhancement), new `runtime/test_runner.py`, `models/prompt.py` (TestDefinition model)

**Effort:** 2-3 hours

---

## 5. Incremental execution (runtime cache) — DONE 2026-06-05

**Problem:** `abt run` always executes ALL nodes. Changing one `.prompt` file re-runs everything, including expensive LLM calls for unchanged nodes. This is the runtime analogue of incremental compilation.

Note: incremental **compilation** is already done (v0.5.0, `cache_manager.py`). This is about incremental **execution** — skipping LLM calls for nodes whose inputs haven't changed.

**Solution:** SQLite table for runtime cache:

```sql
CREATE TABLE IF NOT EXISTS node_cache (
    node_name TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    outputs_json TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (node_name, inputs_hash)
);
```

During execution, before calling LLM:
1. Compute `hash(node.system_prompt + serialized_ref_values + config)`
2. If hash matches `node_cache` → skip LLM call, return cached output
3. If not → call LLM, store result in cache

Cache invalidation:
- TOOL-CTE: cache forever (API results are idempotent for same params)
- LLM-CTE: invalidate when `--refresh` flag is passed, or when temperature != 0

CLI:
```bash
abt run                          # Use cache when possible
abt run --refresh                # Force re-execute all LLM nodes
abt run --refresh --select decide  # Force re-execute specific node
```

**Files changed:** `runtime/db.py` (DDL + 2 methods), `runtime/node_runner.py` (hash function + cache check + cache write), `runtime/executor.py` (use_cache thread), `cli.py` (--refresh flag)

**Effort:** 3-4 hours

---

## 6. Single artifact: manifest.json only (remove codegen)

**Problem:** Two compilation paths exist:
- `manifest.json` — the real artifact, used by `abt run`
- `compiled_graph.py` via `graph_builder.generate_python_code()` — placeholder skeleton with print() stubs, never used at runtime

The generated Python file has already diverged from real execution (no streaming, no MCP, placeholder nodes). This dual path will inevitably cause confusion.

**Solution:**
1. Remove `graph_builder.generate_python_code()` (lines 99-344)
2. Remove `_subgraph_to_dict()` from `graph_builder.py` (already duplicated in `manifest_generator.py`)
3. Remove `compiled_graph.py` generation from `cli.py:compile`
4. If standalone execution is needed later → generate from `manifest.json`, not from in-memory GraphStructure
5. Remove `test_generated_python.py` or adapt it to test manifest.json instead

**Files to change:** `graph_builder.py` (delete ~250 lines), `cli.py` (delete ~5 lines), `test_generated_python.py` (adapt or remove)

**Effort:** 1-2 hours

---

## 7. Triggers (dbt exposures pattern) — DONE 2026-06-05

**Problem:** `abt run` is a one-shot command. But agents are event-driven — they react to schedules, webhooks, chat messages. There's no declarative way to describe *what activates* the agent. Without this, the user must wire up cron, webhook servers, and chat adapters manually outside of ABT.

**Solution:** Trigger definitions as YAML files, inspired by dbt exposures. Exposures say "who consumes my models." Triggers say "who activates my agent." Same pattern, symmetric direction.

New concept: `triggers/` directory + `.triggers.yml` files.

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
    input:
      mapping:
        product_id: "$.body.sku"
        current_qty: "$.body.quantity"

  - name: chat_request
    type: message
    input:
      mapping:
        user_query: "$.text"
```

New CLI command:

```bash
abt serve                          # Start scheduler + webhook server
abt serve --port 8080              # Custom port
abt run --trigger low_stock_alert --input data.json  # Simulate a trigger
```

Architecture:
1. `TriggerDefinition` model: name, type (schedule|webhook|message), config, input mapping
2. `TriggerParser` reads `triggers/` directory, resolves to `{trigger_name: TriggerDefinition}`
3. `TriggerManager` in runtime: for webhooks → lightweight HTTP server (FastAPI/starlette), for schedules → simple in-process scheduler (or APScheduler)
4. Each trigger activation: resolve `input.mapping` against webhook body / schedule context → produce `initial_input` dict → call `executor.execute(initial_input)`
5. Manifest gets a `triggers` section (like nodes, sources, schemas)

Key design decisions:
- Triggers are **not sources**. Sources provide data *within* a step. Triggers activate the *whole agent* and provide initial input.
- Triggers are **not prompts**. They don't do work. They just say "when to start."
- `input.mapping` uses JSONPath to map external event data into agent state — webhook sends `{"body": {"sku": "X"}}`, mapping produces `{"product_id": "X"}`.
- Multiple triggers can activate the same agent with different input shapes.
- `abt serve` is the unified runtime — replaces `abt run` for production. During development, `abt run --trigger X --input file.json` simulates triggers.

Files to create: `models/trigger.py` (TriggerDefinition, TriggerType), `compiler/trigger_parser.py`, `runtime/trigger_manager.py`, `runtime/server.py` (FastAPI)

Files to change: `cli.py` (serve command, --trigger flag on run), `project.py` (discover triggers/), `manifest_generator.py` (triggers section)

Effort: 4-5 hours

---

## 8. LangGraph: use more, not less

**Problem:** ABT currently uses ~20% of LangGraph's capabilities (StateGraph wiring, subgraph compilation). Three features that ABT *should* use:

### 8a. Human-in-the-loop via `interrupt()` — DONE 2026-06-05

Declarative approval gates: `{{ config(approve_when="total_order_cost > 5000") }}`.

At runtime, if condition matches → `interrupt()` pauses execution, user approves/rejects/edits in CLI. `MemorySaver` persists state across the pause.

Implementation:
- `PromptConfig`: `approve_when: str`, `approve_message: str`
- `CompiledNode`: `approve_when`, `approve_message` — passed through `GraphBuilder._compile_node()`
- `node_runner.py:105-143` — safe eval of condition against validated output, `interrupt()` if truthy
- `executor.py:77-130` — `MemorySaver` checkpointer, `execute()` catches `GraphInterrupt` and returns `{"__interrupt__": {...}}`, `resume()` continues with `Command(resume=decision)`
- `cli.py:142-168` — approval loop: show output, prompt y/n/e, call `resume()`
- Edit opens `$EDITOR` for JSON modification; reject returns error to node
- Safe eval reuses `SAFE_BUILTINS` from `test_runner.py`
- `manifest_generator.py` serializes `approve_when`/`approve_message` at both config and node level

**Effort:** 1-2 hours

### 7b. Dynamic routing via `add_conditional_edges` — DONE 2026-06-05

Let LLM output determine the next node at runtime:

```sql
ON priority = 'high'   → escalate.prompt
ON priority = 'medium' → auto_order.prompt
ON priority = 'low'    → END
```

This is agent-specific (dbt can't do it) and is the key differentiator. Folders define the topology; `.prompt` files define dynamic edges.

**Effort:** 2-3 hours

### 7c. Native streaming via `astream_events`

Replace the 4-layer callback chain (`CLI → GraphExecutor → NodeRunner → _execute_llm_cte`) with LangGraph's built-in streaming. Gets node boundaries, tool events, and LLM tokens in a unified event stream.

**Effort:** 1 hour

---

## Priority order (impact / effort) — UPDATED 2026-06-05

| # | What | Hours | Impact | Status |
|---|------|-------|--------|--------|
| 1 | Pydantic output validation | 0.5 | Critical | DONE |
| 2 | Compile-time ref() contracts | 2-3 | High | DONE |
| 3 | Explicit CTE types (AS LLM / AS TOOL) | 1 | Medium | DONE |
| 4 | `abt test` | 2-3 | High | DONE |
| 8b | Dynamic routing (conditional edges) | 2-3 | High | DONE |
| 7 | Triggers (dbt exposures pattern) | 4-5 | High | DONE |
| 8a | Human-in-the-loop | 1-2 | Medium | DONE |
| 5 | Incremental execution | 3-4 | Medium | DONE |
| 8c | Native streaming | 1 | Low | |
| 6 | Remove codegen | 1-2 | Low | |

## Guiding principle

Every time the user writes `FROM {{ ref('X') }}`, the compiler must guarantee that X exists and will produce the expected structure. Every time an LLM returns a response, the runtime must guarantee it matches the declared schema. Without these two guarantees, the declarative syntax is decoration, not a contract.

---

## Implementation notes (2026-06-05 session)

### #1 Pydantic validation — `node_runner.py:92-103`
- After building output dict, validates: `schema_cls(**output)` → `validated.model_dump()`
- On failure: sets `validation_feedback` with pydantic error message → next retry passes it to LLM via `_execute_llm_cte`
- Retries exhausted → existing `on_fail_target` path
- Only applied when `node.output_schema_type is not None`

### #2 ref() contracts — `graph_builder.py:47-57,88-105`
- In `build_structure()`, after building dep_graph, `raw_dependencies` are checked:
  `_resolve_prompt_ref(dep_name) is None` → `GraphBuildError`
- Route targets in `route_when` also validated: `_resolve_prompt_ref(target)` → error if None
- Added `from ..exceptions import GraphBuildError` import

### #3 Explicit CTE types — `cte_parser.py`, `prompt_compiler.py`, `node_runner.py`
- Two regexes: `CTE_START` (with TOOL|LLM) and `CTE_START_LEGACY` (without)
- `parse_file()` tries explicit first, falls back to legacy
- `prompt_compiler.py`: if `cte.cte_type is None` → `detect_cte_type()` fallback
- `node_runner.py`: branches on `cte.cte_type == "tool"`
- `CTEBlock` field: `cte_type: Literal["tool", "llm"] | None`
- Example prompts updated: `check_stock.prompt`, `check_demand.prompt` (AS TOOL), `decide.prompt` (AS LLM)
- `manifest_generator.py:103`: serializes `cte_type`
- `cache_manager.py:176`: deserializes `cte_type`

### #4 abt test — `runtime/test_runner.py` (NEW FILE), `cli.py:158-245`
- `TestDefinition` model: `name: str`, `assert_: str`, `description: str`
- `TestResult` dataclass: `node_name`, `test_name`, `passed`, `message`, `assert_expr`
- `TestRunner.discover()`: finds `*.test.yml` in prompt_root, maps to qualified node names
- `TestRunner.evaluate()`: `eval(expr, {"__builtins__": SAFE_BUILTINS}, context)`
- Safe builtins: `True/False/None`, basic types, `len/abs/min/max/sum/round/isinstance/any/all`
- CLI: compile → discover → execute → evaluate → report (pass/fail counts, exit 1 on failure)
- Example: 3 `.test.yml` files, 8 assertions
- `--select`, `--exclude`, `--verbose` flags

### #5 Dynamic routing — `executor.py:263-290`, `graph_builder.py:70-84,88-105`
- `PromptConfig`: `route_on: str`, `route_when: list[str]` (["value:target", ...]), `route_default: str`
- `CompiledNode`: `route_on`, `route_map: dict[str, str]`, `route_default`
- `graph_builder._compile_node()`: parses `route_when` into `route_map` dict
- `graph_builder.build_structure()`: resolves route targets via `_resolve_prompt_ref()`, validates existence
- `executor._try_add_dynamic_edges()`: checks `node.route_on and node.route_map` → calls `add_conditional_edges`
- `executor._make_route_fn()`: returns function `(state) -> str` that reads output field, looks up in route_map
- `executor._wire_sequential_blocks()`: calls `_try_add_dynamic_edges` for each block exit; on success skips static edge
- Failed nodes (error in output) → returns `route_default or "__END__"`
- `manifest_generator.py`: serializes `route_on`, `route_when`, `route_default` in config + `route_on`, `route_map`, `route_default` in node

### #7 Triggers — `models/trigger.py`, `compiler/trigger_parser.py`, `runtime/trigger_manager.py`, `runtime/server.py`, `cli.py`

**Models** (`models/trigger.py:1-55`):
- `TriggerType(str, Enum)`: SCHEDULE, WEBHOOK, MESSAGE
- `TriggerInput`: mode (str|None), mapping (dict[str,str]), static (dict[str,Any])
- `TriggerDefinition`: name, type, description, schedule, path, method, input
- `TriggerFile`: version, agent, triggers[], from_yaml(path)
- Follows `SourceFile.from_yaml()` pattern exactly

**Parser** (`compiler/trigger_parser.py:1-29`):
- `TriggerParser(project_loader).parse_all() -> dict[str, TriggerDefinition]`
- Follows `SourceParser` pattern: discover files → from_yaml → flatten → detect duplicates
- `resolve_trigger(name, registry)` with `AbtError` on miss

**Project discovery** (`project.py:60-66`):
- `list_trigger_files()` — globs `*.triggers.yml` in `triggers_paths`
- `ProjectPaths.triggers_paths: list[str] = ["triggers"]`
- `validate_project_structure()` validates triggers_paths exist

**Cache** (`cache_manager.py:16,148-152`):
- `__triggers__` added to `GLOBAL_KEYS` — any trigger change → full rebuild
- `_compute_current_hashes()` hashes all trigger files via `hash_file_list()`

**Manifest** (`manifest_generator.py:27-31,55-57,82,92`):
- `generate_manifest(..., triggers=None)` — new optional param
- New `"triggers"` section: `{trigger_name: trigger_def.model_dump()}`
- `trigger_count` in metadata

**Runtime** (`runtime/trigger_manager.py:1-70`):
- `_resolve_jsonpath(data, expr)` — hand-rolled JSONPath: `$.body.sku` → `data["body"]["sku"]`. Returns None on missing path.
- `TriggerManager(triggers, executor)`:
  - `resolve_input(trigger, event_data)` — merge: static → mapping → mode
  - `activate(name, event_data, thread_id)` — resolve + executor.execute()
  - `list_triggers()` — serialized summaries for server routes

**Server** (`runtime/server.py:1-62`):
- `create_app(trigger_manager) -> Starlette`
- Dynamic routes per webhook trigger (path + method from definition)
- Utility routes: GET /triggers, POST /trigger/{name}, GET /health
- `make_webhook_handler()` uses closure with default arg to avoid late-binding trap
- Event data includes `body` (parsed JSON) + `query` (URL params)

**CLI** (`cli.py:77-81,116-128,320-405`):
- `abt run --trigger NAME --input event.json`: resolves trigger input, passes to executor
- `abt serve --port 8000 --host 127.0.0.1`: compiles project, starts Starlette via uvicorn
- `abt serve --no-scheduler`: webhooks only
- `_start_scheduler()`: APScheduler BackgroundScheduler with CronTrigger, warns if not installed
- `_create_project_skeleton()`: creates `triggers/` directory

**Example:** `example_project/triggers/inventory.triggers.yml` — 3 triggers (daily_check schedule, low_stock_alert webhook, chat_request message)

**Tests:** `tests/test_triggers.py` — 12 tests: 3 model parsing, 5 JSONPath, 4 input resolution

### #5 Incremental execution — `runtime/db.py`, `node_runner.py`, `executor.py`, `cli.py`

**Database** (`runtime/db.py:63-71,185-200`):
- `node_cache` table: `(node_name, inputs_hash)` composite PK + `outputs_json` + `created_at`
- `get_cached_node_output(node_name, inputs_hash) -> dict | None` — check cache
- `cache_node_output(node_name, inputs_hash, output)` — store result

**Node runner** (`runtime/node_runner.py:14,67-72,161,188-205`):
- `import hashlib` — SHA256 for inputs hash
- `NodeRunner.__init__(use_cache=True)` — controls caching behavior
- `_compute_node_inputs_hash(node, state)` — hashes system_prompt + config(model,temp,max_tokens) + refs + CTE content
- Cache check before CTE loop: if `use_cache and temp == 0` → `get_cached_node_output()` → hit: return immediately
- Cache write after `db.log_node_complete()`: if `inputs_hash is not None` → `cache_node_output()`
- `inputs_hash = None` when caching disabled → never stored

**Executor** (`runtime/executor.py:68,263,166`):
- `GraphExecutor.__init__(use_cache=True)` — stored as `self._use_cache`
- Passed to `NodeRunner` in `_add_leaf_node_to()` and `execute_sequential()`

**CLI** (`cli.py:81,155`):
- `--refresh/--no-refresh` flag (default: `--no-refresh`)
- `GraphExecutor(..., use_cache=not refresh)`

**Cache skipped when:**
- `temperature != 0` — non-deterministic output
- `--refresh` flag — explicit re-execution
- `use_cache=False` — passed through from executor
