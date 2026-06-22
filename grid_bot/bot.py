"""Основной цикл grid-бота: один тикер или портфель из нескольких.

Архитектура:
- :class:`SymbolGridInstance` — независимая grid-стратегия для одного символа
  (свои уровни, свой исполнитель, свой per-symbol kill switch, своё состояние).
- :class:`GridBot` — оркестратор: собирает портфель (через screener в
  мультирежиме), распределяет капитал по весам категорий, опрашивает цены
  ОДНИМ батчевым запросом, периодически пересобирает портфель и держит
  портфельный kill switch.

Базовая grid-логика на символ: при исполнении buy ставится sell на уровень
выше, при исполнении sell — buy на уровень ниже.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

import ccxt

from .config import Config
from .exchange import (
    build_exchange,
    fetch_current_price,
    fetch_prices,
    resolve_symbol_str,
    setup_futures_market,
)
from .grid import (
    GridLevel,
    GridOrder,
    Side,
    allocate_capital_by_weights,
    amount_for_level,
    auto_price_range,
    build_grid_levels,
    build_initial_orders,
    capital_per_level,
    required_capital,
)
from .logging_setup import get_logger
from .orders import DryRunExecutor, LiveExecutor, OrderExecutor, PlacedOrder
from .risk import DrawdownKillSwitch, warn_liquidation_risk
from .screener import build_universe
from .storage import Storage
from .strategy import refresh_universe_fields

logger = get_logger("bot")

# Статусы экземпляра символа.
STATUS_ACTIVE = "active"
STATUS_WIND_DOWN = "wind_down"
STATUS_KILLED = "killed"
STATUS_STOPPED = "stopped"

# Более жёсткий kill switch для трендовых категорий: grid плохо переносит
# устойчивое одностороннее движение, а "gainer"/"volatile" — это почти по
# определению тренд/резкие движения. Множитель < 1 ужесточает порог просадки.
# Это явный, настраиваемый здесь коэффициент (не догма в комментарии).
CATEGORY_DRAWDOWN_FACTOR = {
    "volume": 1.0,
    "gainer": 0.7,
    "volatile": 0.6,
    "manual": 1.0,
}


def _quote_currency(symbol: str) -> str:
    """Извлечь котируемую валюту из символа (BTC/USDT -> USDT, BTC/USDT:USDT -> USDT)."""
    if "/" in symbol:
        return symbol.split("/", 1)[1].split(":")[0]
    return "USDT"


class SymbolGridInstance:
    """Независимая grid-стратегия для одного символа."""

    def __init__(
        self,
        *,
        config: Config,
        exchange: ccxt.Exchange,
        storage: Storage,
        symbol: str,
        category: str,
        capital: float,
        lower_price: float,
        upper_price: float,
    ) -> None:
        self.config = config
        self.exchange = exchange
        self.storage = storage
        self.symbol = symbol  # уже разрешённый символ рынка
        self.category = category
        self.capital = capital
        self.lower_price = lower_price
        self.upper_price = upper_price
        self.quote_currency = _quote_currency(symbol)

        self.levels: List[GridLevel] = build_grid_levels(
            lower_price, upper_price, config.num_grids
        )
        self.level_price: Dict[int, float] = {lv.index: lv.price for lv in self.levels}
        self.capital_per_level: float = capital_per_level(capital, config.num_grids)
        self.executor: OrderExecutor = self._build_executor()

        # Per-symbol kill switch с ужесточением для трендовых категорий.
        factor = CATEGORY_DRAWDOWN_FACTOR.get(category, 1.0)
        self.effective_max_drawdown_pct: float = max(
            0.01, min(100.0, config.max_drawdown_pct * factor)
        )
        self.kill_switch: Optional[DrawdownKillSwitch] = None
        self.start_equity: float = 0.0
        self.last_equity: float = 0.0
        self.status: str = STATUS_ACTIVE

    def _build_executor(self) -> OrderExecutor:
        if self.config.dry_run:
            return DryRunExecutor(total_capital=self.capital)
        return LiveExecutor(
            exchange=self.exchange,
            symbol=self.symbol,
            total_capital=self.capital,
        )

    @property
    def is_alive(self) -> bool:
        return self.status in (STATUS_ACTIVE, STATUS_WIND_DOWN)

    def setup(self, current_price: float) -> None:
        """Настроить рынок (futures), разместить стартовую сетку, инициализировать kill switch."""
        if self.config.is_futures and not self.config.dry_run:
            setup_futures_market(self.exchange, self.config, self.symbol)

        warn_liquidation_risk(
            self.config,
            current_price,
            lower_price=self.lower_price,
            upper_price=self.upper_price,
            symbol=self.symbol,
        )

        orders = build_initial_orders(self.levels, current_price, self.capital)
        needed = required_capital(orders)
        # Инвариант: на buy-ордера нельзя требовать больше, чем выделено символу.
        if needed > self.capital + 1e-9:
            logger.warning(
                "[%s] Требуемый капитал на buy (%.4f) > выделенного (%.4f).",
                self.symbol,
                needed,
                self.capital,
            )
        for order in orders:
            self.executor.place_limit_order(order)

        self.start_equity = self.executor.fetch_equity(current_price)
        self.last_equity = self.start_equity
        self.kill_switch = DrawdownKillSwitch(
            max_drawdown_pct=self.effective_max_drawdown_pct,
            start_equity=self.start_equity,
        )
        self.storage.record_equity_snapshot(
            equity=self.start_equity,
            quote_currency=self.quote_currency,
            symbol=self.symbol,
        )
        self.storage.record_universe_change(self.symbol, self.category, "added")
        self.storage.record_event(
            "info",
            (
                f"Символ добавлен в портфель: {self.symbol} (категория={self.category}, "
                f"капитал={self.capital:.4f}, диапазон=[{self.lower_price:.6f}.."
                f"{self.upper_price:.6f}], порог просадки={self.effective_max_drawdown_pct:.2f}%)."
            ),
        )
        logger.info(
            "[%s] Сетка размещена: %d ордеров, капитал=%.4f, категория=%s.",
            self.symbol,
            len(orders),
            self.capital,
            self.category,
        )

    def _handle_fill(self, placed: PlacedOrder) -> None:
        """Поставить встречный ордер при исполнении (buy->sell выше, sell->buy ниже)."""
        if placed.side == Side.BUY:
            target_index = placed.level_index + 1
            new_side = Side.SELL
        else:
            target_index = placed.level_index - 1
            new_side = Side.BUY

        target_price = self.level_price.get(target_index)
        if target_price is None:
            return  # край сетки — встречный не ставим

        amount = amount_for_level(self.capital_per_level, target_price)
        self.executor.place_limit_order(
            GridOrder(side=new_side, price=target_price, amount=amount, level_index=target_index)
        )

    def process(self, current_price: float) -> str:
        """Обработать одну итерацию для символа. Возвращает текущий статус."""
        if not self.is_alive:
            return self.status

        fills = self.executor.detect_fills(current_price)
        for placed in fills:
            self.storage.record_trade(
                exchange=self.config.exchange_id,
                symbol=self.symbol,
                market_type=self.config.market_type,
                side=placed.side.value,
                price=placed.price,
                amount=placed.amount,
                order_id=placed.id,
                dry_run=self.config.dry_run,
                leverage=self.config.leverage,
                category=self.category,
            )
            # В режиме wind-down новые уровни НЕ открываем — даём позиции закрыться.
            if self.status == STATUS_ACTIVE:
                self._handle_fill(placed)

        equity = self.executor.fetch_equity(current_price)
        self.last_equity = equity
        self.storage.record_equity_snapshot(
            equity=equity, quote_currency=self.quote_currency, symbol=self.symbol
        )

        # Per-symbol kill switch — останавливает только этот символ.
        if self.kill_switch is not None and self.kill_switch.check(equity):
            self._stop(STATUS_KILLED, reason=(
                f"per-symbol KILL SWITCH: equity={equity:.4f} ниже порога "
                f"{self.kill_switch.threshold_equity:.4f} "
                f"(просадка {self.kill_switch.current_drawdown_pct(equity):.2f}% "
                f"при лимите {self.effective_max_drawdown_pct:.2f}%)."
            ))
            return self.status

        # Wind-down завершён, когда не осталось открытых ордеров.
        if self.status == STATUS_WIND_DOWN and not self.executor.open_orders:
            self._stop(STATUS_STOPPED, reason="wind-down завершён: открытых ордеров не осталось.")

        return self.status

    def begin_wind_down(self) -> None:
        """Начать сворачивание символа (выпал из universe) по политике WIND_DOWN_POLICY."""
        if not self.is_alive or self.status != STATUS_ACTIVE:
            return
        if self.config.wind_down_policy == "cancel":
            self.executor.cancel_all()
            self._stop(STATUS_STOPPED, reason="символ убран из портфеля (policy=cancel).")
        else:  # hold
            self.status = STATUS_WIND_DOWN
            self.storage.record_universe_change(self.symbol, self.category, "wind_down")
            self.storage.record_event(
                "warning",
                f"Символ {self.symbol} переведён в wind-down (policy=hold): "
                "новые уровни не открываем, ждём закрытия текущих ордеров.",
            )

    def _stop(self, status: str, *, reason: str) -> None:
        level = "error" if status == STATUS_KILLED else "info"
        self.status = status
        self.storage.record_universe_change(self.symbol, self.category, "removed")
        self.storage.record_event(level, f"Символ {self.symbol} остановлен: {reason}")
        logger.info("[%s] Остановлен (%s): %s", self.symbol, status, reason)

    def force_stop(self) -> None:
        """Принудительная остановка (портфельный kill switch / завершение бота)."""
        if self.status in (STATUS_STOPPED, STATUS_KILLED):
            return
        try:
            self.executor.cancel_all()
        except Exception as exc:  # noqa: BLE001
            logger.error("[%s] Ошибка отмены ордеров: %s", self.symbol, exc)
        self._stop(STATUS_STOPPED, reason="принудительная остановка.")


class GridBot:
    """Оркестратор: портфель из :class:`SymbolGridInstance` (или один тикер)."""

    def __init__(
        self,
        config: Config,
        exchange: Optional[ccxt.Exchange] = None,
        storage: Optional[Storage] = None,
    ) -> None:
        self.config = config
        self.exchange = exchange if exchange is not None else build_exchange(config)
        self.storage: Storage = storage if storage is not None else Storage()
        self.instances: Dict[str, SymbolGridInstance] = {}
        self.quote_currency = config.quote_currency
        self._running = False
        self._process_name = "grid_bot"
        self._last_rescan_monotonic = 0.0
        self._portfolio_kill_triggered = False

    # --- инфраструктура ---------------------------------------------------

    def _heartbeat(self, detail: Optional[str] = None) -> None:
        self.storage.upsert_process_heartbeat(
            self._process_name, os.getpid(), status="running", detail=detail
        )

    def _build_universe_map(self) -> Dict[str, str]:
        """Вернуть ``{resolved_symbol: category}`` для текущего режима."""
        if not self.config.multi_symbol_mode:
            symbol = resolve_symbol_str(self.config, self.config.symbol)
            return {symbol: "manual"}
        raw = build_universe(self.exchange, self.config)
        resolved: Dict[str, str] = {}
        for symbol, category in raw.items():
            resolved[resolve_symbol_str(self.config, symbol)] = category
        return resolved

    def _fetch_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Цены для символов: батч в мультирежиме, один тикер в одиночном."""
        if not symbols:
            return {}
        if self.config.multi_symbol_mode:
            return fetch_prices(self.exchange, symbols)
        # Одиночный режим: один символ.
        symbol = symbols[0]
        try:
            return {symbol: fetch_current_price(self.exchange, symbol)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось получить цену %s: %s", symbol, exc)
            return {}

    def _category_weight(self, category: str) -> float:
        if category == "manual":
            return 1.0
        return self.config.category_weights.get(category, 0.0)

    def _committed_capital(self) -> float:
        """Капитал, занятый живыми экземплярами (active + wind_down)."""
        return sum(inst.capital for inst in self.instances.values() if inst.is_alive)

    # --- запуск портфеля --------------------------------------------------

    def _create_instance(
        self, symbol: str, category: str, capital: float, price: float
    ) -> Optional[SymbolGridInstance]:
        """Создать и настроить экземпляр символа. Возвращает None при проблеме."""
        if capital <= 0 or price <= 0:
            return None
        try:
            if self.config.multi_symbol_mode:
                lower, upper = auto_price_range(price, self.config.grid_range_pct)
            else:
                lower, upper = self.config.lower_price, self.config.upper_price
            inst = SymbolGridInstance(
                config=self.config,
                exchange=self.exchange,
                storage=self.storage,
                symbol=symbol,
                category=category,
                capital=capital,
                lower_price=lower,
                upper_price=upper,
            )
            inst.setup(price)
            return inst
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось создать экземпляр %s: %s", symbol, exc)
            self.storage.record_event("error", f"Ошибка инициализации {symbol}: {exc}")
            return None

    def _initial_build(self) -> None:
        """Собрать первоначальный портфель и распределить капитал по весам."""
        universe = self._build_universe_map()
        if not universe:
            logger.warning("Universe пуст на старте — ждём следующего ресканирования.")
            self.storage.record_event("warning", "Стартовый портфель пуст (нет подходящих тикеров).")
            return

        # Сброс состава от прошлой сессии: символы, которых нет в новом портфеле
        # (например, после смены spot<->futures или профиля), помечаем removed —
        # иначе они «зависают» в активных на дашборде между перезапусками.
        stale = self.storage.current_universe()
        for old_symbol, meta in stale.items():
            if old_symbol not in universe:
                self.storage.record_universe_change(
                    old_symbol, meta.get("category", "manual"), "removed"
                )

        weights = {sym: self._category_weight(cat) for sym, cat in universe.items()}
        allocation = allocate_capital_by_weights(weights, self.config.total_capital)

        # Инвариант безопасности: суммарно не больше TOTAL_CAPITAL.
        assert sum(allocation.values()) <= self.config.total_capital + 1e-6, (
            "Распределение капитала превысило TOTAL_CAPITAL"
        )

        prices = self._fetch_prices(list(universe.keys()))
        for symbol, category in universe.items():
            price = prices.get(symbol)
            if price is None:
                logger.warning("Нет цены для %s на старте — пропускаем.", symbol)
                continue
            inst = self._create_instance(symbol, category, allocation.get(symbol, 0.0), price)
            if inst is not None:
                self.instances[symbol] = inst

    def _rescan(self) -> None:
        """Пересобрать портфель: убрать выпавшие, добавить новые символы."""
        logger.info("Ресканирование портфеля...")
        # Живое применение настройки «количество символов» с дашборда:
        # обновляем только поля состава портфеля, не трогая риск-параметры
        # уже работающих символов.
        if self.config.multi_symbol_mode:
            self.config = refresh_universe_fields(self.config, self.storage.get_controls())
        try:
            new_universe = self._build_universe_map()
        except Exception as exc:  # noqa: BLE001
            logger.error("Ресканирование не удалось: %s", exc)
            self.storage.record_event("error", f"Ошибка ресканирования: {exc}")
            return

        if not new_universe:
            logger.warning("Новый universe пуст — оставляем текущий портфель без изменений.")
            return

        # 1) Символы, выпавшие из выборки -> wind-down.
        for symbol, inst in list(self.instances.items()):
            if inst.status == STATUS_ACTIVE and symbol not in new_universe:
                inst.begin_wind_down()

        # 2) Новые символы -> создать из свободного капитала.
        committed = self._committed_capital()
        free = max(0.0, self.config.total_capital - committed)
        new_symbols = {
            sym: cat
            for sym, cat in new_universe.items()
            if sym not in self.instances or not self.instances[sym].is_alive
        }
        if new_symbols and free > 0:
            weights = {sym: self._category_weight(cat) for sym, cat in new_symbols.items()}
            allocation = allocate_capital_by_weights(weights, free)
            # Инвариант: committed + новые <= TOTAL_CAPITAL.
            assert committed + sum(allocation.values()) <= self.config.total_capital + 1e-6
            prices = self._fetch_prices(list(new_symbols.keys()))
            for symbol, category in new_symbols.items():
                price = prices.get(symbol)
                if price is None:
                    continue
                inst = self._create_instance(symbol, category, allocation.get(symbol, 0.0), price)
                if inst is not None:
                    self.instances[symbol] = inst
        elif new_symbols:
            logger.info("Нет свободного капитала для новых символов — пропускаем добавление.")

    # --- основной цикл ----------------------------------------------------

    def run(self, max_iterations: Optional[int] = None) -> None:
        """Запустить основной цикл (портфельный или одиночный)."""
        mode = "MULTI" if self.config.multi_symbol_mode else "SINGLE"
        logger.info(
            "Старт бота: режим=%s exchange=%s market=%s dry_run=%s testnet=%s",
            mode, self.config.exchange_id, self.config.market_type,
            self.config.dry_run, self.config.use_testnet,
        )
        self.storage.record_event(
            "info",
            (
                f"Бот запущен: режим={mode} {self.config.exchange_id} "
                f"{self.config.market_type} dry_run={self.config.dry_run} "
                f"плечо={self.config.leverage}x, капитал={self.config.total_capital:.4f}."
            ),
        )
        self._heartbeat(detail=f"{mode} {self.config.exchange_id} dry_run={self.config.dry_run}")

        self._initial_build()
        self._last_rescan_monotonic = time.monotonic()
        self._record_portfolio_equity()

        self._running = True
        iteration = 0
        try:
            while self._running:
                iteration += 1
                self._tick()
                if max_iterations is not None and iteration >= max_iterations:
                    logger.info("Достигнут лимит итераций (%d). Останавливаемся.", max_iterations)
                    break
                if not self._running:
                    break
                time.sleep(self.config.poll_seconds)
        except KeyboardInterrupt:
            logger.info("Прерывание пользователем (Ctrl+C). Останавливаемся.")
        finally:
            self.shutdown()

    def _tick(self) -> None:
        """Одна итерация: батч цен -> обработка символов -> портфельный kill switch -> рескан."""
        alive = [inst for inst in self.instances.values() if inst.is_alive]
        self._heartbeat(
            detail=f"{self.config.exchange_id} активных символов={len(alive)}"
        )
        if not alive:
            # Возможно, портфель пуст — пробуем рескан по таймеру.
            self._maybe_rescan()
            return

        prices = self._fetch_prices([inst.symbol for inst in alive])
        if not prices:
            logger.warning("Не удалось получить цены на этой итерации.")
            return

        for inst in alive:
            price = prices.get(inst.symbol)
            if price is None:
                continue
            inst.process(price)

        self._record_portfolio_equity()
        self._check_portfolio_kill_switch()
        # Убираем полностью остановленные экземпляры из активного словаря.
        self._prune_stopped()

        if self._running:
            self._maybe_rescan()

    def _record_portfolio_equity(self) -> float:
        """Записать агрегированный снимок equity портфеля (symbol=NULL)."""
        alive = [inst for inst in self.instances.values() if inst.is_alive]
        total = sum(inst.last_equity for inst in alive)
        self.storage.record_equity_snapshot(
            equity=total, quote_currency=self.quote_currency, symbol=None
        )
        logger.info(
            "Портфель: активных символов=%d, equity=%.4f",
            len(alive),
            total,
        )
        return total

    def _check_portfolio_kill_switch(self) -> None:
        """Портфельный kill switch: просадка по всему портфелю -> стоп всех символов.

        Действует только в мультирежиме (в одиночном работает per-symbol kill switch).
        """
        if not self.config.multi_symbol_mode or self._portfolio_kill_triggered:
            return
        alive = [inst for inst in self.instances.values() if inst.is_alive]
        if not alive:
            return
        start_sum = sum(inst.start_equity for inst in alive)
        cur_sum = sum(inst.last_equity for inst in alive)
        if start_sum <= 0:
            return
        drawdown_pct = (start_sum - cur_sum) / start_sum * 100.0
        if drawdown_pct >= self.config.max_portfolio_drawdown_pct:
            self._portfolio_kill_triggered = True
            msg = (
                f"ПОРТФЕЛЬНЫЙ KILL SWITCH: просадка по портфелю {drawdown_pct:.2f}% "
                f">= лимит {self.config.max_portfolio_drawdown_pct:.2f}% "
                f"(equity {cur_sum:.4f} из стартовых {start_sum:.4f}). "
                "Останавливаем ВСЕ символы и отменяем все ордера."
            )
            logger.critical(msg)
            self.storage.record_event("error", msg)
            for inst in alive:
                inst.force_stop()
            self._running = False

    def _prune_stopped(self) -> None:
        for symbol in [s for s, i in self.instances.items() if not i.is_alive]:
            self.instances.pop(symbol, None)

    def _maybe_rescan(self) -> None:
        """Рескан по таймеру RESCAN_INTERVAL_HOURS или по запросу с дашборда."""
        if not self.config.multi_symbol_mode:
            return
        # Кнопка «пересканировать сейчас» на дашборде ставит флаг rescan_now=1.
        force = self.storage.get_control("rescan_now") == "1"
        interval_sec = self.config.rescan_interval_hours * 3600.0
        if force or (time.monotonic() - self._last_rescan_monotonic >= interval_sec):
            if force:
                self.storage.set_control("rescan_now", "0")
            self._rescan()
            self._last_rescan_monotonic = time.monotonic()

    def shutdown(self) -> None:
        """Корректное завершение: отменить ордера по всем символам."""
        logger.info("Завершение работы: отменяем открытые ордера по всем символам.")
        for inst in list(self.instances.values()):
            try:
                inst.executor.cancel_all()
            except Exception as exc:  # noqa: BLE001
                logger.error("[%s] Ошибка при отмене ордеров: %s", inst.symbol, exc)

        if self._portfolio_kill_triggered:
            self.storage.record_event("error", "Бот остановлен по портфельному kill switch.")
        else:
            self.storage.record_event("info", "Бот остановлен.")
        self.storage.mark_process_stopped(self._process_name)
        logger.info("Бот остановлен.")
