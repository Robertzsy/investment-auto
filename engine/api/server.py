"""Minimal loopback HTTP command API for the investment engine.

P0 surface: health, runtime status, command dispatch. P1 grows this into the
full command API consumed by the DSH MCP bridge (market data, screening,
portfolio, reports, scheduler control).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

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
        if self.path == "/api/health":
            from engine.version import __version__

            self._send(200, {"ok": True, "service": "investment-auto-engine", "version": __version__, "pid": os.getpid()})
            return
        if self.path == "/api/status":
            from engine.investment.status import runtime_status

            self._send(200, {"ok": True, **runtime_status()})
            return
        self._send(404, {"ok": False, "error": "not found", "path": self.path})

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
