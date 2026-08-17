"""Cross-platform helpers for decoding captured subprocess output."""
from __future__ import annotations

import locale
import os
import subprocess
from typing import Any, Dict, Optional, Union


_IS_WINDOWS = os.name == "nt"


def hidden_subprocess_kwargs() -> Dict[str, Any]:
    """Return flags that stop console children from flashing on Windows.

    The desktop services run through ``pythonw``.  Without this flag, every
    Node.js quote request creates a visible console; concurrent US screening
    therefore produces a burst of windows.  Non-Windows callers receive no
    additional keyword arguments.
    """

    if not _IS_WINDOWS:
        return {}
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
    }


def decode_subprocess_output(value: Optional[Union[bytes, str]]) -> str:
    """Decode child-process output without trusting the Windows ANSI code page.

    Node and Python CLIs normally emit UTF-8 even when the parent Python process
    uses a GBK locale. ``subprocess.run(text=True)`` delegates decoding to that
    locale and can therefore crash its background reader thread. Capture bytes
    instead and try UTF-8, the active locale, then GB18030.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value

    encodings = ["utf-8", locale.getpreferredencoding(False), "gb18030"]
    seen = set()
    for encoding in encodings:
        normalized = (encoding or "").lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")
