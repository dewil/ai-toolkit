"""Проверка собранного конфига и приведение типов."""
from .errors import ConfigError

REQUIRED = ("host", "port")
INT_FIELDS = ("port", "workers", "timeout")


def validate(merged):
    for field in REQUIRED:
        if merged.get(field) in (None, ""):
            raise ConfigError(f"не задано обязательное поле {field}")
    for field in INT_FIELDS:
        if field not in merged:
            continue
        try:
            merged[field] = int(merged[field])
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"поле {field} должно быть числом") from exc
    if not 1 <= merged["port"] <= 65535:
        raise ConfigError("порт вне диапазона")
    if merged.get("queue") and "." not in str(merged["queue"]):
        merged["queue"] = f"default.{merged['queue']}"
    return merged
