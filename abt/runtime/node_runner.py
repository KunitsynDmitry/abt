"""NodeRunner — executes a single node's prompt with CTE loop, tools, and retries."""

import hashlib
import json
import os
import re
import time
from typing import Any, Callable

from openai import OpenAI

from ..models.node import CompiledNode
from ..models.prompt import ContextProjection, WhereCondition
from .db import DatabaseManager


# Provider → (env var for API key, default base URL)
_PROVIDER_MAP: dict[str, tuple[str, str]] = {
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com"),
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1"),
    "anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com"),
}


def _resolve_provider(provider: str) -> tuple[str, str]:
    """Return (api_key, base_url) for the given provider name.

    Falls back to DEEPSEEK_* env vars when provider is empty or unknown,
    then tries OPENAI_*, then raises if nothing is configured.
    """
    if provider and provider in _PROVIDER_MAP:
        env_key, default_url = _PROVIDER_MAP[provider]
        api_key = os.environ.get(env_key, "")
        base_url = os.environ.get(f"{provider.upper()}_BASE_URL", default_url)
        if api_key:
            return api_key, base_url
        raise RuntimeError(
            f"Provider '{provider}' requires {env_key} environment variable.\n"
            f"  Set {env_key}=sk-... or configure a different provider in abt_project.yml"
        )

    # Auto-detect: try common providers in order
    for name, (env_key, default_url) in _PROVIDER_MAP.items():
        api_key = os.environ.get(env_key, "")
        if api_key:
            base_url = os.environ.get(f"{name.upper()}_BASE_URL", default_url)
            return api_key, base_url

    raise RuntimeError(
        "No LLM API key configured. Set one of:\n"
        "  DEEPSEEK_API_KEY=sk-...\n"
        "  OPENAI_API_KEY=sk-...\n"
        "  ANTHROPIC_API_KEY=sk-...\n"
        "Or pass llm_factory to NodeRunner."
    )


class NodeRunner:
    def __init__(
        self,
        compiled_node: CompiledNode,
        tools: list[Callable],
        db: DatabaseManager,
        llm_factory: Callable[[], OpenAI] | None = None,
        use_cache: bool = True,
        source_definitions: dict[str, Any] | None = None,
    ):
        self.node = compiled_node
        self.tools = {t.__name__: t for t in tools}
        self.db = db
        self._llm_factory = llm_factory
        self._llm_client: OpenAI | None = None
        self._use_cache = use_cache
        self._source_defs = source_definitions or {}
        self._tool_call_pattern = re.compile(r"__SOURCE__(\w+)\.(\w+)__")

    def _get_llm(self) -> OpenAI:
        if self._llm_client is not None:
            return self._llm_client
        if self._llm_factory:
            self._llm_client = self._llm_factory()
            return self._llm_client

        provider = self.node.llm_config.get("provider", "")
        api_key, base_url = _resolve_provider(provider)
        self._llm_client = OpenAI(api_key=api_key, base_url=base_url)
        return self._llm_client

    def make_node_function(self) -> Callable:
        node = self.node
        tools = self.tools
        db = self.db
        get_llm = self._get_llm
        use_cache = self._use_cache
        source_defs = self._source_defs

        def node_fn(state: dict) -> dict:
            run_id = state.get("_run_id", "unknown")
            node_name = node.qualified_name
            exec_id = db.log_node_start(run_id, node_name, state)
            validation_feedback = None

            # Incremental execution: skip if inputs unchanged
            config = node.prompt.config
            inputs_hash = None
            if use_cache and config.temperature == 0:
                inputs_hash = _compute_node_inputs_hash(node, state)
                cached = db.get_cached_node_output(node_name, inputs_hash)
                if cached is not None:
                    db.log_node_complete(exec_id, cached)
                    return {"node_outputs": {node_name: cached}}

            for attempt in range(node.max_retries):
                try:
                    cte_results: dict[str, Any] = {}

                    for cte in node.prompt.cte_blocks:
                        if cte.cte_type == "tool":
                            cte_results[cte.name] = _execute_tool_cte(
                                cte, cte_results, tools, db, run_id, source_defs
                            )
                        else:
                            cte_results[cte.name] = _execute_llm_cte(
                                cte, cte_results, state, node,
                                get_llm(), db, run_id,
                                validation_feedback=validation_feedback,
                            )

                    # Build output from SELECT columns
                    last_cte_name = node.prompt.cte_blocks[-1].name if node.prompt.cte_blocks else ""
                    final = cte_results.get(last_cte_name, cte_results) if last_cte_name else cte_results

                    if isinstance(final, dict):
                        if node.prompt.output_columns:
                            output = {}
                            for col in node.prompt.output_columns:
                                val = final.get(col)
                                if val is None:
                                    val = final.get(col.lower())
                                output[col] = val if val is not None else f"[{col}]"
                        else:
                            output = final
                    else:
                        output = {"result": str(final)}

                    # Validate against output schema
                    schema_cls = node.output_schema_type
                    if schema_cls is not None and isinstance(output, dict):
                        try:
                            validated = schema_cls(**output)
                            output = validated.model_dump()
                        except Exception as e:
                            validation_feedback = (
                                f"Previous output failed validation: {e}. "
                                "Please fix the errors and return valid JSON matching the expected schema."
                            )
                            raise

                    # HITL: approval gate via interrupt()
                    if node.approve_when and isinstance(output, dict):
                        from langgraph.types import interrupt

                        safe_builtins = {
                            "True": True, "False": False, "None": None,
                            "int": int, "float": float, "str": str, "bool": bool,
                            "list": list, "dict": dict, "len": len, "abs": abs,
                            "min": min, "max": max, "sum": sum, "round": round,
                            "isinstance": isinstance, "any": any, "all": all,
                        }
                        try:
                            condition_met = eval(
                                node.approve_when,
                                {"__builtins__": safe_builtins},
                                output,
                            )
                        except Exception:
                            condition_met = False

                        if condition_met:
                            decision = interrupt({
                                "node": node_name,
                                "output": output,
                                "message": (
                                    node.approve_message
                                    or f"Approve output for '{node_name}'?"
                                ),
                            })
                            if isinstance(decision, dict):
                                if decision.get("action") == "reject":
                                    return {
                                        "node_outputs": {
                                            node_name: {"error": "Rejected by user"}
                                        },
                                        "errors": [f"{node_name}: rejected by user"],
                                    }
                                elif decision.get("action") == "edit":
                                    output = decision.get("edited_output", output)

                    db.log_node_complete(exec_id, output)
                    if inputs_hash is not None:
                        db.cache_node_output(node_name, inputs_hash, output)
                    return {
                        "node_outputs": {node_name: output},
                    }

                except Exception as e:
                    if attempt < node.max_retries - 1:
                        db.log_node_retry(exec_id, attempt + 1, str(e))
                        continue
                    else:
                        db.log_node_failed(exec_id, str(e))
                        if node.on_fail_target:
                            return {
                                "errors": [f"{node_name}: {e}"],
                                "_route_override": node.on_fail_target,
                            }
                        return {
                            "node_outputs": {node_name: {"error": str(e)}},
                            "errors": [f"{node_name}: {e}"],
                        }

            return {"node_outputs": {node_name: {"error": "max retries exceeded"}}}

        node_fn.__name__ = f"run_{node.name}"
        return node_fn


def _compute_node_inputs_hash(node: CompiledNode, state: dict) -> str:
    """Hash inputs that determine node output: system prompt, config, refs, CTEs."""
    config = node.prompt.config
    refs = state.get("node_outputs", {})
    parts = [
        node.prompt.system_prompt or "",
        json.dumps({"model": config.model, "temperature": config.temperature,
                     "max_tokens": config.max_tokens}, sort_keys=True),
        json.dumps(refs, sort_keys=True, default=str),
    ]
    for cte in node.prompt.cte_blocks:
        parts.append(cte.rendered_content or cte.raw_content)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _execute_tool_cte(cte, cte_results, tools, db, run_id, source_defs=None):
    rendered = cte.rendered_content
    tool_pattern = re.compile(r"__SOURCE__(\w+)\.(\w+)__")
    matches = tool_pattern.findall(rendered)

    if not matches:
        return {"error": "No tool reference found in CTE"}

    source_name, table_name = matches[0]
    tool_key = f"{source_name}_{table_name}"

    if tool_key not in tools:
        return {"error": f"Tool '{source_name}.{table_name}' not bound to this node"}

    # Extract ALL parameters from WHERE clause
    params = _parse_tool_params(rendered)

    # Validate against source table definition
    if source_defs and source_name in source_defs:
        src = source_defs[source_name]
        for tbl in src.tables:
            if tbl.name == table_name:
                _validate_tool_params(params, tbl, source_name, table_name)
                break

    # Resolve $cte.field references in params from previous CTE results
    for key, val in list(params.items()):
        if isinstance(val, str):
            m = re.match(r"^\$(\w+)\.(.+)$", val)
            if m:
                cte_name, field = m.group(1), m.group(2)
                if cte_name in cte_results and isinstance(cte_results[cte_name], dict):
                    params[key] = _resolve_jsonpath(cte_results[cte_name], field)

    try:
        result = tools[tool_key](**params)
        return {"data": result}
    except Exception as e:
        return {"error": str(e)}


def _parse_tool_params(rendered: str) -> dict[str, Any]:
    """Extract all WHERE key=value pairs from rendered tool CTE content.

    Supports: quoted strings, numbers, booleans, None, JSON arrays/objects, $cte.field refs.
    """
    import ast

    params: dict[str, Any] = {}

    where_idx = rendered.upper().find("WHERE")
    if where_idx == -1:
        return params

    where_clause = rendered[where_idx + 5:].strip()

    # Split on top-level AND (not inside quotes or brackets)
    parts = _split_top_level(where_clause, "AND")

    for part in parts:
        part = part.strip()
        # Extract key = value (split on first =)
        m = re.match(r"(\w+)\s*=\s*(.+)", part, re.DOTALL)
        if not m:
            continue
        key = m.group(1)
        value_str = m.group(2).strip()

        # $cte.field reference — keep as-is, resolved later in _execute_tool_cte
        if re.match(r"^\$[\w.]+$", value_str):
            params[key] = value_str
            continue

        # Normalize lowercase booleans (not valid Python, but common in configs)
        if value_str.lower() == "true":
            params[key] = True
            continue
        if value_str.lower() == "false":
            params[key] = False
            continue

        # Try ast.literal_eval (handles strings, numbers, lists, dicts, Python True/False/None)
        try:
            params[key] = ast.literal_eval(value_str)
        except (ValueError, SyntaxError):
            # Unquoted bare word or other unparseable value — keep as string
            params[key] = value_str

    return params


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split text by separator, ignoring separators inside quotes, brackets, or parens."""
    parts: list[str] = []
    depth: dict[str, int] = {"[": 0, "{": 0, "(": 0}
    quote: str | None = None
    start = 0
    i = 0
    sep = f" {separator} "

    while i < len(text):
        ch = text[i]

        if quote:
            if ch == "\\" and i + 1 < len(text):
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue

        if ch == "[":
            depth["["] += 1
        elif ch == "]":
            depth["["] -= 1
        elif ch == "{":
            depth["{"] += 1
        elif ch == "}":
            depth["{"] -= 1
        elif ch == "(":
            depth["("] += 1
        elif ch == ")":
            depth["("] -= 1

        # Check for separator at top level
        if all(d == 0 for d in depth.values()) and text[i:].startswith(sep):
            parts.append(text[start:i])
            start = i + len(sep)
            i += len(sep)
            continue

        i += 1

    parts.append(text[start:])
    return parts


def _validate_tool_params(
    params: dict, table, source_name: str, table_name: str
) -> None:
    """Validate extracted params against table definition. Raises on missing required."""
    expected = table.params or table.input_schema
    if not expected:
        return

    for pname, pdef in expected.items():
        if isinstance(pdef, dict) and pdef.get("required") and pname not in params:
            raise ValueError(
                f"Tool '{source_name}.{table_name}': "
                f"missing required parameter '{pname}'. "
                f"Available params: {list(params.keys())}. "
                f"Expected: {list(expected.keys())}"
            )


def _execute_llm_cte(cte, cte_results, state, node, llm_client, db, run_id,
                     validation_feedback=None):
    context_parts: dict[str, Any] = {}

    if cte_results:
        context_parts["previous_steps"] = cte_results

    for ref_name in cte.model_refs:
        node_outputs = state.get("node_outputs", {})
        raw_value = None
        if ref_name in node_outputs:
            raw_value = node_outputs[ref_name]
        else:
            for key, val in node_outputs.items():
                if key.endswith("/" + ref_name) or key == ref_name:
                    raw_value = val
                    break

        if raw_value is None:
            continue

        # Apply context projection (SELECT columns + WHERE filter)
        projection = cte.context_projection
        if projection and projection.ref_name == ref_name:
            raw_value = _apply_context_projection(raw_value, projection)

        context_parts[ref_name] = raw_value

    # Build system prompt
    config = node.prompt.config
    system = node.prompt.system_prompt or "You are an AI assistant."

    if node.prompt.output_columns:
        cols = ", ".join(node.prompt.output_columns)
        system += (
            f"\n\nReturn ONLY a JSON object with these fields: {cols}.\n"
            "Do not include explanations, just the JSON."
        )
    else:
        system += "\n\nReturn ONLY a JSON object. Do not include explanations."

    if validation_feedback:
        system += f"\n\n{validation_feedback}"

    # Build messages
    messages: list[dict] = [{"role": "system", "content": system}]

    if context_parts:
        ctx = json.dumps(context_parts, indent=2, default=str)
        messages.append({
            "role": "user",
            "content": f"Here is the context from previous steps:\n```json\n{ctx}\n```",
        })

    messages.append({
        "role": "user",
        "content": cte.rendered_content or cte.raw_content,
    })

    # Call LLM
    model_name = config.model if config.model else "deepseek-chat"
    node_name = node.qualified_name
    cte_name = cte.name
    t0 = time.time()

    # Use LangGraph's native streaming via get_stream_writer()
    try:
        from langgraph.config import get_stream_writer
        writer = get_stream_writer()
    except RuntimeError:
        writer = None

    if writer is not None:
        writer({"event": "cte_start", "node": node_name, "cte": cte_name})

        stream = llm_client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            stream=True,
        )

        accumulated: list[str] = []
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                accumulated.append(delta.content)
                writer({"event": "token", "node": node_name, "cte": cte_name,
                        "delta": delta.content})

        content = "".join(accumulated)
        latency_ms = int((time.time() - t0) * 1000)
        usage = None
        writer({"event": "cte_end", "node": node_name, "cte": cte_name,
                "content": content})
    else:
        response = llm_client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
        latency_ms = int((time.time() - t0) * 1000)
        content = response.choices[0].message.content or ""
        usage = response.usage

    # Log trace to SQLite
    db.log_llm_call(
        run_id=run_id,
        node_name=node.qualified_name,
        step_name=cte.name,
        messages=messages,
        model=model_name,
        latency_ms=latency_ms,
        response_content=content,
        tokens_input=usage.prompt_tokens if usage else 0,
        tokens_output=usage.completion_tokens if usage else 0,
    )

    # Parse JSON from response
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Try extracting from markdown code block
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        return {"raw_response": content}


def _resolve_jsonpath(data: dict, path: str) -> Any:
    """Resolve dotted path against a dict. Returns None if missing."""
    for part in path.split("."):
        if isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def _apply_context_projection(raw_value, projection) -> dict | list:
    """Apply SELECT column projection and WHERE filtering to ref'd node output.

    raw_value can be a dict (single row) or list of dicts (multiple rows).
    Returns filtered dict or list.
    """
    from ..models.prompt import ContextProjection

    rows = raw_value if isinstance(raw_value, list) else [raw_value]
    columns = projection.columns
    conditions = projection.conditions
    logic = projection.logic

    filtered: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            filtered.append(row)
            continue

        if conditions:
            results = [_eval_condition(row, c) for c in conditions]
            if logic.upper() == "OR":
                if not any(results):
                    continue
            else:
                if not all(results):
                    continue

        if columns:
            projected = {c: row.get(c) for c in columns}
        else:
            projected = dict(row)

        filtered.append(projected)

    if isinstance(raw_value, list):
        return filtered
    return filtered[0] if filtered else {}


def _eval_condition(row: dict, condition) -> bool:
    """Evaluate a single WhereCondition against a row dict."""
    from ..models.prompt import WhereCondition

    field = condition.field
    op = condition.op
    value = condition.value
    row_value = row.get(field)

    try:
        if op == "=":
            return row_value == value
        elif op == "!=":
            return row_value != value
        elif op == ">":
            return (row_value is not None) and (row_value > value)
        elif op == "<":
            return (row_value is not None) and (row_value < value)
        elif op == ">=":
            return (row_value is not None) and (row_value >= value)
        elif op == "<=":
            return (row_value is not None) and (row_value <= value)
        return True
    except TypeError:
        return False
