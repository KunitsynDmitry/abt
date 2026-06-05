"""GraphExecutor — runs a compiled graph with SQLite checkpointing and tracing.

Uses LangGraph StateGraph for execution with support for three routing types:
- SEQUENTIAL: chain nodes one after another
- REQUIRE_ALL: fan-out parallel → fan-in collector (AND gate)
- REQUIRE_ANY: fan-out → first-success routes forward (OR gate)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Callable

from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

from ..models.graph import RoutingType, SubgraphDef
from .db import DatabaseManager
from .tool_table import ToolTable
from .node_runner import NodeRunner

NODE_OUTPUTS_KEY = "node_outputs"
ERRORS_KEY = "errors"


def _merge_dicts(left: dict, right: dict) -> dict:
    """Reducer: merge right into left for node_outputs accumulation."""
    return {**left, **right}


def _concat_lists(left: list, right: list) -> list:
    """Reducer: concatenate lists for error accumulation."""
    return left + right


class AbtState(TypedDict, total=False):
    """Unified state for the LangGraph agent.

    Uses Annotated reducers so node outputs accumulate across parallel
    execution branches and errors concatenate.
    """

    messages: Annotated[list, add_messages]
    node_outputs: Annotated[dict, _merge_dicts]
    errors: Annotated[list, _concat_lists]
    _run_id: str


class GraphExecutor:
    def __init__(
        self,
        graph_structure: Any,
        db: DatabaseManager,
        tool_table: ToolTable | None = None,
        llm_factory: Callable[[], Any] | None = None,
        stream_callback: Callable[[str, str, str, str], None] | None = None,
    ):
        self.structure = graph_structure
        self.db = db
        self.tool_table = tool_table
        self.llm_factory = llm_factory
        self.stream_callback = stream_callback

    # ── Public API ─────────────────────────────────────────────

    def execute(self, initial_input: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute using real LangGraph StateGraph with proper routing."""
        initial_input = initial_input or {}
        run_id = self.db.create_run(project_name=self.structure.project_name)

        # Build the LangGraph app
        sg = self._build_state_graph()
        app = sg.compile()

        init_state = {
            "messages": [],
            NODE_OUTPUTS_KEY: {},
            ERRORS_KEY: [],
            "_run_id": run_id,
            **initial_input,
        }

        result = app.invoke(init_state)

        self.db.complete_run(run_id, status="completed", final_state=dict(result))
        return dict(result)

    def execute_sequential(self, initial_input: dict[str, Any]) -> dict[str, Any]:
        """Execute all nodes in dependency order (simplified sequential mode).

        Kept as fallback for debugging and comparison.
        """
        run_id = self.db.create_run(project_name=self.structure.project_name)

        state = {
            "_run_id": run_id,
            "messages": [],
            NODE_OUTPUTS_KEY: {},
            ERRORS_KEY: [],
            **initial_input,
        }

        ordered = self._topological_order()

        for node_name in ordered:
            node = self.structure.all_nodes[node_name]
            tools = self.tool_table.get_tools_for_node(node.resolved_tools) if self.tool_table else []
            runner = NodeRunner(node, tools, self.db, llm_factory=self.llm_factory, stream_callback=self.stream_callback)
            node_fn = runner.make_node_function()

            update = node_fn(state)
            state[NODE_OUTPUTS_KEY].update(update.get(NODE_OUTPUTS_KEY, {}))
            errors = update.get(ERRORS_KEY, [])
            if errors:
                state[ERRORS_KEY].extend(errors)

        self.db.complete_run(run_id, status="completed", final_state=state)
        return state

    # ── LangGraph StateGraph construction ──────────────────────

    def _build_state_graph(self) -> StateGraph:
        """Build a LangGraph StateGraph with recursive subgraph compilation."""
        sg = StateGraph(AbtState)

        blocks = self._flatten_tree(self.structure.root)
        wired = self._build_blocks_in_graph(sg, blocks)

        self._wire_sequential_blocks(sg, blocks)

        for node_name in self.structure.all_nodes:
            if node_name not in wired:
                sg.add_edge(START, node_name)
                sg.add_edge(node_name, END)

        return sg

    # ── Tree flattening ────────────────────────────────────────

    def _flatten_tree(self, subgraph_def: SubgraphDef) -> list[dict[str, Any]]:
        """Produce recursively nested execution blocks.

        SEQUENTIAL children are expanded inline. REQUIRE_ALL/REQUIRE_ANY
        children become nested blocks compiled as LangGraph subgraphs.

        Each block is one of:
          - {"type": "node", "name": str}
          - {"type": "parallel", "name": str, "children": [block, ...]}
          - {"type": "any", "name": str, "children": [block, ...]}
        """
        result: list[dict[str, Any]] = []

        for child_sg in sorted(subgraph_def.subgraphs, key=lambda s: s.order_index):
            if child_sg.routing == RoutingType.SEQUENTIAL:
                result.extend(self._flatten_tree(child_sg))
            else:
                result.append(self._make_block(child_sg))

        for node_name in subgraph_def.nodes:
            result.append({"type": "node", "name": node_name})

        return result

    def _make_block(self, subgraph_def: SubgraphDef) -> dict[str, Any]:
        """Create a nested block from a non-sequential subgraph."""
        return {
            "type": "parallel" if subgraph_def.routing == RoutingType.REQUIRE_ALL else "any",
            "name": subgraph_def.name,
            "children": self._flatten_tree(subgraph_def),
        }

    # ── Recursive graph building ───────────────────────────────

    def _build_blocks_in_graph(self, sg: StateGraph, blocks: list[dict]) -> set[str]:
        """Recursively add nodes/subgraphs to sg. Returns set of wired names."""
        wired: set[str] = set()

        for block in blocks:
            if block["type"] == "node":
                self._add_leaf_node_to(sg, block)
                wired.add(block["name"])
            elif block["type"] in ("parallel", "any"):
                child_sg = StateGraph(AbtState)
                child_wired = self._build_blocks_in_graph(child_sg, block["children"])
                wired.update(child_wired)
                wired.add(block["name"])

                if block["type"] == "parallel":
                    self._wire_fan_out_in(child_sg, block["children"])
                else:
                    self._wire_or_gate(child_sg, block)

                compiled = child_sg.compile()
                sg.add_node(block["name"], compiled)

        return wired

    def _add_leaf_node_to(self, sg: StateGraph, block: dict) -> None:
        """Add a leaf .prompt node to the given StateGraph."""
        node_name = block["name"]
        compiled_node = self.structure.all_nodes[node_name]
        tools = (
            self.tool_table.get_tools_for_node(compiled_node.resolved_tools)
            if self.tool_table
            else []
        )
        runner = NodeRunner(compiled_node, tools, self.db, llm_factory=self.llm_factory, stream_callback=self.stream_callback)
        node_fn = runner.make_node_function()
        sg.add_node(node_name, node_fn)

    def _wire_fan_out_in(self, sg: StateGraph, children: list[dict]) -> None:
        """Wire AND-gate: START -> all entry points, all exits -> END."""
        for child in children:
            for entry in self._block_entry_names(child):
                sg.add_edge(START, entry)
            for exit_name in self._block_exit_names(child):
                sg.add_edge(exit_name, END)

    def _wire_or_gate(self, sg: StateGraph, block: dict) -> None:
        """Wire OR-gate: START fan-out -> children -> collector -> END."""
        collector_name = f"{block['name']}__collector"
        leaf_names = self._all_node_names_in_blocks(block["children"])

        def any_collector(state: dict) -> dict:
            node_outputs = state.get(NODE_OUTPUTS_KEY, {})
            for name in leaf_names:
                output = node_outputs.get(name, {})
                if output and "error" not in output:
                    return {}
            return {ERRORS_KEY: [f"require_any block '{block['name']}': all nodes failed"]}

        any_collector.__name__ = f"any_collector_{block['name']}"
        sg.add_node(collector_name, any_collector)

        for child in block["children"]:
            for entry in self._block_entry_names(child):
                sg.add_edge(START, entry)
            for exit_name in self._block_exit_names(child):
                sg.add_edge(exit_name, collector_name)

        sg.add_edge(collector_name, END)

    def _wire_sequential_blocks(self, sg: StateGraph, blocks: list[dict]) -> None:
        """Wire START -> blocks[0] -> blocks[1] -> ... -> END sequentially."""
        if not blocks:
            sg.add_edge(START, END)
            return

        for entry in self._block_entry_names(blocks[0]):
            sg.add_edge(START, entry)

        for i in range(len(blocks) - 1):
            for src in self._block_exit_names(blocks[i]):
                for tgt in self._block_entry_names(blocks[i + 1]):
                    sg.add_edge(src, tgt)

        for exit_name in self._block_exit_names(blocks[-1]):
            sg.add_edge(exit_name, END)

    def _block_entry_names(self, block: dict) -> list[str]:
        """Entry node names for a block: the node itself."""
        return [block["name"]]

    def _block_exit_names(self, block: dict) -> list[str]:
        """Exit node names for a block: the node itself."""
        return [block["name"]]

    def _all_node_names_in_blocks(self, blocks: list[dict]) -> list[str]:
        """Recursively collect all leaf node names from nested blocks."""
        result: list[str] = []
        for block in blocks:
            if block["type"] == "node":
                result.append(block["name"])
            else:
                result.extend(self._all_node_names_in_blocks(block.get("children", [])))
        return result

    # ── Topological order (for sequential fallback) ────────────

    def _topological_order(self) -> list[str]:
        """Compute topological order of nodes based on dependencies."""
        deps = self.structure.dependency_graph
        all_nodes = set(self.structure.all_nodes.keys())
        visited: list[str] = []
        temp: set[str] = set()
        perm: set[str] = set()

        def visit(n: str):
            if n in perm:
                return
            if n in temp:
                return  # Cycle detected — skip edge
            temp.add(n)
            for dep in deps.get(n, set()):
                if dep in all_nodes:
                    visit(dep)
            temp.remove(n)
            perm.add(n)
            visited.append(n)

        for node in all_nodes:
            visit(node)

        return visited

    # ── Tracing ────────────────────────────────────────────────

    def print_traces(self, run_id: str | None = None):
        """Print LLM traces for the latest or specified run."""
        if run_id is None:
            row = self.db.conn.execute(
                "SELECT run_id FROM agent_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                print("No runs found.")
                return
            run_id = row["run_id"]

        traces = self.db.get_run_traces(run_id)
        print(f"\nTraces for run {run_id} ({len(traces)} entries):")
        for t in traces:
            print(
                f"  [{t['node_name']}] {t['step_name'] or 'main'} "
                f"— model={t['model']}, latency={t['latency_ms']}ms, "
                f"tokens: {t['tokens_input']}/{t['tokens_output']}"
            )
