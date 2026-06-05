"""Test dbt-style node selectors — unit tests + integration with GraphExecutor."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from abt.compiler.manifest_generator import generate_manifest
from abt.compiler.selector import NodeSelector


def _load_manifest() -> dict:
    """Load manifest from the example project."""
    project_root = Path(__file__).parent.parent / "example_project"

    from abt.project import ProjectLoader
    from abt.compiler.schema_parser import SchemaParser
    from abt.compiler.source_parser import SourceParser
    from abt.compiler.jinja_env import AbtJinjaEnv
    from abt.compiler.prompt_compiler import PromptCompiler
    from abt.compiler.folder_parser import FolderParser
    from abt.compiler.graph_builder import GraphBuilder

    loader = ProjectLoader(project_root)
    config = loader.load()

    schemas = SchemaParser(loader).parse_all()
    sources = SourceParser(loader).parse_all()

    jinja_env = AbtJinjaEnv(
        schema_registry=schemas, source_registry=sources,
        project_vars=config.vars, strict=False,
    )
    prompt_compiler = PromptCompiler(jinja_env, defaults=config.models.get("default"))
    parsed = prompt_compiler.compile_all(
        loader.list_prompt_files(), project_root / "prompts"
    )
    folder_tree = FolderParser.build_tree(project_root / "prompts", parsed)

    gb = GraphBuilder(
        parsed_prompts=parsed, folder_tree=folder_tree,
        schema_registry=schemas, source_registry=sources,
        project_name=config.name,
    )
    graph_structure = gb.build_structure()
    return generate_manifest(graph_structure, config)


MANIFEST = None


def get_manifest() -> dict:
    global MANIFEST
    if MANIFEST is None:
        MANIFEST = _load_manifest()
    return MANIFEST


def get_selector() -> NodeSelector:
    return NodeSelector(get_manifest())


def test_exact_name():
    """Select by exact qualified name."""
    ns = get_selector()
    result = ns.resolve_selectors(["decide"])
    assert result == ["decide"], f"Expected ['decide'], got {result}"


def test_leaf_name():
    """Select by leaf name (last component)."""
    ns = get_selector()
    result = ns.resolve_selectors(["check_stock"])
    assert "require_all/check_stock" in result
    assert "require_all/check_demand" not in result


def test_ancestors():
    """Select node + ancestors (upstream)."""
    ns = get_selector()
    result = ns.resolve_selectors(["decide+"])
    assert "decide" in result
    assert "require_all/check_stock" in result
    assert "require_all/check_demand" in result
    # fallback nodes should NOT be included
    assert "require_any__fast/fallback_a" not in result
    assert "require_any__fast/fallback_b" not in result


def test_descendants():
    """Select node + descendants (downstream)."""
    ns = get_selector()
    result = ns.resolve_selectors(["+require_all/check_stock"])
    assert set(result) == {"require_all/check_stock", "decide"}, \
        f"Expected {{check_stock, decide}}, got {set(result)}"


def test_ancestors_and_descendants():
    """Select node + ancestors + descendants.

    check_stock has no upstream deps, its only downstream is decide.
    check_demand is a sibling (parallel), not an ancestor or descendant.
    """
    ns = get_selector()
    result = ns.resolve_selectors(["+require_all/check_stock+"])
    assert set(result) == {"require_all/check_stock", "decide"}, \
        f"Expected {{check_stock, decide}}, got {set(result)}"


def test_tag():
    """Select by metadata tag."""
    ns = get_selector()
    result = ns.resolve_selectors(["tag:fast"])
    assert set(result) == {
        "require_any__fast/fallback_a",
        "require_any__fast/fallback_b",
    }, f"Expected fallback nodes, got {set(result)}"


def test_path():
    """Select by file_path prefix."""
    ns = get_selector()
    result = ns.resolve_selectors(["path:require_all"])
    assert set(result) == {
        "require_all/check_stock",
        "require_all/check_demand",
    }, f"Expected require_all nodes, got {set(result)}"


def test_glob():
    """Select by glob pattern."""
    ns = get_selector()
    result = ns.resolve_selectors(["check_*"])
    assert set(result) == {
        "require_all/check_stock",
        "require_all/check_demand",
    }, f"Expected check_* nodes, got {set(result)}"


def test_glob_wildcard():
    """Select by glob with * in middle."""
    ns = get_selector()
    result = ns.resolve_selectors(["require_all/*"])
    assert set(result) == {
        "require_all/check_stock",
        "require_all/check_demand",
    }, f"Expected require_all/*, got {set(result)}"


def test_union():
    """Two selectors = union of both sets."""
    ns = get_selector()
    result = ns.resolve_selectors(["decide", "tag:fast"])
    assert "decide" in result
    assert "require_any__fast/fallback_a" in result
    assert "require_any__fast/fallback_b" in result
    assert len(result) == 3, f"Expected 3 nodes (union), got {len(result)}: {result}"


def test_exclusion():
    """Select all, then exclude some."""
    ns = get_selector()
    all_nodes = ns.resolve_selectors(None)
    assert len(all_nodes) == 5

    filtered = ns.resolve_exclusions(all_nodes, ["tag:fast"])
    assert len(filtered) == 3
    assert "require_any__fast/fallback_a" not in filtered
    assert "require_any__fast/fallback_b" not in filtered


def test_exact_name_not_found():
    """Non-existent name returns empty set."""
    ns = get_selector()
    result = ns.resolve_selectors(["nonexistent"])
    assert result == [], f"Expected [], got {result}"


def test_empty_selectors():
    """Empty selectors returns all nodes in topological order."""
    ns = get_selector()
    result = ns.resolve_selectors(None)
    assert len(result) == 5
    # decide must be last (it depends on others)
    assert result[-1] == "decide", f"Expected decide last, got {result}"


def test_topological_order_preserved():
    """Selected nodes maintain topological order from manifest."""
    ns = get_selector()
    result = ns.resolve_selectors(["decide+"])
    # decide depends on check_stock and check_demand, so it must be last
    decide_idx = result.index("decide")
    stock_idx = result.index("require_all/check_stock")
    demand_idx = result.index("require_all/check_demand")
    assert stock_idx < decide_idx, f"check_stock must be before decide"
    assert demand_idx < decide_idx, f"check_demand must be before decide"


# ── Integration test with GraphExecutor ──────────────────────────


def test_filtered_execution():
    """Full pipeline: select decide+ and verify only 3 nodes execute."""
    from abt.project import ProjectLoader
    from abt.compiler.schema_parser import SchemaParser
    from abt.compiler.source_parser import SourceParser
    from abt.compiler.jinja_env import AbtJinjaEnv
    from abt.compiler.prompt_compiler import PromptCompiler
    from abt.compiler.folder_parser import FolderParser
    from abt.compiler.graph_builder import GraphBuilder
    from abt.runtime.db import DatabaseManager
    from abt.runtime.tool_table import ToolTable
    from abt.runtime.executor import GraphExecutor
    from unittest.mock import MagicMock

    project_root = Path(__file__).parent.parent / "example_project"
    loader = ProjectLoader(project_root)
    config = loader.load()

    schemas = SchemaParser(loader).parse_all()
    sources = SourceParser(loader).parse_all()
    jinja_env = AbtJinjaEnv(schema_registry=schemas, source_registry=sources,
                             project_vars=config.vars, strict=False)
    prompt_compiler = PromptCompiler(jinja_env, defaults=config.models.get("default"))
    parsed = prompt_compiler.compile_all(
        loader.list_prompt_files(), project_root / "prompts"
    )
    folder_tree = FolderParser.build_tree(project_root / "prompts", parsed)
    gb = GraphBuilder(parsed_prompts=parsed, folder_tree=folder_tree,
                      schema_registry=schemas, source_registry=sources,
                      project_name=config.name)
    graph_structure = gb.build_structure()
    manifest = generate_manifest(graph_structure, config)

    # Apply selector: decide+ → decide + check_stock + check_demand (3 nodes)
    ns = NodeSelector(manifest)
    selected = ns.resolve_selectors(["decide+"])
    selected_set = set(selected)
    assert len(selected) == 3, f"Expected 3 nodes, got {len(selected)}: {selected}"

    # Filter graph structure
    from abt.cli import _filter_graph_structure
    filtered = _filter_graph_structure(graph_structure, selected_set)

    assert len(filtered.all_nodes) == 3
    assert "require_any__fast/fallback_a" not in filtered.all_nodes
    assert "require_any__fast/fallback_b" not in filtered.all_nodes

    # Execute with mock LLM
    canned = [
        {"in_stock": True, "quantity_on_hand": 75, "location": "WH-A"},
        {"predicted_demand": 120, "confidence": 0.85, "trend": "increasing"},
        {"in_stock": True, "quantity_on_hand": 75, "location": "WH-A"},
        {"predicted_demand": 120, "confidence": 0.85, "trend": "increasing"},
        {"shortfall": 45, "safety_stock": 20, "gap": 65},
        {"stock_status": "LOW", "items_below_threshold": ["SKU-12345"],
         "total_order_cost": 650.0, "priority": "medium"},
    ]
    call_count = [0]

    def factory():
        mock = MagicMock()
        def mock_create(*, model, messages, temperature, max_tokens):
            idx = min(call_count[0], len(canned) - 1)
            call_count[0] += 1
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = json.dumps(canned[idx])
            resp.usage.prompt_tokens = 100
            resp.usage.completion_tokens = 50
            return resp
        mock.chat.completions.create = mock_create
        return mock

    db = DatabaseManager(":memory:")
    db.connect()
    tool_table = ToolTable(sources, db)
    tool_table.build_all()

    executor = GraphExecutor(filtered, db, tool_table, llm_factory=factory)
    result = executor.execute({"product_id": "SKU-12345"})

    node_outputs = result.get("node_outputs", {})
    print(f"Executed {len(node_outputs)} nodes: {list(node_outputs.keys())}")

    assert len(node_outputs) == 3, f"Expected 3 node outputs, got {len(node_outputs)}"
    assert "decide" in node_outputs
    assert "require_all/check_stock" in node_outputs
    assert "require_all/check_demand" in node_outputs
    assert "require_any__fast/fallback_a" not in node_outputs
    assert "require_any__fast/fallback_b" not in node_outputs

    # Verify topological order is respected
    order = executor._topological_order()
    decide_idx = order.index("decide")
    stock_idx = order.index("require_all/check_stock")
    demand_idx = order.index("require_all/check_demand")
    assert stock_idx < decide_idx
    assert demand_idx < decide_idx

    db.close()
    print("OK: filtered execution produces correct results")


def test_source_selector():
    """Select nodes referencing a specific source."""
    ns = get_selector()
    result = ns.resolve_selectors(["source:warehouse_api"])
    assert "require_all/check_stock" in result
    assert "require_all/check_demand" not in result  # uses demand_forecast_mcp


if __name__ == "__main__":
    test_exact_name()
    test_leaf_name()
    test_ancestors()
    test_descendants()
    test_ancestors_and_descendants()
    test_tag()
    test_path()
    test_glob()
    test_glob_wildcard()
    test_union()
    test_exclusion()
    test_exact_name_not_found()
    test_empty_selectors()
    test_topological_order_preserved()
    test_source_selector()
    test_filtered_execution()
    print("\n=== All selector tests PASSED ===")
