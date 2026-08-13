"""Run one security through the real staged Agent graph without placing orders."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import cfg  # noqa: E402
from src.data.fetcher import snapshot  # noqa: E402
from src.data.research import fetch_research_packet  # noqa: E402
from src.trading.agent_workflow import run_analysis_workflow  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default="us", choices=("cn", "hk", "us", "etf"))
    parser.add_argument("--symbol", default="NVDA")
    args = parser.parse_args()

    symbol = args.symbol.strip().upper()
    market = args.market.strip().lower()
    market_snapshot = snapshot(symbol)
    if market_snapshot.get("error"):
        raise RuntimeError(str(market_snapshot["error"]))
    packet = fetch_research_packet(
        market,
        symbol,
        ttl_minutes=int(cfg.autonomous.get("agent_workflow", {}).get("research_data_ttl_minutes", 30)),
    )
    for key in ("news", "fundamentals", "sentiment"):
        if packet.get(key):
            market_snapshot[key] = packet[key]
    market_snapshot["research_data_sources"] = packet.get("sources", [])
    market_snapshot["research_data_errors"] = packet.get("errors", {})
    realtime = market_snapshot.get("realtime", {})
    context = {
        "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
        "market": market,
        "allowed_symbols": [symbol],
        # Deliberately synthetic: a smoke test never exposes the user's local
        # cash, holdings or trade history to an external model provider.
        "account": {"total_capital": 100_000, "cash": 100_000, "holdings": [], "trade_count": 0},
        "snapshots": {symbol: market_snapshot},
        "market_data_errors": {},
        "research_data_errors": packet.get("errors", {}),
        "macro_excerpt": "",
        "optimizer": {},
        "screening": {
            "status": "smoke_test",
            "selected": [{
                "symbol": symbol,
                "name": realtime.get("name", ""),
                "price": realtime.get("price"),
                "change_pct": realtime.get("change_pct"),
                "market_cap": realtime.get("market_cap"),
                "pe": realtime.get("pe"),
                "pb": realtime.get("pb"),
            }],
        },
        "market_rules": cfg.market_config(market),
        "autonomous_constraints": cfg.autonomous,
        "investment_mandate": {},
        "reflection_lessons": [],
    }
    result = run_analysis_workflow(context, cfg.autonomous)
    research = result["symbol_research"][symbol]
    print(json.dumps({
        "status": "completed",
        "workflow": result.get("workflow"),
        "symbol": symbol,
        "research_roles": sorted(research.get("base_reports", {})),
        "research_debate_rounds": len(research.get("research_debate", [])),
        "trader_stance": research.get("trader", {}).get("stance"),
        "risk_debate_rounds": len(result.get("risk_debate", [])),
        "portfolio_decisions": result.get("portfolio_manager", {}).get("decisions", []),
        "timings_seconds": result.get("timings_seconds", {}),
        "memory_writes": result.get("memory", {}).get("writes"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
