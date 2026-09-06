from __future__ import annotations

import json

from engine.data import research


def test_research_packet_records_missing_provider_without_fabricating(monkeypatch, tmp_path):
    monkeypatch.setattr(research, "CACHE_DIR", tmp_path)
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)

    packet = research.fetch_research_packet("us", "AAPL")

    assert packet["news"] == []
    assert packet["fundamentals"] == {}
    assert "FINNHUB_API_KEY" in packet["errors"]["company_research"]


def test_research_packet_fetches_and_caches_auditable_us_data(monkeypatch, tmp_path):
    monkeypatch.setattr(research, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.setattr(research, "_finnhub_news", lambda symbol, key, timeout: [{
        "headline": "Test headline", "source": "Test source", "published_at": "2026-08-13T00:00:00+00:00",
    }])
    monkeypatch.setattr(research, "_finnhub_metrics", lambda symbol, key, timeout: {"pe_ttm": 20})

    first = research.fetch_research_packet("us", "AAPL")
    second = research.fetch_research_packet("us", "AAPL")

    assert first["news"][0]["source"] == "Test source"
    assert first["fundamentals"]["pe_ttm"] == 20
    assert first["sources"] == ["finnhub:company-news", "finnhub:basic-financials"]
    assert second["cached"] is True
    saved = json.loads((tmp_path / "us" / "AAPL.json").read_text(encoding="utf-8"))
    assert saved["symbol"] == "AAPL"
