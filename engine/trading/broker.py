from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from engine.portfolio import account as account_store
from engine.runtime_lock import atomic_claim

ROOT = Path(__file__).resolve().parents[2]
from engine.paths import runtime_dir
PORTFOLIO_LOCK = runtime_dir() / "data" / ".portfolio.lock"
_thread_lock = threading.RLock()


def _quantity(holding: Mapping[str, Any]) -> int:
    return int(float(holding.get("shares", holding.get("quantity", 0)) or 0))


def _find_holding(holdings: List[Dict[str, Any]], symbol: str) -> Optional[Dict[str, Any]]:
    normalized = symbol.upper()
    return next((item for item in holdings if str(item.get("code", "")).upper() == normalized), None)


def _available_to_sell(holding: Mapping[str, Any], date: str, settlement: str) -> int:
    total = _quantity(holding)
    if str(settlement).upper() != "T+1":
        return total
    lots = holding.get("lots")
    if not isinstance(lots, list):
        # Legacy positions predate lot tracking and are considered settled.
        return total
    return sum(int(lot.get("shares", 0) or 0) for lot in lots if str(lot.get("acquired_date", "")) < date)


def _consume_lots(holding: Dict[str, Any], shares: int, date: str, settlement: str) -> None:
    lots = holding.get("lots")
    if not isinstance(lots, list):
        return
    remaining = shares
    ordered = sorted(lots, key=lambda item: str(item.get("acquired_date", "")))
    for lot in ordered:
        if remaining <= 0:
            break
        if str(settlement).upper() == "T+1" and str(lot.get("acquired_date", "")) >= date:
            continue
        available = int(lot.get("shares", 0) or 0)
        consumed = min(available, remaining)
        lot["shares"] = available - consumed
        remaining -= consumed
    holding["lots"] = [lot for lot in ordered if int(lot.get("shares", 0) or 0) > 0]


def _fees(side: str, gross: float, trading: Mapping[str, Any]) -> Dict[str, float]:
    commission = gross * float(trading.get("commission_rate", 0) or 0)
    stamp_side = str(trading.get("stamp_tax_side", "none")).lower()
    stamp = gross * float(trading.get("stamp_tax", 0) or 0) if stamp_side in {side.lower(), "both"} else 0.0
    return {"commission": commission, "stamp_tax": stamp, "total": commission + stamp}


def execute_orders(
    market: str,
    orders: Sequence[Mapping[str, Any]],
    *,
    market_config: Mapping[str, Any],
    trading_mode: str,
    now: datetime,
    portfolio_path: Optional[Path] = None,
    equity_snapshot: Optional[float] = None,
    mark_prices: Optional[Mapping[str, float]] = None,
    security_names: Optional[Mapping[str, str]] = None,
    idempotency_key: str = "",
    decision_fingerprint: str = "",
) -> Dict[str, Any]:
    """Fill approved orders against the supplied reference price.

    This engine deliberately refuses every mode except paper. It performs an
    atomic portfolio replacement and never delegates balance updates to the AI.

    Idempotency: when ``idempotency_key`` is supplied, the execution receipt
    (key + decision fingerprint + fills) is written INTO the same portfolio
    document as the account mutation, under the same cross-process lock and
    the same atomic replace — so a crash between "fills happened" and any
    later bookkeeping can never be re-executed: a retry finds the receipt in
    the account file itself. A same-key/different-content call is refused.
    """

    if str(trading_mode).lower() != "paper":
        raise RuntimeError("自主执行当前只允许 trading.mode=paper")
    receipt_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(idempotency_key or "")).strip("-")[:120]
    trading = market_config.get("trading", {})
    slippage = float(trading.get("slippage", 0) or 0)
    decimals = int(trading.get("price_decimals", 2) or 2)
    settlement = str(trading.get("settlement", "T+0"))
    date = now.date().isoformat()
    timestamp = now.isoformat(timespec="seconds")

    with _thread_lock:
        with atomic_claim(PORTFOLIO_LOCK, stale_seconds=60) as claimed:
            if not claimed:
                raise RuntimeError("模拟账户正在被另一任务更新")
            data = account_store.load() if portfolio_path is None else json.loads(portfolio_path.read_text(encoding="utf-8"))
            receipts = data.setdefault("execution_receipts", {})
            if receipt_key and receipt_key in receipts:
                existing = receipts[receipt_key]
                if str(existing.get("decision_fingerprint", "")) != str(decision_fingerprint or ""):
                    raise ValueError("同一幂等键对应不同的决策内容，拒绝重复执行（请更换幂等键）")
                return {
                    "fills": list(existing.get("fills", [])),
                    "rejected": list(existing.get("rejected", [])),
                    "cash_after": existing.get("cash_after"),
                    "replayed": True,
                }
            accounts = data.setdefault("accounts", {})
            account = accounts.setdefault(
                market,
                copy.deepcopy(account_store.DEFAULTS.get(market, account_store.DEFAULTS["cn"])),
            )
            holdings: List[Dict[str, Any]] = account.setdefault("holdings", [])
            history: List[Dict[str, Any]] = account.setdefault("tradeHistory", [])
            fills: List[Dict[str, Any]] = []
            rejected: List[Dict[str, Any]] = []

            for holding in holdings:
                symbol = str(holding.get("code", "")).upper()
                resolved_name = str((security_names or {}).get(symbol, "")).strip()
                if resolved_name and str(holding.get("name", "")).strip().upper() in {"", symbol}:
                    holding["name"] = resolved_name
                mark = float((mark_prices or {}).get(symbol, 0) or 0)
                if mark > 0:
                    holding["lastPrice"] = mark
                    holding["highPrice"] = max(float(holding.get("highPrice", 0) or 0), mark)

            if equity_snapshot is not None and equity_snapshot > 0:
                account["highWaterMark"] = round(max(
                    float(account.get("highWaterMark", 0) or 0),
                    float(equity_snapshot),
                ), 4)
                snapshots = account.setdefault("equitySnapshots", [])
                snapshots.append({"time": timestamp, "equity": round(float(equity_snapshot), 4)})
                account["equitySnapshots"] = snapshots[-365:]

            for raw_order in orders:
                side = str(raw_order.get("side", "")).upper()
                symbol = str(raw_order.get("symbol", "")).upper()
                shares = int(raw_order.get("shares", 0) or 0)
                reference = float(raw_order.get("reference_price", 0) or 0)
                if side not in {"BUY", "SELL"} or not symbol or shares <= 0 or reference <= 0:
                    rejected.append({"order": dict(raw_order), "reason": "订单字段无效"})
                    continue

                fill_price = round(reference * (1 + slippage if side == "BUY" else 1 - slippage), decimals)
                gross = round(fill_price * shares, 2)
                fee = _fees(side, gross, trading)
                holding = _find_holding(holdings, symbol)

                if side == "BUY":
                    total_cost = gross + fee["total"]
                    if total_cost > float(account.get("cash", 0) or 0) + 1e-9:
                        rejected.append({"order": dict(raw_order), "reason": "现金不足（含费用和滑点）"})
                        continue
                    old_shares = _quantity(holding or {})
                    old_cost = float((holding or {}).get("costPrice", (holding or {}).get("cost", 0)) or 0)
                    new_shares = old_shares + shares
                    average_cost = (old_cost * old_shares + total_cost) / new_shares
                    if holding is None:
                        name = str((security_names or {}).get(symbol, raw_order.get("name", symbol))).strip() or symbol
                        holding = {"code": symbol, "name": name, "market": market, "lots": []}
                        holdings.append(holding)
                    holding.update({
                        "shares": new_shares,
                        "quantity": new_shares,
                        "costPrice": round(average_cost, decimals),
                        "cost": round(average_cost, decimals),
                        "lastPrice": fill_price,
                    })
                    lots = holding.setdefault("lots", [])
                    if old_shares > 0 and not lots:
                        lots.append({"shares": old_shares, "acquired_date": "1970-01-01", "price": old_cost})
                    lots.append({"shares": shares, "acquired_date": date, "price": fill_price})
                    account["cash"] = round(float(account.get("cash", 0) or 0) - total_cost, 2)
                else:
                    if holding is None:
                        rejected.append({"order": dict(raw_order), "reason": "没有可卖持仓"})
                        continue
                    available = _available_to_sell(holding, date, settlement)
                    if shares > available:
                        rejected.append({"order": dict(raw_order), "reason": f"可卖数量不足（可卖 {available}）"})
                        continue
                    proceeds = gross - fee["total"]
                    remaining = _quantity(holding) - shares
                    _consume_lots(holding, shares, date, settlement)
                    holding.update({"shares": remaining, "quantity": remaining, "lastPrice": fill_price})
                    protection_stage = str(raw_order.get("protection_stage", ""))
                    if protection_stage == "take_profit_1":
                        holding["takeProfit1Done"] = True
                    elif protection_stage == "take_profit_2":
                        holding["takeProfit2Done"] = True
                    account["cash"] = round(float(account.get("cash", 0) or 0) + proceeds, 2)
                    if remaining <= 0:
                        holdings.remove(holding)

                fill = {
                    "id": uuid.uuid4().hex,
                    "decision_id": raw_order.get("decision_id"),
                    "code": symbol,
                    "name": str(holding.get("name", (security_names or {}).get(symbol, symbol))),
                    "action": side,
                    "price": fill_price,
                    "shares": shares,
                    "quantity": shares,
                    "amount": gross,
                    "fees": {key: round(value, 4) for key, value in fee.items()},
                    "date": timestamp,
                    "status": "filled",
                    "note": str(raw_order.get("reason", ""))[:500],
                }
                history.append(fill)
                fills.append(fill)

            if receipt_key:
                # Execution receipt rides the same atomic document replace as
                # the account mutation: this write IS the durability point of
                # the fills (the audit file is only an external record).
                if len(receipts) >= 200:
                    for stale_key in sorted(receipts, key=lambda key: str(receipts[key].get("completed_at", "")), reverse=True)[199:]:
                        receipts.pop(stale_key, None)
                receipts[receipt_key] = {
                    "idempotency_key": receipt_key,
                    "decision_fingerprint": str(decision_fingerprint or ""),
                    "market": market,
                    "completed_at": timestamp,
                    "fills": fills,
                    "rejected": rejected,
                    "cash_after": account.get("cash", 0),
                }
            if portfolio_path is None:
                account_store.save(data)
            else:
                portfolio_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = portfolio_path.with_suffix(portfolio_path.suffix + ".tmp")
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(portfolio_path)
            return {"fills": fills, "rejected": rejected, "cash_after": account.get("cash", 0)}
