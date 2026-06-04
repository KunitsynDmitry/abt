"""Verify generated Python code is valid and runnable."""
import subprocess
import sys
import tempfile
from pathlib import Path

from abt.compiler.jinja_env import AbtJinjaEnv
from abt.compiler.prompt_compiler import PromptCompiler
from abt.compiler.folder_parser import FolderParser
from abt.compiler.graph_builder import GraphBuilder


def test_generated_python_runs():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        (base / "prompts").mkdir()

        (base / "prompts" / "hello.prompt").write_text(
            "{{ config(temperature=0.5) }}\n"
            "You are a helpful agent.\n"
            "SELECT result FROM answer\n"
        )

        jinja_env = AbtJinjaEnv(strict=False)
        compiler = PromptCompiler(jinja_env)
        parsed = {}
        for fp in base.glob("prompts/**/*.prompt"):
            p = compiler.compile_file(fp, base / "prompts")
            parsed[str(p.relative_path.with_suffix("")).replace("\\", "/")] = p

        tree = FolderParser.build_tree(base / "prompts", parsed)
        gb = GraphBuilder(parsed_prompts=parsed, folder_tree=tree)
        structure = gb.build_structure()
        target = Path(tmpdir) / "target"
        code = gb.generate_python_code(structure, target / "compiled_graph.py")

        # Verify syntax
        compile(code, "compiled_graph.py", "exec")
        print("OK: Generated code is valid Python syntax")

        # Run it
        result = subprocess.run(
            [sys.executable, str(target / "compiled_graph.py")],
            capture_output=True, text=True, cwd=str(tmpdir),
        )
        print("stdout:", result.stdout)
        if result.stderr:
            print("stderr:", result.stderr[:400])
        print("exit code:", result.returncode)
        assert result.returncode == 0


if __name__ == "__main__":
    test_generated_python_runs()
