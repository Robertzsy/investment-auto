from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.subprocess_utils import decode_subprocess_output, hidden_subprocess_kwargs

ROOT = Path(__file__).resolve().parent.parent.parent
FETCHER_JS = ROOT / "scripts" / "stock-fetcher.js"


def _run_node(args: List[str], *, timeout: int = 50) -> Any:
    p = subprocess.run(
        ["node", str(FETCHER_JS)] + args,
        cwd=str(ROOT),
        capture_output=True,
        timeout=timeout,
        **hidden_subprocess_kwargs(),
    )
    stdout = decode_subprocess_output(p.stdout)
    stderr = decode_subprocess_output(p.stderr)
    if p.returncode != 0:
        raise RuntimeError(f"fetcher.js error: {stderr.strip()}")
    return json.loads(stdout)


def realtime(symbol: str, *, timeout: int = 30) -> Dict[str, Any]:
    return _run_node(["realtime", symbol], timeout=timeout)


def history(symbol: str, *, lookback: int = 0, timeout: int = 50) -> Dict[str, Any]:
    args = ["history", symbol]
    if lookback > 0:
        args.append(str(lookback + 1))
    return _run_node(args, timeout=timeout)


def snapshot(symbol: str, *, timeout: int = 45) -> Dict[str, Any]:
    return _run_node(["snapshot", symbol], timeout=timeout)


def search(keyword: str, *, timeout: int = 30) -> List[Dict[str, Any]]:
    return _run_node(["search", keyword], timeout=timeout)


def market_list(market: str, *, limit: int = 0, timeout: int = 50) -> Dict[str, Any]:
    """Return a market security list; ``limit=0`` requests the full market."""

    normalized = str(market or "").strip().lower()
    if normalized not in {"cn", "hk", "us", "etf"}:
        raise ValueError(f"unsupported market: {market}")
    requested = int(limit)
    bounded_limit = max(10, min(50_000, requested)) if requested > 0 else 0
    payload = _run_node(["market-list", normalized, str(bounded_limit)], timeout=timeout)
    if not isinstance(payload, dict):
        raise RuntimeError("market-list returned a non-object response")
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    if not isinstance(payload.get("data"), list):
        raise RuntimeError("market-list response is missing data")
    return payload


def get_close_prices(symbol: str, lookback: int = 0) -> List[float]:
    h = history(symbol, lookback=lookback)
    data = h.get("data", [])
    prices = [float(d["close"]) for d in data if d.get("close")]
    return prices[-lookback:] if lookback else prices
