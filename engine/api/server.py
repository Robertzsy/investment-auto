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
    server_version = "InvestmentAutoEngine/2.0"

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        self._send(404, {"ok": False, "error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(403, {"ok": False, "error": "invalid access token"})
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
