from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.subprocess_utils import decode_subprocess_output

ROOT = Path(__file__).resolve().parent.parent.parent
FETCHER_JS = ROOT / "scripts" / "stock-fetcher.js"


def _run_node(args: List[str]) -> Any:
    p = subprocess.run(
        ["node", str(FETCHER_JS)] + args,
        cwd=str(ROOT),
        capture_output=True,
        timeout=50,
    )
    stdout = decode_subprocess_output(p.stdout)
    stderr = decode_subprocess_output(p.stderr)
    if p.returncode != 0:
        raise RuntimeError(f"fetcher.js error: {stderr.strip()}")
    return json.loads(stdout)


def realtime(symbol: str) -> Dict[str, Any]:
    return _run_node(["realtime", symbol])


def history(symbol: str) -> Dict[str, Any]:
    return _run_node(["history", symbol])


def snapshot(symbol: str) -> Dict[str, Any]:
    return _run_node(["snapshot", symbol])


def search(keyword: str) -> List[Dict[str, Any]]:
    return _run_node(["search", keyword])


def get_close_prices(symbol: str, lookback: int = 0) -> List[float]:
    h = history(symbol)
    data = h.get("data", [])
    prices = [float(d["close"]) for d in data if d.get("close")]
    return prices[-lookback:] if lookback else prices
