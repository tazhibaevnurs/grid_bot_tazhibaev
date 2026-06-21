"""Скринер тикеров: получение и ранжирование рынков с биржи через ccxt.

Отбирает портфель из трёх категорий:
- ``volume``   — самые объёмные (24ч quoteVolume);
- ``gainer``   — самые выросшие за 24ч (percentage);
- ``volatile`` — самые волатильные за 24ч ((high-low)/last).

ВАЖНОЕ ПРЕДУПРЕЖДЕНИЕ (см. README): grid-стратегия зарабатывает на
колебаниях ВНУТРИ диапазона и теряет на устойчивом тренде. Категории
"gainer" и "volatile" — это почти по определению тикеры в сильном тренде
или с резкими движениями, то есть наименее подходящие условия для grid.
Поэтому в боте им даётся меньший вес капитала и более жёсткий kill switch
(см. ``CAPITAL_WEIGHT_*`` в конфиге). Здесь скринер только отбирает —
взвешивание капитала и риск-контроль живут в bot.py/risk.py.

Логика фильтрации и ранжирования вынесена в чистые функции (на вход — обычный
словарь тикеров), чтобы покрыть тестами без реальных вызовов биржи.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

import ccxt

from .config import Config
from .logging_setup import get_logger

logger = get_logger("screener")

# Приоритет категорий при пересечении (тикер попал в несколько списков).
CATEGORY_PRIORITY = ("volume", "gainer", "volatile")


def fetch_all_tickers(exchange: ccxt.Exchange) -> Dict[str, Any]:
    """Получить ВСЕ тикеры одним запросом.

    Намеренно используется ``fetch_tickers()`` (один запрос на все рынки),
    а не ``fetch_ticker`` по одному — это критично для лимитов запросов
    при работе с десятками символов.

    :param exchange: ccxt-инстанс.
    :returns: словарь ``{symbol: ticker}``.
    """
    return exchange.fetch_tickers()


def split_base_quote(symbol: str) -> tuple[str, str]:
    """Разобрать символ на (base, quote). ``BTC/USDT:USDT`` -> (``BTC``, ``USDT``)."""
    if "/" not in symbol:
        return symbol.upper(), ""
    base, rest = symbol.split("/", 1)
    quote = rest.split(":", 1)[0]
    return base.upper(), quote.upper()


def _is_leveraged_token(base: str, patterns: Sequence[str]) -> bool:
    """Эвристика плечевого токена: база ОКАНЧИВАЕТСЯ на один из паттернов.

    Используется endswith (а не вхождение), чтобы реже задевать обычные
    монеты. Примеры исключаемых: ``BTCUP``, ``ETHDOWN``, ``ADABULL``, ``BTC3L``.
    """
    base_up = base.upper()
    for pat in patterns:
        pat_up = pat.upper()
        if pat_up and base_up != pat_up and base_up.endswith(pat_up):
            return True
    return False


def filter_tickers(
    tickers: Mapping[str, Any],
    *,
    quote_currency: str,
    stablecoins: Sequence[str],
    leveraged_token_patterns: Sequence[str],
    min_24h_volume_usdt: float,
    exclude_symbols: Sequence[str],
) -> Dict[str, Any]:
    """Отфильтровать тикеры перед ранжированием.

    Отбрасываются: пары не с ``quote_currency``; стейбл-к-стейблу;
    плечевые токены; неликвид (< ``min_24h_volume_usdt``); чёрный список.

    :returns: новый словарь ``{symbol: ticker}`` только из подходящих пар.
    """
    quote_up = quote_currency.upper()
    stable_set = {s.upper() for s in stablecoins}
    exclude_set = {s.upper() for s in exclude_symbols}

    result: Dict[str, Any] = {}
    for symbol, ticker in tickers.items():
        if not symbol or "/" not in symbol:
            continue
        base, quote = split_base_quote(symbol)

        # Только нужная котируемая валюта.
        if quote != quote_up:
            continue
        # Чёрный список (по полному символу или по базе).
        if symbol.upper() in exclude_set or base in exclude_set:
            continue
        # Стейбл-к-стейблу (например USDC/USDT) — нет волатильности для grid.
        if base in stable_set:
            continue
        # Плечевые токены — синтетика, крайне рискованно для grid.
        if _is_leveraged_token(base, leveraged_token_patterns):
            continue
        # Порог ликвидности.
        quote_volume = _safe_float(ticker.get("quoteVolume"))
        if quote_volume is None or quote_volume < min_24h_volume_usdt:
            continue

        result[symbol] = ticker
    return result


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ticker_volatility_pct(ticker: Mapping[str, Any]) -> float | None:
    """Быстрая оценка волатильности: ``(high - low) / last * 100`` без доп. запросов."""
    high = _safe_float(ticker.get("high"))
    low = _safe_float(ticker.get("low"))
    last = _safe_float(ticker.get("last")) or _safe_float(ticker.get("close"))
    if high is None or low is None or last is None or last <= 0:
        return None
    return (high - low) / last * 100.0


def top_by_volume(tickers: Mapping[str, Any], n: int) -> List[str]:
    """Топ-N символов по 24ч quoteVolume (убывание)."""
    if n <= 0:
        return []
    rows = []
    for symbol, t in tickers.items():
        vol = _safe_float(t.get("quoteVolume"))
        if vol is not None:
            rows.append((symbol, vol))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:n]]


def top_gainers(tickers: Mapping[str, Any], n: int) -> List[str]:
    """Топ-N символов по 24ч % изменения цены (убывание)."""
    if n <= 0:
        return []
    rows = []
    for symbol, t in tickers.items():
        pct = _safe_float(t.get("percentage"))
        if pct is not None:
            rows.append((symbol, pct))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:n]]


def top_volatility(tickers: Mapping[str, Any], n: int) -> List[str]:
    """Топ-N символов по амплитуде колебаний за 24ч (убывание)."""
    if n <= 0:
        return []
    rows = []
    for symbol, t in tickers.items():
        vol = ticker_volatility_pct(t)
        if vol is not None:
            rows.append((symbol, vol))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:n]]


def merge_categories(
    by_volume: Sequence[str],
    by_gainers: Sequence[str],
    by_volatile: Sequence[str],
    max_symbols: int,
) -> Dict[str, str]:
    """Слить три списка в ``{symbol: category}`` с приоритетом volume > gainer > volatile.

    При пересечении символ получает категорию с наивысшим приоритетом.
    Итоговое число символов ограничено ``max_symbols`` (приоритетные — первыми).
    """
    ranked: Dict[str, str] = {}
    for category, symbols in (
        ("volume", by_volume),
        ("gainer", by_gainers),
        ("volatile", by_volatile),
    ):
        for symbol in symbols:
            if symbol not in ranked:
                ranked[symbol] = category
            if len(ranked) >= max_symbols:
                return ranked
    return ranked


def build_universe(exchange: ccxt.Exchange, config: Config) -> Dict[str, str]:
    """Собрать финальный портфель ``{symbol: category}`` с биржи.

    Делает один запрос ``fetch_tickers``, фильтрует и ранжирует по трём
    категориям, объединяет с приоритетом и ограничивает
    ``MAX_CONCURRENT_SYMBOLS``.

    :param exchange: ccxt-инстанс.
    :param config: конфигурация бота.
    :returns: словарь ``{symbol: category}``.
    """
    raw = fetch_all_tickers(exchange)
    filtered = filter_tickers(
        raw,
        quote_currency=config.quote_currency,
        stablecoins=config.stablecoins,
        leveraged_token_patterns=config.leveraged_token_patterns,
        min_24h_volume_usdt=config.min_24h_volume_usdt,
        exclude_symbols=config.exclude_symbols,
    )
    by_volume = top_by_volume(filtered, config.num_top_volume)
    by_gainers = top_gainers(filtered, config.num_top_gainers)
    by_volatile = top_volatility(filtered, config.num_top_volatile)
    universe = merge_categories(
        by_volume, by_gainers, by_volatile, config.max_concurrent_symbols
    )
    logger.info(
        "Universe собран: %d символов (volume=%d, gainer=%d, volatile=%d) "
        "из %d отфильтрованных (%d сырых).",
        len(universe),
        sum(1 for c in universe.values() if c == "volume"),
        sum(1 for c in universe.values() if c == "gainer"),
        sum(1 for c in universe.values() if c == "volatile"),
        len(filtered),
        len(raw),
    )
    return universe
