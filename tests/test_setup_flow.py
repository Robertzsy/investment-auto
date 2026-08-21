"""First-run wizard endpoint tests (acceptance blockers 1-4).

Covers: config deep-merge (partial saves never wipe other sections), model
wiring, account file creation, mode/mandate persistence without a running
agent, and legacy migration detection via INVESTMENT_AUTO_HOME.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from src.ui import server
from src.config import cfg


class _ProbeHandler:
    """Minimal ChatHandler stand-in capturing the JSON response."""

    def __init__(self, path: str, body: dict | None = None) -> None:
        self.path = path
        payload = json.dumps(body).encode("utf-8") if body else b""
        self.headers = {"Content-Length": str(len(payload))}
        self._rfile = io.BytesIO(payload)
        self.status = None
        self.body = b""

    @property
    def rfile(self):
        return self._rfile

    def _json_response(self, status: int, data: dict) -> None:
        self.status = status
        self.body += json.dumps(data, ensure_ascii=False).encode("utf-8")

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

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


@pytest.fixture
def config_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg, "reload", lambda: None)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "markets": {"enable": ["cn", "hk"]},
                "llm": {
                    "provider": "deepseek",
                    "quick_model": "deepseek-v4-flash",
                    "deep_model": "deepseek-v4-pro",
                    "models": {
                        "deepseek": {
                            "model": "deepseek-v4-pro",
                            "variants": ["deepseek-v4-pro", "deepseek-v4-flash"],
                            "api_key_env": "DEEPSEEK_API_KEY",
                        }
                    },
                },
                "schedule": {"weekdays_only": True},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_deep_merge_preserves_unrelated_keys():
    base = {"a": {"x": 1, "y": 2}, "b": [1, 2], "c": "keep"}
    merged = server._deep_merge(base, {"a": {"x": 9}, "b": [3]})
    assert merged == {"a": {"x": 9, "y": 2}, "b": [3], "c": "keep"}


def test_save_config_merges_instead_of_overwriting(config_tmp):
    handler = _ProbeHandler("/api/config", body={"markets": {"enable": ["cn"]}})
    server.ChatHandler._handle_save_config(handler)

    assert handler.status == 200
    saved = yaml.safe_load((config_tmp / "config.yaml").read_text(encoding="utf-8"))
    assert saved["markets"]["enable"] == ["cn"]
    # Everything else must survive the partial save.
    assert saved["schedule"]["weekdays_only"] is True
    assert saved["llm"]["provider"] == "deepseek"


def test_setup_models_writes_llm_block(config_tmp, monkeypatch):
    monkeypatch.setattr(cfg, "_data", {
        "llm": {"models": {"deepseek": {"variants": ["deepseek-v4-pro", "deepseek-v4-flash"]}}},
    })
    handler = _ProbeHandler("/api/setup/models", body={
        "provider": "deepseek",
        "quick_model": "deepseek-v4-flash",
        "deep_model": "deepseek-v4-pro",
    })
    server.ChatHandler._handle_setup_models(handler)

    assert handler.status == 200
    saved = yaml.safe_load((config_tmp / "config.yaml").read_text(encoding="utf-8"))
    assert saved["llm"]["provider"] == "deepseek"
    assert saved["llm"]["quick_model"] == "deepseek-v4-flash"
    assert saved["llm"]["deep_model"] == "deepseek-v4-pro"
    assert saved["llm"]["models"]["deepseek"]["model"] == "deepseek-v4-pro"
    assert saved["llm"]["role_model_override"]["trader"] == "deepseek-v4-flash"
    assert saved["llm"]["role_model_override"]["risk_manager"] == "deepseek-v4-pro"
    # merge preserved unrelated sections
    assert saved["schedule"]["weekdays_only"] is True
    assert saved["markets"]["enable"] == ["cn", "hk"]


def test_setup_models_rejects_unknown_variant(config_tmp, monkeypatch):
    monkeypatch.setattr(cfg, "_data", {
        "llm": {"models": {"deepseek": {"variants": ["deepseek-v4-pro"]}}},
    })
    handler = _ProbeHandler("/api/setup/models", body={
        "provider": "deepseek",
        "quick_model": "deepseek-v4-flash",  # not in variants
        "deep_model": "deepseek-v4-pro",
    })
    server.ChatHandler._handle_setup_models(handler)
    assert handler.status == 400


def test_setup_init_creates_portfolio_file(monkeypatch, tmp_path):
    from src.portfolio import account

    monkeypatch.setattr(account, "RUNTIME", tmp_path / "data")

    first = _ProbeHandler("/api/setup/init")
    server.ChatHandler._handle_setup_init(first)
    assert first.status == 200
    assert first.json()["created"] is True
    assert (tmp_path / "data" / "portfolio.json").exists()

    second = _ProbeHandler("/api/setup/init")
    server.ChatHandler._handle_setup_init(second)
    assert second.status == 200
    assert second.json()["created"] is False


def test_setup_init_repairs_existing_empty_account_shell(monkeypatch, tmp_path):
    from src.portfolio import account

    monkeypatch.setattr(account, "RUNTIME", tmp_path / "data")
    account._path().parent.mkdir(parents=True)
    account._path().write_text('{"version": 2, "accounts": {}}', encoding="utf-8")

    handler = _ProbeHandler("/api/setup/init")
    server.ChatHandler._handle_setup_init(handler)

    assert handler.status == 200
    assert handler.json()["created"] is False
    saved = json.loads(account._path().read_text(encoding="utf-8"))
    assert set(saved["accounts"]) >= {"cn", "hk", "us", "etf"}
    assert all(saved["accounts"][market]["cash"] == 500000 for market in ("cn", "hk", "us", "etf"))


def test_dashboard_uses_defaults_for_existing_empty_account_shell(monkeypatch, tmp_path):
    from src import screening
    from src.portfolio import account

    monkeypatch.setattr(account, "RUNTIME", tmp_path / "data")
    account._path().parent.mkdir(parents=True)
    account._path().write_text('{"version": 2, "accounts": {}}', encoding="utf-8")
    monkeypatch.setattr(cfg, "_data", {"markets": {"enable": ["cn", "hk", "us", "etf"]}})
    monkeypatch.setattr(server, "runtime_dir", lambda: tmp_path / "runtime")
    monkeypatch.setattr(screening, "latest_screening", lambda market: None)

    data = server.ChatHandler._build_dashboard_data(object(), "all")

    assert data["stats"]["total_assets"] == 2_000_000
    assert data["stats"]["cash_ratio"] == 100
    assert data["asset_distribution"]["values"] == [500000, 500000, 500000, 500000]


def test_setup_mandate_writes_file(monkeypatch, tmp_path):
    from src.investment import mandate

    target = tmp_path / "investment" / "mandate.json"
    monkeypatch.setattr(mandate, "MANDATE_FILE", target)

    handler = _ProbeHandler("/api/setup/mandate", body={"profile": "aggressive"})
    server.ChatHandler._handle_setup_mandate(handler)
    assert handler.status == 200
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["profile"] == "aggressive"

    bad = _ProbeHandler("/api/setup/mandate", body={"profile": "nope"})
    server.ChatHandler._handle_setup_mandate(bad)
    assert bad.status == 400


def test_setup_mode_writes_operation_mode(monkeypatch, tmp_path, config_tmp):
    from src.investment import service

    monkeypatch.setattr(service.cfg, "_path", str(config_tmp / "config.yaml"))

    handler = _ProbeHandler("/api/setup/mode", body={"mode": "manual"})
    server.ChatHandler._handle_setup_mode(handler)
    assert handler.status == 200
    saved = yaml.safe_load((config_tmp / "config.yaml").read_text(encoding="utf-8"))
    assert saved["autonomous"]["operation_mode"] == "manual"
    # merge preserved unrelated sections
    assert saved["schedule"]["weekdays_only"] is True

    bad = _ProbeHandler("/api/setup/mode", body={"mode": "turbo"})
    server.ChatHandler._handle_setup_mode(bad)
    assert bad.status == 400


def test_account_exists_flag(monkeypatch, tmp_path):
    from src.portfolio import account

    monkeypatch.setattr(account, "RUNTIME", tmp_path / "data")
    assert account.exists() is False
    account.save(account.load())
    assert account.exists() is True


def test_migrate_detect_honors_env_home(monkeypatch, tmp_path):
    from src import migration

    legacy = tmp_path / "legacy"
    (legacy / "src").mkdir(parents=True)
    (legacy / "src" / "main.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("INVESTMENT_AUTO_HOME", str(legacy))

    sources = migration.detect_sources()
    assert any(s["path"] == str(legacy) for s in sources)
