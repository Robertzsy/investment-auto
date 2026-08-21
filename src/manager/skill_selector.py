from __future__ import annotations

import re
from typing import List, Optional, Tuple

from src.manager.skill_models import SkillPackage
from src.manager.skill_registry import SkillRegistry


def _tokens(value: str) -> set[str]:
    return {
        item.casefold()
        for item in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}|[\u4e00-\u9fff]{2,6}", str(value))
    }


_BROAD_MARKET_REQUEST = re.compile(
    r"(?:今天|今日|现在)?.{0,3}(?:a股|A股|沪深|大盘|市场).{0,5}(?:行情|表现|概览|怎么样|如何)|"
    r"(?:涨跌家数|市场宽度|上证指数.*深证|market\s+overview)",
    re.I,
)


class SkillSelector:
    """Deterministic high-level selection; low-level Actions are not model choices."""

    def __init__(self, registry: Optional[SkillRegistry] = None) -> None:
        self.registry = registry or SkillRegistry()

    def ranked(self, request: str, *, session_scope: str = "") -> List[Tuple[int, SkillPackage]]:
        text = str(request or "").strip().casefold()
        request_tokens = _tokens(text)
        broad_market = bool(_BROAD_MARKET_REQUEST.search(str(request or "")))
        rows: List[Tuple[int, SkillPackage]] = []
        for package in self.registry.catalog():
            manifest = package.manifest
            if session_scope and manifest.session_scope != session_scope:
                continue
            score = manifest.priority
            if broad_market and manifest.name == "market-overview":
                score += 700
            elif broad_market and manifest.name == "security-analysis":
                score -= 150
            if manifest.name.casefold() in text:
                score += 1000
            for trigger in manifest.triggers:
                normalized = trigger.casefold()
                if normalized and normalized in text:
                    score += 100 + min(50, len(normalized) * 2)
            intent_tokens = _tokens(" ".join(manifest.intents))
            description_tokens = _tokens(manifest.description)
            score += len(request_tokens & intent_tokens) * 20
            score += len(request_tokens & description_tokens) * 8
            if score > manifest.priority:
                rows.append((score, package))
        return sorted(rows, key=lambda item: (-item[0], -item[1].manifest.priority, item[1].manifest.name))

    def select(
        self,
        request: str,
        *,
        explicit_name: str = "",
        session_scope: str = "",
    ) -> SkillPackage:
        if explicit_name:
            package = self.registry.get(explicit_name)
            if session_scope and package.manifest.session_scope != session_scope:
                raise ValueError(
                    f"Skill {explicit_name} 属于 {package.manifest.session_scope}，不能在 {session_scope} 会话运行"
                )
            return package
        ranked = self.ranked(request, session_scope=session_scope)
        if not ranked:
            raise LookupError("没有与当前请求匹配的 Skill；请创建新 Skill 或明确指定已有 Skill")
        return ranked[0][1]
