"""Тесты скринера тикеров (фильтрация и ранжирование) без реальных вызовов биржи."""

from __future__ import annotations

import pytest

from grid_bot.config import Config
from grid_bot.screener import (
    build_universe,
    filter_tickers,
    is_perpetual_futures_symbol,
    merge_categories,
    split_base_quote,
    ticker_volatility_pct,
    top_by_volume,
    top_gainers,
    top_volatility,
)


def _ticker(symbol, qv, pct, high, low, last):
    return {
        "symbol": symbol,
        "quoteVolume": qv,
        "percentage": pct,
        "high": high,
        "low": low,
        "last": last,
    }


@pytest.fixture()
def sample_tickers():
    return {
        "BTC/USDT": _ticker("BTC/USDT", 1_000_000_000, 2.0, 105, 95, 100),
        "ETH/USDT": _ticker("ETH/USDT", 500_000_000, 10.0, 2200, 1800, 2000),
        "DOGE/USDT": _ticker("DOGE/USDT", 200_000_000, 25.0, 0.2, 0.1, 0.15),
        # Стейбл к стейблу — должен быть исключён.
        "USDC/USDT": _ticker("USDC/USDT", 9_000_000_000, 0.0, 1.001, 0.999, 1.0),
        # Плечевой токен — должен быть исключён.
        "BTCUP/USDT": _ticker("BTCUP/USDT", 300_000_000, 40.0, 10, 5, 8),
        # Низкая ликвидность — должен быть исключён.
        "SMALL/USDT": _ticker("SMALL/USDT", 1000, 50.0, 2, 1, 1.5),
        # Не та котируемая валюта — должен быть исключён.
        "ADA/BTC": _ticker("ADA/BTC", 1_000_000_000, 5.0, 0.00002, 0.00001, 0.000015),
    }


DEFAULTS = dict(
    quote_currency="USDT",
    stablecoins=("USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD"),
    leveraged_token_patterns=("UP", "DOWN", "BULL", "BEAR", "3L", "3S"),
    min_24h_volume_usdt=5_000_000.0,
    exclude_symbols=(),
)


def test_split_base_quote():
    assert split_base_quote("BTC/USDT") == ("BTC", "USDT")
    assert split_base_quote("BTC/USDT:USDT") == ("BTC", "USDT")
    assert split_base_quote("ETH/BTC") == ("ETH", "BTC")


def test_is_perpetual_futures_symbol():
    assert is_perpetual_futures_symbol("BTC/USDT") is True
    assert is_perpetual_futures_symbol("BTC/USDT:USDT") is True
    assert is_perpetual_futures_symbol("BTC/USDT:USDT-260327") is False
    assert is_perpetual_futures_symbol("ETH/USDT:USDT-251226") is False


def test_build_universe_excludes_dated_futures(sample_tickers):
    tickers = {
        **sample_tickers,
        "BTC/USDT:USDT": _ticker("BTC/USDT:USDT", 800_000_000, 3.0, 105, 95, 100),
        "BTC/USDT:USDT-260327": _ticker(
            "BTC/USDT:USDT-260327", 900_000_000, 5.0, 106, 94, 101
        ),
    }
    config = _multi_config(market_type="futures", leverage=2)
    exchange = _FakeExchange(tickers)
    universe = build_universe(exchange, config)
    assert "BTC/USDT:USDT-260327" not in universe
    assert "BTC/USDT:USDT" in universe or "BTC/USDT" in universe


def test_filter_excludes_unwanted(sample_tickers):
    filtered = filter_tickers(sample_tickers, **DEFAULTS)
    assert set(filtered.keys()) == {"BTC/USDT", "ETH/USDT", "DOGE/USDT"}


def test_filter_explicit_exclude(sample_tickers):
    opts = {**DEFAULTS, "exclude_symbols": ("DOGE",)}
    filtered = filter_tickers(sample_tickers, **opts)
    assert "DOGE/USDT" not in filtered
    assert "BTC/USDT" in filtered


def test_min_volume_threshold(sample_tickers):
    opts = {**DEFAULTS, "min_24h_volume_usdt": 600_000_000.0}
    filtered = filter_tickers(sample_tickers, **opts)
    # Только BTC (1e9) проходит порог 6e8.
    assert set(filtered.keys()) == {"BTC/USDT"}


def test_ticker_volatility_pct():
    assert ticker_volatility_pct(_ticker("X/USDT", 1, 0, 110, 90, 100)) == pytest.approx(20.0)
    assert ticker_volatility_pct({"high": None, "low": 1, "last": 2}) is None


def test_top_by_volume(sample_tickers):
    filtered = filter_tickers(sample_tickers, **DEFAULTS)
    assert top_by_volume(filtered, 2) == ["BTC/USDT", "ETH/USDT"]


def test_top_gainers(sample_tickers):
    filtered = filter_tickers(sample_tickers, **DEFAULTS)
    assert top_gainers(filtered, 2) == ["DOGE/USDT", "ETH/USDT"]


def test_top_volatility(sample_tickers):
    filtered = filter_tickers(sample_tickers, **DEFAULTS)
    # Волатильность: DOGE ~66.7%, ETH 20%, BTC 10%.
    assert top_volatility(filtered, 2) == ["DOGE/USDT", "ETH/USDT"]


def test_top_n_zero_returns_empty(sample_tickers):
    filtered = filter_tickers(sample_tickers, **DEFAULTS)
    assert top_by_volume(filtered, 0) == []


def test_merge_categories_priority():
    merged = merge_categories(
        by_volume=["BTC/USDT", "ETH/USDT"],
        by_gainers=["DOGE/USDT", "ETH/USDT"],
        by_volatile=["DOGE/USDT", "ETH/USDT"],
        max_symbols=8,
    )
    # ETH и DOGE пересекаются: приоритет volume > gainer > volatile.
    assert merged == {
        "BTC/USDT": "volume",
        "ETH/USDT": "volume",
        "DOGE/USDT": "gainer",
    }


def test_merge_categories_respects_max():
    merged = merge_categories(
        by_volume=["A", "B", "C"],
        by_gainers=["D", "E"],
        by_volatile=["F"],
        max_symbols=4,
    )
    assert len(merged) == 4
    assert merged["A"] == "volume"


def _multi_config(**overrides) -> Config:
    base = dict(
        exchange_id="binance", api_key=None, api_secret=None, use_testnet=True,
        dry_run=True, symbol="BTC/USDT", lower_price=0.0, upper_price=0.0,
        num_grids=10, total_capital=1000.0, max_drawdown_pct=20.0, poll_seconds=10.0,
        market_type="spot", leverage=1, margin_mode="isolated",
        multi_symbol_mode=True, quote_currency="USDT",
        num_top_volume=2, num_top_gainers=2, num_top_volatile=2,
        max_concurrent_symbols=8, min_24h_volume_usdt=5_000_000.0,
    )
    base.update(overrides)
    return Config(**base)


class _FakeExchange:
    def __init__(self, tickers):
        self._tickers = tickers

    def fetch_tickers(self, symbols=None):
        return self._tickers


def test_build_universe(sample_tickers):
    config = _multi_config()
    exchange = _FakeExchange(sample_tickers)
    universe = build_universe(exchange, config)
    assert universe == {
        "BTC/USDT": "volume",
        "ETH/USDT": "volume",
        "DOGE/USDT": "gainer",
    }


def test_build_universe_respects_max_concurrent(sample_tickers):
    config = _multi_config(max_concurrent_symbols=2)
    exchange = _FakeExchange(sample_tickers)
    universe = build_universe(exchange, config)
    assert len(universe) == 2
