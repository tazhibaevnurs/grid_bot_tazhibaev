"""Тесты SQLite-хранилища."""

from __future__ import annotations

import pytest

from grid_bot.storage import Storage


@pytest.fixture()
def storage(tmp_path):
    db = tmp_path / "test.db"
    st = Storage(str(db))
    yield st
    st.close()


def test_record_and_read_trade(storage):
    tid = storage.record_trade(
        exchange="binance",
        symbol="BTC/USDT",
        market_type="spot",
        side="buy",
        price=100.0,
        amount=2.0,
        order_id="sim-1",
        dry_run=True,
        leverage=1,
    )
    assert tid > 0
    trades = storage.get_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t["side"] == "buy"
    assert t["quote_value"] == pytest.approx(200.0)
    assert t["dry_run"] == 1


def test_filters_and_count(storage):
    storage.record_trade(exchange="binance", symbol="BTC/USDT", market_type="spot",
                         side="buy", price=100.0, amount=1.0, order_id="1", dry_run=True)
    storage.record_trade(exchange="binance", symbol="BTC/USDT", market_type="spot",
                         side="sell", price=110.0, amount=1.0, order_id="2", dry_run=True)
    storage.record_trade(exchange="binance", symbol="ETH/USDT", market_type="spot",
                         side="buy", price=10.0, amount=1.0, order_id="3", dry_run=True)

    assert storage.count_trades() == 3
    assert storage.count_trades(side="buy") == 2
    assert storage.count_trades(symbol="ETH/USDT") == 1
    assert len(storage.get_trades(side="sell")) == 1


def test_pagination(storage):
    for i in range(5):
        storage.record_trade(exchange="binance", symbol="BTC/USDT", market_type="spot",
                             side="buy", price=100.0 + i, amount=1.0, order_id=str(i),
                             dry_run=True)
    page1 = storage.get_trades(limit=2, offset=0)
    page2 = storage.get_trades(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert page1[0]["id"] != page2[0]["id"]


def test_equity_snapshots(storage):
    storage.record_equity_snapshot(equity=1000.0, quote_currency="USDT")
    storage.record_equity_snapshot(equity=1100.0, quote_currency="USDT")
    storage.record_equity_snapshot(equity=950.0, quote_currency="USDT")
    assert storage.first_equity()["equity"] == pytest.approx(1000.0)
    assert storage.last_equity()["equity"] == pytest.approx(950.0)
    assert storage.peak_equity() == pytest.approx(1100.0)
    assert len(storage.get_equity_history()) == 3


def test_events(storage):
    storage.record_event("info", "Бот запущен.")
    storage.record_event("error", "KILL SWITCH сработал.")
    events = storage.get_events()
    assert len(events) == 2
    # Новые сверху.
    assert events[0]["level"] == "error"


def test_process_heartbeat_and_stop(storage):
    storage.upsert_process_heartbeat("grid_bot", 12345, detail="BTC/USDT")
    row = storage.get_process("grid_bot")
    assert row is not None
    assert row["pid"] == 12345
    assert row["status"] == "running"
    assert row["detail"] == "BTC/USDT"

    storage.upsert_process_heartbeat("grid_bot", 12345, detail="orders=10")
    row2 = storage.get_process("grid_bot")
    assert row2["started_at"] == row["started_at"]

    storage.mark_process_stopped("grid_bot")
    row3 = storage.get_process("grid_bot")
    assert row3["status"] == "stopped"


def test_get_processes(storage):
    storage.upsert_process_heartbeat("grid_bot", 1)
    storage.upsert_process_heartbeat("dashboard", 2)
    processes = storage.get_processes()
    assert len(processes) == 2
    names = {p["name"] for p in processes}
    assert names == {"grid_bot", "dashboard"}


def test_trade_category_default_and_explicit(storage):
    storage.record_trade(exchange="binance", symbol="BTC/USDT", market_type="spot",
                         side="buy", price=100.0, amount=1.0, order_id="1", dry_run=True)
    storage.record_trade(exchange="binance", symbol="ETH/USDT", market_type="spot",
                         side="buy", price=10.0, amount=1.0, order_id="2", dry_run=True,
                         category="gainer")
    trades = {t["symbol"]: t for t in storage.get_trades()}
    assert trades["BTC/USDT"]["category"] == "manual"
    assert trades["ETH/USDT"]["category"] == "gainer"


def test_per_symbol_equity_snapshots(storage):
    # Портфельные снимки (symbol IS NULL) и per-symbol — раздельно.
    storage.record_equity_snapshot(equity=1000.0, quote_currency="USDT")  # portfolio
    storage.record_equity_snapshot(equity=600.0, quote_currency="USDT", symbol="BTC/USDT")
    storage.record_equity_snapshot(equity=650.0, quote_currency="USDT", symbol="BTC/USDT")
    storage.record_equity_snapshot(equity=1050.0, quote_currency="USDT")  # portfolio

    # Портфельные методы видят только symbol IS NULL.
    assert storage.first_equity()["equity"] == pytest.approx(1000.0)
    assert storage.last_equity()["equity"] == pytest.approx(1050.0)
    # equity-curve по умолчанию только портфельный (2 точки).
    assert len(storage.get_equity_history()) == 2

    bounds = storage.symbol_equity_bounds("BTC/USDT")
    assert bounds["start_equity"] == pytest.approx(600.0)
    assert bounds["current_equity"] == pytest.approx(650.0)


def test_control_settings(storage):
    assert storage.get_control("risk_profile") is None
    assert storage.get_control("risk_profile", "balanced") == "balanced"
    storage.set_control("risk_profile", "aggressive")
    storage.set_control("max_symbols", "10")
    assert storage.get_control("risk_profile") == "aggressive"
    # upsert обновляет значение.
    storage.set_control("risk_profile", "conservative")
    assert storage.get_control("risk_profile") == "conservative"
    controls = storage.get_controls()
    assert controls["max_symbols"] == "10"
    assert controls["risk_profile"] == "conservative"


def test_universe_history_and_current(storage):
    storage.record_universe_change("BTC/USDT", "volume", "added")
    storage.record_universe_change("ETH/USDT", "gainer", "added")
    storage.record_universe_change("ETH/USDT", "gainer", "wind_down")
    storage.record_universe_change("DOGE/USDT", "volatile", "added")
    storage.record_universe_change("DOGE/USDT", "volatile", "removed")

    current = storage.current_universe()
    assert current["BTC/USDT"]["status"] == "active"
    assert current["ETH/USDT"]["status"] == "wind_down"
    assert "DOGE/USDT" not in current  # removed

    history = storage.get_universe_history()
    assert len(history) == 5
    assert history[0]["symbol"] == "DOGE/USDT"  # новые сверху
