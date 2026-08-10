from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME = ROOT / "runtime" / "data"

DEFAULTS = {
    "cn": {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []},
    "hk": {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []},
    "us": {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []},
    "etf": {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []},
}

def _path() -> Path:
    return RUNTIME / "portfolio.json"

def load() -> Dict[str, Any]:
    p = _path()
    if not p.exists():
        return {"version": 2, "multiMarket": True, "accounts": dict(DEFAULTS), "fxRates": {"USD_CNY": 7.2, "HKD_CNY": 0.92}}
    return json.loads(p.read_text(encoding="utf-8"))

def save(data: Dict[str, Any]):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def account(market: str) -> Dict[str, Any]:
    pf = load()
    return pf["accounts"].get(market, dict(DEFAULTS.get(market, DEFAULTS["cn"])))

def record_trade(market: str, code: str, action: str, price: float, shares: int, date: str, note: str = "") -> None:
    pf = load()
    acct = pf["accounts"].setdefault(market, dict(DEFAULTS.get(market, DEFAULTS["cn"])))
    acct.setdefault("tradeHistory", []).append({
        "code": code, "action": action, "price": price, "shares": shares,
        "amount": round(price * shares, 2), "date": date, "note": note,
    })
    # simple cash/holds update stub
    save(pf)
