from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from src.platform.memory_store import StructuredMemoryStore


class ManagerMemory:
    def __init__(self, store: Optional[StructuredMemoryStore] = None) -> None:
        self.store = store or StructuredMemoryStore()

    def remember(self, note: str, *, source: str = "manager", confidence: float = 1.0) -> Dict[str, Any]:
        cleaned = str(note or "").strip()
        if not cleaned:
            raise ValueError("记忆内容不能为空")
        return self.store.append("manager_memories", {
            "scope": "management",
            "source": str(source)[:80],
            "confidence": max(0.0, min(1.0, float(confidence))),
            "note": cleaned[:4000],
            "expires_at": None,
        })

    def recent(self, limit: int = 12) -> List[Dict[str, Any]]:
        return self.store.recent("manager_memories", limit=limit)

    def as_prompt(self, limit: int = 12) -> str:
        rows = []
        for item in reversed(self.recent(limit)):
            rows.append(f"- {item.get('created_at', '')} [{item.get('source', '')}]: {item.get('note', '')}")
        return "\n".join(rows)

