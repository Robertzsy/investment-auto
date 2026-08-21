"""P0b tests: per-launch access token and chat ready file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ui import server


class _ProbeHandler:
    def __init__(self, path: str, headers: dict | None = None) -> None:
        self.path = path
        self.headers = headers or {}
        self.status = None
        self.body = b""

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        pass

    def end_headers(self) -> None:
        pass

    @property
    def wfile(self):
        return self

    def write(self, data: bytes) -> None:
        self.body += data


def test_development_no_token_allows_open_access(monkeypatch):
    monkeypatch.setattr(server, "ACCESS_TOKEN", "")
    handler = _ProbeHandler("/api/history")
    assert server.ChatHandler._authorized(handler) is True


def test_token_required_without_credentials(monkeypatch):
    monkeypatch.setattr(server, "ACCESS_TOKEN", "sekret-token")
    handler = _ProbeHandler("/api/history")
    assert server.ChatHandler._authorized(handler) is False


def test_token_accepted_via_query(monkeypatch):
    monkeypatch.setattr(server, "ACCESS_TOKEN", "sekret-token")
    handler = _ProbeHandler("/api/history?token=sekret-token")
    assert server.ChatHandler._authorized(handler) is True


def test_token_accepted_via_header(monkeypatch):
    monkeypatch.setattr(server, "ACCESS_TOKEN", "sekret-token")
    handler = _ProbeHandler("/api/history", {"X-IA-Token": "sekret-token"})
    assert server.ChatHandler._authorized(handler) is True
    bearer = _ProbeHandler("/api/history", {"Authorization": "Bearer sekret-token"})
    assert server.ChatHandler._authorized(bearer) is True


def test_wrong_token_rejected(monkeypatch):
    monkeypatch.setattr(server, "ACCESS_TOKEN", "sekret-token")
    handler = _ProbeHandler("/api/history", {"X-IA-Token": "wrong"})
    assert server.ChatHandler._authorized(handler) is False


def test_unauthorized_returns_401(monkeypatch):
    monkeypatch.setattr(server, "ACCESS_TOKEN", "sekret-token")
    handler = _ProbeHandler("/api/history")
    server.ChatHandler._unauthorized(handler)
    assert handler.status == 401
    assert b"unauthorized" in handler.body


def test_ready_file_written_with_port_and_token(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "runtime_dir", lambda: tmp_path)
    server._write_ready_file("127.0.0.1", 8123, "abc-token")
    ready = tmp_path / "chat.ready.json"
    payload = json.loads(ready.read_text(encoding="utf-8"))
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 8123
    assert payload["token"] == "abc-token"
    assert payload["url"] == "http://127.0.0.1:8123"  # never "localhost" (IPv6 SYN_SENT hang)
    assert payload["pid"] > 0
