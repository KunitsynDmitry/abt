"""NodeRunner — executes a single node's prompt with CTE loop, tools, and retries."""

import hashlib
import json
import os
import re
import time
from typing import Any, Callable

from openai import OpenAI

from ..models.node import CompiledNode
from .db import DatabaseManager


class NodeRunner:
    def __init__(
        self,
        compiled_node: CompiledNode,
        tools: list[Callable],
        db: DatabaseManager,
        llm_factory: Callable[[], OpenAI] | None = None,
        stream_callback: Callable[[str, str, str, str], None] | None = None,
        use_cache: bool = True,
    ):
        self.node = compiled_node
        self.tools = {t.__name__: t for t in tools}
        self.db = db
        self._llm_factory = llm_factory
        self._llm_client: OpenAI | None = None
        self._stream_callback = stream_callback
        self._use_cache = use_cache
        self._tool_call_pattern = re.compile(r"__SOURCE__(\w+)\.(\w+)__")

    def _get_llm(self) -> OpenAI:
        if self._llm_client is not None:
            return self._llm_client
        if self._llm_factory:
            self._llm_client = self._llm_factory()
            return self._llm_client
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError(
                "No LLM API key configured. Set DEEPSEEK_API_KEY environment variable "
                "or pass llm_factory to NodeRunner."
            )
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self._llm_client = OpenAI(api_key=api_key, base_url=base_url)
        return self._llm_client

    def make_node_function(self) -> Callable:
        node = self.node
        tools = self.tools
        db = self.db
        get_llm = self._get_llm
        stream_callback = self._stream_callback
        use_cache = self._use_cache

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
                                cte, cte_results, tools, db, run_id
                            )
                        else:
                            cte_results[cte.name] = _execute_llm_cte(
                                cte, cte_results, state, node,
                                get_llm(), db, run_id,
                                stream_callback=stream_callback,
                                validation_feedback=validation_feedback,
                            )

                    # Build output from SELECT columns
                    last_cte_name = node.prompt.cte_blocks[-1].name if node.prompt.cte_blocks else ""
                    final = cte_results.get(last_cte_name, cte_results) if last_cte_name else cte_results

                    if isinstance(final, dict):
                        if node.prompt.output_columns:
                            output = {}
                            for col in node.prompt.output_columns:
                                output[col] = final.get(col) or final.get(col.lower()) or f"[{col}]"
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


def _execute_tool_cte(cte, cte_results, tools, db, run_id):
    rendered = cte.rendered_content
    # Find tool references
    tool_pattern = re.compile(r"__SOURCE__(\w+)\.(\w+)__")
    matches = tool_pattern.findall(rendered)

    if not matches:
        return {"error": "No tool reference found in CTE"}

    source_name, table_name = matches[0]
    tool_key = f"{source_name}_{table_name}"

    if tool_key not in tools:
        return {"error": f"Tool '{source_name}.{table_name}' not bound to this node"}

    # Extract parameters from the rendered content
    # e.g. WHERE product_id = 'SKU-123' → {"product_id": "SKU-123"}
    params = {}
    where_match = re.search(r"WHERE\s+(\w+)\s*=\s*'([^']*)'", rendered, re.IGNORECASE)
    if where_match:
        params[where_match.group(1)] = where_match.group(2)

    try:
        result = tools[tool_key](**params)
        return {"data": result}
    except Exception as e:
        return {"error": str(e)}


def _execute_llm_cte(cte, cte_results, state, node, llm_client, db, run_id,
                     stream_callback=None, validation_feedback=None):
    context_parts: dict[str, Any] = {}

    if cte_results:
        context_parts["previous_steps"] = cte_results

    for ref_name in cte.model_refs:
        node_outputs = state.get("node_outputs", {})
        if ref_name in node_outputs:
            context_parts[ref_name] = node_outputs[ref_name]
        else:
            for key, val in node_outputs.items():
                if key.endswith("/" + ref_name) or key == ref_name:
                    context_parts[ref_name] = val
                    break

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
    t0 = time.time()

    if stream_callback is not None:
        node_name = node.qualified_name
        cte_name = cte.name
        stream_callback(node_name, cte_name, "", "cte_start")

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
                stream_callback(node_name, cte_name, delta.content, "token")

        content = "".join(accumulated)
        latency_ms = int((time.time() - t0) * 1000)
        usage = None
        stream_callback(node_name, cte_name, content, "cte_end")
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
