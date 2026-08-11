from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

# ── project root ──────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent

def _default_config_path() -> Path:
    p = os.getenv("CONFIG_PATH")
    return Path(p) if p else ROOT / "config" / "config.yaml"

# ── load env ────────────────────────────────────────
loaded = load_dotenv(ROOT / ".env") or load_dotenv(ROOT / ".env.example") or None

# ── config singleton ─────────────────────────────
class AppConfig:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _default_config_path()
        self._data: Dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        with open(self._path, encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}

    @property
    def raw(self) -> Dict[str, Any]: return self._data

    # ── markets ────────────────────────────────
    @property
    def enabled_markets(self) -> List[str]:
        return self._data.get("markets", {}).get("enable", ["cn"])

    def market_config(self, market: str) -> Dict[str, Any]:
        p = ROOT / "config" / "market" / f"{market}.yaml"
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return {}

    # ── schedule ─────────────────────────────────
    @property
    def schedule(self) -> Dict[str, Any]:
        return self._data.get("schedule", {})

    def intraday_times(self, market: str) -> List[str]:
        return self.schedule.get("intraday_rounds", {}).get(market, [])

    def close_time(self, market: str) -> str:
        return self.schedule.get("close_rounds", {}).get(market, "")

    # ── llm ──────────────────────────────────────────
    @property
    def llm_primary_provider(self) -> str:
        return os.getenv("LLM_PROVIDER") or self._data.get("llm", {}).get("provider", "deepseek")

    @property
    def llm_fallback_providers(self) -> List[str]:
        return self._data.get("llm", {}).get("provider_fallback", [])

    def llm_model_config(self, provider: str) -> Dict[str, Any]:
        return self._data.get("llm", {}).get("models", {}).get(provider, {})

    def llm_model(self, provider: str) -> str:
        return self.llm_model_config(provider).get("model", "")

    def llm_api_key(self, provider: str) -> str:
        env_key = self.llm_model_config(provider).get("api_key_env", "")
        return os.getenv(env_key, "")

    def llm_role_model(self, role: str) -> str:
        mapping = self._data.get("llm", {}).get("role_model_override", {})
        return mapping.get(role, self.llm_model(self.llm_primary_provider))

    # ── optimizer ──────────────────────────────
    @property
    def optimizer(self) -> Dict[str, Any]:
        return self._data.get("optimizer", {})

    @property
    def autonomous(self) -> Dict[str, Any]:
        return self._data.get("autonomous", {})

    @property
    def screening(self) -> Dict[str, Any]:
        return self._data.get("screening", {})

    # ── trading / notify ─────────────────────
    @property
    def trading(self) -> Dict[str, Any]:
        return self._data.get("trading", {})

    @property
    def notify(self) -> Dict[str, Any]:
        return self._data.get("notify", {})


cfg = AppConfig()
