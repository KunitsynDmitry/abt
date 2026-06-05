"""CacheManager — coordinates incremental compilation via manifest.json cache."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .fingerprint import hash_file, hash_file_list
from ..models.prompt import CTEBlock, ParsedPrompt, PromptConfig


class CacheManager:
    """Loads previous manifest, detects staleness, merges cached + fresh results."""

    GLOBAL_KEYS = ["__project__", "__macros__", "__schemas__", "__sources__"]

    def __init__(self, target_dir: Path):
        self.target_dir = target_dir
        self.manifest_path = target_dir / "manifest.json"

    def load_previous_manifest(self) -> dict[str, Any] | None:
        """Load the manifest from the previous compilation, if it exists."""
        if not self.manifest_path.exists():
            return None
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            return None

    def compute_hashes(self, loader, prompt_root: Path) -> dict[str, str]:
        """Compute SHA256 hashes for all source files in the project."""
        return self._compute_current_hashes(loader, prompt_root)

    def detect_changes(
        self,
        loader,
        prompt_root: Path,
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        """Compare current file hashes against cached manifest.

        Returns a dict with:
        - full_rebuild: bool — true if everything must be recompiled
        - reason: str | None — explanation when full_rebuild is True
        - changed_qualified: set[str] — qualified names of stale prompts
        - unchanged_qualified: set[str] — qualified names to reuse from cache
        - current_hashes: dict[str, str] — fresh file hashes for the new manifest
        """
        current = self._compute_current_hashes(loader, prompt_root)
        prev_hashes = previous.get("file_hashes", {})
        empty = {
            "current_hashes": current,
            "changed_qualified": set(),
            "unchanged_qualified": set(),
        }

        # 1. Structural change: prompt file count differs
        prev_prompts = {
            k for k in prev_hashes
            if k not in self.GLOBAL_KEYS
        }
        curr_prompts = {
            k for k in current
            if k not in self.GLOBAL_KEYS
        }
        if prev_prompts != curr_prompts:
            return {
                **empty,
                "full_rebuild": True,
                "reason": "prompt files added or removed",
            }

        # 2. Global invalidation: project config, macros, schemas, sources
        for key in self.GLOBAL_KEYS:
            if key in current and current[key] != prev_hashes.get(key):
                label = key.strip("_").replace("_", " ")
                return {
                    **empty,
                    "full_rebuild": True,
                    "reason": f"{label} changed",
                }

        # 3. Per-prompt diff
        changed = set()
        unchanged = set()
        for qualified_name in curr_prompts:
            if current[qualified_name] != prev_hashes.get(qualified_name):
                changed.add(qualified_name)
            else:
                unchanged.add(qualified_name)

        return {
            **empty,
            "full_rebuild": False,
            "reason": None,
            "changed_qualified": changed,
            "unchanged_qualified": unchanged,
        }

    def load_cached_prompts(
        self,
        qualified_names: set[str],
        previous: dict[str, Any],
        project_root: Path,
        prompt_rel: str,
    ) -> dict[str, ParsedPrompt]:
        """Reconstruct ParsedPrompt dict from manifest nodes for unchanged files."""
        cached: dict[str, ParsedPrompt] = {}
        prev_nodes = previous.get("nodes", {})
        prompt_base = project_root / prompt_rel

        for qname in qualified_names:
            node_dict = prev_nodes.get(qname)
            if node_dict is None:
                continue
            cached[qname] = self._prompt_from_manifest_node(
                node_dict, prompt_base
            )
        return cached

    def _compute_current_hashes(
        self, loader, prompt_root: Path
    ) -> dict[str, str]:
        """Compute SHA256 hashes for all source files in the project."""
        hashes: dict[str, str] = {}

        # Project config
        project_yml = loader.root / "abt_project.yml"
        if project_yml.exists():
            hashes["__project__"] = hash_file(project_yml)

        # Macros (combined — any macro change invalidates all prompts)
        macro_files = loader.list_macro_files()
        if macro_files:
            hashes["__macros__"] = hash_file_list(macro_files)

        # Schemas (combined — any schema change invalidates all prompts)
        schema_files = loader.list_schema_files()
        if schema_files:
            hashes["__schemas__"] = hash_file_list(schema_files)

        # Sources (combined — any source change invalidates all prompts)
        source_files = loader.list_source_files()
        if source_files:
            hashes["__sources__"] = hash_file_list(source_files)

        # Individual prompt files, keyed by qualified name
        for fp in loader.list_prompt_files():
            try:
                relative = fp.relative_to(prompt_root)
            except ValueError:
                # File outside prompt_root — use absolute path as fallback
                qualified = fp.stem
            else:
                qualified = str(relative.with_suffix("")).replace("\\", "/")
            hashes[qualified] = hash_file(fp)

        return hashes

    @staticmethod
    def _prompt_from_manifest_node(
        node_dict: dict[str, Any],
        prompt_base: Path,
    ) -> ParsedPrompt:
        """Reconstruct a ParsedPrompt from a serialized manifest node entry."""
        cte_blocks = []
        for cte_dict in node_dict.get("cte_blocks", []):
            cte_config = None
            if cte_dict.get("config"):
                cte_config = PromptConfig(**cte_dict["config"])
            cte_blocks.append(CTEBlock(
                name=cte_dict["name"],
                raw_content=cte_dict.get("raw_content", ""),
                rendered_content=cte_dict.get("rendered_content", ""),
                cte_type=cte_dict.get("cte_type"),
                is_tool_step=cte_dict.get("is_tool_step", False),
                tool_refs=[tuple(t) for t in cte_dict.get("tool_refs", [])],
                model_refs=cte_dict.get("model_refs", []),
                config=cte_config,
            ))

        file_path_str = node_dict.get("file_path", "")
        relative_path = Path(file_path_str)
        file_path = prompt_base / file_path_str

        config_dict = node_dict.get("config", {})
        if config_dict is None:
            config_dict = {}

        source_refs_raw = node_dict.get("source_refs", [])
        source_refs = {tuple(t) for t in source_refs_raw}

        return ParsedPrompt(
            name=node_dict.get("name", ""),
            file_path=file_path,
            relative_path=relative_path,
            config=PromptConfig(**config_dict),
            system_prompt=node_dict.get("system_prompt", ""),
            cte_blocks=cte_blocks,
            output_columns=node_dict.get("output_columns", []),
            raw_dependencies=set(node_dict.get("dependencies", [])),
            raw_source_refs=source_refs,
        )
