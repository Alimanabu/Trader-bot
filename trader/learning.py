"""Цикл обучения. Система учится на журнале, а не «сама по себе».

1. Каждому решению через несколько часов проставляется результат (куда пошла цена).
2. Раз в сутки для нейро-агентов из ошибок формируются «уроки», которые попадают в их промпт.
3. Раз в неделю параметры агентов на правилах перепроверяются бэктестом на свежей истории.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .agents.base import Agent
from .config import Settings
from .journal import Journal
from .llm import ClaudeClient, LLMUnavailable
from .models import Candle
from .research import StrategyLab, backtest

log = logging.getLogger(__name__)

LESSON_SCHEMA = {
    "type": "object",
    "properties": {"lesson": {"type": "string"}},
    "required": ["lesson"],
    "additionalProperties": False,
}


class Learner:
    def __init__(self, settings: Settings, journal: Journal, client: ClaudeClient | None = None):
        self.s = settings
        self.j = journal
        self.client = client
        self.lab = StrategyLab(fee_rate=settings.fee_rate)

    def after_tick(self, agents: list[Agent], candles: list[Candle]) -> None:
        price = candles[-1].close
        ts = candles[-1].ts
        self.j.fill_outcomes(price, ts)
        day_key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if self.j.kv_get("lessons_day") != day_key:
            self.j.kv_set("lessons_day", day_key)
            for a in agents:
                if a.strategy.uses_llm() and a.status != "fired":
                    self._make_lesson(a, ts)
        last_retune = self.j.kv_get("last_retune_ts", 0)
        if ts - last_retune >= self.s.retune_every_hours * 3600:
            self.j.kv_set("last_retune_ts", ts)
            self.retune(agents, candles, ts)

    def _make_lesson(self, agent: Agent, ts: int) -> None:
        mistakes = self.j.mistakes(agent.name, limit=6)
        if not mistakes:
            return
        text = self._lesson_text(agent, mistakes)
        if text:
            self.j.lesson(agent.name, text, ts)
            agent.notes.append(text)
            agent.notes = agent.notes[-20:]
            self.j.event("lesson", f"{agent.name}: {text}", agent.name, ts=ts)

    def _lesson_text(self, agent: Agent, mistakes: list[dict]) -> str:
        lines = [f"{m['action']} по {m['price']:.0f} с обоснованием «{m['reason']}» → цена через 4ч: {m['outcome_pct']:+.2f}%"
                 for m in mistakes]
        if self.client and self.client.enabled:
            try:
                data = self.client.structured(
                    "Ты наставник трейдера. Тебе дают список его ошибочных решений. Сформулируй один короткий "
                    "конкретный урок (одно-два предложения), который поможет не повторять эту ошибку. Без общих слов.",
                    "Ошибки:\n" + "\n".join(lines), LESSON_SCHEMA, max_tokens=1000)
                return data["lesson"].strip()
            except LLMUnavailable as e:
                log.warning("урок без LLM: %s", e)
        buys = sum(1 for m in mistakes if m["action"] == "BUY")
        sells = len(mistakes) - buys
        if buys >= sells:
            return f"Из последних {len(mistakes)} ошибок {buys} — покупки перед падением. Требуй подтверждения тренда перед входом."
        return f"Из последних {len(mistakes)} ошибок {sells} — продажи перед ростом. Не выходи из позиции на первом откате."

    def retune(self, agents: list[Agent], candles: list[Candle], ts: int) -> list[str]:
        """Проверить параметры агентов на правилах на свежей истории и обновить, если есть явное улучшение."""
        changed: list[str] = []
        hist = candles[-self.s.research_lookback:]
        for a in agents:
            if a.strategy.uses_llm() or a.status == "fired":
                continue
            current = backtest(a.strategy, hist, fee_rate=self.s.fee_rate)
            best = self.lab.best_params(a.strategy.family, hist)
            if best.params != a.strategy.params and best.score() > current.score() + 1.0:
                old = dict(a.strategy.params)
                a.strategy.params.update(best.params)
                changed.append(a.name)
                self.j.event("retune", f"{a.name}: параметры {old} → {best.params} (оценка {current.score():.2f} → {best.score():.2f})",
                             a.name, {"old": old, "new": best.params}, ts=ts)
        return changed
