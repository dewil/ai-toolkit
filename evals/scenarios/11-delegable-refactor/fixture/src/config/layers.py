"""Слои конфигурации. Каждый отдает плоский словарь или None."""
from .parser import parse_ini
from .errors import ConfigError

BOOLS = {"1": True, "0": False, "true": True, "false": False, "yes": True, "no": False}


class DefaultsLayer:
    priority = 0

    def load(self):
        return {"host": "127.0.0.1", "port": 8080, "workers": 4,
                "debug": False, "timeout": 30, "queue": None}


class FileLayer:
    priority = 10

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigError(f"конфиг не читается: {self.path}") from exc
        return parse_ini(raw)


class EnvLayer:
    priority = 20
    PREFIX = "SERVICE_"

    def __init__(self, env):
        self.env = env

    def load(self):
        out = {}
        for key, value in self.env.items():
            if not key.startswith(self.PREFIX) or key == "SERVICE_CONFIG":
                continue
            name = key[len(self.PREFIX):].lower()
            out[name] = coerce(value)
        return out or None


class RemoteLayer:
    priority = 30

    def __init__(self, fetcher):
        self.fetcher = fetcher

    def load(self):
        if self.fetcher is None:
            return None
        payload = self.fetcher()
        if not isinstance(payload, dict):
            raise ConfigError("удаленный слой отдал не словарь")
        return {k: coerce(v) if isinstance(v, str) else v for k, v in payload.items()}


def coerce(value):
    low = value.strip().lower()
    if low in BOOLS:
        return BOOLS[low]
    if low.isdigit():
        return int(low)
    return value.strip()
