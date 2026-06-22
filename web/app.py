"""FastAPI backend веб-дашборда статистики grid-бота.

Читает ту же SQLite-базу (``bot_data.db``), что пишет бот, и отдаёт сводку,
историю сделок (с FIFO-P&L), временной ряд equity и ленту событий.

БЕЗОПАСНОСТЬ:
- Сервер по умолчанию слушает ТОЛЬКО 127.0.0.1 (loopback), не 0.0.0.0 —
  это локальный инструмент для одного пользователя, без удалённого доступа.
- В базе НЕТ API-ключей/секретов, и backend их никогда не возвращает.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from grid_bot.storage import DEFAULT_DB_PATH, Storage
from grid_bot.strategy import DEFAULT_PROFILE, RISK_PROFILES

from .pnl import compute_fifo_pnl

# Корень проекта (на уровень выше web/) — нужен для запуска бота как процесса.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_LOG_PATH = PROJECT_ROOT / "bot_run.log"

# Путь к базе можно переопределить переменной окружения (по умолчанию bot_data.db).
DB_PATH = os.getenv("GRID_BOT_DB", DEFAULT_DB_PATH)

STATIC_DIR = Path(__file__).parent / "static"

# Если heartbeat старше этого порога — процесс считается «завис/офлайн».
PROCESS_STALE_SECONDS = int(os.getenv("PROCESS_STALE_SECONDS", "45"))

PROCESS_LABELS = {
    "grid_bot": "Grid Bot (main.py)",
    "dashboard": "Dashboard (web/app.py)",
}


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _heartbeat_age_seconds(last_heartbeat: str) -> float:
    hb = _parse_iso(last_heartbeat)
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - hb).total_seconds()


def _dashboard_heartbeat() -> None:
    """Обновить heartbeat текущего процесса дашборда."""
    storage.upsert_process_heartbeat(
        "dashboard",
        os.getpid(),
        status="running",
        detail="127.0.0.1:8000",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Регистрация процесса дашборда при старте и при остановке."""
    _dashboard_heartbeat()
    yield
    storage.mark_process_stopped("dashboard")


app = FastAPI(title="Grid Bot Dashboard", version="1.0.0", lifespan=lifespan)

# Одно подключение Storage на процесс дашборда (read-mostly).
storage = Storage(DB_PATH)


def _bot_status(events: List[Dict[str, Any]]) -> str:
    """Определить статус ВСЕГО бота по последним событиям: running/stopped/killed.

    KILLED ставится только когда остановлен сам бот по kill switch
    (портфельному или в одиночном режиме), а не когда остановлен отдельный
    символ — в портфеле остальные символы продолжают торговать.
    """
    for ev in events:  # события уже отсортированы по убыванию времени
        msg = ev.get("message", "").lower()
        if "остановлен по" in msg and "kill switch" in msg:
            return "killed"
        if "бот остановлен" in msg:
            return "stopped"
        if "бот запущен" in msg:
            return "running"
    return "stopped"


def _process_runtime_status(row: Optional[Dict[str, Any]]) -> str:
    """Статус процесса для UI: running / stale / stopped / offline."""
    if row is None:
        return "offline"
    if row.get("status") == "stopped":
        return "stopped"
    age = _heartbeat_age_seconds(row["last_heartbeat"])
    if age > PROCESS_STALE_SECONDS:
        return "stale"
    return "running"


def get_processes_payload() -> List[Dict[str, Any]]:
    """Сводка по известным процессам для дашборда."""
    _dashboard_heartbeat()
    known = {p["name"]: p for p in storage.get_processes()}
    result: List[Dict[str, Any]] = []
    for name in ("grid_bot", "dashboard"):
        row = known.get(name)
        runtime = _process_runtime_status(row)
        age = None
        if row:
            age = round(_heartbeat_age_seconds(row["last_heartbeat"]), 1)
        result.append(
            {
                "name": name,
                "label": PROCESS_LABELS.get(name, name),
                "status": runtime,
                "pid": row["pid"] if row else None,
                "started_at": row["started_at"] if row else None,
                "last_heartbeat": row["last_heartbeat"] if row else None,
                "heartbeat_age_sec": age,
                "detail": row["detail"] if row else None,
            }
        )
    return result


@app.get("/api/stats")
def get_stats() -> JSONResponse:
    """Сводная статистика для карточек дашборда."""
    first = storage.first_equity()
    last = storage.last_equity()
    peak = storage.peak_equity()
    events = storage.get_events(limit=200)
    total_trades = storage.count_trades()

    start_equity = first["equity"] if first else None
    current_equity = last["equity"] if last else None
    quote_currency = (last or first or {}).get("quote_currency")

    pnl_abs: Optional[float] = None
    pnl_pct: Optional[float] = None
    if start_equity is not None and current_equity is not None:
        pnl_abs = current_equity - start_equity
        if start_equity != 0:
            pnl_pct = pnl_abs / start_equity * 100.0

    drawdown_pct: Optional[float] = None
    if peak and current_equity is not None and peak != 0:
        drawdown_pct = max(0.0, (peak - current_equity) / peak * 100.0)

    # Win rate по закрытым сделкам (с реализованным P&L).
    trades = storage.get_all_trades_chronological()
    pnl_map = compute_fifo_pnl(trades)
    closed = [v for v in pnl_map.values() if v is not None]
    wins = sum(1 for v in closed if v > 0)
    win_rate = (wins / len(closed) * 100.0) if closed else None
    realized_pnl_total = sum(closed) if closed else 0.0

    # Режим/плечо/dry_run берём из самой свежей сделки (ключи не хранятся!).
    last_trade = trades[-1] if trades else None
    market_type = last_trade["market_type"] if last_trade else None
    leverage = last_trade["leverage"] if last_trade else None
    dry_run = bool(last_trade["dry_run"]) if last_trade else None

    processes = get_processes_payload()
    bot_process = next((p for p in processes if p["name"] == "grid_bot"), None)
    bot_runtime = bot_process["status"] if bot_process else "offline"
    # Статус торговли: события + живой heartbeat процесса бота.
    trade_status = _bot_status(events)
    if trade_status == "running" and bot_runtime in ("stale", "offline", "stopped"):
        trade_status = "stopped" if bot_runtime == "stopped" else "stale"

    # Портфель: активные символы и breakdown по категориям.
    active_symbols = _active_symbols(trades)
    multi_symbol_mode = any(s["category"] != "manual" for s in active_symbols) or len(
        active_symbols
    ) > 1
    categories_breakdown = _categories_breakdown(active_symbols)

    return JSONResponse(
        {
            "status": trade_status,
            "processes": processes,
            "current_equity": current_equity,
            "start_equity": start_equity,
            "quote_currency": quote_currency,
            "pnl_abs": pnl_abs,
            "pnl_pct": pnl_pct,
            "realized_pnl_total": realized_pnl_total,
            "win_rate": win_rate,
            "closed_trades": len(closed),
            "total_trades": total_trades,
            "drawdown_pct": drawdown_pct,
            "peak_equity": peak,
            "market_type": market_type,
            "leverage": leverage,
            "dry_run": dry_run,
            "multi_symbol_mode": multi_symbol_mode,
            "active_symbol_count": len(active_symbols),
            "categories": categories_breakdown,
        }
    )


def _active_symbols(all_trades: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Список активных символов портфеля с per-symbol equity и FIFO-P&L."""
    universe = storage.current_universe()
    if all_trades is None:
        all_trades = storage.get_all_trades_chronological()
    # Сгруппируем сделки по символам один раз.
    trades_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for t in all_trades:
        trades_by_symbol.setdefault(t["symbol"], []).append(t)

    result: List[Dict[str, Any]] = []
    for symbol, meta in universe.items():
        bounds = storage.symbol_equity_bounds(symbol)
        start_eq = bounds["start_equity"] if bounds else None
        cur_eq = bounds["current_equity"] if bounds else None

        sym_trades = trades_by_symbol.get(symbol, [])
        pnl_map = compute_fifo_pnl(sym_trades)
        closed = [v for v in pnl_map.values() if v is not None]
        realized = sum(closed) if closed else 0.0

        pnl_abs = None
        pnl_pct = None
        if start_eq is not None and cur_eq is not None:
            pnl_abs = cur_eq - start_eq
            if start_eq != 0:
                pnl_pct = pnl_abs / start_eq * 100.0

        result.append(
            {
                "symbol": symbol,
                "category": meta.get("category", "manual"),
                "status": meta.get("status", "active"),
                "since": meta.get("since"),
                "start_equity": start_eq,
                "current_equity": cur_eq,
                "pnl_abs": pnl_abs,
                "pnl_pct": pnl_pct,
                "realized_pnl": realized,
                "trades": len(sym_trades),
            }
        )
    # Сортировка: по текущей equity убыванию (None в конец).
    result.sort(key=lambda r: (r["current_equity"] is None, -(r["current_equity"] or 0.0)))
    return result


def _categories_breakdown(active_symbols: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Свод по категориям: число символов и суммарная equity/P&L."""
    agg: Dict[str, Dict[str, float]] = {}
    for s in active_symbols:
        cat = s["category"]
        bucket = agg.setdefault(cat, {"count": 0, "equity": 0.0, "pnl_abs": 0.0})
        bucket["count"] += 1
        if s["current_equity"] is not None:
            bucket["equity"] += s["current_equity"]
        if s["pnl_abs"] is not None:
            bucket["pnl_abs"] += s["pnl_abs"]
    return [
        {
            "category": cat,
            "count": int(v["count"]),
            "equity": v["equity"],
            "pnl_abs": v["pnl_abs"],
        }
        for cat, v in sorted(agg.items())
    ]


@app.get("/api/trades")
def get_trades(
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    side: Optional[str] = Query(None, pattern="^(buy|sell)$"),
    symbol: Optional[str] = None,
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
) -> JSONResponse:
    """Список сделок с пагинацией/фильтрами и реализованным P&L по каждой."""
    # FIFO считаем по всей хронологии символа, чтобы P&L был корректным,
    # затем проставляем значение к отфильтрованной странице.
    all_chrono = storage.get_all_trades_chronological(symbol=symbol)
    pnl_map = compute_fifo_pnl(all_chrono)

    page = storage.get_trades(
        limit=limit,
        offset=offset,
        side=side,
        symbol=symbol,
        date_from=date_from,
        date_to=date_to,
        ascending=False,
    )
    for row in page:
        row["realized_pnl"] = pnl_map.get(row["id"])
        row["dry_run"] = bool(row["dry_run"])

    total = storage.count_trades(
        side=side, symbol=symbol, date_from=date_from, date_to=date_to
    )
    return JSONResponse(
        {"trades": page, "total": total, "limit": limit, "offset": offset}
    )


@app.get("/api/equity-history")
def get_equity_history(
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
) -> JSONResponse:
    """Временной ряд equity для графика."""
    series = storage.get_equity_history(date_from=date_from, date_to=date_to)
    return JSONResponse({"equity": series})


@app.get("/api/events")
def get_events(limit: int = Query(100, ge=1, le=1000)) -> JSONResponse:
    """Последние важные события (kill switch, старт/стоп, ошибки)."""
    return JSONResponse({"events": storage.get_events(limit=limit)})


@app.get("/api/processes")
def get_processes() -> JSONResponse:
    """Статус процессов: grid_bot и dashboard (heartbeat из bot_data.db)."""
    return JSONResponse({"processes": get_processes_payload()})


@app.get("/api/active-symbols")
def get_active_symbols() -> JSONResponse:
    """Активные тикеры портфеля: символ/категория/equity/P&L/статус."""
    symbols = _active_symbols()
    return JSONResponse(
        {"symbols": symbols, "categories": _categories_breakdown(symbols)}
    )


@app.get("/api/universe-history")
def get_universe_history(limit: int = Query(100, ge=1, le=1000)) -> JSONResponse:
    """История изменений состава портфеля (added/wind_down/removed)."""
    return JSONResponse({"universe": storage.get_universe_history(limit=limit)})


# ---------------------------------------------------------------------------
# Пульт управления ботом (дашборд пишет настройки в таблицу control и при
# необходимости перезапускает процесс бота).
# ---------------------------------------------------------------------------


# Хэндлы запущенных процессов бота — чтобы reap'ать их (иначе остаются зомби).
_child_procs: List[subprocess.Popen] = []


def _reap_children() -> None:
    """Подобрать (reap) завершившихся дочерних ботов, чтобы не плодить зомби."""
    for proc in list(_child_procs):
        if proc.poll() is not None:  # poll() пожинает зомби-потомка
            _child_procs.remove(proc)


def _bot_runtime_status() -> str:
    """Статус процесса бота: running / stale / stopped / offline.

    Если heartbeat «свежий», но процесс физически мёртв/зомби — считаем stopped.
    """
    _reap_children()
    row = storage.get_process("grid_bot")
    status = _process_runtime_status(row)
    if status in ("running", "stale") and row and not _pid_alive(row["pid"]):
        return "stopped"
    return status


def start_bot() -> Dict[str, Any]:
    """Запустить процесс бота, если он ещё не запущен (отдельная сессия)."""
    _reap_children()
    if _bot_runtime_status() in ("running", "stale"):
        return {"ok": True, "message": "Бот уже запущен."}
    try:
        log = open(BOT_LOG_PATH, "a")
        proc = subprocess.Popen(
            [sys.executable, "-m", "grid_bot.main"],
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # детач: переживает reload дашборда
        )
        _child_procs.append(proc)
        return {"ok": True, "message": "Бот запускается."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Не удалось запустить бота: {exc}"}


def stop_bot() -> Dict[str, Any]:
    """Остановить процесс бота по PID из heartbeat (SIGTERM -> штатный shutdown)."""
    row = storage.get_process("grid_bot")
    if not row or _bot_runtime_status() in ("stopped", "offline"):
        return {"ok": True, "message": "Бот не запущен."}
    pid = row["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        storage.mark_process_stopped("grid_bot")
        return {"ok": True, "message": "Процесс уже завершён."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Не удалось остановить бота: {exc}"}
    # Дождёмся, пока бот отметит себя остановленным (отмена ордеров и т.п.)
    # ИЛИ пока процесс физически не исчезнет (на случай не-graceful завершения).
    for _ in range(20):
        time.sleep(0.25)
        cur = storage.get_process("grid_bot")
        if not cur or cur["status"] == "stopped":
            break
        if not _pid_alive(pid):
            storage.mark_process_stopped("grid_bot")
            break
    return {"ok": True, "message": "Бот остановлен."}


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс. Зомби (<defunct>) считаем мёртвым.

    ``os.kill(pid, 0)`` возвращает успех и для зомби-процессов, поэтому
    дополнительно проверяем состояние через ``ps`` — состояние ``Z`` означает,
    что процесс уже завершился и ждёт reap родителем.
    """
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    try:
        out = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        state = out.stdout.strip()
        if state and state[0] == "Z":
            return False
    except Exception:  # noqa: BLE001 - ps недоступен -> не блокируем
        pass
    return True


def restart_bot() -> Dict[str, Any]:
    """Перезапустить бота (стоп -> старт), чтобы применить новые настройки."""
    stop_bot()
    time.sleep(0.5)
    return start_bot()


def _current_settings() -> Dict[str, Any]:
    """Текущие управляющие настройки (с дефолтами) для UI."""
    controls = storage.get_controls()
    profile = controls.get("risk_profile", DEFAULT_PROFILE)
    try:
        max_symbols = int(controls.get("max_symbols", "8"))
    except (TypeError, ValueError):
        max_symbols = 8
    market_type = controls.get("market_type", "spot")
    try:
        leverage = int(controls.get("leverage", "1"))
    except (TypeError, ValueError):
        leverage = 1
    return {
        "risk_profile": profile,
        "max_symbols": max_symbols,
        "market_type": market_type,
        "leverage": leverage,
        "bot_status": _bot_runtime_status(),
    }


@app.get("/api/control")
def get_control() -> JSONResponse:
    """Текущие настройки пульта + доступные профили стратегии."""
    profiles = [
        {
            "id": pid,
            "label": p["label"],
            "description": p["description"],
        }
        for pid, p in RISK_PROFILES.items()
    ]
    return JSONResponse({"settings": _current_settings(), "profiles": profiles})


@app.post("/api/control/symbols")
def set_symbols(count: int = Query(..., ge=1, le=30)) -> JSONResponse:
    """Задать число одновременно торгуемых символов (применяется при рескане)."""
    storage.set_control("max_symbols", str(count))
    storage.set_control("rescan_now", "1")  # применить немедленно
    storage.record_event("info", f"Дашборд: число символов изменено на {count} (применяется при рескане).")
    return JSONResponse({"ok": True, "message": f"Число символов = {count}. Пересканирование запрошено.", "settings": _current_settings()})


@app.post("/api/control/strategy")
def set_strategy(profile: str = Query(...)) -> JSONResponse:
    """Сменить профиль стратегии (риск/доходность). Требует перезапуска бота."""
    if profile not in RISK_PROFILES:
        return JSONResponse({"ok": False, "message": "Неизвестный профиль."}, status_code=400)
    storage.set_control("risk_profile", profile)
    storage.record_event("info", f"Дашборд: профиль стратегии -> {RISK_PROFILES[profile]['label']} (перезапуск бота).")
    result = restart_bot()
    return JSONResponse({**result, "settings": _current_settings()})


@app.post("/api/control/market")
def set_market(
    market_type: str = Query(..., pattern="^(spot|futures)$"),
    leverage: int = Query(2, ge=1, le=50),
) -> JSONResponse:
    """Переключить рынок spot<->futures (с плечом). Требует перезапуска бота."""
    storage.set_control("market_type", market_type)
    if market_type == "futures":
        storage.set_control("leverage", str(leverage))
    else:
        storage.set_control("leverage", "1")
    storage.record_event(
        "info",
        f"Дашборд: рынок -> {market_type.upper()}"
        + (f" (плечо {leverage}x)" if market_type == "futures" else "")
        + " (перезапуск бота).",
    )
    result = restart_bot()
    return JSONResponse({**result, "settings": _current_settings()})


@app.post("/api/bot/{action}")
def bot_action(action: str) -> JSONResponse:
    """Управление процессом бота: start / stop / restart."""
    if action == "start":
        result = start_bot()
    elif action == "stop":
        result = stop_bot()
    elif action == "restart":
        result = restart_bot()
    else:
        return JSONResponse({"ok": False, "message": "Неизвестное действие."}, status_code=400)
    return JSONResponse({**result, "settings": _current_settings()})


@app.get("/")
def index() -> FileResponse:
    """Главная страница дашборда."""
    return FileResponse(STATIC_DIR / "index.html")


# Статика (index.html, style.css, app.js, и т.п.).
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    """Запуск дашборда напрямую: python -m web.app (слушает только 127.0.0.1)."""
    import uvicorn

    # host=127.0.0.1 намеренно: НЕ открываем дашборд во внешнюю сеть.
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
