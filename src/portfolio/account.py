from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.runtime_lock import atomic_claim

ROOT = Path(__file__).resolve().parent.parent.parent
from src.paths import runtime_dir
RUNTIME = runtime_dir() / "data"

DEFAULTS = {
    "cn": {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []},
    "hk": {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []},
    "us": {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []},
    "etf": {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []},
}

FX_RATE_DEFAULTS = {"USD_CNY": 7.2, "HKD_CNY": 0.92}


def _default_portfolio() -> Dict[str, Any]:
    return {
        "version": 2,
        "multiMarket": True,
        "accounts": copy.deepcopy(DEFAULTS),
        "fxRates": copy.deepcopy(FX_RATE_DEFAULTS),
    }


def normalize_portfolio(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a complete portfolio without discarding valid stored state.

    Older setup and migration paths could leave an existing ``portfolio.json``
    with ``accounts: {}``.  Treating that shell as initialized later caused the
    broker to materialize every missing market with zero capital.  Merge the
    schema defaults at the storage boundary so every reader and writer sees a
    complete paper-account structure.  Explicit stored balances and holdings
    always win over defaults.
    """
    if not isinstance(data, Mapping):
        raise ValueError("portfolio.json 顶层必须是对象")

    normalized = copy.deepcopy(dict(data))
    normalized.setdefault("version", 2)
    normalized.setdefault("multiMarket", True)

    stored_accounts = normalized.get("accounts")
    if not isinstance(stored_accounts, Mapping):
        stored_accounts = {}
    accounts = copy.deepcopy(dict(stored_accounts))
    for market, defaults in DEFAULTS.items():
        stored = accounts.get(market)
        if not isinstance(stored, Mapping):
            stored = {}
        merged = copy.deepcopy(defaults)
        merged.update(copy.deepcopy(dict(stored)))
        for field in ("holdings", "tradeHistory"):
            if not isinstance(merged.get(field), list):
                merged[field] = []
        accounts[market] = merged
    normalized["accounts"] = accounts

    stored_rates = normalized.get("fxRates")
    rates = copy.deepcopy(FX_RATE_DEFAULTS)
    if isinstance(stored_rates, Mapping):
        rates.update(copy.deepcopy(dict(stored_rates)))
    normalized["fxRates"] = rates
    return normalized

def _path() -> Path:
    return RUNTIME / "portfolio.json"

def exists() -> bool:
    """True when the on-disk portfolio file has actually been created."""
    return _path().exists()

def load() -> Dict[str, Any]:
    p = _path()
    if not p.exists():
        return _default_portfolio()
    return normalize_portfolio(json.loads(p.read_text(encoding="utf-8")))

def save(data: Dict[str, Any]):
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary = p.with_suffix(p.suffix + ".tmp")
    normalized = normalize_portfolio(data)
    temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(p)

def account(market: str) -> Dict[str, Any]:
    pf = load()
    return pf["accounts"].get(market, copy.deepcopy(DEFAULTS.get(market, DEFAULTS["cn"])))


def reset_market(market: str, *, backup_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Reset exactly one paper account while preserving all other markets.

    A full pre-reset snapshot is written before the atomic portfolio replace.
    The same cross-process lock used by the paper broker prevents a reset from
    racing an order fill.
    """
    normalized = str(market or "").strip().lower()
    if normalized not in DEFAULTS:
        raise ValueError("market 必须是 cn、hk、us 或 etf")
    lock_path = RUNTIME / ".portfolio.lock"
    with atomic_claim(lock_path, stale_seconds=60) as claimed:
        if not claimed:
            raise RuntimeError("模拟账户正在被另一任务更新")
        data = load()
        accounts = data.setdefault("accounts", {})
        previous = copy.deepcopy(accounts.get(normalized, DEFAULTS[normalized]))
        destination = backup_dir or (runtime_dir() / "backups" / "portfolio")
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = destination / f"{stamp}-before-reset-{normalized}.json"
        temporary = backup_path.with_suffix(backup_path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(backup_path)
        accounts[normalized] = copy.deepcopy(DEFAULTS[normalized])
        save(data)
    return {
        "market": normalized,
        "backup": str(backup_path),
        "previous": {
            "total_capital": float(previous.get("totalCapital", 0) or 0),
            "cash": float(previous.get("cash", 0) or 0),
            "holdings": len(previous.get("holdings", []) or []),
            "trades": len(previous.get("tradeHistory", []) or []),
        },
        "account": copy.deepcopy(DEFAULTS[normalized]),
    }

def record_trade(market: str, code: str, action: str, price: float, shares: int, date: str, note: str = "") -> None:
    pf = load()
    acct = pf["accounts"].setdefault(market, copy.deepcopy(DEFAULTS.get(market, DEFAULTS["cn"])))
    acct.setdefault("tradeHistory", []).append({
        "code": code, "action": action, "price": price, "shares": shares,
        "amount": round(price * shares, 2), "date": date, "note": note,
    })
    # Legacy append-only helper. Autonomous fills use src.trading.broker so
    # cash, lots, holdings and fees are updated atomically.
    save(pf)
