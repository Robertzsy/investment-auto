"""Engine tests for the product-settings config endpoints."""
from __future__ import annotations

import json

import pytest

from engine import config_api


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "schedule:\n  timezone: Asia/Shanghai\n  intraday_rounds:\n    cn: ['09:30']\n"
        "autonomous:\n  operation_mode: manual\n  enabled: true\n"
        "trading:\n  mode: paper\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    from engine import config as config_module

    # The global cfg singleton is shared by every test in the session; keep
    # the original path/data so later tests see the real repo config again.
    original_path = config_module.cfg._path
    original_data = config_module.cfg._data
    monkeypatch.setattr(config_module.cfg, "_path", str(config_path))
    monkeypatch.setattr(config_module.cfg, "_data", original_data)
    config_module.cfg.reload()
    yield config_path
    # Restore the singleton state for the rest of the suite.
    config_module.cfg._data = original_data
    config_module.cfg._path = original_path


def test_apply_allows_whitelisted_paths(isolated_config):
    updated = config_api.apply_config_changes({
        "autonomous.operation_mode": "automatic",
        "schedule.intraday_rounds.cn": ["09:30", "10:30"],
        "markets.enable": ["cn", "us"],
    })
    assert updated == sorted(["autonomous.operation_mode", "markets.enable", "schedule.intraday_rounds.cn"])
    from engine.config import cfg

    assert cfg.autonomous.get("operation_mode") == "automatic"
    assert cfg.schedule.get("intraday_rounds", {}).get("cn") == ["09:30", "10:30"]


def test_apply_refuses_forbidden_boundaries(isolated_config):
    with pytest.raises(ValueError, match="禁止修改"):
        config_api.apply_config_changes({"trading.mode": "live"})
    with pytest.raises(ValueError, match="不允许修改"):
        config_api.apply_config_changes({"llm.provider": "openai"})
    with pytest.raises(ValueError, match="不允许修改"):
        config_api.apply_config_changes({"secrets.whatever": 1})
    with pytest.raises(ValueError, match="非空"):
        config_api.apply_config_changes({})


def test_failed_reload_rolls_back_file(isolated_config, monkeypatch):
    import yaml

    from engine import config as config_module

    before = yaml.safe_load(isolated_config.read_text(encoding="utf-8"))

    def broken_reload():
        raise RuntimeError("bad config")

    monkeypatch.setattr(config_module.cfg, "reload", broken_reload)
    with pytest.raises(RuntimeError, match="bad config"):
        config_api.apply_config_changes({"schedule.macro_daily_time": "08:30"})
    after = yaml.safe_load(isolated_config.read_text(encoding="utf-8"))
    assert after == before
    assert after["schedule"]["timezone"] == "Asia/Shanghai"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("schedule.timezone", "UTC"),
        ("autonomous.unknown", True),
        ("optimizer.enabled", False),
        ("autonomous.operation_mode", "sometimes"),
        ("autonomous.enabled", 1),
        ("schedule.macro_daily_time", "25:00"),
        ("schedule.intraday_rounds.cn", ["09:30", "09:30"]),
        ("markets.enable", []),
        ("markets.enable", ["cn", "crypto"]),
        ("screening.shortlist_size", 0),
        ("screening.refresh_minutes", 2),
        ("notify.channels", ["email"]),
    ],
)
def test_apply_rejects_unknown_or_invalid_product_fields(isolated_config, path, value):
    with pytest.raises(ValueError):
        config_api.apply_config_changes({path: value})


def test_validation_is_atomic_before_any_write(isolated_config):
    before = isolated_config.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        config_api.apply_config_changes({
            "autonomous.operation_mode": "automatic",
            "schedule.macro_daily_time": "not-a-time",
        })
    assert isolated_config.read_text(encoding="utf-8") == before


def test_config_endpoint_roundtrip(monkeypatch, tmp_path):
    from engine.api import server

    config_path = tmp_path / "config.yaml"
    config_path.write_text("autonomous:\n  operation_mode: manual\n", encoding="utf-8")
    from engine import config as config_module

    original_path = config_module.cfg._path
    original_data = config_module.cfg._data
    monkeypatch.setattr(config_module.cfg, "_path", str(config_path))
    monkeypatch.setattr(config_module.cfg, "_data", original_data)
    config_module.cfg.reload()
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server._Handler)
    try:
        import threading

        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            import urllib.request

            req = urllib.request.Request(
                base + "/api/config/update",
                data=json.dumps({"changes": {"autonomous.operation_mode": "automatic"}}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                payload = json.loads(response.read().decode())
            assert payload["ok"] is True
            with urllib.request.urlopen(base + "/api/config", timeout=10) as response:
                cfg_payload = json.loads(response.read().decode())
            assert cfg_payload["config"]["autonomous"]["operation_mode"] == "automatic"
        finally:
            httpd.shutdown()
            httpd.server_close()
    finally:
        config_module.cfg._data = original_data
        config_module.cfg._path = original_path
