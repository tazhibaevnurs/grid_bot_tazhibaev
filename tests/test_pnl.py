"""Тесты FIFO-расчёта реализованного P&L."""

from __future__ import annotations

import pytest

from web.pnl import compute_fifo_pnl


def _t(tid, side, price, amount, symbol="BTC/USDT"):
    return {"id": tid, "symbol": symbol, "side": side, "price": price, "amount": amount}


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


def test_loss_is_negative():
    trades = [
        _t(1, "buy", 100.0, 1.0),
        _t(2, "sell", 90.0, 1.0),
    ]
    pnl = compute_fifo_pnl(trades)
    assert pnl[2] == pytest.approx(-10.0)
