"""Test nested subgraph compilation: folders as LangGraph subgraphs.

Structure tested:
  require_all/               # AND gate (becomes compiled subgraph)
    sequential_child/         # SEQUENTIAL (flattened inline)
      step_a.prompt
      step_b.prompt
    check_stock.prompt        # Leaf node

After flattening:
  - require_all becomes a "parallel" block with children:
      - node: step_a, node: step_b, node: check_stock
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from abt.compiler.jinja_env import AbtJinjaEnv
from abt.compiler.prompt_compiler import PromptCompiler
from abt.compiler.folder_parser import FolderParser
from abt.compiler.graph_builder import GraphBuilder
from abt.runtime.db import DatabaseManager
from abt.runtime.tool_table import ToolTable
from abt.runtime.executor import GraphExecutor, RoutingType


def _make_mock_llm_factory():
    canned = [
        {"result": "step_a_ok"},
        {"result": "step_b_ok"},
        {"in_stock": True, "quantity": 42},
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
            resp.usage.prompt_tokens = 10
            resp.usage.completion_tokens = 5
            return resp
        mock.chat.completions.create = mock_create
        return mock
    return factory


def build_nested_project(base: Path):
    """Create a nested folder structure and compile it."""
    prompts_dir = base / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "require_all").mkdir()
    (prompts_dir / "require_all" / "sequential_child").mkdir()

    # Leaf in require_all
    (prompts_dir / "require_all" / "check_stock.prompt").write_text(
        "Check warehouse stock.\n"
        "SELECT in_stock, quantity FROM stock\n"
    )

    # Sequential child leaves
    (prompts_dir / "require_all" / "sequential_child" / "step_a.prompt").write_text(
        "Step A.\nSELECT result FROM step_a_query\n"
    )
    (prompts_dir / "require_all" / "sequential_child" / "step_b.prompt").write_text(
        "Step B.\nSELECT result FROM step_b_query\n"
    )

    jinja_env = AbtJinjaEnv(strict=False)
    compiler = PromptCompiler(jinja_env)
    parsed = {}
    for fp in sorted(base.glob("prompts/**/*.prompt")):
        p = compiler.compile_file(fp, prompts_dir)
        key = str(p.relative_path.with_suffix("")).replace("\\", "/")
        parsed[key] = p

    tree = FolderParser.build_tree(prompts_dir, parsed)
    return parsed, tree


def test_flatten_tree_nested():
    """Verify _flatten_tree produces nested blocks for nested folders."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        parsed, tree = build_nested_project(base)

        gb = GraphBuilder(parsed_prompts=parsed, folder_tree=tree, project_name="test")
        from abt.runtime.executor import GraphExecutor as GE

        executor = GE.__new__(GE)
        executor.structure = gb.build_structure()

        blocks = executor._flatten_tree(executor.structure.root)

        # Root has one block: the require_all parallel block
        assert len(blocks) == 1, f"Expected 1 top-level block, got {len(blocks)}"
        block = blocks[0]

        assert block["type"] == "parallel"
        assert block["name"] == "prompts.require_all"
        assert "children" in block
        children = block["children"]
        assert len(children) == 3  # step_a, step_b (from sequential_child) + check_stock

        # Children are in order: sequential_child nodes first, then check_stock leaf
        child_types = [c["type"] for c in children]
        assert child_types == ["node", "node", "node"]

        child_names = [c["name"] for c in children]
        assert "require_all/sequential_child/step_a" in child_names
        assert "require_all/sequential_child/step_b" in child_names
        assert "require_all/check_stock" in child_names

        # SEQUENTIAL child is flattened inline, so step_a before step_b
        idx_a = child_names.index("require_all/sequential_child/step_a")
        idx_b = child_names.index("require_all/sequential_child/step_b")
        assert idx_a < idx_b, "step_a must come before step_b in sequential order"

        print("OK: _flatten_tree produces nested blocks")


def test_nested_graph_execution():
    """Verify a nested subgraph compiles and executes with a mock LLM."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        parsed, tree = build_nested_project(base)

        gb = GraphBuilder(parsed_prompts=parsed, folder_tree=tree, project_name="test")
        structure = gb.build_structure()

        db = DatabaseManager(":memory:")
        db.connect()

        tool_table = ToolTable({}, db)
        tool_table.build_all()

        executor = GraphExecutor(structure, db, tool_table,
                                 llm_factory=_make_mock_llm_factory())
        result = executor.execute({"product_id": "SKU-999"})

        node_outputs = result.get("node_outputs", {})
        print(f"Executed {len(node_outputs)} nodes:")
        for name, output in node_outputs.items():
            print(f"  {name}: {output}")

        assert len(node_outputs) == 3, f"Expected 3 node outputs, got {len(node_outputs)}"
        assert "require_all/sequential_child/step_a" in node_outputs
        assert "require_all/sequential_child/step_b" in node_outputs
        assert "require_all/check_stock" in node_outputs

        db.close()
        print("OK: nested graph execution")


def test_nested_generated_code():
    """Verify generated Python code for nested structure is valid and runs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        parsed, tree = build_nested_project(base)

        gb = GraphBuilder(parsed_prompts=parsed, folder_tree=tree, project_name="test")
        structure = gb.build_structure()

        target = base / "target"
        code = gb.generate_python_code(structure, target / "compiled_graph.py")

        # Must be valid Python
        compile(code, "compiled_graph.py", "exec")
        assert "def _flatten_blocks" in code
        assert 'children' in code  # Nested block structure
        assert "def _build_graph_recursive" in code

        print("OK: generated code is valid Python with recursive subgraph support")


def test_deeply_nested():
    """Verify three-level nesting: require_all > require_any > sequential > leaves."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        prompts_dir = base / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "require_all").mkdir()
        (prompts_dir / "require_all" / "require_any__fallback").mkdir()
        (prompts_dir / "require_all" / "require_any__fallback" / "sequential").mkdir()

        (prompts_dir / "require_all" / "require_any__fallback" / "sequential" / "step_a.prompt").write_text(
            "Step A.\nSELECT result FROM a\n"
        )
        (prompts_dir / "require_all" / "require_any__fallback" / "sequential" / "step_b.prompt").write_text(
            "Step B.\nSELECT result FROM b\n"
        )
        (prompts_dir / "require_all" / "main.prompt").write_text(
            "Main.\nSELECT result FROM main\n"
        )

        jinja_env = AbtJinjaEnv(strict=False)
        compiler = PromptCompiler(jinja_env)
        parsed = {}
        for fp in sorted(base.glob("prompts/**/*.prompt")):
            p = compiler.compile_file(fp, prompts_dir)
            key = str(p.relative_path.with_suffix("")).replace("\\", "/")
            parsed[key] = p

        tree = FolderParser.build_tree(prompts_dir, parsed)

        gb = GraphBuilder(parsed_prompts=parsed, folder_tree=tree, project_name="deep_test")
        structure = gb.build_structure()

        # Flatten and verify structure
        from abt.runtime.executor import GraphExecutor as GE
        executor = GE.__new__(GE)
        executor.structure = structure

        blocks = executor._flatten_tree(executor.structure.root)
        assert len(blocks) == 1
        block = blocks[0]

        assert block["type"] == "parallel"  # require_all
        assert block["name"] == "prompts.require_all"
        assert len(block["children"]) == 2  # require_any block + main leaf

        # First child is the require_any block
        any_block = block["children"][0]
        assert any_block["type"] == "any"
        assert any_block["name"] == "prompts.require_all.require_any__fallback"
        assert len(any_block["children"]) == 2  # step_a, step_b

        # Second child is main.prompt leaf
        leaf = block["children"][1]
        assert leaf["type"] == "node"
        assert leaf["name"] == "require_all/main"

        print("OK: deeply nested (3-level) block structure is correct")

        # Compile and run with mock LLM
        db = DatabaseManager(":memory:")
        db.connect()
        tool_table = ToolTable({}, db)
        tool_table.build_all()

        executor = GraphExecutor(structure, db, tool_table,
                                 llm_factory=_make_mock_llm_factory())
        result = executor.execute({})

        node_outputs = result.get("node_outputs", {})
        assert len(node_outputs) == 3
        print("OK: deeply nested execution")


if __name__ == "__main__":
    test_flatten_tree_nested()
    test_nested_graph_execution()
    test_nested_generated_code()
    test_deeply_nested()
    print("\n=== All nested subgraph tests PASSED ===")
