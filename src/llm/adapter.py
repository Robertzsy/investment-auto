from __future__ import annotations

from typing import Any, Dict, List

from openai import OpenAI

from .base import BaseLLM


class GenericOpenAILLM(BaseLLM):
    """Handles any provider that exposes an OpenAI-compatible v1/chat/completions endpoint.

    Provider config keys:
        api_base: str       (e.g. https://api.deepseek.com)
        model: str          (e.g. deepseek-v4-pro)
        api_key_env: str    (env-var name)
    """

    def __init__(self, provider_config: Dict[str, Any]) -> None:
        import os

        key_env = provider_config.get("api_key_env", "")
        api_key = os.getenv(key_env, "")
        if not api_key:
            # last-resort: try OPENAI_API_KEY or empty string
            api_key = os.getenv("OPENAI_API_KEY", "")

        base = provider_config.get("api_base", "")
        self._client = OpenAI(api_key=api_key, base_url=base + "/" if base else None)
        self._model = provider_config.get("model", "gpt-3.5-turbo")
        self._provider_name = provider_config.get("provider_name", "openai")

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> str:
        extra_body = kwargs.pop("extra_body", None) or None
        params: Dict[str, Any] = dict(
            model=self._model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        if extra_body:
            params["extra_body"] = extra_body
        resp = self._client.chat.completions.create(**params)
        return resp.choices[0].message.content or ""


class DeepSeekLLM(GenericOpenAILLM):
    @property
    def provider_name(self) -> str: return "deepseek"


class GLMLLM(GenericOpenAILLM):
    @property
    def provider_name(self) -> str: return "glm"


class KimiLLM(GenericOpenAILLM):
    @property
    def provider_name(self) -> str: return "kimi"
