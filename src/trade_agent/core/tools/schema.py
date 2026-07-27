"""首版 tool JSON schema 的确定性本地校验器。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from trade_agent.core.llm.contracts import JsonValue


class SchemaValidationError(ValueError):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


class JsonSchemaValidator:
    """校验项目 tool schema 使用的受控 JSON Schema 子集。"""

    def validate(
        self,
        value: JsonValue | Mapping[str, JsonValue],
        schema: Mapping[str, JsonValue],
        *,
        path: str = "$",
    ) -> None:
        expected = schema.get("type")
        if expected is not None and not self._matches_type(value, expected):
            raise SchemaValidationError(path, f"类型必须是 {expected}")

        enum = schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            raise SchemaValidationError(path, "值不在允许枚举中")

        if isinstance(value, str):
            minimum = schema.get("minLength")
            if isinstance(minimum, int) and len(value) < minimum:
                raise SchemaValidationError(path, f"长度不能小于 {minimum}")

        if isinstance(value, Mapping):
            self._validate_object(value, schema, path)
        elif isinstance(value, list):
            items = schema.get("items")
            if isinstance(items, Mapping):
                for index, item in enumerate(value):
                    self.validate(item, items, path=f"{path}[{index}]")

    def _validate_object(
        self,
        value: Mapping[str, JsonValue],
        schema: Mapping[str, JsonValue],
        path: str,
    ) -> None:
        required = schema.get("required")
        if isinstance(required, list):
            missing = [key for key in required if isinstance(key, str) and key not in value]
            if missing:
                raise SchemaValidationError(path, f"缺少字段: {', '.join(missing)}")

        properties_value = schema.get("properties")
        properties: Mapping[str, JsonValue] = (
            properties_value if isinstance(properties_value, Mapping) else {}
        )
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise SchemaValidationError(path, f"包含未知字段: {', '.join(unknown)}")
        for key, item in value.items():
            item_schema = properties.get(key)
            if isinstance(item_schema, Mapping):
                self.validate(item, item_schema, path=f"{path}.{key}")

    @staticmethod
    def _matches_type(value: object, expected: JsonValue) -> bool:
        expected_types: Sequence[JsonValue] = (
            expected if isinstance(expected, list) else (expected,)
        )
        return any(JsonSchemaValidator._matches_single_type(value, item) for item in expected_types)

    @staticmethod
    def _matches_single_type(value: object, expected: JsonValue) -> bool:
        if expected == "object":
            return isinstance(value, Mapping)
        if expected == "array":
            return isinstance(value, list)
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "null":
            return value is None
        return False
