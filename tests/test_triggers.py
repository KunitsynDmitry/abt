"""Tests for trigger models, parser, and manager."""

import tempfile
from pathlib import Path


class TestTriggerModels:
    """Test TriggerFile.from_yaml() parses correctly."""

    def test_parses_schedule_trigger(self):
        from abt.models.trigger import TriggerFile, TriggerType
        yaml_content = """
version: 1
agent: test_agent
triggers:
  - name: daily
    type: schedule
    schedule: "0 9 * * *"
    input:
      mode: full_scan
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            path = Path(f.name)
        try:
            tf = TriggerFile.from_yaml(path)
            assert tf.version == 1
            assert tf.agent == "test_agent"
            assert len(tf.triggers) == 1
            t = tf.triggers[0]
            assert t.name == "daily"
            assert t.type == TriggerType.SCHEDULE
            assert t.schedule == "0 9 * * *"
            assert t.input.mode == "full_scan"
        finally:
            path.unlink()

    def test_parses_webhook_trigger(self):
        from abt.models.trigger import TriggerFile, TriggerType
        yaml_content = """
version: 1
agent: test_agent
triggers:
  - name: alert
    type: webhook
    path: "/hooks/alert"
    method: POST
    input:
      mapping:
        sku: "$.body.product_id"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            path = Path(f.name)
        try:
            tf = TriggerFile.from_yaml(path)
            t = tf.triggers[0]
            assert t.type == TriggerType.WEBHOOK
            assert t.path == "/hooks/alert"
            assert t.method == "POST"
            assert t.input.mapping == {"sku": "$.body.product_id"}
        finally:
            path.unlink()

    def test_default_values(self):
        from abt.models.trigger import TriggerFile, TriggerType
        yaml_content = """
version: 1
agent: test_agent
triggers:
  - name: simple
    type: message
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml_content)
            path = Path(f.name)
        try:
            tf = TriggerFile.from_yaml(path)
            t = tf.triggers[0]
            assert t.type == TriggerType.MESSAGE
            assert t.description == ""
            assert t.input.mapping == {}
            assert t.input.static == {}
            assert t.input.mode is None
        finally:
            path.unlink()


class TestJsonPathResolution:
    """Test the lightweight JSONPath resolver."""

    def test_resolves_dot_path(self):
        from abt.runtime.trigger_manager import _resolve_jsonpath
        data = {"body": {"sku": "SKU-123", "quantity": 50}}
        assert _resolve_jsonpath(data, "$.body.sku") == "SKU-123"
        assert _resolve_jsonpath(data, "$.body.quantity") == 50

    def test_returns_none_for_missing_path(self):
        from abt.runtime.trigger_manager import _resolve_jsonpath
        data = {"body": {}}
        assert _resolve_jsonpath(data, "$.body.nonexistent") is None

    def test_top_level_key(self):
        from abt.runtime.trigger_manager import _resolve_jsonpath
        data = {"text": "hello world"}
        assert _resolve_jsonpath(data, "$.text") == "hello world"

    def test_literal_returns_self(self):
        from abt.runtime.trigger_manager import _resolve_jsonpath
        assert _resolve_jsonpath({}, "not-a-path") == "not-a-path"

    def test_query_params(self):
        from abt.runtime.trigger_manager import _resolve_jsonpath
        data = {"query": {"token": "abc123", "limit": "10"}}
        assert _resolve_jsonpath(data, "$.query.token") == "abc123"
        assert _resolve_jsonpath(data, "$.query.limit") == "10"


class TestTriggerInputResolution:
    """Test TriggerManager.resolve_input()."""

    def test_mode_only(self):
        from abt.models.trigger import TriggerDefinition, TriggerType, TriggerInput
        from abt.runtime.trigger_manager import TriggerManager
        td = TriggerDefinition(
            name="test",
            type=TriggerType.SCHEDULE,
            schedule="0 9 * * *",
            input=TriggerInput(mode="full_scan"),
        )
        tm = TriggerManager({"test": td})
        result = tm.resolve_input(td, {})
        assert result == {"mode": "full_scan"}

    def test_mapping_overrides_static(self):
        from abt.models.trigger import TriggerDefinition, TriggerType, TriggerInput
        from abt.runtime.trigger_manager import TriggerManager
        td = TriggerDefinition(
            name="test",
            type=TriggerType.WEBHOOK,
            path="/hook",
            input=TriggerInput(
                mapping={"product_id": "$.body.sku"},
                static={"product_id": "fallback", "env": "prod"},
            ),
        )
        tm = TriggerManager({"test": td})
        result = tm.resolve_input(td, {"body": {"sku": "SKU-999"}})
        assert result["product_id"] == "SKU-999"
        assert result["env"] == "prod"

    def test_mapping_with_missing_key_excluded(self):
        from abt.models.trigger import TriggerDefinition, TriggerType, TriggerInput
        from abt.runtime.trigger_manager import TriggerManager
        td = TriggerDefinition(
            name="test",
            type=TriggerType.WEBHOOK,
            path="/hook",
            input=TriggerInput(
                mapping={"product_id": "$.body.nonexistent"},
                static={"env": "prod"},
            ),
        )
        tm = TriggerManager({"test": td})
        result = tm.resolve_input(td, {"body": {}})
        assert "product_id" not in result
        assert result["env"] == "prod"

    def test_static_only_no_event(self):
        from abt.models.trigger import TriggerDefinition, TriggerType, TriggerInput
        from abt.runtime.trigger_manager import TriggerManager
        td = TriggerDefinition(
            name="test",
            type=TriggerType.MESSAGE,
            input=TriggerInput(static={"channel": "slack", "priority": "high"}),
        )
        tm = TriggerManager({"test": td})
        result = tm.resolve_input(td, None)
        assert result == {"channel": "slack", "priority": "high"}
