"""GraphBuilder — assembles a LangGraph StateGraph from the parsed project structure."""

from __future__ import annotations

from typing import Any

from ..models.graph import GraphStructure, SubgraphDef
from ..models.node import CompiledNode
from ..models.prompt import ParsedPrompt
from ..exceptions import GraphBuildError


# State key where node outputs are stored
NODE_OUTPUTS_KEY = "node_outputs"
ERRORS_KEY = "errors"


class GraphBuilder:
    def __init__(
        self,
        parsed_prompts: dict[str, ParsedPrompt],
        folder_tree: SubgraphDef,
        schema_registry: dict[str, type] | None = None,
        source_registry: dict[str, Any] | None = None,
        project_name: str = "abt_project",
    ):
        self.parsed_prompts = parsed_prompts
        self.tree = folder_tree
        self.schemas = schema_registry or {}
        self.sources = source_registry or {}
        self.project_name = project_name

    def build_structure(self) -> GraphStructure:
        """Build the full GraphStructure (metadata + compiled nodes + dependency graph)."""

        # Resolve compiled nodes
        all_nodes: dict[str, CompiledNode] = {}
        for qualified_name, prompt in self.parsed_prompts.items():
            node = self._compile_node(qualified_name, prompt)
            all_nodes[qualified_name] = node

        # Build dependency graph from ref() calls
        dep_graph: dict[str, set[str]] = {}
        all_qualified = sorted(all_nodes.keys())
        for qualified_name, node in all_nodes.items():
            deps = set()
            for dep_name in node.prompt.raw_dependencies:
                resolved = self._resolve_prompt_ref(dep_name, all_nodes)
                if resolved is None:
                    similar = _find_similar(dep_name, all_qualified)
                    hint = ""
                    if similar:
                        hint = f"\n  Did you mean? {', '.join(similar)}"
                    raise GraphBuildError(
                        f"Unresolved ref('{dep_name}') in '{qualified_name}'\n"
                        f"  File: {node.prompt.file_path}\n"
                        f"  Available refs ({len(all_qualified)}): "
                        + ", ".join(all_qualified[:8])
                        + ("..." if len(all_qualified) > 8 else "")
                        + hint
                    )
                deps.add(resolved)
            dep_graph[qualified_name] = deps

        # Resolve dynamic route targets
        for qualified_name, node in all_nodes.items():
            if node.route_map:
                resolved_map: dict[str, str] = {}
                for value, target in node.route_map.items():
                    if target in ("__END__", "END"):
                        resolved_map[value] = "__END__"
                    else:
                        resolved = self._resolve_prompt_ref(target, all_nodes)
                        if resolved is None:
                            all_qualified = sorted(all_nodes.keys())
                            similar = _find_similar(target, all_qualified)
                            hint = ""
                            if similar:
                                hint = f"\n  Did you mean? {', '.join(similar)}"
                            raise GraphBuildError(
                                f"Route target '{target}' in node '{qualified_name}' "
                                f"does not exist\n"
                                f"  File: {node.prompt.file_path}\n"
                                f"  Available nodes ({len(all_qualified)}): "
                                + ", ".join(all_qualified[:8])
                                + ("..." if len(all_qualified) > 8 else "")
                                + hint
                            )
                        resolved_map[value] = resolved
                node.route_map = resolved_map

        return GraphStructure(
            project_name=self.project_name,
            root=self.tree,
            all_nodes=all_nodes,
            all_schemas=self.schemas,
            all_sources=self.sources,
            dependency_graph=dep_graph,
        )

    def _compile_node(self, qualified_name: str, prompt: ParsedPrompt) -> CompiledNode:
        output_schema = None
        if prompt.config.output_schema and prompt.config.output_schema in self.schemas:
            output_schema = self.schemas[prompt.config.output_schema]

        llm_config = {
            "provider": prompt.config.provider,
            "model": prompt.config.model,
            "temperature": prompt.config.temperature,
            "max_tokens": prompt.config.max_tokens,
        }

        # Parse dynamic routing config
        route_on = prompt.config.route_on or None
        route_map: dict[str, str] = {}
        route_default = prompt.config.route_default or None

        for entry in prompt.config.route_when:
            if ":" in entry:
                value, target = entry.split(":", 1)
                route_map[value.strip()] = target.strip()

        # Resolve route targets to qualified names (deferred — done in build_structure)
        # For now, store leaf names; build_structure will resolve them

        return CompiledNode(
            name=prompt.name,
            qualified_name=qualified_name,
            prompt=prompt,
            output_schema_type=output_schema,
            resolved_tools=[f"{s}.{t}" for s, t in prompt.raw_source_refs],
            on_fail_target=prompt.config.on_fail_route,
            max_retries=prompt.config.max_retries,
            llm_config=llm_config,
            route_on=route_on,
            route_map=route_map,
            route_default=route_default,
            approve_when=prompt.config.approve_when or None,
            approve_message=prompt.config.approve_message or None,
        )

    def _resolve_prompt_ref(
        self, dep_name: str, all_nodes: dict[str, CompiledNode]
    ) -> str | None:
        """Resolve a ref('name') to a qualified node path.

        Tries exact match first, then leaf name match. Fails with a
        diagnostic error when the leaf name is ambiguous.
        """
        # Exact match against qualified name
        if dep_name in all_nodes:
            return dep_name
        # Match against leaf name — collect all candidates
        matches = [
            qualified
            for qualified, node in all_nodes.items()
            if node.name == dep_name
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise GraphBuildError(
                f"Ambiguous ref('{dep_name}'): matches {len(matches)} nodes.\n"
                + "\n".join(f"  • {m}" for m in sorted(matches))
                + f"\nUse a qualified name (e.g. ref('{matches[0]}'))."
            )
        return None

def _subgraph_to_dict(sg: SubgraphDef) -> dict:
    return {
        "name": sg.name,
        "folder_name": sg.folder_name,
        "routing": sg.routing.value,
        "metadata": sg.metadata,
        "nodes": sg.nodes,
        "subgraphs": [_subgraph_to_dict(s) for s in sg.subgraphs],
    }


def _find_similar(target: str, candidates: list[str], limit: int = 3) -> list[str]:
    """Find candidate strings that contain the target as a substring."""
    lower = target.lower()
    matches = [c for c in candidates if lower in c.lower()]
    return matches[:limit]
