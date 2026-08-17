"""Ralph-style offline research loop: fresh agent per round, bounded reports.

Every round opens a brand-new agent with no conversation seed.  The shared
workspace is the only long-term memory, and only a bounded structured report
crosses rounds, so early-round hallucinations cannot pollute later rounds
through the prompt.  Completion and blockers are worker reports; the loop
itself never judges them.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from pydantic_ai import RunContext

from src.research.workspace import ResearchWorkspace

logger = logging.getLogger("investment-auto.research")
TIMEZONE = ZoneInfo("Asia/Shanghai")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PRIVATE_PROJECT_PARTS = {".git", ".venv", "runtime", "data", "build", "release"}

TASK_DESCRIPTIONS: Dict[str, str] = {
    "backtest": (
        "用确定性规则回测验证策略假设：调用 run_backtest 工具，对比不同参数，"
        "把结果与结论写入工作区文件；不要修改任何生产配置或源代码。"
    ),
    "strategy_experiment": (
        "围绕参数空间做对比实验：多次调用 run_backtest，记录对比表，"
        "给出可复现的推荐参数；生产配置只能通过 apply_code_change 版本化修改。"
    ),
    "bugfix": (
        "先复现缺陷：工作区 cwd 在 runtime/research/workspace 下，PYTHONPATH 已指向项目根。"
        "跑测试必须用项目根绝对路径，并且要用正斜杠加双引号："
        "python -m pytest \"{PROJECT_ROOT}/tests/test_core.py\"（反斜杠会被命令解析吞掉）。"
        "定位后通过 apply_code_change 修改代码，该工具会执行全量测试并在失败时自动回滚；"
        "每个修改都要写清原因。"
    ),
}


@dataclass
class RoundReport:
    status: str  # completed | blocked | needs_more_rounds
    findings: List[str] = field(default_factory=list)
    changes_made: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)
    blocker_reason: str = ""


@dataclass
class ResearchDeps:
    workspace: ResearchWorkspace
    round_no: int
    shell_timeout_seconds: int = 60


@dataclass
class ResearchOutcome:
    run_id: str
    status: str  # completed | blocked | rounds_exhausted | error
    rounds: int
    final_report: Dict[str, Any]
    workspace: str
    blocker_reason: str = ""


def _report_summary(report: Mapping[str, Any], limit: int = 6) -> str:
    rows = [
        f"status={report.get('status')}",
        f"findings: {'; '.join(str(item) for item in report.get('findings', [])[:limit]) or '-'}",
        f"changes_made: {'; '.join(str(item) for item in report.get('changes_made', [])[:limit]) or '-'}",
        f"next_steps: {'; '.join(str(item) for item in report.get('next_steps', [])[:limit]) or '-'}",
    ]
    blocker = str(report.get("blocker_reason", "")).strip()
    if blocker:
        rows.append(f"blocker_reason={blocker}")
    return "\n".join(rows)


def build_round_prompt(
    objective: str,
    task: str,
    *,
    round_no: int,
    workspace_index: Mapping[str, Any],
    previous_reports: Sequence[Mapping[str, Any]],
) -> str:
    from src.research.workspace import ROOT as _PROJECT_ROOT

    task_hint = TASK_DESCRIPTIONS.get(task, TASK_DESCRIPTIONS["backtest"]).replace(
        "{PROJECT_ROOT}", str(_PROJECT_ROOT).replace(chr(92), "/")
    )
    summaries = "\n".join(
        f"--- 第 {report.get('round', idx + 1)} 轮报告摘要 ---\n{_report_summary(report)}"
        for idx, report in enumerate(previous_reports)
    ) or "（尚无历史轮次）"
    files = workspace_index.get("files", [])
    file_list = "\n".join(f"- {name}" for name in files[:100]) or "（工作区为空）"
    return (
        f"不可变目标：{objective}\n"
        f"任务类型：{task}\n任务说明：{task_hint}\n"
        f"当前是第 {round_no} 轮。你没有上一轮的对话记忆，只能依据下面的工作区状态。\n\n"
        f"工作区文件清单：\n{file_list}\n\n"
        f"历史轮次报告摘要（只读，不是事实来源）：\n{summaries}\n\n"
        "本轮要求：\n"
        "1. 先读取工作区已有文件与工具结果，不要凭空假设。\n"
        "2. 用工具完成任务；结果必须写进工作区文件，重要结论同时写入最终报告。\n"
        "3. 最终报告 status 只能是 completed / blocked / needs_more_rounds；\n"
        "   blocker_reason 只在 blocked 时填写，必须说明具体阻塞条件。\n"
        "4. 不得修改生产配置、账户、提示词或交易边界；代码修改只能通过 apply_code_change。"
    )


def _workspace_tools(include_shell_tools: bool = True):
    """Return the shared workspace tools with a bound RunContext.

    include_shell_tools=False produces a shell-less agent (architecture.
    research.shell=none); the trading plane never has a shell either way.
    """
    from pydantic_ai import Tool

    async def list_workspace(ctx: RunContext[ResearchDeps]) -> Dict[str, Any]:
        return {"files": ctx.deps.workspace.list_files()}

    async def read_workspace_file(ctx: RunContext[ResearchDeps], path: str) -> str:
        return ctx.deps.workspace.read_file(path)

    async def write_workspace_file(ctx: RunContext[ResearchDeps], path: str, content: str) -> Dict[str, Any]:
        saved = ctx.deps.workspace.write_file(ctx.deps.round_no, path, content)
        return {"saved": str(saved.relative_to(ctx.deps.workspace.run_dir)).replace(chr(92), "/")}

    async def read_project_file(
        ctx: RunContext[ResearchDeps], path: str, start_line: int = 1, max_lines: int = 200,
    ) -> Dict[str, Any]:
        """Read one non-sensitive source or test file from the project.

        Secret values and generated runtime/build trees are intentionally
        unavailable to the external research model.
        """
        relative = Path(str(path or "").replace(chr(92), "/"))
        if relative.is_absolute() or ".." in relative.parts:
            return {"ok": False, "error": "只允许项目内相对路径"}
        if not relative.parts or relative.name.startswith(".env") or any(
            part in _PRIVATE_PROJECT_PARTS for part in relative.parts
        ):
            return {"ok": False, "error": "该路径属于私密或生成数据，禁止研究模型读取"}
        target = (PROJECT_ROOT / relative).resolve()
        if target != PROJECT_ROOT and PROJECT_ROOT not in target.parents:
            return {"ok": False, "error": "路径越出项目目录"}
        if not target.is_file():
            return {"ok": False, "error": "文件不存在"}
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return {"ok": False, "error": str(exc)[:500]}
        lines = content.splitlines()
        start = max(1, int(start_line))
        count = max(20, min(300, int(max_lines)))
        selected = lines[start - 1:start - 1 + count]
        return {
            "ok": True,
            "path": str(relative).replace(chr(92), "/"),
            "start_line": start,
            "end_line": start + len(selected) - 1,
            "content": "\n".join(f"{start + index}: {line}" for index, line in enumerate(selected)),
            "has_more": start - 1 + len(selected) < len(lines),
        }

    tools = [
        Tool(list_workspace, sequential=True, timeout=15),
        Tool(read_workspace_file, sequential=True, timeout=15),
        Tool(write_workspace_file, sequential=True, timeout=15),
        Tool(read_project_file, sequential=True, timeout=15),
    ]
    if include_shell_tools:
        async def run_research_command(ctx: RunContext[ResearchDeps], command: str, timeout: Optional[int] = None) -> Dict[str, Any]:
            from src.research.sandbox import run_command

            configured = max(1, ctx.deps.shell_timeout_seconds)
            limit = max(1, min(configured, int(timeout or configured)))
            try:
                return run_command(
                    command,
                    workdir=ctx.deps.workspace.run_dir,
                    timeout=limit,
                )
            except (OSError, ValueError) as exc:
                # A rejected research command is recoverable model feedback,
                # not a reason to abort the entire fresh-agent round.
                return {
                    "exit_code": None,
                    "timed_out": False,
                    "rejected": True,
                    "stdout": "",
                    "stderr": str(exc)[:1000],
                }

        tools.append(Tool(run_research_command, sequential=True, timeout=310))
    return tools


def default_agent_factory(extra_tools: Sequence[Any] = (), include_shell_tools: bool = True):
    from pydantic_ai import Agent
    from src.llm.agent_model import resolve_agent_model

    tools = [*_workspace_tools(include_shell_tools=include_shell_tools), *list(extra_tools)]
    return Agent(
        name="investment_research",
        deps_type=ResearchDeps,
        output_type=RoundReport,
        tools=tools,
        retries=1,
        tool_timeout=90,
        model=resolve_agent_model(role="research"),
    )


def run_research_loop(
    objective: str,
    task: str,
    *,
    market: str = "",
    max_rounds: int = 6,
    workspace_root: Optional[Path] = None,
    extra_tools: Sequence[Any] = (),
    include_shell_tools: bool = True,
    shell_timeout_seconds: int = 60,
    request_limit: int = 32,
    on_progress: Optional[Callable[[str], None]] = None,
    agent_factory: Optional[Callable[..., Any]] = None,
) -> ResearchOutcome:
    """Run fresh-agent rounds toward one immutable objective."""
    objective = str(objective or "").strip()
    if not objective:
        raise ValueError("research objective 不能为空")
    task = str(task or "").strip().lower()
    if task not in TASK_DESCRIPTIONS:
        raise ValueError("task 必须是 backtest、strategy_experiment 或 bugfix")
    rounds_limit = max(1, min(20, int(max_rounds)))

    def progress(message: str) -> None:
        if on_progress is not None:
            try:
                on_progress(message)
            except Exception:
                logger.debug("Progress callback failed", exc_info=True)

    workspace = ResearchWorkspace(workspace_root)
    workspace.initialize(objective, task)
    if market:
        try:
            import json as _json

            meta_path = workspace.run_dir / "meta.json"
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            meta["market"] = str(market).lower()
            temporary = meta_path.with_suffix(".json.tmp")
            temporary.write_text(_json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(meta_path)
        except Exception:
            logger.debug("Could not record market in workspace meta", exc_info=True)
    factory = agent_factory or default_agent_factory
    agent = factory(extra_tools=extra_tools, include_shell_tools=include_shell_tools)
    from pydantic_ai import UsageLimits

    last_report: Dict[str, Any] = {}
    for round_no in range(1, rounds_limit + 1):
        progress(f"研究循环第 {round_no}/{rounds_limit} 轮：新 Agent 启动")
        prompt = build_round_prompt(
            objective,
            task,
            round_no=round_no,
            workspace_index=workspace.index(),
            previous_reports=workspace.read_round_reports(3),
        )
        deps = ResearchDeps(
            workspace=workspace, round_no=round_no,
            shell_timeout_seconds=shell_timeout_seconds,
        )
        try:
            result = agent.run_sync(
                prompt,
                deps=deps,
                usage_limits=UsageLimits(
                    request_limit=max(4, min(128, int(request_limit))),
                    total_tokens_limit=120000,
                ),
            )
            report_payload = result.output
        except Exception as exc:
            logger.exception("Research round %s failed", round_no)
            report_payload = RoundReport(
                status="blocked",
                findings=[],
                blocker_reason=f"第 {round_no} 轮执行异常: {str(exc)[:500]}",
            )
        if isinstance(report_payload, RoundReport):
            last_report = asdict(report_payload)
        elif isinstance(report_payload, Mapping):
            last_report = dict(report_payload)
        else:
            last_report = {"status": "blocked", "blocker_reason": "Agent 未返回结构化报告"}
        workspace.write_round_report(round_no, last_report)
        status = str(last_report.get("status", ""))
        if status == "completed":
            progress("研究循环完成")
            return ResearchOutcome(
                run_id=workspace.run_id,
                status="completed",
                rounds=round_no,
                final_report=last_report,
                workspace=str(workspace.run_dir),
            )
        if status == "blocked":
            progress("研究循环被阻塞，保留工作区供后续续跑")
            return ResearchOutcome(
                run_id=workspace.run_id,
                status="blocked",
                rounds=round_no,
                final_report=last_report,
                workspace=str(workspace.run_dir),
                blocker_reason=str(last_report.get("blocker_reason", "")),
            )
        progress(f"第 {round_no} 轮要求继续：{str(last_report.get('next_steps', []))[:200]}")
    return ResearchOutcome(
        run_id=workspace.run_id,
        status="rounds_exhausted",
        rounds=rounds_limit,
        final_report=last_report,
        workspace=str(workspace.run_dir),
    )
