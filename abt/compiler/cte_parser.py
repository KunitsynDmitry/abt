"""CTEParser — extracts config blocks, CTE blocks, and SELECT from .prompt files."""

import re

from ..models.prompt import CTEBlock, PromptConfig


class CTEParser:
    # Match {{ config(...) }} — handles nested braces for dict literals
    CONFIG_PATTERN = re.compile(
        r'\{\{\s*config\s*\((.*?)\)\s*\}\}',
        re.DOTALL,
    )

    # Match "name AS TYPE (" — start of a CTE block. TYPE is TOOL or LLM, optional.
    # Group 1: name, Group 2: type (TOOL|LLM|None), Group 3: content after opening paren
    CTE_START = re.compile(
        r'^(?:WITH\s+)?(\w+)\s+AS\s+(TOOL|LLM)\s*\((.*)$',
        re.MULTILINE | re.IGNORECASE,
    )

    # Fallback: "name AS (" without type (backward compat)
    CTE_START_LEGACY = re.compile(
        r'^(?:WITH\s+)?(\w+)\s+AS\s*\((.*)$',
        re.MULTILINE | re.IGNORECASE,
    )

    # Match line starting with WITH (first CTE marker)
    WITH_LINE = re.compile(
        r'^WITH\s+',
        re.MULTILINE | re.IGNORECASE,
    )

    # Match standalone SELECT
    SELECT_START = re.compile(
        r'^\s*SELECT\s*$',
        re.MULTILINE | re.IGNORECASE,
    )

    @classmethod
    def extract_config_dict(cls, content: str) -> tuple[dict, str]:
        """Extract {{ config(...) }} and return (config_kwargs_dict, content_without_config)."""
        match = cls.CONFIG_PATTERN.search(content)
        if not match:
            return {}, content

        config_str = match.group(1)
        kwargs = cls._parse_kwargs(config_str)
        clean = cls.CONFIG_PATTERN.sub("", content, count=1)
        return kwargs, clean

    @classmethod
    def _parse_kwargs(cls, config_str: str) -> dict:
        """Parse keyword arguments from a config string like 'key=value, key2=value2'."""
        kwargs: dict = {}
        if not config_str.strip():
            return kwargs

        # Simple kwarg parser — handles strings, numbers, booleans, lists
        # e.g. temperature=0.7, max_tool_calls=5, allowed_tools=['a','b']
        token_pattern = re.compile(
            r"""(\w+)\s*=\s*(?:
                '([^']*)'|
                "([^"]*)"|
                \[([^\]]*)\]|
                ([^,]+)
            )""",
            re.VERBOSE,
        )
        for m in token_pattern.finditer(config_str):
            key = m.group(1)
            str_val = m.group(2)
            dbl_val = m.group(3)
            list_val = m.group(4)
            raw_val = m.group(5)

            if str_val is not None:
                kwargs[key] = str_val
            elif dbl_val is not None:
                kwargs[key] = dbl_val
            elif list_val is not None:
                items = [i.strip().strip("'\"") for i in list_val.split(",") if i.strip()]
                kwargs[key] = items
            elif raw_val is not None:
                raw_val = raw_val.strip()
                if raw_val.lower() == "true":
                    kwargs[key] = True
                elif raw_val.lower() == "false":
                    kwargs[key] = False
                elif raw_val == "None":
                    kwargs[key] = None
                else:
                    try:
                        kwargs[key] = int(raw_val)
                    except ValueError:
                        try:
                            kwargs[key] = float(raw_val)
                        except ValueError:
                            kwargs[key] = raw_val
        return kwargs

    @classmethod
    def extract_system_prompt_and_body(cls, content: str) -> tuple[str, str]:
        """Extract system prompt (everything before first WITH or SELECT keyword)."""
        lines = content.split("\n")
        system_lines = []
        body_lines = []
        in_body = False

        for line in lines:
            stripped = line.strip()
            if not in_body and (
                cls.WITH_LINE.match(stripped)
                or cls.CTE_START.match(stripped)
                or cls.SELECT_START.match(stripped)
            ):
                in_body = True
            if not in_body:
                system_lines.append(line)
            else:
                body_lines.append(line)

        return "\n".join(system_lines).strip(), "\n".join(body_lines).strip()

    @classmethod
    def parse_file(cls, content: str) -> tuple[list[CTEBlock], list[str]]:
        """
        Parse CTE blocks and SELECT from content.
        Returns: (list of CTEBlock, list of output columns).
        """
        ctes: list[CTEBlock] = []
        output_columns: list[str] = []

        lines = content.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            cte_name = None
            cte_type = None
            after_paren = None

            # Try new explicit syntax first: name AS TOOL|LLM (...)
            cte_match = cls.CTE_START.match(line)
            if cte_match:
                cte_name = cte_match.group(1)
                cte_type = cte_match.group(2).lower()
                after_paren = cte_match.group(3)
            else:
                # Fallback: legacy syntax name AS (...)
                legacy_match = cls.CTE_START_LEGACY.match(line)
                if legacy_match:
                    cte_name = legacy_match.group(1)
                    after_paren = legacy_match.group(2)

            if cte_name:
                # Check if this is a single-line CTE: content and closing paren on same line
                if after_paren and ")" in after_paren:
                    # Single-line CTE: everything between ( and ) is content
                    content_part = after_paren.rsplit(")", 1)[0]
                    raw = content_part.strip()
                else:
                    # Multi-line CTE
                    depth = 1
                    cte_lines = []
                    if after_paren.strip():
                        cte_lines.append(after_paren)
                    i += 1
                    while i < len(lines) and depth > 0:
                        cte_line = lines[i]
                        depth += cte_line.count("(") - cte_line.count(")")
                        if depth <= 0:
                            break
                        cte_lines.append(cte_line)
                        i += 1
                    raw = "\n".join(cte_lines).strip()

                ctes.append(CTEBlock(name=cte_name, raw_content=raw, cte_type=cte_type))
            elif cls.SELECT_START.match(line):
                # Collect SELECT columns
                i += 1
                while i < len(lines):
                    col_line = lines[i].strip()
                    if not col_line or col_line.upper().startswith("FROM "):
                        break
                    col_name = re.split(r'\s*--\s*', col_line)[0].strip().rstrip(",")
                    if col_name:
                        output_columns.append(col_name)
                    i += 1
                break
            i += 1

        return ctes, output_columns

    @classmethod
    def detect_cte_type(cls, rendered_content: str) -> tuple[bool, list[tuple[str, str]], list[str]]:
        """
        Analyze rendered CTE content.
        Returns: (is_tool_step, tool_refs, model_refs).
        """
        is_tool = "__SOURCE__" in rendered_content
        tool_refs: list[tuple[str, str]] = []
        model_refs: list[str] = []

        # Extract tool refs from placeholders
        for m in re.finditer(r"__SOURCE__(\w+)\.(\w+)__", rendered_content):
            tool_refs.append((m.group(1), m.group(2)))

        # Extract model refs (SELECT ... FROM __REF__name__)
        for m in re.finditer(r"__REF__([\w/]+)__", rendered_content):
            model_refs.append(m.group(1))

        return is_tool, tool_refs, model_refs
