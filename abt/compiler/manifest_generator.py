"""Manifest generator — produces manifest.json from compiled GraphStructure."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..models.config import AbtProjectConfig
from ..models.graph import GraphStructure
from ..compiler.graph_builder import _subgraph_to_dict


def load_manifest(target_dir: Path) -> dict[str, Any] | None:
    """Load a previously generated manifest.json, if it exists."""
    path = target_dir / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError):
        return None


def generate_manifest(
    graph_structure: GraphStructure,
    project_config: AbtProjectConfig,
    file_hashes: dict[str, str] | None = None,
    triggers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the full manifest dict from a compiled GraphStructure and project config.

    If file_hashes is provided, it is included as the ``file_hashes`` section
    for use as a cache fingerprint on the next compilation.
    """

    topological_order = _topological_sort(graph_structure.dependency_graph)

    nodes = {}
    total_cte_blocks = 0
    for qualified_name, node in graph_structure.all_nodes.items():
        node_dict = _serialize_node(node, graph_structure.dependency_graph)
        nodes[qualified_name] = node_dict
        total_cte_blocks += len(node.prompt.cte_blocks)

    sources = {}
    for source_name, source_def in graph_structure.all_sources.items():
        sources[source_name] = source_def.model_dump()

    schemas = {}
    for schema_name, model_cls in graph_structure.all_schemas.items():
        schemas[schema_name] = _serialize_schema(schema_name, model_cls)

    triggers_serialized = {}
    if triggers:
        for trigger_name, trigger_def in triggers.items():
            triggers_serialized[trigger_name] = trigger_def.model_dump()

    graph = {
        "routing_tree": _subgraph_to_dict(graph_structure.root),
        "dependency_graph": {
            node: sorted(deps) for node, deps in graph_structure.dependency_graph.items()
        },
        "topological_order": topological_order,
    }

    project = {
        "name": project_config.name,
        "version": project_config.version,
        "paths": project_config.paths.model_dump(),
        "models": {
            k: v.model_dump() for k, v in project_config.models.items()
        },
        "vars": project_config.vars,
    }

    result: dict[str, Any] = {
        "metadata": {
            "project_name": project_config.name,
            "project_version": project_config.version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "abt_version": __version__,
            "node_count": len(graph_structure.all_nodes),
            "source_count": len(graph_structure.all_sources),
            "schema_count": len(graph_structure.all_schemas),
            "trigger_count": len(triggers_serialized),
            "total_cte_blocks": total_cte_blocks,
        },
        "file_hashes": file_hashes or {},
        "nodes": nodes,
        "sources": sources,
        "schemas": schemas,
        "triggers": triggers_serialized,
        "graph": graph,
        "project": project,
    }
    return result


def _serialize_node(node: Any, dep_graph: dict[str, set[str]]) -> dict[str, Any]:
    prompt = node.prompt

    cte_blocks = []
    for cte in prompt.cte_blocks:
        cte_blocks.append({
            "name": cte.name,
            "raw_content": cte.raw_content,
            "rendered_content": cte.rendered_content,
            "cte_type": cte.cte_type,
            "is_tool_step": cte.is_tool_step,
            "tool_refs": [list(t) for t in cte.tool_refs],
            "model_refs": cte.model_refs,
            "config": cte.config.model_dump() if cte.config else None,
        })

    return {
        "name": node.name,
        "qualified_name": node.qualified_name,
        "file_path": prompt.relative_path.as_posix(),
        "config": {
            "model": prompt.config.model,
            "temperature": prompt.config.temperature,
            "max_tokens": prompt.config.max_tokens,
            "on_fail_route": prompt.config.on_fail_route,
            "max_retries": prompt.config.max_retries,
            "max_tool_calls": prompt.config.max_tool_calls,
            "on_exhaust": prompt.config.on_exhaust,
            "allowed_tools": prompt.config.allowed_tools,
            "output_schema": prompt.config.output_schema,
            "route_on": prompt.config.route_on,
            "route_when": prompt.config.route_when,
            "route_default": prompt.config.route_default,
            "approve_when": prompt.config.approve_when,
            "approve_message": prompt.config.approve_message,
        },
        "system_prompt": prompt.system_prompt,
        "cte_blocks": cte_blocks,
        "output_columns": prompt.output_columns,
        "dependencies": sorted(dep_graph.get(node.qualified_name, set())),
        "source_refs": [list(t) for t in sorted(prompt.raw_source_refs)],
        "resolved_tools": node.resolved_tools,
        "output_schema": prompt.config.output_schema or None,
        "on_fail_target": node.on_fail_target,
        "max_retries": node.max_retries,
        "route_on": node.route_on,
        "route_map": node.route_map,
        "route_default": node.route_default,
        "approve_when": node.approve_when,
        "approve_message": node.approve_message,
    }


def _clean_type_name(annotation) -> str:
    """Convert a type annotation to a readable string."""
    origin = getattr(annotation, "__origin__", None)
    if origin is list:
        args = getattr(annotation, "__args__", ())
        if args:
            inner = _clean_type_name(args[0])
            return f"list[{inner}]"
        return "list"
    if origin is dict:
        return "dict"
    # typing.Literal[...]
    if str(origin) == "typing.Literal":
        args = getattr(annotation, "__args__", ())
        values = ", ".join(map(repr, args))
        return f"Literal[{values}]"
    name = getattr(annotation, "__name__", str(annotation))
    return name


def _serialize_schema(schema_name: str, model_cls: type) -> dict[str, Any]:
    json_schema = model_cls.model_json_schema()

    fields = []
    for field_name, field_info in model_cls.model_fields.items():
        field_def: dict[str, Any] = {
            "name": field_name,
            "type": _clean_type_name(field_info.annotation),
            "description": field_info.description or "",
            "required": field_info.is_required(),
        }
        if field_info.default is not None and not field_info.is_required():
            field_def["default"] = field_info.default
        fields.append(field_def)

    return {
        "name": schema_name,
        "description": json_schema.get("description", ""),
        "fields": fields,
        "json_schema": json_schema,
    }


def _topological_sort(dep_graph: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm — returns nodes in topological order.

    dep_graph is {node: {upstream_deps}}. An edge dep → node means
    dep must execute before node.
    """
    # Collect all nodes
    all_nodes: set[str] = set(dep_graph.keys())
    for deps in dep_graph.values():
        all_nodes.update(deps)

    # in_degree: how many unfulfilled upstream deps each node has
    in_degree: dict[str, int] = {n: 0 for n in all_nodes}
    # reverse_graph: node → nodes that depend on it
    reverse: dict[str, set[str]] = {n: set() for n in all_nodes}

    for node, upstream in dep_graph.items():
        in_degree[node] = len(upstream)
        for dep in upstream:
            reverse[dep].add(node)

    # Start with nodes that have no upstream deps
    queue = [n for n, d in in_degree.items() if d == 0]
    result = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        for dependent in reverse.get(node, set()):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    # Any remaining nodes are part of a cycle — append them anyway
    remaining = [n for n, d in in_degree.items() if d > 0]
    result.extend(remaining)

    return result
