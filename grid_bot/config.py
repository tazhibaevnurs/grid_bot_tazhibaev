"""Загрузка и строгая валидация конфигурации из переменных окружения (.env).

Вся конфигурация бота собрана в иммутабельный dataclass :class:`Config`.
Валидация выполняется при старте: при некорректных значениях бросается
:class:`ConfigError`, чтобы бот НЕ запускался с опасными настройками
(например, spot + плечо), а не игнорировал их тихо.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple

try:
    # python-dotenv не обязателен, но если установлен — подхватываем .env
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - окружение без python-dotenv
    load_dotenv = None  # type: ignore[assignment]


SUPPORTED_EXCHANGES = ("binance", "bybit")
SUPPORTED_MARKET_TYPES = ("spot", "futures")
SUPPORTED_MARGIN_MODES = ("isolated", "cross")
SUPPORTED_WIND_DOWN_POLICIES = ("hold", "cancel")


class ConfigError(ValueError):
    """Ошибка конфигурации. Бросается при невалидных значениях .env."""


def _get_bool(name: str, default: bool) -> bool:
    """Прочитать булеву переменную окружения.

    Истинными считаются значения: ``1, true, yes, y, on`` (регистр не важен).
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _get_str(name: str, default: Optional[str] = None) -> Optional[str]:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw != "" else default


def _get_float(name: str, default: Optional[float] = None) -> Optional[float]:
    raw = _get_str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть числом, получено: {raw!r}") from exc


def _get_int(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = _get_str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом, получено: {raw!r}") from exc


def _get_list(name: str, default: Tuple[str, ...] = ()) -> Tuple[str, ...]:
    """Прочитать список из CSV-переменной окружения (через запятую).

    Пустые элементы отбрасываются, значения приводятся к верхнему регистру и
    обрезаются по пробелам. Пустая/незаданная переменная даёт ``default``.
    """
    raw = _get_str(name)
    if raw is None:
        return default
    items = tuple(p.strip().upper() for p in raw.split(",") if p.strip())
    return items if items else default


@dataclass(frozen=True)
class Config:
    """Полная конфигурация бота.

    Атрибуты соответствуют переменным из ``.env`` (см. ``.env.example``).
    """

    exchange_id: str
    api_key: Optional[str]
    api_secret: Optional[str]
    use_testnet: bool
    dry_run: bool
    symbol: str
    lower_price: float
    upper_price: float
    num_grids: int
    total_capital: float
    max_drawdown_pct: float
    poll_seconds: float
    market_type: str
    leverage: int
    margin_mode: str

    # --- мультитикерный режим (по умолчанию выключен для совместимости) ---
    multi_symbol_mode: bool = False
    quote_currency: str = "USDT"
    num_top_volume: int = 3
    num_top_gainers: int = 3
    num_top_volatile: int = 3
    max_concurrent_symbols: int = 8
    min_24h_volume_usdt: float = 5_000_000.0
    rescan_interval_hours: float = 12.0
    exclude_symbols: Tuple[str, ...] = ()
    stablecoins: Tuple[str, ...] = ("USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD")
    leveraged_token_patterns: Tuple[str, ...] = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")
    # Вес капитала по категориям (нормализуется в боте, сумма не обязана = 1).
    capital_weight_volume: float = 1.0
    capital_weight_gainer: float = 0.5
    capital_weight_volatile: float = 0.4
    # Ширина авто-диапазона сетки в % от текущей цены (для мультирежима).
    grid_range_pct: float = 12.0
    # Портфельный kill switch: просадка по всему портфелю.
    max_portfolio_drawdown_pct: float = 25.0
    # Что делать с символом, выпавшим из universe: hold (дать доиграть) / cancel.
    wind_down_policy: str = "hold"

    @property
    def is_futures(self) -> bool:
        return self.market_type == "futures"

    @property
    def is_spot(self) -> bool:
        return self.market_type == "spot"

    @property
    def category_weights(self) -> dict:
        """Веса капитала по категориям (volume/gainer/volatile/manual)."""
        return {
            "volume": self.capital_weight_volume,
            "gainer": self.capital_weight_gainer,
            "volatile": self.capital_weight_volatile,
        }

    def validate(self) -> None:
        """Проверить все инварианты конфигурации.

        Бросает :class:`ConfigError` при первой найденной проблеме.
        Это вызывается при старте — лучше упасть сразу, чем потерять деньги.
        """
        if self.exchange_id not in SUPPORTED_EXCHANGES:
            raise ConfigError(
                f"EXCHANGE_ID={self.exchange_id!r} не поддерживается. "
                f"Допустимо: {', '.join(SUPPORTED_EXCHANGES)}"
            )

        if self.market_type not in SUPPORTED_MARKET_TYPES:
            raise ConfigError(
                f"MARKET_TYPE={self.market_type!r} не поддерживается. "
                f"Допустимо: {', '.join(SUPPORTED_MARKET_TYPES)}"
            )

        if self.margin_mode not in SUPPORTED_MARGIN_MODES:
            raise ConfigError(
                f"MARGIN_MODE={self.margin_mode!r} не поддерживается. "
                f"Допустимо: {', '.join(SUPPORTED_MARGIN_MODES)}"
            )

        # SYMBOL и ручной диапазон обязательны только в режиме одного тикера.
        # В мультитикерном режиме символы и диапазоны подбираются автоматически.
        if not self.multi_symbol_mode:
            if not self.symbol:
                raise ConfigError("SYMBOL не задан.")

            if self.lower_price <= 0 or self.upper_price <= 0:
                raise ConfigError("LOWER_PRICE и UPPER_PRICE должны быть > 0.")

            if self.lower_price >= self.upper_price:
                raise ConfigError(
                    f"LOWER_PRICE ({self.lower_price}) должна быть строго меньше "
                    f"UPPER_PRICE ({self.upper_price})."
                )

        if self.num_grids < 2:
            raise ConfigError("NUM_GRIDS должно быть >= 2 (минимум один интервал).")

        if self.total_capital <= 0:
            raise ConfigError("TOTAL_CAPITAL должен быть > 0.")

        if not (0 < self.max_drawdown_pct <= 100):
            raise ConfigError("MAX_DRAWDOWN_PCT должен быть в диапазоне (0, 100].")

        if self.poll_seconds <= 0:
            raise ConfigError("POLL_SECONDS должен быть > 0.")

        if self.leverage < 1:
            raise ConfigError("LEVERAGE должно быть целым числом >= 1.")

        # Ключевой инвариант безопасности: spot не может иметь плечо.
        if self.is_spot and self.leverage != 1:
            raise ConfigError(
                "MARKET_TYPE=spot несовместим с LEVERAGE != 1. "
                f"Получено LEVERAGE={self.leverage}. На споте плеча нет — "
                "установите LEVERAGE=1 или переключитесь на MARKET_TYPE=futures."
            )

        # Реальные сделки без ключей невозможны.
        if not self.dry_run and (not self.api_key or not self.api_secret):
            raise ConfigError(
                "DRY_RUN=false требует заданных API_KEY и API_SECRET в .env."
            )

        # --- инварианты мультитикерного режима ---------------------------
        if self.multi_symbol_mode:
            if not self.quote_currency:
                raise ConfigError("QUOTE_CURRENCY не задан для мультитикерного режима.")

            for name, value in (
                ("NUM_TOP_VOLUME", self.num_top_volume),
                ("NUM_TOP_GAINERS", self.num_top_gainers),
                ("NUM_TOP_VOLATILE", self.num_top_volatile),
            ):
                if value < 0:
                    raise ConfigError(f"{name} не может быть отрицательным.")

            if (self.num_top_volume + self.num_top_gainers + self.num_top_volatile) < 1:
                raise ConfigError(
                    "Хотя бы одна из NUM_TOP_VOLUME/NUM_TOP_GAINERS/NUM_TOP_VOLATILE "
                    "должна быть >= 1, иначе портфель будет пустым."
                )

            if self.max_concurrent_symbols < 1:
                raise ConfigError("MAX_CONCURRENT_SYMBOLS должно быть >= 1.")

            if self.min_24h_volume_usdt < 0:
                raise ConfigError("MIN_24H_VOLUME_USDT не может быть отрицательным.")

            if self.rescan_interval_hours <= 0:
                raise ConfigError("RESCAN_INTERVAL_HOURS должен быть > 0.")

            if self.grid_range_pct <= 0 or self.grid_range_pct >= 100:
                raise ConfigError("GRID_RANGE_PCT должен быть в диапазоне (0, 100).")

            if not (0 < self.max_portfolio_drawdown_pct <= 100):
                raise ConfigError(
                    "MAX_PORTFOLIO_DRAWDOWN_PCT должен быть в диапазоне (0, 100]."
                )

            for name, value in (
                ("CAPITAL_WEIGHT_VOLUME", self.capital_weight_volume),
                ("CAPITAL_WEIGHT_GAINER", self.capital_weight_gainer),
                ("CAPITAL_WEIGHT_VOLATILE", self.capital_weight_volatile),
            ):
                if value < 0:
                    raise ConfigError(f"{name} не может быть отрицательным.")

            if (
                self.capital_weight_volume
                + self.capital_weight_gainer
                + self.capital_weight_volatile
            ) <= 0:
                raise ConfigError(
                    "Сумма CAPITAL_WEIGHT_* должна быть > 0 (иначе нечем взвешивать капитал)."
                )

            if self.wind_down_policy not in SUPPORTED_WIND_DOWN_POLICIES:
                raise ConfigError(
                    f"WIND_DOWN_POLICY={self.wind_down_policy!r} не поддерживается. "
                    f"Допустимо: {', '.join(SUPPORTED_WIND_DOWN_POLICIES)}"
                )


def load_config(dotenv_path: Optional[str] = None) -> Config:
    """Загрузить конфигурацию из окружения (и из .env, если доступно).

    :param dotenv_path: путь к .env-файлу; если ``None`` — ищется автоматически.
    :returns: провалидированный :class:`Config`.
    :raises ConfigError: при некорректной конфигурации.
    """
    if load_dotenv is not None:
        load_dotenv(dotenv_path=dotenv_path, override=False)

    config = Config(
        exchange_id=(_get_str("EXCHANGE_ID", "binance") or "binance").lower(),
        api_key=_get_str("API_KEY"),
        api_secret=_get_str("API_SECRET"),
        use_testnet=_get_bool("USE_TESTNET", True),
        dry_run=_get_bool("DRY_RUN", True),
        symbol=_get_str("SYMBOL", "BTC/USDT") or "BTC/USDT",
        lower_price=_get_float("LOWER_PRICE", 0.0) or 0.0,
        upper_price=_get_float("UPPER_PRICE", 0.0) or 0.0,
        num_grids=_get_int("NUM_GRIDS", 10) or 10,
        total_capital=_get_float("TOTAL_CAPITAL", 0.0) or 0.0,
        max_drawdown_pct=_get_float("MAX_DRAWDOWN_PCT", 20.0) or 20.0,
        poll_seconds=_get_float("POLL_SECONDS", 10.0) or 10.0,
        market_type=(_get_str("MARKET_TYPE", "spot") or "spot").lower(),
        leverage=_get_int("LEVERAGE", 1) or 1,
        margin_mode=(_get_str("MARGIN_MODE", "isolated") or "isolated").lower(),
        multi_symbol_mode=_get_bool("MULTI_SYMBOL_MODE", False),
        quote_currency=(_get_str("QUOTE_CURRENCY", "USDT") or "USDT").upper(),
        num_top_volume=_get_int("NUM_TOP_VOLUME", 3) or 0,
        num_top_gainers=_get_int("NUM_TOP_GAINERS", 3) or 0,
        num_top_volatile=_get_int("NUM_TOP_VOLATILE", 3) or 0,
        max_concurrent_symbols=_get_int("MAX_CONCURRENT_SYMBOLS", 8) or 8,
        min_24h_volume_usdt=_get_float("MIN_24H_VOLUME_USDT", 5_000_000.0) or 0.0,
        rescan_interval_hours=_get_float("RESCAN_INTERVAL_HOURS", 12.0) or 12.0,
        exclude_symbols=_get_list("EXCLUDE_SYMBOLS", ()),
        stablecoins=_get_list(
            "STABLECOINS", ("USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD")
        ),
        leveraged_token_patterns=_get_list(
            "LEVERAGED_TOKEN_PATTERNS", ("UP", "DOWN", "BULL", "BEAR", "3L", "3S")
        ),
        capital_weight_volume=_get_float("CAPITAL_WEIGHT_VOLUME", 1.0) or 0.0,
        capital_weight_gainer=_get_float("CAPITAL_WEIGHT_GAINER", 0.5) or 0.0,
        capital_weight_volatile=_get_float("CAPITAL_WEIGHT_VOLATILE", 0.4) or 0.0,
        grid_range_pct=_get_float("GRID_RANGE_PCT", 12.0) or 12.0,
        max_portfolio_drawdown_pct=_get_float("MAX_PORTFOLIO_DRAWDOWN_PCT", 25.0) or 25.0,
        wind_down_policy=(_get_str("WIND_DOWN_POLICY", "hold") or "hold").lower(),
    )
    config.validate()
    return config
