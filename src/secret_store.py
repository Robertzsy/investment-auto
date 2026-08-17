"""Windows DPAPI-backed secret storage for API keys.

Secrets live encrypted in the user data root (secrets.enc), protected with
CryptProtectData - tied to the current Windows user, so a per-user install
is a hard requirement for the desktop app.  Secrets never touch the
install directory, plain-text .env or logs.

On non-Windows platforms the store degrades safely: loading returns None
and saving raises NotImplementedError.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from ctypes import wintypes

from src import paths

_store_lock = threading.RLock()


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _is_windows() -> bool:
    return os.name == "nt" and hasattr(ctypes, "windll")


def _blob_from_bytes(data: bytes) -> _DATA_BLOB:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _bytes_from_blob(blob: _DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def dpapi_protect(data: bytes) -> bytes:
    if not _is_windows():
        raise NotImplementedError("DPAPI 仅适用于 Windows")
    blob_in = _blob_from_bytes(data)
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), "investment-auto", None, None, None, 0, ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptProtectData 失败")
    return _bytes_from_blob(blob_out)


def dpapi_unprotect(data: bytes) -> bytes:
    if not _is_windows():
        raise NotImplementedError("DPAPI 仅适用于 Windows")
    blob_in = _blob_from_bytes(data)
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptUnprotectData 失败")
    return _bytes_from_blob(blob_out)


def store_path() -> Path:
    return paths.data_root() / "secrets.enc"


def _read_store() -> Dict[str, Any]:
    path = store_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_store(payload: Dict[str, Any]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".enc.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def save_secret(key: str, value: str) -> None:
    """Encrypt and persist one secret under a key (e.g. DEEPSEEK_API_KEY)."""
    name = str(key or "").strip()
    if not name:
        raise ValueError("key 不能为空")
    with _store_lock:
        payload = _read_store()
        if str(value or "").strip():
            payload[name] = base64.b64encode(dpapi_protect(value.encode("utf-8"))).decode("ascii")
        else:
            payload.pop(name, None)
        _write_store(payload)


def load_secret(key: str) -> Optional[str]:
    """Decrypt one stored secret, or None when absent/undecryptable."""
    name = str(key or "").strip()
    if not name or not _is_windows():
        return None
    with _store_lock:
        payload = _read_store()
        encoded = payload.get(name)
    if not encoded:
        return None
    try:
        return dpapi_unprotect(base64.b64decode(encoded)).decode("utf-8")
    except Exception:
        return None


def list_keys() -> list[str]:
    with _store_lock:
        return sorted(_read_store().keys())


def delete_secret(key: str) -> None:
    with _store_lock:
        payload = _read_store()
        payload.pop(str(key or "").strip(), None)
        _write_store(payload)


_KEY_SHAPE_RE = re.compile(r"(?<![A-Za-z0-9])(?:sk|key)-[A-Za-z0-9_-]{12,}", re.I)
_LABELLED_SECRET_RE = re.compile(
    r"(?i)((?:api[_ -]?key|token|secret|password|passwd)\s*[:=：]?\s*)([^\s,;，；]{12,})"
)


def redact_text(value: Any) -> str:
    """Remove configured and key-shaped secrets from user-visible text."""
    text = str(value or "")
    names = set(list_keys())
    try:
        from src.config import cfg

        names.update(
            str(item.get("api_key_env", "")).strip()
            for item in cfg.raw.get("llm", {}).get("models", {}).values()
            if str(item.get("api_key_env", "")).strip()
        )
    except Exception:
        pass
    for name in names:
        secret = os.getenv(name, "") or load_secret(name) or ""
        if len(secret) >= 8:
            text = text.replace(secret, "********")
    text = _KEY_SHAPE_RE.sub("********", text)
    return _LABELLED_SECRET_RE.sub(lambda match: match.group(1) + "********", text)


def redact_mapping(value: Any) -> Any:
    """Recursively redact tool arguments without changing their structure."""
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            if any(marker in str(key).upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")):
                result[str(key)] = "********" if item else item
            else:
                result[str(key)] = redact_mapping(item)
        return result
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_mapping(item) for item in value)
    return redact_text(value) if isinstance(value, str) else value
