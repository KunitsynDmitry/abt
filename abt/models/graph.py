"""Graph structure models — SubgraphDef, RoutingType, GraphStructure."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RoutingType(str, Enum):
    SEQUENTIAL = "sequential"
    REQUIRE_ALL = "require_all"
    REQUIRE_ANY = "require_any"
    REQUIRE_FIRST = "require_first"


class SubgraphDef(BaseModel):
    name: str
    folder_name: str
    routing: RoutingType = RoutingType.SEQUENTIAL
    metadata: dict[str, Any] = Field(default_factory=dict)
    parent_ref: str | None = None
    nodes: list[str] = Field(default_factory=list)
    subgraphs: list["SubgraphDef"] = Field(default_factory=list)
    order_index: int = 0

    @property
    def all_child_names(self) -> list[str]:
        return self.nodes + [sg.name for sg in self.subgraphs]


class GraphStructure(BaseModel):
    project_name: str
    root: SubgraphDef = Field(default_factory=lambda: SubgraphDef(
        name="root", folder_name="root", routing=RoutingType.SEQUENTIAL
    ))
    all_nodes: dict[str, Any] = Field(default_factory=dict)      # qualified_name → CompiledNode
    all_schemas: dict[str, type] = Field(default_factory=dict, exclude=True)  # schema_name → Pydantic class
    all_sources: dict[str, Any] = Field(default_factory=dict)    # source_name → SourceDefinition
    dependency_graph: dict[str, set[str]] = Field(default_factory=dict)  # node → upstream deps
