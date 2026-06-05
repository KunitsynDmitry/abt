"""TriggerManager — resolves trigger input mappings and activates agent execution."""

from typing import Any

from ..models.trigger import TriggerDefinition


def _resolve_jsonpath(data: dict, expr: str) -> Any:
    """Lightweight JSONPath: supports $.body.key, $.query.param, $.text.

    No dependency needed. Handles nested dict access via dot notation.
    The $. prefix is stripped, then each segment is used as a dict key.
    Returns None if any segment is missing.
    """
    if not expr.startswith("$."):
        return expr
    path = expr[2:]
    current = data
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            return None
    return current


class TriggerManager:
    def __init__(self, triggers: dict[str, TriggerDefinition], executor=None):
        self.triggers = triggers
        self.executor = executor

    def resolve_input(
        self,
        trigger: TriggerDefinition,
        event_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the initial_input dict from a trigger definition and event data.

        Merge order (later overrides earlier):
        1. trigger.input.static — fixed values
        2. trigger.input.mapping — JSONPath from event_data
        3. trigger.input.mode — shorthand (added as {"mode": mode})
        """
        result: dict[str, Any] = {}

        result.update(trigger.input.static)

        event_data = event_data or {}
        for key, path_expr in trigger.input.mapping.items():
            value = _resolve_jsonpath(event_data, path_expr)
            if value is not None:
                result[key] = value

        if trigger.input.mode:
            result["mode"] = trigger.input.mode

        return result

    def activate(
        self,
        trigger_name: str,
        event_data: dict[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Activate a trigger: resolve input, execute the full agent graph."""
        if self.executor is None:
            raise RuntimeError("TriggerManager has no executor bound.")
        trigger = self.triggers[trigger_name]
        initial_input = self.resolve_input(trigger, event_data)
        return self.executor.execute(initial_input, thread_id=thread_id)

    def list_triggers(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "type": t.type.value,
                "description": t.description,
                "schedule": t.schedule,
                "path": t.path,
                "method": t.method,
            }
            for t in self.triggers.values()
        ]
