"""Тесты FIFO-расчёта реализованного P&L."""

from __future__ import annotations

import pytest

from web.pnl import compute_fifo_pnl, portfolio_pnl_metrics


def _t(tid, side, price, amount, symbol="BTC/USDT"):
    return {"id": tid, "symbol": symbol, "side": side, "price": price, "amount": amount}


def test_portfolio_pnl_metrics_uses_equity_delta():
    pnl_abs, pnl_pct, display_eq = portfolio_pnl_metrics(10000.0, 10035.0, 20.0)
    assert pnl_abs == pytest.approx(35.0)
    assert pnl_pct == pytest.approx(0.35)
    assert display_eq == pytest.approx(10035.0)


def test_portfolio_pnl_metrics_falls_back_to_realized_when_equity_flat():
    pnl_abs, pnl_pct, display_eq = portfolio_pnl_metrics(10000.0, 10000.0, 45.65)
    assert pnl_abs == pytest.approx(45.65)
    assert pnl_pct == pytest.approx(0.4565)
    assert display_eq == pytest.approx(10045.65)


def test_portfolio_pnl_metrics_prefers_mark_to_market_when_higher():
    pnl_abs, _, display_eq = portfolio_pnl_metrics(10000.0, 10049.95, 45.65)
    assert pnl_abs == pytest.approx(49.95)
    assert display_eq == pytest.approx(10049.95)


def test_simple_long_pair():
    trades = [
        _t(1, "buy", 100.0, 1.0),
        _t(2, "sell", 110.0, 1.0),
    ]
    pnl = compute_fifo_pnl(trades)
    assert pnl[1] is None              # открывающая покупка
    assert pnl[2] == pytest.approx(10.0)  # (110 - 100) * 1


def test_fifo_matches_oldest_first():
    trades = [
        _t(1, "buy", 100.0, 1.0),
        _t(2, "buy", 120.0, 1.0),
        _t(3, "sell", 130.0, 1.0),  # закрывает покупку по 100 -> +30
        _t(4, "sell", 130.0, 1.0),  # закрывает покупку по 120 -> +10
    ]
    pnl = compute_fifo_pnl(trades)
    assert pnl[1] is None
    assert pnl[2] is None
    assert pnl[3] == pytest.approx(30.0)
    assert pnl[4] == pytest.approx(10.0)


def test_partial_close():
    trades = [
        _t(1, "buy", 100.0, 2.0),
        _t(2, "sell", 110.0, 1.0),  # закрывает половину -> +10
    ]
    pnl = compute_fifo_pnl(trades)
    assert pnl[1] is None
    assert pnl[2] == pytest.approx(10.0)


def test_short_position_futures():
    # Сначала продаём (открываем шорт), потом выкупаем дешевле -> прибыль.
    trades = [
        _t(1, "sell", 100.0, 1.0),
        _t(2, "buy", 90.0, 1.0),  # закрытие шорта: (100 - 90) * 1 = +10
    ]
    pnl = compute_fifo_pnl(trades)
    assert pnl[1] is None
    assert pnl[2] == pytest.approx(10.0)


def test_position_flip():
    # Лонг 1, затем продажа 2: закрывает лонг (+10) и открывает шорт на 1.
    trades = [
        _t(1, "buy", 100.0, 1.0),
        _t(2, "sell", 110.0, 2.0),
        _t(3, "buy", 105.0, 1.0),  # закрывает шорт по 110 -> (110 - 105) = +5
    ]
    pnl = compute_fifo_pnl(trades)
    assert pnl[1] is None
    assert pnl[2] == pytest.approx(10.0)  # закрытая часть лонга
    assert pnl[3] == pytest.approx(5.0)


def test_separate_symbols_isolated():
    trades = [
        _t(1, "buy", 100.0, 1.0, symbol="BTC/USDT"),
        _t(2, "buy", 10.0, 1.0, symbol="ETH/USDT"),
        _t(3, "sell", 110.0, 1.0, symbol="BTC/USDT"),
        _t(4, "sell", 12.0, 1.0, symbol="ETH/USDT"),
    ]
    pnl = compute_fifo_pnl(trades)
    assert pnl[3] == pytest.approx(10.0)
    assert pnl[4] == pytest.approx(2.0)


def test_simple_long_pair_details():
    trades = [
        _t(1, "buy", 100.0, 1.0),
        _t(2, "sell", 110.0, 1.0),
    ]
    from web.pnl import compute_fifo_closure_details

    details = compute_fifo_closure_details(trades)
    assert details[1] is None
    assert details[2]["realized_pnl"] == pytest.approx(10.0)
    assert details[2]["entry_price"] == pytest.approx(100.0)
    assert details[2]["exit_price"] == pytest.approx(110.0)
    assert details[2]["entry_quote_value"] == pytest.approx(100.0)
    assert details[2]["quote_value"] == pytest.approx(110.0)
    assert details[2]["position_side"] == "long"
    assert details[2]["entry_side"] == "buy"
    assert details[2]["exit_side"] == "sell"


def test_short_pair_details():
    trades = [
        _t(1, "sell", 100.0, 1.0),
        _t(2, "buy", 90.0, 1.0),
    ]
    from web.pnl import compute_fifo_closure_details

    details = compute_fifo_closure_details(trades)
    assert details[2]["position_side"] == "short"
    assert details[2]["entry_side"] == "sell"
    assert details[2]["exit_side"] == "buy"
    assert details[2]["entry_price"] == pytest.approx(100.0)
    assert details[2]["exit_price"] == pytest.approx(90.0)


def test_loss_is_negative():
    trades = [
        _t(1, "buy", 100.0, 1.0),
        _t(2, "sell", 90.0, 1.0),
    ]
    pnl = compute_fifo_pnl(trades)
    assert pnl[2] == pytest.approx(-10.0)
