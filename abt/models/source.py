"""Source models — tool/source definitions (the 'sources' layer)."""

from enum import Enum
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pathlib import Path


class ToolType(str, Enum):
    REST_API = "rest_api"
    MCP_SERVER = "mcp_server"
    PYTHON_FUNCTION = "python_function"
    GRAPHQL = "graphql"


class SourceTable(BaseModel):
    name: str
    description: str = ""
    endpoint: str = ""
    method: str = "GET"
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    result_path: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    module: str = ""
    function: str = ""


class SourceDefinition(BaseModel):
    name: str
    type: ToolType = ToolType.REST_API
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    tables: list[SourceTable] = Field(default_factory=list)


class SourceFile(BaseModel):
    version: int = 1
    sources: list[SourceDefinition] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> "SourceFile":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
