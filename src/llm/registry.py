from __future__ import annotations

from typing import Dict, Optional, Type

from ..config import cfg
from .base import BaseLLM
from .adapter import DeepSeekLLM, GLMLLM, KimiLLM, GenericOpenAILLM

PROVIDER_CLASS: Dict[str, Type[BaseLLM]] = {
    "openai": GenericOpenAILLM,
    "deepseek": DeepSeekLLM,
    "glm": GLMLLM,
    "kimi": KimiLLM,
}


def _build_llm(provider: str, model_override: Optional[str] = None) -> BaseLLM:
    config = cfg.llm_model_config(provider)
    if not config:
        raise ValueError(f"LLM provider '{provider}' not configured in config.yaml")
    config = dict(config, provider_name=provider)
    cls = PROVIDER_CLASS.get(provider, GenericOpenAILLM)
    return cls(config, model_override=model_override)


def available_models() -> Dict[str, dict]:
    return cfg.raw.get("llm", {}).get("models", {})


def resolve_llm(provider: Optional[str] = None, model: Optional[str] = None, role: Optional[str] = None) -> BaseLLM:
    """Return an LLM instance.

    Priority:
      explicit provider/model > role override > primary+fallback.
    """
    if provider:
        return _build_llm(provider, model_override=model)

    if role:
        model_name = cfg.llm_role_model(role)
        for prov_name, prov_cfg in cfg.raw.get("llm", {}).get("models", {}).items():
            variants = prov_cfg.get("variants", []) or [prov_cfg.get("model", "")]
            if model_name in variants or model_name == prov_cfg.get("model"):
                return _build_llm(prov_name, model_override=model_name)
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
