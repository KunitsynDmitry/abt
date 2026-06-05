"""Golden tests for DSL parsing: CTE extraction, config, context projection, tool params."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from abt.compiler.cte_parser import CTEParser
from abt.models.prompt import CTEBlock


# ── Config extraction ──────────────────────────────────────────


def test_config_extraction_simple():
    content = "{{ config(temperature=0.3, model='deepseek-chat') }}\nYou are an agent."
    kwargs, clean = CTEParser.extract_config_dict(content)
    assert kwargs == {"temperature": 0.3, "model": "deepseek-chat"}
    assert "config" not in clean


def test_config_extraction_with_lists():
    content = "{{ config(allowed_tools=['a','b'], max_tool_calls=5) }}\nPrompt."
    kwargs, _ = CTEParser.extract_config_dict(content)
    assert kwargs["allowed_tools"] == ["a", "b"]
    assert kwargs["max_tool_calls"] == 5


def test_config_extraction_routing():
    content = '{{ config(route_on="priority", route_when=["high:escalate","medium:auto_order"], route_default="__END__") }}\n'
    kwargs, _ = CTEParser.extract_config_dict(content)
    assert kwargs["route_on"] == "priority"
    assert kwargs["route_when"] == ["high:escalate", "medium:auto_order"]
    assert kwargs["route_default"] == "__END__"


def test_config_extraction_booleans():
    content = "{{ config(approve_when='total > 100') }}\n"
    kwargs, _ = CTEParser.extract_config_dict(content)
    assert kwargs["approve_when"] == "total > 100"


def test_config_not_present():
    content = "No config here.\nJust plain text."
    kwargs, clean = CTEParser.extract_config_dict(content)
    assert kwargs == {}
    assert clean == content


# ── System prompt extraction ───────────────────────────────────


def test_system_prompt_simple():
    content = "You are an agent.\nHelp the user.\n\nWITH step1 AS (\n  content\n)"
    system, body = CTEParser.extract_system_prompt_and_body(content)
    assert "You are an agent" in system
    assert "WITH step1" in body


def test_system_prompt_select_only():
    content = "You are helpful.\n\nSELECT\n    result\nFROM step"
    system, body = CTEParser.extract_system_prompt_and_body(content)
    assert "You are helpful" in system
    assert "SELECT" in body


# ── CTE parsing ────────────────────────────────────────────────


def test_cte_single_line_tool():
    content = "WITH fetch_data AS TOOL (\n  SELECT * FROM __SOURCE__api.table__\n)"
    ctes, cols = CTEParser.parse_file(content)
    assert len(ctes) == 1
    assert ctes[0].name == "fetch_data"
    assert ctes[0].cte_type == "tool"
    assert cols == []


def test_cte_single_line_llm():
    content = "WITH analyze AS LLM (\n  Think about the data.\n)"
    ctes, _ = CTEParser.parse_file(content)
    assert len(ctes) == 1
    assert ctes[0].name == "analyze"
    assert ctes[0].cte_type == "llm"


def test_cte_legacy_syntax():
    content = "WITH step1 AS (\n  content without type\n)"
    ctes, _ = CTEParser.parse_file(content)
    assert len(ctes) == 1
    assert ctes[0].name == "step1"
    assert ctes[0].cte_type is None


def test_cte_multi_line():
    content = """WITH step1 AS LLM (
      Line 1
      Line 2
      Line 3
    )"""
    ctes, _ = CTEParser.parse_file(content)
    assert len(ctes) == 1
    assert "Line 1" in ctes[0].raw_content
    assert "Line 3" in ctes[0].raw_content


def test_cte_multiple_blocks():
    content = """WITH first AS TOOL (
      tool content
    ),
    second AS LLM (
      llm content
    )"""
    ctes, _ = CTEParser.parse_file(content)
    assert len(ctes) == 2
    assert ctes[0].name == "first"
    assert ctes[0].cte_type == "tool"
    assert ctes[1].name == "second"
    assert ctes[1].cte_type == "llm"


def test_cte_no_cte_blocks():
    content = "Just a plain prompt, no CTE blocks at all."
    ctes, cols = CTEParser.parse_file(content)
    assert ctes == []
    assert cols == []


# ── SELECT parsing ─────────────────────────────────────────────


def test_select_columns():
    content = """WITH step1 AS LLM (
      Analyze the data.
    )
    SELECT
        stock_status,       -- str: OK | LOW | CRITICAL
        total_order_cost,   -- float
        priority            -- str: low | medium | high
    FROM step1"""
    ctes, cols = CTEParser.parse_file(content)
    assert cols == ["stock_status", "total_order_cost", "priority"]


def test_select_without_comments():
    content = """SELECT
        field_a,
        field_b
    FROM last_step"""
    _, cols = CTEParser.parse_file(content)
    assert cols == ["field_a", "field_b"]


# ── Context projection parsing ─────────────────────────────────


def test_context_projection_full():
    content = "SELECT article_id, competitor_price\nFROM __REF__market_research__\nWHERE confidence_score > 0.8"
    proj = CTEParser.parse_context_projection(content)
    assert proj is not None
    assert proj.columns == ["article_id", "competitor_price"]
    assert proj.ref_name == "market_research"
    assert len(proj.conditions) == 1
    assert proj.conditions[0].field == "confidence_score"
    assert proj.conditions[0].op == ">"
    assert proj.conditions[0].value == 0.8


def test_context_projection_string_where():
    content = "SELECT *\nFROM __REF__inventory__\nWHERE location = 'WH-A'"
    proj = CTEParser.parse_context_projection(content)
    assert proj is not None
    assert proj.ref_name == "inventory"
    assert proj.conditions[0].field == "location"
    assert proj.conditions[0].value == "WH-A"


def test_context_projection_multiple_where_and():
    content = "SELECT a, b\nFROM __REF__data__\nWHERE score > 0.5 AND status = 'active'"
    proj = CTEParser.parse_context_projection(content)
    assert proj is not None
    assert len(proj.conditions) == 2
    assert proj.logic == "AND"


def test_context_projection_multiple_where_or():
    content = "SELECT x\nFROM __REF__data__\nWHERE flag = true OR flag = false"
    proj = CTEParser.parse_context_projection(content)
    assert proj is not None
    assert len(proj.conditions) == 2
    assert proj.logic == "OR"


def test_context_projection_raw_jinja_ref():
    content = "SELECT col1, col2\nFROM {{ ref('source_node') }}\nWHERE active = true"
    proj = CTEParser.parse_context_projection(content)
    assert proj is not None
    assert proj.ref_name == "source_node"
    assert proj.conditions[0].value is True


def test_context_projection_no_match():
    content = "Just a plain LLM prompt without SELECT/FROM/WHERE."
    proj = CTEParser.parse_context_projection(content)
    assert proj is None


def test_context_projection_no_where():
    content = "SELECT a, b, c\nFROM __REF__some_node__"
    proj = CTEParser.parse_context_projection(content)
    assert proj is not None
    assert proj.columns == ["a", "b", "c"]
    assert proj.conditions == []


def test_context_projection_no_select():
    content = "FROM __REF__node__\nWHERE status = 'ok'"
    proj = CTEParser.parse_context_projection(content)
    assert proj is not None
    assert proj.columns == []
    assert len(proj.conditions) == 1


# ── Detect CTE type ────────────────────────────────────────────


def test_detect_tool_type():
    content = "__SOURCE__warehouse_api.current_stock__"
    is_tool, tools, refs = CTEParser.detect_cte_type(content)
    assert is_tool
    assert ("warehouse_api", "current_stock") in tools


def test_detect_llm_type():
    content = "__REF__previous_step__ and analysis text"
    is_tool, tools, refs = CTEParser.detect_cte_type(content)
    assert not is_tool
    assert "previous_step" in refs


def test_detect_mixed_type():
    content = "__SOURCE__api.data__\n__REF__other_node__"
    is_tool, tools, refs = CTEParser.detect_cte_type(content)
    assert is_tool
    assert len(tools) == 1
    assert len(refs) == 1


def test_detect_plain_text():
    content = "Just a plain LLM prompt."
    is_tool, tools, refs = CTEParser.detect_cte_type(content)
    assert not is_tool
    assert tools == []
    assert refs == []
