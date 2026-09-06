"""One-shot 2.0 migration step 1: rename the `src` package to `engine`.

Reads and writes every touched file as UTF-8 (no BOM). Import references are
rewritten (`from engine.` -> `from engine.`, `import engine.` -> `import engine.`,
`-m src.` strings, quoted 'engine.'/'src/' path literals) — nothing else changes.
Run once from the repo root with the venv interpreter.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".venv", "dist", "build", "release", "runtime", "node_modules", ".git"}

IMPORT_RE = re.compile(r"\b(from\s+)src(\.|\s)")
IMPORT2_RE = re.compile(r"\b(import\s+)src(\.|\s)")
MODM_RE = re.compile(r'("src\.)')
QUOTED_DOT_RE = re.compile(r"(['\"])src\.")


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def main() -> None:
    src = ROOT / "src"
    eng = ROOT / "engine"
    if eng.exists():
        raise SystemExit("engine/ already exists — aborting")
    shutil.move(str(src), str(eng))

    rewritten = 0
    for path in eng.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        new = IMPORT_RE.sub(r"\1engine\2", text)
        new = IMPORT2_RE.sub(r"\1engine\2", new)
        new = MODM_RE.sub(r'"engine.', new)
        new = QUOTED_DOT_RE.sub(r"\1engine.", new)
        if new != text:
            path.write_text(new, encoding="utf-8", newline="\n")
            rewritten += 1
    print(f"engine/: rewrote {rewritten} files")

    for path in (ROOT / "tests").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        new = IMPORT_RE.sub(r"\1engine\2", text)
        new = IMPORT2_RE.sub(r"\1engine\2", new)
        new = MODM_RE.sub(r'"engine.', new)
        new = QUOTED_DOT_RE.sub(r"\1engine.", new)
        if new != text:
            path.write_text(new, encoding="utf-8", newline="\n")
            rewritten += 1
    for path in (ROOT / "scripts").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        new = IMPORT_RE.sub(r"\1engine\2", text)
        new = IMPORT2_RE.sub(r"\1engine\2", new)
        new = MODM_RE.sub(r'"engine.', new)
        new = QUOTED_DOT_RE.sub(r"\1engine.", new)
        if new != text:
            path.write_text(new, encoding="utf-8", newline="\n")
            rewritten += 1
    print(f"total rewritten: {rewritten}")


if __name__ == "__main__":
    main()
