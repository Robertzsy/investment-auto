from __future__ import annotations

import json
import sys


_MARKER = "IA_REPAIR_REPLAY="


def main() -> int:
    if len(sys.argv) != 2:
        print(_MARKER + json.dumps({"status": "error", "error": "execution_id required"}))
        return 2
    try:
        from src.manager.incident_repair import IncidentRepairSupervisor

        result = IncidentRepairSupervisor().replay(sys.argv[1])
        print(_MARKER + json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(_MARKER + json.dumps({
            "status": "error",
            "semantic_validation_passed": False,
            "error": str(exc)[:4000],
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
