"""Исполнение ордеров: симуляция (DRY_RUN) и реальная торговля.

Логика DRY_RUN и реальная работа с ccxt вынесены в отдельные классы с общим
интерфейсом :class:`OrderExecutor`, чтобы в основном цикле не было
разрастающихся if/else. Бот работает с любым исполнителем одинаково.
"""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import ccxt

from .grid import GridOrder, Side
from .logging_setup import get_logger

logger = get_logger("orders")


@dataclass
class PlacedOrder:
    """Размещённый ордер, за которым следит бот."""

    id: str
    side: Side
    price: float
    amount: float
    level_index: int
    filled: bool = False


class OrderExecutor(ABC):
    """Единый интерфейс исполнения ордеров (sim или live)."""

    @abstractmethod
    def place_limit_order(self, order: GridOrder) -> PlacedOrder:
        """Разместить лимитный ордер и вернуть его представление."""

    @abstractmethod
    def detect_fills(self, current_price: float) -> List[PlacedOrder]:
        """Вернуть список ордеров, которые исполнились с прошлой проверки."""

    @abstractmethod
    def cancel_all(self) -> None:
        """Отменить все открытые ордера бота."""

    @abstractmethod
    def fetch_equity(self, current_price: float) -> float:
        """Текущее equity (в котируемой валюте) для kill switch."""

    @property
    @abstractmethod
    def open_orders(self) -> List[PlacedOrder]:
        """Список текущих открытых (неисполненных) ордеров."""


class DryRunExecutor(OrderExecutor):
    """Симулятор: ордера не уходят на биржу, заполнения считаются локально.

    Заполнение определяется по реальной текущей цене с биржи:
    - buy исполняется, когда ``current_price <= price`` ордера;
    - sell исполняется, когда ``current_price >= price`` ордера.

    Equity моделируется как котируемый баланс + стоимость накопленной
    базовой позиции по текущей цене. Стартовый баланс ограничен капиталом.
    """

    def __init__(self, total_capital: float) -> None:
        self._counter = itertools.count(1)
        self._orders: Dict[str, PlacedOrder] = {}
        self.quote_balance: float = total_capital
        self.base_position: float = 0.0
        logger.info(
            "DRY_RUN исполнитель инициализирован (стартовый капитал=%.4f).",
            total_capital,
        )

    def place_limit_order(self, order: GridOrder) -> PlacedOrder:
        order_id = f"sim-{next(self._counter)}"
        placed = PlacedOrder(
            id=order_id,
            side=order.side,
            price=order.price,
            amount=order.amount,
            level_index=order.level_index,
        )
        self._orders[order_id] = placed
        logger.info(
            "[DRY_RUN] Размещён %s лимит: price=%.4f amount=%.6f (level=%d, id=%s)",
            order.side.value,
            order.price,
            order.amount,
            order.level_index,
            order_id,
        )
        return placed

    def detect_fills(self, current_price: float) -> List[PlacedOrder]:
        filled: List[PlacedOrder] = []
        for placed in list(self._orders.values()):
            if placed.filled:
                continue
            hit_buy = placed.side == Side.BUY and current_price <= placed.price
            hit_sell = placed.side == Side.SELL and current_price >= placed.price
            if hit_buy or hit_sell:
                placed.filled = True
                self._apply_fill(placed)
                filled.append(placed)
                logger.info(
                    "[DRY_RUN] Исполнен %s: price=%.4f amount=%.6f (id=%s) "
                    "при текущей цене=%.4f",
                    placed.side.value,
                    placed.price,
                    placed.amount,
                    placed.id,
                    current_price,
                )
        # Исполненные ордера убираем из отслеживаемых открытых.
        for placed in filled:
            self._orders.pop(placed.id, None)
        return filled

    def _apply_fill(self, placed: PlacedOrder) -> None:
        """Обновить смоделированные балансы при исполнении ордера."""
        if placed.side == Side.BUY:
            self.quote_balance -= placed.price * placed.amount
            self.base_position += placed.amount
        else:
            self.quote_balance += placed.price * placed.amount
            self.base_position -= placed.amount

    def cancel_all(self) -> None:
        count = len(self._orders)
        self._orders.clear()
        logger.info("[DRY_RUN] Отменены все открытые ордера (%d шт.).", count)

    def fetch_equity(self, current_price: float) -> float:
        return self.quote_balance + self.base_position * current_price

    @property
    def open_orders(self) -> List[PlacedOrder]:
        return [o for o in self._orders.values() if not o.filled]


class LiveExecutor(OrderExecutor):
    """Реальное исполнение через ccxt. Используется только при DRY_RUN=false.

    Капитал ограничивается параметром ``total_capital`` на уровне бота
    (см. :mod:`grid` и проверки в :mod:`bot`): сюда передаются уже
    рассчитанные объёмы, не превышающие лимит.
    """

    def __init__(
        self,
        exchange: ccxt.Exchange,
        symbol: str,
        total_capital: float,
    ) -> None:
        self.exchange = exchange
        self.symbol = symbol
        self.total_capital = total_capital
        self._orders: Dict[str, PlacedOrder] = {}
        logger.info(
            "LIVE исполнитель инициализирован: symbol=%s, лимит капитала=%.4f.",
            symbol,
            total_capital,
        )

    def place_limit_order(self, order: GridOrder) -> PlacedOrder:
        result = self.exchange.create_order(
            symbol=self.symbol,
            type="limit",
            side=order.side.value,
            amount=order.amount,
            price=order.price,
        )
        order_id = str(result.get("id"))
        placed = PlacedOrder(
            id=order_id,
            side=order.side,
            price=order.price,
            amount=order.amount,
            level_index=order.level_index,
        )
        self._orders[order_id] = placed
        logger.info(
            "[LIVE] Размещён %s лимит: price=%.4f amount=%.6f (level=%d, id=%s)",
            order.side.value,
            order.price,
            order.amount,
            order.level_index,
            order_id,
        )
        return placed

    def detect_fills(self, current_price: float) -> List[PlacedOrder]:
        filled: List[PlacedOrder] = []
        for order_id, placed in list(self._orders.items()):
            if placed.filled:
                continue
            try:
                info = self.exchange.fetch_order(order_id, self.symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[LIVE] Не удалось проверить ордер %s: %s", order_id, exc)
                continue
            status = info.get("status")
            if status == "closed":
                placed.filled = True
                filled.append(placed)
                logger.info(
                    "[LIVE] Исполнен %s: price=%.4f amount=%.6f (id=%s)",
                    placed.side.value,
                    placed.price,
                    placed.amount,
                    order_id,
                )
        for placed in filled:
            self._orders.pop(placed.id, None)
        return filled

    def cancel_all(self) -> None:
        for order_id in list(self._orders.keys()):
            try:
                self.exchange.cancel_order(order_id, self.symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[LIVE] Не удалось отменить ордер %s: %s", order_id, exc)
        self._orders.clear()
        logger.info("[LIVE] Запрошена отмена всех открытых ордеров бота.")

    def fetch_equity(self, current_price: float) -> float:
        """Equity из баланса биржи, но не больше лимита капитала.

        Возвращаем минимум из реального свободного капитала и заданного
        ``total_capital`` — бот не должен «видеть» больше, чем ему выделили.
        """
        try:
            balance = self.exchange.fetch_balance()
            total = balance.get("total", {})
            # Берём котируемую валюту из символа (часть после '/').
            quote = self.symbol.split("/")[-1].split(":")[0] if "/" in self.symbol else "USDT"
            quote_total = float(total.get(quote, 0.0) or 0.0)
            return min(quote_total, self.total_capital)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[LIVE] Не удалось получить баланс: %s", exc)
            return self.total_capital

    @property
    def open_orders(self) -> List[PlacedOrder]:
        return [o for o in self._orders.values() if not o.filled]
