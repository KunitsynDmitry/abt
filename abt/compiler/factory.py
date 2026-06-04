"""ModelFactory — wraps pydantic.create_model() for abt schema definitions."""

from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from ..models.schema import FieldConstraint, SchemaField, SchemaModel

TYPE_MAP: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list[str]": list[str],
    "list[int]": list[int],
    "list[float]": list[float],
    "list[dict]": list[dict],
    "dict": dict,
    "any": Any,
}


class ModelFactory:
    @staticmethod
    def field_type_from_yaml(type_str: str) -> type:
        py_type = TYPE_MAP.get(type_str)
        if py_type is None:
            raise TypeError(
                f"Unknown type '{type_str}'. Available: {list(TYPE_MAP.keys())}"
            )
        return py_type

    @staticmethod
    def constraint_to_field(constraint: FieldConstraint) -> dict[str, Any]:
        """Convert FieldConstraint to pydantic Field() kwargs."""
        kwargs: dict[str, Any] = {}
        if constraint.ge is not None:
            kwargs["ge"] = constraint.ge
        if constraint.le is not None:
            kwargs["le"] = constraint.le
        if constraint.gt is not None:
            kwargs["gt"] = constraint.gt
        if constraint.lt is not None:
            kwargs["lt"] = constraint.lt
        if constraint.multiple_of is not None:
            kwargs["multiple_of"] = constraint.multiple_of
        if constraint.min_length is not None:
            kwargs["min_length"] = constraint.min_length
        if constraint.max_length is not None:
            kwargs["max_length"] = constraint.max_length
        if constraint.regex is not None:
            kwargs["pattern"] = constraint.regex
        return kwargs

    @classmethod
    def create_model_from_schema(cls, schema_model: SchemaModel) -> type:
        fields: dict[str, tuple[type, Any]] = {}
        for field_def in schema_model.fields:
            py_type = cls.field_type_from_yaml(field_def.type)
            kwargs = cls.constraint_to_field(field_def.constraints)
            kwargs["description"] = field_def.description

            # Enum constraint → use Literal[...] as the type
            constraint = field_def.constraints
            if constraint.enum:
                py_type = Literal[tuple(constraint.enum)]  # type: ignore

            if not field_def.required:
                py_type = py_type | None
                kwargs["default"] = field_def.default if field_def.default is not None else None

            field_info = Field(**kwargs)
            fields[field_def.name] = (py_type, field_info)

        model_name = schema_model.name.replace("-", "_").replace(" ", "_")
        return create_model(model_name, **fields, __base__=BaseModel)

    @classmethod
    def model_to_json_schema(cls, model_cls: type) -> dict[str, Any]:
        """Export a Pydantic model to JSON Schema (for embedding in prompts)."""
        return model_cls.model_json_schema()
