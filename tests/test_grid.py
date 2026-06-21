"""Тесты чистой математики сетки (модуль grid)."""

from __future__ import annotations

import math

import pytest

from grid_bot.grid import (
    GridLevel,
    Side,
    allocate_capital_by_weights,
    amount_for_level,
    auto_price_range,
    build_grid_levels,
    build_initial_orders,
    capital_per_level,
    grid_step,
    required_capital,
)


def test_build_grid_levels_count_and_bounds():
    levels = build_grid_levels(100.0, 200.0, 5)
    assert len(levels) == 5
    assert levels[0].price == pytest.approx(100.0)
    assert levels[-1].price == pytest.approx(200.0)
    assert [lv.index for lv in levels] == [0, 1, 2, 3, 4]


def test_build_grid_levels_even_spacing():
    levels = build_grid_levels(100.0, 200.0, 5)
    prices = [lv.price for lv in levels]
    assert prices == pytest.approx([100.0, 125.0, 150.0, 175.0, 200.0])


def test_grid_step():
    assert grid_step(100.0, 200.0, 5) == pytest.approx(25.0)


@pytest.mark.parametrize(
    "lower,upper,n",
    [
        (0.0, 100.0, 5),     # lower <= 0
        (-1.0, 100.0, 5),    # отрицательная цена
        (200.0, 100.0, 5),   # lower >= upper
        (100.0, 200.0, 1),   # num_grids < 2
    ],
)
def test_build_grid_levels_invalid(lower, upper, n):
    with pytest.raises(ValueError):
        build_grid_levels(lower, upper, n)


def test_capital_per_level():
    assert capital_per_level(1000.0, 10) == pytest.approx(100.0)


def test_capital_per_level_invalid():
    with pytest.raises(ValueError):
        capital_per_level(0.0, 10)
    with pytest.raises(ValueError):
        capital_per_level(1000.0, 1)


def test_amount_for_level():
    assert amount_for_level(100.0, 50.0) == pytest.approx(2.0)


def test_amount_for_level_invalid_price():
    with pytest.raises(ValueError):
        amount_for_level(100.0, 0.0)


def test_build_initial_orders_sides():
    levels = build_grid_levels(100.0, 200.0, 5)  # 100,125,150,175,200
    current_price = 150.0
    orders = build_initial_orders(levels, current_price, total_capital=1000.0)

    buys = [o for o in orders if o.side == Side.BUY]
    sells = [o for o in orders if o.side == Side.SELL]

    # Ниже 150: 100, 125 -> buy. Выше 150: 175, 200 -> sell. 150 пропущен.
    assert sorted(o.price for o in buys) == pytest.approx([100.0, 125.0])
    assert sorted(o.price for o in sells) == pytest.approx([175.0, 200.0])
    assert len(orders) == 4


def test_build_initial_orders_amount_uses_capital_per_level():
    levels = build_grid_levels(100.0, 200.0, 5)
    orders = build_initial_orders(levels, 150.0, total_capital=1000.0)
    cpl = capital_per_level(1000.0, 5)  # 200 на уровень
    buy_at_100 = next(o for o in orders if o.price == pytest.approx(100.0))
    assert buy_at_100.amount == pytest.approx(cpl / 100.0)


def test_required_capital_only_counts_buys():
    levels = build_grid_levels(100.0, 200.0, 5)
    orders = build_initial_orders(levels, 150.0, total_capital=1000.0)
    needed = required_capital(orders)
    cpl = capital_per_level(1000.0, 5)
    # Два buy-ордера, каждый ровно на cpl котируемой валюты.
    assert needed == pytest.approx(2 * cpl)
    assert needed <= 1000.0


# --- auto_price_range ------------------------------------------------------


def test_auto_price_range_symmetric():
    lower, upper = auto_price_range(100.0, 12.0)
    assert lower == pytest.approx(88.0)
    assert upper == pytest.approx(112.0)
    # Текущая цена строго внутри диапазона.
    assert lower < 100.0 < upper


@pytest.mark.parametrize("price,pct", [(0.0, 12.0), (-5.0, 12.0), (100.0, 0.0), (100.0, 100.0)])
def test_auto_price_range_invalid(price, pct):
    with pytest.raises(ValueError):
        auto_price_range(price, pct)


# --- allocate_capital_by_weights (инвариант капитала) ----------------------


def test_allocate_capital_proportional():
    weights = {"A": 1.0, "B": 0.5, "C": 0.5}
    alloc = allocate_capital_by_weights(weights, 2000.0)
    assert alloc["A"] == pytest.approx(1000.0)
    assert alloc["B"] == pytest.approx(500.0)
    assert alloc["C"] == pytest.approx(500.0)


def test_allocate_capital_never_exceeds_total():
    # Ключевой инвариант безопасности: сумма выделенного <= TOTAL_CAPITAL.
    weights = {"A": 1.0, "B": 0.5, "C": 0.4, "D": 0.1}
    total = 1000.0
    alloc = allocate_capital_by_weights(weights, total)
    assert sum(alloc.values()) <= total + 1e-9
    assert sum(alloc.values()) == pytest.approx(total)


def test_allocate_capital_zero_weights_get_nothing():
    weights = {"A": 1.0, "B": 0.0}
    alloc = allocate_capital_by_weights(weights, 1000.0)
    assert alloc["A"] == pytest.approx(1000.0)
    assert alloc["B"] == pytest.approx(0.0)


def test_allocate_capital_all_zero_weights():
    alloc = allocate_capital_by_weights({"A": 0.0, "B": 0.0}, 1000.0)
    assert alloc == {"A": 0.0, "B": 0.0}


def test_allocate_capital_invalid_total():
    with pytest.raises(ValueError):
        allocate_capital_by_weights({"A": 1.0}, 0.0)
