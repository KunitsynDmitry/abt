"""TriggerParser — reads .triggers.yml files and resolves trigger definitions."""

from ..project import ProjectLoader
from ..models.trigger import TriggerFile, TriggerDefinition
from ..exceptions import AbtError


class TriggerParser:
    def __init__(self, project_loader: ProjectLoader):
        self.loader = project_loader

    def parse_all(self) -> dict[str, TriggerDefinition]:
        triggers: dict[str, TriggerDefinition] = {}
        for trigger_path in self.loader.list_trigger_files():
            trigger_file = TriggerFile.from_yaml(trigger_path)
            for trigger_def in trigger_file.triggers:
                if trigger_def.name in triggers:
                    raise AbtError(
                        f"Trigger '{trigger_def.name}' is defined in multiple files. "
                        f"Trigger names must be globally unique."
                    )
                triggers[trigger_def.name] = trigger_def
        return triggers

    def resolve_trigger(
        self, name: str, all_triggers: dict[str, TriggerDefinition]
    ) -> TriggerDefinition:
        if name not in all_triggers:
            raise AbtError(
                f"Trigger '{name}' not found. "
                f"Available: {list(all_triggers.keys())}"
            )
        return all_triggers[name]
