"""CLI — Click-based command-line interface for abt."""

import copy
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

    artifacts = _compile_project(project_root, full_refresh)
    config = artifacts["config"]
    loader = artifacts["loader"]
    graph_structure = artifacts["graph_structure"]
    gb = artifacts["graph_builder"]
    manifest = artifacts["manifest"]

    target = loader.get_target_dir()

    # Generate manifest
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )
    click.echo(f"  Manifest: target/manifest.json")

    # Generate code
    code = gb.generate_python_code(graph_structure, target / "compiled_graph.py")
    click.echo(f"  Generated: target/compiled_graph.py ({len(code)} bytes)")
    click.echo("Compilation complete.")


@cli.command()
@click.option("--select", "-s", "selectors", multiple=True,
              help="Select nodes (dbt-style: +name+, tag:xxx, path:xxx, glob)")
@click.option("--exclude", multiple=True, help="Exclude nodes from selection")
@click.option("--thread-id", help="Execution thread ID (for resuming)")
@click.option("--input", "-i", "input_file", type=click.Path(exists=True), help="JSON input file")
@click.option("--db-path", default="abt_state.db", help="SQLite database path")
@click.option("--stream/--no-stream", default=True, help="Stream output to console")
@click.option("--verbose", "-v", is_flag=True, help="Show LLM traces")
def run(selectors, exclude, thread_id, input_file, db_path, stream, verbose):
    """Execute the compiled graph with SQLite persistence."""
    project_root = _find_project_root()
    click.echo(f"Running project at {project_root}...")

    from .compiler.selector import NodeSelector
    from .runtime.db import DatabaseManager
    from .runtime.tool_table import ToolTable
    from .runtime.executor import GraphExecutor

    artifacts = _compile_project(project_root, full_refresh=False)
    config = artifacts["config"]
    loader = artifacts["loader"]
    graph_structure = artifacts["graph_structure"]
    manifest = artifacts["manifest"]

    # Write manifest
    target = loader.get_target_dir()
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    # Apply selectors
    node_selector = NodeSelector(manifest)
    selected_names = node_selector.resolve_selectors(list(selectors) if selectors else None)
    selected_names = node_selector.resolve_exclusions(selected_names, list(exclude) if exclude else None)

    if selectors or exclude:
        graph_structure = _filter_graph_structure(graph_structure, set(selected_names))
        click.echo(f"  Selected: {len(selected_names)} nodes")

    # Load input
    initial_input = {}
    if input_file:
        with open(input_file) as f:
            initial_input = json.load(f)

    # Execute
    db = DatabaseManager(db_path)
    db.connect()
    click.echo(f"  Database: {db_path}")

    sources = graph_structure.all_sources
    tool_table = ToolTable(sources, db)
    tool_table.build_all()

    if stream:
        def _stream_printer(node_name: str, cte_name: str, delta: str, event: str) -> None:
            if event == "cte_start":
                click.echo(f"\n[{node_name}/{cte_name}] ", nl=False)
            elif event == "token":
                click.echo(delta, nl=False)
            elif event == "cte_end":
                click.echo()
    else:
        _stream_printer = None

    executor = GraphExecutor(graph_structure, db, tool_table, llm_factory=None,
                             stream_callback=_stream_printer)
    click.echo(f"  Nodes: {len(graph_structure.all_nodes)}")

    result = executor.execute(initial_input, thread_id=thread_id)

    # HITL approval loop
    while "__interrupt__" in result:
        interrupt_info = result.pop("__interrupt__")
        node_name = interrupt_info.get("node", "unknown")
        output = interrupt_info.get("output", {})
        message = interrupt_info.get("message", "Approve?")

        click.echo(f"\n[HITL] {message}")
        click.echo(f"  Node: {node_name}")
        click.echo(f"  Output: {json.dumps(output, indent=4)}")

        choice = click.prompt(
            "Action (y=approve / n=reject / e=edit)",
            type=click.Choice(["y", "n", "e"]),
            default="y",
        )
        if choice == "y":
            decision = {"action": "approve"}
        elif choice == "n":
            decision = {"action": "reject"}
        else:  # choice == "e"
            edit_input = click.edit(json.dumps(output, indent=2))
            if edit_input:
                try:
                    edited = json.loads(edit_input)
                    decision = {"action": "edit", "edited_output": edited}
                except json.JSONDecodeError:
                    click.echo("Invalid JSON, approving original output.")
                    decision = {"action": "approve"}
            else:
                decision = {"action": "approve"}

        click.echo("  Resuming...")
        result = executor.resume(decision, thread_id=thread_id)

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
@click.option("--select", "-s", "selectors", multiple=True,
              help="Test specific nodes (dbt-style)")
@click.option("--exclude", multiple=True, help="Exclude nodes from testing")
@click.option("--db-path", default=":memory:", help="SQLite database path")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed test output")
def test(selectors, exclude, db_path, verbose):
    """Run data assertions defined in .test.yml files.

    Test files live alongside .prompt files and define assertions
    that validate the semantic correctness of node outputs.

    \\b
    Example .test.yml:
        tests:
          - name: stock_not_negative
            assert: quantity_on_hand >= 0
          - name: location_not_empty
            assert: location is not null
    """
    project_root = _find_project_root()

    from .compiler.selector import NodeSelector
    from .runtime.db import DatabaseManager
    from .runtime.tool_table import ToolTable
    from .runtime.executor import GraphExecutor
    from .runtime.test_runner import TestRunner

    artifacts = _compile_project(project_root, full_refresh=False)
    config = artifacts["config"]
    loader = artifacts["loader"]
    graph_structure = artifacts["graph_structure"]
    manifest = artifacts["manifest"]

    target = loader.get_target_dir()
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    # Apply selectors
    node_selector = NodeSelector(manifest)
    selected_names = node_selector.resolve_selectors(list(selectors) if selectors else None)
    selected_names = node_selector.resolve_exclusions(selected_names, list(exclude) if exclude else None)

    if selectors or exclude:
        graph_structure = _filter_graph_structure(graph_structure, set(selected_names))

    # Discover tests
    prompt_root = project_root / config.paths.prompt_paths[0]
    test_runner = TestRunner(prompt_root)
    tests_by_node = test_runner.discover()

    if test_runner.test_count == 0:
        click.echo("No .test.yml files found.")
        return

    click.echo(f"  Tests: {test_runner.test_count} assertions across "
               f"{len(tests_by_node)} nodes")

    # Execute graph
    db = DatabaseManager(db_path)
    db.connect()

    sources = graph_structure.all_sources
    tool_table = ToolTable(sources, db)
    tool_table.build_all()

    executor = GraphExecutor(graph_structure, db, tool_table, llm_factory=None)
    click.echo(f"  Nodes: {len(graph_structure.all_nodes)}")

    result = executor.execute({})
    node_outputs = result.get("node_outputs", {})

    # Evaluate tests
    all_results = []
    for node_name in graph_structure.all_nodes:
        output = node_outputs.get(node_name)
        results = test_runner.evaluate(node_name, output)
        all_results.extend(results)

    # Report
    passed = sum(1 for r in all_results if r.passed)
    failed = sum(1 for r in all_results if not r.passed)

    click.echo(f"\nResults: {passed} passed, {failed} failed, {len(all_results)} total\n")

    for r in all_results:
        if r.passed:
            if verbose:
                click.echo(f"  {r.node_name} :: {r.test_name} — PASS")
        else:
            click.echo(f"  {r.node_name} :: {r.test_name} — FAIL")
            click.echo(f"    {r.message}")

    if verbose:
        click.echo(f"\nNode outputs:")
        for node_name, output in node_outputs.items():
            click.echo(f"  {node_name}:")
            for key, val in output.items():
                click.echo(f"    {key}: {val}")

    executor.print_traces()
    db.close()

    if failed > 0:
        click.echo(f"\n{failed} test(s) failed.")
        raise SystemExit(1)
    else:
        click.echo("\nAll tests passed.")


def _compile_project(project_root: Path, full_refresh: bool) -> dict:
    """Compile the project, returning a dict with all build artifacts.

    Uses incremental compilation when possible: only recompiles .prompt files
    whose content has changed since the last run. Schema, source, macro, or
    project config changes trigger a full rebuild.
    """
    from .project import ProjectLoader
    from .compiler.schema_parser import SchemaParser
    from .compiler.source_parser import SourceParser
    from .compiler.jinja_env import AbtJinjaEnv
    from .compiler.prompt_compiler import PromptCompiler
    from .compiler.folder_parser import FolderParser
    from .compiler.graph_builder import GraphBuilder
    from .compiler.manifest_generator import generate_manifest
    from .compiler.cache_manager import CacheManager

    loader = ProjectLoader(project_root)
    config = loader.load()

    click.echo(f"  Project: {config.name} v{config.version}")

    schemas = SchemaParser(loader).parse_all()
    click.echo(f"  Schemas: {len(schemas)} models")

    sources = SourceParser(loader).parse_all()
    click.echo(f"  Sources: {len(sources)}")

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
    prompt_rel = config.paths.prompt_paths[0]

    target_dir = loader.get_target_dir()
    cache = CacheManager(target_dir)
    previous = cache.load_previous_manifest() if not full_refresh else None

    if previous is None:
        if full_refresh:
            click.echo(f"  Full refresh: recompiling all prompts")
        else:
            click.echo(f"  Compiling all prompts (no cache found)")
        parsed = prompt_compiler.compile_all(prompt_files, prompt_root)
        file_hashes = cache.compute_hashes(loader, prompt_root)
    else:
        prev_meta = previous.get("metadata", {})
        prev_time = prev_meta.get("generated_at", "unknown")
        click.echo(f"  Using cached manifest from {prev_time}")

        changes = cache.detect_changes(loader, prompt_root, previous)

        if changes["full_rebuild"]:
            click.echo(f"  Full rebuild: {changes['reason']}")
            parsed = prompt_compiler.compile_all(prompt_files, prompt_root)
        else:
            changed = changes["changed_qualified"]
            unchanged = changes["unchanged_qualified"]

            if not changed:
                click.echo(f"  All {len(unchanged)} prompts unchanged — using cache")
                parsed = cache.load_cached_prompts(
                    unchanged, previous, project_root, prompt_rel
                )
            else:
                click.echo(
                    f"  Incremental: {len(changed)} changed, {len(unchanged)} cached"
                )
                changed_files = [
                    f for f in prompt_files
                    if str(f.relative_to(prompt_root).with_suffix("")).replace("\\", "/")
                    in changed
                ]
                parsed_new = prompt_compiler.compile_all(changed_files, prompt_root)
                parsed_cached = cache.load_cached_prompts(
                    unchanged, previous, project_root, prompt_rel
                )
                parsed = {**parsed_new, **parsed_cached}

        file_hashes = changes.get("current_hashes",
                                  cache.compute_hashes(loader, prompt_root))

    click.echo(f"  Prompts: {len(parsed)} files")

    folder_tree = FolderParser.build_tree(prompt_root, parsed)
    subgraph_count = _count_subgraphs(folder_tree)
    click.echo(f"  Folders: {subgraph_count} subgraphs")

    gb = GraphBuilder(
        parsed_prompts=parsed,
        folder_tree=folder_tree,
        schema_registry=schemas,
        source_registry=sources,
        project_name=config.name,
    )
    graph_structure = gb.build_structure()
    manifest = generate_manifest(graph_structure, config, file_hashes)

    return {
        "config": config,
        "loader": loader,
        "graph_structure": graph_structure,
        "graph_builder": gb,
        "manifest": manifest,
    }


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


def _filter_graph_structure(graph_structure, selected: set):
    """Return a shallow copy of graph_structure with only selected nodes.

    Filters all_nodes, dependency_graph, and prunes the root subgraph tree.
    """
    gs = copy.copy(graph_structure)
    gs.all_nodes = {
        k: v for k, v in gs.all_nodes.items() if k in selected
    }
    gs.dependency_graph = {
        k: {d for d in deps if d in selected}
        for k, deps in gs.dependency_graph.items()
        if k in selected
    }
    gs.root = _prune_subgraph(gs.root, selected)
    return gs


def _prune_subgraph(sg, selected: set):
    """Recursively remove non-selected nodes from a SubgraphDef. Returns a new SubgraphDef."""
    from .models.graph import SubgraphDef

    pruned_nodes = [n for n in sg.nodes if n in selected]
    pruned_children = [
        _prune_subgraph(child, selected)
        for child in sg.subgraphs
    ]
    pruned_children = [c for c in pruned_children if c is not None]

    if not pruned_nodes and not pruned_children:
        return None

    return SubgraphDef(
        name=sg.name,
        folder_name=sg.folder_name,
        routing=sg.routing,
        metadata=sg.metadata,
        parent_ref=sg.parent_ref,
        nodes=pruned_nodes,
        subgraphs=pruned_children,
        order_index=sg.order_index,
    )


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
