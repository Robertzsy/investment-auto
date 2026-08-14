from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from src.manager.memory import ManagerMemory
from src.platform.memory_store import StructuredMemoryStore


class ManagerReflectionService:
    """Post-task verification and learning for the conversation manager."""

    def __init__(
        self,
        store: Optional[StructuredMemoryStore] = None,
        memory: Optional[ManagerMemory] = None,
    ) -> None:
        self.store = store or StructuredMemoryStore()
        self.memory = memory or ManagerMemory(self.store)

    def record(
        self,
        *,
        user_goal: str,
        outcome: str,
        tool_calls: Sequence[str],
        error: str = "",
        verified: bool,
    ) -> Dict[str, Any]:
        lesson = (
            "工具或执行失败；下次先读取真实状态，再选择单一管理工具，并在写操作后复读验证。"
            if error else
            "任务已完成且外部状态已验证。" if verified else
            "回答已生成但缺少外部状态验证；后续涉及修改或控制时必须调用读取接口确认。"
        )
        record = self.store.append("manager_reflections", {
            "kind": "management_task_reflection",
            "user_goal": str(user_goal)[:4000],
            "outcome": str(outcome)[:6000],
            "tool_calls": list(tool_calls)[:40],
            "error": str(error)[:2000],
            "verified": bool(verified),
            "lesson": lesson,
        })
        if error or not verified:
            self.memory.remember(lesson, source="reflection", confidence=0.8)
        return record

