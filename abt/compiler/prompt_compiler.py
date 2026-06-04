"""PromptCompiler — compiles a single .prompt file into a ParsedPrompt object."""

from pathlib import Path

from .cte_parser import CTEParser
from .jinja_env import AbtJinjaEnv
from ..models.config import ModelDefaults
from ..models.prompt import ParsedPrompt, PromptConfig


class PromptCompiler:
    def __init__(self, jinja_env: AbtJinjaEnv, defaults: ModelDefaults | None = None):
        self.jinja_env = jinja_env
        self.defaults = defaults

    def compile_file(self, file_path: Path, relative_to: Path) -> ParsedPrompt:
        raw = file_path.read_text(encoding="utf-8")

        # Step 1: extract {{ config(...) }}
        config_kwargs, content_no_config = CTEParser.extract_config_dict(raw)

        # Step 2: extract system prompt (pre-WITH/CTE content)
        system_prompt, sql_section = CTEParser.extract_system_prompt_and_body(content_no_config)

        # Step 3: parse CTE blocks and SELECT
        cte_blocks, output_columns = CTEParser.parse_file(sql_section)

        # Step 4: render everything through Jinja
        context = {"this": {"name": file_path.stem}}
        rendered_system = self.jinja_env.render(system_prompt, context)

        all_refs: set[str] = set()
        all_sources: set[tuple[str, str]] = set()

        for cte in cte_blocks:
            rendered = self.jinja_env.render(cte.raw_content, context)
            cte.rendered_content = rendered
            is_tool, tools, refs = CTEParser.detect_cte_type(rendered)
            cte.is_tool_step = is_tool
            cte.tool_refs = tools
            cte.model_refs = refs
            all_refs.update(refs)
            all_sources.update(tools)

        # Also collect refs from system prompt (for schema references)
        for m in __import__("re").finditer(r"__REF__([\w/]+)__", rendered_system):
            all_refs.add(m.group(1))
        for m in __import__("re").finditer(r"__SOURCE__(\w+)\.(\w+)__", rendered_system):
            all_sources.add((m.group(1), m.group(2)))

        # Merge config: project defaults < file-level {{ config(...) }}
        merged_cfg: dict = {}
        if self.defaults:
            merged_cfg["model"] = self.defaults.model
            merged_cfg["temperature"] = self.defaults.temperature
            merged_cfg["max_tokens"] = self.defaults.max_tokens
        merged_cfg.update(config_kwargs)
        prompt_config = PromptConfig(**merged_cfg) if merged_cfg else PromptConfig()

        relative_path = file_path.relative_to(relative_to)

        return ParsedPrompt(
            name=file_path.stem,
            file_path=file_path.resolve(),
            relative_path=relative_path,
            config=prompt_config,
            system_prompt=rendered_system,
            cte_blocks=cte_blocks,
            output_columns=output_columns,
            raw_dependencies=all_refs,
            raw_source_refs=all_sources,
        )

    def compile_all(
        self, prompt_files: list[Path], relative_to: Path
    ) -> dict[str, ParsedPrompt]:
        result: dict[str, ParsedPrompt] = {}
        for fp in prompt_files:
            parsed = self.compile_file(fp, relative_to)
            qualified = str(parsed.relative_path.with_suffix("")).replace("\\", "/")
            result[qualified] = parsed
        return result
