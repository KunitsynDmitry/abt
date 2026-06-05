"""NodeSelector — dbt-style node selection from manifest.json.

Parses selectors like +name+, tag:xxx, path:xxx, glob patterns.
Resolves against the manifest's dependency graph and routing tree.
"""

from __future__ import annotations

import fnmatch
from collections import deque
from typing import Any


class NodeSelector:
    """Resolves dbt-style selectors to sets of qualified node names.

    Uses manifest.json as the source of truth for node resolution.
    """

    def __init__(self, manifest: dict[str, Any]) -> None:
        self.nodes: dict[str, dict] = manifest["nodes"]
        self.dep_graph: dict[str, list[str]] = manifest["graph"]["dependency_graph"]
        self.routing_tree: dict = manifest["graph"]["routing_tree"]
        self.topo_order: list[str] = manifest["graph"]["topological_order"]

        # Build reverse graph for downstream traversal
        self._reverse_graph: dict[str, list[str]] = {}
        for node, upstream in self.dep_graph.items():
            self._reverse_graph.setdefault(node, [])
            for dep in upstream:
                self._reverse_graph.setdefault(dep, [])
                self._reverse_graph[dep].append(node)

    # ── Public API ──────────────────────────────────────────────

    def resolve_selectors(self, selectors: list[str] | None) -> list[str]:
        """Resolve a list of selector strings to a topologically sorted list of node names.

        If selectors is empty or None, returns all nodes in topological order.
        """
        if not selectors:
            return list(self.topo_order)

        matched: set[str] = set()
        for sel in selectors:
            matched |= self._parse_single(sel)

        return [n for n in self.topo_order if n in matched]

    def resolve_exclusions(
        self, names: list[str], exclusions: list[str] | None
    ) -> list[str]:
        """Remove excluded nodes from a resolved list."""
        if not exclusions:
            return list(names)

        excluded: set[str] = set()
        for sel in exclusions:
            excluded |= self._parse_single(sel)

        return [n for n in names if n not in excluded]

    # ── Single selector parsing ─────────────────────────────────

    def _parse_single(self, selector: str) -> set[str]:
        """Parse a single selector string into a set of qualified names."""
        s = selector.strip()

        if not s:
            return set()

        # tag:xxx
        if s.startswith("tag:"):
            return self._match_tag(s[4:])

        # path:xxx
        if s.startswith("path:"):
            return self._match_path(s[5:])

        # source:xxx (future)
        if s.startswith("source:"):
            return self._match_source_ref(s[7:])

        # +name+ — ancestors + descendants
        if s.startswith("+") and s.endswith("+"):
            name = s[1:-1]
            resolved = self._resolve_name(name)
            if not resolved:
                return set()
            return self._ancestors(resolved) | self._descendants(resolved)

        # name+ — node + ancestors
        if s.endswith("+"):
            name = s[:-1]
            resolved = self._resolve_name(name)
            if not resolved:
                return set()
            return self._ancestors(resolved)

        # +name — node + descendants
        if s.startswith("+"):
            name = s[1:]
            resolved = self._resolve_name(name)
            if not resolved:
                return set()
            return self._descendants(resolved)

        # Plain name or glob pattern
        if "*" in s or "?" in s or "[" in s:
            return self._match_glob(s)
        else:
            resolved = self._resolve_name(s)
            return {resolved} if resolved else set()

    # ── Name resolution ─────────────────────────────────────────

    def _resolve_name(self, name: str) -> str | None:
        """Resolve a name to a qualified name. Returns None if not found."""
        # Exact match
        if name in self.nodes:
            return name
        # Match against leaf name (last component of qualified name)
        for qualified, node in self.nodes.items():
            if node["name"] == name:
                return qualified
        return None

    # ── Graph traversal ─────────────────────────────────────────

    def _ancestors(self, name: str) -> set[str]:
        """Return {name} ∪ all upstream nodes reachable via dep_graph (BFS)."""
        result: set[str] = {name}
        queue = deque([name])
        while queue:
            current = queue.popleft()
            for dep in self.dep_graph.get(current, []):
                if dep not in result:
                    result.add(dep)
                    queue.append(dep)
        return result

    def _descendants(self, name: str) -> set[str]:
        """Return {name} ∪ all downstream nodes reachable via reverse graph (BFS)."""
        result: set[str] = {name}
        queue = deque([name])
        while queue:
            current = queue.popleft()
            for dependent in self._reverse_graph.get(current, []):
                if dependent not in result:
                    result.add(dependent)
                    queue.append(dependent)
        return result

    # ── Tag matching ────────────────────────────────────────────

    def _match_tag(self, tag: str) -> set[str]:
        """Return all nodes in subgraphs whose metadata contains the given tag."""
        result: set[str] = set()
        self._collect_tagged_nodes(self.routing_tree, tag, result)
        return result

    def _collect_tagged_nodes(
        self, tree: dict, tag: str, result: set[str]
    ) -> None:
        """Recursively collect nodes from subgraphs matching the tag."""
        metadata = tree.get("metadata", {})
        if self._metadata_has_tag(metadata, tag):
            self._collect_all_leaves(tree, result)

        for child in tree.get("subgraphs", []):
            self._collect_tagged_nodes(child, tag, result)

    @staticmethod
    def _metadata_has_tag(metadata: dict, tag: str) -> bool:
        """Check if metadata contains a tag matching the selector.

        Supports: {"tag": "fast"} and {"tags": ["fast", "daily"]}.
        """
        if metadata.get("tag") == tag:
            return True
        tags = metadata.get("tags", [])
        return tag in tags

    def _collect_all_leaves(self, tree: dict, result: set[str]) -> None:
        """Recursively collect all leaf node names from a tree into result."""
        for node_name in tree.get("nodes", []):
            result.add(node_name)
        for child in tree.get("subgraphs", []):
            self._collect_all_leaves(child, result)

    # ── Path matching ───────────────────────────────────────────

    def _match_path(self, path: str) -> set[str]:
        """Return all nodes whose file_path starts with the given path prefix."""
        result: set[str] = set()
        for qualified, node in self.nodes.items():
            file_path = node.get("file_path", "")
            if file_path.startswith(path):
                result.add(qualified)
        return result

    # ── Glob matching ───────────────────────────────────────────

    def _match_glob(self, pattern: str) -> set[str]:
        """Return all nodes whose qualified_name or leaf name matches the glob pattern."""
        result: set[str] = set()
        for qualified, node in self.nodes.items():
            leaf = node["name"]
            if fnmatch.fnmatch(qualified, pattern) or fnmatch.fnmatch(leaf, pattern):
                result.add(qualified)
        return result

    # ── Source ref matching ─────────────────────────────────────

    def _match_source_ref(self, source_name: str) -> set[str]:
        """Return all nodes that reference a given source."""
        result: set[str] = set()
        for qualified, node in self.nodes.items():
            for src_ref in node.get("source_refs", []):
                if src_ref and src_ref[0] == source_name:
                    result.add(qualified)
                    break
        return result
