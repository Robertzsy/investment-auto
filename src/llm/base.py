from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


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
