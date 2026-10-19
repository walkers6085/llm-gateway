"""Маленький валидатор подмножества JSON Schema.

Один и тот же код проверяет настройки (settings.json / профили сервера) и аргументы,
которые модель передаёт инструментам. Поддерживается: type (строка или список),
properties, required, additionalProperties (bool или схема), items, enum,
minimum/maximum, minLength. Остальные ключи схемы (description, default, …) игнорируются.
"""

TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


class SchemaError(ValueError):
    """Ошибка проверки; `path` — путь до значения вида `shell.timeout` или `servers.github.url`."""

    def __init__(self, path: str, message: str):
        self.path, self.message = path, message
        super().__init__(f"{path or '<корень>'}: {message}")


def _join(path: str, key) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def validate(value, schema: dict, path: str = "") -> None:
    """Бросает SchemaError на первой найденной ошибке."""
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        if not any(TYPES[name](value) for name in names):
            raise SchemaError(path, f"ожидается {' | '.join(names)}, получено {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(path, f"допустимые значения: {', '.join(map(repr, schema['enum']))}")
    if TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(path, f"не меньше {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(path, f"не больше {schema['maximum']}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        raise SchemaError(path, f"длина не меньше {schema['minLength']}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], _join(path, i))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaError(_join(path, key), "обязательное поле")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                validate(item, props[key], _join(path, key))
            elif extra is False:
                raise SchemaError(_join(path, key), "неизвестный ключ")
            elif isinstance(extra, dict):
                validate(item, extra, _join(path, key))
