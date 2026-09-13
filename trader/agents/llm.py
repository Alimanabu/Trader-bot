"""Два агента с участием нейросети. Их решения тоже проходят через риск-менеджера."""
from __future__ import annotations

import json
import logging

from ..data import indicators as ind
from ..llm import ClaudeClient, LLMUnavailable
from ..models import Action, Candle, Signal
from .base import Strategy, hold

log = logging.getLogger(__name__)

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "target_exposure": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
        "key_levels": {"type": "array", "items": {"type": "number"}},
        "next_check_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
        "wake_if_above": {"type": "number"},
        "wake_if_below": {"type": "number"},
    },
    "required": ["action", "target_exposure", "confidence", "reason", "key_levels", "next_check_minutes", "wake_if_above", "wake_if_below"],
    "additionalProperties": False,
}


def _summarize(candles: list[Candle], n: int = 48) -> str:
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    r = ind.rsi(closes, 14)[-1]
    e21 = ind.ema(closes, 21)[-1]
    e55 = ind.ema(closes, 55)[-1]
    a = ind.atr(highs, lows, closes, 14)[-1]
    _, _, h = ind.macd(closes)
    recent = candles[-n:]
    rows = [f"{c.ts},{c.open:.0f},{c.high:.0f},{c.low:.0f},{c.close:.0f},{c.volume:.0f}" for c in recent]
    stats = {
        "last_close": closes[-1],
        "change_24h_pct": round((closes[-1] / closes[-25] - 1) * 100, 2) if len(closes) > 25 else None,
        "change_7d_pct": round((closes[-1] / closes[-169] - 1) * 100, 2) if len(closes) > 169 else None,
        "rsi14": None if r is None else round(r, 1),
        "ema21": None if e21 is None else round(e21, 0),
        "ema55": None if e55 is None else round(e55, 0),
        "atr14_pct": None if a is None else round(a / closes[-1] * 100, 2),
        "macd_hist": None if h[-1] is None else round(h[-1], 1),
        "high_7d": max(highs[-168:]),
        "low_7d": min(lows[-168:]),
    }
    return "Сводка:\n" + json.dumps(stats, ensure_ascii=False) + f"\n\nПоследние {n} часовых свечей (ts,open,high,low,close,volume):\n" + "\n".join(rows)


class LLMStrategyBase(Strategy):
    system_prompt = ""

    def __init__(self, params=None, client: ClaudeClient | None = None):
        super().__init__(params)
        self.client = client

    def uses_llm(self) -> bool:
        return True

    def warmup(self):
        return 200

    def cadence_minutes(self):
        return 60   # запасной темп, если модель не назвала свой

    def decide(self, candles, context=None):
        context = context or {}
        if not self.client or not self.client.enabled:
            return hold("LLM выключен (нет ключа API)", context.get("exposure", 0.0))
        lessons = context.get("lessons") or []
        lessons_text = "\n".join(f"- {x}" for x in lessons[-8:]) if lessons else "- пока нет"
        news = context.get("news")
        news_block = f"Сводка новостей:\n{news}\n\n" if news else ""
        lo = int(context.get("llm_min_interval", 15)); hi = int(context.get("llm_max_interval", 360))
        user = (
            f"{news_block}{_summarize(candles)}\n\n"
            f"Текущая доля BTC в портфеле: {context.get('exposure', 0.0):.0%}. Текущая цена: {candles[-1].close:.0f}.\n"
            f"Уроки из твоих прошлых ошибок (из журнала):\n{lessons_text}\n\n"
            + ("Прими решение. Фьючерсы без плеча: target_exposure от -1 (шорт на всё) до 1 (лонг на всё).\n" if context.get("two_sided")
               else "Прими решение. Спот, без плеча, без шортов: target_exposure от 0 до 1.\n")
            + ""
            f"Ты сам выбираешь, когда посмотреть на рынок снова: next_check_minutes от {lo} до {hi}. "
            "В спокойном рынке проверяй реже, при важных уровнях рядом чаще. Дополнительно можешь поставить будильники по цене: "
            "wake_if_above и wake_if_below (0 = не нужен): если цена их пересечёт, тебя разбудят раньше срока."
        )
        schema = DECISION_SCHEMA
        if context.get("two_sided"):
            schema = {**DECISION_SCHEMA, "properties": {**DECISION_SCHEMA["properties"], "target_exposure": {"type": "number", "minimum": -1, "maximum": 1}}}
        try:
            data = self.client.structured(self.system_prompt, user, schema)
        except LLMUnavailable as e:
            log.warning("%s: %s", self.family, e)
            return hold(f"LLM недоступен: {e}", context.get("exposure", 0.0))
        return Signal(Action(data["action"]), data["target_exposure"], data["confidence"], data["reason"],
                      {"key_levels": data.get("key_levels", []), "next_check_minutes": data.get("next_check_minutes", 60),
                       "wake_if_above": data.get("wake_if_above", 0) or 0, "wake_if_below": data.get("wake_if_below", 0) or 0})


class LLMTechnician(LLMStrategyBase):
    family = "llm_technician"
    description = "Нейросеть-технарь: читает свечи и индикаторы, ищет уровни и структуру рынка."
    system_prompt = (
        "Ты трейдер-аналитик, торгуешь BTC/USDT на часовом графике, только спот и только лонг. "
        "Тебе дают сводку индикаторов и последние свечи. Определи структуру рынка (тренд, флэт, "
        "разворот), ближайшие уровни поддержки и сопротивления и реши, какую долю капитала держать в BTC "
        "на следующий час. Будь консервативен: при неопределённости уменьшай долю, а не угадывай. "
        "Учитывай уроки из своих прошлых ошибок. Отвечай строго по схеме."
    )


class LLMRegime(LLMStrategyBase):
    family = "llm_regime"
    description = "Нейросеть-стратег: определяет режим рынка и меняет долю плавно, а не рывками."
    system_prompt = (
        "Ты портфельный стратег по BTC/USDT, горизонт — дни, решения раз в час, только спот и лонг. "
        "Твоя задача — определить режим рынка (рост, падение, боковик, высокая волатильность) и задать "
        "долю капитала в BTC: в устойчивом росте — высокую, в падении — низкую, в боковике — среднюю, "
        "при высокой волатильности — сниженную. Меняй долю плавно, не более чем на 0.3 за один шаг относительно "
        "текущей, чтобы не платить лишние комиссии. Учитывай уроки из прошлых ошибок. Отвечай строго по схеме."
    )


class LLMRegimeBoth(LLMStrategyBase):
    family = "llm_regime_both"
    side = "both"
    description = "Нейросеть-двусторонний: определяет режим рынка и торгует в обе стороны на фьючерсном демосчёте."
    system_prompt = (
        "Ты портфельный стратег по BTC/USDT на бессрочных фьючерсах без плеча, решения раз в час или реже. "
        "Ты можешь держать лонг (target_exposure от 0 до 1), шорт (от -1 до 0) или быть вне рынка (0). "
        "Определи режим рынка: в устойчивом росте держи лонг, в устойчивом падении шорт, в боковике и при высокой "
        "волатильности сокращай позицию до нуля. Учитывай, что за удержание позиции платится финансирование. "
        "Меняй долю плавно, не более чем на 0.5 за один шаг. Учитывай уроки из прошлых ошибок. Отвечай строго по схеме."
    )

    def decide(self, candles, context=None):
        context = dict(context or {})
        context["two_sided"] = True
        return super().decide(candles, context)


class LLMNews(LLMStrategyBase):
    family = "llm_news"
    description = "Нейросеть-новостник: ищет свежие новости по биткоину и оценивает их влияние на ближайшие часы."
    system_prompt = (
        "Ты трейдер по BTC/USDT, решения раз в час, только спот и лонг. Тебе дают краткую сводку свежих "
        "новостей и рыночную сводку. Оцени, есть ли в новостях события, способные сдвинуть цену в ближайшие "
        "часы (регуляторы, ETF, взломы, макростатистика, крупные ликвидации), и задай долю капитала в BTC. "
        "Если новостной фон нейтральный, опирайся на рыночную сводку и держи умеренную долю. "
        "Учитывай уроки из прошлых ошибок. Отвечай строго по схеме."
    )
    search_prompt = (
        "Ты новостной аналитик крипторынка. Найди самые свежие новости о биткоине и крипторынке за последние "
        "сутки (регуляторы, ETF, биржи, макроэкономика, крупные движения). Составь сводку из 5-8 пунктов: "
        "факт, источник, дата, и ожидаемое влияние на цену BTC в ближайшие часы (позитив / негатив / нейтрально). "
        "Никаких рекомендаций, только факты и оценка влияния."
    )

    def decide(self, candles, context=None):
        context = dict(context or {})
        if not self.client or not self.client.enabled:
            return hold("LLM выключен (нет ключа API)", context.get("exposure", 0.0))
        try:
            news = self.client.search_summary(self.search_prompt, "Найди новости по биткоину за последние 24 часа.")
        except LLMUnavailable as e:
            log.warning("%s: поиск новостей недоступен: %s", self.family, e)
            news = "Новости недоступны, опирайся только на рыночную сводку."
        context["news"] = news
        return super().decide(candles, context)


LLM_STRATEGIES: list[type[Strategy]] = [LLMTechnician, LLMRegime, LLMNews, LLMRegimeBoth]
