"""Optional live-API eval for the manager's self-tooling autonomy.

This script is NOT part of the unit suite (it needs a real LLM provider and
costs tokens).  It asks the real MANAGER_AGENT a natural-language request
that no current tool serves and checks whether the model autonomously:

1. decides to call create_manager_tool (instead of declining), and
2. uses the fulfill_result to answer in the same turn.

Usage:
    python scripts/eval-manager-tooling.py --request "帮我查一下今天 A 股成交额排名"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pydantic_ai import CancellationToken, UsageLimits

from src.config import cfg
from src.llm.agent_model import resolve_agent_model
from src.manager.capabilities import CapabilityRegistry
from src.ui.agent_runtime import ChatAgentDeps, MANAGER_AGENT, _memory_instructions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", default="帮我查一下今天 A 股成交额排名")
    parser.add_argument("--max-requests", type=int, default=40)
    args = parser.parse_args()

    before = {item["name"] for item in CapabilityRegistry().catalog()["tools"]}
    events: list[dict] = []

    async def scenario() -> str:
        deps = ChatAgentDeps(cancellation_token=CancellationToken(), event_queue=queue.Queue())
        reserved = set(MANAGER_AGENT._function_toolset.tools)
        result = await MANAGER_AGENT.run(
            args.request,
            model=resolve_agent_model(role="chat"),
            deps=deps,
            usage_limits=UsageLimits(
                request_limit=args.max_requests,
                tool_calls_limit=args.max_requests * 2,
                total_tokens_limit=200_000,
            ),
            instructions=_memory_instructions(""),
            toolsets=[CapabilityRegistry().toolset(reserved)],
        )
        return str(result.output)

    output = asyncio.run(scenario())
    after = {item["name"] for item in CapabilityRegistry().catalog()["tools"]}
    created = sorted(after - before)
    print(json.dumps({
        "request": args.request,
        "output": output[:2000],
        "tools_created": created,
        "llm_provider": cfg.llm_primary_provider,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
