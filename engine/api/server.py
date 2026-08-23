"""Loopback HTTP command API for the investment engine.

Read surface: health, status, portfolio, market data, screening, optimizer,
reports, macro, mandate. Command surface: POST /api/commands/issue (the same
command facade as the CLI). The DSH tools bridge (app/plugins) consumes this
API; it owns no AI.
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("investment-auto.api")


def _api_token() -> str:
    return os.getenv("IA_ACCESS_TOKEN", "").strip()


def _read_json(handler: BaseHTTPRequestHandler, limit: int = 1_000_000) -> Optional[Dict[str, Any]]:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > limit:
        return None
    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _query(handler: BaseHTTPRequestHandler) -> Dict[str, str]:
    parsed = urlsplit(handler.path)
    values = parse_qs(parsed.query)
    return {key: value[-1] for key, value in values.items() if value}


def _latest_optimizer_file(market: str) -> Optional[Dict[str, Any]]:
    from engine.paths import runtime_dir

    directory = runtime_dir() / "optimizer"
    if not directory.exists():
        return None
    files = sorted(directory.glob(f"*-{market}.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        return None
    try:
        payload = json.loads(files[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"file": files[0].name, "error": "unreadable"}
    if isinstance(payload, dict):
        payload = dict(payload)
    return {"file": files[0].name, **payload} if isinstance(payload, dict) else {"file": files[0].name, "result": payload}


class _Handler(BaseHTTPRequestHandler):
    server_version = "InvestmentAutoEngine/2.1"

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _complete_setup(self, payload: Dict[str, Any]) -> None:
        """First-run setup: initialize the paper account, optionally import
        legacy data, then write the setup marker that releases the desktop
        shell to start the autonomous engine."""
        from engine.paths import runtime_dir
        from engine.portfolio import account

        if not account.exists():
            account.save({"version": 2, "multiMarket": True, "accounts": {
                market: {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []}
                for market in ["cn", "hk", "us", "etf"]
            }, "fxRates": {"USD_CNY": 7.2, "HKD_CNY": 0.92}})
            logger.info("setup: initialized paper account")
        source = str(payload.get("import_from", "")).strip()
        if source:
            from engine.migration import plan_migration, run_migration

            items = [item.strip() for item in str(payload.get("items", "")).split(",") if item.strip()] or None
            logger.info("setup: importing legacy data from %s (items=%s)", source, items or "all")
            plan_migration(source)
            run_migration(source, items)
        marker = runtime_dir() / "setup.complete"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("completed", encoding="utf-8")

    def _authorized(self) -> bool:
        token = _api_token()
        if not token:
            return True  # plain loopback dev mode
        supplied = self.headers.get("X-IA-Token", "")
        return supplied == token

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(403, {"ok": False, "error": "invalid access token"})
            return
        path = urlsplit(self.path).path
        try:
            self._route_get(path)
        except Exception as exc:  # noqa: BLE001 - the caller needs the message
            logger.exception("GET %s failed", path)
            self._send(500, {"ok": False, "error": str(exc)[:1000]})

    def _route_get(self, path: str) -> None:
        if path == "/api/health":
            from engine.version import __version__

            self._send(200, {"ok": True, "service": "investment-auto-engine", "version": __version__, "pid": os.getpid()})
            return
        if path == "/api/status":
            from engine.investment.status import runtime_status

            self._send(200, {"ok": True, **runtime_status()})
            return
        if path.startswith("/api/portfolio/"):
            market = path.rsplit("/", 1)[-1].strip().lower()
            if market not in {"cn", "hk", "us", "etf"}:
                self._send(400, {"ok": False, "error": "market 必须是 cn、hk、us 或 etf"})
                return
            from engine.portfolio import account

            self._send(200, {"ok": True, "market": market, **account.account(market)})
            return
        if path == "/api/market/snapshot":
            symbol = _query(self).get("symbol", "").strip()
            if not symbol:
                self._send(400, {"ok": False, "error": "missing symbol"})
                return
            from engine.data import fetcher

            self._send(200, {"ok": True, "symbol": symbol, **fetcher.snapshot(symbol)})
            return
        if path == "/api/market/history":
            values = _query(self)
            symbol = values.get("symbol", "").strip()
            if not symbol:
                self._send(400, {"ok": False, "error": "missing symbol"})
                return
            try:
                lookback = max(0, min(1023, int(values.get("lookback", "60"))))
            except ValueError:
                lookback = 60
            from engine.data import fetcher

            self._send(200, {"ok": True, "symbol": symbol, "lookback": lookback, **fetcher.history(symbol, lookback=lookback)})
            return
        if path == "/api/market/search":
            keyword = _query(self).get("q", "").strip()
            if not keyword:
                self._send(400, {"ok": False, "error": "missing q"})
                return
            from engine.data import fetcher

            self._send(200, {"ok": True, "keyword": keyword, "results": fetcher.search(keyword)})
            return
        if path.startswith("/api/screening/"):
            market = path.rsplit("/", 1)[-1].strip().lower()
            if market not in {"cn", "hk", "us", "etf"}:
                self._send(400, {"ok": False, "error": "market 必须是 cn、hk、us 或 etf"})
                return
            from datetime import datetime
            from zoneinfo import ZoneInfo

            from engine.config import cfg
            from engine.screening import latest_screening

            cached = latest_screening(market, now=datetime.now(ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai"))))
            if cached is None:
                self._send(200, {"ok": True, "market": market, "cached": False, "screening": None})
                return
            self._send(200, {"ok": True, "market": market, "cached": True, "screening": cached})
            return
        if path.startswith("/api/optimizer/"):
            market = path.rsplit("/", 1)[-1].strip().lower()
            if market not in {"cn", "hk", "us", "etf"}:
                self._send(400, {"ok": False, "error": "market 必须是 cn、hk、us 或 etf"})
                return
            self._send(200, {"ok": True, "market": market, "optimizer": _latest_optimizer_file(market)})
            return
        if path == "/api/reports":
            values = _query(self)
            market = values.get("market", "").strip().lower()
            try:
                limit = max(1, min(50, int(values.get("limit", "10"))))
            except ValueError:
                limit = 10
            from engine.paths import runtime_dir

            directory = runtime_dir() / "reports"
            pattern = f"*-{market}-*.md" if market else "*.md"
            files = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True) if directory.exists() else []
            rows = [{
                "file": path.name,
                "mtime": path.stat().st_mtime,
                "size": path.stat().st_size,
            } for path in files[:limit]]
            self._send(200, {"ok": True, "market": market or None, "reports": rows})
            return
        if path == "/api/reports/latest":
            market = _query(self).get("market", "").strip().lower()
            from engine.investment.status import _latest_report

            self._send(200, {"ok": True, "market": market or None, "report": _latest_report(market) if market else None})
            return
        if path == "/api/macro/latest":
            from engine.macro import DATA_ROOT, latest_dates, report_path

            dates = latest_dates(DATA_ROOT)
            if not dates:
                self._send(200, {"ok": True, "macro": None})
                return
            macro_path = report_path(dates[0], DATA_ROOT)
            content = macro_path.read_text(encoding="utf-8")[:20000] if macro_path.exists() else ""
            self._send(200, {"ok": True, "date": dates[0], "file": str(macro_path), "content": content})
            return
        if path == "/api/mandate":
            from engine.investment.mandate import get_mandate

            self._send(200, {"ok": True, "mandate": get_mandate()})
            return
        if path == "/api/analysis/latest":
            from engine.analysis_runs import latest

            market = _query(self).get("market", "").strip().lower()
            self._send(200, {"ok": True, "analysis": latest(market=market)})
            return
        if path == "/api/analysis/runs":
            from engine.analysis_runs import list_runs

            values = _query(self)
            market = values.get("market", "").strip().lower()
            try:
                limit = int(values.get("limit", "20"))
            except ValueError:
                limit = 20
            self._send(200, {"ok": True, "runs": list_runs(market=market, limit=limit)})
            return
        if path == "/api/analysis/run":
            from engine.analysis_runs import get

            cycle_id = _query(self).get("cycle_id", "").strip()
            if not cycle_id:
                self._send(400, {"ok": False, "error": "missing cycle_id"})
                return
            run = get(cycle_id)
            self._send(200 if run is not None else 404, {"ok": run is not None, "analysis": run})
            return
        if path == "/api/analysis/rounds/active":
            from engine import analysis_rounds

            self._send(200, {"ok": True, "rounds": analysis_rounds.active_rounds()})
            return
        if path == "/api/credentials/resolve":
            ref = _query(self).get("ref", "").strip()
            if not ref:
                self._send(400, {"ok": False, "error": "missing ref"})
                return
            from engine.secret_store import load_secret

            value = load_secret(ref)
            self._send(200, {"ok": True, "ref": ref, "configured": value is not None, "value": value or ""})
            return
        if path == "/setup":
            from engine.api.setup_page import SETUP_PAGE_HTML
            from engine.migration import detect_sources

            suggested = ""
            try:
                sources = detect_sources()
                if sources:
                    suggested = str(sources[0]["path"])
            except Exception:
                pass
            page = SETUP_PAGE_HTML.replace(
                '<div class="hint">检测到旧版项目时会自动填入；数据只复制、不删除。</div>',
                '<div class="hint">' + (
                    f"检测到旧版项目：<code>{suggested}</code>（数据只复制、不删除）"
                    if suggested else "未检测到旧版项目；数据只复制、不删除。"
                ) + '</div>',
            )
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/config":
            from engine.config import cfg
            from engine.secret_store import redact_mapping

            self._send(200, {"ok": True, "config": redact_mapping(dict(cfg.raw))})
            return
        if path == "/api/setup/status":
            from engine.paths import runtime_dir

            self._send(200, {"ok": True, "first_run": not (runtime_dir() / "setup.complete").exists()})
            return
        self._send(404, {"ok": False, "error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(403, {"ok": False, "error": "invalid access token"})
            return
        path = urlsplit(self.path).path
        if path == "/api/analysis/rounds/start":
            payload = _read_json(self)
            if payload is None:
                self._send(400, {"ok": False, "error": "invalid JSON body"})
                return
            try:
                from engine import analysis_rounds

                # Web-launched rounds are analysis-only: trading always waits
                # for the user's later explicit, approved submission.
                payload = dict(payload)
                payload["submit"] = False
                # The two-block boundary is enforced here too: the manual
                # entry requires an explicit standardized symbol list.
                symbols = payload.get("symbols")
                if not isinstance(symbols, list) or len(symbols) == 0:
                    self._send(400, {"ok": False, "error": "手动分析必须提供 symbols：请先运行选股得到标准化候选列表（或直接传入用户点名的股票）"})
                    return
                self._send(200, analysis_rounds.start(payload))
            except (KeyError, ValueError) as exc:
                self._send(400, {"ok": False, "error": str(exc)[:800]})
            except Exception as exc:  # noqa: BLE001
                logger.exception("analysis round start failed")
                self._send(500, {"ok": False, "error": str(exc)[:800]})
            return
        if path.startswith("/api/analysis/runs/"):
            payload = _read_json(self)
            if payload is None:
                self._send(400, {"ok": False, "error": "invalid JSON body"})
                return
            try:
                from engine import analysis_runs

                if path == "/api/analysis/runs/start":
                    result = analysis_runs.start_or_resume(payload)
                elif path == "/api/analysis/runs/update":
                    result = analysis_runs.update(payload)
                elif path == "/api/analysis/runs/complete":
                    result = analysis_runs.finish(payload)
                elif path == "/api/analysis/runs/fail":
                    result = analysis_runs.finish(payload, failed=True)
                else:
                    self._send(404, {"ok": False, "error": "not found", "path": path})
                    return
                self._send(200, {"ok": True, "analysis": result})
            except (KeyError, ValueError) as exc:
                self._send(400, {"ok": False, "error": str(exc)[:800]})
            except Exception as exc:  # noqa: BLE001
                logger.exception("analysis run update failed")
                self._send(500, {"ok": False, "error": str(exc)[:800]})
            return
        if self.path == "/api/credentials/set":
            payload = _read_json(self)
            if payload is None:
                self._send(400, {"ok": False, "error": "invalid JSON body"})
                return
            ref = str(payload.get("ref", "")).strip()
            if not ref:
                self._send(400, {"ok": False, "error": "missing ref"})
                return
            try:
                from engine.secret_store import save_secret

                save_secret(ref, str(payload.get("value", "")))
                self._send(200, {"ok": True, "ref": ref, "configured": bool(str(payload.get("value", "")).strip())})
            except Exception as exc:  # noqa: BLE001
                logger.exception("credential set failed")
                self._send(500, {"ok": False, "error": str(exc)[:500]})
            return
        if self.path == "/api/credentials/unset":
            payload = _read_json(self)
            if payload is None:
                self._send(400, {"ok": False, "error": "invalid JSON body"})
                return
            ref = str(payload.get("ref", "")).strip()
            from engine.secret_store import delete_secret

            delete_secret(ref)
            self._send(200, {"ok": True, "ref": ref, "configured": False})
            return
        if self.path == "/api/config/update":
            payload = _read_json(self)
            if payload is None:
                self._send(400, {"ok": False, "error": "invalid JSON body"})
                return
            try:
                from engine.config_api import apply_config_changes

                changes = payload.get("changes")
                result = apply_config_changes(changes)
                self._send(200, {"ok": True, "updated": result})
            except Exception as exc:  # noqa: BLE001
                logger.exception("config update failed")
                self._send(400, {"ok": False, "error": str(exc)[:800]})
            return
        if self.path == "/api/setup/complete":
            payload = _read_json(self) or {}
            try:
                self._complete_setup(payload)
                self._send(200, {"ok": True, "setup": "complete"})
            except Exception as exc:  # noqa: BLE001
                logger.exception("setup complete failed")
                self._send(500, {"ok": False, "error": str(exc)[:1000]})
            return
        if self.path != "/api/commands/issue":
            self._send(404, {"ok": False, "error": "not found", "path": self.path})
            return
        payload = _read_json(self)
        if payload is None:
            self._send(400, {"ok": False, "error": "invalid JSON body"})
            return
        command = payload.get("command")
        if not command:
            self._send(400, {"ok": False, "error": "missing command"})
            return
        try:
            from engine.investment.service import InvestmentAgentService

            result = InvestmentAgentService().issue(
                str(command),
                payload=payload.get("payload") or {},
                requested_by=str(payload.get("requested_by", "api"))[:80],
            )
            self._send(200, {"ok": True, **result})
        except Exception as exc:  # noqa: BLE001 - the caller needs the message
            logger.exception("command dispatch failed: %s", command)
            self._send(500, {"ok": False, "error": str(exc)[:1000]})


def serve(*, host: str = "127.0.0.1", port: int = 8790) -> None:
    logger.info("Starting engine command API on %s:%s", host, port)
    server = ThreadingHTTPServer((host, port), _Handler)

    # Recovery scan runs only after the socket is bound/listening: resumed
    # headless rounds must be able to post their checkpoints back to THIS
    # server. Only the serve process hosts analysis workers, so only it scans.
    import threading

    def _startup_recovery() -> None:
        import time as _time

        _time.sleep(1.5)
        try:
            from engine import analysis_rounds

            analysis_rounds.recover_on_startup()
        except Exception:  # noqa: BLE001
            logger.exception("analysis round startup recovery failed")

    threading.Thread(target=_startup_recovery, name="analysis-rounds-recovery", daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
