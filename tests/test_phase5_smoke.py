"""Smoke test for Phase 5: runtime execution."""
from pathlib import Path
from abt.compiler.jinja_env import AbtJinjaEnv
from abt.compiler.prompt_compiler import PromptCompiler
from abt.compiler.folder_parser import FolderParser
from abt.compiler.graph_builder import GraphBuilder
from abt.runtime.db import DatabaseManager
from abt.runtime.tool_table import ToolTable
from abt.runtime.executor import GraphExecutor
from abt.models.source import SourceDefinition, SourceTable
import tempfile


def test_executor_with_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        (base / "prompts").mkdir()

        (base / "prompts" / "hello.prompt").write_text(
            "{{ config(temperature=0.5) }}\n"
            "You are a helpful agent.\n"
            "SELECT result FROM answer\n"
        )
        (base / "prompts" / "step2.prompt").write_text(
            "{{ config(temperature=0.3) }}\n"
            "Process the previous result: {{ ref('hello') }}\n"
            "SELECT summary FROM analysis\n"
        )

        jinja_env = AbtJinjaEnv(strict=False)
        compiler = PromptCompiler(jinja_env)
        parsed = {}
        for fp in sorted(base.glob("prompts/**/*.prompt")):
            p = compiler.compile_file(fp, base / "prompts")
            key = str(p.relative_path.with_suffix("")).replace("\\", "/")
            parsed[key] = p

        tree = FolderParser.build_tree(base / "prompts", parsed)
        gb = GraphBuilder(parsed_prompts=parsed, folder_tree=tree, project_name="test_runtime")
        structure = gb.build_structure()

        print("Nodes:", list(structure.all_nodes.keys()))
        print("Deps:", {k: list(v) for k, v in structure.dependency_graph.items()})

        # Set up runtime
        db = DatabaseManager(":memory:")
        db.connect()

        # Create a stub tool
        from abt.models.source import SourceDefinition, SourceTable, ToolType
        sources = {
            "test_api": SourceDefinition(
                name="test_api", type=ToolType.PYTHON_FUNCTION,
                tables=[SourceTable(name="lookup", description="Test lookup")]
            )
        }
        tool_table = ToolTable(sources, db)
        tool_table.build_all()

        # Execute
        executor = GraphExecutor(structure, db, tool_table)
        result = executor.execute_sequential({"user_input": "hello world"})

        print(f"\nResult keys: {list(result.keys())}")
        print(f"Node outputs: {list(result.get('node_outputs', {}).keys())}")

        # Verify DB traces
        run_row = db.conn.execute("SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        print(f"\nDB Run: id={run_row['run_id']}, status={run_row['status']}, project={run_row['project_name']}")

        exec_rows = db.conn.execute("SELECT * FROM node_executions").fetchall()
        print(f"Node executions: {len(exec_rows)}")
        for row in exec_rows:
            print(f"  {row['node_name']}: status={row['status']}, retries={row['retry_count']}")

        # Verify topological order (step2 depends on hello, so hello must be first)
        print("\nTopological order:", executor._topological_order())
        assert executor._topological_order()[-1] == "step2", "step2 should be last (depends on hello)"

        executor.print_traces()
        db.close()
        print("\nOK: Phase 5 smoke test passed")


if __name__ == "__main__":
    test_executor_with_db()
