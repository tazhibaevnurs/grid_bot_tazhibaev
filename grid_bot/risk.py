"""Защитные механизмы: kill switch по просадке и предупреждение о ликвидации.

Это приоритетный модуль: его задача — не дать боту потерять больше, чем
допустимо, и громко предупредить о рисках futures-режима.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import Config
from .logging_setup import get_logger

logger = get_logger("risk")


@dataclass
class DrawdownKillSwitch:
    """Kill switch по просадке equity.

    Запоминает стартовое equity и проверяет, не упало ли текущее equity
    ниже порога ``(1 - max_drawdown_pct/100) * start_equity``.

    :param max_drawdown_pct: допустимая просадка в процентах (0, 100].
    :param start_equity: equity на старте бота.
    """

    max_drawdown_pct: float
    start_equity: float
    triggered: bool = False

    @property
    def threshold_equity(self) -> float:
        """Абсолютный порог equity, ниже которого срабатывает kill switch."""
        return self.start_equity * (1.0 - self.max_drawdown_pct / 100.0)

    def current_drawdown_pct(self, current_equity: float) -> float:
        """Текущая просадка в процентах относительно стартового equity."""
        if self.start_equity <= 0:
            return 0.0
        return (self.start_equity - current_equity) / self.start_equity * 100.0

    def check(self, current_equity: float) -> bool:
        """Проверить, не пора ли остановиться.

        :param current_equity: текущее значение equity.
        :returns: ``True``, если просадка превысила лимит (kill switch).
        """
        if current_equity <= self.threshold_equity:
            if not self.triggered:
                self.triggered = True
                logger.critical(
                    "KILL SWITCH: equity=%.4f упало ниже порога=%.4f "
                    "(стартовое=%.4f, просадка=%.2f%% при лимите %.2f%%). "
                    "Останавливаемся.",
                    current_equity,
                    self.threshold_equity,
                    self.start_equity,
                    self.current_drawdown_pct(current_equity),
                    self.max_drawdown_pct,
                )
            return True
        return False


def estimate_liquidation_distance_pct(leverage: int) -> float:
    """Грубая оценка расстояния до ликвидации в процентах от цены входа.

    ВНИМАНИЕ: это ПРИБЛИЖЁННАЯ оценка, а НЕ точный расчёт биржи. Реальная
    цена ликвидации зависит от ставки поддерживающей маржи, комиссий,
    funding, размера позиции и режима маржи. Здесь используется упрощённая
    модель: для изолированной позиции движение цены примерно на
    ``100% / leverage`` против позиции стирает начальную маржу.

    :param leverage: плечо (>= 1).
    :returns: приблизительный процент движения цены до ликвидации.
    """
    if leverage < 1:
        leverage = 1
    return 100.0 / leverage


def warn_liquidation_risk(
    config: Config,
    current_price: float,
    *,
    lower_price: Optional[float] = None,
    upper_price: Optional[float] = None,
    symbol: Optional[str] = None,
    maintenance_margin_buffer_pct: float = 0.5,
) -> Optional[str]:
    """Громко предупредить о риске ликвидации для futures-конфигурации.

    Сравнивает расстояние от текущей цены до границ сетки с приближённой
    дистанцией до ликвидации, исходя из плеча. Если границы диапазона выходят
    за (или близки к) оценочную зону ликвидации — пишет CRITICAL-предупреждение.

    Для spot-режима ничего не делает (ликвидации нет).

    :param config: конфигурация бота.
    :param current_price: текущая рыночная цена.
    :param lower_price: нижняя граница сетки; если ``None`` — берётся из конфига
        (используется для мультитикерного режима, где диапазон у каждого символа свой).
    :param upper_price: верхняя граница сетки; аналогично ``lower_price``.
    :param symbol: символ (для понятного лога в мультирежиме).
    :param maintenance_margin_buffer_pct: запас (множитель порога) на
        поддерживающую маржу и комиссии; делает оценку консервативнее.
    :returns: текст предупреждения, если риск обнаружен, иначе ``None``.
    """
    if not config.is_futures:
        return None
    if current_price <= 0:
        return None

    low = lower_price if lower_price is not None else config.lower_price
    high = upper_price if upper_price is not None else config.upper_price

    liq_pct = estimate_liquidation_distance_pct(config.leverage)
    # Консервативный порог: считаем рискованным, если граница диапазона
    # ближе к цене входа, чем (1 + buffer) доля от дистанции ликвидации.
    risky_pct = liq_pct * (1.0 + maintenance_margin_buffer_pct)

    down_move_pct = (current_price - low) / current_price * 100.0
    up_move_pct = (high - current_price) / current_price * 100.0

    risky_down = down_move_pct >= risky_pct
    risky_up = up_move_pct >= risky_pct

    if not (risky_down or risky_up):
        return None

    message = (
        "ПРЕДУПРЕЖДЕНИЕ О РИСКЕ ЛИКВИДАЦИИ (ПРИБЛИЖЁННАЯ ОЦЕНКА, не точный "
        "расчёт биржи!)%s: при плече %dx ориентировочная дистанция до "
        "ликвидации ~%.2f%% от цены входа. Текущая цена=%.4f, диапазон сетки "
        "[%.4f .. %.4f] (вниз=%.2f%%, вверх=%.2f%%). "
        "Цена может дойти до зоны ликвидации, не покинув заданный диапазон. "
        "Снизьте плечо, сузьте диапазон или используйте isolated-маржу."
    ) % (
        f" [{symbol}]" if symbol else "",
        config.leverage,
        liq_pct,
        current_price,
        low,
        high,
        down_move_pct,
        up_move_pct,
    )
    logger.critical(message)
    return message
