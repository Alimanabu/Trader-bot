"""Обёртка над Claude API. Единственное место, где система обращается к нейросети.

Все вызовы возвращают строго структурированный JSON (output_config.format),
поэтому агент не может «уговорить» систему свободным текстом.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    pass


class ClaudeClient:
    def __init__(self, api_key: str | None, model: str = "claude-opus-5", effort: str = "medium"):
        self.model = model
        self.effort = effort
        self._client = None
        self._fallbacks_supported = True
        if api_key:
            import anthropic  # импорт здесь, чтобы тесты без ключа не требовали сеть
            self._client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=180.0)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def structured(self, system: str, user: str, schema: dict[str, Any], max_tokens: int = 4000) -> dict[str, Any]:
        """Запрос с гарантированным JSON-ответом по схеме."""
        if not self._client:
            raise LLMUnavailable("ANTHROPIC_API_KEY не задан")
        import anthropic

        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            if self._fallbacks_supported:
                # Серверный запасной вариант: если модель отказала по политике, запрос
                # автоматически повторяется на другой модели внутри того же вызова.
                response = self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                response = self._client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            if self._fallbacks_supported:
                log.warning("Серверные fallbacks не приняты (%s), повторяю без них", e.message)
                self._fallbacks_supported = False
                response = self._client.messages.create(**kwargs)
            else:
                raise
        except anthropic.RateLimitError as e:
            raise LLMUnavailable(f"лимит запросов: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMUnavailable(f"нет связи с API: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMUnavailable(f"ошибка API {e.status_code}: {e.message}") from e

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMUnavailable(f"модель отказалась отвечать: {getattr(details, 'category', None)}")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMUnavailable("пустой ответ модели")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMUnavailable(f"ответ не является JSON: {e}") from e

    def search_summary(self, system: str, user: str, max_searches: int = 3, max_tokens: int = 4000) -> str:
        """Запрос с серверным веб-поиском. Возвращает итоговый текст модели."""
        if not self._client:
            raise LLMUnavailable("ANTHROPIC_API_KEY не задан")
        import anthropic

        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
        try:
            for _ in range(4):
                response = self._client.messages.create(
                    model=self.model, max_tokens=max_tokens, system=system, messages=messages,
                    tools=tools, thinking={"type": "adaptive"}, output_config={"effort": "low"},
                )
                if response.stop_reason == "pause_turn":
                    messages.append({"role": "assistant", "content": response.content})
                    continue
                break
        except anthropic.RateLimitError as e:
            raise LLMUnavailable(f"лимит запросов: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMUnavailable(f"нет связи с API: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMUnavailable(f"ошибка API {e.status_code}: {e.message}") from e
        if response.stop_reason == "refusal":
            raise LLMUnavailable("модель отказалась отвечать")
        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise LLMUnavailable("поиск не вернул текста")
        return text
