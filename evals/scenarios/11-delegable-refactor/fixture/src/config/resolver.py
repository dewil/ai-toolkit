"""Точка входа: собирает итоговый конфиг из всех слоев."""
import os

from .layers import DefaultsLayer, FileLayer, EnvLayer, RemoteLayer
from .registry import REGISTRY, layer_order
from .validate import validate
from .errors import ConfigError

DEFAULT_PATH = "/etc/service/config.ini"


def resolve(path=None, env=None, fetcher=None):
    env = os.environ if env is None else env
    layers = []
    for name in layer_order():
        factory = REGISTRY[name]
        if name == "file":
            layers.append(factory(path or env.get("SERVICE_CONFIG") or DEFAULT_PATH))
        elif name == "env":
            layers.append(factory(env))
        elif name == "remote":
            layers.append(factory(fetcher))
        else:
            layers.append(factory())
    merged = {}
    for layer in layers:
        chunk = layer.load()
        if chunk is None:
            continue
        for key, value in chunk.items():
            if value is None and key in merged:
                continue
            merged[key] = value
    try:
        return validate(merged)
    except ConfigError:
        raise
