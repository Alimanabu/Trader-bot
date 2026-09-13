"""Цикл обучения. Система учится на журнале, а не «сама по себе».

1. Каждому решению через несколько часов проставляется результат (куда пошла цена).
2. Раз в сутки для нейро-аналитиков из ошибочных взглядов формируются «уроки», которые попадают в их промпт.
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
from .research import StrategyLab

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
            self.analyst_lessons(ts)
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

    def analyst_lessons(self, ts: int) -> int:
        """Уроки аналитикам по взглядам, которые не подтвердились ценой."""
        n = 0
        for name in {v["analyst"] for v in self.j.latest_views()}:
            mistakes = self.j.analyst_mistakes(name, limit=6)
            if len(mistakes) < 2:
                continue
            last_lesson_ts = self.j.kv_get(f"analyst_lesson_ts:{name}", 0)
            if mistakes[0]["ts"] <= last_lesson_ts:
                continue
            self.j.kv_set(f"analyst_lesson_ts:{name}", mistakes[0]["ts"])
            ru = {"up": "рост", "flat": "боковик", "down": "падение"}
            lines = [f"{ru.get(m['regime'], m['regime'])} с уверенностью {m['confidence']:.0%} («{m['summary'][:120]}») → цена через сутки: {m['outcome_pct']:+.2f}%"
                     for m in mistakes]
            text = ""
            if self.client and self.client.enabled:
                try:
                    data = self.client.structured(
                        "Ты наставник рыночного аналитика. Тебе дают его ошибочные прогнозы. Сформулируй один короткий "
                        "конкретный урок (одно-два предложения), который поможет не повторять эту ошибку. Без общих слов.",
                        "Ошибки:\n" + "\n".join(lines), LESSON_SCHEMA, max_tokens=800)
                    text = data["lesson"].strip()
                except LLMUnavailable as e:
                    log.warning("урок аналитику без LLM: %s", e)
            if not text:
                ups = sum(1 for m in mistakes if m["regime"] == "up")
                downs = sum(1 for m in mistakes if m["regime"] == "down")
                text = (f"Из последних {len(mistakes)} ошибок {ups} — ожидание роста, которого не случилось. Не спеши со ставкой на рост."
                        if ups >= downs else
                        f"Из последних {len(mistakes)} ошибок {downs} — ожидание падения, которого не случилось. Не спеши со ставкой на падение.")
            self.j.add_knowledge(ts, "lesson", name, text, "наставник аналитиков")
            self.j.event("lesson", f"{name}: {text}", name, ts=ts)
            n += 1
        return n

    def retune(self, agents: list[Agent], candles: list[Candle], ts: int) -> list[str]:
        """Проверить параметры агентов на правилах на свежей истории и обновить, если есть явное улучшение."""
        changed: list[str] = []
        hist = candles[-self.s.research_lookback:]
        for a in agents:
            if a.strategy.uses_llm() or a.status == "fired":
                continue
            current = self.lab.evaluate(a.strategy, hist)
            best = self.lab.best_params(a.strategy.family, hist)
            if best.params != a.strategy.params and best.score() > current.score() + 1.0:
                old = dict(a.strategy.params)
                a.strategy.params.update(best.params)
                changed.append(a.name)
                self.j.event("retune", f"{a.name}: параметры {old} → {best.params} (оценка {current.score():.2f} → {best.score():.2f})",
                             a.name, {"old": old, "new": best.params}, ts=ts)
        return changed
