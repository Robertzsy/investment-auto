"""Optional live-API eval for the manager's self-tooling autonomy.

NOT part of the unit suite (needs a real LLM provider, costs tokens).
It asks the real MANAGER_AGENT a natural-language request no current tool
serves and verifies whether the model autonomously:

1. called create_manager_tool (captured from the run event stream), and
2. answered with the fulfill result in the same turn.

A unique nonce is injected into the request; the eval PASSES only when the
nonce shows up in the create_manager_tool tool return AND in the final
answer.  Tools created during the eval are uninstalled automatically in a
finally block unless --keep is given.  Exit code 0 = pass, 1 = fail.

Usage:
    python scripts/eval-manager-tooling.py --request "帮我查一下今天 A 股成交额排名"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic_ai import CancellationToken, UsageLimits
from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent

from src.config import cfg
from src.llm.agent_model import resolve_agent_model
from src.manager.capabilities import CapabilityRegistry
from src.manager.tool_factory import uninstall_manager_tool_complete
from src.ui.agent_runtime import ChatAgentDeps, MANAGER_AGENT, _memory_instructions


async def _run(request: str, nonce: str, max_requests: int, total_tokens: int):
    registry = CapabilityRegistry()
    before = {item["name"] for item in registry.catalog()["tools"]}
    created_calls: list[dict] = []
    tool_results: list[dict] = []

    deps = ChatAgentDeps(cancellation_token=CancellationToken(), event_queue=queue.Queue())
    reserved = set(MANAGER_AGENT._function_toolset.tools)

    async def event_handler(_, stream):
        async for event in stream:
            if isinstance(event, FunctionToolCallEvent):
                if event.part.tool_name == "create_manager_tool":
                    created_calls.append({
                        "tool_call_id": event.tool_call_id,
                        "args": event.part.args_as_dict(),
                    })
            elif isinstance(event, FunctionToolResultEvent):
                if event.part.tool_name == "create_manager_tool":
                    tool_results.append({
                        "tool_call_id": event.tool_call_id,
                        "content": event.part.content,
                    })

    result = await MANAGER_AGENT.run(
        request,
        model=resolve_agent_model(role="chat"),
        deps=deps,
        usage_limits=UsageLimits(
            request_limit=max_requests,
            tool_calls_limit=max_requests * 2,
            total_tokens_limit=total_tokens,
        ),
        instructions=_memory_instructions(""),
        toolsets=[CapabilityRegistry().toolset(reserved)],
        event_stream_handler=event_handler,
    )
    after = {item["name"] for item in registry.catalog()["tools"]}
    created = sorted(after - before)
    return str(result.output), created, created_calls, tool_results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", default="帮我查一下今天 A 股成交额排名")
    parser.add_argument("--max-requests", type=int, default=40)
    parser.add_argument("--total-tokens", type=int, default=600_000)
    parser.add_argument("--keep", action="store_true", help="Keep created eval tools")
    args = parser.parse_args()

    nonce = uuid.uuid4().hex[:8]
    request = f"{args.request}\n\n请在工具结果与最终回答中都带上校验码 EVAL-{nonce}。"
    created: list[str] = []
    try:
        output, created, created_calls, tool_results = asyncio.run(
            _run(request, nonce, args.max_requests, args.total_tokens)
        )
        called = len(created_calls) >= 1
        result_nonce = any(
            f"EVAL-{nonce}" in json.dumps(item.get("content", ""), ensure_ascii=False)
            for item in tool_results
        )
        answer_nonce = f"EVAL-{nonce}" in output
        passed = called and result_nonce and answer_nonce
        print(json.dumps({
            "passed": passed,
            "nonce": nonce,
            "request": args.request,
            "output": output[:2000],
            "tools_created": created,
            "create_tool_calls": len(created_calls),
            "nonce_in_tool_result": result_nonce,
            "nonce_in_answer": answer_nonce,
            "llm_provider": cfg.llm_primary_provider,
        }, ensure_ascii=False, indent=2))
        return 0 if passed else 1
    finally:
        if not args.keep:
            for name in created:
                try:
                    uninstall_manager_tool_complete(name)
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
