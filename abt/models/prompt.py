"""Prompt models — CTEBlock, PromptConfig, ParsedPrompt."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class WhereCondition(BaseModel):
    """A single WHERE condition: field op value."""
    field: str
    op: str = "="  # =, !=, >, <, >=, <=
    value: Any = None


class ContextProjection(BaseModel):
    """Parsed SELECT/FROM/WHERE from a CTE — defines what context the node actually receives."""
    columns: list[str] = Field(default_factory=list)
    ref_name: str = ""
    conditions: list[WhereCondition] = Field(default_factory=list)
    logic: str = "AND"  # AND | OR


class PromptConfig(BaseModel):
    provider: str = ""  # deepseek, openai, anthropic, or custom — empty = auto-detect from env
    model: str = "deepseek-chat"
    temperature: float = 0.7
    max_tokens: int = 4096
    on_fail_route: str | None = None
    max_retries: int = 1
    max_tool_calls: int = 5
    on_exhaust: str = "finalize"  # "finalize" | "fail"
    allowed_tools: list[str] = Field(default_factory=list)  # empty = all available
    output_schema: str = ""
    # Dynamic routing: LLM output determines next node
    route_on: str = ""                       # output field to route on
    route_when: list[str] = Field(default_factory=list)  # ["value:target", ...]
    route_default: str = ""                  # default target, "__END__" = END
    # Human-in-the-loop: approval gate via interrupt()
    approve_when: str = ""                   # Python expression evaluated against output
    approve_message: str = ""                # Custom message shown during approval


class CTEBlock(BaseModel):
    name: str
    raw_content: str
    rendered_content: str = ""
    cte_type: Literal["tool", "llm"] | None = None
    is_tool_step: bool = False
    tool_refs: list[tuple[str, str]] = Field(default_factory=list)
    model_refs: list[str] = Field(default_factory=list)
    context_projection: ContextProjection | None = None  # parsed SELECT/FROM/WHERE
    config: PromptConfig | None = None  # CTE-level config override


class TestDefinition(BaseModel):
    """A data assertion on a node's output — equivalent to dbt test."""

    name: str
    assert_: str = Field(alias="assert")
    description: str = ""


class ParsedPrompt(BaseModel):
    name: str
    file_path: Path
    relative_path: Path
    config: PromptConfig = Field(default_factory=PromptConfig)
    system_prompt: str = ""
    cte_blocks: list[CTEBlock] = Field(default_factory=list)
    output_columns: list[str] = Field(default_factory=list)
    raw_dependencies: set[str] = Field(default_factory=set)
    raw_source_refs: set[tuple[str, str]] = Field(default_factory=set)
