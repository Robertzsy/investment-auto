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


def test_credentials_roundtrip(monkeypatch, api):
    stored = {}

    def fake_save(key, value):
        stored[key] = value

    def fake_load(key):
        return stored.get(key)

    def fake_delete(key):
        stored.pop(key, None)

    monkeypatch.setattr("engine.secret_store.save_secret", fake_save)
    monkeypatch.setattr("engine.secret_store.load_secret", fake_load)
    monkeypatch.setattr("engine.secret_store.delete_secret", fake_delete)

    assert _get(api, "/api/credentials/resolve?ref=DEEPSEEK_API_KEY")["configured"] is False
    status, payload = _post(api, "/api/credentials/set", {"ref": "DEEPSEEK_API_KEY", "value": "sk-test"})
    assert status == 200 and payload["configured"] is True
    resolved = _get(api, "/api/credentials/resolve?ref=DEEPSEEK_API_KEY")
    assert resolved["configured"] is True and resolved["value"] == "sk-test"
    _post(api, "/api/credentials/unset", {"ref": "DEEPSEEK_API_KEY"})
    assert _get(api, "/api/credentials/resolve?ref=DEEPSEEK_API_KEY")["configured"] is False


def test_setup_status_and_complete(monkeypatch, api, tmp_path):
    from engine import paths

    marker_dir = tmp_path / "runtime"
    marker_dir.mkdir()
    monkeypatch.setattr(paths, "runtime_dir", lambda: marker_dir)
    from engine.portfolio import account as account_module

    portfolio = tmp_path / "portfolio.json"
    monkeypatch.setattr(account_module, "_path", lambda: portfolio)
    monkeypatch.setattr(account_module, "exists", lambda: False)
    monkeypatch.setattr(
        "engine.portfolio.account.save",
        lambda data: portfolio.write_text(json.dumps(data), encoding="utf-8"),
    )

    assert _get(api, "/api/setup/status")["first_run"] is True
    status, payload = _post(api, "/api/setup/complete", {"import_from": ""})
    assert status == 200
    assert (marker_dir / "setup.complete").exists()
    assert _get(api, "/api/setup/status")["first_run"] is False
    assert portfolio.exists()
