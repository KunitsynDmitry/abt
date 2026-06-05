"""Test the local_only example: compiles, runs with mock LLM, and passes tests."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from abt.project import ProjectLoader
from abt.compiler.schema_parser import SchemaParser
from abt.compiler.source_parser import SourceParser
from abt.compiler.jinja_env import AbtJinjaEnv
from abt.compiler.prompt_compiler import PromptCompiler
from abt.compiler.folder_parser import FolderParser
from abt.compiler.graph_builder import GraphBuilder
from abt.compiler.manifest_generator import generate_manifest
from abt.runtime.db import DatabaseManager
from abt.runtime.tool_table import ToolTable
from abt.runtime.executor import GraphExecutor
from abt.runtime.test_runner import TestRunner


EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "local_only"


def test_local_only_compiles():
    """The local_only example should compile without errors."""
    loader = ProjectLoader(EXAMPLE_DIR)
    config = loader.load()
    assert config.name == "local_only"

    schemas = SchemaParser(loader).parse_all()
    assert "greeting_output" in schemas

    sources = SourceParser(loader).parse_all()
    assert "local_utils" in sources

    jinja_env = AbtJinjaEnv(
        schema_registry=schemas,
        source_registry=sources,
        project_vars=config.vars,
        strict=False,
    )
    compiler = PromptCompiler(jinja_env, defaults=config.models.get("default"))
    prompt_files = loader.list_prompt_files()
    prompt_root = EXAMPLE_DIR / config.paths.prompt_paths[0]
    parsed = compiler.compile_all(prompt_files, prompt_root)

    assert len(parsed) == 1
    greet = parsed["greet"]
    assert greet.output_columns == ["greeting", "name", "length"]
    assert len(greet.cte_blocks) == 2
    assert greet.cte_blocks[0].cte_type == "tool"
    assert greet.cte_blocks[1].cte_type == "llm"

    folder_tree = FolderParser.build_tree(prompt_root, parsed)
    gb = GraphBuilder(
        parsed_prompts=parsed, folder_tree=folder_tree,
        schema_registry=schemas, source_registry=sources,
        project_name=config.name,
    )
    graph_structure = gb.build_structure()
    manifest = generate_manifest(graph_structure, config, {})

    assert "greet" in manifest["nodes"]
    assert manifest["metadata"]["project_name"] == "local_only"


def test_local_only_runs_with_mock_llm():
    """The local_only example should run with a mock LLM."""
    loader = ProjectLoader(EXAMPLE_DIR)
    config = loader.load()

    schemas = SchemaParser(loader).parse_all()
    sources = SourceParser(loader).parse_all()

    jinja_env = AbtJinjaEnv(
        schema_registry=schemas, source_registry=sources,
        project_vars=config.vars, strict=False,
    )
    compiler = PromptCompiler(jinja_env, defaults=config.models.get("default"))
    prompt_files = loader.list_prompt_files()
    prompt_root = EXAMPLE_DIR / config.paths.prompt_paths[0]
    parsed = compiler.compile_all(prompt_files, prompt_root)

    folder_tree = FolderParser.build_tree(prompt_root, parsed)
    gb = GraphBuilder(
        parsed_prompts=parsed, folder_tree=folder_tree,
        schema_registry=schemas, source_registry=sources,
        project_name=config.name,
    )
    graph_structure = gb.build_structure()

    db = DatabaseManager(":memory:")
    db.connect()

    tool_table = ToolTable(sources, db)
    tool_table.build_all()

    # Mock LLM: returns a valid greeting_output
    mock = MagicMock()
    canned_json = json.dumps({
        "greeting": "Hello ABT Developer!",
        "name": "ABT Developer",
        "length": 20,
    })

    def _mock_create(*, model, messages, temperature, max_tokens, stream=False, **kwargs):
        if stream:
            # Return an iterator of chunks for streaming mode
            class Delta:
                def __init__(self, content):
                    self.content = content

            class Chunk:
                def __init__(self, content):
                    self.choices = [MagicMock()]
                    self.choices[0].delta = Delta(content)

            return iter([Chunk(canned_json)])
        else:
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = canned_json
            resp.usage.prompt_tokens = 50
            resp.usage.completion_tokens = 15
            return resp

    mock.chat.completions.create.side_effect = _mock_create

    executor = GraphExecutor(
        graph_structure, db, tool_table,
        llm_factory=lambda: mock,
    )
    result = executor.execute({})
    node_outputs = result.get("node_outputs", {})

    greet_output = node_outputs.get("greet", {})
    assert greet_output.get("greeting") == "Hello ABT Developer!"
    assert greet_output.get("name") == "ABT Developer"
    assert greet_output.get("length") == 20

    db.close()


def test_local_only_tests_pass():
    """The local_only .test.yml assertions should pass."""
    loader = ProjectLoader(EXAMPLE_DIR)
    config = loader.load()

    schemas = SchemaParser(loader).parse_all()
    sources = SourceParser(loader).parse_all()

    jinja_env = AbtJinjaEnv(
        schema_registry=schemas, source_registry=sources,
        project_vars=config.vars, strict=False,
    )
    compiler = PromptCompiler(jinja_env, defaults=config.models.get("default"))
    prompt_files = loader.list_prompt_files()
    prompt_root = EXAMPLE_DIR / config.paths.prompt_paths[0]
    parsed = compiler.compile_all(prompt_files, prompt_root)

    folder_tree = FolderParser.build_tree(prompt_root, parsed)
    gb = GraphBuilder(
        parsed_prompts=parsed, folder_tree=folder_tree,
        schema_registry=schemas, source_registry=sources,
        project_name=config.name,
    )
    graph_structure = gb.build_structure()

    # Discover and evaluate tests against known output
    test_runner = TestRunner(prompt_root)
    tests_by_node = test_runner.discover()

    assert test_runner.test_count == 4

    node_output = {
        "greeting": "Hello ABT Developer!",
        "name": "ABT Developer",
        "length": 20,
    }
    results = test_runner.evaluate("greet", node_output)
    assert len(results) == 4
    assert all(r.passed for r in results)
