"""Руководитель отдела: следит за командой и стажёрами, увольняет, нанимает, повышает, пишет отчёт.

Устройство:
- Команда (team_size агентов) торгует и считается в капитал отдела.
- Стажёры (intern_count) торгуют в тени на своих демосчетах. В капитал отдела не входят.
- Отдел исследований наполняет скамейку кандидатов; из неё берутся стажёры.
- Освободившееся место в команде занимает лучший стажёр. Раз в сутки руководитель
  предлагает владельцу заменить худшего опытного члена команды лучшим опытным стажёром.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .agents.base import Agent
from .agents.registry import build_strategy, family_label, new_account
from .config import Settings
from .journal import Journal
from .llm import ClaudeClient, LLMUnavailable
from .models import Candle
from .research import StrategyLab, combo_key, result_to_dict
from .data import indicators as ind

log = logging.getLogger(__name__)

TENURE_DAYS = 14
TEAM_STATUSES = {"active", "paused"}

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


def is_team(a: Agent) -> bool:
    return a.status in TEAM_STATUSES


def is_intern(a: Agent) -> bool:
    return a.status == "intern"


class DepartmentHead:
    def __init__(self, settings: Settings, journal: Journal, client: ClaudeClient | None = None, team_size: int | None = None):
        self.s = settings
        self.j = journal
        self.client = client
        self.team_size = team_size or settings.team_size
        self.lab = StrategyLab(fee_rate=settings.fee_rate)

    # --- политика руководителя: режим рынка, потолок доли, ответственность за результат ---
    CAPS = {"balanced": {"up": 1.0, "flat": 0.7, "down": 0.4}, "defensive": {"up": 0.7, "flat": 0.4, "down": 0.2}}

    def policy(self) -> dict:
        return self.j.kv_get("head_policy", None) or {
            "mode": "balanced", "regime": "flat", "cap": 1.0, "min_rebalance": 0.05,
            "fail_weeks": 0, "good_weeks": 0, "weeks": [], "changed_ts": 0,
        }

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

    def daily_policy(self, candles: list[Candle], ts: int) -> dict:
        """Раз в день: оценить рынок и выставить потолок доли для всего отдела."""
        pol = self.policy()
        if not self.s.head_policy:
            pol.update({"cap": 1.0, "regime": "off"})
            self._save_policy(pol)
            return pol
        regime = self.assess_regime(candles)
        cap = self.CAPS[pol["mode"]][regime]
        if regime != pol.get("regime") or abs(cap - pol.get("cap", 1.0)) > 1e-9:
            names = {"up": "рост", "flat": "боковик", "down": "падение"}
            self.j.event("head", f"Руководитель: рынок — {names[regime]}, потолок доли для отдела {cap:.0%} "
                                 f"(подход: {'обычный' if pol['mode'] == 'balanced' else 'защитный'})", None,
                         {"regime": regime, "cap": cap, "mode": pol["mode"]}, ts=ts)
        pol["regime"], pol["cap"], pol["changed_ts"] = regime, cap, ts
        self._save_policy(pol)
        return pol

    def weekly_policy(self, agents: list[Agent], candles: list[Candle], price: float, ts: int) -> dict:
        """Раз в неделю: KPI руководителя. Сравнение с «держать доллары» и «держать биткоин», смена подхода."""
        pol = self.policy()
        team = [a for a in agents if is_team(a) and a.week_start_equity > 0]
        if not team:
            return pol
        start = sum(a.week_start_equity for a in team)
        dept_pct = (sum(a.equity(price) for a in team) - start) / start * 100 if start else 0.0
        week_ago = [c for c in candles if c.ts <= ts - 7 * 86400]
        btc_pct = (price / week_ago[-1].close - 1) * 100 if week_ago else 0.0
        fees = self.j.fees_since(ts - 7 * 86400, {a.name for a in team})
        loss = max(0.0, -(sum(a.equity(price) for a in team) - start))
        fee_share = fees / loss if loss > 0 else 0.0
        beat_cash = dept_pct > 0
        pol["weeks"] = (pol.get("weeks") or [])[-11:] + [{"ts": ts, "dept": round(dept_pct, 2), "btc": round(btc_pct, 2), "fees": round(fees, 2)}]
        pol["fail_weeks"] = 0 if beat_cash else pol.get("fail_weeks", 0) + 1
        pol["good_weeks"] = pol.get("good_weeks", 0) + 1 if beat_cash else 0
        notes = [f"отдел {dept_pct:+.2f}% за неделю, биткоин {btc_pct:+.2f}%, комиссии {fees:.2f} $"]
        # торговать реже, если потери в основном из комиссий
        new_min = 0.10 if fee_share > 0.5 else 0.05
        if abs(new_min - pol.get("min_rebalance", 0.05)) > 1e-9:
            notes.append("комиссии съедают больше половины потерь: торгуем реже" if new_min > 0.05 else "порог сделок возвращён к обычному")
        pol["min_rebalance"] = new_min
        # смена подхода
        if pol["fail_weeks"] >= self.s.head_fail_weeks and pol["mode"] == "balanced":
            pol["mode"] = "defensive"
            notes.append(f"{pol['fail_weeks']} недели подряд хуже долларов: перехожу на защитный подход и переобучаю всех")
            self.retrain(agents, candles, ts, all_agents=True)
        elif pol["good_weeks"] >= 2 and pol["mode"] == "defensive" and pol.get("regime") == "up":
            pol["mode"] = "balanced"
            notes.append("две недели в плюсе и рынок растёт: возвращаю обычный подход")
        pol["cap"] = self.CAPS[pol["mode"]].get(pol.get("regime", "flat"), 1.0) if self.s.head_policy else 1.0
        self._save_policy(pol)
        self.j.event("head", "Отчёт руководителя за неделю: " + "; ".join(notes), None, pol, ts=ts)
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
        agent = Agent(name=name, strategy=strat, account=new_account(self.s, name), hired_at=ts, status=status)
        self.j.save_agent(agent)
        return agent

    # --- стажёры ---
    def fill_interns(self, agents: list[Agent], candles: list[Candle], ts: int) -> list[Agent]:
        """Добрать стажёров до intern_count из скамейки кандидатов."""
        interns = [a for a in agents if is_intern(a)]
        need = self.s.intern_count - len(interns)
        added: list[Agent] = []
        if need <= 0:
            return added
        if not self.j.bench():
            self.refresh_bench(candles, ts, agents)
        for _ in range(need):
            cand = self.j.take_from_bench(self._taken(agents + added))
            if not cand:
                break
            a = self._create(cand["strategy"], cand["params"], agents + added, ts, "intern")
            added.append(a)
        if added:
            self.j.event("intern", f"Набрано стажёров: {len(added)} ({', '.join(a.name for a in added[:6])}{'…' if len(added) > 6 else ''})", None, ts=ts)
        return added

    def best_intern(self, agents: list[Agent], price: float, min_days: int = 0) -> Agent | None:
        pool = [a for a in agents if is_intern(a) and (price and (a.last_price or price))]
        pool = [a for a in pool if a.hired_at and (min_days == 0 or True)]
        seasoned = [a for a in pool if self._days(a) >= min_days]
        if not seasoned:
            return None
        return max(seasoned, key=lambda a: a.pnl_total(price))

    def _days(self, a: Agent) -> float:
        return max(0.0, (a.last_ts_seen - a.hired_at) / 86400) if getattr(a, "last_ts_seen", 0) else 0.0

    # --- найм в команду ---
    def hire_if_needed(self, agents: list[Agent], candles: list[Candle], ts: int) -> list[Agent]:
        price = candles[-1].close
        team = [a for a in agents if is_team(a)]
        vacancies = self.team_size - len(team)
        hired: list[Agent] = []
        if vacancies <= 0:
            return hired
        if not self.s.auto_hire:
            if not any(p["kind"] == "hire" for p in self.j.pending_approvals()):
                best = self.best_intern(agents, price)
                self.j.request_approval("hire", "Нанять в команду лучшего стажёра?",
                                        {"intern": best.name if best else None, "pnl": round(best.pnl_total(price), 2) if best else None}, ts=ts)
            return hired
        for _ in range(vacancies):
            best = self.best_intern(agents, price, min_days=3) or self.best_intern(agents, price)
            if best is not None:
                self.promote(best, price, ts, "занял свободное место в команде")
                hired.append(best)
                continue
            # стажёров нет — берём кандидата со скамейки напрямую
            if not self.j.bench():
                self.refresh_bench(candles, ts, agents + hired)
            cand = self.j.take_from_bench(self._taken(agents + hired))
            if not cand:
                break
            a = self._create(cand["strategy"], cand["params"], agents + hired, ts, "active")
            self.j.event("hire", f"Нанят {a.name} ({a.strategy.family}, {cand['params']})", a.name, ts=ts)
            hired.append(a)
        return hired

    def promote(self, intern: Agent, price: float, ts: int, reason: str) -> None:
        """Стажёр становится членом команды со свежим счётом."""
        pnl = intern.pnl_total(price)
        days = self._days(intern)
        intern.account.flatten(price, ts, "повышение: сброс счёта")
        intern.reset_account(self.s.agent_start_balance, ts)
        intern.status = "active"
        self.j.save_agent(intern)
        self.j.event("hire", f"{intern.name} повышен из стажёров ({reason}; за {days:.0f} дн. стажировки {pnl:+.2f} $)", intern.name, ts=ts)
        log.info("Повышен %s", intern.name)

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
        """Понедельник, начало недели по UTC.

        1. Члены команды с минусом за неделю (не больше weekly_demote_max худших) → стажёры на испытательный срок.
        2. Их места занимают лучшие стажёры с плюсом за неделю (со свежим счётом).
        3. Серия недель в плюсе: live_ready_weeks подряд → кандидат на реальный счёт.
        4. Стажёр с минусом две недели подряд → отчислен, его место займёт новый кандидат.
        """
        res = {"demoted": [], "promoted": [], "dropped": [], "live_ready": []}
        team = [a for a in agents if is_team(a)]
        interns = [a for a in agents if is_intern(a)]
        if not team:
            return res
        if candles:
            res["head"] = self.weekly_policy(agents, candles, price, ts)
        # серии
        for a in team:
            if a.week_start_equity <= 0:
                continue
            if a.pnl_week_pct(price) > 0:
                a.streak_weeks += 1
            else:
                a.streak_weeks = 0
            if a.trial_weeks:
                a.trial_weeks += 1
            if a.streak_weeks >= self.s.live_ready_weeks and not a.live_ready:
                a.live_ready = True
                res["live_ready"].append(a.name)
                self.j.event("live_ready", f"{a.name}: {a.streak_weeks} недели подряд в плюсе, кандидат на реальный счёт", a.name, ts=ts)
                self.j.request_approval("live", f"{a.name} готов к реальным торгам. Переводить?",
                                        {"agent": a.name, "streak_weeks": a.streak_weeks}, ts=ts)
        # понижение
        rated = sorted([a for a in team if a.week_start_equity > 0], key=lambda a: a.pnl_week_pct(price))
        losers = [a for a in rated if a.pnl_week_pct(price) < 0][: self.s.weekly_demote_max]
        # спящие: без единой сделки team_idle_days дней — тоже в котята, но только если есть кем заменить
        # (активные котята с плюсом за неделю), иначе команда осталась бы пустой
        promotable = [a for a in interns if a.week_start_equity > 0 and a.pnl_week_pct(price) > 0]
        idle_slots = max(0, len(promotable) - len(losers))
        for a in sorted(rated, key=lambda a: -self.idle_days(a, ts)):
            if idle_slots <= 0:
                break
            if a not in losers and self.idle_days(a, ts) >= self.s.team_idle_days:
                losers.append(a)
                idle_slots -= 1
        for a in losers:
            pct = a.pnl_week_pct(price)
            a.account.flatten(price, ts, "перевод в стажёры")
            a.reset_account(self.s.agent_start_balance, ts)
            if candles:
                self.retrain(agents, candles, ts, only=[a])
            a.status = "intern"
            a.streak_weeks = 0
            a.trial_weeks = 1
            a.live_ready = False
            self.j.save_agent(a)
            res["demoted"].append(a.name)
            why = f"неделя {pct:+.2f}%" if pct < 0 else f"нет сделок {self.idle_days(a, ts):.0f} дн."
            self.j.event("demote", f"{a.name} переведён в стажёры: {why}", a.name, ts=ts)
        # повышение лучших стажёров с плюсом за неделю
        vacancies = self.team_size - len([a for a in agents if is_team(a)])
        cands = sorted([a for a in interns if a.week_start_equity > 0 and a.pnl_week_pct(price) > 0],
                       key=lambda a: a.pnl_week_pct(price), reverse=True)
        for a in cands[:max(0, vacancies)]:
            pct = a.pnl_week_pct(price)
            self.promote(a, price, ts, f"лучший стажёр недели, {pct:+.2f}%")
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
        self.j.event("weekly", f"Недельная ротация: в стажёры {len(res['demoted'])}, в команду {len(res['promoted'])}, "
                               f"отчислено {len(res['dropped'])}, кандидатов на реальный счёт {len(res['live_ready'])}", None, res, ts=ts)
        return res

    def _report(self, team: list[Agent], interns: list[Agent], price: float, ts: int) -> None:
        rows = [a.snapshot(price) for a in team]
        total = sum(r.equity for r in rows)
        table = "\n".join(f"{r.name} [{r.strategy}] статус={r.status} капитал={r.equity:.2f} день={r.pnl_day:+.2f} "
                          f"всего={r.pnl_total:+.2f} просадка={r.drawdown*100:.1f}% сделок={r.trades} "
                          f"последнее: {r.last_reason}" for r in rows)
        irows = sorted((a.snapshot(price) for a in interns), key=lambda r: r.pnl_total, reverse=True)[:5]
        itable = "\n".join(f"{r.name} [{r.strategy}] всего={r.pnl_total:+.2f} просадка={r.drawdown*100:.1f}%" for r in irows)
        text = f"Капитал отдела: {total:.2f}. Команда:\n{table}\n\nЛучшие стажёры:\n{itable or 'нет'}"
        if self.client and self.client.enabled:
            try:
                data = self.client.structured(
                    "Ты руководитель отдела алгоритмической торговли. Тебе дают дневную сводку по команде и стажёрам. "
                    "Напиши короткий отчёт для владельца: что произошло, кто лучший, кто худший, что рекомендуешь. "
                    "Рекомендации должны быть конкретными и проверяемыми.",
                    text, REPORT_SCHEMA, max_tokens=2000)
                self.j.event("report", data["summary"], None, data, ts=ts)
                return
            except LLMUnavailable as e:
                log.warning("отчёт без LLM: %s", e)
        best = max(rows, key=lambda r: r.pnl_total)
        worst = min(rows, key=lambda r: r.pnl_total)
        self.j.event("report", f"Капитал отдела {total:.2f}. Лучший: {best.name} ({best.pnl_total:+.2f}), "
                               f"худший: {worst.name} ({worst.pnl_total:+.2f})."
                               + (f" Лучший стажёр: {irows[0].name} ({irows[0].pnl_total:+.2f})." if irows else ""),
                     None, {"best_agent": best.name, "worst_agent": worst.name}, ts=ts)
