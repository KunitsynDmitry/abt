"""SourceParser — parses sources.yml files into validated SourceDefinition objects."""

from ..project import ProjectLoader
from ..models.source import SourceFile, SourceDefinition, SourceTable
from ..exceptions import DuplicateSourceError, SourceNotFoundError


class SourceParser:
    def __init__(self, project_loader: ProjectLoader):
        self.loader = project_loader

    def parse_all(self) -> dict[str, SourceDefinition]:
        sources: dict[str, SourceDefinition] = {}
        for source_path in self.loader.list_source_files():
            source_file = SourceFile.from_yaml(source_path)
            for source_def in source_file.sources:
                if source_def.name in sources:
                    raise DuplicateSourceError(
                        f"Source '{source_def.name}' is defined in multiple files. "
                        f"Source names must be globally unique."
                    )
                sources[source_def.name] = source_def
        return sources

    def resolve_table(
        self,
        source_name: str,
        table_name: str,
        all_sources: dict[str, SourceDefinition],
    ) -> SourceTable:
        if source_name not in all_sources:
            raise SourceNotFoundError(
                f"Source '{source_name}' not found. "
                f"Available: {list(all_sources.keys())}"
            )
        source = all_sources[source_name]
        for table in source.tables:
            if table.name == table_name:
                return table
        raise SourceNotFoundError(
            f"Table '{table_name}' not found in source '{source_name}'. "
            f"Available tables: {[t.name for t in source.tables]}"
        )
