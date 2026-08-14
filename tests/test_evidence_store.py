from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from src.trading import agent_workflow, evidence_store


def test_compact_report_trims_findings_but_keeps_decisions():
    payload = {
        "role": "trader",
        "summary": "买入",
        "stance": "BUY",
        "confidence": 0.8,
        "citations": ["AGENT:RESEARCH_MANAGER"],
        "findings": [
            {"claim": "A" * 120, "evidence_ids": ["MARKET:600519"], "reason": "r" * 150},
            {"claim": "B", "evidence_ids": ["TECHNICAL:600519"]},
            {"claim": "C", "evidence_ids": ["HISTORY:600519"]},
            {"claim": "D", "evidence_ids": ["NEWS:600519"]},
        ],
        "decisions": [{"decision_id": "d1", "symbol": "600519", "action": "BUY",
                       "target_weight": 0.1, "confidence": 0.8, "reason": "x"}],
        "data_gaps": ["a", "b", "c", "d", "e", "f"],
        "memory_note": "m" * 300,
    }
    compact = evidence_store.compact_report(payload)

    assert len(compact["findings"]) == 3
    assert compact["findings"][0]["claim"] == "A" * 80
    assert compact["findings"][0]["reason"] == "r" * 120
    assert "D" not in [f["claim"] for f in compact["findings"]]
    assert compact["decisions"] == payload["decisions"]  # intact
    assert len(compact["data_gaps"]) == 5
    assert len(compact["memory_note"]) == 160


def test_save_and_load_cycle_evidence_roundtrip(tmp_path):
    evidence_store.EVIDENCE_DIR = tmp_path / "evidence"
    ref = evidence_store.save_cycle_evidence(
        "2026-08-14T10:30:00+08:00", "cn",
        {"catalog": {"MARKET:600519": {"price": 100}}, "portfolio_evidence": {}},
    )
    assert ref.endswith("cn.json")
    assert (tmp_path / "evidence").exists()
    loaded = evidence_store.load_cycle_evidence(ref)
    assert loaded["catalog"]["MARKET:600519"]["price"] == 100
    evidence_store.EVIDENCE_DIR = evidence_store.ROOT / "runtime" / "trading" / "evidence"


def test_workflow_archives_evidence_and_returns_compact_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_store, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(agent_workflow, "_architecture_settings", lambda: {"evidence_store": True})
    calls = []

    def fake_call(role, **kwargs):
        evidence = kwargs["evidence"]
        calls.append((role, sorted(evidence)))
        citation = next(iter(evidence))
        if kwargs.get("portfolio"):
            return {
                "role": role,
                "thesis": "test",
                "decisions": [{
                    "decision_id": "p1", "symbol": "600519", "action": "BUY",
                    "target_weight": 0.1, "confidence": 0.8, "reason": "test",
                    "evidence_ids": [citation],
                }],
                "citations": [citation],
            }
        return {
            "role": role,
            "summary": "test",
            "findings": [
                {"claim": f"finding {i}", "evidence_ids": [citation]} for i in range(5)
            ],
            "citations": [citation],
        }

    monkeypatch.setattr(agent_workflow, "_call_role", fake_call)
    monkeypatch.setattr(
        agent_workflow,
        "_parallel_roles",
        lambda roles, **kwargs: ({role: fake_call(role, **kwargs) for role in roles}, {}),
    )
    context = {
        "as_of": "2026-08-12T10:00:00+08:00",
        "market": "cn",
        "allowed_symbols": ["600519"],
        "account": {"cash": 100000, "holdings": []},
        "snapshots": {
            "600519": {
                "realtime": {"price": 100, "pe": 20},
                "indicators": {"rsi": {"rsi14": 55}},
                "history": [{"date": "2026-08-11", "close": 99}],
            }
        },
        "screening": {
            "selected": [{"symbol": "600519", "score": 80}],
            "selected_symbols": ["600519"],
        },
        "macro_excerpt": "测试宏观摘要",
        "optimizer": {"recommended_scheme": "risk_parity"},
        "market_rules": {"trading": {"settlement": "T+1"}},
    }
    config = {
        "agent_workflow": {
            "enabled": True,
            "require_citations": True,
            "minimum_citations": 1,
            "minimum_base_analysts": 2,
            "research_debate_rounds": 1,
            "risk_debate_rounds": 1,
            "memory_enabled": True,
            "memory_entries_per_role": 3,
            "memory_chars_per_entry": 200,
            "roles": {},
        }
    }
    result = agent_workflow.run_analysis_workflow(
        context, config, memory_store=agent_workflow.AgentMemoryStore(tmp_path)
    )

    assert result["evidence_ref"].endswith("cn.json")
    # The archived graph carries the full base reports.
    archived = evidence_store.load_cycle_evidence(result["evidence_ref"])
    assert isinstance(archived, Mapping)
    # Compact summary keeps decisions but trims long findings.
    trader = result["symbol_research"]["600519"]["trader"]
    assert "findings" in trader and len(trader["findings"]) <= 3
    assert result["portfolio_manager"]["decisions"][0]["symbol"] == "600519"


def test_evidence_text_cap_respects_architecture_setting(monkeypatch):
    evidence = {"MARKET:600519": {"price": 100, "blob": "x" * 50000}}
    monkeypatch.setattr(
        agent_workflow, "_architecture_settings",
        lambda: {"evidence_text_max_chars": 800},
    )
    text = agent_workflow._evidence_text(evidence, max_chars=800)
    assert len(text) <= 800



def test_workflow_without_architecture_config_matches_legacy(monkeypatch, tmp_path):
    # Rollback smoke: with no architecture block every new behavior is off,
    # so the workflow behaves exactly like the pre-upgrade version.
    monkeypatch.setattr(evidence_store, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(agent_workflow, "_architecture_settings", lambda: {})

    def fake_call(role, **kwargs):
        evidence = kwargs["evidence"]
        citation = next(iter(evidence))
        if kwargs.get("portfolio"):
            return {
                "role": role, "thesis": "test",
                "decisions": [{"decision_id": "p1", "symbol": "600519",
                                 "action": "BUY", "target_weight": 0.1,
                                 "confidence": 0.8, "reason": "test",
                                 "evidence_ids": [citation]}],
                "citations": [citation],
            }
        return {
            "role": role, "summary": "test",
            "findings": [{"claim": "f", "evidence_ids": [citation]}],
            "citations": [citation],
        }

    monkeypatch.setattr(agent_workflow, "_call_role", fake_call)
    monkeypatch.setattr(
        agent_workflow, "_parallel_roles",
        lambda roles, **kwargs: ({role: fake_call(role, **kwargs) for role in roles}, {}),
    )
    result = agent_workflow.run_analysis_workflow(
        {
            "as_of": "2026-08-12T10:00:00+08:00", "market": "cn",
            "allowed_symbols": ["600519"],
            "account": {"cash": 100000, "holdings": []},
            "snapshots": {"600519": {"realtime": {"price": 100}, "indicators": {}, "history": []}},
            "screening": {"selected": [{"symbol": "600519"}]},
            "macro_excerpt": "", "optimizer": {}, "market_rules": {},
        },
        {
            "agent_workflow": {
                "enabled": True, "require_citations": True, "minimum_citations": 1,
                "minimum_base_analysts": 2, "research_debate_rounds": 1,
                "risk_debate_rounds": 1, "memory_enabled": True,
                "memory_entries_per_role": 3, "memory_chars_per_entry": 200, "roles": {},
            }
        },
        memory_store=agent_workflow.AgentMemoryStore(tmp_path),
    )

    assert result["evidence_ref"] == ""
    assert not (tmp_path / "evidence").exists()
