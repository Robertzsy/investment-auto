"""
HTTP server for the AI chat panel.
Uses Python stdlib only – zero extra dependencies.
"""

from __future__ import annotations

import json
import logging
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
UI_DIR = Path(__file__).resolve().parent

logger = logging.getLogger("investment-auto.http")


class ChatHandler(SimpleHTTPRequestHandler):
    """Serves static files from src/ui/ and handles /api/chat POST."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path
        # Default to index.html
        if path == "/" or path == "":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_response(400, {"error": "invalid json"})
            return

        message = data.get("message", "").strip()
        thinking = data.get("thinking", False)

        if not message:
            self._json_response(400, {"error": "empty message"})
            return

        logger.info(f"Chat request: {message[:100]}...")
        try:
            from .chat_server import handle_chat
            reply = handle_chat(message, thinking)
            self._json_response(200, {"reply": reply})
        except Exception as e:
            logger.exception("chat error")
            self._json_response(500, {"error": str(e)})

    def _json_response(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        # suppress default logging noise
        pass


def start_server(host: str = "0.0.0.0", port: int = 8080):
    server = HTTPServer((host, port), ChatHandler)
    logger.info(f"AI Chat Panel running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Chat server stopped")
        server.shutdown()
