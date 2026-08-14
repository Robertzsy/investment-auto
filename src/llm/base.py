from __future__ import annotations

from abc import ABC, abstractmethod
import threading
from typing import Any, Dict, Iterator, List, Optional


class BaseLLM(ABC):
    """Unified LLM interface – all providers support OpenAI-compatible ChatCompletions."""

    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> str:
        """Return the assistant text content from a chat-completion call."""
        ...

    def chat_tools(
        self,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """OpenAI-style completion returning content and tool_calls.

        The default implementation only produces text and no tool calls;
        providers with a native function-calling transport override it.
        """
        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens, **kwargs)
        return {"content": text, "tool_calls": None}

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        cancel_event: Optional[threading.Event] = None,
        **kwargs: Any,
    ) -> Iterator[str]:
        """Yield assistant text, with a non-streaming fallback for providers.

        Providers with a streaming transport should override this method so a
        cancellation event can also interrupt an in-flight network read.
        """
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("chat completion cancelled")
        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens, **kwargs)
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("chat completion cancelled")
        if text:
            yield text

    def chat_structured(
        self,
        messages: List[Dict[str, str]],
        schema: Dict[str, Any],
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Fallback: call chat and attempt naive JSON extraction. Override for native structured output."""
        import json

        text = self.chat(messages, **kwargs)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # try extracting first JSON brace block
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(text[start:end])
            raise
