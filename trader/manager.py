"""Директор компании Botz: распределяет капитал между десками, следит за трейдерами и стажёрами,
увольняет, нанимает, повышает, пишет отчёт и сам отвечает за результат.

Устройство:
- Три деска: быки (спот, рост), медведи (шорт), двусторонние. На каждом desk_size трейдеров.
- Стажёры (intern_count) торгуют в тени на своих демосчетах, распределены по дескам.
- Отдел исследований наполняет скамейку кандидатов; из неё берутся стажёры.
- Карьерная лестница: кандидат → стажёр → трейдер → старший трейдер → реальный счёт.
- Директор раз в день оценивает режим рынка (свои правила плюс голоса аналитического отдела) и
  выставляет потолок доли для каждого деска. Раз в неделю его распределение сравнивается с равным
  («если бы всем дали 100%»), а компания с «держать доллары» и «держать биткоин».
- База знаний компании: каждый день результат каждого трейдера и стажёра записывается в память его
  семейства и деска под текущий режим рынка. Память живёт дольше людей: директор режет капитал деску,
  который исторически теряет в текущем режиме, и при найме предпочитает семейства с хорошей памятью.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .agents.base import Agent
from .agents.registry import (DESKS, RANK_INTERN, RANK_LIVE, RANK_SENIOR, RANK_TRADER, RANK_LABELS, build_strategy,
                              desk_of, family_label, family_side, new_account)
from .config import Settings
from .journal import Journal
from .llm import ClaudeClient, LLMUnavailable
from .models import Candle
from .research import StrategyLab, combo_key, result_to_dict
from .data import indicators as ind

log = logging.getLogger(__name__)

TENURE_DAYS = 14
TEAM_STATUSES = {"active", "paused"}
DESK_KEYS = list(DESKS)

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "best_agent": {"type": "string"},
        "worst_agent": {"type": "string"},
        "recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "best_agent", "worst_agent", "recommendations"],
    "additionalProperties": False,
}

REGIME_RU = {"up": "рост", "flat": "боковик", "down": "падение", "off": "выключено"}


def is_team(a: Agent) -> bool:
    return a.status in TEAM_STATUSES


def is_intern(a: Agent) -> bool:
    return a.status == "intern"


DIRECTOR_STYLES = {
    "balanced": "сбалансированный",
    "aggressive": "агрессивный",
    "cautious": "осторожный",
}
STYLE_ORDER = ["balanced", "aggressive", "cautious"]


class Director:
    # распределение капитала по режиму рынка: потолок доли для каждого деска (стиль «сбалансированный»)
    ALLOC = {
        "balanced": {
            "up": {"bulls": 1.0, "bears": 0.3, "both": 0.8},
            "flat": {"bulls": 0.6, "bears": 0.6, "both": 1.0},
            "down": {"bulls": 0.3, "bears": 1.0, "both": 0.8},
        },
        "defensive": {
            "up": {"bulls": 0.6, "bears": 0.2, "both": 0.5},
            "flat": {"bulls": 0.4, "bears": 0.4, "both": 0.6},
            "down": {"bulls": 0.2, "bears": 0.6, "both": 0.5},
        },
    }

    # другие стили директоров: агрессивный держит больше в направлении режима, осторожный меньше везде
    ALLOC_STYLES = {
        "balanced": ALLOC,
        "aggressive": {
            "balanced": {"up": {"bulls": 1.0, "bears": 0.2, "both": 1.0}, "flat": {"bulls": 0.8, "bears": 0.8, "both": 1.0},
                         "down": {"bulls": 0.2, "bears": 1.0, "both": 1.0}},
            "defensive": {"up": {"bulls": 0.7, "bears": 0.2, "both": 0.6}, "flat": {"bulls": 0.5, "bears": 0.5, "both": 0.7},
                          "down": {"bulls": 0.2, "bears": 0.7, "both": 0.6}},
        },
        "cautious": {
            "balanced": {"up": {"bulls": 0.7, "bears": 0.2, "both": 0.6}, "flat": {"bulls": 0.4, "bears": 0.4, "both": 0.7},
                         "down": {"bulls": 0.2, "bears": 0.7, "both": 0.6}},
            "defensive": {"up": {"bulls": 0.5, "bears": 0.1, "both": 0.4}, "flat": {"bulls": 0.3, "bears": 0.3, "both": 0.5},
                          "down": {"bulls": 0.1, "bears": 0.5, "both": 0.4}},
        },
    }

    def __init__(self, settings: Settings, journal: Journal, client: ClaudeClient | None = None, team_size: int | None = None):
        self.s = settings
        self.j = journal
        self.client = client
        self.desk_size = max(1, (team_size or settings.team_size) // 3)
        self.team_size = self.desk_size * 3
        self.lab = StrategyLab(fee_rate=settings.fee_rate)

    # --- личность директора: стиль, рейтинг, премия ---
    def new_director(self, number: int, style: str, ts: int) -> dict:
        return {"number": number, "name": f"Директор №{number}", "style": style, "style_ru": DIRECTOR_STYLES[style],
                "since_ts": ts, "rating": 50, "bonus": 0.0, "weeks": 0, "low_weeks": 0, "best_rating": 50}

    def director(self) -> dict:
        pol = self.policy()
        d = pol.get("director")
        if not d:
            d = self.new_director(1, "balanced", int(pol.get("changed_ts") or 0))
            pol["director"] = d
            self._save_policy(pol)
        return d

    def alloc(self) -> dict:
        style = (self.policy().get("director") or {}).get("style", "balanced")
        return self.ALLOC_STYLES.get(style, self.ALLOC)

    def rate_week(self, pol: dict, company_pct: float, equal_pct: float, btc_pct: float, pnl: float, equal_pnl: float, ts: int) -> dict:
        """Рейтинг 0–100 и премия директора по итогам недели.

        Рейтинг: +10 если распределение лучше равного (иначе −10), +10 если компания в плюсе (иначе −10),
        +5 если компания не хуже биткоина (иначе −5).
        Премия: director_bonus_pct % от прибыли недели плюс столько же от выигрыша над равным распределением
        (только в прибыльную неделю); при убытке штраф в половину ставки. Премия виртуальная: из капитала
        компании не вычитается, это счёт мотивации.
        """
        d = pol.get("director") or self.new_director(1, "balanced", ts)
        delta = (10 if company_pct >= equal_pct - 1e-9 else -10) + (10 if company_pct > 0 else -10) + (5 if company_pct >= btc_pct else -5)
        d["rating"] = int(max(0, min(100, d.get("rating", 50) + delta)))
        d["best_rating"] = max(d.get("best_rating", 50), d["rating"])
        rate = self.s.director_bonus_pct / 100.0
        bonus = 0.0
        if pnl > 0:
            bonus += rate * pnl
            if pnl - equal_pnl > 0:                 # премия за распределение только в прибыльную неделю
                bonus += rate * (pnl - equal_pnl)
        if pnl < 0:
            bonus -= rate / 2 * (-pnl)
        d["bonus"] = round(d.get("bonus", 0.0) + bonus, 2)
        d["bonus_week"] = round(bonus, 2)
        d["weeks"] = d.get("weeks", 0) + 1
        d["low_weeks"] = d.get("low_weeks", 0) + 1 if d["rating"] < 30 else 0
        d["rating_delta"] = delta
        pol["director"] = d
        if d["low_weeks"] >= self.s.director_fail_weeks and not any(p["kind"] == "director" for p in self.j.pending_approvals()):
            nxt = STYLE_ORDER[(STYLE_ORDER.index(d["style"]) + 1) % len(STYLE_ORDER)] if d["style"] in STYLE_ORDER else "balanced"
            self.j.request_approval("director", f"{d['name']} ({d['style_ru']}): рейтинг {d['rating']} уже {d['low_weeks']} нед. Сменить директора?",
                                    {"number": d["number"], "next_style": nxt, "next_style_ru": DIRECTOR_STYLES[nxt], "rating": d["rating"], "bonus": d["bonus"]}, ts=ts)
        return d

    def replace_director(self, ts: int, next_style: str | None = None) -> dict:
        """Смена директора: старый уходит в базу знаний, новый приходит со своим стилем и рейтингом 50."""
        pol = self.policy()
        old = pol.get("director") or self.new_director(1, "balanced", ts)
        style = next_style or STYLE_ORDER[(STYLE_ORDER.index(old["style"]) + 1) % len(STYLE_ORDER)]
        self.j.add_knowledge(ts, "director", old["name"],
                             f"{old['name']} ({old['style_ru']}) работал {max(0, (ts - old['since_ts']) // 86400)} дн., {old.get('weeks', 0)} нед.; "
                             f"итоговый рейтинг {old['rating']}, лучший {old.get('best_rating', old['rating'])}, премия {old['bonus']:+.2f} $",
                             "смена директора", {**old, "left_ts": ts}, status="retired")
        new = self.new_director(old["number"] + 1, style, ts)
        pol["director"] = new
        pol["mode"] = "balanced"
        pol["fail_weeks"] = 0
        pol["good_weeks"] = 0
        regime = pol.get("regime", "flat")
        pol["caps"] = dict(self.ALLOC_STYLES[style]["balanced"].get(regime, self.ALLOC_STYLES[style]["balanced"]["flat"]))
        self._save_policy(pol)
        self.j.event("head", f"Смена директора: {old['name']} ({old['style_ru']}, рейтинг {old['rating']}) уходит, "
                             f"приходит {new['name']} ({new['style_ru']}). База знаний, память и правила остаются в компании", None, new, ts=ts)
        return new

    # --- политика директора ---
    def policy(self) -> dict:
        pol = self.j.kv_get("head_policy", None) or {}
        base = {
            "mode": "balanced", "regime": "flat", "rule_regime": "flat", "caps": dict(self.ALLOC["balanced"]["flat"]),
            "min_rebalance": 0.05, "fail_weeks": 0, "good_weeks": 0, "weeks": [], "changed_ts": 0,
            "alloc_wins": 0, "alloc_weeks": 0, "analysts": None, "week_caps": None,
        }
        base.update(pol)
        if "caps" not in pol:      # старая политика с cap / cap_short
            base["caps"] = {"bulls": float(pol.get("cap", 1.0)), "bears": float(pol.get("cap_short", pol.get("cap", 1.0))), "both": 1.0}
        return base

    def _save_policy(self, pol: dict) -> None:
        self.j.kv_set("head_policy", pol)

    def assess_regime(self, candles: list[Candle]) -> str:
        closes = [c.close for c in candles]
        if len(closes) < 210:
            return "flat"
        s50, s200 = ind.sma(closes, 50)[-1], ind.sma(closes, 200)[-1]
        s200_prev = ind.sma(closes, 200)[-25]
        if s50 is None or s200 is None or s200_prev is None:
            return "flat"
        price = closes[-1]
        if price > s50 > s200 and s200 >= s200_prev:
            return "up"
        if price < s50 < s200 and s200 <= s200_prev:
            return "down"
        return "flat"

    @staticmethod
    def combine(rule: str, consensus: dict | None, threshold: float = 0.65) -> tuple[str, str]:
        """Итоговый режим: правила директора плюс уверенный консенсус аналитиков.

        - Правила говорят «боковик», а аналитики уверенно за рост или падение → берём их сторону.
        - Правила говорят рост/падение, а аналитики уверенно против → боковик (осторожность).
        Возвращает (режим, источник).
        """
        if not consensus or not consensus.get("fresh") or consensus.get("strength", 0) < threshold:
            return rule, "правила"
        a = consensus["regime"]
        if rule == "flat" and a in {"up", "down"}:
            return a, "аналитики"
        if rule in {"up", "down"} and a in {"up", "down"} and a != rule:
            return "flat", "спор правил и аналитиков"
        return rule, "правила и аналитики согласны" if a == rule else "правила"

    def daily_policy(self, candles: list[Candle], ts: int, consensus: dict | None = None) -> dict:
        """Оценить рынок и распределить капитал между десками."""
        self.director()
        pol = self.policy()
        if not self.s.head_policy:
            pol.update({"caps": {k: 1.0 for k in DESK_KEYS}, "regime": "off"})
            self._save_policy(pol)
            return pol
        rule = self.assess_regime(candles)
        regime, source = self.combine(rule, consensus)
        caps = dict(self.alloc()[pol["mode"]][regime])
        memory_adj = self.memory_adjust(caps, regime)
        if regime != pol.get("regime") or caps != pol.get("caps"):
            desc = ", ".join(f"{DESKS[k]['label'].lower()} {caps[k]:.0%}" for k in DESK_KEYS)
            mem = ("; память компании: " + ", ".join(memory_adj)) if memory_adj else ""
            self.j.event("head", f"Директор: рынок — {REGIME_RU[regime]} (источник: {source}). Капитал по дескам: {desc} "
                                 f"(подход: {'обычный' if pol['mode'] == 'balanced' else 'защитный'}){mem}", None,
                         {"regime": regime, "rule_regime": rule, "caps": caps, "mode": pol["mode"], "source": source, "memory": memory_adj}, ts=ts)
        pol.update({"regime": regime, "rule_regime": rule, "caps": caps, "changed_ts": ts, "source": source, "memory_adj": memory_adj,
                    "analysts": {k: consensus[k] for k in ("regime", "strength", "votes")} if consensus else None,
                    "regime_price": float(candles[-1].close) if candles else pol.get("regime_price"), "intraday": None})
        self.record_regime_day(regime, ts)
        if not pol.get("week_caps"):
            pol["week_caps"] = dict(caps)
        self._save_policy(pol)
        return pol

    # --- база знаний: память по режимам ---
    def record_regime_day(self, regime: str, ts: int) -> None:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        logd = self.j.kv_get("regime_log", {}) or {}
        logd[day] = regime
        if len(logd) > 90:
            for k in sorted(logd)[:-90]:
                logd.pop(k, None)
        self.j.kv_set("regime_log", logd)

    def regime_for_day(self, day: str) -> str:
        logd = self.j.kv_get("regime_log", {}) or {}
        if day in logd:
            return logd[day]
        return self.policy().get("regime", "flat")

    def remember_day(self, results: list[tuple[Agent, float]], regime: str, ts: int) -> int:
        """Итог дня каждого трейдера и стажёра (в % от капитала на утро) → память семейства и деска."""
        n = 0
        desk_sum: dict[str, list[float]] = {}
        for a, pct in results:
            self.j.memory_add("family", a.strategy.family, regime, pct, ts)
            desk_sum.setdefault(a.desk, []).append(pct)
            n += 1
        for d, vals in desk_sum.items():
            self.j.memory_add("desk", d, regime, sum(vals) / len(vals), ts)
        return n

    def memory_bias(self, family: str, regime: str) -> int:
        """+1 семейство исторически зарабатывает в этом режиме, −1 теряет, 0 данных мало."""
        m = self.j.memory_get("family", family, regime)
        if not m or m["days"] < self.s.memory_min_days:
            return 0
        return 1 if m["avg"] > 0 else -1

    def memory_adjust(self, caps: dict[str, float], regime: str) -> list[str]:
        """Деск, который в этом режиме исторически теряет, получает на 30% меньше; стабильно зарабатывающий — до полного."""
        notes = []
        for d in DESK_KEYS:
            m = self.j.memory_get("desk", d, regime)
            if not m or m["days"] < self.s.memory_min_days:
                continue
            if m["avg"] < 0 and m["losses"] >= m["wins"]:
                caps[d] = round(caps[d] * 0.7, 2)
                notes.append(f"{DESKS[d]['label'].lower()} в этом режиме теряли {m['days']} дн. из памяти (в среднем {m['avg']:+.2f}%/день), потолок урезан до {caps[d]:.0%}")
            elif m["avg"] > 0.1 and m["wins"] > m["losses"] and caps[d] < 1.0:
                caps[d] = round(min(1.0, caps[d] + 0.2), 2)
                notes.append(f"{DESKS[d]['label'].lower()} в этом режиме стабильно в плюсе ({m['avg']:+.2f}%/день за {m['days']} дн.), потолок поднят до {caps[d]:.0%}")
        return notes

    def company_summary(self, agents: list[Agent], price: float, analysts: list[dict], ts: int) -> str:
        """Сводка для стратега развития: дески, память, аналитики, правила."""
        pol = self.policy()
        d = pol.get("director") or {}
        lines = [f"Режим рынка сейчас: {REGIME_RU.get(pol.get('regime'), '?')}, подход {pol.get('mode')}, потолки {pol.get('caps')}. "
                 f"{d.get('name', 'Директор')} ({d.get('style_ru', '')}): рейтинг {d.get('rating', 50)}, премия {d.get('bonus', 0):+.2f} $ за {d.get('weeks', 0)} нед."]
        for w in (pol.get("weeks") or [])[-4:]:
            lines.append(f"Неделя {datetime.fromtimestamp(w['ts'], tz=timezone.utc):%d.%m}: компания {w['dept']:+.2f}%, равное распределение {w.get('equal', 0):+.2f}%, "
                         f"биткоин {w['btc']:+.2f}%, дески " + ", ".join(f"{k} {v['pct']:+.2f}%" for k, v in (w.get("desks") or {}).items()))
        for d in DESK_KEYS:
            members = self.desk_members(agents, d)
            best = max(members, key=lambda a: a.pnl_total(price), default=None)
            worst = min(members, key=lambda a: a.pnl_total(price), default=None)
            lines.append(f"Деск {DESKS[d]['label']}: {len(members)} трейдеров, суммарно {sum(a.pnl_total(price) for a in members):+.2f} $"
                         + (f", лучший {best.name} ({best.strategy.family}) {best.pnl_total(price):+.2f} $, худший {worst.name} ({worst.strategy.family}) {worst.pnl_total(price):+.2f} $" if best else ""))
        mem = [m for m in self.j.memory_table("family") if m["days"] >= 5]
        mem.sort(key=lambda m: m["avg"], reverse=True)
        if mem:
            lines.append("Память по режимам (семейство/режим: средний % в день, дней): " +
                         "; ".join(f"{m['key']}/{m['regime']} {m['avg']:+.2f}% ({m['days']})" for m in mem[:8] + mem[-4:]))
        for a in analysts:
            acc = a.get("accuracy")
            lines.append(f"Аналитик {a['name']}: точность {'—' if acc is None else f'{acc:.0%}'} на {a.get('scored', 0)} взглядах")
        rules = self.j.active_rules()
        lines.append("Действующие правила: " + ("; ".join(r["text"] for r in rules) if rules else "нет"))
        ev = self.j.event_counts(ts - 7 * 86400)
        lines.append(f"За неделю: стопов {ev.get('stop', 0)}, ликвидаций {ev.get('liquidation', 0)}, увольнений {ev.get('fire', 0)}, "
                     f"отчислений стажёров {ev.get('drop', 0)}, повышений {ev.get('hire', 0)}")
        return "\n".join(lines)

    def intraday_check(self, price: float, ts: int) -> dict | None:
        """Внутридневной пересмотр: цена ушла против режима больше чем на intraday_move_pct.

        - Режим «рост», а цена упала, или «падение», а цена выросла → до утра нейтральное распределение (боковик).
        - Режим «боковик», а цена уверенно пошла в сторону → распределение под это направление.
        Не чаще раза в intraday_cooldown_h часов. Утром директор пересматривает всё заново.
        """
        if not self.s.head_policy:
            return None
        pol = self.policy()
        base = float(pol.get("regime_price") or 0)
        regime = pol.get("regime", "flat")
        if not base or not price or regime not in {"up", "flat", "down"}:
            return None
        last = int((pol.get("intraday") or {}).get("ts") or 0)
        if ts - last < self.s.intraday_cooldown_h * 3600:
            return None
        move = (price / base - 1) * 100
        thr = self.s.intraday_move_pct
        new = None
        if regime == "up" and move <= -thr or regime == "down" and move >= thr:
            new = "flat"
        elif regime == "flat" and abs(move) >= thr:
            new = "up" if move > 0 else "down"
        if not new:
            return None
        caps = dict(self.alloc()[pol["mode"]][new])
        self.memory_adjust(caps, new)
        desc = ", ".join(f"{DESKS[k]['label'].lower()} {caps[k]:.0%}" for k in DESK_KEYS)
        info = {"ts": ts, "from": regime, "to": new, "move": round(move, 2), "price": price}
        pol.update({"regime": new, "caps": caps, "source": "внутридневной пересмотр", "regime_price": price, "intraday": info, "changed_ts": ts})
        self._save_policy(pol)
        self.j.event("head", f"Директор, внутридневной пересмотр: цена ушла от {base:.0f} до {price:.0f} ({move:+.1f}%) против режима "
                             f"«{REGIME_RU[regime]}». До утра режим «{REGIME_RU[new]}», капитал по дескам: {desc}", None, info, ts=ts)
        return info

    def desk_members(self, agents: list[Agent], desk: str, interns: bool = False) -> list[Agent]:
        return [a for a in agents if a.desk == desk and (is_intern(a) if interns else is_team(a))]

    def weekly_policy(self, agents: list[Agent], candles: list[Candle], price: float, ts: int) -> dict:
        """KPI директора за неделю: компания против долларов и биткоина, распределение против равного."""
        pol = self.policy()
        team = [a for a in agents if is_team(a) and a.week_start_equity > 0]
        if not team:
            return pol
        start = sum(a.week_start_equity for a in team)
        pnl = sum(a.equity(price) - a.week_start_equity for a in team)
        company_pct = pnl / start * 100 if start else 0.0
        week_ago = [c for c in candles if c.ts <= ts - 7 * 86400]
        btc_pct = (price / week_ago[-1].close - 1) * 100 if week_ago else 0.0
        fees = self.j.fees_since(ts - 7 * 86400, {a.name for a in team})
        loss = max(0.0, -pnl)
        fee_share = fees / loss if loss > 0 else 0.0
        # по дескам и оценка «равного распределения»: результат деска делим на выданный ему потолок
        week_caps = pol.get("week_caps") or pol["caps"]
        desks: dict[str, dict] = {}
        equal_pnl = 0.0
        for d in DESK_KEYS:
            members = [a for a in team if a.desk == d]
            d_start = sum(a.week_start_equity for a in members)
            d_pnl = sum(a.equity(price) - a.week_start_equity for a in members)
            cap = max(0.1, float(week_caps.get(d, 1.0)))
            equal_pnl += d_pnl / cap
            bench = btc_pct if d == "bulls" else (-btc_pct if d == "bears" else 0.0)
            desks[d] = {"pct": round(d_pnl / d_start * 100, 2) if d_start else 0.0, "bench": round(bench, 2),
                        "cap": cap, "agents": len(members), "pnl": round(d_pnl, 2)}
        equal_pct = equal_pnl / start * 100 if start else 0.0
        beat_cash = company_pct > 0
        beat_equal = company_pct >= equal_pct - 1e-9
        pol["alloc_weeks"] = pol.get("alloc_weeks", 0) + 1
        pol["alloc_wins"] = pol.get("alloc_wins", 0) + (1 if beat_equal else 0)
        pol["weeks"] = (pol.get("weeks") or [])[-11:] + [{"ts": ts, "dept": round(company_pct, 2), "equal": round(equal_pct, 2),
                                                          "btc": round(btc_pct, 2), "fees": round(fees, 2), "desks": desks}]
        pol["fail_weeks"] = 0 if beat_cash else pol.get("fail_weeks", 0) + 1
        pol["good_weeks"] = pol.get("good_weeks", 0) + 1 if beat_cash else 0
        notes = [f"компания {company_pct:+.2f}% за неделю, при равном распределении было бы {equal_pct:+.2f}%, "
                 f"биткоин {btc_pct:+.2f}%, комиссии {fees:.2f} $"]
        notes.append("; ".join(f"{DESKS[d]['label'].lower()} {desks[d]['pct']:+.2f}% (ориентир {desks[d]['bench']:+.2f}%)" for d in DESK_KEYS))
        notes.append("распределение директора помогло" if beat_equal else "распределение директора помешало")
        new_min = 0.10 if fee_share > 0.5 else 0.05
        if abs(new_min - pol.get("min_rebalance", 0.05)) > 1e-9:
            notes.append("комиссии съедают больше половины потерь: торгуем реже" if new_min > 0.05 else "порог сделок возвращён к обычному")
        pol["min_rebalance"] = new_min
        if pol["fail_weeks"] >= self.s.head_fail_weeks and pol["mode"] == "balanced":
            pol["mode"] = "defensive"
            notes.append(f"{pol['fail_weeks']} недели подряд хуже долларов: перехожу на защитный подход и переобучаю всех")
            self.retrain(agents, candles, ts, all_agents=True)
        elif pol["good_weeks"] >= 2 and pol["mode"] == "defensive":
            pol["mode"] = "balanced"
            notes.append("две недели в плюсе: возвращаю обычный подход")
        regime = pol.get("regime", "flat")
        table = self.alloc()[pol["mode"]]
        pol["caps"] = dict(table[regime]) if self.s.head_policy and regime in table else {k: 1.0 for k in DESK_KEYS}
        pol["week_caps"] = dict(pol["caps"])
        d = self.rate_week(pol, company_pct, equal_pct, btc_pct, pnl, equal_pnl, ts)
        notes.append(f"рейтинг директора {d['rating']} ({d['rating_delta']:+d}), премия за неделю {d['bonus_week']:+.2f} $, всего {d['bonus']:+.2f} $")
        self._save_policy(pol)
        self.j.event("head", "Отчёт директора за неделю: " + "; ".join(notes), None, pol, ts=ts)
        return pol

    def retrain(self, agents: list[Agent], candles: list[Candle], ts: int, all_agents: bool = False, only: list[Agent] | None = None) -> int:
        """Переобучение: заново подобрать параметры под последние 30 дней."""
        hist = candles[-self.s.research_lookback:]
        targets = only if only is not None else [a for a in agents if a.status not in {"fired", "dropped"} and (all_agents or is_team(a))]
        n = 0
        for a in targets:
            if a.strategy.uses_llm():
                continue
            try:
                best = self.lab.best_params(a.strategy.family, hist)
            except Exception:  # noqa: BLE001
                continue
            if best.params != a.strategy.params:
                old = dict(a.strategy.params)
                a.strategy.params.update(best.params)
                self.j.event("retune", f"{a.name}: переобучен, параметры {old} → {best.params}", a.name, ts=ts)
                n += 1
        return n

    # --- увольнение и отчисление ---
    def fire(self, agent: Agent, price: float, ts: int, reason: str) -> None:
        agent.account.flatten(price, ts, f"увольнение: {reason}")
        agent.status = "fired"
        self.j.set_status(agent.name, "fired", ts)
        self.j.event("fire", f"{agent.name} уволен: {reason}", agent.name, {"equity": agent.equity(price)}, ts=ts)
        log.info("Уволен %s: %s", agent.name, reason)

    def drop_intern(self, agent: Agent, price: float, ts: int, reason: str) -> None:
        agent.account.flatten(price, ts, f"отчисление: {reason}")
        agent.status = "dropped"
        self.j.set_status(agent.name, "dropped", ts)
        self.j.event("drop", f"Стажёр {agent.name} отчислен: {reason}", agent.name, ts=ts)

    def demote(self, a: Agent, price: float, ts: int, why: str, candles: list[Candle] | None = None) -> None:
        """Трейдер → стажёр на испытательный срок со свежим счётом."""
        a.account.flatten(price, ts, "перевод в стажёры")
        a.reset_account(self.s.agent_start_balance, ts)
        if candles:
            self.retrain([a], candles, ts, only=[a])
        a.status = "intern"
        a.rank = RANK_INTERN
        a.streak_weeks = 0
        a.trial_weeks = 1
        a.live_ready = False
        self.j.save_agent(a)
        self.j.event("demote", f"{a.name} переведён в стажёры: {why}", a.name, ts=ts)

    # --- скамейка кандидатов ---
    def _taken(self, agents: list[Agent]) -> set[str]:
        return {combo_key(a.strategy.family, a.strategy.params) for a in agents if a.status not in {"fired", "dropped"}}

    def refresh_bench(self, candles: list[Candle], ts: int, agents: list[Agent] | None = None) -> int:
        hist = candles[-self.s.research_lookback:]
        results = self.lab.research(hist, top_n=self.s.bench_size, exclude=self._taken(agents or []))
        self.j.clear_bench()
        for r in results:
            self.j.add_bench(r.family, r.params, r.score(), result_to_dict(r), ts=ts)
        self.j.event("research", f"Отдел исследований проверил {len(self.lab.families_count())} семейств и отобрал {len(results)} кандидатов",
                     None, {"candidates": [result_to_dict(r) for r in results[:10]]}, ts=ts)
        return len(results)

    def _take_candidate(self, agents: list[Agent], desk: str | None = None) -> dict | None:
        """Лучший кандидат со скамейки для деска (или любого), ещё не работающий в компании."""
        taken = self._taken(agents)
        for r in self.j.bench():
            if desk and desk_of(family_side(r["strategy"])) != desk:
                continue
            key = combo_key(r["strategy"], r["params"])
            self.j.mark_bench_used(r["id"])
            if key in taken:
                continue
            return r
        return None

    # --- имена ---
    def _unique_name(self, family: str, existing: list[Agent]) -> str:
        base = family_label(family)
        taken = {a.name for a in existing} | {r["name"] for r in self.j.all_agents()}
        name, n = base, 2
        while name in taken:
            name = f"{base} #{n}"
            n += 1
        return name

    def _create(self, family: str, params: dict, existing: list[Agent], ts: int, status: str) -> Agent:
        name = self._unique_name(family, existing)
        strat = build_strategy(family, params, self.client)
        agent = Agent(name=name, strategy=strat, account=new_account(self.s, name, allow_short=strat.side != "long"), hired_at=ts, status=status,
                      rank=RANK_INTERN if status == "intern" else RANK_TRADER)
        self.j.save_agent(agent)
        return agent

    # --- стажёры ---
    def fill_interns(self, agents: list[Agent], candles: list[Candle], ts: int) -> list[Agent]:
        """Добрать стажёров до intern_count из скамейки кандидатов, по очереди для каждого деска."""
        interns = [a for a in agents if is_intern(a)]
        need = self.s.intern_count - len(interns)
        added: list[Agent] = []
        if need <= 0:
            return added
        if not self.j.bench():
            self.refresh_bench(candles, ts, agents)
        counts = {d: len([a for a in interns if a.desk == d]) for d in DESK_KEYS}
        misses = 0
        while len(added) < need and misses < len(DESK_KEYS):
            desk = min(DESK_KEYS, key=lambda d: counts[d])       # деск, где стажёров меньше всего
            cand = self._take_candidate(agents + added, desk) or None
            if not cand:
                misses += 1
                counts[desk] += 10 ** 6            # этот деск больше не пробуем
                continue
            a = self._create(cand["strategy"], cand["params"], agents + added, ts, "intern")
            added.append(a)
            counts[desk] += 1
        if len(added) < need:                       # добираем чем угодно
            while len(added) < need:
                cand = self._take_candidate(agents + added)
                if not cand:
                    break
                added.append(self._create(cand["strategy"], cand["params"], agents + added, ts, "intern"))
        if added:
            self.j.event("intern", f"Набрано стажёров: {len(added)} ({', '.join(a.name for a in added[:6])}{'…' if len(added) > 6 else ''})", None, ts=ts)
        return added

    def best_intern(self, agents: list[Agent], price: float, min_days: int = 0, desk: str | None = None) -> Agent | None:
        pool = [a for a in agents if is_intern(a) and (desk is None or a.desk == desk)]
        # только что переведённые в стажёры отбывают испытательный срок: минимум неделю не возвращаем
        pool = [a for a in pool if not (a.trial_weeks > 0 and self._days(a) < 7)]
        seasoned = [a for a in pool if self._days(a) >= min_days]
        if not seasoned:
            return None
        regime = self.policy().get("regime", "flat")
        # предпочтение семействам, которые по памяти компании зарабатывают в текущем режиме
        return max(seasoned, key=lambda a: (self.memory_bias(a.strategy.family, regime), a.pnl_total(price)))

    def _days(self, a: Agent) -> float:
        return max(0.0, (a.last_ts_seen - a.hired_at) / 86400) if getattr(a, "last_ts_seen", 0) else 0.0

    # --- найм в команду ---
    def vacancies(self, agents: list[Agent]) -> dict[str, int]:
        return {d: self.desk_size - len(self.desk_members(agents, d)) for d in DESK_KEYS}

    def hire_if_needed(self, agents: list[Agent], candles: list[Candle], ts: int) -> list[Agent]:
        price = candles[-1].close
        hired: list[Agent] = []
        vac = self.vacancies(agents)
        if all(v <= 0 for v in vac.values()):
            return hired
        if not self.s.auto_hire:
            if not any(p["kind"] == "hire" for p in self.j.pending_approvals()):
                desk = next(d for d in DESK_KEYS if vac[d] > 0)
                best = self.best_intern(agents, price, desk=desk) or self.best_intern(agents, price)
                self.j.request_approval("hire", f"Нанять в деск «{DESKS[desk]['label']}» лучшего стажёра?",
                                        {"intern": best.name if best else None, "desk": desk,
                                         "pnl": round(best.pnl_total(price), 2) if best else None}, ts=ts)
            return hired
        for desk in DESK_KEYS:
            for _ in range(max(0, vac[desk])):
                best = self.best_intern(agents + hired, price, min_days=3, desk=desk) or self.best_intern(agents + hired, price, desk=desk)
                if best is not None and best not in hired:
                    self.promote(best, price, ts, f"занял свободное место в деске «{DESKS[desk]['label']}»")
                    hired.append(best)
                    continue
                if not self.j.bench():
                    self.refresh_bench(candles, ts, agents + hired)
                cand = self._take_candidate(agents + hired, desk)
                if not cand:
                    break
                a = self._create(cand["strategy"], cand["params"], agents + hired, ts, "active")
                self.j.event("hire", f"Нанят {a.name} в деск «{DESKS[desk]['label']}» ({a.strategy.family}, {cand['params']})", a.name, ts=ts)
                hired.append(a)
        return hired

    def promote(self, intern: Agent, price: float, ts: int, reason: str) -> None:
        """Стажёр становится трейдером со свежим счётом."""
        pnl = intern.pnl_total(price)
        days = self._days(intern)
        intern.account.flatten(price, ts, "повышение: сброс счёта")
        intern.reset_account(self.s.agent_start_balance, ts)
        intern.status = "active"
        intern.rank = RANK_TRADER
        self.j.save_agent(intern)
        self.j.event("hire", f"{intern.name} повышен до трейдера ({reason}; за {days:.0f} дн. стажировки {pnl:+.2f} $)", intern.name, ts=ts)
        log.info("Повышен %s", intern.name)

    def trim_desks(self, agents: list[Agent], price: float, ts: int, candles: list[Candle] | None = None) -> list[str]:
        """Если на деске трейдеров больше desk_size (после перестройки компании), худшие лишние уходят в стажёры."""
        out: list[str] = []
        for d in DESK_KEYS:
            members = sorted(self.desk_members(agents, d), key=lambda a: a.pnl_total(price))
            extra = len(members) - self.desk_size
            for a in members[:max(0, extra)]:
                self.demote(a, price, ts, f"деск «{DESKS[d]['label']}» переполнен, худший по результату", candles)
                out.append(a.name)
        return out

    # --- активность ---
    @staticmethod
    def idle_days(a: Agent, ts: int) -> float:
        last = a.account.trades[-1].ts if a.account.trades else a.hired_at
        return max(0.0, (ts - last) / 86400)

    def drop_idle_interns(self, agents: list[Agent], price: float, ts: int) -> list[str]:
        out = []
        for a in [x for x in agents if is_intern(x)]:
            d = self.idle_days(a, ts)
            if d >= self.s.intern_idle_days:
                self.drop_intern(a, price, ts, f"нет сделок {d:.0f} дн.")
                out.append(a.name)
        return out

    # --- ежедневный отчёт ---
    def review(self, agents: list[Agent], price: float, ts: int) -> None:
        day_key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if self.j.kv_get("review_day") == day_key:
            return
        self.j.kv_set("review_day", day_key)
        self.drop_idle_interns(agents, price, ts)
        team = [a for a in agents if is_team(a)]
        if team:
            self._report(team, [a for a in agents if is_intern(a)], price, ts)

    # --- недельная ротация ---
    @staticmethod
    def week_key(ts: int) -> str:
        d = datetime.fromtimestamp(ts, tz=timezone.utc).isocalendar()
        return f"{d[0]}-W{d[1]:02d}"

    def weekly_review(self, agents: list[Agent], price: float, ts: int, candles: list[Candle] | None = None) -> dict:
        """Понедельник, начало недели по UTC. На каждом деске отдельно:

        1. Трейдеры с минусом за неделю (не больше weekly_demote_max худших) → стажёры на испытательный срок.
        2. Их места занимают лучшие стажёры деска с плюсом за неделю (со свежим счётом).
        3. Звания: senior_weeks недель в плюсе подряд → старший трейдер; минус → снова трейдер;
           live_ready_weeks подряд → кандидат на реальный счёт (нужно ваше одобрение).
        4. Стажёр с минусом две недели подряд → отчислен, его место займёт новый кандидат.
        """
        res = {"demoted": [], "promoted": [], "dropped": [], "live_ready": [], "senior": []}
        team = [a for a in agents if is_team(a)]
        interns = [a for a in agents if is_intern(a)]
        if not team:
            return res
        if candles:
            res["head"] = self.weekly_policy(agents, candles, price, ts)
        # серии и звания
        for a in team:
            if a.week_start_equity <= 0:
                continue
            if a.pnl_week_pct(price) > 0:
                a.streak_weeks += 1
            else:
                a.streak_weeks = 0
                if a.rank == RANK_SENIOR:
                    a.rank = RANK_TRADER
                    self.j.event("rank", f"{a.name}: минус за неделю, снова трейдер", a.name, ts=ts)
            if a.trial_weeks:
                a.trial_weeks += 1
            if a.streak_weeks >= self.s.senior_weeks and a.rank == RANK_TRADER:
                a.rank = RANK_SENIOR
                res["senior"].append(a.name)
                self.j.event("rank", f"{a.name}: {a.streak_weeks} недели подряд в плюсе, повышен до старшего трейдера", a.name, ts=ts)
            if a.streak_weeks >= self.s.live_ready_weeks and not a.live_ready:
                a.live_ready = True
                res["live_ready"].append(a.name)
                self.j.event("live_ready", f"{a.name}: {a.streak_weeks} недели подряд в плюсе, кандидат на реальный счёт", a.name, ts=ts)
                self.j.request_approval("live", f"{a.name} готов к реальным торгам. Переводить?",
                                        {"agent": a.name, "streak_weeks": a.streak_weeks}, ts=ts)
        for desk in DESK_KEYS:
            members = [a for a in team if a.desk == desk and a.week_start_equity > 0]
            d_interns = [a for a in interns if a.desk == desk]
            rated = sorted(members, key=lambda a: a.pnl_week_pct(price))
            losers = [a for a in rated if a.pnl_week_pct(price) < 0][: self.s.weekly_demote_max]
            # спящие: без единой сделки team_idle_days дней тоже в стажёры, но только если есть кем заменить
            promotable = [a for a in d_interns if a.week_start_equity > 0 and a.pnl_week_pct(price) > 0]
            idle_slots = max(0, len(promotable) - len(losers))
            for a in sorted(rated, key=lambda a: -self.idle_days(a, ts)):
                if idle_slots <= 0:
                    break
                if a not in losers and self.idle_days(a, ts) >= self.s.team_idle_days:
                    losers.append(a)
                    idle_slots -= 1
            for a in losers:
                pct = a.pnl_week_pct(price)
                why = f"неделя {pct:+.2f}%" if pct < 0 else f"нет сделок {self.idle_days(a, ts):.0f} дн."
                self.demote(a, price, ts, why, candles)
                res["demoted"].append(a.name)
            vacancies = self.desk_size - len(self.desk_members(agents, desk))
            cands = sorted(promotable, key=lambda a: a.pnl_week_pct(price), reverse=True)
            for a in cands[:max(0, vacancies)]:
                pct = a.pnl_week_pct(price)
                self.promote(a, price, ts, f"лучший стажёр недели в деске «{DESKS[desk]['label']}», {pct:+.2f}%")
                a.trial_weeks = 1
                a.streak_weeks = 0
                self.j.save_agent(a)
                res["promoted"].append(a.name)
        # отчисление стажёров с минусом две недели подряд
        for a in [x for x in agents if is_intern(x) and x.week_start_equity > 0]:
            if a.pnl_week_pct(price) < 0:
                a.streak_weeks -= 1          # у стажёров отрицательная серия = недели в минусе подряд
                if a.streak_weeks <= -2:
                    self.drop_intern(a, price, ts, "две недели подряд в минусе")
                    res["dropped"].append(a.name)
            else:
                a.streak_weeks = 0
        self.j.event("weekly", f"Недельная ротация: в стажёры {len(res['demoted'])}, в трейдеры {len(res['promoted'])}, "
                               f"старших трейдеров +{len(res['senior'])}, отчислено {len(res['dropped'])}, "
                               f"кандидатов на реальный счёт {len(res['live_ready'])}", None, res, ts=ts)
        return res

    def approve_rule(self, details: dict, ts: int) -> int:
        data = details.get("rule") or {}
        text = details.get("text") or str(data)
        kid = self.j.add_knowledge(ts, "rule", data.get("type", "rule"), text, "ревизор, одобрено владельцем",
                                   {**data, "rationale": details.get("rationale", "")})
        self.j.event("rule", f"Новое правило риск-менеджера: {text}", None, {"id": kid}, ts=ts)
        return kid

    def approve_live(self, a: Agent, ts: int) -> None:
        a.rank = RANK_LIVE
        self.j.save_agent(a)
        self.j.event("rank", f"{a.name}: одобрен перевод на реальный счёт (модуль реальной торговли ещё не подключён, торгует на демо)", a.name, ts=ts)

    def _report(self, team: list[Agent], interns: list[Agent], price: float, ts: int) -> None:
        rows = [a.snapshot(price) for a in team]
        total = sum(r.equity for r in rows)
        table = "\n".join(f"{r.name} [{r.strategy}] деск={DESKS[r.desk]['label']} звание={RANK_LABELS.get(r.rank)} капитал={r.equity:.2f} "
                          f"день={r.pnl_day:+.2f} всего={r.pnl_total:+.2f} просадка={r.drawdown*100:.1f}% сделок={r.trades} "
                          f"последнее: {r.last_reason}" for r in rows)
        irows = sorted((a.snapshot(price) for a in interns), key=lambda r: r.pnl_total, reverse=True)[:5]
        itable = "\n".join(f"{r.name} [{r.strategy}] всего={r.pnl_total:+.2f} просадка={r.drawdown*100:.1f}%" for r in irows)
        text = f"Капитал компании: {total:.2f}. Трейдеры:\n{table}\n\nЛучшие стажёры:\n{itable or 'нет'}"
        if self.client and self.client.enabled:
            try:
                data = self.client.structured(
                    "Ты директор компании алгоритмической торговли Botz с тремя десками (быки, медведи, двусторонние). "
                    "Тебе дают дневную сводку по трейдерам и стажёрам. "
                    "Напиши короткий отчёт для владельца: что произошло, кто лучший, кто худший, что рекомендуешь. "
                    "Рекомендации должны быть конкретными и проверяемыми.",
                    text, REPORT_SCHEMA, max_tokens=2000, strong=True)
                self.j.event("report", data["summary"], None, data, ts=ts)
                return
            except LLMUnavailable as e:
                log.warning("отчёт без LLM: %s", e)
        best = max(rows, key=lambda r: r.pnl_total)
        worst = min(rows, key=lambda r: r.pnl_total)
        self.j.event("report", f"Капитал компании {total:.2f}. Лучший: {best.name} ({best.pnl_total:+.2f}), "
                               f"худший: {worst.name} ({worst.pnl_total:+.2f})."
                               + (f" Лучший стажёр: {irows[0].name} ({irows[0].pnl_total:+.2f})." if irows else ""),
                     None, {"best_agent": best.name, "worst_agent": worst.name}, ts=ts)


DepartmentHead = Director
