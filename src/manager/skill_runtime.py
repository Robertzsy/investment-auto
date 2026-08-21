from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional

from src.manager.action_registry import ActionRegistry
from src.manager.completion_validator import CompletionValidator
from src.manager.execution_trace import ExecutionTrace
from src.manager.session_router import SessionRouter
from src.manager.session_store import SessionStore
from src.manager.skill_registry import SkillRegistry
from src.manager.skill_selector import SkillSelector


_SIDE_EFFECT_RANK = {
    "read_only": 0,
    "portfolio_write": 1,
    "investment_execution": 2,
    "system_admin": 3,
}


def _failure_category(error: str) -> str:
    text = str(error or "").casefold()
    if any(value in text for value in ("身份不一致", "identity", "symbol", "代码")):
        return "identity"
    if any(value in text for value in ("timeout", "timed out", "连接", "network", "dns")):
        return "network"
    if any(value in text for value in ("402", "余额", "billing", "quota")):
        return "external_billing"
    if any(value in text for value in ("过期", "fresh", "时间")):
        return "freshness"
    return "action_error"


class SkillRuntime:
    def __init__(
        self,
        *,
        registry: Optional[SkillRegistry] = None,
        actions: Optional[ActionRegistry] = None,
        sessions: Optional[SessionStore] = None,
    ) -> None:
        self.registry = registry or SkillRegistry()
        self.actions = actions or ActionRegistry()
        self.sessions = sessions or SessionStore()
        self.selector = SkillSelector(self.registry)
        self.router = SessionRouter()
        self.validator = CompletionValidator()

    def _validate_permissions(self, package) -> None:
        manifest = package.manifest
        for name in manifest.allowed_actions:
            action = self.actions.get(name)
            if manifest.session_scope not in action.session_scopes:
                raise PermissionError(
                    f"Skill {manifest.name} 的会话 {manifest.session_scope} 无权调用 {name}"
                )
            if _SIDE_EFFECT_RANK[action.side_effect_level] > _SIDE_EFFECT_RANK[manifest.side_effect_level]:
                raise PermissionError(
                    f"Skill {manifest.name} 声明的副作用等级不足以调用 {name}"
                )

    def run(
        self,
        request: str,
        *,
        skill_name: str = "",
        inputs: Optional[Mapping[str, Any]] = None,
        session_scope: str = "",
        requested_by: str = "conversation-manager",
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        normalized_inputs: Dict[str, Any] = dict(inputs or {})
        normalized_inputs.setdefault("request", str(request))
        last_scope = self.sessions.last_scope()
        initial_scope = self.router.route(request, skill_scope=session_scope, last_scope=last_scope)
        try:
            package = self.selector.select(
                request,
                explicit_name=skill_name,
                session_scope=initial_scope if not skill_name else "",
            )
        except LookupError:
            package = self.selector.select(request, explicit_name=skill_name, session_scope="")
        scope = self.router.route(
            request,
            skill_scope=package.manifest.session_scope,
            last_scope=last_scope,
        )
        previous = self.sessions.load(scope)
        if not normalized_inputs.get("symbols") and str(request).strip().startswith(("继续", "接着", "再看", "它", "这个")):
            entities = previous.get("entities", [])
            if entities:
                normalized_inputs["symbols"] = entities
        self._validate_permissions(package)
        trace = ExecutionTrace(
            request=request,
            skill=package.manifest.name,
            version=package.manifest.version,
            session_scope=scope,
            inputs=normalized_inputs,
        )
        state: Dict[str, Any] = {
            "request": str(request),
            "inputs": normalized_inputs,
            "outputs": {},
            "step_statuses": {},
            "recoveries": [],
        }
        fatal_error = ""
        for step in package.steps:
            if progress_callback:
                progress_callback(f"[{package.manifest.name}] {step.step_id}: {step.action}")
            step_inputs = {**step.input_defaults, **normalized_inputs}
            started = time.monotonic()
            last_error = ""
            result: Any = None
            attempts = 0
            for attempt in range(min(step.retries, 2) + 1):
                attempts = attempt + 1
                try:
                    result = self.actions.execute(
                        step.action,
                        step_inputs,
                        {
                            "state": state,
                            "skill": package.manifest.to_dict(),
                            "instructions": package.instructions,
                            "requested_by": requested_by,
                            "progress_callback": progress_callback,
                        },
                        session_scope=scope,
                    )
                    last_error = ""
                    break
                except Exception as exc:
                    last_error = str(exc)[:2000]
                    if progress_callback and attempt < step.retries:
                        progress_callback(f"[{package.manifest.name}] {step.step_id} 失败，准备重试：{last_error}")
                    if attempt < min(step.retries, 2):
                        recovery = {
                            "step": step.step_id,
                            "action": step.action,
                            "category": _failure_category(last_error),
                            "attempt": attempt + 1,
                            "reason": last_error,
                        }
                        state["recoveries"].append(recovery)
                        trace.step({
                            "id": step.step_id,
                            "action": step.action,
                            "status": "recovering",
                            "required": step.required,
                            **recovery,
                        })
            duration_ms = int((time.monotonic() - started) * 1000)
            if last_error:
                state["step_statuses"][step.step_id] = "failed"
                trace.step({
                    "id": step.step_id,
                    "action": step.action,
                    "status": "failed",
                    "required": step.required,
                    "error": last_error,
                    "duration_ms": duration_ms,
                })
                if step.required:
                    fatal_error = last_error
                    break
                continue
            state["step_statuses"][step.step_id] = "completed"
            state["outputs"][step.step_id] = result
            state["outputs"][step.action] = result
            trace.step({
                "id": step.step_id,
                "action": step.action,
                "status": "completed",
                "required": step.required,
                "attempts": attempts,
                "duration_ms": duration_ms,
                "output": result,
            })
        validation = self.validator.validate(
            package.completion_contract,
            outputs=state["outputs"],
            step_statuses=state["step_statuses"],
        )
        if fatal_error or not validation.get("structural_passed", validation["passed"]):
            status = "incomplete"
        elif validation.get("degraded"):
            status = "degraded"
        else:
            status = "completed"
        user_report = ""
        entities: list[str] = []
        for step in reversed(package.steps):
            value = state["outputs"].get(step.step_id)
            if isinstance(value, Mapping):
                if not user_report and value.get("user_report"):
                    user_report = str(value["user_report"])
                if not entities and isinstance(value.get("entities"), list):
                    entities = [str(item) for item in value["entities"]]
        if not entities:
            resolved = state["outputs"].get("resolve", {})
            if isinstance(resolved, Mapping):
                entities = [
                    str(item.get("symbol"))
                    for item in resolved.get("securities", [])
                    if isinstance(item, Mapping) and item.get("symbol")
                ]
        result = {
            "status": status,
            "skill": package.manifest.name,
            "skill_version": package.manifest.version,
            "session_scope": scope,
            "execution_id": trace.execution_id,
            "trace_path": str(trace.path),
            "user_report": user_report,
            "outputs": state["outputs"],
            "validation": validation,
            "recoveries": state["recoveries"],
            "error": fatal_error,
            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        trace.finish(status=status, validation=validation, result=result)
        self.sessions.record(
            scope,
            request=request,
            skill=package.manifest.name,
            execution_id=trace.execution_id,
            status=status,
            entities=entities,
            summary=user_report or fatal_error,
        )
        return result
