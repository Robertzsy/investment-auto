"""Engine HTTP command API smoke tests (loopback, in-process)."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from engine.api import server


@pytest.fixture
def api():
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base: str, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_health_endpoint(api):
    payload = _get(api, "/api/health")
    assert payload["ok"] is True
    assert payload["service"] == "investment-auto-engine"


def test_status_endpoint_reports_engine_state(api):
    payload = _get(api, "/api/status")
    assert payload["ok"] is True
    assert "control" in payload
    assert "markets" in payload
    assert payload["operation_mode"] in {"manual", "automatic"}


def test_unknown_route_returns_404(api):
    status, payload = _post(api, "/api/unknown", {})
    assert status == 404
    assert payload["ok"] is False


def test_invalid_json_body_is_rejected(api):
    request = urllib.request.Request(
        api + "/api/commands/issue",
        data=b"{not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5)
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_unknown_command_returns_dispatch_error(api):
    status, payload = _post(api, "/api/commands/issue", {"command": "bogus_command", "payload": {}})
    assert status == 500
    assert payload["ok"] is False


def test_authorized_mode_rejects_missing_token(monkeypatch, api):
    monkeypatch.setenv("IA_ACCESS_TOKEN", "secret-token")
    status, payload = _post(api, "/api/commands/issue", {"command": "status", "payload": {}})
    assert status == 403
