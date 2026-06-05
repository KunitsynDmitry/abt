"""FolderParser — walks the prompt directory tree and extracts routing structure.

Folder naming convention:
- Plain name (e.g., "inventory"): SEQUENTIAL routing
- "require_all" prefix: AND gate (all children must complete)
- "require_any" prefix: OR gate (any child success is sufficient)
- "require_first" prefix: sequential fallback (first successful child wins)
- "__" separator: metadata follows (e.g., "require_all__priority_high")
"""

import re
from pathlib import Path

from ..models.graph import RoutingType, SubgraphDef


class FolderParser:
    ROUTING_PATTERN = re.compile(
        r'^(require_all|require_any|require_first)(?:__(.+))?$'
    )

    @classmethod
    def parse_folder_name(cls, folder_name: str) -> tuple[RoutingType, dict]:
        """Parse folder name into routing type and metadata."""
        match = cls.ROUTING_PATTERN.match(folder_name)
        if match:
            routing_str = match.group(1)
            routing_map = {
                "require_all": RoutingType.REQUIRE_ALL,
                "require_any": RoutingType.REQUIRE_ANY,
                "require_first": RoutingType.REQUIRE_FIRST,
            }
            routing = routing_map[routing_str]
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
        blueprints_root: Path | None = None,
    ) -> SubgraphDef:
        """
        Recursively walk the directory, building a SubgraphDef tree.

        Args:
            root_path: Directory to walk
            prompt_files: Map of {relative_path: ParsedPrompt}
            parent_ref: Qualified name of parent
            order_index: Position within parent
            prompt_root: Top-level prompts directory (used for key computation)
            blueprints_root: Blueprints directory for resolving _references
        """
        if prompt_root is None:
            prompt_root = root_path

        folder_name = root_path.name
        routing, metadata = cls.parse_folder_name(folder_name)

        # Detect blueprint reference: folders starting with _ (e.g. _approval)
        if folder_name.startswith("_") and blueprints_root is not None:
            blueprint_name = folder_name[1:]  # strip _
            blueprint_dir = blueprints_root / blueprint_name
            if blueprint_dir.is_dir():
                # Graft the blueprint's tree under the current qualified path
                qualified = f"{parent_ref}.{folder_name}" if parent_ref else folder_name
                return cls._graft_blueprint(
                    blueprint_dir, prompt_files, qualified,
                    folder_name, routing, metadata, parent_ref,
                    order_index, prompt_root, blueprints_root,
                )

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
                    blueprints_root=blueprints_root,
                )
                subgraph.subgraphs.append(child_sg)
            elif entry.suffix == ".prompt":
                node_key = str(entry.relative_to(prompt_root).with_suffix("")).replace("\\", "/")
                if node_key in prompt_files:
                    subgraph.nodes.append(node_key)

        return subgraph

    @classmethod
    def _graft_blueprint(
        cls,
        blueprint_dir: Path,
        prompt_files: dict[str, any],
        qualified: str,
        folder_name: str,
        routing: RoutingType,
        metadata: dict,
        parent_ref: str | None,
        order_index: int,
        prompt_root: Path,
        blueprints_root: Path,
    ) -> SubgraphDef:
        """Build a SubgraphDef from a blueprint directory, mapping node keys."""
        subgraph = SubgraphDef(
            name=qualified,
            folder_name=folder_name,
            routing=routing,
            metadata=metadata,
            parent_ref=parent_ref,
            order_index=order_index,
        )

        entries = sorted(blueprint_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name))

        for entry in entries:
            if entry.name.startswith(".") or entry.name == "__pycache__":
                continue

            if entry.is_dir():
                # Prevent infinite recursion: _ folders in blueprints are real dirs
                child_sg = cls.build_tree(
                    entry, prompt_files, parent_ref=qualified,
                    order_index=len(subgraph.subgraphs),
                    prompt_root=prompt_root,
                    blueprints_root=None,  # no further blueprint resolution in blueprints
                )
                subgraph.subgraphs.append(child_sg)
            elif entry.suffix == ".prompt":
                # Blueprint prompts are keyed as blueprints/<name>/<file>
                # First try relative to the project root (if same filesystem)
                bp_key = f"blueprints/{entry.relative_to(blueprints_root).with_suffix('')}".replace("\\", "/")
                if bp_key in prompt_files:
                    subgraph.nodes.append(bp_key)
                else:
                    # Fallback: try as relative to prompt_root
                    try:
                        alt_key = str(entry.relative_to(prompt_root).with_suffix("")).replace("\\", "/")
                        if alt_key in prompt_files:
                            subgraph.nodes.append(alt_key)
                    except ValueError:
                        pass

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
