"""Слой хранения данных бота в SQLite (без внешних зависимостей).

Хранит исполненные сделки, снимки equity и важные события. База используется
двумя процессами: бот (`main.py`) пишет, веб-дашборд (`web/app.py`) читает.
Включён WAL-режим, чтобы чтение не блокировало запись.

ВАЖНО: здесь НЕ хранятся API-ключи/секреты. В таблицах нет полей под них.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DB_PATH = "bot_data.db"


def utc_now_iso() -> str:
    """Текущее время в ISO8601 (UTC). Строковый формат сортируется лексикографически."""
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """Обёртка над SQLite с методами записи и чтения данных бота."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI выполняет sync-эндпоинты в пуле потоков.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT    NOT NULL,
                    exchange    TEXT    NOT NULL,
                    symbol      TEXT    NOT NULL,
                    market_type TEXT    NOT NULL,
                    side        TEXT    NOT NULL,
                    price       REAL    NOT NULL,
                    amount      REAL    NOT NULL,
                    quote_value REAL    NOT NULL,
                    order_id    TEXT,
                    dry_run     INTEGER NOT NULL,
                    leverage    INTEGER NOT NULL DEFAULT 1,
                    category    TEXT    NOT NULL DEFAULT 'manual'
                );

                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp      TEXT    NOT NULL,
                    equity         REAL    NOT NULL,
                    quote_currency TEXT    NOT NULL,
                    symbol         TEXT
                );

                CREATE TABLE IF NOT EXISTS events (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT    NOT NULL,
                    level     TEXT    NOT NULL,
                    message   TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS universe_history (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT    NOT NULL,
                    symbol    TEXT    NOT NULL,
                    category  TEXT    NOT NULL,
                    action    TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processes (
                    name           TEXT PRIMARY KEY,
                    pid            INTEGER NOT NULL,
                    status         TEXT    NOT NULL,
                    started_at     TEXT    NOT NULL,
                    last_heartbeat TEXT    NOT NULL,
                    detail         TEXT
                );
                """
            )
            # Миграции колонок ДО создания индексов, которые на них ссылаются
            # (иначе на старых базах индекс по equity_snapshots.symbol упадёт).
            self._migrate()
            self._conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp);
                CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
                CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(timestamp);
                CREATE INDEX IF NOT EXISTS idx_equity_symbol ON equity_snapshots(symbol);
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
                CREATE INDEX IF NOT EXISTS idx_universe_ts ON universe_history(timestamp);
                CREATE INDEX IF NOT EXISTS idx_universe_symbol ON universe_history(symbol);
                """
            )
            self._conn.commit()

    def _migrate(self) -> None:
        """Лёгкие миграции для уже существующих баз (добавление новых колонок)."""
        # trades.category — для старых баз без этой колонки.
        trade_cols = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(trades)")
        }
        if "category" not in trade_cols:
            self._conn.execute(
                "ALTER TABLE trades ADD COLUMN category TEXT NOT NULL DEFAULT 'manual'"
            )
        # equity_snapshots.symbol — per-symbol equity (NULL = весь портфель).
        eq_cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(equity_snapshots)")
        }
        if "symbol" not in eq_cols:
            self._conn.execute("ALTER TABLE equity_snapshots ADD COLUMN symbol TEXT")

    # --- запись -----------------------------------------------------------

    def record_trade(
        self,
        *,
        exchange: str,
        symbol: str,
        market_type: str,
        side: str,
        price: float,
        amount: float,
        order_id: Optional[str],
        dry_run: bool,
        leverage: int = 1,
        category: str = "manual",
        timestamp: Optional[str] = None,
    ) -> int:
        """Записать исполненную сделку. ``quote_value`` считается как price*amount."""
        ts = timestamp or utc_now_iso()
        quote_value = float(price) * float(amount)
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO trades
                    (timestamp, exchange, symbol, market_type, side, price,
                     amount, quote_value, order_id, dry_run, leverage, category)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts, exchange, symbol, market_type, side, float(price),
                    float(amount), quote_value, order_id, int(bool(dry_run)),
                    int(leverage), category,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def record_equity_snapshot(
        self,
        *,
        equity: float,
        quote_currency: str,
        symbol: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> int:
        """Записать снимок equity для графика.

        ``symbol=None`` означает агрегированный портфельный снимок (используется
        графиком equity curve). Снимки с конкретным символом — для per-symbol
        статистики в разделе "Активные тикеры".
        """
        ts = timestamp or utc_now_iso()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO equity_snapshots (timestamp, equity, quote_currency, symbol) "
                "VALUES (?, ?, ?, ?)",
                (ts, float(equity), quote_currency, symbol),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def record_universe_change(
        self,
        symbol: str,
        category: str,
        action: str,
        timestamp: Optional[str] = None,
    ) -> int:
        """Записать изменение состава портфеля (action: ``added`` / ``removed``)."""
        ts = timestamp or utc_now_iso()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO universe_history (timestamp, symbol, category, action) "
                "VALUES (?, ?, ?, ?)",
                (ts, symbol, category, action),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def record_event(
        self,
        level: str,
        message: str,
        timestamp: Optional[str] = None,
    ) -> int:
        """Записать важное событие (старт/стоп, kill switch, ошибка биржи)."""
        ts = timestamp or utc_now_iso()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (timestamp, level, message) VALUES (?, ?, ?)",
                (ts, level, message),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def upsert_process_heartbeat(
        self,
        name: str,
        pid: int,
        *,
        status: str = "running",
        detail: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        """Обновить heartbeat процесса (бот или дашборд) для мониторинга на UI."""
        now = timestamp or utc_now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT pid, started_at, status FROM processes WHERE name = ?", (name,)
            ).fetchone()
            if row and row["pid"] == pid and row["status"] == "running":
                started_at = row["started_at"]
            else:
                started_at = now
            self._conn.execute(
                """
                INSERT INTO processes (name, pid, status, started_at, last_heartbeat, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    pid = excluded.pid,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    last_heartbeat = excluded.last_heartbeat,
                    detail = excluded.detail
                """,
                (name, int(pid), status, started_at, now, detail),
            )
            self._conn.commit()

    def mark_process_stopped(self, name: str, *, timestamp: Optional[str] = None) -> None:
        """Пометить процесс как остановленный (при завершении бота/дашборда)."""
        now = timestamp or utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE processes
                SET status = 'stopped', last_heartbeat = ?
                WHERE name = ?
                """,
                (now, name),
            )
            self._conn.commit()

    def get_process(self, name: str) -> Optional[Dict[str, Any]]:
        """Получить запись о процессе по имени."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM processes WHERE name = ?", (name,)
            ).fetchone()
        return dict(row) if row else None

    def get_processes(self) -> List[Dict[str, Any]]:
        """Все зарегистрированные процессы."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM processes ORDER BY name ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- чтение -----------------------------------------------------------

    def get_trades(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        side: Optional[str] = None,
        symbol: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        ascending: bool = False,
    ) -> List[Dict[str, Any]]:
        """Список сделок с фильтрами и пагинацией (по умолчанию новые сверху)."""
        clauses: List[str] = []
        params: List[Any] = []
        if side:
            clauses.append("side = ?")
            params.append(side)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if date_from:
            clauses.append("timestamp >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("timestamp <= ?")
            params.append(date_to)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "ASC" if ascending else "DESC"
        params.extend([int(limit), int(offset)])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM trades{where} ORDER BY timestamp {order}, id {order} "
                f"LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def count_trades(
        self,
        *,
        side: Optional[str] = None,
        symbol: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> int:
        """Количество сделок под фильтр (для пагинации)."""
        clauses: List[str] = []
        params: List[Any] = []
        if side:
            clauses.append("side = ?")
            params.append(side)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if date_from:
            clauses.append("timestamp >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("timestamp <= ?")
            params.append(date_to)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM trades{where}", params
            ).fetchone()
        return int(row["c"]) if row else 0

    def get_all_trades_chronological(
        self, symbol: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Все сделки в хронологическом порядке (старые сверху) — для FIFO P&L."""
        clauses: List[str] = []
        params: List[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM trades{where} ORDER BY timestamp ASC, id ASC", params
            ).fetchall()
        return [dict(r) for r in rows]

    def get_equity_history(
        self,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        portfolio_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """Временной ряд equity (старые сверху) для графика.

        По умолчанию возвращает только портфельные снимки (``symbol IS NULL``),
        чтобы график equity curve показывал портфель целиком, а не сумму
        дублирующихся per-symbol точек.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if portfolio_only:
            clauses.append("symbol IS NULL")
        if date_from:
            clauses.append("timestamp >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("timestamp <= ?")
            params.append(date_to)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT timestamp, equity, quote_currency FROM equity_snapshots"
                f"{where} ORDER BY timestamp ASC, id ASC",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def symbol_equity_bounds(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Первый и последний снимок equity для символа (для per-symbol P&L)."""
        with self._lock:
            first = self._conn.execute(
                "SELECT equity FROM equity_snapshots WHERE symbol = ? "
                "ORDER BY timestamp ASC, id ASC LIMIT 1",
                (symbol,),
            ).fetchone()
            last = self._conn.execute(
                "SELECT equity FROM equity_snapshots WHERE symbol = ? "
                "ORDER BY timestamp DESC, id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        if not first or not last:
            return None
        return {"start_equity": float(first["equity"]), "current_equity": float(last["equity"])}

    def get_universe_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Последние изменения состава портфеля (новые сверху)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM universe_history ORDER BY timestamp DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def current_universe(self) -> Dict[str, Dict[str, str]]:
        """Текущий состав портфеля по последнему действию для каждого символа.

        - ``added``     -> символ присутствует со статусом ``active``;
        - ``wind_down`` -> символ присутствует со статусом ``wind_down``
          (выпал из выборки, но доигрывает открытые ордера);
        - ``removed``   -> символ убран из портфеля (отсутствует в результате).

        :returns: ``{symbol: {"category": ..., "status": ..., "since": timestamp}}``.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, category, action, timestamp FROM universe_history "
                "ORDER BY timestamp ASC, id ASC"
            ).fetchall()
        latest: Dict[str, Dict[str, str]] = {}
        for r in rows:
            action = r["action"]
            if action == "added":
                latest[r["symbol"]] = {
                    "category": r["category"],
                    "status": "active",
                    "since": r["timestamp"],
                }
            elif action == "wind_down":
                if r["symbol"] in latest:
                    latest[r["symbol"]]["status"] = "wind_down"
                else:
                    latest[r["symbol"]] = {
                        "category": r["category"],
                        "status": "wind_down",
                        "since": r["timestamp"],
                    }
            elif action == "removed":
                latest.pop(r["symbol"], None)
        return latest

    def get_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Последние важные события (новые сверху)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY timestamp DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def first_equity(self) -> Optional[Dict[str, Any]]:
        """Первый (стартовый) портфельный снимок equity (symbol IS NULL)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT timestamp, equity, quote_currency FROM equity_snapshots "
                "WHERE symbol IS NULL ORDER BY timestamp ASC, id ASC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def last_equity(self) -> Optional[Dict[str, Any]]:
        """Последний (текущий) портфельный снимок equity (symbol IS NULL)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT timestamp, equity, quote_currency FROM equity_snapshots "
                "WHERE symbol IS NULL ORDER BY timestamp DESC, id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def peak_equity(self) -> Optional[float]:
        """Максимальное значение портфельной equity (пик) — для расчёта просадки."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(equity) AS m FROM equity_snapshots WHERE symbol IS NULL"
            ).fetchone()
        if row and row["m"] is not None:
            return float(row["m"])
        return None

    def last_event_by_levels(self, levels: List[str]) -> Optional[Dict[str, Any]]:
        """Последнее событие с уровнем из списка (для определения статуса бота)."""
        placeholders = ",".join("?" for _ in levels)
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM events WHERE level IN ({placeholders}) "
                f"ORDER BY timestamp DESC, id DESC LIMIT 1",
                levels,
            ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
