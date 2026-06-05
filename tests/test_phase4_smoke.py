"""Smoke test for Phase 4: folder parsing and graph compilation."""
from pathlib import Path
from abt.compiler.jinja_env import AbtJinjaEnv
from abt.compiler.prompt_compiler import PromptCompiler
from abt.compiler.folder_parser import FolderParser
from abt.compiler.graph_builder import GraphBuilder
import tempfile


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        (base / "prompts" / "require_all").mkdir(parents=True)
        (base / "prompts" / "require_any__fast").mkdir(parents=True)

        (base / "prompts" / "decide.prompt").write_text("""\
{{ config(temperature=0.3, output_schema='decision') }}
You are a decision agent.
Context: {{ ref('check_stock') }}, {{ ref('check_demand') }}
SELECT decision, confidence FROM analysis
""")

        (base / "prompts" / "require_all" / "check_stock.prompt").write_text("""\
{{ config(temperature=0.1) }}
Check inventory.
WITH fetch AS (SELECT * FROM {{ source('warehouse', 'stock') }})
SELECT in_stock, quantity FROM fetch
""")

        (base / "prompts" / "require_all" / "check_demand.prompt").write_text("""\
{{ config(temperature=0.1) }}
Forecast demand.
WITH fetch AS (SELECT * FROM {{ source('demand', 'forecast') }})
SELECT predicted, confidence FROM fetch
""")

        (base / "prompts" / "require_any__fast" / "fallback_a.prompt").write_text("""\
{{ config(temperature=0.2) }}
Try vendor A.
SELECT result FROM attempt
""")

        (base / "prompts" / "require_any__fast" / "fallback_b.prompt").write_text("""\
{{ config(temperature=0.2) }}
Try vendor B.
SELECT result FROM attempt
""")

        jinja_env = AbtJinjaEnv(strict=False)
        compiler = PromptCompiler(jinja_env)
        parsed = {}
        for fp in sorted(base.glob("prompts/**/*.prompt")):
            p = compiler.compile_file(fp, base / "prompts")
            key = str(p.relative_path.with_suffix("")).replace("\\", "/")
            parsed[key] = p

        print("Parsed prompts:")
        for k, v in sorted(parsed.items()):
            deps = v.raw_dependencies
            srcs = [f"{s}.{t}" for s, t in v.raw_source_refs]
            print(f"  {k}: deps={deps}, sources={srcs}")

        tree = FolderParser.build_tree(base / "prompts", parsed)
        print(f"\nFolder tree root: routing={tree.routing.value}")
        for sg in tree.subgraphs:
            print(f"  {sg.name}: routing={sg.routing.value}, nodes={sg.nodes}, metadata={sg.metadata}")

        gb = GraphBuilder(parsed_prompts=parsed, folder_tree=tree, project_name="test")
        structure = gb.build_structure()
        print(f"\nAll nodes: {list(structure.all_nodes.keys())}")
        for name, deps in structure.dependency_graph.items():
            print(f"  Deps: {name} -> {deps}")

        print("OK: Phase 4 smoke test passed")


if __name__ == "__main__":
    main()
