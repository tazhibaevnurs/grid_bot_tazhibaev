"""Профили стратегии (риск/доходность) и применение настроек с дашборда.

«Стратегия» здесь — это профиль риск/вознаграждение, который меняет агрессивность
портфеля: ширину сетки, пороги kill switch, состав по категориям и веса капитала.

ВАЖНО (см. README): grid зарабатывает на колебаниях ВНУТРИ диапазона и теряет
на устойчивом тренде. Поэтому «агрессивный» профиль (шире диапазон, больше
gainer/volatile, выше допустимая просадка) потенциально доходнее, но и
заметно рискованнее; «консервативный» — спокойнее и устойчивее.

Все управляющие настройки приходят из таблицы ``control`` (их пишет дашборд) и
накладываются поверх базового ``Config`` из .env через :func:`build_effective_config`.
"""

from __future__ import annotations

import dataclasses
from typing import Dict

from .config import Config

DEFAULT_PROFILE = "balanced"

# Профили риск/доходности. ``ratio`` — пропорции категорий (volume/gainer/volatile),
# по которым общее число символов делится между категориями.
RISK_PROFILES: Dict[str, Dict] = {
    "conservative": {
        "label": "Консервативный",
        "description": "Упор на ликвидные тикеры (volume), узкий диапазон, "
        "жёсткие стопы. Спокойнее и устойчивее, но меньше потенциал.",
        "ratio": {"volume": 5, "gainer": 1, "volatile": 0},
        "grid_range_pct": 8.0,
        "max_drawdown_pct": 10.0,
        "max_portfolio_drawdown_pct": 12.0,
        "weights": {"volume": 1.0, "gainer": 0.4, "volatile": 0.3},
    },
    "balanced": {
        "label": "Сбалансированный",
        "description": "Базовый режим: смесь объёма/роста/волатильности, "
        "умеренные диапазон и стопы.",
        "ratio": {"volume": 3, "gainer": 3, "volatile": 2},
        "grid_range_pct": 12.0,
        "max_drawdown_pct": 20.0,
        "max_portfolio_drawdown_pct": 25.0,
        "weights": {"volume": 1.0, "gainer": 0.5, "volatile": 0.4},
    },
    "aggressive": {
        "label": "Агрессивный",
        "description": "Больше трендовых тикеров (рост/волатильность), широкий "
        "диапазон, высокая терпимость к просадке. Доходнее, но РИСКОВАННЕЕ.",
        "ratio": {"volume": 2, "gainer": 4, "volatile": 4},
        "grid_range_pct": 18.0,
        "max_drawdown_pct": 30.0,
        "max_portfolio_drawdown_pct": 40.0,
        "weights": {"volume": 1.0, "gainer": 1.0, "volatile": 0.8},
    },
}


def distribute_counts(ratio: Dict[str, int], total: int) -> Dict[str, int]:
    """Поделить ``total`` символов между категориями по пропорциям ``ratio``.

    Используется метод наибольших остатков, чтобы сумма точно равнялась
    ``total`` (насколько позволяет ненулевая пропорция).

    :param ratio: пропорции категорий, напр. ``{"volume": 3, "gainer": 3, "volatile": 2}``.
    :param total: желаемое суммарное число символов (>= 0).
    :returns: словарь ``{category: count}`` с суммой == ``total`` (если ratio_sum>0).
    """
    if total <= 0:
        return {k: 0 for k in ratio}
    ratio_sum = sum(max(0, v) for v in ratio.values())
    if ratio_sum <= 0:
        return {k: 0 for k in ratio}

    raw = {k: total * max(0, v) / ratio_sum for k, v in ratio.items()}
    floors = {k: int(v) for k, v in raw.items()}
    assigned = sum(floors.values())
    remainder = total - assigned
    # Раздаём остаток категориям с наибольшей дробной частью.
    frac_order = sorted(raw.keys(), key=lambda k: raw[k] - floors[k], reverse=True)
    for k in frac_order:
        if remainder <= 0:
            break
        if ratio.get(k, 0) > 0:
            floors[k] += 1
            remainder -= 1
    return floors


def get_profile(name: str) -> Dict:
    """Вернуть профиль по имени (с откатом на DEFAULT_PROFILE)."""
    return RISK_PROFILES.get(name, RISK_PROFILES[DEFAULT_PROFILE])


def build_effective_config(base: Config, controls: Dict[str, str]) -> Config:
    """Наложить управляющие настройки (из таблицы control) поверх базового конфига.

    Поддерживаемые ключи в ``controls``:
    - ``risk_profile``  — имя профиля (conservative/balanced/aggressive);
    - ``max_symbols``   — желаемое суммарное число символов;
    - ``market_type``   — ``spot`` / ``futures``;
    - ``leverage``      — плечо для futures (на spot принудительно 1).

    Возвращает НОВЫЙ провалидированный :class:`Config`. Базовый не мутируется.
    """
    overrides: Dict[str, object] = {}

    profile_name = controls.get("risk_profile", DEFAULT_PROFILE)
    profile = get_profile(profile_name)

    # Профиль задаёт риск-параметры и состав по категориям.
    total_symbols = base.max_concurrent_symbols
    if "max_symbols" in controls:
        try:
            total_symbols = max(1, int(controls["max_symbols"]))
        except (TypeError, ValueError):
            total_symbols = base.max_concurrent_symbols

    counts = distribute_counts(profile["ratio"], total_symbols)
    overrides["num_top_volume"] = counts.get("volume", 0)
    overrides["num_top_gainers"] = counts.get("gainer", 0)
    overrides["num_top_volatile"] = counts.get("volatile", 0)
    overrides["max_concurrent_symbols"] = total_symbols
    overrides["grid_range_pct"] = profile["grid_range_pct"]
    overrides["max_drawdown_pct"] = profile["max_drawdown_pct"]
    overrides["max_portfolio_drawdown_pct"] = profile["max_portfolio_drawdown_pct"]
    overrides["capital_weight_volume"] = profile["weights"]["volume"]
    overrides["capital_weight_gainer"] = profile["weights"]["gainer"]
    overrides["capital_weight_volatile"] = profile["weights"]["volatile"]

    # Тип рынка и плечо.
    market_type = controls.get("market_type", base.market_type)
    if market_type in ("spot", "futures"):
        overrides["market_type"] = market_type
    else:
        market_type = base.market_type

    if market_type == "spot":
        overrides["leverage"] = 1  # на споте плеча нет (инвариант валидации)
    else:
        lev = base.leverage if base.leverage > 1 else 2
        if "leverage" in controls:
            try:
                lev = max(1, int(controls["leverage"]))
            except (TypeError, ValueError):
                pass
        overrides["leverage"] = lev

    effective = dataclasses.replace(base, **overrides)
    effective.validate()
    return effective


def refresh_universe_fields(config: Config, controls: Dict[str, str]) -> Config:
    """Обновить ТОЛЬКО поля состава портфеля (для живого ресканирования).

    Меняет число символов по категориям и общий лимит, не трогая риск-параметры
    уже работающих символов (диапазон/стопы), чтобы не дёргать открытые позиции.
    """
    profile = get_profile(controls.get("risk_profile", DEFAULT_PROFILE))
    total_symbols = config.max_concurrent_symbols
    if "max_symbols" in controls:
        try:
            total_symbols = max(1, int(controls["max_symbols"]))
        except (TypeError, ValueError):
            total_symbols = config.max_concurrent_symbols
    counts = distribute_counts(profile["ratio"], total_symbols)
    return dataclasses.replace(
        config,
        num_top_volume=counts.get("volume", 0),
        num_top_gainers=counts.get("gainer", 0),
        num_top_volatile=counts.get("volatile", 0),
        max_concurrent_symbols=total_symbols,
    )
