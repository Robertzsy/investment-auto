from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.trading import agent_workflow


def _context() -> dict:
    return {
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


def _config() -> dict:
    return {
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


def test_evidence_catalog_has_stable_fact_ids():
    evidence = agent_workflow.build_evidence_catalog(_context())
    assert evidence["MARKET:600519"]["price"] == 100
    assert evidence["TECHNICAL:600519"]["rsi"]["rsi14"] == 55
    assert "MACRO:LATEST" in evidence
    assert "OPTIMIZER:LATEST" in evidence
    assert "NEWS:MACRO" in evidence
    assert "FUNDAMENTALS:600519" in evidence
    assert "SENTIMENT:600519" in evidence


def test_citation_validator_rejects_unknown_and_missing_references():
    with pytest.raises(ValueError, match="不存在"):
        agent_workflow.validate_citations(
            {"findings": [{"claim": "上涨", "evidence_ids": ["FAKE:1"]}]},
            ["MARKET:600519"],
            required=True,
            minimum=1,
        )
    with pytest.raises(ValueError, match="evidence_ids"):
        agent_workflow.validate_citations(
            {"findings": [{"claim": "上涨"}]},
            ["MARKET:600519"],
            required=True,
            minimum=1,
        )


def test_memory_is_isolated_by_market_and_role(tmp_path: Path):
    store = agent_workflow.AgentMemoryStore(tmp_path)
    store.append(
        "cn", "bull_researcher", generated_at="2026-08-12T10:00:00+08:00",
        situation="测试", memory_note="多头记忆", citations=["MARKET:600519"], limit=3, max_chars=100,
    )
    assert store.load("cn", "bull_researcher", 3)[0]["memory_note"] == "多头记忆"
    assert store.load("cn", "bear_researcher", 3) == []
    assert store.load("us", "bull_researcher", 3) == []


def test_base_roles_only_receive_their_evidence_domain():
    evidence = agent_workflow.build_evidence_catalog(_context())
    technical = agent_workflow._role_evidence("technical_analyst", evidence)
    news = agent_workflow._role_evidence("news_analyst", evidence)
    fundamentals = agent_workflow._role_evidence("fundamentals_analyst", evidence)

    assert "TECHNICAL:600519" in technical
    assert "MACRO:LATEST" not in technical
    assert "NEWS:MACRO" in news
    assert "TECHNICAL:600519" not in news
    assert "FUNDAMENTALS:600519" in fundamentals


def test_manager_must_cite_direct_upstream_report():
    evidence = {
        "MARKET:600519": {"price": 100},
        "AGENT:BULL_RESEARCHER:R1": {"summary": "bull"},
    }
    with pytest.raises(ValueError, match="上游"):
        agent_workflow.validate_citations(
            {"findings": [{"claim": "裁决", "evidence_ids": ["MARKET:600519"]}]},
            evidence,
            required=True,
            minimum=1,
            required_upstream_prefixes=("AGENT:BULL_RESEARCHER",),
        )


def test_portfolio_decision_must_cover_every_candidate_and_holding():
    with pytest.raises(ValueError, match="600519"):
        agent_workflow.validate_portfolio_coverage(
            {"decisions": [{"symbol": "000001", "action": "HOLD"}]},
            ["000001", "600519"],
        )
    agent_workflow.validate_portfolio_coverage(
        {"decisions": [
            {"symbol": "000001", "action": "BUY"},
            {"symbol": "600519", "action": "SELL"},
        ]},
        ["000001", "600519"],
    )


def test_staged_workflow_orders_roles_and_builds_portfolio(monkeypatch, tmp_path):
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
            "findings": [{"claim": "test", "evidence_ids": [citation]}],
            "citations": [citation],
        }

    monkeypatch.setattr(agent_workflow, "_call_role", fake_call)
    monkeypatch.setattr(
        agent_workflow,
        "_parallel_roles",
        lambda roles, **kwargs: ({role: fake_call(role, **kwargs) for role in roles}, {}),
    )
    result = agent_workflow.run_analysis_workflow(
        _context(), _config(), memory_store=agent_workflow.AgentMemoryStore(tmp_path)
    )

    assert result["workflow"] == "tradingagents_staged_v1"
    assert result["portfolio_manager"]["decisions"][0]["symbol"] == "600519"
    research_manager_call = next(item for item in calls if item[0] == "research_manager")
    assert "AGENT:BULL_RESEARCHER:R1" in research_manager_call[1]
    portfolio_call = next(item for item in calls if item[0] == "portfolio_manager")
    assert "AGENT:RISK_MANAGER" in portfolio_call[1]


def test_call_role_retries_truncated_json_and_persists_own_memory(monkeypatch, tmp_path):
    responses = iter([
        '{"summary":"broken",',
        json.dumps({
            "summary": "ok",
            "findings": [{"claim": "价格有效", "impact": "neutral", "evidence_ids": ["MARKET:600519"]}],
            "stance": "HOLD",
            "confidence": 0.6,
            "data_gaps": [],
            "citations": ["MARKET:600519"],
            "memory_note": "下一轮继续核验价格",
        }, ensure_ascii=False),
    ])

    calls = []

    class LLM:
        provider_name = "deepseek"

        def chat(self, messages, **kwargs):
            calls.append(kwargs)
            return next(responses)

    monkeypatch.setattr(agent_workflow, "resolve_llm", lambda role: LLM())
    store = agent_workflow.AgentMemoryStore(tmp_path)
    result = agent_workflow._call_role(
        "technical_analyst",
        stage="base_analysis",
        context=_context(),
        evidence={"MARKET:600519": {"price": 100}},
        settings={**_config()["agent_workflow"], "json_retries": 1},
        memory_store=store,
        generated_at="2026-08-12T10:00:00+08:00",
    )

    assert result["summary"] == "ok"
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["max_tokens"] == 10000
    assert calls[1]["max_tokens"] == 10000
    assert calls[1]["temperature"] == 0
    assert store.load("cn", "technical_analyst", 3)[0]["memory_note"] == "下一轮继续核验价格"


def test_invalid_agent_output_is_saved_for_diagnosis(monkeypatch, tmp_path):
    class LLM:
        provider_name = "deepseek"

        def chat(self, messages, **kwargs):
            return '{"summary":"broken"'

    monkeypatch.setattr(agent_workflow, "resolve_llm", lambda role: LLM())
    monkeypatch.setattr(agent_workflow, "AGENT_FAILURE_DIR", tmp_path / "failures")

    with pytest.raises(RuntimeError, match="原始输出诊断"):
        agent_workflow._call_role(
            "research_manager",
            stage="research_judgement",
            context=_context(),
            evidence={"AGENT:BULL_RESEARCHER:R1": {"summary": "bull"}},
            settings={**_config()["agent_workflow"], "json_retries": 1},
            memory_store=agent_workflow.AgentMemoryStore(tmp_path / "memory"),
            generated_at="2026-08-13T02:00:00+08:00",
        )

    diagnostics = list((tmp_path / "failures").glob("*.json"))
    assert len(diagnostics) == 2
    saved = json.loads(diagnostics[-1].read_text(encoding="utf-8"))
    assert saved["role"] == "research_manager"
    assert saved["output_chars"] == len('{"summary":"broken"')
