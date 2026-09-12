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

    # --- ежедневный разбор ---
    def review(self, agents: list[Agent], price: float, ts: int) -> None:
        day_key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if self.j.kv_get("review_day") == day_key:
            return
        self.j.kv_set("review_day", day_key)
        team = [a for a in agents if is_team(a)]
        if not team:
            return
        pending_kinds = {(p["kind"], p["details"].get("agent")) for p in self.j.pending_approvals()}
        seasoned = [a for a in team if self._days(a) >= TENURE_DAYS]
        if seasoned:
            worst = min(seasoned, key=lambda a: a.pnl_total(price))
            if worst.pnl_total(price) < 0 and ("promote", worst.name) not in pending_kinds and ("fire", worst.name) not in pending_kinds:
                best = self.best_intern(agents, price, min_days=TENURE_DAYS)
                if best is not None and best.pnl_total(price) > worst.pnl_total(price):
                    self.j.request_approval(
                        "promote", f"Заменить {worst.name} стажёром {best.name}?",
                        {"agent": worst.name, "intern": best.name, "agent_pnl": round(worst.pnl_total(price), 2),
                         "intern_pnl": round(best.pnl_total(price), 2)}, ts=ts)
                else:
                    self.j.request_approval("fire", f"Уволить {worst.name}? Худший результат за {TENURE_DAYS}+ дней",
                                            {"agent": worst.name, "pnl": round(worst.pnl_total(price), 2)}, ts=ts)
        self._report(team, [a for a in agents if is_intern(a)], price, ts)

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
