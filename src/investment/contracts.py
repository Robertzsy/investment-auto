from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional


MARKETS = {"cn", "hk", "us", "etf"}


class InvestmentCommand(str, Enum):
    RUN_CYCLE = "run_cycle"
    PAUSE = "pause"
    RESUME = "resume"
    KILL = "kill"
    RESET_KILL = "reset_kill"
    RUN_SCREENING = "run_screening"
    RUN_OPTIMIZER = "run_optimizer"
    SET_MODE = "set_mode"
    SET_STRATEGY = "set_strategy"
    REFLECT = "reflect"
    RESET_PAPER_ACCOUNT = "reset_paper_account"
    STATUS = "status"


@dataclass(frozen=True)
class CommandEnvelope:
    command_id: str
    command: InvestmentCommand
    payload: Dict[str, Any] = field(default_factory=dict)
    requested_by: str = "manager"
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["command"] = self.command.value
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CommandEnvelope":
        payload = value.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValueError("command payload 必须是对象")
        return cls(
            command_id=str(value.get("command_id", "")),
            command=InvestmentCommand(str(value.get("command", ""))),
            payload=dict(payload),
            requested_by=str(value.get("requested_by", "manager")),
            created_at=str(value.get("created_at", "")),
        )


def normalize_market(value: Optional[str]) -> str:
    market = str(value or "").strip().lower()
    if market not in MARKETS:
        raise ValueError("market 必须是 cn、hk、us 或 etf")
    return market
