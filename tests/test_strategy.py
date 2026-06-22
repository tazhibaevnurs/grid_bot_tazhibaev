"""Тесты профилей стратегии: распределение символов и применение настроек."""

from __future__ import annotations

import pytest

from grid_bot.config import Config
from grid_bot.strategy import (
    RISK_PROFILES,
    build_effective_config,
    distribute_counts,
    refresh_universe_fields,
)


def _base_config(**overrides) -> Config:
    base = dict(
        exchange_id="binance", api_key="k", api_secret="s", use_testnet=True,
        dry_run=True, symbol="BTC/USDT", lower_price=0.0, upper_price=0.0,
        num_grids=10, total_capital=1000.0, max_drawdown_pct=20.0, poll_seconds=5.0,
        market_type="spot", leverage=1, margin_mode="isolated",
        multi_symbol_mode=True, max_concurrent_symbols=8,
    )
    base.update(overrides)
    return Config(**base)


# --- distribute_counts -----------------------------------------------------


def test_distribute_counts_sums_to_total():
    counts = distribute_counts({"volume": 3, "gainer": 3, "volatile": 2}, 8)
    assert sum(counts.values()) == 8
    assert counts == {"volume": 3, "gainer": 3, "volatile": 2}


def test_distribute_counts_proportional_rounding():
    counts = distribute_counts({"volume": 3, "gainer": 3, "volatile": 2}, 4)
    assert sum(counts.values()) == 4


def test_distribute_counts_zero_ratio_category_gets_none():
    counts = distribute_counts({"volume": 5, "gainer": 1, "volatile": 0}, 6)
    assert counts["volatile"] == 0
    assert sum(counts.values()) == 6


def test_distribute_counts_zero_total():
    counts = distribute_counts({"volume": 3, "gainer": 3, "volatile": 2}, 0)
    assert counts == {"volume": 0, "gainer": 0, "volatile": 0}


# --- build_effective_config ------------------------------------------------


def test_effective_config_applies_profile():
    cfg = build_effective_config(_base_config(), {"risk_profile": "aggressive"})
    prof = RISK_PROFILES["aggressive"]
    assert cfg.grid_range_pct == prof["grid_range_pct"]
    assert cfg.max_drawdown_pct == prof["max_drawdown_pct"]
    assert cfg.max_portfolio_drawdown_pct == prof["max_portfolio_drawdown_pct"]


def test_effective_config_symbol_count():
    cfg = build_effective_config(_base_config(), {"risk_profile": "balanced", "max_symbols": "10"})
    assert cfg.max_concurrent_symbols == 10
    assert cfg.num_top_volume + cfg.num_top_gainers + cfg.num_top_volatile == 10


def test_effective_config_futures_with_leverage():
    cfg = build_effective_config(
        _base_config(), {"market_type": "futures", "leverage": "5"}
    )
    assert cfg.market_type == "futures"
    assert cfg.leverage == 5
    cfg.validate()  # futures + leverage>1 валиден


def test_effective_config_spot_forces_leverage_one():
    # Даже если в controls осталось плечо — на споте оно принудительно 1.
    cfg = build_effective_config(
        _base_config(), {"market_type": "spot", "leverage": "5"}
    )
    assert cfg.market_type == "spot"
    assert cfg.leverage == 1


def test_effective_config_defaults_to_balanced():
    cfg = build_effective_config(_base_config(), {})
    prof = RISK_PROFILES["balanced"]
    assert cfg.grid_range_pct == prof["grid_range_pct"]


def test_refresh_universe_fields_only_changes_counts():
    cfg = build_effective_config(_base_config(), {"risk_profile": "balanced"})
    refreshed = refresh_universe_fields(cfg, {"risk_profile": "balanced", "max_symbols": "6"})
    assert refreshed.max_concurrent_symbols == 6
    assert refreshed.num_top_volume + refreshed.num_top_gainers + refreshed.num_top_volatile == 6
    # Риск-параметры не изменились.
    assert refreshed.grid_range_pct == cfg.grid_range_pct
    assert refreshed.max_drawdown_pct == cfg.max_drawdown_pct
