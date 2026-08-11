"""
investment-auto – Multi-market paper-trading investment automation.

Common commands:
  python -m src.main chat                         Start chat + scheduler.
  python -m src.main optimizer -m cn             Run portfolio optimization.
  python -m src.main catchup -m cn               Backfill today's missed rounds.
  python -m src.main macro                        Generate/sync the macro daily.
  python -m src.main run                          Run the scheduler only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
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


def _configure_logging() -> logging.Logger:
    log_dir = ROOT / "runtime" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(log_dir / "investment-auto.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[stream, file_handler], force=True)
    return logging.getLogger("investment-auto")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="investment-auto")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "once", "catchup", "macro", "optimizer", "init", "version", "chat"],
    )
    parser.add_argument("--market", "-m", default="cn")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols for optimizer")
    parser.add_argument("--force", action="store_true", help="Force regeneration where supported")
    parser.add_argument("--config", default=None)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.config:
        os.environ["CONFIG_PATH"] = args.config
    logger = _configure_logging()

    if args.command == "version":
        print("investment-auto 0.2.0")
        return

    if args.command in {"run", "once", "catchup", "macro", "optimizer", "chat"} and not shutil.which("node"):
        raise SystemExit("未找到 Node.js。行情、优化器和宏观日报需要 Node.js 18+，请安装后重试。")

    from src.config import cfg

    if args.command == "init":
        from src.portfolio import account

        account.save({"version": 2, "multiMarket": True, "accounts": {
            market: {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []}
            for market in ["cn", "hk", "us", "etf"]
        }, "fxRates": {"USD_CNY": 7.2, "HKD_CNY": 0.92}})
        logger.info("Initialized empty portfolio.json in runtime/data/")
        return

    if args.command == "optimizer":
        from src.optimizer.runner import compact_result, parse_symbols, run_optimizer

        result = run_optimizer(market=args.market, symbols=parse_symbols(args.symbols))
        print(json.dumps(compact_result(result), ensure_ascii=False, indent=2))
        return

    if args.command == "macro":
        from src.macro import run_daily

        print(json.dumps(run_daily(force=args.force), ensure_ascii=False, indent=2))
        return

    if args.command == "catchup":
        from src.scheduler import run_catch_up

        print(json.dumps(run_catch_up(markets=[args.market]), ensure_ascii=False, indent=2))
        return

    if args.command == "once":
        from src.scheduler import run_once

        print(run_once(args.market))
        return

    if args.command == "run":
        logger.info("Starting investment-auto scheduler...")
        from src.scheduler import start

        scheduler = start(catch_up=True)
        try:
            import time

            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Shutting down scheduler...")
            scheduler.shutdown(wait=False)
            return

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
            scheduler_default = bool(cfg.schedule.get("chat_start_scheduler", True))
            start_scheduler = _env_bool("CHAT_START_SCHEDULER", scheduler_default)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

        scheduler = None
        if start_scheduler:
            from src.scheduler import start

            try:
                scheduler = start(catch_up=True)
                logger.info("Automatic scheduler enabled inside chat process")
            except RuntimeError as exc:
                if "调度器已经在运行" not in str(exc):
                    raise
                logger.warning("Using existing scheduler process: %s", exc)
        else:
            logger.info("Automatic scheduler disabled inside chat process")

        logger.info("Starting AI Chat Panel on %s:%s ...", host, port)
        from src.ui.server import start_server

        try:
            start_server(host=host, port=port, open_browser=open_browser)
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)
        return


if __name__ == "__main__":
    main()
