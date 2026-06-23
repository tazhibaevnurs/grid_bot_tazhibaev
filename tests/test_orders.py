"""Тесты симулятора ордеров (DryRunExecutor) и live-исполнителя (LiveExecutor)."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from grid_bot.grid import GridOrder, Side
from grid_bot.orders import DryRunExecutor, LiveExecutor


class MockExchange:
    """Минимальный мок ccxt.Exchange для LiveExecutor без сетевых вызовов."""

    def __init__(self) -> None:
        self._counter = 0
        self.open_orders: List[Dict[str, Any]] = []
        self.balance: Dict[str, float] = {"USDT": 0.0}
        self.fetch_open_orders_calls = 0
        self.fetch_balance_should_fail = False

    def create_order(
        self,
        symbol: str,
        type: str,
        side: str,
        amount: float,
        price: float,
    ) -> Dict[str, Any]:
        self._counter += 1
        order = {
            "id": str(self._counter),
            "symbol": symbol,
            "type": type,
            "side": side,
            "amount": amount,
            "price": price,
            "status": "open",
        }
        self.open_orders.append(order)
        return order

    def fetch_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        self.fetch_open_orders_calls += 1
        return [o for o in self.open_orders if o["symbol"] == symbol and o["status"] == "open"]

    def cancel_order(self, order_id: str, symbol: str) -> None:
        self.open_orders = [
            o for o in self.open_orders if not (str(o["id"]) == order_id and o["symbol"] == symbol)
        ]

    def fetch_balance(self) -> Dict[str, Any]:
        if self.fetch_balance_should_fail:
            raise RuntimeError("network down")
        return {"total": dict(self.balance)}

    def simulate_fill(self, order_id: str) -> None:
        """Тестовый хелпер: убрать ордер из открытых (как будто исполнен)."""
        for order in self.open_orders:
            if str(order["id"]) == order_id:
                order["status"] = "closed"
        self.open_orders = [o for o in self.open_orders if o["status"] == "open"]


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


# --- LiveExecutor -----------------------------------------------------------


def test_live_equity_tracks_base_position_after_fills():
    exchange = MockExchange()
    executor = LiveExecutor(exchange=exchange, symbol="BTC/USDT", total_capital=1000.0)

    placed = executor.place_limit_order(
        GridOrder(Side.BUY, price=100.0, amount=1.0, level_index=0)
    )
    exchange.simulate_fill(placed.id)

    fills = executor.detect_fills(current_price=100.0)
    assert len(fills) == 1
    assert executor.quote_balance == pytest.approx(900.0)
    assert executor.base_position == pytest.approx(1.0)
    assert executor.fetch_equity(current_price=100.0) == pytest.approx(1000.0)
    assert executor.fetch_equity(current_price=120.0) == pytest.approx(1020.0)


def test_live_equity_after_buy_and_sell():
    exchange = MockExchange()
    executor = LiveExecutor(exchange=exchange, symbol="BTC/USDT", total_capital=1000.0)

    buy = executor.place_limit_order(GridOrder(Side.BUY, price=100.0, amount=1.0, level_index=0))
    exchange.simulate_fill(buy.id)
    executor.detect_fills(current_price=100.0)

    sell = executor.place_limit_order(GridOrder(Side.SELL, price=110.0, amount=1.0, level_index=1))
    exchange.simulate_fill(sell.id)
    executor.detect_fills(current_price=110.0)

    assert executor.quote_balance == pytest.approx(1010.0)
    assert executor.base_position == pytest.approx(0.0)
    assert executor.fetch_equity(current_price=110.0) == pytest.approx(1010.0)


def test_live_fetch_equity_stale_does_not_reset_to_total_capital():
    exchange = MockExchange()
    executor = LiveExecutor(exchange=exchange, symbol="BTC/USDT", total_capital=1000.0)

    buy = executor.place_limit_order(
        GridOrder(Side.BUY, price=100.0, amount=1.0, level_index=0)
    )
    executor.place_limit_order(GridOrder(Side.SELL, price=120.0, amount=1.0, level_index=2))
    exchange.simulate_fill(buy.id)
    executor.detect_fills(current_price=100.0)
    assert executor.fetch_equity(current_price=80.0) == pytest.approx(980.0)

    exchange.fetch_open_orders = lambda symbol: (_ for _ in ()).throw(RuntimeError("timeout"))  # type: ignore[method-assign, assignment]
    executor.detect_fills(current_price=50.0)

    stale_equity = executor.fetch_equity(current_price=50.0)
    assert stale_equity == pytest.approx(980.0)
    assert stale_equity != pytest.approx(1000.0)


def test_live_detect_fills_uses_batch_fetch_open_orders():
    exchange = MockExchange()
    executor = LiveExecutor(exchange=exchange, symbol="BTC/USDT", total_capital=1000.0)

    buy = executor.place_limit_order(GridOrder(Side.BUY, price=100.0, amount=1.0, level_index=0))
    sell = executor.place_limit_order(GridOrder(Side.SELL, price=120.0, amount=1.0, level_index=2))
    exchange.simulate_fill(buy.id)

    fills = executor.detect_fills(current_price=100.0)
    assert len(fills) == 1
    assert fills[0].id == buy.id
    assert exchange.fetch_open_orders_calls == 1
    assert len(executor.open_orders) == 1
    assert executor.open_orders[0].id == sell.id


def test_live_detect_fills_returns_empty_on_exchange_error():
    exchange = MockExchange()
    executor = LiveExecutor(exchange=exchange, symbol="BTC/USDT", total_capital=1000.0)
    executor.place_limit_order(GridOrder(Side.BUY, price=100.0, amount=1.0, level_index=0))

    exchange.fetch_open_orders = lambda symbol: (_ for _ in ()).throw(RuntimeError("rate limit"))  # type: ignore[method-assign, assignment]
    assert executor.detect_fills(current_price=100.0) == []
    assert executor._exchange_data_stale is True


def test_live_per_symbol_equity_isolated_from_exchange_balance():
    """Локальный учёт не смешивает общий баланс счёта между символами."""
    exchange = MockExchange()
    exchange.balance["USDT"] = 5000.0

    btc = LiveExecutor(exchange=exchange, symbol="BTC/USDT", total_capital=1000.0)
    eth = LiveExecutor(exchange=exchange, symbol="ETH/USDT", total_capital=500.0)

    placed = btc.place_limit_order(GridOrder(Side.BUY, price=100.0, amount=1.0, level_index=0))
    exchange.simulate_fill(placed.id)
    btc.detect_fills(current_price=100.0)

    assert btc.fetch_equity(current_price=100.0) == pytest.approx(1000.0)
    assert eth.fetch_equity(current_price=2000.0) == pytest.approx(500.0)
