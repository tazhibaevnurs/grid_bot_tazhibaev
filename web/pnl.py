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


def compute_fifo_pnl(trades_chronological: List[Dict[str, Any]]) -> Dict[int, Optional[float]]:
    """Посчитать реализованный P&L для каждой сделки.

    :param trades_chronological: сделки в хронологическом порядке (старые сверху).
        Каждая — dict с ключами ``id``, ``symbol``, ``side`` (buy/sell),
        ``price``, ``amount``.
    :returns: словарь ``{trade_id: realized_pnl}``. ``None`` — если сделка
        только открывает (часть) позиции и ничего не закрыла.
    """
    pnl: Dict[int, Optional[float]] = {}
    # Для каждого символа: направление позиции и очередь открытых лотов [price, amount].
    books: Dict[str, Dict[str, Any]] = {}

    for trade in trades_chronological:
        symbol = trade["symbol"]
        book = books.setdefault(symbol, {"direction": 0, "lots": deque()})
        sign = 1 if trade["side"] == "buy" else -1
        amount = float(trade["amount"])
        price = float(trade["price"])

        # Нет позиции или сделка в ту же сторону -> открываем/доливаем (P&L нет).
        if book["direction"] == 0 or book["direction"] == sign:
            book["direction"] = sign
            book["lots"].append([price, amount])
            pnl[trade["id"]] = None
            continue

        # Сделка против позиции -> закрываем по FIFO.
        remaining = amount
        realized = 0.0
        closed_any = False
        lots: deque = book["lots"]
        while remaining > _EPS and lots:
            lot = lots[0]
            lot_price, lot_amount = lot[0], lot[1]
            matched = min(remaining, lot_amount)
            if book["direction"] == 1:
                # Закрываем лонг продажей: (цена_продажи - цена_покупки) * объём.
                realized += (price - lot_price) * matched
            else:
                # Закрываем шорт покупкой: (цена_продажи - цена_покупки) * объём.
                realized += (lot_price - price) * matched
            closed_any = True
            lot[1] -= matched
            remaining -= matched
            if lot[1] <= _EPS:
                lots.popleft()

        if not lots:
            book["direction"] = 0

        # Если закрыли всю позицию и остался объём — переворот в обратную сторону.
        if remaining > _EPS:
            book["direction"] = sign
            lots.append([price, remaining])

        pnl[trade["id"]] = realized if closed_any else None

    return pnl
