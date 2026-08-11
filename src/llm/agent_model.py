from __future__ import annotations

import os
from typing import Optional

from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from src.config import cfg


def _configured_model(provider: str, model_override: Optional[str] = None) -> Model:
    """Build a Pydantic AI model from the existing OpenAI-compatible config."""

    provider_config = cfg.llm_model_config(provider)
    if not provider_config:
        raise ValueError(f"LLM provider '{provider}' 未配置")

    key_env = str(provider_config.get("api_key_env", "")).strip()
    if not key_env:
        raise ValueError(f"LLM provider '{provider}' 缺少 api_key_env")
    api_key = os.getenv(key_env, "").strip()
    if not api_key:
        raise ValueError(f"LLM provider '{provider}' 需要环境变量 {key_env}")

    model_name = str(model_override or provider_config.get("model", "")).strip()
    if not model_name:
        raise ValueError(f"LLM provider '{provider}' 未配置模型")
    base_url = str(provider_config.get("api_base", "")).strip() or None
    return OpenAIChatModel(
        model_name,  # type: ignore[arg-type]
        provider=OpenAIProvider(base_url=base_url, api_key=api_key),
    )


def resolve_agent_model(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    *,
    role: str = "chat",
    include_fallback: bool = True,
) -> Model:
    """Resolve the selected model and configured fallbacks for Agent runs.

    Unlike the legacy chat adapter, an explicitly selected UI provider still
    receives the configured fallback chain. A temporary provider outage should
    not leave the web conversation hanging.
    """

    selected_provider = provider or cfg.llm_primary_provider
    selected_model = model
    if not provider and not model and role:
        role_model = cfg.llm_role_model(role)
        for provider_name, provider_config in cfg.raw.get("llm", {}).get("models", {}).items():
            variants = provider_config.get("variants", []) or [provider_config.get("model", "")]
            if role_model in variants or role_model == provider_config.get("model"):
                selected_provider = provider_name
                selected_model = role_model
                break

    provider_names = [selected_provider]
    if include_fallback:
        provider_names.extend(cfg.llm_fallback_providers)
    provider_names = list(dict.fromkeys(name for name in provider_names if name))

    models: list[Model] = []
    errors: list[str] = []
    for provider_name in provider_names:
        try:
            models.append(
                _configured_model(
                    provider_name,
                    selected_model if provider_name == selected_provider else None,
                )
            )
        except ValueError as exc:
            errors.append(str(exc))

    if not models:
        raise ValueError("没有可用的 Agent 模型：" + "；".join(errors))
    if len(models) == 1:
        return models[0]
    return FallbackModel(models[0], *models[1:])
