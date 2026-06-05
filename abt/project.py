"""ProjectLoader — reads and validates an abt project from disk."""

from pathlib import Path

from .exceptions import ProjectNotFoundError, ProjectValidationError
from .models.config import AbtProjectConfig


class ProjectLoader:
    def __init__(self, project_root: Path):
        self.root = Path(project_root).resolve()
        self.config: AbtProjectConfig | None = None
        self._loaded = False

    def load(self) -> AbtProjectConfig:
        config_path = self.root / "abt_project.yml"
        if not config_path.exists():
            raise ProjectNotFoundError(
                f"abt_project.yml not found in {self.root}. Run 'abt init' first."
            )
        self.config = AbtProjectConfig.from_yaml(config_path)
        errors = self.config.validate_project_structure(self.root)
        if errors:
            raise ProjectValidationError("\n".join(errors))
        self._loaded = True
        return self.config

    def list_prompt_files(self) -> list[Path]:
        files = []
        for prompt_path in self.config.paths.prompt_paths:
            base = self.root / prompt_path
            if base.exists():
                files.extend(sorted(base.rglob("*.prompt")))
        return files

    def list_schema_files(self) -> list[Path]:
        files = []
        for schema_path in self.config.paths.schema_paths:
            base = self.root / schema_path
            if base.exists():
                files.extend(sorted(base.rglob("*.yml")))
        return files

    def list_source_files(self) -> list[Path]:
        files = []
        for source_path in self.config.paths.source_paths:
            base = self.root / source_path
            if base.exists():
                files.extend(sorted(base.rglob("*.yml")))
        return files

    def list_macro_files(self) -> list[Path]:
        files = []
        for macro_path in self.config.paths.macro_paths:
            base = self.root / macro_path
            if base.exists():
                files.extend(sorted(base.rglob("*.jinja")))
        return files

    def list_trigger_files(self) -> list[Path]:
        files = []
        for triggers_path in self.config.paths.triggers_paths:
            base = self.root / triggers_path
            if base.exists():
                files.extend(sorted(base.rglob("*.triggers.yml")))
        return files

    def get_target_dir(self) -> Path:
        target = self.root / self.config.paths.target_path
        target.mkdir(parents=True, exist_ok=True)
        return target
