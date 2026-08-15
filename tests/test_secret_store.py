"""DPAPI secret store tests (Windows-only roundtrip)."""

from __future__ import annotations

import os

import pytest

from src import paths, secret_store


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    yield


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_secret_roundtrip_without_plaintext(isolated_store, tmp_path):
    secret_store.save_secret("OPENAI_API_KEY", "sk-very-secret")
    assert secret_store.load_secret("OPENAI_API_KEY") == "sk-very-secret"
    assert secret_store.list_keys() == ["OPENAI_API_KEY"]
    raw = (tmp_path / "secrets.enc").read_text(encoding="utf-8")
    assert "sk-very-secret" not in raw  # ciphertext only


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_delete_and_empty_value_remove(isolated_store, tmp_path):
    secret_store.save_secret("KEY_A", "one")
    secret_store.save_secret("KEY_A", "")  # empty clears
    assert secret_store.load_secret("KEY_A") is None
    secret_store.save_secret("KEY_B", "two")
    secret_store.delete_secret("KEY_B")
    assert secret_store.load_secret("KEY_B") is None


def test_missing_key_returns_none(isolated_store):
    assert secret_store.load_secret("NEVER_SET") is None


def test_save_requires_key(isolated_store):
    with pytest.raises(ValueError):
        secret_store.save_secret("", "x")
