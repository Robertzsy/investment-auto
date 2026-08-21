"""
investment-auto 2.0 — Multi-market paper-trading investment engine.

This process is the non-bypassable execution plane: market data, screening,
portfolio optimization, hard risk controls, paper broker, scheduler, and the
HTTP command API consumed by the DSH app (the conversation/decision plane).

Common commands:
  python -m engine.main run                          Run engine + scheduler.
  python -m engine.main serve                        Run the HTTP command API.
  python -m engine.main init                         Initialize the paper account.
  python -m engine.main screen -m cn                 Refresh the stock shortlist.
  python -m engine.main optimizer -m cn              Run portfolio optimization.
  python -m engine.main macro                        Generate/sync the macro daily.
  python -m engine.main catchup -m cn                Backfill today's missed rounds.
  python -m engine.main status / pause / resume / kill / reset-kill
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
from engine.paths import runtime_dir


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


def _ensure_data_layout(logger: logging.Logger) -> None:
    """Copy missing config templates from the app root on first launch.

    The installer never touches user data; the first run seeds the data
    directory (config.yaml, market rules) from the installed templates.
    In development data root == app root, so this is a no-op.
    """
    try:
        import shutil

        from engine.paths import APP_ROOT, config_dir, market_config_dir

        target = config_dir() / "config.yaml"
        if not target.exists():
            source = APP_ROOT / "config" / "config.yaml"
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                logger.info("Seeded data config.yaml from template")
        market_source = APP_ROOT / "config" / "market"
        market_target = market_config_dir()
        if market_source.exists():
            market_target.mkdir(parents=True, exist_ok=True)
            for template in market_source.glob("*.yaml"):
                if not (market_target / template.name).exists():
                    shutil.copy2(template, market_target / template.name)
    except Exception:
        logger.debug("Config template seeding skipped", exc_info=True)


def _configure_logging() -> logging.Logger:
    log_dir = runtime_dir() / "logs"
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
            "run", "serve", "once", "catchup", "macro", "optimizer", "screen",
            "pause", "resume", "kill", "reset-kill", "status",
            "init", "version", "migrate",
        ],
    )
    parser.add_argument("--market", "-m", default="cn")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols for optimizer/screen")
    parser.add_argument("--force", action="store_true", help="Force regeneration where supported")
    parser.add_argument("--reason", default="", help="Reason recorded for pause/resume/kill controls")
    parser.add_argument("--config", default=None)
    parser.add_argument("--from", dest="from_path", default=None, help="Legacy project root to migrate from")
    parser.add_argument("--items", default="", help="Comma-separated migration items; empty = all")
    return parser


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    _configure_stdio()
    args = _parser().parse_args()
    if args.config:
        os.environ["CONFIG_PATH"] = args.config
    logger = _configure_logging()

    if args.command == "version":
        from engine.version import __version__

        print(f"investment-auto {__version__}")
        return

    if args.command in {"run", "serve", "once", "catchup", "macro", "optimizer", "screen"} and not shutil.which("node"):
        raise SystemExit("未找到 Node.js。行情、优化器和宏观日报需要 Node.js 18+，请安装后重试。")

    _ensure_data_layout(logger)
    from engine.config import cfg

    if args.command == "init":
        from engine.portfolio import account

        account.save({"version": 2, "multiMarket": True, "accounts": {
            market: {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []}
            for market in ["cn", "hk", "us", "etf"]
        }, "fxRates": {"USD_CNY": 7.2, "HKD_CNY": 0.92}})
        logger.info("Initialized empty portfolio.json in runtime/data/")
        return

    if args.command in {"pause", "resume", "kill", "reset-kill", "status"}:
        from engine.portfolio import account as portfolio_account
        from engine.trading import control

        if args.command == "pause":
            result = control.set_paused(True, reason=args.reason or "人工暂停")
        elif args.command == "resume":
            result = control.set_paused(False, reason=args.reason or "人工恢复")
        elif args.command == "kill":
            result = control.activate_kill_switch(reason=args.reason or "人工紧急停止")
        elif args.command == "reset-kill":
            result = control.reset_kill_switch(reason=args.reason or "人工解除紧急停止")
        else:
            enabled = bool(cfg.autonomous.get("enabled", False))
            result = {
                "enabled": enabled,
                "config_enabled": enabled,
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
        _print(result)
        return

    if args.command == "screen":
        from engine.screening.preview import run_screening_preview

        _print(run_screening_preview(args.market, symbols=parse_symbols_optional(args.symbols)))
        return

    if args.command == "optimizer":
        from engine.optimizer.runner import compact_result, parse_symbols, run_optimizer

        result = run_optimizer(market=args.market, symbols=parse_symbols(args.symbols))
        _print(compact_result(result))
        return

    if args.command == "macro":
        from engine.macro import run_daily

        _print(run_daily(force=args.force))
        return

    if args.command == "catchup":
        from engine.scheduler import run_catch_up

        _print(run_catch_up(markets=[args.market]))
        return

    if args.command == "once":
        from engine.scheduler import run_once

        _print(run_once(args.market))
        return

    if args.command == "serve":
        from engine.api.server import serve

        serve(host=os.getenv("INVESTMENT_API_HOST", "127.0.0.1"), port=int(os.getenv("INVESTMENT_API_PORT", "8790")))
        return

    if args.command == "run":
        logger.info("Starting investment engine + scheduler...")
        from engine.dsh_bridge import install_dsh_runner
        from engine.scheduler import start
        from engine.investment.command_bus import InvestmentCommandWorker

        install_dsh_runner()
        command_worker = InvestmentCommandWorker().start()
        scheduler = start(catch_up=True)
        try:
            import time

            restart_request = runtime_dir() / "investment" / "restart_requested.json"
            while True:
                time.sleep(1)
                if restart_request.exists():
                    logger.info("Verified engine change detected; restarting worker process")
                    restart_request.unlink()
                    scheduler.shutdown(wait=False)
                    command_worker.stop()
                    os.execv(sys.executable, [sys.executable, "-m", "engine.main", "run"])
        except KeyboardInterrupt:
            logger.info("Shutting down scheduler...")
            scheduler.shutdown(wait=False)
            command_worker.stop()
            return

    if args.command == "migrate":
        from engine.migration import detect_sources, plan_migration, run_migration

        source = args.from_path or os.getenv("INVESTMENT_AUTO_HOME", "")
        if not source:
            sources = detect_sources()
            if not sources:
                _print({"status": "no_source", "reason": "未检测到旧版 investment-auto（D:\\investment-auto）或 INVESTMENT_AUTO_HOME"})
                return
            source = sources[0]["path"]
        items = [item.strip() for item in args.items.split(",") if item.strip()] or None
        plan = plan_migration(source)
        result = run_migration(source, items)
        _print({**plan, **result})
        return


def parse_symbols_optional(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


if __name__ == "__main__":
    main()
