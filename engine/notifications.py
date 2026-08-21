from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlparse

from engine.config import cfg


def _webhook_url(settings: Mapping[str, Any]) -> str:
    env_name = str(settings.get("webhook_url_env", "NOTIFY_WEBHOOK_URL")).strip()
    return os.getenv(env_name, "").strip() if env_name else ""


def deliver_report(
    *,
    title: str,
    content: str,
    report_path: Optional[Path] = None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Deliver one completed report without changing the completed trade."""
    settings = cfg.notify
    if not bool(settings.get("enabled", False)):
        return {"status": "disabled"}
    channels = [str(value).lower() for value in settings.get("channels", [])]
    if "webhook" not in channels:
        return {"status": "skipped", "reason": "没有启用 webhook 通知渠道"}
    url = _webhook_url(settings)
    if not url:
        return {"status": "skipped", "reason": "未配置通知 webhook 地址"}
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"status": "error", "error": "通知 webhook 地址无效"}

    payload = {
        "title": title,
        "text": content,
        "content": content,
        "report_file": report_path.name if report_path else None,
        "metadata": dict(metadata or {}),
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    timeout = max(1.0, min(30.0, float(settings.get("timeout_seconds", 10))))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status_code = int(getattr(response, "status", 200))
        if 200 <= status_code < 300:
            return {"status": "delivered", "channel": "webhook", "status_code": status_code}
        return {"status": "error", "channel": "webhook", "status_code": status_code}
    except urllib.error.HTTPError as exc:
        return {"status": "error", "channel": "webhook", "status_code": int(exc.code)}
    except Exception as exc:
        return {"status": "error", "channel": "webhook", "error": str(exc)[:500]}
