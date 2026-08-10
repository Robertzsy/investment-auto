"""Contract tests for the bundled Node stock fetcher."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "stock-fetcher.js"


def _detect(value: str) -> dict | None:
    js = (
        f"const {{detectMarket}} = require({json.dumps(str(SCRIPT))});"
        f"console.log(JSON.stringify(detectMarket({json.dumps(value)})));"
    )
    proc = subprocess.run(["node", "-e", js], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def test_bare_five_digit_code_is_hong_kong():
    assert _detect("00700") == {"market": "hk", "code": "00700"}


def test_existing_market_formats_still_work():
    assert _detect("600519") == {"market": "cn", "code": "sh600519"}
    assert _detect("hk00700") == {"market": "hk", "code": "00700"}
    assert _detect("AAPL") == {"market": "us", "code": "AAPL"}


def test_watchlist_is_stored_under_project_runtime():
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'path.join(__dirname, "..", "runtime", "data", "watchlist.json")' in source
    assert "fs.mkdirSync(path.dirname(WATCHLIST_FILE), { recursive: true })" in source
