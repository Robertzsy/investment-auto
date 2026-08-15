"""Cycle evidence archive: full reports off the hot path, summaries on it.

The audit trail used to carry every intermediate agent report, pushing single
audit files toward 600KB and bloat each downstream prompt.  Now the complete
evidence graph is archived once per cycle under runtime/trading/evidence and
the workflow return value only carries compact summaries; the audit keeps a
reference so the management plane can trace any decision back to its source.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

ROOT = Path(__file__).resolve().parents[2]
from src.paths import runtime_dir
EVIDENCE_DIR = runtime_dir() / "trading" / "evidence"


def sanitize_cycle_id(value: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "cycle").strip()).strip("-")[:80]
    return safe or "cycle"


def evidence_path(cycle_id: str, market: str) -> Path:
    safe_market = re.sub(r"[^a-z0-9_-]+", "-", str(market or "market").strip().lower()) or "market"
    return EVIDENCE_DIR / sanitize_cycle_id(cycle_id) / f"{safe_market}.json"


def save_cycle_evidence(cycle_id: Any, market: str, payload: Mapping[str, Any]) -> str:
    """Atomically archive one cycle's evidence; returns the archive reference."""
    path = evidence_path(sanitize_cycle_id(cycle_id), market)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)
    try:
        return str(path.relative_to(ROOT)).replace(chr(92), "/")
    except ValueError:
        # Redirected evidence dirs (tests) cannot be rooted; absolute works.
        return str(path)


def load_cycle_evidence(reference: str) -> Optional[Dict[str, Any]]:
    """Load an archived evidence graph by its audit reference."""
    # Forward slashes are valid on every platform, so normalize any
    # backslashes (Windows-authored references) instead of the reverse.
    normalized = str(reference or "").replace(chr(92), "/")
    candidates = [ROOT / normalized]
    relative = Path(normalized)
    if "evidence" in relative.parts:
        # Strip a runtime/trading/evidence prefix and re-anchor on the
        # (possibly redirected) evidence directory.
        index = relative.parts.index("evidence")
        candidates.append(EVIDENCE_DIR.joinpath(*relative.parts[index + 1:]))
    candidates.append(EVIDENCE_DIR / relative.name)
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping):
            return payload
    return None


def compact_report(payload: Mapping[str, Any], *, portfolio: bool = False) -> Dict[str, Any]:
    """Reduce one agent report to the fields downstream stages actually need.

    Decisions are kept intact because the risk layer and the broker consume
    them verbatim; long-form reasoning (findings) is trimmed to a few short
    claims with their evidence IDs.  The full text stays in the evidence
    archive.
    """
    if not isinstance(payload, Mapping) or not payload:
        return {}
    keep = (
        "role", "role_name", "stage", "summary", "thesis", "stance", "confidence",
        "data_gaps", "citations", "citation_repairs", "memory_note", "workflow",
    )
    compact: Dict[str, Any] = {key: payload.get(key) for key in keep if key in payload}
    findings = payload.get("findings", [])
    if isinstance(findings, list) and findings and not portfolio:
        trimmed = []
        for finding in findings[:3]:
            if isinstance(finding, Mapping):
                item = dict(finding)
                item["claim"] = str(item.get("claim", ""))[:80]
                item["reason"] = str(item.get("reason", ""))[:120]
                refs = item.get("evidence_ids")
                item["evidence_ids"] = refs[:6] if isinstance(refs, list) else refs
                trimmed.append(item)
        if trimmed:
            compact["findings"] = trimmed
    decisions = payload.get("decisions")
    if isinstance(decisions, list) and decisions:
        compact["decisions"] = [
            dict(item) for item in decisions if isinstance(item, Mapping)
        ]
    gaps = compact.get("data_gaps")
    if isinstance(gaps, list):
        compact["data_gaps"] = gaps[:5]
    note = compact.get("memory_note")
    if isinstance(note, str):
        compact["memory_note"] = note[:160]
    return compact
