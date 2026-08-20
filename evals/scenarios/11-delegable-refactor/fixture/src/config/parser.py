"""Разбор ini-подобного текста в плоский словарь."""
from .errors import ConfigError


def parse_ini(text):
    out, section = {}, ""
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if "=" not in line:
            raise ConfigError(f"строка {lineno}: нет разделителя '='")
        key, value = line.split("=", 1)
        key = key.strip().lower()
        if section:
            key = f"{section}.{key}"
        out[key] = value.strip()
    return out
