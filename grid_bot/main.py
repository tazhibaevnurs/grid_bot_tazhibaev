"""Точка входа: загрузка конфигурации, настройка логов, запуск бота."""

from __future__ import annotations

import sys

from .bot import GridBot
from .config import ConfigError, load_config
from .logging_setup import setup_logging


def main() -> int:
    """Запустить бот. Возвращает код выхода процесса."""
    logger = setup_logging()
    logger.info("=" * 70)
    logger.info("Запуск grid-бота.")

    try:
        config = load_config()
    except ConfigError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        return 2

    if not config.dry_run:
        logger.warning(
            "ВНИМАНИЕ: DRY_RUN=false — бот будет выставлять РЕАЛЬНЫЕ ордера "
            "на %s (testnet=%s). Убедитесь, что это намеренно.",
            config.exchange_id,
            config.use_testnet,
        )

    try:
        bot = GridBot(config)
        bot.run()
    except ConfigError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.exception("Необработанная ошибка во время работы бота: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
