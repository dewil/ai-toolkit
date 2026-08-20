"""Реестр слоев: имя -> фабрика. Порядок задается приоритетом."""
from .layers import DefaultsLayer, FileLayer, EnvLayer, RemoteLayer

REGISTRY = {
    "defaults": DefaultsLayer,
    "file": FileLayer,
    "env": EnvLayer,
    "remote": RemoteLayer,
}

DISABLED = set()


def layer_order():
    """Имена слоев от низшего приоритета к высшему."""
    items = [(name, cls.priority) for name, cls in REGISTRY.items() if name not in DISABLED]
    return [name for name, _ in sorted(items, key=lambda pair: pair[1])]


def disable(name):
    DISABLED.add(name)
