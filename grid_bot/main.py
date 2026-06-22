"""Точка входа: загрузка конфигурации, настройка логов, запуск бота."""

from __future__ import annotations

import signal
import sys

from .bot import GridBot
from .config import ConfigError, load_config
from .logging_setup import setup_logging
from .storage import Storage
from .strategy import build_effective_config


def _install_signal_handlers(logger) -> None:
    """SIGTERM/SIGINT -> KeyboardInterrupt, чтобы сработал штатный shutdown().

    Это нужно, в частности, когда дашборд перезапускает бота: процесс получает
    SIGTERM и должен корректно отменить ордера и отметить себя остановленным.
    """

    def _handler(signum, _frame):
        # Игнорируем дальнейшие сигналы, чтобы shutdown() (отмена ордеров —
        # сетевые вызовы) успел завершиться без повторного KeyboardInterrupt.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        logger.info("Получен сигнал %s — корректно завершаемся.", signum)
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _handler)


def main() -> int:
    """Запустить бот. Возвращает код выхода процесса."""
    logger = setup_logging()
    logger.info("=" * 70)
    logger.info("Запуск grid-бота.")
    _install_signal_handlers(logger)

    try:
        config = load_config()
        # Накладываем управляющие настройки с дашборда (профиль/символы/рынок).
        storage = Storage()
        config = build_effective_config(config, storage.get_controls())
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
        bot = GridBot(config, storage=storage)
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
