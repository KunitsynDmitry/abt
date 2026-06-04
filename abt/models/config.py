"""Project configuration models."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ProjectPaths(BaseModel):
    prompt_paths: list[str] = Field(default_factory=lambda: ["prompts"])
    schema_paths: list[str] = Field(default_factory=lambda: ["schemas"])
    source_paths: list[str] = Field(default_factory=lambda: ["sources"])
    macro_paths: list[str] = Field(default_factory=lambda: ["macros"])
    target_path: str = "target"


class ModelDefaults(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-20250514"
    temperature: float = 0.7
    max_tokens: int = 4096


class AbtProjectConfig(BaseModel):
    name: str
    version: str = "0.1.0"
    paths: ProjectPaths = Field(default_factory=ProjectPaths)
    models: dict[str, ModelDefaults] = Field(default_factory=dict)
    vars: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> "AbtProjectConfig":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    def validate_project_structure(self, root: Path) -> list[str]:
        errors = []
        for prompt_path in self.paths.prompt_paths:
            if not (root / prompt_path).exists():
                errors.append(f"Prompt path not found: {prompt_path}")
        for schema_path in self.paths.schema_paths:
            if not (root / schema_path).exists():
                errors.append(f"Schema path not found: {schema_path}")
        return errors
