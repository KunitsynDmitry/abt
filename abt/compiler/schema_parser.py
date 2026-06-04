"""SchemaParser — parses all schema.yml files into dynamic Pydantic models."""

from ..project import ProjectLoader
from ..models.schema import SchemaFile
from .factory import ModelFactory


class SchemaParser:
    def __init__(self, project_loader: ProjectLoader):
        self.loader = project_loader

    def parse_all(self) -> dict[str, type]:
        schemas: dict[str, type] = {}
        for schema_path in self.loader.list_schema_files():
            schema_file = SchemaFile.from_yaml(schema_path)
            for model_def in schema_file.models:
                model_cls = ModelFactory.create_model_from_schema(model_def)
                schemas[model_def.name] = model_cls
        return schemas

    def resolve_ref(self, model_name: str, all_schemas: dict[str, type]) -> type:
        if model_name not in all_schemas:
            from ...exceptions import SchemaNotFoundError
            raise SchemaNotFoundError(
                f"Schema '{model_name}' not found. "
                f"Available: {list(all_schemas.keys())}"
            )
        return all_schemas[model_name]

    def get_json_schema_for_prompt(self, model_name: str, all_schemas: dict[str, type]) -> str:
        """Return a human-readable JSON schema to inject into a prompt."""
        import json
        model_cls = self.resolve_ref(model_name, all_schemas)
        return json.dumps(ModelFactory.model_to_json_schema(model_cls), indent=2)
