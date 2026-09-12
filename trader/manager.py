"""Руководитель отдела: следит за агентами, увольняет, нанимает со скамейки, пишет отчёт."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .agents.base import Agent
from .agents.registry import DEFAULT_NAMES, build_strategy, new_account
from .config import Settings
from .journal import Journal
from .llm import ClaudeClient, LLMUnavailable
from .models import Candle
from .research import StrategyLab, result_to_dict

log = logging.getLogger(__name__)

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


class DepartmentHead:
    def __init__(self, settings: Settings, journal: Journal, client: ClaudeClient | None = None, team_size: int = 10):
        self.s = settings
        self.j = journal
        self.client = client
        self.team_size = team_size
        self.lab = StrategyLab(fee_rate=settings.fee_rate)

    # --- увольнение ---
    def fire(self, agent: Agent, price: float, ts: int, reason: str) -> None:
        agent.account.flatten(price, ts, f"увольнение: {reason}")
        agent.status = "fired"
        self.j.mark_fired(agent.name, ts)
        self.j.event("fire", f"{agent.name} уволен: {reason}", agent.name, {"equity": agent.equity(price)}, ts=ts)
        log.info("Уволен %s: %s", agent.name, reason)

    # --- скамейка ---
    def refresh_bench(self, candles: list[Candle], ts: int) -> int:
        hist = candles[-self.s.research_lookback:]
        results = self.lab.research(hist, top_n=self.s.bench_size)
        self.j.clear_bench()
        for r in results:
            self.j.add_bench(r.family, r.params, r.score(), result_to_dict(r), ts=ts)
        self.j.event("research", f"Отдел исследований обновил скамейку: {len(results)} кандидатов", None,
                     {"candidates": [result_to_dict(r) for r in results]}, ts=ts)
        return len(results)

    # --- найм ---
    def hire_if_needed(self, agents: list[Agent], candles: list[Candle], ts: int) -> list[Agent]:
        alive = [a for a in agents if a.status != "fired"]
        vacancies = self.team_size - len(alive)
        hired: list[Agent] = []
        if vacancies <= 0:
            return hired
        if not self.j.bench():
            self.refresh_bench(candles, ts)
        for _ in range(vacancies):
            if not self.s.auto_hire:
                if not any(p["kind"] == "hire" for p in self.j.pending_approvals()):
                    self.j.request_approval("hire", "Нанять нового агента со скамейки?",
                                            {"bench": self.j.bench()[:3]}, ts=ts)
                break
            cand = self.j.take_from_bench()
            if not cand:
                self.refresh_bench(candles, ts)
                cand = self.j.take_from_bench()
            if not cand:
                break
            agent = self.hire(cand["strategy"], cand["params"], agents + hired, ts)
            hired.append(agent)
        return hired

    def hire(self, family: str, params: dict, existing: list[Agent], ts: int) -> Agent:
        base = DEFAULT_NAMES.get(family, family)
        n = 2
        name = base
        taken = {a.name for a in existing} | {r["name"] for r in self.j.all_agents()}
        while name in taken:
            name = f"{base} #{n}"
            n += 1
        strat = build_strategy(family, params, self.client)
        agent = Agent(name=name, strategy=strat, account=new_account(self.s, name), hired_at=ts)
        self.j.save_agent(agent)
        self.j.event("hire", f"Нанят {name} ({family}, {params})", name, {"params": params}, ts=ts)
        log.info("Нанят %s", name)
        return agent

    # --- оценка и отчёт ---
    def review(self, agents: list[Agent], price: float, ts: int) -> None:
        """Раз в сутки: рекомендации по слабым агентам и отчёт."""
        day_key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if self.j.kv_get("review_day") == day_key:
            return
        self.j.kv_set("review_day", day_key)
        alive = [a for a in agents if a.status != "fired"]
        if not alive:
            return
        # Слабый агент: работает больше 14 дней, в минусе, и худший в отделе.
        seasoned = [a for a in alive if ts - a.hired_at >= 14 * 86400]
        if seasoned:
            worst = min(seasoned, key=lambda a: a.pnl_total(price))
            if worst.pnl_total(price) < 0 and not any(
                p["kind"] == "fire" and p["details"].get("agent") == worst.name for p in self.j.pending_approvals()):
                self.j.request_approval("fire", f"Уволить {worst.name}? Худший результат за 14+ дней",
                                        {"agent": worst.name, "pnl": round(worst.pnl_total(price), 2)}, ts=ts)
        self._report(alive, price, ts)

    def _report(self, alive: list[Agent], price: float, ts: int) -> None:
        rows = [a.snapshot(price) for a in alive]
        total = sum(r.equity for r in rows)
        table = "\n".join(f"{r.name} [{r.strategy}] статус={r.status} капитал={r.equity:.2f} день={r.pnl_day:+.2f} "
                          f"всего={r.pnl_total:+.2f} просадка={r.drawdown*100:.1f}% сделок={r.trades} "
                          f"последнее: {r.last_reason}" for r in rows)
        text = f"Капитал отдела: {total:.2f}. Агенты:\n{table}"
        if self.client and self.client.enabled:
            try:
                data = self.client.structured(
                    "Ты руководитель отдела алгоритмической торговли. Тебе дают дневную сводку по агентам. "
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
                               f"худший: {worst.name} ({worst.pnl_total:+.2f}).", None,
                     {"best_agent": best.name, "worst_agent": worst.name}, ts=ts)
