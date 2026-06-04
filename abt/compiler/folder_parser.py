"""FolderParser — walks the prompt directory tree and extracts routing structure.

Folder naming convention:
- Plain name (e.g., "inventory"): SEQUENTIAL routing
- "require_all" prefix: AND gate (all children must complete)
- "require_any" prefix: OR gate (any child success is sufficient)
- "__" separator: metadata follows (e.g., "require_all__priority_high")
"""

import re
from pathlib import Path

from ..models.graph import RoutingType, SubgraphDef


class FolderParser:
    ROUTING_PATTERN = re.compile(
        r'^(require_all|require_any)(?:__(.+))?$'
    )

    @classmethod
    def parse_folder_name(cls, folder_name: str) -> tuple[RoutingType, dict]:
        """Parse folder name into routing type and metadata."""
        match = cls.ROUTING_PATTERN.match(folder_name)
        if match:
            routing_str = match.group(1)
            routing = RoutingType.REQUIRE_ALL if routing_str == "require_all" else RoutingType.REQUIRE_ANY
            meta = {"tag": match.group(2)} if match.group(2) else {}
            return routing, meta
        return RoutingType.SEQUENTIAL, {}

    @classmethod
    def build_tree(
        cls,
        root_path: Path,
        prompt_files: dict[str, any],
        parent_ref: str | None = None,
        order_index: int = 0,
        prompt_root: Path | None = None,
    ) -> SubgraphDef:
        """
        Recursively walk the directory, building a SubgraphDef tree.

        Args:
            root_path: Directory to walk
            prompt_files: Map of {relative_path: ParsedPrompt}
            parent_ref: Qualified name of parent
            order_index: Position within parent
            prompt_root: Top-level prompts directory (used for key computation)
        """
        if prompt_root is None:
            prompt_root = root_path

        folder_name = root_path.name
        routing, metadata = cls.parse_folder_name(folder_name)

        qualified = f"{parent_ref}.{folder_name}" if parent_ref else folder_name

        subgraph = SubgraphDef(
            name=qualified,
            folder_name=folder_name,
            routing=routing,
            metadata=metadata,
            parent_ref=parent_ref,
            order_index=order_index,
        )

        # Collect entries: directories first (sorted), then .prompt files (sorted)
        entries = sorted(root_path.iterdir(), key=lambda p: (not p.is_dir(), p.name))

        for entry in entries:
            if entry.name.startswith(".") or entry.name == "__pycache__":
                continue

            if entry.is_dir():
                child_sg = cls.build_tree(
                    entry, prompt_files, parent_ref=qualified,
                    order_index=len(subgraph.subgraphs),
                    prompt_root=prompt_root,
                )
                subgraph.subgraphs.append(child_sg)
            elif entry.suffix == ".prompt":
                node_key = str(entry.relative_to(prompt_root).with_suffix("")).replace("\\", "/")
                if node_key in prompt_files:
                    subgraph.nodes.append(node_key)

        return subgraph

    @classmethod
    def collect_all_prompt_paths(cls, subgraph: SubgraphDef) -> list[str]:
        """Collect all leaf prompt paths from the tree in dependency order."""
        result = []
        for sg in sorted(subgraph.subgraphs, key=lambda s: s.order_index):
            result.extend(cls.collect_all_prompt_paths(sg))
        for node in sorted(subgraph.nodes):
            result.append(node)
        return result
