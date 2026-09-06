"""Regression tests for Windows GBK subprocess decoding."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from engine.data import fetcher
from engine import main as main_module
from engine import subprocess_utils
from engine.subprocess_utils import decode_subprocess_output, hidden_subprocess_kwargs
from engine.platform import market_tools


def test_windows_stdio_is_reconfigured_to_utf8(monkeypatch):
    calls = []

    class FakeStream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(main_module.sys, "stdout", FakeStream())
    monkeypatch.setattr(main_module.sys, "stderr", FakeStream())
    main_module._configure_stdio()
    assert calls == [
        {"encoding": "utf-8", "errors": "replace"},
        {"encoding": "utf-8", "errors": "replace"},
    ]


def test_decode_subprocess_output_accepts_utf8_and_gb18030():
    text = "贵州茅台：测试输出"
    assert decode_subprocess_output(text.encode("utf-8")) == text
    assert decode_subprocess_output(text.encode("gb18030")) == text


def test_hidden_subprocess_kwargs_uses_create_no_window_on_windows(monkeypatch):
    monkeypatch.setattr(subprocess_utils, "_IS_WINDOWS", True)
    monkeypatch.setattr(subprocess_utils.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert hidden_subprocess_kwargs() == {"creationflags": 0x08000000}


def test_hidden_subprocess_kwargs_is_empty_off_windows(monkeypatch):
    monkeypatch.setattr(subprocess_utils, "_IS_WINDOWS", False)
    assert hidden_subprocess_kwargs() == {}


def test_stock_fetcher_captures_bytes_instead_of_locale_text(monkeypatch):
    def fake_run(*args, **kwargs):
        assert kwargs.get("text") is not True
        if subprocess_utils._IS_WINDOWS:
            assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW
        return SimpleNamespace(
            stdout='{"name":"贵州茅台"}'.encode("utf-8"),
            stderr=b"",
            returncode=0,
        )

    monkeypatch.setattr(market_tools.subprocess, "run", fake_run)
    assert market_tools.stock_fetcher("snapshot", "600519") == {"name": "贵州茅台"}


def test_data_fetcher_decodes_node_utf8(monkeypatch):
    def fake_run(*args, **kwargs):
        assert kwargs.get("text") is not True
        if subprocess_utils._IS_WINDOWS:
            assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW
        return SimpleNamespace(
            stdout='{"code":"sh600519","name":"贵州茅台"}'.encode("utf-8"),
            stderr=b"",
            returncode=0,
        )

    monkeypatch.setattr(fetcher.subprocess, "run", fake_run)
    assert fetcher.snapshot("600519")["name"] == "贵州茅台"
