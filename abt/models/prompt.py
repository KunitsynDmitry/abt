"""Prompt models — CTEBlock, PromptConfig, ParsedPrompt."""

from pathlib import Path

from pydantic import BaseModel, Field


class PromptConfig(BaseModel):
    model: str = "deepseek-chat"
    temperature: float = 0.7
    max_tokens: int = 4096
    on_fail_route: str | None = None
    max_retries: int = 1
    max_tool_calls: int = 5
    on_exhaust: str = "finalize"  # "finalize" | "fail"
    allowed_tools: list[str] = Field(default_factory=list)  # empty = all available
    output_schema: str = ""


class CTEBlock(BaseModel):
    name: str
    raw_content: str
    rendered_content: str = ""
    is_tool_step: bool = False
    tool_refs: list[tuple[str, str]] = Field(default_factory=list)
    model_refs: list[str] = Field(default_factory=list)
    config: PromptConfig | None = None  # CTE-level config override


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
