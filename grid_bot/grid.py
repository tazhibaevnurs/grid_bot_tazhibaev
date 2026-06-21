"""Чистая математика grid-сетки.

Этот модуль НЕ зависит от ccxt и от сети — только арифметика. Благодаря
этому его легко покрыть unit-тестами. Здесь строятся уровни сетки,
рассчитывается капитал на уровень и формируется начальный набор ордеров.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Mapping, Tuple


class Side(str, Enum):
    """Сторона ордера."""

    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class GridLevel:
    """Один ценовой уровень сетки."""

    index: int
    price: float


@dataclass
class GridOrder:
    """Ордер сетки (как намерение, без привязки к конкретной бирже).

    :param side: покупка или продажа.
    :param price: цена лимитного ордера.
    :param amount: количество базового актива (например, BTC).
    :param level_index: индекс уровня сетки, к которому привязан ордер.
    """

    side: Side
    price: float
    amount: float
    level_index: int


def build_grid_levels(
    lower_price: float,
    upper_price: float,
    num_grids: int,
) -> List[GridLevel]:
    """Построить равномерные ценовые уровни сетки.

    Уровни располагаются линейно (арифметическая прогрессия) от
    ``lower_price`` до ``upper_price`` включительно. ``num_grids`` —
    это количество уровней (точек), то есть интервалов будет ``num_grids - 1``.

    :param lower_price: нижняя граница диапазона (> 0).
    :param upper_price: верхняя граница диапазона (> lower_price).
    :param num_grids: количество уровней (>= 2).
    :returns: список уровней, отсортированный по возрастанию цены.
    :raises ValueError: при некорректных аргументах.
    """
    if lower_price <= 0 or upper_price <= 0:
        raise ValueError("Цены должны быть > 0.")
    if lower_price >= upper_price:
        raise ValueError("lower_price должна быть строго меньше upper_price.")
    if num_grids < 2:
        raise ValueError("num_grids должно быть >= 2.")

    step = (upper_price - lower_price) / (num_grids - 1)
    return [
        GridLevel(index=i, price=lower_price + step * i)
        for i in range(num_grids)
    ]


def grid_step(lower_price: float, upper_price: float, num_grids: int) -> float:
    """Вернуть шаг сетки (расстояние между соседними уровнями)."""
    if num_grids < 2:
        raise ValueError("num_grids должно быть >= 2.")
    return (upper_price - lower_price) / (num_grids - 1)


def auto_price_range(current_price: float, range_pct: float) -> Tuple[float, float]:
    """Автоматически рассчитать диапазон сетки вокруг текущей цены.

    В мультитикерном режиме руками выставить ``LOWER_PRICE``/``UPPER_PRICE``
    для каждого символа невозможно, поэтому диапазон строится симметрично:
    ``lower = price * (1 - range_pct/100)``, ``upper = price * (1 + range_pct/100)``.

    :param current_price: текущая рыночная цена (> 0).
    :param range_pct: половина ширины диапазона в процентах, ``0 < range_pct < 100``.
    :returns: кортеж ``(lower, upper)``.
    :raises ValueError: при некорректных аргументах.
    """
    if current_price <= 0:
        raise ValueError("current_price должна быть > 0.")
    if not (0 < range_pct < 100):
        raise ValueError("range_pct должен быть в диапазоне (0, 100).")
    lower = current_price * (1.0 - range_pct / 100.0)
    upper = current_price * (1.0 + range_pct / 100.0)
    return lower, upper


def capital_per_level(total_capital: float, num_grids: int) -> float:
    """Рассчитать капитал (в котируемой валюте) на один уровень.

    Капитал распределяется поровну между уровнями. Используется
    ``num_grids`` уровней как делитель — это консервативно (резервирует
    немного капитала), что лучше для соблюдения лимита ``TOTAL_CAPITAL``.

    :param total_capital: общий доступный капитал в котируемой валюте.
    :param num_grids: количество уровней сетки.
    :returns: капитал на один уровень.
    :raises ValueError: при некорректных аргументах.
    """
    if total_capital <= 0:
        raise ValueError("total_capital должен быть > 0.")
    if num_grids < 2:
        raise ValueError("num_grids должно быть >= 2.")
    return total_capital / num_grids


def amount_for_level(capital_per_level_value: float, price: float) -> float:
    """Перевести капитал на уровне в количество базового актива.

    :param capital_per_level_value: капитал (в котируемой валюте) на уровень.
    :param price: цена уровня.
    :returns: количество базового актива (amount = capital / price).
    :raises ValueError: если цена <= 0.
    """
    if price <= 0:
        raise ValueError("price должна быть > 0.")
    return capital_per_level_value / price


def build_initial_orders(
    levels: List[GridLevel],
    current_price: float,
    total_capital: float,
) -> List[GridOrder]:
    """Сформировать стартовый набор ордеров относительно текущей цены.

    Правило grid-бота: уровни НИЖЕ текущей цены — это лимитные ордера на
    покупку, уровни ВЫШЕ — лимитные ордера на продажу. Уровень, совпадающий
    с текущей ценой (если попал точно), пропускается.

    :param levels: уровни сетки (из :func:`build_grid_levels`).
    :param current_price: текущая рыночная цена.
    :param total_capital: общий капитал в котируемой валюте.
    :returns: список :class:`GridOrder`.
    """
    cpl = capital_per_level(total_capital, len(levels))
    orders: List[GridOrder] = []
    for level in levels:
        if level.price < current_price:
            amount = amount_for_level(cpl, level.price)
            orders.append(
                GridOrder(
                    side=Side.BUY,
                    price=level.price,
                    amount=amount,
                    level_index=level.index,
                )
            )
        elif level.price > current_price:
            amount = amount_for_level(cpl, level.price)
            orders.append(
                GridOrder(
                    side=Side.SELL,
                    price=level.price,
                    amount=amount,
                    level_index=level.index,
                )
            )
        # level.price == current_price -> пропускаем, чтобы не ставить
        # ордер прямо по рынку.
    return orders


def required_capital(orders: List[GridOrder]) -> float:
    """Оценить капитал (в котируемой валюте), необходимый для buy-ордеров.

    Учитываются только ордера на покупку: именно они требуют котируемой
    валюты прямо сейчас. Это нужно для проверки лимита ``TOTAL_CAPITAL``.
    """
    return sum(o.price * o.amount for o in orders if o.side == Side.BUY)


def allocate_capital_by_weights(
    weights_by_symbol: Mapping[str, float],
    total_capital: float,
) -> Dict[str, float]:
    """Распределить капитал между символами пропорционально весам.

    Инвариант (критично для безопасности): сумма выделенного капитала НИКОГДА
    не превышает ``total_capital``. При положительной сумме весов капитал
    распределяется полностью (сумма == ``total_capital`` с точностью float);
    символы с нулевым/отрицательным весом получают 0.

    :param weights_by_symbol: словарь ``{symbol: weight}`` (веса >= 0).
    :param total_capital: общий лимит капитала (> 0).
    :returns: словарь ``{symbol: capital}``.
    :raises ValueError: если ``total_capital`` <= 0.
    """
    if total_capital <= 0:
        raise ValueError("total_capital должен быть > 0.")
    positive = {s: w for s, w in weights_by_symbol.items() if w and w > 0}
    sum_w = sum(positive.values())
    result: Dict[str, float] = {s: 0.0 for s in weights_by_symbol}
    if sum_w <= 0:
        return result
    for symbol, weight in positive.items():
        result[symbol] = total_capital * (weight / sum_w)
    return result
