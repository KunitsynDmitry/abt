"""Test blueprint subgraph resolution via _folder_name references."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from abt.compiler.folder_parser import FolderParser
from abt.compiler.jinja_env import AbtJinjaEnv
from abt.compiler.prompt_compiler import PromptCompiler
from abt.models.prompt import ParsedPrompt, PromptConfig
from abt.models.graph import RoutingType


def _make_dummy_parsed(name: str, file_path: Path) -> ParsedPrompt:
    return ParsedPrompt(
        name=name,
        file_path=file_path,
        relative_path=file_path.relative_to(file_path.parent.parent),
        config=PromptConfig(),
        system_prompt="test",
        cte_blocks=[],
        output_columns=[],
    )


def test_blueprint_resolution():
    """Verify that _folder_name resolves to blueprints/<name>/."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Create prompts directory structure with a _reference
        prompts_dir = root / "prompts"
        prompts_dir.mkdir(parents=True)

        main_flow = prompts_dir / "main_flow"
        main_flow.mkdir()
        (main_flow / "step1.prompt").write_text("test")
        (main_flow / "step2.prompt").write_text("test")

        # Create _approval reference folder (empty — just a marker)
        approval_ref = main_flow / "_approval"
        approval_ref.mkdir()

        # Create blueprints directory with actual content
        blueprints_dir = root / "blueprints"
        approval_bp = blueprints_dir / "approval"
        approval_bp.mkdir(parents=True)
        (approval_bp / "request.prompt").write_text("test")
        (approval_bp / "handle.prompt").write_text("test")

        # Build prompt_files dict with all prompts
        jinja = AbtJinjaEnv(strict=False)
        compiler = PromptCompiler(jinja)

        # Compile main prompts
        main_parsed = compiler.compile_all(
            [main_flow / "step1.prompt", main_flow / "step2.prompt"],
            prompts_dir,
        )

        # Compile blueprint prompts (as the CLI would)
        bp_parsed = {}
        for bp_file in blueprints_dir.rglob("*.prompt"):
            bp_compiled = compiler.compile_file(bp_file, blueprints_dir)
            bp_key = f"blueprints/{bp_file.relative_to(blueprints_dir).with_suffix('')}".replace("\\", "/")
            bp_parsed[bp_key] = bp_compiled

        all_parsed = {**main_parsed, **bp_parsed}

        # Build tree WITH blueprints_root
        tree = FolderParser.build_tree(
            prompts_dir, all_parsed,
            blueprints_root=blueprints_dir,
        )

        # The tree should have main_flow which contains:
        # - step1 (node)
        # - step2 (node)
        # - _approval subgraph → resolved from blueprints/approval/
        assert len(tree.subgraphs) == 1
        main_flow_sg = tree.subgraphs[0]
        assert main_flow_sg.folder_name == "main_flow"

        # Check _approval was resolved
        approval_sgs = [sg for sg in main_flow_sg.subgraphs
                        if sg.folder_name == "_approval"]
        assert len(approval_sgs) == 1
        approval_sg = approval_sgs[0]

        # The blueprint should have its nodes grafted in
        assert len(approval_sg.nodes) >= 1
        node_names = [n.split("/")[-1] for n in approval_sg.nodes]
        assert "request" in node_names or "handle" in node_names

        # Verify the blueprint key format
        for node in approval_sg.nodes:
            assert "blueprints/approval" in node


def test_blueprint_not_resolved_without_root():
    """Without blueprints_root, _folders are treated as regular empty dirs."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        prompts_dir = root / "prompts"
        prompts_dir.mkdir()
        ref_dir = prompts_dir / "_approval"
        ref_dir.mkdir()

        jinja = AbtJinjaEnv(strict=False)
        compiler = PromptCompiler(jinja)
        parsed = compiler.compile_all([], prompts_dir)

        # Build WITHOUT blueprints_root (default)
        tree = FolderParser.build_tree(prompts_dir, parsed)

        # _approval should still exist as a subgraph, just empty
        assert len(tree.subgraphs) == 1
        assert tree.subgraphs[0].folder_name == "_approval"
        assert tree.subgraphs[0].nodes == []
        assert tree.subgraphs[0].subgraphs == []
