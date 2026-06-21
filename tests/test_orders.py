"""Тесты симулятора ордеров (DryRunExecutor)."""

from __future__ import annotations

import pytest

from grid_bot.grid import GridOrder, Side
from grid_bot.orders import DryRunExecutor


def test_buy_fills_when_price_drops():
    executor = DryRunExecutor(total_capital=1000.0)
    executor.place_limit_order(GridOrder(Side.BUY, price=100.0, amount=1.0, level_index=0))

    # Цена выше — не исполняется.
    assert executor.detect_fills(current_price=110.0) == []
    # Цена опустилась до уровня — исполняется.
    fills = executor.detect_fills(current_price=100.0)
    assert len(fills) == 1
    assert fills[0].side == Side.BUY
    assert executor.base_position == pytest.approx(1.0)
    assert executor.quote_balance == pytest.approx(900.0)


def test_sell_fills_when_price_rises():
    executor = DryRunExecutor(total_capital=1000.0)
    executor.place_limit_order(GridOrder(Side.SELL, price=200.0, amount=1.0, level_index=4))

    assert executor.detect_fills(current_price=150.0) == []
    fills = executor.detect_fills(current_price=200.0)
    assert len(fills) == 1
    assert fills[0].side == Side.SELL


def test_filled_orders_removed_from_open():
    executor = DryRunExecutor(total_capital=1000.0)
    executor.place_limit_order(GridOrder(Side.BUY, price=100.0, amount=1.0, level_index=0))
    assert len(executor.open_orders) == 1
    executor.detect_fills(current_price=100.0)
    assert executor.open_orders == []


def test_cancel_all_clears_orders():
    executor = DryRunExecutor(total_capital=1000.0)
    executor.place_limit_order(GridOrder(Side.BUY, price=100.0, amount=1.0, level_index=0))
    executor.place_limit_order(GridOrder(Side.SELL, price=200.0, amount=1.0, level_index=4))
    executor.cancel_all()
    assert executor.open_orders == []


def test_equity_reflects_position_value():
    executor = DryRunExecutor(total_capital=1000.0)
    # Без сделок equity == капиталу.
    assert executor.fetch_equity(current_price=150.0) == pytest.approx(1000.0)
    executor.place_limit_order(GridOrder(Side.BUY, price=100.0, amount=1.0, level_index=0))
    executor.detect_fills(current_price=100.0)
    # Купили 1 ед. по 100, остаток 900 + позиция 1*цена.
    assert executor.fetch_equity(current_price=100.0) == pytest.approx(1000.0)
    assert executor.fetch_equity(current_price=120.0) == pytest.approx(1020.0)
