from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterator, List, Optional, Type

from ..config import cfg
from .base import BaseLLM
from .adapter import DeepSeekLLM, GLMLLM, KimiLLM, GenericOpenAILLM

PROVIDER_CLASS: Dict[str, Type[BaseLLM]] = {
    "openai": GenericOpenAILLM,
    "deepseek": DeepSeekLLM,
    "glm": GLMLLM,
    "kimi": KimiLLM,
}

logger = logging.getLogger(__name__)


class RuntimeFallbackLLM(BaseLLM):
    """Try provider adapters at call time, not only while constructing them."""

    def __init__(self, chain: List[tuple[str, Optional[str]]]) -> None:
        if not chain:
            raise ValueError("LLM fallback chain must not be empty")
        self._chain = chain

    @property
    def provider_name(self) -> str:
        return self._chain[0][0]

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> str:
        errors = []
        for provider, model_override in self._chain:
            try:
                llm = _build_llm(provider, model_override=model_override)
                text = llm.chat(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )
                if not text or not text.strip():
                    raise RuntimeError("provider returned an empty completion")
                return text
            except InterruptedError:
                raise
            except Exception as exc:
                errors.append(f"{provider}: {exc}")
                logger.warning("LLM provider %s failed, trying fallback: %s", provider, exc)
        raise RuntimeError(
            f"LLM chat failed – chain={[provider for provider, _ in self._chain]} "
            f"errors={'; '.join(errors)}"
        )

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        cancel_event: Optional[threading.Event] = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        """Apply the same runtime fallback policy before a stream emits data."""
        errors = []
        for provider, model_override in self._chain:
            emitted = False
            try:
                llm = _build_llm(provider, model_override=model_override)
                for chunk in llm.chat_stream(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    cancel_event=cancel_event,
                    **kwargs,
                ):
                    emitted = True
                    yield chunk
                if not emitted:
                    raise RuntimeError("provider returned an empty stream")
                return
            except InterruptedError:
                raise
            except Exception as exc:
                # Switching after yielding text would splice two unrelated
                # completions together, so only a pre-output failure is safe.
                if emitted:
                    raise
                errors.append(f"{provider}: {exc}")
                logger.warning("LLM provider %s stream failed, trying fallback: %s", provider, exc)
        raise RuntimeError(
            f"LLM stream failed – chain={[provider for provider, _ in self._chain]} "
            f"errors={'; '.join(errors)}"
        )


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

    selected_provider = cfg.llm_primary_provider
    selected_model: Optional[str] = None
    if role:
        model_name = cfg.llm_role_model(role)
        for prov_name, prov_cfg in cfg.raw.get("llm", {}).get("models", {}).items():
            variants = prov_cfg.get("variants", []) or [prov_cfg.get("model", "")]
            if model_name in variants or model_name == prov_cfg.get("model"):
                selected_provider = prov_name
                selected_model = model_name
                break

    providers = list(dict.fromkeys([selected_provider, *cfg.llm_fallback_providers]))
    chain = [
        (provider_name, selected_model if provider_name == selected_provider else None)
        for provider_name in providers
    ]
    return RuntimeFallbackLLM(chain)
