from __future__ import annotations

from typing import Any, Dict, Mapping, Sequence


def _path(value: Mapping[str, Any], dotted: str) -> Any:
    current: Any = value
    for part in str(dotted).split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _present(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _gate_passed(actual: Any, gate: Mapping[str, Any]) -> bool:
    operator = str(gate.get("operator", "equals")).strip().lower()
    expected = gate.get("value", True)
    if operator in {"equals", "eq"}:
        return actual == expected
    if operator in {"not_equals", "ne"}:
        return actual != expected
    if operator in {"min", "gte"}:
        try:
            return float(actual) >= float(expected)
        except (TypeError, ValueError):
            return False
    if operator in {"max", "lte"}:
        try:
            return float(actual) <= float(expected)
        except (TypeError, ValueError):
            return False
    if operator == "one_of":
        values = gate.get("values", [])
        return actual in values if isinstance(values, Sequence) and not isinstance(values, str) else False
    if operator == "present":
        return _present(actual)
    if operator == "min_count":
        try:
            return len(actual) >= int(expected)
        except (TypeError, ValueError):
            return False
    return False


class CompletionValidator:
    def validate(
        self,
        contract: Mapping[str, Any],
        *,
        outputs: Mapping[str, Any],
        step_statuses: Mapping[str, str],
    ) -> Dict[str, Any]:
        required_steps: Sequence[str] = contract.get("required_steps", [])
        required_outputs: Sequence[str] = contract.get("required_outputs", [])
        quality_gates = [
            item for item in contract.get("quality_gates", []) if isinstance(item, Mapping)
        ]
        missing_steps = [step for step in required_steps if step_statuses.get(str(step)) != "completed"]
        missing_outputs = [item for item in required_outputs if not _present(_path(outputs, str(item)))]
        failed_steps = [key for key, status in step_statuses.items() if status == "failed"]
        gate_results = []
        for gate in quality_gates:
            dotted = str(gate.get("path", "")).strip()
            actual = _path(outputs, dotted) if dotted else None
            passed = bool(dotted) and _gate_passed(actual, gate)
            gate_results.append({
                "path": dotted,
                "operator": str(gate.get("operator", "equals")),
                "expected": gate.get("value", True),
                "actual": actual,
                "passed": passed,
                "severity": str(gate.get("severity", "error")),
                "message": str(gate.get("message", "")),
            })
        structural_passed = not missing_steps and not missing_outputs
        blocking_gate_failures = [
            item for item in gate_results if not item["passed"] and item["severity"] != "warning"
        ]
        warning_gate_failures = [
            item for item in gate_results if not item["passed"] and item["severity"] == "warning"
        ]
        quality_passed = not blocking_gate_failures
        passed = structural_passed and quality_passed and not warning_gate_failures
        return {
            "passed": passed,
            "structural_passed": structural_passed,
            "quality_passed": quality_passed and not warning_gate_failures,
            "degraded": structural_passed and bool(blocking_gate_failures or warning_gate_failures),
            "missing_steps": missing_steps,
            "missing_outputs": missing_outputs,
            "failed_steps": failed_steps,
            "quality_gates": gate_results,
            "failed_quality_gates": [item["path"] for item in gate_results if not item["passed"]],
            "checked_required_steps": list(required_steps),
            "checked_required_outputs": list(required_outputs),
        }
