"""Cross-platform helpers for decoding captured subprocess output."""
from __future__ import annotations

import locale
from typing import Optional, Union


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
