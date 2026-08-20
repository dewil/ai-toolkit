"""Запуск сервиса."""
from config import resolve
from config.errors import ConfigError


def main():
    try:
        cfg = resolve()
    except ConfigError as exc:
        raise SystemExit(f"конфиг: {exc}")
    print(f"слушаю {cfg['host']}:{cfg['port']} воркеров {cfg['workers']}")


if __name__ == "__main__":
    main()
