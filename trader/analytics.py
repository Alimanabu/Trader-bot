"""Аналитический отдел: нейросеть советует директору и владельцу, но сама не торгует.

- Технический аналитик читает свечи и индикаторы.
- Макро-стратег определяет режим рынка на горизонте дней.
- Новостной аналитик ищет свежие новости через веб-поиск.
- Ревизор раз в неделю разбирает худшие сделки компании и предлагает одно правило для риск-менеджера
  (действует только после одобрения владельца).
- Стратег развития раз в несколько дней смотрит на рынок и статистику компании, пишет наблюдения
  в базу знаний и предлагает изменения стратегий, риска и самого приложения (на решение владельца).

Каждый взгляд аналитика: режим (рост / боковик / падение), уверенность, короткое обоснование. Через сутки
взгляд сверяется с реальным движением цены: так считается точность каждого аналитика. Директор
взвешивает их голоса по точности. Уроки аналитикам проверяются: если точность после урока не выросла,
урок уходит в архив.
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

RULE_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["desk_cap", "no_entry_hours", "family_ban", "max_trades_day", "stop_mult", "cooldown_min", "none"]},
        "desk": {"type": "string", "enum": ["", "bulls", "bears", "both"]},
        "regime": {"type": "string", "enum": ["", "up", "flat", "down"]},
        "family": {"type": "string"},
        "hours": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 23}},
        "cap": {"type": "number", "minimum": 0, "maximum": 1},
        "n": {"type": "integer", "minimum": 0, "maximum": 50},
        "value": {"type": "number", "minimum": 0, "maximum": 10},
        "minutes": {"type": "integer", "minimum": 0, "maximum": 1440},
        "rationale": {"type": "string"},
        "expected_effect": {"type": "string"},
    },
    "required": ["type", "desk", "regime", "family", "hours", "cap", "n", "value", "minutes", "rationale", "expected_effect"],
    "additionalProperties": False,
}

STRATEGY_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {"type": "array", "items": {"type": "string"}},
        "proposals": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["strategy", "risk", "product"]},
                "title": {"type": "string"},
                "details": {"type": "string"},
                "expected_effect": {"type": "string"},
            },
            "required": ["kind", "title", "details", "expected_effect"],
            "additionalProperties": False,
        }},
    },
    "required": ["observations", "proposals"],
    "additionalProperties": False,
}

REVISER_PROMPT = (
    "Ты ревизор торговой компании Botz. Тебе дают худшие закрытые сделки за неделю (деск, семейство стратегии, час входа "
    "по UTC, режим рынка, результат, обоснование) и уже действующие правила. Найди повторяющуюся причину потерь и предложи "
    "ОДНО новое правило для риск-менеджера строго из списка типов: desk_cap (потолок доли деска в режиме: desk, regime, cap), "
    "no_entry_hours (запрет новых входов в часы UTC: hours, при желании desk), family_ban (семейство вне рынка в режиме: family, regime), "
    "max_trades_day (не больше n сделок в день на трейдера), stop_mult (стоп на расстоянии value·ATR), cooldown_min (пауза после стопа minutes). "
    "Незадействованные поля оставь пустыми или нулевыми. Если убедительной причины нет или подходящее правило уже действует, "
    "верни type=none. Правило должно быть проверяемым и объяснимым владельцу простыми словами. rationale и expected_effect на русском, коротко."
)

STRATEGIST_PROMPT = (
    "Ты стратег развития торговой компании Botz: три деска трейдеров на правилах (быки на споте, медведи и двусторонние на "
    "фьючерсах без плеча), стажёры, директор, распределяющий капитал по режиму рынка, аналитики на нейросети, ревизор правил, "
    "отдел исследований с перебором параметров на истории, база знаний с памятью по режимам рынка. Ты не торгуешь. "
    "Тебе дают сводку рынка и новостей, статистику компании и список прошлых предложений с их судьбой. "
    "Задача: 1) записать 2-4 наблюдения о рынке и о том, что в компании работает, а что нет (конкретно, с цифрами из сводки); "
    "2) предложить 1-3 изменения: kind=strategy (новая или изменённая стратегия, параметры, состав десков), kind=risk "
    "(лимиты, правила), kind=product (как изменить само приложение: логику, экраны, процессы). Не повторяй отклонённые и уже "
    "принятые предложения. Каждое предложение: короткий заголовок, что именно сделать, ожидаемый эффект. Всё на русском."
)


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

    def lessons_for(self, name: str, limit: int = 6) -> list[str]:
        rows = self.j.knowledge("lesson", "active", topic=name, limit=limit)
        return [r["text"] for r in reversed(rows)]

    def _ask(self, a: Analyst, candles: list[Candle], price: float, ts: int) -> dict | None:
        lessons = self.lessons_for(a.name, 6)
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

    # --- уроки с проверкой ---
    def verify_lessons(self, ts: int, min_views: int = 5) -> list[dict]:
        """Точность аналитика до урока и после. Урок, после которого точность не выросла, уходит в архив."""
        out = []
        for L in self.j.knowledge("lesson", "active", limit=200):
            n_after, h_after = self.j.analyst_accuracy_between(L["topic"], L["ts"], ts)
            if n_after < min_views:
                continue
            n_before, h_before = self.j.analyst_accuracy_between(L["topic"], L["ts"] - 14 * 86400, L["ts"])
            before = h_before / n_before if n_before else None
            after = h_after / n_after
            if before is not None and after < before:
                self.j.set_knowledge_status(L["id"], "retired", ts)
                self.j.event("lesson", f"{L['topic']}: урок не помог (точность {before:.0%} → {after:.0%}), отправлен в архив", L["topic"], ts=ts)
                out.append({"id": L["id"], "kept": False, "before": before, "after": after})
            else:
                self.j.set_knowledge_status(L["id"], "verified", ts)
                self.j.event("lesson", f"{L['topic']}: урок подтверждён (точность {'—' if before is None else f'{before:.0%}'} → {after:.0%})", L["topic"], ts=ts)
                out.append({"id": L["id"], "kept": True, "before": before, "after": after})
        return out

    # --- ревизор: правила из ошибок ---
    def weekly_rule_proposal(self, trades: list[dict], ts: int) -> dict | None:
        """trades: худшие закрытые сделки недели с полями agent, desk, family, hour, regime, pnl, reason."""
        if not self.enabled or len(trades) < 3:
            return None
        if any(p["kind"] == "rule" for p in self.j.pending_approvals()):
            return None
        active = self.j.active_rules()
        rules_text = "\n".join(f"- {r['text']}" for r in active) or "- пока нет"
        lines = [f"{t['agent']} [{t['desk']}/{t['family']}] вход {t['hour']:02d}:00 UTC, режим {REGIME_RU.get(t['regime'], t['regime'])}, "
                 f"итог {t['pnl']:+.2f} $, обоснование: {t['reason'][:80]}" for t in trades[:12]]
        try:
            d = self.client.structured(REVISER_PROMPT, "Худшие сделки недели:\n" + "\n".join(lines) + "\n\nДействующие правила:\n" + rules_text,
                                       RULE_SCHEMA, max_tokens=1500, strong=True)
        except LLMUnavailable as e:
            log.warning("ревизор: %s", e)
            return None
        if d["type"] == "none":
            self.j.event("reviser", f"Ревизор: новых правил не предлагает. {d['rationale']}", None, ts=ts)
            return None
        data = {k: d[k] for k in ("type", "desk", "regime", "family", "hours", "cap", "n", "value", "minutes")}
        text = describe_rule(data)
        self.j.event("reviser", f"Ревизор предлагает правило: {text}. {d['rationale']}", None, d, ts=ts)
        self.j.request_approval("rule", f"Ревизор предлагает правило: {text}",
                                {"rule": data, "text": text, "rationale": d["rationale"], "expected_effect": d["expected_effect"]}, ts=ts)
        return d

    # --- стратег развития ---
    def strategist_due(self, ts: int) -> bool:
        return self.enabled and int(self.j.kv_get("strategist_next", 0) or 0) <= ts

    def run_strategist(self, candles: list[Candle], price: float, ts: int, company: str) -> dict | None:
        self.j.kv_set("strategist_next", ts + self.s.strategist_interval_h * 3600)
        if not self.enabled:
            return None
        try:
            market = self.client.search_summary(
                "Ты рыночный обозреватель. Найди, что за последние дни обсуждают о биткоине и крипторынке: тренды, ожидания, "
                "риски, новые подходы к торговле. Составь сводку из 5-7 пунктов: факт, источник, дата.",
                "Тренды биткоина и крипторынка за последнюю неделю.")
        except LLMUnavailable as e:
            log.warning("стратег: поиск недоступен: %s", e)
            market = "Сводка рынка недоступна."
        prior = self.j.knowledge("proposal", None, limit=20)
        prior_text = "\n".join(f"- [{p['status']}] {p['text'][:120]}" for p in prior) or "- пока нет"
        user = (f"Сводка рынка:\n{market}\n\n{_summarize(candles, 24)}\n\nСтатистика компании:\n{company}\n\n"
                f"Прошлые предложения (pending = ждёт решения, accepted = принято, rejected = отклонено):\n{prior_text}")
        try:
            d = self.client.structured(STRATEGIST_PROMPT, user, STRATEGY_SCHEMA, max_tokens=3000, strong=True)
        except LLMUnavailable as e:
            log.warning("стратег: %s", e)
            self.j.event("strategist", f"Стратег развития: нейросеть недоступна ({e})", None, ts=ts)
            return None
        for obs in d.get("observations", [])[:4]:
            self.j.add_knowledge(ts, "insight", "стратег", str(obs).strip()[:500], "Стратег развития")
        for p in d.get("proposals", [])[:3]:
            title = str(p["title"]).strip()[:120]
            kid = self.j.add_knowledge(ts, "proposal", p["kind"], f"{title}. {p['details']}".strip()[:1200], "Стратег развития",
                                       {"kind": p["kind"], "title": title, "details": p["details"], "expected_effect": p["expected_effect"]},
                                       status="pending")
            self.j.request_approval("proposal", f"Стратег предлагает ({PROPOSAL_RU.get(p['kind'], p['kind'])}): {title}",
                                    {"knowledge_id": kid, "kind": p["kind"], "details": p["details"], "expected_effect": p["expected_effect"]}, ts=ts)
        self.j.event("strategist", f"Стратег развития: {len(d.get('observations', []))} наблюдений в базу знаний, "
                                   f"{len(d.get('proposals', []))} предложений на ваше решение", None, d, ts=ts)
        return d

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
                        "lessons": self.lessons_for(a.name, 3)})
        return out

    def staff(self) -> dict:
        return {"strategist_next": int(self.j.kv_get("strategist_next", 0) or 0),
                "strategist_runs": len(self.j.events_of(("strategist",), 100)),
                "reviser_runs": len(self.j.events_of(("reviser",), 100))}


PROPOSAL_RU = {"strategy": "стратегия", "risk": "риск", "product": "приложение"}
DESK_RU = {"bulls": "быки", "bears": "медведи", "both": "двусторонние", "": "все дески"}


def describe_rule(d: dict) -> str:
    t = d.get("type")
    reg = f" в режиме «{REGIME_RU.get(d.get('regime'), d.get('regime'))}»" if d.get("regime") else ""
    if t == "desk_cap":
        return f"потолок деска «{DESK_RU.get(d.get('desk'), d.get('desk'))}»{reg} {float(d.get('cap', 1)):.0%}"
    if t == "no_entry_hours":
        hrs = ", ".join(f"{int(h):02d}" for h in d.get("hours", []))
        return f"нет новых входов в {hrs} UTC ({DESK_RU.get(d.get('desk') or '', 'все дески')})"
    if t == "family_ban":
        return f"семейство {d.get('family')} вне рынка{reg}"
    if t == "max_trades_day":
        return f"не больше {d.get('n')} сделок в день на трейдера"
    if t == "stop_mult":
        return f"стоп на расстоянии {float(d.get('value', 2)):.1f}·ATR"
    if t == "cooldown_min":
        return f"пауза {d.get('minutes')} мин после стопа"
    return str(t)
