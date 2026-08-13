from __future__ import annotations

import json

from src.investment import status


def test_cycle_evidence_distinguishes_trigger_from_execution_failure(monkeypatch, tmp_path):
    reports = tmp_path / "reports"
    audits = tmp_path / "audits"
    reports.mkdir()
    audits.mkdir()
    (reports / "20260813-cn-1300.md").write_text("report", encoding="utf-8")
    (audits / "20260813-130142-000000-cn-1300.json").write_text(json.dumps({
        "status": "error", "error": "risk manager failed", "execution": {"fills": []},
    }), encoding="utf-8")
    monkeypatch.setattr(status, "REPORT_DIR", reports)
    monkeypatch.setattr(status, "AUDIT_DIR", audits)

    result = status.cycle_evidence("cn", "2026-08-13", "1300")
    assert result["triggered"] is True
    assert result["investment_status"] == "error"
    assert result["error"] == "risk manager failed"
