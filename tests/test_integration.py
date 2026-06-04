"""End-to-end integration test: compile and run the example project."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Ensure abt is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

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


def _make_mock_llm_factory():
    """Build a mock OpenAI client that returns canned JSON for each LLM CTE call.

    With LangGraph routing the execution order is:
      1. check_stock (1 CTE) + check_demand (1 CTE) — parallel
      2. fallback_a (1 CTE) → fallback_b (1 CTE) — sequential
      3. decide (4 CTEs)

    Total: 8 LLM calls.
    """
    canned = [
        # 0: check_stock
        {"in_stock": True, "quantity_on_hand": 75, "location": "WH-A"},
        # 1: check_demand
        {"predicted_demand": 120, "confidence": 0.85, "trend": "increasing"},
        # 2: fallback_a
        {"vendor": "VendorA", "available": True, "price_per_unit": 12.50},
        # 3: fallback_b
        {"vendor": "VendorB", "available": False, "price_per_unit": 15.00},
        # 4-7: decide (stock_analysis, demand_analysis, gap_analysis, order_calculation)
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
    return factory


EXAMPLE_PROJECT = Path(__file__).parent.parent / "example_project"


def test_full_pipeline():
    """Compile and execute the example project end-to-end."""
    project_root = EXAMPLE_PROJECT
    assert (project_root / "abt_project.yml").exists(), "Example project must exist"

    # ── Load project ──────────────────────────────────────────
    loader = ProjectLoader(project_root)
    config = loader.load()
    assert config.name == "inventory_agent"
    assert config.vars["company_name"] == "ACME Corp"

    # ── Parse schemas ─────────────────────────────────────────
    schema_parser = SchemaParser(loader)
    schemas = schema_parser.parse_all()
    assert "stock_check" in schemas
    assert "demand_forecast" in schemas
    assert "inventory_analysis" in schemas

    # Verify Pydantic models work
    StockCheck = schemas["stock_check"]
    obj = StockCheck(in_stock=True, quantity_on_hand=42, location="WH-A")
    assert obj.in_stock is True
    assert obj.quantity_on_hand == 42

    InventoryAnalysis = schemas["inventory_analysis"]
    obj2 = InventoryAnalysis(
        stock_status="OK",
        items_below_threshold=[],
        total_order_cost=0.0,
        priority="low",
    )
    assert obj2.stock_status == "OK"

    # ── Parse sources ─────────────────────────────────────────
    source_parser = SourceParser(loader)
    sources = source_parser.parse_all()
    assert "warehouse_api" in sources
    assert "demand_forecast_mcp" in sources

    # ── Compile prompts ──────────────────────────────────────
    jinja_env = AbtJinjaEnv(
        schema_registry=schemas,
        source_registry=sources,
        project_vars=config.vars,
        strict=False,
    )
    prompt_compiler = PromptCompiler(jinja_env, defaults=config.models.get("default"))
    prompt_files = loader.list_prompt_files()
    assert len(prompt_files) == 5, f"Expected 5 prompt files, got {len(prompt_files)}"

    parsed = prompt_compiler.compile_all(prompt_files, project_root / "prompts")
    assert len(parsed) == 5

    # Verify the decide prompt has dependencies
    decide_key = None
    for k, v in parsed.items():
        if "decide" in k:
            decide_key = k
            break
    assert decide_key is not None
    decide = parsed[decide_key]
    assert len(decide.raw_dependencies) >= 2, "decide should depend on check_stock and check_demand"

    # ── Build folder tree ────────────────────────────────────
    folder_tree = FolderParser.build_tree(project_root / "prompts", parsed)
    assert len(folder_tree.subgraphs) == 2
    require_all = folder_tree.subgraphs[0]
    require_any = folder_tree.subgraphs[1]
    assert require_all.routing.value == "require_all"
    assert require_any.routing.value == "require_any"
    assert require_any.metadata == {"tag": "fast"}
    assert len(require_all.nodes) == 2
    assert len(require_any.nodes) == 2

    # ── Build graph structure ────────────────────────────────
    gb = GraphBuilder(
        parsed_prompts=parsed,
        folder_tree=folder_tree,
        schema_registry=schemas,
        source_registry=sources,
        project_name=config.name,
    )
    graph_structure = gb.build_structure()
    assert len(graph_structure.all_nodes) == 5
    assert len(graph_structure.dependency_graph) == 5

    # ── Generate Python code ─────────────────────────────────
    target = project_root / "target"
    code = gb.generate_python_code(graph_structure, target / "compiled_graph.py")
    assert "def build_graph" in code
    assert "AbtState" in code
    compile(code, "compiled_graph.py", "exec")  # Must be valid Python

    # ── Execute with runtime ─────────────────────────────────
    db = DatabaseManager(":memory:")
    db.connect()

    tool_table = ToolTable(sources, db)
    tool_table.build_all()

    mock_llm_factory = _make_mock_llm_factory()

    executor = GraphExecutor(graph_structure, db, tool_table, llm_factory=mock_llm_factory)
    result = executor.execute({"product_id": "SKU-12345"})

    # Verify all 5 nodes executed
    node_outputs = result.get("node_outputs", {})
    print(f"Executed {len(node_outputs)} nodes:")
    for name, output in node_outputs.items():
        print(f"  {name}: {output}")

    assert len(node_outputs) == 5, f"Expected 5 node outputs, got {len(node_outputs)}"

    # Verify routing: the compiled graph has a LangGraph StateGraph inside
    assert "_run_id" in result

    # Verify topological order is valid (decide must come after its dependencies)
    order = executor._topological_order()
    decide_idx = order.index(decide_key)
    require_all_nodes = [n for n in order if "require_all" in n]
    for dep_node in require_all_nodes:
        dep_idx = order.index(dep_node)
        assert dep_idx < decide_idx, f"{dep_node} must execute before decide (got order: {order})"

    # Verify DB traces
    traces = db.get_run_traces(result["_run_id"])
    print(f"\nLLM traces: {len(traces)}")

    exec_rows = db.conn.execute(
        "SELECT node_name, status FROM node_executions ORDER BY started_at"
    ).fetchall()
    print("Node executions:")
    for row in exec_rows:
        print(f"  {row['node_name']}: {row['status']}")
    assert len(exec_rows) == 5

    db.close()

    print("\n=== Integration test PASSED ===")


if __name__ == "__main__":
    test_full_pipeline()
