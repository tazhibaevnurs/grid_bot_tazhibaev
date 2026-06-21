"""Настройка логирования: одновременно в файл и в консоль, с таймстампами."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_file: str = "grid_bot.log",
    level: int = logging.INFO,
) -> logging.Logger:
    """Сконфигурировать корневой логгер бота.

    Логи пишутся и в консоль (stdout), и в файл ``log_file``.

    :param log_file: путь к файлу лога.
    :param level: уровень логирования.
    :returns: настроенный логгер ``grid_bot``.
    """
    logger = logging.getLogger("grid_bot")
    logger.setLevel(level)

    # Избегаем дублирования хендлеров при повторном вызове.
    if logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def get_logger(name: str = "grid_bot") -> logging.Logger:
    """Получить дочерний логгер из пространства ``grid_bot``."""
    if name == "grid_bot":
        return logging.getLogger("grid_bot")
    return logging.getLogger(f"grid_bot.{name}")
