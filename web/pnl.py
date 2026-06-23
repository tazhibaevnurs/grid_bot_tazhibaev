"""Расчёт реализованного P&L по алгоритму FIFO (First In, First Out).

Грид-бот не закрывает позицию целиком — прибыль складывается из пар buy/sell
на соседних уровнях. Здесь каждая закрывающая сделка сопоставляется с самыми
старыми ещё не закрытыми противоположными сделками того же символа.

Поддерживается и futures-шорт: если позиция короткая (открыта продажей),
последующая покупка её закрывает (симметрично лонгу). Знак позиции
отслеживается через ``direction`` (+1 лонг / -1 шорт / 0 нет позиции).
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional

_EPS = 1e-12


def _fifo_books_process(
    trades_chronological: List[Dict[str, Any]],
) -> Dict[int, Optional[Dict[str, Any]]]:
    """FIFO по всем сделкам: P&L и цены входа/выхода для закрывающих ног."""
    details: Dict[int, Optional[Dict[str, Any]]] = {}
    books: Dict[str, Dict[str, Any]] = {}

    for trade in trades_chronological:
        symbol = trade["symbol"]
        book = books.setdefault(symbol, {"direction": 0, "lots": deque()})
        sign = 1 if trade["side"] == "buy" else -1
        amount = float(trade["amount"])
        price = float(trade["price"])

        if book["direction"] == 0 or book["direction"] == sign:
            book["direction"] = sign
            book["lots"].append([price, amount])
            details[trade["id"]] = None
            continue

        remaining = amount
        realized = 0.0
        closed_any = False
        entry_cost = 0.0
        closed_amount = 0.0
        position_direction = book["direction"]  # +1 лонг, -1 шорт
        lots: deque = book["lots"]
        while remaining > _EPS and lots:
            lot = lots[0]
            lot_price, lot_amount = lot[0], lot[1]
            matched = min(remaining, lot_amount)
            if book["direction"] == 1:
                realized += (price - lot_price) * matched
            else:
                realized += (lot_price - price) * matched
            closed_any = True
            entry_cost += lot_price * matched
            closed_amount += matched
            lot[1] -= matched
            remaining -= matched
            if lot[1] <= _EPS:
                lots.popleft()

        if not lots:
            book["direction"] = 0

        if remaining > _EPS:
            book["direction"] = sign
            lots.append([price, remaining])

        if not closed_any:
            details[trade["id"]] = None
            continue

        entry_price = entry_cost / closed_amount if closed_amount > 0 else price
        exit_price = price
        is_long = position_direction == 1
        details[trade["id"]] = {
            "realized_pnl": realized,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "closed_amount": closed_amount,
            "entry_quote_value": entry_cost,
            "quote_value": exit_price * closed_amount,
            "position_side": "long" if is_long else "short",
            "entry_side": "buy" if is_long else "sell",
            "exit_side": "sell" if is_long else "buy",
        }

    return details


def compute_fifo_pnl(trades_chronological: List[Dict[str, Any]]) -> Dict[int, Optional[float]]:
    """Посчитать реализованный P&L для каждой сделки.

    :param trades_chronological: сделки в хронологическом порядке (старые сверху).
        Каждая — dict с ключами ``id``, ``symbol``, ``side`` (buy/sell),
        ``price``, ``amount``.
    :returns: словарь ``{trade_id: realized_pnl}``. ``None`` — если сделка
        только открывает (часть) позиции и ничего не закрыла.
    """
    return {
        tid: (info["realized_pnl"] if info else None)
        for tid, info in compute_fifo_closure_details(trades_chronological).items()
    }


def compute_fifo_closure_details(
    trades_chronological: List[Dict[str, Any]],
) -> Dict[int, Optional[Dict[str, Any]]]:
    """FIFO-детали закрытия: вход, выход, объём и P&L по каждой закрывающей сделке."""
    return _fifo_books_process(trades_chronological)


def portfolio_pnl_metrics(
    start_equity: Optional[float],
    current_equity: Optional[float],
    realized_pnl_total: float,
    *,
    flat_threshold: float = 0.01,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Сводный P&L и equity для карточек дашборда (стабильно, без «мигания» нулём).

    Портфельные снимки equity иногда кратковременно «залипают» на стартовом
    значении между тиками бота. В таких случаях опираемся на реализованный P&L
    и синхронизируем отображаемую equity с P&L (``start + pnl``).

    :returns: ``(pnl_abs, pnl_pct, display_equity)``.
    """
    if start_equity is None:
        return None, None, current_equity

    equity_pnl: Optional[float] = None
    if current_equity is not None:
        equity_pnl = current_equity - start_equity

    pnl_abs = equity_pnl

    if (
        pnl_abs is not None
        and abs(pnl_abs) < flat_threshold
        and abs(realized_pnl_total) >= flat_threshold
    ):
        pnl_abs = realized_pnl_total
    elif (
        pnl_abs is not None
        and realized_pnl_total > pnl_abs + flat_threshold
        and realized_pnl_total >= flat_threshold
    ):
        pnl_abs = realized_pnl_total

    pnl_pct: Optional[float] = None
    if pnl_abs is not None and start_equity != 0:
        pnl_pct = pnl_abs / start_equity * 100.0

    display_equity = current_equity
    if pnl_abs is not None:
        display_equity = start_equity + pnl_abs

    return pnl_abs, pnl_pct, display_equity
