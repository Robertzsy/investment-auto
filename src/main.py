"""
investment-auto – Multi-market paper-trading investment automation.
Usage:
  python -m src.main run              Start the automated scheduler.
  python -m src.main once [--market]  Run a single analysis round.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: true/false, yes/no, on/off, 1/0")

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
        os.environ["CONFIG_PATH"] = args.config

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
        host = os.getenv("CHAT_HOST", "localhost")
        try:
            port = int(os.getenv("CHAT_PORT", "8080"))
        except ValueError as exc:
            raise SystemExit("CHAT_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise SystemExit("CHAT_PORT must be between 1 and 65535")
        try:
            open_browser = _env_bool("CHAT_OPEN_BROWSER", True)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

        logger.info("Starting AI Chat Panel on %s:%s ...", host, port)
        from src.ui.server import start_server
        start_server(host=host, port=port, open_browser=open_browser)
        return

if __name__ == "__main__":
    main()
