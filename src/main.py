"""
investment-auto – Multi-market paper-trading investment automation.

Common commands:
  python -m src.main chat                         Start management chat only.
  python -m src.main optimizer -m cn             Run portfolio optimization.
  python -m src.main screen -m cn                Refresh the stock shortlist.
  python -m src.main catchup -m cn               Backfill today's missed rounds.
  python -m src.main macro                        Generate/sync the macro daily.
  python -m src.main run                          Run investment Agent + scheduler.
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


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (LookupError, OSError):
                pass


def _configure_logging() -> logging.Logger:
    log_dir = ROOT / "runtime" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.FileHandler(log_dir / "investment-auto.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        handlers.insert(0, stream)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    return logging.getLogger("investment-auto")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="investment-auto")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=[
            "run", "once", "catchup", "macro", "optimizer", "screen", "autonomous",
            "pause", "resume", "kill", "reset-kill", "status",
            "init", "version", "chat", "research",
        ],
    )
    parser.add_argument("--market", "-m", default="cn")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols for optimizer")
    parser.add_argument("--force", action="store_true", help="Force regeneration where supported")
    parser.add_argument("--dry-run", action="store_true", help="Run autonomous decision and risk checks without fills")
    parser.add_argument("--reason", default="", help="Reason recorded for pause/resume/kill controls")
    parser.add_argument("--config", default=None)
    parser.add_argument("--task", default="backtest", choices=["backtest", "strategy_experiment", "bugfix"],
                        help="Research task type (research command)")
    parser.add_argument("--objective", default="", help="Immutable research objective")
    parser.add_argument("--max-rounds", type=int, default=None, help="Fresh-agent round cap (config default when empty)")
    return parser


def main() -> None:
    _configure_stdio()
    args = _parser().parse_args()
    if args.config:
        os.environ["CONFIG_PATH"] = args.config
    logger = _configure_logging()

    if args.command == "version":
        from src.version import __version__

        print(f"investment-auto {__version__}")
        return

    if args.command in {"run", "once", "catchup", "macro", "optimizer", "screen", "autonomous", "chat", "research"} and not shutil.which("node"):
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

    if args.command in {"pause", "resume", "kill", "reset-kill", "status"}:
        from src.portfolio import account as portfolio_account
        from src.trading import control
        from src.trading.controller import autonomous_enabled

        if args.command == "pause":
            result = control.set_paused(True, reason=args.reason or "人工暂停")
        elif args.command == "resume":
            result = control.set_paused(False, reason=args.reason or "人工恢复")
        elif args.command == "kill":
            result = control.activate_kill_switch(reason=args.reason or "人工紧急停止")
        elif args.command == "reset-kill":
            result = control.reset_kill_switch(reason=args.reason or "人工解除紧急停止")
        else:
            result = {
                "enabled": autonomous_enabled(),
                "config_enabled": bool(cfg.autonomous.get("enabled", False)),
                "control": control.load_state(),
                "markets": {
                    market: {
                        "cash": market_account.get("cash", 0),
                        "holdings": len(market_account.get("holdings", [])),
                        "trades": len(market_account.get("tradeHistory", [])),
                    }
                    for market in cfg.enabled_markets
                    for market_account in [portfolio_account.account(market)]
                },
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "screen":
        from src.trading.controller import run_screening_preview

        result = run_screening_preview(args.market)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.command == "autonomous":
        from src.trading.controller import run_autonomous_cycle

        result = run_autonomous_cycle(args.market, label="manual", dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
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
        logger.info("Starting standalone investment Agent + scheduler...")
        from src.scheduler import start
        from src.investment.command_bus import InvestmentCommandWorker

        command_worker = InvestmentCommandWorker().start()
        scheduler = start(catch_up=True)
        try:
            import time

            restart_request = ROOT / "runtime" / "investment" / "restart_requested.json"
            while True:
                time.sleep(1)
                if restart_request.exists():
                    logger.info("Verified investment Agent change detected; restarting worker process")
                    restart_request.unlink()
                    scheduler.shutdown(wait=False)
                    command_worker.stop()
                    os.execv(sys.executable, [sys.executable, "-m", "src.main", "run"])
        except KeyboardInterrupt:
            logger.info("Shutting down scheduler...")
            scheduler.shutdown(wait=False)
            command_worker.stop()
            return

    if args.command == "research":
        from dataclasses import asdict
        from pathlib import Path

        from src.research.loop import run_research_loop
        from src.research.tasks import task_tools

        research_settings = cfg.raw.get("architecture", {}).get("research", {}) or {}
        if not bool(research_settings.get("enabled", True)):
            raise SystemExit("离线研究循环未启用（architecture.research.enabled=false）")
        max_rounds = args.max_rounds or int(research_settings.get("max_rounds", 6))
        shell_mode = str(research_settings.get("shell", "restricted")).strip().lower()
        include_shell_tools = shell_mode != "none"
        shell_timeout_seconds = int(research_settings.get("shell_timeout_seconds", 60))
        workspace_value = str(research_settings.get("workspace", "")).strip()
        workspace_root = Path(workspace_value) if workspace_value else None
        if workspace_root is not None and not workspace_root.is_absolute():
            workspace_root = ROOT / workspace_root
        outcome = run_research_loop(
            args.objective,
            args.task,
            market=args.market,
            max_rounds=max_rounds,
            workspace_root=workspace_root,
            include_shell_tools=include_shell_tools,
            shell_timeout_seconds=shell_timeout_seconds,
            extra_tools=task_tools(args.task),
            on_progress=lambda message: logger.info(message),
        )
        print(json.dumps(asdict(outcome), ensure_ascii=False, indent=2))
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
            scheduler_default = False
            start_scheduler = _env_bool("CHAT_START_SCHEDULER", scheduler_default)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

        if start_scheduler:
            logger.warning("CHAT_START_SCHEDULER is ignored: chat is now management-only")
        logger.info("Scheduler and investment worker are isolated from the chat process")

        logger.info("Starting AI Chat Panel on %s:%s ...", host, port)
        from src.ui.server import start_server

        start_server(host=host, port=port, open_browser=open_browser)
        return


if __name__ == "__main__":
    main()
