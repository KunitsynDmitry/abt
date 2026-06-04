"""AbtJinjaEnv — creates and configures a Jinja2 Environment with abt-specific globals."""

import os
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined, Undefined


class _PlaceholderUndefined(Undefined):
    """Returns __VAR__name__ for undefined variables instead of raising."""

    def __str__(self):
        return f"__VAR__{self._undefined_name}__"

    def __getattr__(self, name):
        return _PlaceholderUndefined(name=f"{self._undefined_name}.{name}")


class AbtJinjaEnv:
    """Custom Jinja environment with abt builtins: ref(), source(), config(), var(), env_var()."""

    def __init__(
        self,
        schema_registry: dict[str, type] | None = None,
        source_registry: dict[str, Any] | None = None,
        project_vars: dict[str, Any] | None = None,
        macro_paths: list[Path] | None = None,
        strict: bool = False,
    ):
        self.schemas = schema_registry or {}
        self.sources = source_registry or {}
        self.vars = project_vars or {}
        self._macro_paths = macro_paths or []
        self.strict = strict

        # Pending state — collected during template rendering
        self._pending_refs: set[str] = set()
        self._pending_source_refs: set[tuple[str, str]] = set()
        self._pending_config: dict[str, Any] = {}

        self._env = self._create_environment()

    def _create_environment(self) -> Environment:
        env = Environment(
            undefined=StrictUndefined if self.strict else _PlaceholderUndefined,
            extensions=["jinja2.ext.do"],
        )
        env.globals["ref"] = self._ref
        env.globals["source"] = self._source
        env.globals["config"] = self._config
        env.globals["var"] = self._var
        env.globals["env_var"] = self._env_var

        for macro_path in self._macro_paths:
            if macro_path.exists():
                with open(macro_path) as f:
                    template = env.parse(f.read())
                    env.globals.update(
                        {k: v for k, v in template.globals.items()}
                    )

        return env

    def _ref(self, model_name: str) -> str:
        """{{ ref('model_name') }} — records a dependency, returns placeholder."""
        self._pending_refs.add(model_name)
        return f"__REF__{model_name}__"

    def _source(self, source_name: str, table_name: str) -> str:
        """{{ source('src', 'table') }} — records tool dependency, returns placeholder."""
        self._pending_source_refs.add((source_name, table_name))
        return f"__SOURCE__{source_name}.{table_name}__"

    def _config(self, **kwargs) -> str:
        """{{ config(...) }} — captures config kwargs, returns empty string."""
        self._pending_config = kwargs
        return ""

    def _var(self, name: str, default: Any = None) -> Any:
        """{{ var('name') }} — resolve project variable."""
        return self.vars.get(name, default)

    def _env_var(self, name: str, default: str = "") -> str:
        """{{ env_var('NAME') }} — resolve environment variable."""
        return os.environ.get(name, default)

    def render(self, template_str: str, context: dict[str, Any] | None = None) -> str:
        template = self._env.from_string(template_str)
        return template.render(**(context or {}))
