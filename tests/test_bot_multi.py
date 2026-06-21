"""Интеграционные тесты портфельного режима (dry-run, фейковая биржа).

Без реальной сети: фейковая биржа отдаёт заранее заданные тикеры/цены.
Главное, что проверяем — инвариант капитала: суммарно по всем живым
символам капитал НИКОГДА не превышает TOTAL_CAPITAL.
"""

from __future__ import annotations

import pytest

from grid_bot.bot import GridBot, SymbolGridInstance
from grid_bot.config import Config
from grid_bot.storage import Storage


def _ticker(symbol, qv, pct, high, low, last):
    return {
        "symbol": symbol, "quoteVolume": qv, "percentage": pct,
        "high": high, "low": low, "last": last,
    }


SAMPLE = {
    "BTC/USDT": _ticker("BTC/USDT", 1_000_000_000, 2.0, 105, 95, 100),
    "ETH/USDT": _ticker("ETH/USDT", 500_000_000, 10.0, 2200, 1800, 2000),
    "DOGE/USDT": _ticker("DOGE/USDT", 200_000_000, 25.0, 0.2, 0.1, 0.15),
}


class FakeExchange:
    """Минимальная заглушка ccxt для dry-run портфельных тестов."""

    def __init__(self, tickers):
        self._tickers = tickers

    def fetch_tickers(self, symbols=None):
        if symbols is None:
            return self._tickers
        return {s: self._tickers[s] for s in symbols if s in self._tickers}

    def fetch_ticker(self, symbol):
        return self._tickers[symbol]


def _multi_config(**overrides) -> Config:
    base = dict(
        exchange_id="binance", api_key=None, api_secret=None, use_testnet=True,
        dry_run=True, symbol="BTC/USDT", lower_price=0.0, upper_price=0.0,
        num_grids=6, total_capital=1000.0, max_drawdown_pct=20.0, poll_seconds=0.0,
        market_type="spot", leverage=1, margin_mode="isolated",
        multi_symbol_mode=True, quote_currency="USDT",
        num_top_volume=2, num_top_gainers=2, num_top_volatile=2,
        max_concurrent_symbols=8, min_24h_volume_usdt=5_000_000.0,
        grid_range_pct=10.0,
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture()
def storage(tmp_path):
    st = Storage(str(tmp_path / "t.db"))
    yield st
    st.close()


def test_initial_build_capital_invariant(storage):
    config = _multi_config()
    bot = GridBot(config, exchange=FakeExchange(SAMPLE), storage=storage)
    bot._initial_build()

    # Собрались все три символа (BTC/ETH=volume, DOGE=gainer).
    assert set(bot.instances.keys()) == {"BTC/USDT", "ETH/USDT", "DOGE/USDT"}
    # ИНВАРИАНТ: суммарный капитал не превышает TOTAL_CAPITAL.
    total = sum(i.capital for i in bot.instances.values())
    assert total <= config.total_capital + 1e-6


def test_capital_weights_favor_volume(storage):
    config = _multi_config()
    bot = GridBot(config, exchange=FakeExchange(SAMPLE), storage=storage)
    bot._initial_build()
    # volume-символам — больший вес, чем gainer.
    btc = bot.instances["BTC/USDT"].capital
    doge = bot.instances["DOGE/USDT"].capital
    assert btc > doge


def test_per_symbol_categories(storage):
    config = _multi_config()
    bot = GridBot(config, exchange=FakeExchange(SAMPLE), storage=storage)
    bot._initial_build()
    assert bot.instances["BTC/USDT"].category == "volume"
    assert bot.instances["DOGE/USDT"].category == "gainer"


def test_volatile_has_tighter_killswitch(storage):
    config = _multi_config()
    inst = SymbolGridInstance(
        config=config, exchange=FakeExchange(SAMPLE), storage=storage,
        symbol="DOGE/USDT", category="volatile", capital=100.0,
        lower_price=90.0, upper_price=110.0,
    )
    volume_inst = SymbolGridInstance(
        config=config, exchange=FakeExchange(SAMPLE), storage=storage,
        symbol="BTC/USDT", category="volume", capital=100.0,
        lower_price=90.0, upper_price=110.0,
    )
    # Трендовая категория — жёстче (меньший допустимый % просадки).
    assert inst.effective_max_drawdown_pct < volume_inst.effective_max_drawdown_pct


def test_multi_run_records_portfolio_and_symbols(storage):
    config = _multi_config()
    bot = GridBot(config, exchange=FakeExchange(SAMPLE), storage=storage)
    bot.run(max_iterations=2)

    # Портфельные снимки equity (symbol IS NULL) записаны.
    assert storage.last_equity() is not None
    # Per-symbol снимки тоже есть.
    assert storage.symbol_equity_bounds("BTC/USDT") is not None
    # universe_history содержит добавления.
    universe = storage.current_universe()
    assert "BTC/USDT" in universe


def test_single_symbol_mode_still_works(storage):
    config = Config(
        exchange_id="binance", api_key=None, api_secret=None, use_testnet=True,
        dry_run=True, symbol="BTC/USDT", lower_price=90.0, upper_price=110.0,
        num_grids=6, total_capital=1000.0, max_drawdown_pct=20.0, poll_seconds=0.0,
        market_type="spot", leverage=1, margin_mode="isolated",
        multi_symbol_mode=False,
    )
    bot = GridBot(config, exchange=FakeExchange(SAMPLE), storage=storage)
    bot.run(max_iterations=2)
    # Один символ, категория manual, капитал = весь total_capital.
    # (после прогона он остаётся в instances, если не остановлен)
    assert storage.last_equity() is not None
    universe = storage.current_universe()
    assert "BTC/USDT" in universe
    assert universe["BTC/USDT"]["category"] == "manual"
