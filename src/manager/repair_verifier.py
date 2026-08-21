from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from typing import Callable


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _probe_identity() -> None:
    from src.manager.action_registry import ActionRegistry
    from src.manager.security_identity import SecurityIdentity, identity_matches, normalize_provider_text
    from src.platform import market_tools

    row = {
        "code": "000002",
        "symbol": "sh000002",
        "name": r"A\u80a1\u6307\u6570",
        "market": "沪市",
        "type": "ZS",
    }
    identity = SecurityIdentity.from_search_row(row).to_dict()
    _require(identity["provider_symbol"] == "sh000002", "provider symbol lost exchange")
    _require(identity["canonical_symbol"] == "SH:000002", "canonical identity is ambiguous")
    _require(identity["asset_type"] == "index", "index was classified as an equity")
    _require(normalize_provider_text(row["name"]) == "A股指数", "escaped provider name not decoded")
    _require(
        not identity_matches(identity, {"realtime": {"code": "sz000002"}}),
        "cross-exchange quote mismatch was accepted",
    )

    calls: list[tuple[str, str]] = []
    original = market_tools.stock_fetcher

    def fake_fetcher(command: str, value: str, option: str = ""):
        calls.append((command, value))
        if command == "search":
            return {"results": [row]}
        return {
            "realtime": {
                "code": "sz000002",
                "time": datetime.now().strftime("%Y%m%d%H%M%S"),
                "name": "万科A",
                "price": 3.14,
            }
        }

    market_tools.stock_fetcher = fake_fetcher
    try:
        registry = ActionRegistry(include_dynamic=False)
        resolved = registry.execute(
            "security.resolve",
            {"request": "分析A股指数"},
            {"state": {"outputs": {}}},
            session_scope="investment_research",
        )
        state = {"outputs": {"resolve": resolved}}
        try:
            registry.execute(
                "security.snapshot",
                {"request": "分析A股指数"},
                {"state": state},
                session_scope="investment_research",
            )
        except RuntimeError as exc:
            _require("身份不一致" in str(exc), "identity mismatch raised the wrong failure")
        else:
            raise AssertionError("sh000002 -> sz000002 reached the report pipeline")
        _require(calls[-1] == ("snapshot", "sh000002"), "typed provider symbol was not used")
    finally:
        market_tools.stock_fetcher = original


def _probe_freshness() -> None:
    from src.manager.security_identity import is_fresh

    today = date.today()
    current = {"realtime": {"time": today.strftime("%Y%m%d") + "150000"}}
    stale_day = today - timedelta(days=10)
    stale = {"realtime": {"time": stale_day.strftime("%Y%m%d") + "150000"}}
    _require(is_fresh(current, today=today), "current quote was rejected as stale")
    _require(not is_fresh(stale, today=today), "stale quote passed the freshness gate")
    _require(not is_fresh({"realtime": {}}, today=today), "undated quote passed freshness")


def _probe_contract() -> None:
    from src.manager.completion_validator import CompletionValidator

    contract = {
        "required_steps": ["quality", "report"],
        "required_outputs": ["report.user_report"],
        "quality_gates": [
            {"path": "quality.identity_consistent", "operator": "equals", "value": True},
            {"path": "quality.coverage_ratio", "operator": "min", "value": 0.8},
        ],
    }
    validator = CompletionValidator()
    failed = validator.validate(
        contract,
        outputs={
            "quality": {"identity_consistent": False, "coverage_ratio": 1.0},
            "report": {"user_report": "non-empty but wrong"},
        },
        step_statuses={"quality": "completed", "report": "completed"},
    )
    _require(not failed["passed"] and failed["degraded"], "semantic failure was marked completed")
    passed = validator.validate(
        contract,
        outputs={
            "quality": {"identity_consistent": True, "coverage_ratio": 0.8},
            "report": {"user_report": "verified"},
        },
        step_statuses={"quality": "completed", "report": "completed"},
    )
    _require(passed["passed"], "valid semantic completion contract did not pass")


def _probe_workflow() -> None:
    from src.manager.action_registry import ActionRegistry
    from src.manager.skill_registry import SkillRegistry
    from src.manager.skill_runtime import SkillRuntime
    from src.manager.skill_selector import SkillSelector

    registry = SkillRegistry()
    names = {package.manifest.name for package in registry.catalog()}
    _require({"market-overview", "security-analysis", "incident-repair"} <= names, "core Skills missing")
    selector = SkillSelector(registry)
    _require(selector.select("今天的A股行情怎么样").manifest.name == "market-overview", "broad market route regressed")
    _require(selector.select("修复数据缺口").manifest.name == "incident-repair", "repair route regressed")
    runtime = SkillRuntime(registry=registry, actions=ActionRegistry(include_dynamic=False))
    for package in registry.catalog():
        runtime._validate_permissions(package)


_PROBES: dict[str, tuple[Callable[[], None], ...]] = {
    "identity_mismatch": (_probe_identity, _probe_contract, _probe_workflow),
    "stale_data": (_probe_freshness, _probe_contract, _probe_workflow),
    "data_gap": (_probe_contract, _probe_workflow),
    "contract_failure": (_probe_contract, _probe_workflow),
    "workflow_failure": (_probe_identity, _probe_freshness, _probe_contract, _probe_workflow),
}


def verify(category: str) -> dict[str, object]:
    normalized = str(category or "workflow_failure").strip().lower()
    probes = _PROBES.get(normalized)
    if probes is None:
        raise ValueError(f"unsupported repair category: {normalized}")
    completed = []
    for probe in probes:
        probe()
        completed.append(probe.__name__)
    return {"status": "passed", "category": normalized, "probes": completed}


def main() -> int:
    category = sys.argv[1] if len(sys.argv) > 1 else "workflow_failure"
    try:
        result = verify(category)
    except Exception as exc:
        print(json.dumps({"status": "failed", "category": category, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
