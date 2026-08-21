from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict

from engine.subprocess_utils import decode_subprocess_output, hidden_subprocess_kwargs


ROOT = Path(__file__).resolve().parents[2]


def stock_fetcher(command: str, value: str, option: str = "") -> Dict[str, Any]:
    """Call the bundled market-data adapter without depending on the UI."""
    script = ROOT / "scripts" / "stock-fetcher.js"
    try:
        argv = ["node", str(script), command, str(value)]
        if str(option).strip():
            argv.append(str(option))
        process = subprocess.run(
            argv,
            cwd=str(ROOT),
            capture_output=True,
            timeout=45,
            **hidden_subprocess_kwargs(),
        )
        text = decode_subprocess_output(process.stdout).strip()
        stderr = decode_subprocess_output(process.stderr)
        try:
            data = json.loads(text) if text else {}
        except Exception:
            data = {"stdout": text[:8000]}
        if process.returncode != 0:
            data["stderr"] = stderr[:4000]
            data["returncode"] = process.returncode
        return data
    except Exception as exc:
        return {"error": str(exc)}

