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
            "Skill 或 Action 执行失败；下次依据 trajectory 定位失败步骤，并在写操作后验证完成契约。"
            if error else
            "Skill 完成契约已通过，任务结果已验证。" if verified else
            "回答已生成但 Skill 完成契约未通过；不得把调用过能力当作任务成功。"
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
