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


# Цены за 1 млн токенов (вход, выход, запись кэша, чтение кэша) и за один веб-поиск.
PRICES = {
    "claude-opus-5": (5.0, 25.0, 6.25, 0.5),
    "claude-opus-4-8": (5.0, 25.0, 6.25, 0.5),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 0.2),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.3),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.1),
    "claude-fable-5-1": (10.0, 50.0, 12.5, 1.0),
}
WEB_SEARCH_USD = 0.01


def estimate_cost(model: str, usage) -> float:
    """Оценка стоимости ответа по usage. Если модели нет в таблице, считаем по Opus."""
    pi, po, pcw, pcr = PRICES.get(model, PRICES["claude-opus-5"])
    g = lambda k: float(getattr(usage, k, 0) or 0)  # noqa: E731
    cost = (g("input_tokens") * pi + g("output_tokens") * po
            + g("cache_creation_input_tokens") * pcw + g("cache_read_input_tokens") * pcr) / 1e6
    stu = getattr(usage, "server_tool_use", None)
    if stu is not None:
        cost += float(getattr(stu, "web_search_requests", 0) or 0) * WEB_SEARCH_USD
    return cost


class ClaudeClient:
    def __init__(self, api_key: str | None, model: str = "claude-opus-5", effort: str = "medium",
                 daily_budget_usd: float = 0.0, spend_store=None):
        self.model = model
        self.effort = effort
        self._client = None
        self._fallbacks_supported = True
        self.daily_budget = daily_budget_usd          # 0 = без ограничения
        self.spend_store = spend_store                # объект с kv_get/kv_set (журнал) для учёта по дням
        self._spend_day = ""
        self._spend_usd = 0.0
        self._calls = 0
        if api_key:
            import anthropic  # импорт здесь, чтобы тесты без ключа не требовали сеть
            self._client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=180.0)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # --- учёт расходов ---
    def _today(self) -> str:
        import datetime as _dt
        return _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y-%m-%d")

    def _load_spend(self) -> None:
        day = self._today()
        if day != self._spend_day:
            self._spend_day = day
            rec = self.spend_store.kv_get(f"llm_spend:{day}", None) if self.spend_store else None
            self._spend_usd = float(rec["usd"]) if rec else 0.0
            self._calls = int(rec["calls"]) if rec else 0

    def spend_today(self) -> dict:
        self._load_spend()
        return {"day": self._spend_day, "usd": round(self._spend_usd, 4), "calls": self._calls, "budget": self.daily_budget}

    def _check_budget(self) -> None:
        self._load_spend()
        if self.daily_budget and self._spend_usd >= self.daily_budget:
            raise LLMUnavailable(f"дневной бюджет нейросети {self.daily_budget:.2f} $ исчерпан ({self._spend_usd:.2f} $), ждём завтра")

    def _account(self, response) -> None:
        cost = estimate_cost(self.model, getattr(response, "usage", None))
        self._load_spend()
        self._spend_usd += cost
        self._calls += 1
        if self.spend_store:
            self.spend_store.kv_set(f"llm_spend:{self._spend_day}", {"usd": self._spend_usd, "calls": self._calls})
        log.info("LLM-вызов: %.4f $, за день %.4f $ (%d вызовов)", cost, self._spend_usd, self._calls)

    def structured(self, system: str, user: str, schema: dict[str, Any], max_tokens: int = 4000) -> dict[str, Any]:
        """Запрос с гарантированным JSON-ответом по схеме."""
        if not self._client:
            raise LLMUnavailable("ANTHROPIC_API_KEY не задан")
        self._check_budget()
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

        self._account(response)
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
        self._check_budget()
        import anthropic

        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
        try:
            for _ in range(4):
                response = self._client.messages.create(
                    model=self.model, max_tokens=max_tokens, system=system, messages=messages,
                    tools=tools, thinking={"type": "adaptive"}, output_config={"effort": "low"},
                )
                self._account(response)
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
