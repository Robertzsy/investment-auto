from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


def _parse_json_object(text: str) -> Dict[str, Any]:
    cleaned = str(text or "").strip()
    fence = chr(96) * 3
    if cleaned.startswith(fence):
        cleaned = re.sub(r"^[A-Za-z]*\s*", "", cleaned[len(fence):], count=1)
        if cleaned.endswith(fence):
            cleaned = cleaned[:-len(fence)].rstrip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Agent 没有返回 JSON 对象")
        payload = json.loads(cleaned[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Agent 输出必须是 JSON 对象")
    return payload


def _citation_ids(payload: Mapping[str, Any]) -> List[str]:
    values: List[Any] = list(payload.get("citations", [])) if isinstance(payload.get("citations"), list) else []
    findings = payload.get("findings", [])
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, Mapping) and isinstance(finding.get("evidence_ids"), list):
                values.extend(finding["evidence_ids"])
    decisions = payload.get("decisions", [])
    if isinstance(decisions, list):
        for decision in decisions:
            if isinstance(decision, Mapping) and isinstance(decision.get("evidence_ids"), list):
                values.extend(decision["evidence_ids"])
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def validate_citations(
    payload: Mapping[str, Any],
    allowed_ids: Iterable[str],
    *,
    required: bool,
    minimum: int,
    require_decision_citations: bool = False,
    required_upstream_prefixes: Sequence[str] = (),
    require_all_upstream_prefixes: bool = False,
) -> List[str]:
    allowed = set(allowed_ids)
    citations = _citation_ids(payload)
    unknown = [item for item in citations if item not in allowed]
    if unknown:
        raise ValueError("引用了不存在的证据 ID: " + ", ".join(unknown[:8]))
    findings = payload.get("findings", [])
    if required and not require_decision_citations and (not isinstance(findings, list) or not findings):
        raise ValueError("必须输出至少一条带引用的 finding")
    if required and isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, Mapping) and str(finding.get("claim", "")).strip():
                refs = finding.get("evidence_ids", [])
                if not isinstance(refs, list) or not refs:
                    raise ValueError("每条 finding 必须包含 evidence_ids")
    if required and require_decision_citations:
        decisions = payload.get("decisions", [])
        for decision in decisions if isinstance(decisions, list) else []:
            if isinstance(decision, Mapping):
                refs = decision.get("evidence_ids", [])
                if not isinstance(refs, list) or not refs:
                    raise ValueError("每条组合决策必须包含 evidence_ids")
    if required and len(citations) < max(1, minimum):
        raise ValueError(f"有效引用不足 {max(1, minimum)} 条")
    available_prefixes = [
        prefix for prefix in required_upstream_prefixes
        if any(evidence_id.startswith(prefix) for evidence_id in allowed)
    ]
    if required and available_prefixes:
        cited_prefixes = {
            prefix for prefix in available_prefixes
            if any(citation.startswith(prefix) for citation in citations)
        }
        if require_all_upstream_prefixes and len(cited_prefixes) != len(available_prefixes):
            missing = [prefix for prefix in available_prefixes if prefix not in cited_prefixes]
            raise ValueError("必须引用全部直接上游 Agent 报告: " + ", ".join(missing))
        if not cited_prefixes:
            raise ValueError("必须引用至少一份直接上游 Agent 报告")
    return citations


def repair_citations(
    payload: Mapping[str, Any],
    allowed_ids: Iterable[str],
    *,
    required_upstream_prefixes: Sequence[str] = (),
    require_all_upstream_prefixes: bool = False,
    attach_missing_upstream: bool = True,
) -> tuple[Dict[str, Any], List[str]]:
    """Normalize citation IDs and attach missing mandatory upstream citations.

    Most agent failures are citation-format mistakes, not reasoning failures:
    the model references upstream reports by role name, drops the round
    suffix, or forgets to put them in the top-level citations block.  Instead
    of burning a full regeneration, repair the payload when the fix is
    unambiguous:

    * exact catalog ID stays untouched;
    * a bare report ID gains the missing round suffix (or loses a wrong one);
    * a bare role name such as NEWS_ANALYST maps to AGENT:NEWS_ANALYST;
    * mandatory upstream IDs the model forgot are attached to top-level
      citations -- only system-supplied upstream Agent IDs, never raw facts.

    Returns the repaired payload and repair notes.  An unrecoverable payload
    is returned untouched with empty notes so the caller retries as before.
    """
    allowed = {str(item).strip() for item in allowed_ids}
    agent_allowed = sorted(
        candidate for candidate in allowed if str(candidate).startswith("AGENT:")
    )
    repairs: List[str] = []

    def _normalize(raw: str) -> Optional[str]:
        value = raw.strip().upper()
        if not value or value in allowed:
            return None
        # Strip a wrong round suffix: AGENT:BULL_RESEARCHER:R1 when the
        # catalog only carries the bare report ID.
        if ":" in value:
            bare = value.rsplit(":", 1)[0]
            if bare in allowed:
                return bare
        # Attach the missing round suffix when exactly one candidate matches.
        suffixed = sorted(
            candidate for candidate in agent_allowed if candidate.startswith(value + ":")
        )
        if len(suffixed) == 1:
            return suffixed[0]
        if len(suffixed) > 1:
            return None  # ambiguous
        # Model wrote a bare role name (NEWS_ANALYST) instead of an ID.
        prefixed = sorted(
            candidate for candidate in agent_allowed if candidate == "AGENT:" + value
        )
        if len(prefixed) == 1:
            return prefixed[0]
        prefixed_round = sorted(
            candidate for candidate in agent_allowed if candidate.startswith("AGENT:" + value + ":")
        )
        if len(prefixed_round) == 1:
            return prefixed_round[0]
        return None

    def _replace(container: Any, path: str) -> None:
        if not isinstance(container, list):
            return
        for index, item in enumerate(container):
            if not isinstance(item, str):
                continue
            fixed = _normalize(str(item))
            if fixed:
                container[index] = fixed
                repairs.append(path + "[" + str(index) + "]: " + item.strip() + " -> " + fixed)

    repaired = copy.deepcopy(dict(payload))
    if isinstance(repaired.get("citations"), list):
        _replace(repaired["citations"], "citations")
    findings = repaired.get("findings", [])
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, dict):
                _replace(finding.get("evidence_ids"), "findings.evidence_ids")
    decisions = repaired.get("decisions", [])
    if isinstance(decisions, list):
        for decision in decisions:
            if isinstance(decision, dict):
                _replace(decision.get("evidence_ids"), "decisions.evidence_ids")

    available_prefixes = [
        prefix for prefix in required_upstream_prefixes
        if any(evidence_id.startswith(prefix) for evidence_id in allowed)
    ]
    if available_prefixes:
        citations = (
            list(repaired["citations"]) if isinstance(repaired.get("citations"), list) else []
        )
        cited = [str(item) for item in citations]
        cited_prefixes = {
            prefix for prefix in available_prefixes
            if any(citation.startswith(prefix) for citation in cited)
        }
        missing = [prefix for prefix in available_prefixes if prefix not in cited_prefixes]
        if attach_missing_upstream and missing and (require_all_upstream_prefixes or not cited_prefixes):
            targets = missing if require_all_upstream_prefixes else missing[:1]
            for prefix in targets:
                exact = sorted(
                    evidence_id for evidence_id in allowed if evidence_id.startswith(prefix)
                )
                if not exact:
                    continue
                repaired["citations"] = [*citations, exact[0]]
                repairs.append("citations: auto_attached " + exact[0])
    return repaired, repairs


