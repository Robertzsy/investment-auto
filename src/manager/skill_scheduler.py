from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from src.config import cfg
from src.manager.skill_registry import SkillRegistry
from src.manager.skill_runtime import SkillRuntime
from src.paths import runtime_dir


SCHEDULE_DIR = runtime_dir() / "manager" / "skill_schedules"
_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_lock = threading.RLock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


class SkillScheduler:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = (root or SCHEDULE_DIR).resolve()

    def create(
        self,
        *,
        skill_name: str,
        cron: str,
        inputs: Mapping[str, Any],
        schedule_id: str = "",
        timezone: str = "",
        enabled: bool = True,
    ) -> Dict[str, Any]:
        from src.secret_store import redact_mapping

        package = SkillRegistry().get(skill_name)
        if package.manifest.session_scope == "system_admin" or package.manifest.side_effect_level == "system_admin":
            raise PermissionError("自动执行不允许 system_admin Skill；系统修改必须由用户在前台明确触发")
        normalized_id = str(schedule_id or f"{skill_name}-{uuid.uuid4().hex[:8]}").strip().lower()
        if not _ID.fullmatch(normalized_id):
            raise ValueError("schedule_id 必须是 2-64 位小写字母、数字或连字符")
        zone = str(timezone or cfg.schedule.get("timezone", "Asia/Shanghai"))
        ZoneInfo(zone)
        CronTrigger.from_crontab(str(cron), timezone=zone)
        safe_inputs = redact_mapping(dict(inputs))
        if safe_inputs != dict(inputs):
            raise ValueError("Skill 定时任务 inputs 不得保存密钥原文")
        payload = {
            "schedule_id": normalized_id,
            "skill_name": package.manifest.name,
            "skill_version": package.manifest.version,
            "cron": str(cron),
            "timezone": zone,
            "inputs": safe_inputs,
            "enabled": bool(enabled),
            "created_at": _now(),
            "updated_at": _now(),
        }
        with _lock:
            path = self.root / f"{normalized_id}.json"
            if path.exists():
                raise FileExistsError(f"Skill 定时任务已存在: {normalized_id}")
            _atomic_json(path, payload)
        try:
            from src.scheduler import refresh_skill_schedules

            refresh_skill_schedules()
        except Exception:
            pass
        return {"status": "scheduled", **payload}

    def list(self, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
        rows = []
        if not self.root.exists():
            return rows
        for path in sorted(self.root.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and (not enabled_only or value.get("enabled", True)):
                    rows.append(value)
            except (OSError, json.JSONDecodeError):
                continue
        return rows

    def get(self, schedule_id: str) -> Dict[str, Any]:
        normalized = str(schedule_id or "").strip().lower()
        if not _ID.fullmatch(normalized):
            raise ValueError("schedule_id 无效")
        path = self.root / f"{normalized}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise FileNotFoundError(f"Skill 定时任务不存在: {normalized}") from exc
        if not isinstance(value, dict):
            raise ValueError("Skill 定时任务文件无效")
        return value

    @staticmethod
    def _refresh_live_scheduler() -> None:
        try:
            from src.scheduler import refresh_skill_schedules

            refresh_skill_schedules()
        except Exception:
            pass

    def set_enabled(self, schedule_id: str, enabled: bool) -> Dict[str, Any]:
        with _lock:
            payload = self.get(schedule_id)
            payload["enabled"] = bool(enabled)
            payload["updated_at"] = _now()
            _atomic_json(self.root / f"{payload['schedule_id']}.json", payload)
        self._refresh_live_scheduler()
        return {"status": "enabled" if enabled else "disabled", **payload}

    def delete(self, schedule_id: str) -> Dict[str, Any]:
        normalized = str(schedule_id or "").strip().lower()
        if not _ID.fullmatch(normalized):
            raise ValueError("schedule_id 无效")
        path = self.root / f"{normalized}.json"
        with _lock:
            if not path.is_file():
                raise FileNotFoundError(f"Skill 定时任务不存在: {normalized}")
            path.unlink()
        self._refresh_live_scheduler()
        return {"status": "deleted", "schedule_id": normalized}

    def execute(self, schedule_id: str) -> Dict[str, Any]:
        schedule = self.get(schedule_id)
        if not schedule.get("enabled", True):
            return {"status": "disabled", "schedule_id": schedule_id}
        package = SkillRegistry().get(str(schedule["skill_name"]))
        if package.manifest.version != str(schedule.get("skill_version")):
            raise RuntimeError(
                f"Skill 版本已变化，拒绝静默运行: schedule={schedule.get('skill_version')} current={package.manifest.version}"
            )
        result = SkillRuntime().run(
            f"定时任务 {schedule_id} 执行 {package.manifest.name}",
            skill_name=package.manifest.name,
            inputs=schedule.get("inputs", {}),
            requested_by=f"skill-scheduler:{schedule_id}",
        )
        from src.manager.report_inbox import publish_skill_report

        delivery = publish_skill_report(
            result,
            title=f"Skill 定时任务：{schedule_id}",
            report_content=str(result.get("user_report") or result.get("error") or "无报告"),
        )
        return {**result, "schedule_id": schedule_id, "chat_delivery": delivery}

    def add_to_scheduler(self, scheduler: Any) -> int:
        count = 0
        for schedule in self.list(enabled_only=True):
            schedule_id = str(schedule["schedule_id"])
            trigger = CronTrigger.from_crontab(str(schedule["cron"]), timezone=str(schedule["timezone"]))
            scheduler.add_job(
                execute_skill_schedule,
                args=[schedule_id],
                trigger=trigger,
                id=f"skill-{schedule_id}",
                replace_existing=True,
            )
            count += 1
        return count


def execute_skill_schedule(schedule_id: str) -> Dict[str, Any]:
    return SkillScheduler().execute(schedule_id)
