"""Тесты загрузки и валидации конфигурации."""

from __future__ import annotations

import pytest

from grid_bot.config import Config, ConfigError, load_config


def make_config(**overrides) -> Config:
    """Собрать валидный по умолчанию Config с возможностью переопределить поля."""
    base = dict(
        exchange_id="binance",
        api_key="k",
        api_secret="s",
        use_testnet=True,
        dry_run=True,
        symbol="BTC/USDT",
        lower_price=100.0,
        upper_price=200.0,
        num_grids=10,
        total_capital=1000.0,
        max_drawdown_pct=20.0,
        poll_seconds=10.0,
        market_type="spot",
        leverage=1,
        margin_mode="isolated",
    )
    base.update(overrides)
    return Config(**base)


def test_valid_spot_config_passes():
    cfg = make_config(market_type="spot", leverage=1)
    cfg.validate()  # не должно бросать


def test_spot_with_leverage_not_one_raises():
    cfg = make_config(market_type="spot", leverage=3)
    with pytest.raises(ConfigError):
        cfg.validate()


def test_futures_with_leverage_ok():
    cfg = make_config(market_type="futures", leverage=5)
    cfg.validate()


def test_invalid_exchange_raises():
    cfg = make_config(exchange_id="kraken")
    with pytest.raises(ConfigError):
        cfg.validate()


def test_invalid_market_type_raises():
    cfg = make_config(market_type="margin")
    with pytest.raises(ConfigError):
        cfg.validate()


def test_lower_ge_upper_raises():
    cfg = make_config(lower_price=200.0, upper_price=100.0)
    with pytest.raises(ConfigError):
        cfg.validate()


def test_num_grids_too_small_raises():
    cfg = make_config(num_grids=1)
    with pytest.raises(ConfigError):
        cfg.validate()


def test_drawdown_out_of_range_raises():
    with pytest.raises(ConfigError):
        make_config(max_drawdown_pct=0.0).validate()
    with pytest.raises(ConfigError):
        make_config(max_drawdown_pct=150.0).validate()


def test_leverage_below_one_raises():
    cfg = make_config(market_type="futures", leverage=0)
    with pytest.raises(ConfigError):
        cfg.validate()


def test_live_without_keys_raises():
    cfg = make_config(dry_run=False, api_key=None, api_secret=None)
    with pytest.raises(ConfigError):
        cfg.validate()


def test_load_config_from_env(monkeypatch):
    env = {
        "EXCHANGE_ID": "bybit",
        "API_KEY": "abc",
        "API_SECRET": "def",
        "USE_TESTNET": "true",
        "DRY_RUN": "true",
        "SYMBOL": "ETH/USDT",
        "LOWER_PRICE": "1000",
        "UPPER_PRICE": "2000",
        "NUM_GRIDS": "8",
        "TOTAL_CAPITAL": "500",
        "MAX_DRAWDOWN_PCT": "15",
        "POLL_SECONDS": "5",
        "MARKET_TYPE": "futures",
        "LEVERAGE": "3",
        "MARGIN_MODE": "cross",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    cfg = load_config()
    assert cfg.exchange_id == "bybit"
    assert cfg.symbol == "ETH/USDT"
    assert cfg.num_grids == 8
    assert cfg.market_type == "futures"
    assert cfg.leverage == 3
    assert cfg.margin_mode == "cross"
    assert cfg.is_futures is True


def test_multi_mode_does_not_require_prices():
    # В мультирежиме ручной диапазон не нужен — нули допустимы.
    cfg = make_config(multi_symbol_mode=True, lower_price=0.0, upper_price=0.0)
    cfg.validate()


def test_multi_mode_invalid_grid_range_pct():
    with pytest.raises(ConfigError):
        make_config(multi_symbol_mode=True, lower_price=0.0, upper_price=0.0,
                    grid_range_pct=0.0).validate()
    with pytest.raises(ConfigError):
        make_config(multi_symbol_mode=True, lower_price=0.0, upper_price=0.0,
                    grid_range_pct=100.0).validate()


def test_multi_mode_invalid_wind_down_policy():
    with pytest.raises(ConfigError):
        make_config(multi_symbol_mode=True, lower_price=0.0, upper_price=0.0,
                    wind_down_policy="freeze").validate()


def test_multi_mode_requires_at_least_one_category():
    with pytest.raises(ConfigError):
        make_config(multi_symbol_mode=True, lower_price=0.0, upper_price=0.0,
                    num_top_volume=0, num_top_gainers=0, num_top_volatile=0).validate()


def test_multi_mode_zero_weights_sum_raises():
    with pytest.raises(ConfigError):
        make_config(multi_symbol_mode=True, lower_price=0.0, upper_price=0.0,
                    capital_weight_volume=0.0, capital_weight_gainer=0.0,
                    capital_weight_volatile=0.0).validate()


def test_multi_mode_portfolio_drawdown_range():
    with pytest.raises(ConfigError):
        make_config(multi_symbol_mode=True, lower_price=0.0, upper_price=0.0,
                    max_portfolio_drawdown_pct=0.0).validate()


def test_load_config_multi_mode_from_env(monkeypatch):
    env = {
        "MULTI_SYMBOL_MODE": "true",
        "QUOTE_CURRENCY": "USDT",
        "TOTAL_CAPITAL": "1000",
        "NUM_TOP_VOLUME": "4",
        "EXCLUDE_SYMBOLS": "doge, shib",
        "STABLECOINS": "usdt,usdc",
        "LEVERAGED_TOKEN_PATTERNS": "up,down,3l",
        "GRID_RANGE_PCT": "8",
        "WIND_DOWN_POLICY": "cancel",
        "DRY_RUN": "true",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = load_config()
    assert cfg.multi_symbol_mode is True
    assert cfg.num_top_volume == 4
    assert cfg.exclude_symbols == ("DOGE", "SHIB")
    assert cfg.stablecoins == ("USDT", "USDC")
    assert cfg.leveraged_token_patterns == ("UP", "DOWN", "3L")
    assert cfg.grid_range_pct == 8.0
    assert cfg.wind_down_policy == "cancel"


def test_load_config_spot_leverage_conflict(monkeypatch):
    monkeypatch.setenv("MARKET_TYPE", "spot")
    monkeypatch.setenv("LEVERAGE", "5")
    monkeypatch.setenv("LOWER_PRICE", "100")
    monkeypatch.setenv("UPPER_PRICE", "200")
    monkeypatch.setenv("TOTAL_CAPITAL", "1000")
    monkeypatch.setenv("DRY_RUN", "true")
    with pytest.raises(ConfigError):
        load_config()
