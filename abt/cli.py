"""CLI — Click-based command-line interface for abt."""

import json
import sys
from pathlib import Path

import click

from . import __version__


@click.group()
@click.version_option(__version__, prog_name="abt")
def cli():
    """abt — Agent Building Tool.

    Compile declarative .prompt files into LangGraph agents.
    Inspired by dbt's declarative elegance.
    """


@cli.command()
@click.argument("project_name")
@click.option("--directory", "-d", default=".", help="Parent directory for the new project")
def init(project_name: str, directory: str):
    """Create a new abt project skeleton."""
    project_root = Path(directory).resolve() / project_name

    if project_root.exists():
        click.echo(f"Error: directory '{project_root}' already exists.")
        raise SystemExit(1)

    _create_project_skeleton(project_root, project_name)
    click.echo(f"Created abt project '{project_name}' at {project_root}")
    click.echo(f"  cd {project_name}")
    click.echo(f"  abt compile")


@cli.command()
@click.option("--select", "-s", multiple=True, help="Select specific prompt files")
@click.option("--full-refresh", is_flag=True, help="Ignore cache, recompile from scratch")
def compile(select: tuple, full_refresh: bool):
    """Compile the abt project into a LangGraph graph.

    Produces target/compiled_graph.py — a standalone runnable Python script.
    """
    project_root = _find_project_root()
    click.echo(f"Compiling project at {project_root}...")

    from .project import ProjectLoader
    from .compiler.schema_parser import SchemaParser
    from .compiler.source_parser import SourceParser
    from .compiler.jinja_env import AbtJinjaEnv
    from .compiler.prompt_compiler import PromptCompiler
    from .compiler.folder_parser import FolderParser
    from .compiler.graph_builder import GraphBuilder

    loader = ProjectLoader(project_root)
    config = loader.load()

    click.echo(f"  Project: {config.name} v{config.version}")

    # Parse schemas
    schema_parser = SchemaParser(loader)
    schemas = schema_parser.parse_all()
    click.echo(f"  Schemas: {len(schemas)} models")

    # Parse sources
    source_parser = SourceParser(loader)
    sources = source_parser.parse_all()
    click.echo(f"  Sources: {len(sources)}")

    # Compile prompts
    jinja_env = AbtJinjaEnv(
        schema_registry=schemas,
        source_registry=sources,
        project_vars=config.vars,
        macro_paths=loader.list_macro_files(),
        strict=False,
    )
    prompt_compiler = PromptCompiler(jinja_env, defaults=config.models.get("default"))
    prompt_files = loader.list_prompt_files()
    prompt_root = project_root / config.paths.prompt_paths[0]
    parsed = prompt_compiler.compile_all(prompt_files, prompt_root)
    click.echo(f"  Prompts: {len(parsed)} files")

    # Build folder tree
    folder_tree = FolderParser.build_tree(prompt_root, parsed)
    subgraph_count = _count_subgraphs(folder_tree)
    click.echo(f"  Folders: {subgraph_count} subgraphs")

    # Build graph and generate code
    gb = GraphBuilder(
        parsed_prompts=parsed,
        folder_tree=folder_tree,
        schema_registry=schemas,
        source_registry=sources,
        project_name=config.name,
    )
    graph_structure = gb.build_structure()
    target = loader.get_target_dir()
    code = gb.generate_python_code(graph_structure, target / "compiled_graph.py")

    click.echo(f"  Generated: target/compiled_graph.py ({len(code)} bytes)")
    click.echo("Compilation complete.")


@cli.command()
@click.option("--thread-id", help="Execution thread ID (for resuming)")
@click.option("--input", "-i", "input_file", type=click.Path(exists=True), help="JSON input file")
@click.option("--db-path", default="abt_state.db", help="SQLite database path")
@click.option("--stream/--no-stream", default=True, help="Stream output to console")
@click.option("--verbose", "-v", is_flag=True, help="Show LLM traces")
def run(thread_id: str, input_file: str, db_path: str, stream: bool, verbose: bool):
    """Execute the compiled graph with SQLite persistence."""
    project_root = _find_project_root()
    click.echo(f"Running project at {project_root}...")

    from .project import ProjectLoader
    from .compiler.schema_parser import SchemaParser
    from .compiler.source_parser import SourceParser
    from .compiler.jinja_env import AbtJinjaEnv
    from .compiler.prompt_compiler import PromptCompiler
    from .compiler.folder_parser import FolderParser
    from .compiler.graph_builder import GraphBuilder
    from .runtime.db import DatabaseManager
    from .runtime.tool_table import ToolTable
    from .runtime.executor import GraphExecutor

    loader = ProjectLoader(project_root)
    config = loader.load()

    schemas = SchemaParser(loader).parse_all()
    sources = SourceParser(loader).parse_all()

    jinja_env = AbtJinjaEnv(
        schema_registry=schemas, source_registry=sources,
        project_vars=config.vars, macro_paths=loader.list_macro_files(),
        strict=False,
    )
    prompt_compiler = PromptCompiler(jinja_env, defaults=config.models.get("default"))
    prompt_files = loader.list_prompt_files()
    prompt_root = project_root / config.paths.prompt_paths[0]
    parsed = prompt_compiler.compile_all(prompt_files, prompt_root)
    folder_tree = FolderParser.build_tree(prompt_root, parsed)

    gb = GraphBuilder(
        parsed_prompts=parsed, folder_tree=folder_tree,
        schema_registry=schemas, source_registry=sources,
        project_name=config.name,
    )
    graph_structure = gb.build_structure()

    # Load input
    initial_input = {}
    if input_file:
        with open(input_file) as f:
            initial_input = json.load(f)

    # Execute
    db = DatabaseManager(db_path)
    db.connect()
    click.echo(f"  Database: {db_path}")

    tool_table = ToolTable(sources, db)
    tool_table.build_all()

    executor = GraphExecutor(graph_structure, db, tool_table, llm_factory=None)
    click.echo(f"  Nodes: {len(graph_structure.all_nodes)}")

    result = executor.execute(initial_input)
    node_outputs = result.get("node_outputs", {})

    click.echo("\nResults:")
    for node_name, output in node_outputs.items():
        click.echo(f"  {node_name}:")
        for key, val in output.items():
            click.echo(f"    {key}: {val}")

    if verbose:
        executor.print_traces()

    db.close()
    click.echo("\nDone.")


@cli.command()
@click.option("--select", "-s", multiple=True, help="Test specific nodes")
@click.option("--db-path", default=":memory:", help="SQLite database (in-memory default)")
def test(select: tuple, db_path: str):
    """Run tests defined in the project (schema validation, source connectivity)."""
    project_root = _find_project_root()
    click.echo(f"Testing project at {project_root}...")

    from .project import ProjectLoader
    from .compiler.schema_parser import SchemaParser
    from .compiler.source_parser import SourceParser

    loader = ProjectLoader(project_root)
    config = loader.load()

    # Test schemas parse and validate
    schema_parser = SchemaParser(loader)
    schemas = schema_parser.parse_all()
    click.echo(f"  Schemas: {len(schemas)} models loaded")

    for name, model_cls in schemas.items():
        schema = model_cls.model_json_schema()
        field_count = len(schema.get("properties", {}))
        click.echo(f"    {name}: {field_count} fields")

    # Test sources parse
    source_parser = SourceParser(loader)
    sources = source_parser.parse_all()
    click.echo(f"  Sources: {len(sources)} sources loaded")

    for name, src in sources.items():
        table_count = len(src.tables)
        click.echo(f"    {name}: {table_count} tables ({src.type.value})")

    click.echo("All tests passed.")


def _find_project_root() -> Path:
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / "abt_project.yml").exists():
            return parent
    raise click.ClickException(
        "abt_project.yml not found. Run 'abt init' first or cd into an abt project."
    )


def _count_subgraphs(sg) -> int:
    count = len(sg.subgraphs)
    for child in sg.subgraphs:
        count += _count_subgraphs(child)
    return count


def _create_project_skeleton(root: Path, project_name: str):
    root.mkdir(parents=True)

    config = (
        f"name: {project_name}\n"
        f"version: '0.1.0'\n"
        f"\n"
        f"paths:\n"
        f"  prompt_paths: [prompts]\n"
        f"  schema_paths: [schemas]\n"
        f"  source_paths: [sources]\n"
        f"  macro_paths: [macros]\n"
        f"  target_path: target\n"
        f"\n"
        f"models:\n"
        f"  default:\n"
        f"    provider: deepseek\n"
        f"    model: deepseek-chat\n"
        f"    temperature: 0.7\n"
        f"    max_tokens: 4096\n"
        f"\n"
        f"vars: {{}}\n"
    )
    (root / "abt_project.yml").write_text(config)

    for d in ["prompts", "schemas", "sources", "macros", "target"]:
        (root / d).mkdir(exist_ok=True)

    example_schema = (
        "version: 1\n"
        "models:\n"
        "  - name: example_output\n"
        "    description: \"Example output schema - replace with your own.\"\n"
        "    fields:\n"
        "      - name: result\n"
        "        type: str\n"
        "        description: \"The agent's final result\"\n"
    )
    (root / "schemas" / "example.yml").write_text(example_schema)

    example_prompt = (
        "{{ config(temperature=0.7, max_tool_calls=3) }}\n"
        "\n"
        "You are a helpful agent.\n"
        "Answer the user's question based on the available context.\n"
        "\n"
        "SELECT\n"
        "    result  -- str: your final answer\n"
        "FROM analysis\n"
    )
    (root / "prompts" / "hello_agent.prompt").write_text(example_prompt)
