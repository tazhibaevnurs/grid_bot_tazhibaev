"""Исполнение ордеров: симуляция (DRY_RUN) и реальная торговля.

Логика DRY_RUN и реальная работа с ccxt вынесены в отдельные классы с общим
интерфейсом :class:`OrderExecutor`, чтобы в основном цикле не было
разрастающихся if/else. Бот работает с любым исполнителем одинаково.
"""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from dataclasses import dataclass
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

    Каждый экземпляр ведёт **локальный** учёт позиции (``quote_balance`` +
    ``base_position``), как :class:`DryRunExecutor`, обновляя его при
    детекте исполнения. Это даёт корректный per-symbol equity для kill switch
    в мультитикерном режиме и не смешивает баланс всего счёта.

    ``fetch_balance()`` не используется для kill switch — только опциональная
    диагностическая сверка через :meth:`_diagnose_exchange_equity`.
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
        self.quote_balance: float = total_capital
        self.base_position: float = 0.0
        self._last_valid_equity: float = total_capital
        self._exchange_data_stale: bool = False
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
        """Определить исполнения одним батчевым ``fetch_open_orders``.

        Ордер считается исполненным, если он отслеживается ботом, но больше
        не присутствует в списке открытых ордеров биржи.
        """
        if not self._orders:
            return []

        try:
            exchange_open = self.exchange.fetch_open_orders(self.symbol)
            self._exchange_data_stale = False
        except Exception as exc:  # noqa: BLE001
            self._exchange_data_stale = True
            logger.critical(
                "[LIVE] Не удалось получить открытые ордера для %s: %s. "
                "Данные equity могут быть неактуальны — kill switch использует "
                "последнее известное значение (%.4f).",
                self.symbol,
                exc,
                self._last_valid_equity,
            )
            return []

        open_ids = {
            str(order.get("id"))
            for order in exchange_open
            if order.get("id") is not None
        }

        filled: List[PlacedOrder] = []
        for order_id, placed in list(self._orders.items()):
            if placed.filled or order_id in open_ids:
                continue
            placed.filled = True
            self._apply_fill(placed)
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

    def _apply_fill(self, placed: PlacedOrder) -> None:
        """Обновить локальную позицию при исполнении ордера."""
        if placed.side == Side.BUY:
            self.quote_balance -= placed.price * placed.amount
            self.base_position += placed.amount
        else:
            self.quote_balance += placed.price * placed.amount
            self.base_position -= placed.amount

    def cancel_all(self) -> None:
        for order_id in list(self._orders.keys()):
            try:
                self.exchange.cancel_order(order_id, self.symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[LIVE] Не удалось отменить ордер %s: %s", order_id, exc)
        self._orders.clear()
        logger.info("[LIVE] Запрошена отмена всех открытых ордеров бота.")

    def fetch_equity(self, current_price: float) -> float:
        """Equity из локального учёта позиции (quote + base * price).

        При сбое связи с биржей на предыдущей итерации (``detect_fills``)
        возвращает последнее валидное значение, а не ``total_capital``.
        """
        if self._exchange_data_stale:
            logger.critical(
                "[LIVE] Equity для %s неактуальна (сбой биржи). "
                "Kill switch использует последнее известное значение=%.4f.",
                self.symbol,
                self._last_valid_equity,
            )
            return self._last_valid_equity

        equity = self.quote_balance + self.base_position * current_price
        self._last_valid_equity = equity
        return equity

    def _diagnose_exchange_equity(self, current_price: float) -> None:
        """Диагностика: сравнить локальный учёт с балансом биржи (только лог).

        Не используется для kill switch — только для обнаружения расхождений.
        """
        try:
            balance = self.exchange.fetch_balance()
            total = balance.get("total", {})
            quote = self._quote_currency()
            quote_total = float(total.get(quote, 0.0) or 0.0)
            local_equity = self.quote_balance + self.base_position * current_price
            diff = abs(local_equity - quote_total)
            if diff > max(1.0, local_equity * 0.01):
                logger.warning(
                    "[LIVE] Расхождение equity для %s: локально=%.4f, "
                    "свободный quote на бирже=%.4f (разница=%.4f). "
                    "Kill switch опирается на локальный учёт.",
                    self.symbol,
                    local_equity,
                    quote_total,
                    diff,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[LIVE] Диагностическая сверка баланса для %s не удалась: %s",
                self.symbol,
                exc,
            )

    def _quote_currency(self) -> str:
        if "/" in self.symbol:
            return self.symbol.split("/")[-1].split(":")[0]
        return "USDT"

    @property
    def open_orders(self) -> List[PlacedOrder]:
        return [o for o in self._orders.values() if not o.filled]
