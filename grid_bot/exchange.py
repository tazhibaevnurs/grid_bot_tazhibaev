"""Обёртка над ccxt: создание инстанса биржи, resolve_symbol, leverage/margin.

Вся логика про различия Binance/Bybit и spot/futures (формат символа,
``defaultType``/``category``) сосредоточена здесь, чтобы в остальном коде
не было разбросанных if-ветвлений по бирже и типу рынка.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import ccxt

from .config import Config
from .logging_setup import get_logger

logger = get_logger("exchange")


def _default_type_for(config: Config) -> str:
    """ccxt ``options.defaultType`` для выбранного типа рынка.

    Для futures на обеих биржах используем ``swap`` (бессрочные контракты,
    linear perpetual). Для spot — ``spot``.
    """
    return "swap" if config.is_futures else "spot"


def build_exchange(config: Config) -> ccxt.Exchange:
    """Создать и сконфигурировать ccxt-инстанс биржи.

    Настраивает ключи, testnet (sandbox), ``defaultType`` и
    ``defaultMarginMode``. Реальные ключи не логируются.

    :param config: конфигурация бота.
    :returns: готовый ccxt-инстанс.
    """
    exchange_class = getattr(ccxt, config.exchange_id)

    # timeout в миллисекундах: не даём запросам к бирже «висеть» минутами
    # (без этого ccxt может ждать очень долго при сбоях testnet/сети).
    params: Dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": 15000,
        "options": {
            "defaultType": _default_type_for(config),
        },
    }
    if config.api_key and config.api_secret:
        params["apiKey"] = config.api_key
        params["secret"] = config.api_secret

    # Для Bybit futures полезно заранее сообщить категорию (linear).
    if config.exchange_id == "bybit" and config.is_futures:
        params["options"]["defaultSubType"] = "linear"

    exchange: ccxt.Exchange = exchange_class(params)

    if config.use_testnet:
        try:
            exchange.set_sandbox_mode(True)
            logger.info("Включён testnet/sandbox режим для %s.", config.exchange_id)
        except Exception as exc:  # noqa: BLE001 - хотим понятный лог
            logger.warning(
                "Не удалось включить sandbox для %s: %s", config.exchange_id, exc
            )

    logger.info(
        "Создан инстанс биржи: id=%s, market_type=%s, testnet=%s (ключи %s)",
        config.exchange_id,
        config.market_type,
        config.use_testnet,
        "заданы" if config.api_key else "не заданы",
    )
    return exchange


def resolve_symbol_str(config: Config, symbol: str) -> str:
    """Привести произвольный символ к формату выбранного рынка ccxt.

    Для spot возвращается ``BASE/QUOTE`` (например ``BTC/USDT``).
    Для futures (linear perpetual) — ``BASE/QUOTE:QUOTE`` (``BTC/USDT:USDT``).
    Единая точка преобразования и для одного тикера, и для мультирежима,
    чтобы не плодить if-ы по коду.

    :param config: конфигурация бота.
    :param symbol: исходный символ (например из скринера или из ``SYMBOL``).
    :returns: символ в формате, понятном выбранному рынку ccxt.
    """
    symbol = symbol.strip().upper()

    if config.is_spot:
        return symbol

    # futures / swap: нужен суффикс settle-валюты, если его ещё нет.
    if ":" in symbol:
        return symbol

    if "/" in symbol:
        _, quote = symbol.split("/", 1)
    else:
        quote = "USDT"
    return f"{symbol}:{quote}"


def resolve_symbol(exchange: ccxt.Exchange, config: Config) -> str:
    """Привести ``SYMBOL`` из конфига к корректному виду (режим одного тикера).

    :param exchange: ccxt-инстанс (для совместимости сигнатуры).
    :param config: конфигурация бота.
    :returns: символ в формате, понятном выбранному рынку ccxt.
    """
    return resolve_symbol_str(config, config.symbol)


def setup_futures_market(exchange: ccxt.Exchange, config: Config, symbol: str) -> None:
    """Выставить плечо и режим маржи для futures-режима.

    Вызывается при старте. Ошибки биржи перехватываются и логируются
    понятным сообщением, но НЕ роняют бот — некоторые биржи отклоняют
    повторную установку тех же параметров.

    :param exchange: ccxt-инстанс.
    :param config: конфигурация бота.
    :param symbol: уже разрешённый через :func:`resolve_symbol` символ.
    """
    if not config.is_futures:
        return

    # Режим маржи.
    try:
        exchange.set_margin_mode(config.margin_mode, symbol)
        logger.info("Установлен margin mode=%s для %s.", config.margin_mode, symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Не удалось установить margin mode=%s для %s: %s "
            "(возможно, уже установлен или не поддерживается).",
            config.margin_mode,
            symbol,
            exc,
        )

    # Плечо.
    try:
        exchange.set_leverage(config.leverage, symbol)
        logger.info("Установлено плечо=%sx для %s.", config.leverage, symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Не удалось установить плечо=%sx для %s: %s "
            "(проверьте лимиты биржи и режим позиции).",
            config.leverage,
            symbol,
            exc,
        )


def _price_from_ticker(ticker: Dict[str, Any]) -> Optional[float]:
    """Извлечь цену из тикера: last -> close -> середина bid/ask."""
    price: Optional[float] = ticker.get("last") or ticker.get("close")
    if price is None:
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        if bid and ask:
            price = (bid + ask) / 2
    return float(price) if price is not None else None


def fetch_current_price(exchange: ccxt.Exchange, symbol: str) -> float:
    """Получить текущую цену (last) по символу через ticker.

    :raises RuntimeError: если цена недоступна.
    """
    ticker = exchange.fetch_ticker(symbol)
    price = _price_from_ticker(ticker)
    if price is None:
        raise RuntimeError(f"Не удалось получить текущую цену для {symbol}.")
    return price


def fetch_prices(exchange: ccxt.Exchange, symbols: list[str]) -> Dict[str, float]:
    """Получить цены для нескольких символов.

    Сначала один батч ``fetch_tickers(symbols)``; при ошибке или пропусках —
    деградация на ``fetch_ticker`` по одному символу (медленнее, но надёжнее).
    """
    if not symbols:
        return {}

    prices: Dict[str, float] = {}
    try:
        tickers = exchange.fetch_tickers(symbols)
        for symbol in symbols:
            ticker = tickers.get(symbol)
            if not ticker:
                continue
            price = _price_from_ticker(ticker)
            if price is not None and price > 0:
                prices[symbol] = price
    except Exception as exc:  # noqa: BLE001
        logger.warning("Батч fetch_tickers не удался: %s", exc)

    missing = [s for s in symbols if s not in prices]
    if missing:
        if prices:
            logger.info(
                "Батч цен: %d/%d символов — догружаем по одному.",
                len(prices),
                len(symbols),
            )
        for symbol in missing:
            try:
                prices[symbol] = fetch_current_price(exchange, symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось получить цену %s: %s", symbol, exc)
    return prices
