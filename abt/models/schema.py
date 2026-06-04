"""Schema models — YAML schema definitions that become Pydantic models."""

from typing import Any

import yaml
from pydantic import BaseModel, Field
from pathlib import Path


class FieldConstraint(BaseModel):
    ge: float | None = None
    le: float | None = None
    gt: float | None = None
    lt: float | None = None
    regex: str | None = None
    enum: list[Any] | None = None
    min_length: int | None = None
    max_length: int | None = None
    multiple_of: float | None = None


class SchemaField(BaseModel):
    name: str
    type: str
    description: str = ""
    required: bool = True
    default: Any = None
    constraints: FieldConstraint = Field(default_factory=FieldConstraint)
    examples: list[Any] = Field(default_factory=list)


class SchemaModel(BaseModel):
    name: str
    description: str = ""
    fields: list[SchemaField] = Field(default_factory=list)


class SchemaFile(BaseModel):
    version: int = 1
    models: list[SchemaModel] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> "SchemaFile":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
