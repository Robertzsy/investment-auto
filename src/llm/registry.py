from __future__ import annotations

from typing import Dict, List, Optional, Type

from ..config import cfg
from .base import BaseLLM
from .adapter import DeepSeekLLM, GLMLLM, KimiLLM, GenericOpenAILLM

PROVIDER_CLASS: Dict[str, Type[BaseLLM]] = {
    "openai": GenericOpenAILLM,
    "deepseek": DeepSeekLLM,
    "glm": GLMLLM,
    "kimi": KimiLLM,
}


def _build_llm(provider: str) -> BaseLLM:
    config = cfg.llm_model_config(provider)
    if not config:
        raise ValueError(f"LLM provider '{provider}' not configured in config.yaml")
    config = dict(config, provider_name=provider)
    cls = PROVIDER_CLASS.get(provider, GenericOpenAILLM)
    return cls(config)


def resolve_llm(provider: Optional[str] = None, role: Optional[str] = None) -> BaseLLM:
    """Return an LLM instance following the config chain: role→provider→fallback.

    If ``role`` is given, overrides per role_model_override.
    Otherwise uses the primary provider; falls through cfg.llm_fallback_providers if needed.
    """
    if role:
        model_name = cfg.llm_role_model(role)
        # model_name could be e.g. 'deepseek-v4-pro' or 'gpt-5.6-sol' – map it back to provider.
        for prov_name, prov_cfg in cfg._data.get("llm", {}).get("models", {}).items():
            variants = prov_cfg.get("variants", []) or [prov_cfg.get("model", "")]
            if model_name in variants:
                return _build_llm(prov_name)
        # fallback: treat as primary
        return _build_llm(cfg.llm_primary_provider)

    primary = cfg.llm_primary_provider
    chain = [primary] + [p for p in cfg.llm_fallback_providers if p != primary]
    last_err = None
    for prov in chain:
        try:
            return _build_llm(prov)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"LLM resolution failed – chain={chain} last_error={last_err}")
