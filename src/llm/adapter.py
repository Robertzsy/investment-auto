from __future__ import annotations

import threading
from typing import Any, Dict, Iterator, List, Optional

from openai import OpenAI

from .base import BaseLLM


class GenericOpenAILLM(BaseLLM):
    """Handles any provider that exposes an OpenAI-compatible v1/chat/completions endpoint.

    Provider config keys:
        api_base: str       (e.g. https://api.deepseek.com)
        model: str          (e.g. deepseek-v4-pro)
        api_key_env: str    (env-var name)
    """

    def __init__(self, provider_config: Dict[str, Any], model_override: str | None = None) -> None:
        import os

        key_env = str(provider_config.get("api_key_env", "")).strip()
        provider_name = str(provider_config.get("provider_name", "openai"))
        if not key_env:
            raise ValueError(f"LLM provider '{provider_name}' has no api_key_env configured")
        api_key = os.getenv(key_env, "").strip()
        if not api_key:
            raise ValueError(f"LLM provider '{provider_name}' requires environment variable {key_env}")

        base = provider_config.get("api_base", "")
        request_timeout = float(provider_config.get("request_timeout_seconds", 120))
        if request_timeout <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self._client = OpenAI(
            api_key=api_key,
            base_url=base + "/" if base else None,
            timeout=request_timeout,
        )
        self._model = model_override or provider_config.get("model", "gpt-3.5-turbo")
        self._provider_name = provider_name

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

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        cancel_event: Optional[threading.Event] = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        """Read an OpenAI-compatible response as a real, cancellable stream."""
        extra_body = kwargs.pop("extra_body", None) or None
        params: Dict[str, Any] = dict(
            model=self._model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            **kwargs,
        )
        if extra_body:
            params["extra_body"] = extra_body
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("chat completion cancelled")

        stream = self._client.chat.completions.create(**params)
        finished = threading.Event()

        def close_when_cancelled() -> None:
            assert cancel_event is not None
            while not finished.is_set():
                if cancel_event.wait(0.05):
                    if not finished.is_set():
                        try:
                            stream.close()
                        except Exception:
                            pass
                    return

        if cancel_event is not None:
            threading.Thread(target=close_when_cancelled, name="llm-stream-canceller", daemon=True).start()

        try:
            for chunk in stream:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("chat completion cancelled")
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    yield content
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("chat completion cancelled")
        except InterruptedError:
            raise
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("chat completion cancelled") from exc
            raise
        finally:
            finished.set()
            try:
                stream.close()
            except Exception:
                pass


class DeepSeekLLM(GenericOpenAILLM):
    @property
    def provider_name(self) -> str: return "deepseek"


class GLMLLM(GenericOpenAILLM):
    @property
    def provider_name(self) -> str: return "glm"


class KimiLLM(GenericOpenAILLM):
    @property
    def provider_name(self) -> str: return "kimi"
