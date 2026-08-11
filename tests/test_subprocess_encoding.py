"""Regression tests for Windows GBK subprocess decoding."""
from __future__ import annotations

from types import SimpleNamespace

from src.data import fetcher
from src import main as main_module
from src.subprocess_utils import decode_subprocess_output
from src.ui import chat_server


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


def test_stock_fetcher_captures_bytes_instead_of_locale_text(monkeypatch):
    def fake_run(*args, **kwargs):
        assert kwargs.get("text") is not True
        return SimpleNamespace(
            stdout='{"name":"贵州茅台"}'.encode("utf-8"),
            stderr=b"",
            returncode=0,
        )

    monkeypatch.setattr(chat_server.subprocess, "run", fake_run)
    assert chat_server._stock_fetcher("snapshot", "600519") == {"name": "贵州茅台"}


def test_run_shell_decodes_utf8_without_windows_code_page(monkeypatch):
    def fake_run(*args, **kwargs):
        assert kwargs.get("text") is not True
        return SimpleNamespace(stdout="成功".encode("utf-8"), stderr=b"", returncode=0)

    monkeypatch.setattr(chat_server.subprocess, "run", fake_run)
    assert chat_server.run_shell("ignored") == {"stdout": "成功", "stderr": "", "returncode": 0}


def test_data_fetcher_decodes_node_utf8(monkeypatch):
    def fake_run(*args, **kwargs):
        assert kwargs.get("text") is not True
        return SimpleNamespace(
            stdout='{"code":"sh600519","name":"贵州茅台"}'.encode("utf-8"),
            stderr=b"",
            returncode=0,
        )

    monkeypatch.setattr(fetcher.subprocess, "run", fake_run)
    assert fetcher.snapshot("600519")["name"] == "贵州茅台"
