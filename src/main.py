"""
investment-auto – Multi-market paper-trading investment automation.
Usage:
  python -m src.main run              Start the automated scheduler.
  python -m src.main once [--market]  Run a single analysis round.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger = logging.getLogger("investment-auto")

    ap = argparse.ArgumentParser(prog="investment-auto")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "once", "init", "version", "chat"])
    ap.add_argument("--market", "-m", default="cn")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    # load config early
    if args.config:
        import os; os.environ["CONFIG_PATH"] = args.config

    # defer heavy imports until needed
    if args.command == "version":
        print("investment-auto 0.1.0")
        return

    from src.config import cfg
    if args.command == "init":
        from src.portfolio import account
        account.save({"version": 2, "multiMarket": True, "accounts": {
            m: {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []}
            for m in ["cn","hk","us","etf"]}, "fxRates": {"USD_CNY":7.2,"HKD_CNY":0.92}})
        logger.info("Initialized empty portfolio.json in runtime/data/")
        return

    if args.command == "once":
        from src.scheduler import run_once
        run_once(args.market)
        return

    if args.command == "run":
        logger.info("Starting investment-auto scheduler...")
        from src.scheduler import start
        sched = start()
        try:
            import time
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Shutting down scheduler...")
            sched.shutdown(wait=False)
            sys.exit(0)

    if args.command == "chat":
        logger.info("Starting AI Chat Panel on http://localhost:8080 ...")
        from src.ui.server import start_server
        start_server(host="localhost", port=8080)
        return

if __name__ == "__main__":
    main()
