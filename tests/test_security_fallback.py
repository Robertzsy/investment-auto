"""Security and runtime LLM fallback regression tests."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.llm import registry
from src.llm.adapter import GenericOpenAILLM
from src.ui import server


@pytest.fixture(autouse=True)
def isolated_investment_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.investment import service

    monkeypatch.setenv("INVESTMENT_AGENT_TRANSPORT", "local")
    monkeypatch.setattr(service, "COMMAND_DIR", tmp_path / "investment_commands")


class _FakeHandler:
    def __init__(self, body: bytes = b"") -> None:
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.responses: list[tuple[int, dict]] = []

    def _json_response(self, status: int, data: dict) -> None:
        self.responses.append((status, data))


def test_env_endpoint_masks_sensitive_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = "super-secret-value-that-must-never-leak"
    (tmp_path / ".env").write_text(
        f"OPENAI_API_KEY={secret}\nPUBLIC_REGION=cn-east\nEMPTY_TOKEN=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "data_root", lambda: tmp_path)
    monkeypatch.setattr(server, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(server, "runtime_dir", lambda: tmp_path / "runtime")
    handler = _FakeHandler()

    server.ChatHandler._handle_get_env(handler)  # type: ignore[arg-type]

    status, payload = handler.responses[-1]
    assert status == 200
    assert payload["OPENAI_API_KEY"] == server.ENV_VALUE_MASK
    assert secret not in repr(payload)
    assert payload["PUBLIC_REGION"] == "cn-east"
    assert payload["EMPTY_TOKEN"] == ""


def test_env_save_validates_and_updates_running_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("EXISTING_API_KEY=keep-me\n", encoding="utf-8")
    monkeypatch.setattr(server, "data_root", lambda: tmp_path)
    monkeypatch.setattr(server, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(server, "runtime_dir", lambda: tmp_path / "runtime")
    monkeypatch.delenv("NEW_API_KEY", raising=False)
    handler = _FakeHandler(b'{"EXISTING_API_KEY":"","NEW_API_KEY":"fresh"}')

    server.ChatHandler._handle_save_env(handler)  # type: ignore[arg-type]

    assert handler.responses[-1] == (200, {"ok": True})
    saved = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "EXISTING_API_KEY=keep-me" in saved
    assert "NEW_API_KEY=fresh" in saved
    assert os.environ["NEW_API_KEY"] == "fresh"
    # Windows does not implement POSIX mode bits through chmod. The file is
    # still created successfully; ACL hardening is an OS/deployment concern.
    if os.name != "nt":
        assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600

    invalid = _FakeHandler(b'{"BAD\\nKEY":"value"}')
    server.ChatHandler._handle_save_env(invalid)  # type: ignore[arg-type]
    assert invalid.responses[-1][0] == 400
    assert "BAD\nKEY=value" not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_operation_mode_api_enables_complete_cycle_execution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INVESTMENT_AGENT_TRANSPORT", "local")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text("autonomous:\n  enabled: false\n  auto_execute: false\n", encoding="utf-8")
    monkeypatch.setattr(server, "data_root", lambda: tmp_path)
    monkeypatch.setattr(server, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(server, "runtime_dir", lambda: tmp_path / "runtime")
    monkeypatch.setattr("src.config.cfg._path", config_path)
    monkeypatch.setattr("src.config.cfg.reload", lambda: None)

    handler = _FakeHandler(b'{"mode":"automatic"}')
    server.ChatHandler._handle_autonomy_control(handler, "mode")  # type: ignore[arg-type]

    assert handler.responses[-1][0] == 200
    assert handler.responses[-1][1]["ok"] is True
    assert handler.responses[-1][1]["mode"] == "automatic"
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["autonomous"] == {
        "enabled": True,
        "auto_execute": True,
        "operation_mode": "automatic",
    }

    invalid = _FakeHandler(b'{"mode":"sometimes"}')
    server.ChatHandler._handle_autonomy_control(invalid, "mode")  # type: ignore[arg-type]
    assert invalid.responses[-1][0] == 400


def test_autonomy_status_api_exposes_operation_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.trading import controller

    monkeypatch.setattr(controller, "AUDIT_DIR", tmp_path / "audit")
    handler = _FakeHandler()

    server.ChatHandler._handle_autonomy_status(handler)  # type: ignore[arg-type]

    status, payload = handler.responses[-1]
    assert status == 200
    assert payload["operation_mode"] in {"manual", "automatic"}
    assert isinstance(payload["auto_execute"], bool)
    assert "control" in payload


def test_market_config_api_exposes_rules_and_updates_only_risk_controls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    market_dir = tmp_path / "config" / "market"
    market_dir.mkdir(parents=True)
    base = {
        "name": "测试市场",
        "trading": {
            "settlement": "T+1",
            "lot_size": 100,
            "commission_rate": 0.00025,
            "stamp_tax": 0.001,
            "slippage": 0.001,
        },
        "risk": {
            "single_stock_max_pct": 12,
            "min_cash_reserve_pct": 3,
            "hard_stop_pct": -8,
            "trailing_stop_pct": -5,
            "take_profit_1_pct": 12,
            "take_profit_1_sell_ratio": 0.4,
            "take_profit_2_pct": 20,
            "take_profit_2_sell_ratio": 0.5,
            "max_drawdown_pct": -20,
            "unexposed_control": 99,
        },
    }
    for filename in server._MARKET_CONFIG_FILES.values():
        (market_dir / filename).write_text(
            yaml.safe_dump(base, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    monkeypatch.setattr(server, "data_root", lambda: tmp_path)
    monkeypatch.setattr(server, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(server, "runtime_dir", lambda: tmp_path / "runtime")

    get_handler = _FakeHandler()
    server.ChatHandler._handle_get_market_configs(get_handler)  # type: ignore[arg-type]
    status, payload = get_handler.responses[-1]
    assert status == 200
    assert payload["cn"]["rules"]["settlement"] == "T+1"
    assert payload["cn"]["risk"]["hard_stop_pct"] == -8
    assert "unexposed_control" not in payload["cn"]["risk"]

    update = {
        "cn": {
            "risk": {
                "single_stock_max_pct": 10,
                "min_cash_reserve_pct": 5,
                "hard_stop_pct": -7,
                "trailing_stop_pct": -4,
                "take_profit_1_pct": 15,
                "take_profit_1_sell_ratio": 0.3,
                "take_profit_2_pct": 25,
                "take_profit_2_sell_ratio": 0.4,
                "max_drawdown_pct": -18,
            }
        }
    }
    save_handler = _FakeHandler(json.dumps(update).encode())
    server.ChatHandler._handle_save_market_configs(save_handler)  # type: ignore[arg-type]
    assert save_handler.responses[-1] == (200, {"ok": True})
    saved = yaml.safe_load((market_dir / "cn.yaml").read_text(encoding="utf-8"))
    assert saved["risk"]["hard_stop_pct"] == -7
    assert saved["risk"]["unexposed_control"] == 99
    assert saved["trading"] == base["trading"]


def test_market_config_api_rejects_unexposed_or_invalid_controls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server, "data_root", lambda: tmp_path)
    monkeypatch.setattr(server, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(server, "runtime_dir", lambda: tmp_path / "runtime")
    unknown = _FakeHandler(b'{"cn":{"risk":{"commission_rate":0}}}')
    server.ChatHandler._handle_save_market_configs(unknown)  # type: ignore[arg-type]
    assert unknown.responses[-1][0] == 400

    invalid = _FakeHandler(b'{"cn":{"risk":{"min_cash_reserve_pct":101}}}')
    server.ChatHandler._handle_save_market_configs(invalid)  # type: ignore[arg-type]
    assert invalid.responses[-1][0] == 400


def test_control_plane_responses_do_not_enable_wildcard_cors() -> None:
    handler = server.ChatHandler.__new__(server.ChatHandler)
    sent_headers: list[tuple[str, str]] = []
    handler.send_response = lambda status: None  # type: ignore[method-assign]
    handler.send_header = lambda key, value: sent_headers.append((key, value))  # type: ignore[method-assign]
    handler.end_headers = lambda: None  # type: ignore[method-assign]
    handler.wfile = io.BytesIO()

    handler._json_response(200, {"ok": True})
    handler.do_OPTIONS()

    assert not any(key.lower() == "access-control-allow-origin" for key, _ in sent_headers)
    assert "Access-Control-Allow-Origin" not in Path(server.__file__).read_text(encoding="utf-8")


class _FakeConfig:
    llm_primary_provider = "primary"
    llm_fallback_providers = ["secondary", "tertiary"]
    raw = {"llm": {"models": {}}}

    @staticmethod
    def llm_role_model(role: str) -> str:
        return "primary-model"


def test_runtime_fallback_switches_provider_after_chat_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    class Adapter:
        def __init__(self, provider: str) -> None:
            self.provider = provider

        def chat(self, messages, **kwargs):
            attempts.append(self.provider)
            if self.provider == "primary":
                raise ConnectionError("primary unavailable")
            return "fallback response"

    monkeypatch.setattr(registry, "cfg", _FakeConfig())
    monkeypatch.setattr(
        registry,
        "_build_llm",
        lambda provider, model_override=None: Adapter(provider),
    )

    llm = registry.resolve_llm()
    assert llm.chat([{"role": "user", "content": "hello"}]) == "fallback response"
    assert attempts == ["primary", "secondary"]


def test_runtime_fallback_switches_provider_after_empty_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    class Adapter:
        def __init__(self, provider: str) -> None:
            self.provider = provider

        def chat(self, messages, **kwargs):
            attempts.append(self.provider)
            return "" if self.provider == "primary" else "non-empty fallback"

        def chat_stream(self, messages, **kwargs):
            attempts.append(self.provider)
            if self.provider != "primary":
                yield "stream fallback"

    monkeypatch.setattr(registry, "cfg", _FakeConfig())
    monkeypatch.setattr(registry, "_build_llm", lambda provider, model_override=None: Adapter(provider))

    assert registry.resolve_llm().chat([]) == "non-empty fallback"
    assert attempts == ["primary", "secondary"]
    attempts.clear()
    assert list(registry.resolve_llm().chat_stream([])) == ["stream fallback"]
    assert attempts == ["primary", "secondary"]


def test_runtime_fallback_propagates_interruption_without_switching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    class Adapter:
        def __init__(self, provider: str) -> None:
            self.provider = provider

        def chat(self, messages, **kwargs):
            attempts.append(self.provider)
            raise InterruptedError("cancelled")

    monkeypatch.setattr(registry, "cfg", _FakeConfig())
    monkeypatch.setattr(
        registry,
        "_build_llm",
        lambda provider, model_override=None: Adapter(provider),
    )

    with pytest.raises(InterruptedError, match="cancelled"):
        registry.resolve_llm().chat([{"role": "user", "content": "hello"}])
    assert attempts == ["primary"]


def test_runtime_stream_fallback_and_interruption(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    class Adapter:
        def __init__(self, provider: str) -> None:
            self.provider = provider

        def chat_stream(self, messages, **kwargs):
            attempts.append(self.provider)
            if self.provider == "primary":
                raise ConnectionError("stream unavailable")
            yield "fallback stream"

    monkeypatch.setattr(registry, "cfg", _FakeConfig())
    monkeypatch.setattr(
        registry,
        "_build_llm",
        lambda provider, model_override=None: Adapter(provider),
    )

    assert list(registry.resolve_llm().chat_stream([])) == ["fallback stream"]
    assert attempts == ["primary", "secondary"]

    attempts.clear()

    def interrupted_build(provider: str, model_override=None):
        class InterruptedAdapter:
            def chat_stream(self, messages, **kwargs):
                attempts.append(provider)
                raise InterruptedError("cancelled")
                yield  # pragma: no cover - make this a generator

        return InterruptedAdapter()

    monkeypatch.setattr(registry, "_build_llm", interrupted_build)
    with pytest.raises(InterruptedError, match="cancelled"):
        list(registry.resolve_llm().chat_stream([]))
    assert attempts == ["primary"]


def test_server_never_falls_back_to_all_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = []

    def fail_bind(address, handler):
        attempts.append(address)
        raise OSError("address unavailable")

    monkeypatch.setattr(server, "ThreadingHTTPServer", fail_bind)
    with pytest.raises(RuntimeError, match="Cannot bind to localhost:8080"):
        server.start_server(host="localhost", port=8080, open_browser=False)
    assert attempts == [("localhost", 8080)]


def test_provider_never_reuses_openai_key_for_another_vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-only-secret")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        GenericOpenAILLM(
            {
                "provider_name": "deepseek",
                "api_key_env": "DEEPSEEK_API_KEY",
                "api_base": "https://api.deepseek.com",
                "model": "deepseek-v4-pro",
            }
        )


def test_llm_client_uses_bounded_configurable_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    monkeypatch.setenv("TEST_API_KEY", "configured")
    monkeypatch.setattr(
        "src.llm.adapter.OpenAI",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(),
    )

    GenericOpenAILLM(
        {
            "provider_name": "test",
            "api_key_env": "TEST_API_KEY",
            "api_base": "https://example.invalid/v1",
            "model": "test-model",
            "request_timeout_seconds": 75,
        }
    )

    assert captured["timeout"] == 75


def test_chat_and_cancel_handlers_forward_valid_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    request_id = "request-1234"
    payload = (
        '{"message":"hello","stream":true,"provider":"deepseek",'
        '"model":"deepseek-v4-pro","request_id":"request-1234"}'
    ).encode()
    handler = server.ChatHandler.__new__(server.ChatHandler)
    handler.headers = {"Content-Length": str(len(payload))}
    handler.rfile = io.BytesIO(payload)
    responses = []
    handler._json_response = lambda status, data: responses.append((status, data))  # type: ignore[method-assign]
    forwarded = []
    handler._handle_chat_stream = lambda *args: forwarded.append(args)  # type: ignore[method-assign]

    handler._handle_chat()

    assert forwarded == [("hello", False, "deepseek", "deepseek-v4-pro", request_id)]
    assert responses == []

    cancelled = []
    monkeypatch.setattr("src.ui.chat_server.request_cancel", lambda value=None: cancelled.append(value) or True)
    cancel_body = b'{"request_id":"request-1234"}'
    handler.path = "/api/chat/cancel"
    handler.headers = {"Content-Length": str(len(cancel_body))}
    handler.rfile = io.BytesIO(cancel_body)
    handler._handle_chat_cancel()

    assert cancelled == [request_id]
    assert responses[-1] == (200, {"ok": True, "cancelled": True})


def test_investment_cycle_endpoint_validates_and_forwards_market(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b'{"market":"us","request_id":"cycle-123"}'
    handler = server.ChatHandler.__new__(server.ChatHandler)
    handler.headers = {"Content-Length": str(len(payload))}
    handler.rfile = io.BytesIO(payload)
    handler.wfile = io.BytesIO()
    handler.send_response = lambda status: None  # type: ignore[method-assign]
    handler.send_header = lambda key, value: None  # type: ignore[method-assign]
    handler.end_headers = lambda: None  # type: ignore[method-assign]
    forwarded = []
    monkeypatch.setattr(
        "src.ui.chat_server.handle_investment_cycle_stream",
        lambda market, request_id=None: forwarded.append((market, request_id)) or iter([
            {"type": "final", "content": "done"}
        ]),
    )

    handler._handle_investment_cycle()

    assert forwarded == [("us", "cycle-123")]
    assert b'"type": "final"' in handler.wfile.getvalue()

    invalid = _FakeHandler(b'{"market":"mars"}')
    server.ChatHandler._handle_investment_cycle(invalid)  # type: ignore[arg-type]
    assert invalid.responses[-1][0] == 400


def test_invalid_request_id_is_rejected() -> None:
    payload = b'{"message":"hello","request_id":"bad id with spaces"}'
    handler = server.ChatHandler.__new__(server.ChatHandler)
    handler.headers = {"Content-Length": str(len(payload))}
    handler.rfile = io.BytesIO(payload)
    responses = []
    handler._json_response = lambda status, data: responses.append((status, data))  # type: ignore[method-assign]

    handler._handle_chat()

    assert responses[-1] == (400, {"error": "invalid request_id"})


def test_explicit_provider_does_not_use_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[str] = []

    class ExplicitAdapter:
        def chat(self, messages, **kwargs):
            raise ConnectionError("explicit provider unavailable")

    def build(provider: str, model_override=None):
        builds.append(provider)
        return ExplicitAdapter()

    monkeypatch.setattr(registry, "cfg", _FakeConfig())
    monkeypatch.setattr(registry, "_build_llm", build)

    llm = registry.resolve_llm(provider="secondary", model="chosen-model")
    with pytest.raises(ConnectionError, match="explicit provider unavailable"):
        llm.chat([{"role": "user", "content": "hello"}])
    assert builds == ["secondary"]
