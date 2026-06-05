"""Trigger models — declarative agent activation sources (dbt exposures pattern).

Triggers describe *what activates* an agent: schedules, webhooks, messages.
"""

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class TriggerType(str, Enum):
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    MESSAGE = "message"


class TriggerInput(BaseModel):
    """Resolved input for agent activation. Three merge mechanisms:

    - mode: literal mode string (e.g. 'full_scan')
    - mapping: JSONPath-like mapping from event data to agent input keys
    - static: fixed key-value pairs

    Merge order (later overrides): static → mapping → mode.
    """

    mode: str | None = None
    mapping: dict[str, str] = Field(default_factory=dict)
    static: dict[str, Any] = Field(default_factory=dict)


class TriggerDefinition(BaseModel):
    name: str
    type: TriggerType
    description: str = ""
    schedule: str = ""       # cron expression (schedule only)
    path: str = ""           # URL path (webhook only)
    method: str = "POST"     # HTTP method (webhook only)
    input: TriggerInput = Field(default_factory=TriggerInput)


class TriggerFile(BaseModel):
    version: int = 1
    agent: str = ""
    triggers: list[TriggerDefinition] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path) -> "TriggerFile":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)
