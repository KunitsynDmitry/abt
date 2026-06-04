"""CompiledNode — a fully resolved node ready for graph assembly."""

from typing import Any

from pydantic import BaseModel, Field

from .prompt import ParsedPrompt


class CompiledNode(BaseModel):
    name: str
    qualified_name: str
    prompt: ParsedPrompt
    input_schema_type: type | None = Field(default=None, exclude=True)
    output_schema_type: type | None = Field(default=None, exclude=True)
    resolved_tools: list[str] = Field(default_factory=list)
    on_fail_target: str | None = None
    max_retries: int = 1
    llm_config: dict[str, Any] = Field(default_factory=dict)
