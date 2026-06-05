"""CLI — Click-based command-line interface for abt."""

import copy
import json
import sys
from pathlib import Path
from typing import Any

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
@click.option("--project-dir", "-d", default=None,
              help="Path to abt project (default: auto-detect from CWD)")
@click.option("--select", "-s", multiple=True, help="Select specific prompt files")
@click.option("--full-refresh", is_flag=True, help="Ignore cache, recompile from scratch")
def compile(select: tuple, full_refresh: bool, project_dir):
    """Compile the abt project into manifest.json and target artifacts."""
    project_root = _find_project_root(project_dir)
    click.echo(f"Compiling project at {project_root}...")

    selectors = list(select) if select else None
    artifacts = _compile_project(project_root, full_refresh, select=selectors)
    config = artifacts["config"]
    loader = artifacts["loader"]
    graph_structure = artifacts["graph_structure"]
    manifest = artifacts["manifest"]

    target = loader.get_target_dir()

    # Generate manifest
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )
    click.echo(f"  Manifest: target/manifest.json")
    click.echo("Compilation complete.")


@cli.command()
@click.option("--select", "-s", "selectors", multiple=True,
              help="Nodes to inspect (qualified name or leaf name)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def inspect(selectors, as_json):
    """Show compiled details for nodes: refs, context projection, tools, routing.

    Without --select, lists all compiled nodes with a short summary.
    With --select, shows full details for the specified node(s).

    \\b
    Examples:
        abt inspect
        abt inspect -s check_stock
        abt inspect -s require_all/check_stock
    """
    project_root = _find_project_root()
    artifacts = _compile_project(project_root, full_refresh=False)
    graph_structure = artifacts["graph_structure"]
    manifest = artifacts["manifest"]

    all_nodes = graph_structure.all_nodes

    if selectors:
        # Resolve selectors to qualified names
        resolved: list[str] = []
        for sel in selectors:
            if sel in all_nodes:
                resolved.append(sel)
            else:
                matches = [
                    qn for qn, node in all_nodes.items()
                    if node.name == sel or qn.endswith("/" + sel)
                ]
                if not matches:
                    click.echo(f"  Warning: '{sel}' not found, skipping")
                resolved.extend(matches)
        target_names = resolved
    else:
        target_names = sorted(all_nodes.keys())

    if as_json:
        result = {}
        for qname in target_names:
            result[qname] = _inspect_node(all_nodes[qname], manifest)
        click.echo(json.dumps(result, indent=2, default=str))
    else:
        click.echo(f"\n{'=' * 60}")
        for qname in target_names:
            _print_node_inspect(qname, all_nodes[qname], manifest)
        click.echo(f"{'=' * 60}\n")


def _inspect_node(node, manifest: dict) -> dict:
    """Build a dict with compiled node details."""
    p = node.prompt
    info: dict[str, Any] = {
        "name": node.name,
        "qualified_name": node.qualified_name,
        "file_path": str(p.file_path),
        "config": {
            "model": p.config.model,
            "temperature": p.config.temperature,
            "max_tokens": p.config.max_tokens,
            "max_retries": node.max_retries,
        },
        "dependencies": sorted(p.raw_dependencies),
        "tool_refs": [f"{s}.{t}" for s, t in p.raw_source_refs],
        "resolved_tools": node.resolved_tools,
        "output_schema": p.config.output_schema or None,
        "output_columns": p.output_columns,
        "on_fail_target": node.on_fail_target,
        "cte_blocks": [],
    }
    if node.route_on:
        info["routing"] = {
            "route_on": node.route_on,
            "route_map": node.route_map,
            "route_default": node.route_default,
        }
    if node.approve_when:
        info["approval"] = {
            "approve_when": node.approve_when,
            "approve_message": node.approve_message,
        }

    for cte in p.cte_blocks:
        cte_info: dict[str, Any] = {
            "name": cte.name,
            "type": cte.cte_type,
            "tool_refs": [f"{s}.{t}" for s, t in cte.tool_refs],
            "model_refs": cte.model_refs,
        }
        if cte.context_projection:
            proj = cte.context_projection
            cte_info["context_projection"] = {
                "columns": proj.columns,
                "ref_name": proj.ref_name,
                "conditions": [
                    {"field": c.field, "op": c.op, "value": c.value}
                    for c in proj.conditions
                ],
                "logic": proj.logic,
            }
        info["cte_blocks"].append(cte_info)

    return info


def _print_node_inspect(qname: str, node, manifest: dict) -> None:
    """Pretty-print compiled node details."""
    p = node.prompt
    click.echo(f"\n  Node: {qname}")
    click.echo(f"  File: {p.file_path}")
    click.echo(f"  Config: model={p.config.model}, temp={p.config.temperature}, "
               f"max_tokens={p.config.max_tokens}, retries={node.max_retries}")

    if p.output_columns:
        click.echo(f"  Output columns: {', '.join(p.output_columns)}")
    if p.config.output_schema:
        click.echo(f"  Output schema: {p.config.output_schema}")

    click.echo(f"  Dependencies ({len(p.raw_dependencies)}): "
               f"{', '.join(sorted(p.raw_dependencies)) if p.raw_dependencies else '(none)'}")
    click.echo(f"  Tools ({len(node.resolved_tools)}): "
               f"{', '.join(node.resolved_tools) if node.resolved_tools else '(none)'}")

    if node.route_on:
        click.echo(f"  Dynamic routing: route_on='{node.route_on}', "
                   f"routes={node.route_map}, default={node.route_default}")
    if node.approve_when:
        click.echo(f"  Approval gate: when='{node.approve_when}', "
                   f"message='{node.approve_message}'")
    if node.on_fail_target:
        click.echo(f"  On fail: → {node.on_fail_target}")

    if p.cte_blocks:
        click.echo(f"  CTE blocks ({len(p.cte_blocks)}):")
        for cte in p.cte_blocks:
            flags = []
            if cte.cte_type:
                flags.append(cte.cte_type.upper())
            if cte.model_refs:
                flags.append(f"refs={cte.model_refs}")
            if cte.tool_refs:
                flags.append(f"tools={[(s, t) for s, t in cte.tool_refs]}")
            click.echo(f"    [{cte.name}] {' | '.join(flags) if flags else '(no refs)'}")

            proj = cte.context_projection
            if proj:
                parts = []
                if proj.columns:
                    parts.append(f"SELECT {', '.join(proj.columns)}")
                parts.append(f"FROM {proj.ref_name}")
                if proj.conditions:
                    conds = f" {proj.logic} ".join(
                        f"{c.field} {c.op} {c.value!r}" for c in proj.conditions
                    )
                    parts.append(f"WHERE {conds}")
                click.echo(f"      → context: {' '.join(parts)}")

@cli.command()
@click.option("--project-dir", "-d", default=None,
              help="Path to abt project (default: auto-detect from CWD)")
@click.option("--select", "-s", "selectors", multiple=True,
              help="Select nodes (qualified name or leaf name)")
@click.option("--exclude", multiple=True, help="Exclude nodes from selection")
@click.option("--thread-id", help="Execution thread ID (for resuming)")
@click.option("--input", "-i", "input_file", type=click.Path(exists=True),
              help="JSON input file (or event data file when used with --trigger)")
@click.option("--trigger", help="Trigger name to simulate")
@click.option("--db-path", default="abt_state.db", help="SQLite database path")
@click.option("--stream/--no-stream", default=True, help="Stream output to console")
@click.option("--refresh/--no-refresh", default=False, help="Force re-execute all nodes (ignore cache)")
@click.option("--verbose", "-v", is_flag=True, help="Show LLM traces")
def run(selectors, exclude, thread_id, input_file, trigger, db_path, stream, refresh, verbose, project_dir):
    """Execute the compiled graph with SQLite persistence."""
    project_root = _find_project_root(project_dir)
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

    # If --trigger is specified, resolve input from trigger definition
    if trigger:
        from .compiler.trigger_parser import TriggerParser
        trigger_parser = TriggerParser(loader)
        all_triggers = trigger_parser.parse_all()
        trigger_def = trigger_parser.resolve_trigger(trigger, all_triggers)

        from .runtime.trigger_manager import TriggerManager
        tm = TriggerManager(all_triggers)
        event_data = initial_input if initial_input else {}
        initial_input = tm.resolve_input(trigger_def, event_data)
        click.echo(f"  Trigger: {trigger} ({trigger_def.type.value})")

    # Execute
    db = DatabaseManager(db_path)
    db.connect()
    click.echo(f"  Database: {db_path}")

    sources = graph_structure.all_sources
    tool_table = ToolTable(sources, db)
    tool_table.build_all()

    executor = GraphExecutor(graph_structure, db, tool_table, llm_factory=None,
                             use_cache=not refresh)
    click.echo(f"  Nodes: {len(graph_structure.all_nodes)}")

    if stream:
        result = {}
        for stream_event in executor.execute_stream(initial_input, thread_id=thread_id):
            etype = stream_event["type"]
            data = stream_event["data"]

            if etype == "event":
                evt = data.get("event", "")
                if evt == "cte_start":
                    click.echo(f"\n[{data['node']}/{data['cte']}] ", nl=False)
                elif evt == "token":
                    try:
                        click.echo(data["delta"], nl=False)
                    except UnicodeEncodeError:
                        click.echo(data["delta"].encode("ascii", errors="replace").decode("ascii"), nl=False)
                elif evt == "cte_end":
                    click.echo()
            elif etype == "interrupt":
                result["__interrupt__"] = data
            elif etype == "final":
                result = data
    else:
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
            try:
                click.echo(f"    {key}: {val}")
            except UnicodeEncodeError:
                # Windows cp1251 console — replace emoji and other non-encodable chars
                click.echo(f"    {key}: {str(val).encode('ascii', errors='replace').decode('ascii')}")

    if verbose:
        executor.print_traces()

    db.close()
    click.echo("\nDone.")


@cli.command()
@click.option("--project-dir", "-d", default=None,
              help="Path to abt project (default: auto-detect from CWD)")
@click.option("--select", "-s", "selectors", multiple=True,
              help="Test specific nodes (dbt-style)")
@click.option("--exclude", multiple=True, help="Exclude nodes from testing")
@click.option("--db-path", default=":memory:", help="SQLite database path")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed test output")
def test(selectors, exclude, db_path, verbose, project_dir):
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
    project_root = _find_project_root(project_dir)

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


@cli.command()
@click.option("--project-dir", "-d", default=None,
              help="Path to abt project (default: auto-detect from CWD)")
@click.option("--port", type=int, default=8000, help="HTTP server port")
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--no-scheduler", is_flag=True, help="Disable cron scheduler")
@click.option("--no-webhook", is_flag=True, help="Disable webhook routes")
@click.option("--db-path", default="abt_state.db", help="SQLite database path")
def serve(port, host, no_scheduler, no_webhook, db_path, project_dir):
    """Start the ABT server with webhook handlers and optional scheduler."""
    import uvicorn

    project_root = _find_project_root(project_dir)
    click.echo(f"Serving project at {project_root}...")

    from .compiler.trigger_parser import TriggerParser
    from .runtime.db import DatabaseManager
    from .runtime.tool_table import ToolTable
    from .runtime.executor import GraphExecutor
    from .runtime.trigger_manager import TriggerManager
    from .runtime.server import create_app

    artifacts = _compile_project(project_root, full_refresh=False)
    config = artifacts["config"]
    loader = artifacts["loader"]
    graph_structure = artifacts["graph_structure"]
    manifest = artifacts["manifest"]

    target = loader.get_target_dir()
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    trigger_parser = TriggerParser(loader)
    all_triggers = trigger_parser.parse_all()

    webhook_count = sum(1 for t in all_triggers.values() if t.type.value == "webhook")
    schedule_count = sum(1 for t in all_triggers.values() if t.type.value == "schedule")
    click.echo(f"  Triggers: {len(all_triggers)} total "
               f"({webhook_count} webhook, {schedule_count} schedule)")

    db = DatabaseManager(db_path)
    db.connect()

    sources = graph_structure.all_sources
    tool_table = ToolTable(sources, db)
    tool_table.build_all()

    executor = GraphExecutor(graph_structure, db, tool_table, llm_factory=None)
    trigger_manager = TriggerManager(all_triggers, executor)

    if not no_scheduler and schedule_count > 0:
        _start_scheduler(all_triggers, trigger_manager)

    app = create_app(trigger_manager)

    click.echo(f"\nStarting server on http://{host}:{port}")
    click.echo(f"  Webhooks: http://{host}:{port}/triggers")
    click.echo(f"  Health:   http://{host}:{port}/health")

    uvicorn.run(app, host=host, port=port)


def _start_scheduler(schedule_triggers, trigger_manager):
    """Start in-process cron scheduler (requires apscheduler)."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        click.echo("  (install apscheduler for cron support: pip install apscheduler)")
        return

    scheduler = BackgroundScheduler()

    for trigger_name, trigger_def in schedule_triggers.items():
        if trigger_def.type.value != "schedule" or not trigger_def.schedule:
            continue
        try:
            cron_trigger = CronTrigger.from_crontab(trigger_def.schedule)
        except ValueError as e:
            click.echo(f"  Warning: invalid cron '{trigger_def.schedule}' "
                       f"for trigger '{trigger_name}': {e}")
            continue

        def make_job(name=trigger_name):
            def job():
                try:
                    trigger_manager.activate(name, {})
                except Exception as e:
                    click.echo(f"  Scheduler error [{name}]: {e}")
            return job

        scheduler.add_job(
            make_job(),
            trigger=cron_trigger,
            id=trigger_name,
            name=trigger_name,
            replace_existing=True,
        )
        click.echo(f"  Scheduled: {trigger_name} ({trigger_def.schedule})")

    scheduler.start()


def _compile_project(project_root: Path, full_refresh: bool,
                     select: tuple[str, ...] | None = None) -> dict:
    """Compile the project, returning a dict with all build artifacts.

    Uses incremental compilation when possible: only recompiles .prompt files
    whose content has changed since the last run. Schema, source, macro, or
    project config changes trigger a full rebuild.

    When ``select`` is provided, only those prompt files are force-recompiled;
    all other prompts come from cache (unless global invalidation forces a
    full rebuild).
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

    # Parse triggers (optional — project may have no triggers/)
    from .compiler.trigger_parser import TriggerParser
    all_triggers = TriggerParser(loader).parse_all()
    if all_triggers:
        click.echo(f"  Triggers: {len(all_triggers)}")

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

            # --select: force recompile selected files even if unchanged
            if select:
                select_set = set(select)
                forced = set()
                for qname in list(unchanged):
                    # Match by qualified name (exact or suffix)
                    if qname in select_set or any(
                        qname.endswith("/" + s) or qname == s for s in select_set
                    ):
                        forced.add(qname)
                        unchanged.remove(qname)
                if forced:
                    changed = changed | forced
                    click.echo(
                        f"  --select: force recompiling {len(forced)} specified prompts"
                    )

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

    # Compile blueprint prompts (reusable subgraphs referenced via _folder_name)
    blueprints_root = project_root / config.paths.blueprint_paths[0]
    if blueprints_root.exists():
        bp_files = list(blueprints_root.rglob("*.prompt"))
        if bp_files:
            bp_parsed = {}
            for bp_file in bp_files:
                bp_compiled = prompt_compiler.compile_file(bp_file, blueprints_root)
                bp_key = str(bp_file.relative_to(project_root).with_suffix("")).replace("\\", "/")
                bp_parsed[bp_key] = bp_compiled
            parsed = {**parsed, **bp_parsed}
            click.echo(f"  Blueprints: {len(bp_parsed)} prompts")

    folder_tree = FolderParser.build_tree(
        prompt_root, parsed,
        blueprints_root=blueprints_root if blueprints_root.exists() else None,
    )
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
    manifest = generate_manifest(graph_structure, config, file_hashes, triggers=all_triggers)

    return {
        "config": config,
        "loader": loader,
        "graph_structure": graph_structure,
        "manifest": manifest,
    }


def _find_project_root(explicit: str | None = None) -> Path:
    if explicit:
        root = Path(explicit).resolve()
        if not (root / "abt_project.yml").exists():
            raise click.ClickException(
                f"abt_project.yml not found in '{root}'. Is this an abt project?"
            )
        sys.path.insert(0, str(root))
        _load_dotenv(root)
        return root
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        if (parent / "abt_project.yml").exists():
            sys.path.insert(0, str(parent))
            _load_dotenv(parent)
            return parent
    raise click.ClickException(
        "abt_project.yml not found. Run 'abt init' first or cd into an abt project."
    )


def _load_dotenv(project_root: Path):
    """Load .env file from project root. Silently skips if file or package missing."""
    env_file = project_root / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
    except ImportError:
        pass


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

    for d in ["prompts", "schemas", "sources", "macros", "triggers", "blueprints", "target"]:
        (root / d).mkdir(exist_ok=True)

    # .env.example — copy to .env and fill in your keys
    env_example = (
        "# LLM API key (required)\n"
        "DEEPSEEK_API_KEY=sk-...\n"
        "# Optional: override base URL\n"
        "# DEEPSEEK_BASE_URL=https://api.deepseek.com\n"
    )
    (root / ".env.example").write_text(env_example)

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
