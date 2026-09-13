"""Аналитический отдел: три нейро-аналитика дают директору взгляд на рынок, но сами не торгуют.

- Технический аналитик читает свечи и индикаторы.
- Макро-стратег определяет режим рынка на горизонте дней.
- Новостной аналитик ищет свежие новости через веб-поиск.

Каждый взгляд: режим (рост / боковик / падение), уверенность, короткое обоснование. Через сутки
взгляд сверяется с реальным движением цены: так считается точность каждого аналитика. Директор
взвешивает их голоса по точности и учитывает при распределении капитала между десками.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .agents.llm import _summarize
from .config import Settings
from .journal import Journal
from .llm import ClaudeClient, LLMUnavailable
from .models import Candle

log = logging.getLogger(__name__)

VIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "regime": {"type": "string", "enum": ["up", "flat", "down"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string"},
        "key_levels": {"type": "array", "items": {"type": "number"}},
    },
    "required": ["regime", "confidence", "summary", "key_levels"],
    "additionalProperties": False,
}

REGIME_RU = {"up": "рост", "flat": "боковик", "down": "падение"}


@dataclass
class Analyst:
    key: str
    name: str
    system_prompt: str
    news: bool = False


ANALYSTS = [
    Analyst("technician", "Технический аналитик",
            "Ты технический аналитик по BTC/USDT на часовом графике. Тебе дают сводку индикаторов и последние свечи. "
            "Определи структуру рынка на ближайшие сутки: рост (up), боковик (flat) или падение (down), назови ближайшие "
            "уровни поддержки и сопротивления. Уверенность ставь честно: при неопределённости низкую. Ты не торгуешь, "
            "твой взгляд читает директор, который распределяет капитал между десками быков, медведей и двусторонних. "
            "Учитывай уроки из своих прошлых ошибок. Отвечай строго по схеме, summary на русском, до 300 знаков."),
    Analyst("regime", "Макро-стратег",
            "Ты портфельный стратег по BTC/USDT, горизонт дни. По сводке индикаторов и свечам определи режим рынка на ближайшие "
            "сутки: устойчивый рост (up), боковик или высокая волатильность без направления (flat), устойчивое падение (down). "
            "Меняй мнение только при весомых причинах. Ты не торгуешь, твой взгляд читает директор компании. "
            "Учитывай уроки из прошлых ошибок. Отвечай строго по схеме, summary на русском, до 300 знаков."),
    Analyst("news", "Новостной аналитик",
            "Ты новостной аналитик крипторынка. Тебе дают сводку свежих новостей и рыночную сводку. Оцени, куда новостной фон "
            "толкает цену биткоина в ближайшие сутки: up, flat или down, и с какой уверенностью. Если фон нейтральный, ставь flat "
            "с низкой уверенностью. Ты не торгуешь, твой взгляд читает директор. Отвечай строго по схеме, summary на русском, до 300 знаков.",
            news=True),
]

SEARCH_PROMPT = (
    "Ты новостной аналитик крипторынка. Найди самые свежие новости о биткоине и крипторынке за последние "
    "сутки (регуляторы, ETF, биржи, макроэкономика, крупные движения). Составь сводку из 5-8 пунктов: "
    "факт, источник, дата, и ожидаемое влияние на цену BTC в ближайшие часы (позитив / негатив / нейтрально). "
    "Никаких рекомендаций, только факты и оценка влияния."
)


class AnalyticsDept:
    def __init__(self, settings: Settings, journal: Journal, client: ClaudeClient | None):
        self.s = settings
        self.j = journal
        self.client = client
        self.analysts = list(ANALYSTS)

    @property
    def enabled(self) -> bool:
        return bool(self.client and self.client.enabled)

    def _next_key(self, a: Analyst) -> str:
        return f"analyst_next:{a.key}"

    def due(self, ts: int) -> list[Analyst]:
        return [a for a in self.analysts if int(self.j.kv_get(self._next_key(a), 0) or 0) <= ts]

    def run_due(self, candles: list[Candle], price: float, ts: int) -> list[dict]:
        """Опросить аналитиков, у которых подошло время. Возвращает новые взгляды."""
        self.j.score_views(price, ts)
        if not self.enabled:
            return []
        out: list[dict] = []
        for a in self.due(ts):
            interval = self.s.analyst_news_interval_min if a.news else self.s.analyst_interval_min
            self.j.kv_set(self._next_key(a), ts + interval * 60)
            view = self._ask(a, candles, price, ts)
            if view:
                out.append(view)
        return out

    def _ask(self, a: Analyst, candles: list[Candle], price: float, ts: int) -> dict | None:
        lessons = self.j.lessons_for(a.name, 6)
        lessons_text = "\n".join(f"- {x}" for x in lessons) if lessons else "- пока нет"
        news_block = ""
        if a.news:
            try:
                news = self.client.search_summary(SEARCH_PROMPT, "Найди новости по биткоину за последние 24 часа.")
            except LLMUnavailable as e:
                log.warning("новости недоступны: %s", e)
                news = "Новости недоступны, опирайся только на рыночную сводку."
            news_block = f"Сводка новостей:\n{news}\n\n"
        user = (f"{news_block}{_summarize(candles)}\n\nТекущая цена: {price:.0f}.\n"
                f"Уроки из твоих прошлых ошибок:\n{lessons_text}\n\nДай взгляд на ближайшие 24 часа.")
        try:
            data = self.client.structured(a.system_prompt, user, VIEW_SCHEMA, max_tokens=1500)
        except LLMUnavailable as e:
            log.warning("%s: %s", a.name, e)
            self.j.event("analytics", f"{a.name}: нейросеть недоступна ({e})", a.name, ts=ts)
            return None
        regime = data["regime"]
        conf = float(data["confidence"])
        summary = str(data["summary"]).strip()[:400]
        self.j.add_view(ts, a.name, regime, conf, summary, price)
        self.j.event("analytics", f"{a.name}: {REGIME_RU[regime]} (уверенность {conf:.0%}). {summary}", a.name,
                     {"regime": regime, "confidence": conf, "levels": data.get("key_levels", [])}, ts=ts)
        return {"analyst": a.name, "regime": regime, "confidence": conf, "summary": summary, "ts": ts}

    def consensus(self, ts: int, max_age_h: int = 12) -> dict:
        """Взвешенное мнение отдела: голоса аналитиков по точности и уверенности."""
        stats = self.j.analyst_stats()
        votes = {"up": 0.0, "flat": 0.0, "down": 0.0}
        views = []
        for v in self.j.latest_views():
            age_h = (ts - v["ts"]) / 3600
            st = stats.get(v["analyst"], {})
            acc = st.get("accuracy")
            weight = 0.5 if acc is None or st.get("scored", 0) < 5 else max(0.2, min(1.0, acc))
            fresh = age_h <= max_age_h
            if fresh:
                votes[v["regime"]] += weight * v["confidence"]
            views.append({**v, "weight": round(weight, 2), "fresh": fresh, "accuracy": acc, "scored": st.get("scored", 0)})
        total = sum(votes.values())
        best = max(votes, key=votes.get) if total > 0 else "flat"
        return {"regime": best, "strength": (votes[best] / total) if total > 0 else 0.0,
                "votes": {k: round(v, 2) for k, v in votes.items()}, "views": views, "fresh": total > 0}

    def stats(self) -> list[dict]:
        st = self.j.analyst_stats()
        latest = {v["analyst"]: v for v in self.j.latest_views()}
        out = []
        for a in self.analysts:
            s = st.get(a.name, {"views": 0, "scored": 0, "hits": 0, "accuracy": None})
            v = latest.get(a.name)
            out.append({"key": a.key, "name": a.name, "views": s["views"], "scored": s["scored"], "hits": s["hits"],
                        "accuracy": s["accuracy"], "next_ts": int(self.j.kv_get(self._next_key(a), 0) or 0),
                        "last": {"ts": v["ts"], "regime": v["regime"], "confidence": v["confidence"], "summary": v["summary"],
                                 "outcome_pct": v["outcome_pct"], "hit": v["hit"]} if v else None,
                        "lessons": self.j.lessons_for(a.name, 3)})
        return out
