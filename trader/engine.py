"""Движок: один «тик» = одна закрытая часовая свеча.

Порядок на каждом тике:
1. взять свечи → 2. обновить день/пики → 3. проверка лимитов отдела →
4. каждый агент решает → риск-менеджер проверяет → бумажный счёт исполняет → журнал →
5. обучение → 6. руководитель (найм, отчёт) → 7. сохранить состояние.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from .agents.base import Agent
from .agents.registry import DEFAULT_NAMES, build_strategy, default_department, new_account
from .config import Settings
from .data.market import MarketData, get_market
from .journal import Journal
from .learning import Learner
from .llm import ClaudeClient
from .manager import DepartmentHead, is_intern, is_team
from .models import Candle, Trade
from .paper import PaperAccount
from .risk import RiskManager

log = logging.getLogger(__name__)


def day_key_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class Engine:
    def __init__(self, settings: Settings, market: MarketData | None = None, journal: Journal | None = None,
                 client: ClaudeClient | None = None):
        self.s = settings
        self.market = market or get_market(settings.market_source)
        self.j = journal or Journal(settings.db_path)
        self.client = client if client is not None else ClaudeClient(settings.anthropic_api_key, settings.llm_model)
        self.risk = RiskManager(settings)
        self.head = DepartmentHead(settings, self.j, self.client)
        self.learner = Learner(settings, self.j, self.client)
        self.agents: list[Agent] = []
        self.last_candles: list[Candle] = []
        self.last_tick_ts: int = self.j.kv_get("last_tick_ts", 0)
        self.last_price: float = float(self.j.kv_get("last_price", 0.0))
        self.last_error: str = ""
        self._load()

    # --- состояние ---
    def _load(self) -> None:
        rows = self.j.load_agents()
        if not rows:
            self.agents = default_department(self.s, self.client)
            for a in self.agents:
                self.j.save_agent(a)
            self.j.event("start", f"Создан отдел из {len(self.agents)} агентов")
            return
        for r in rows:
            strat = build_strategy(r["strategy"], json.loads(r["params"]), self.client)
            acc = PaperAccount(owner=r["name"], cash=r["cash"], btc=r["btc"], fee_rate=self.s.fee_rate,
                               slippage_rate=self.s.slippage_rate)
            acc.realized_pnl = r["realized_pnl"]
            acc._avg_entry = r["avg_entry"]
            acc.trades = [Trade(t["ts"], t["agent"], t["side"], t["price"], t["qty"], t["fee"], t["reason"] or "")
                          for t in self.j.trades_for(r["name"])]
            a = Agent(name=r["name"], strategy=strat, account=acc, status=r["status"], hired_at=r["hired_at"],
                      peak_equity=r["peak_equity"], day_start_equity=r["day_start_equity"], day_key=r["day_key"],
                      notes=json.loads(r["notes"] or "[]"))
            a._start_balance = r["start_balance"]
            a.last_price = self.last_price
            self.agents.append(a)
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        """Если в коде появился новый штатный агент, добавить его в уже работающий отдел."""
        known = {r["strategy"] for r in self.j.all_agents()}
        for family, name in DEFAULT_NAMES.items():
            if family in known:
                continue
            strat = build_strategy(family, None, self.client)
            a = Agent(name=name, strategy=strat, account=new_account(self.s, name))
            a.last_price = self.last_price
            self.agents.append(a)
            self.j.save_agent(a)
            self.j.event("hire", f"В отдел добавлен новый штатный агент: {name}", name)

    def save(self) -> None:
        for a in self.agents:
            self.j.save_agent(a)
        self.j.kv_set("last_tick_ts", self.last_tick_ts)
        self.j.kv_set("last_price", self.last_price)

    # --- тик ---
    def tick(self, candles: list[Candle] | None = None, force: bool = False) -> dict:
        try:
            candles = candles or self.market.candles(self.s.symbol, self.s.timeframe, self.s.history_candles)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"котировки: {e}"
            log.error(self.last_error)
            self.j.event("error", self.last_error)
            return {"ok": False, "error": self.last_error}
        if not candles:
            return {"ok": False, "error": "нет свечей"}
        last = candles[-1]
        self.last_candles = candles
        self.last_price = last.close
        self.last_error = ""
        if last.ts <= self.last_tick_ts and not force:
            return {"ok": True, "skipped": True, "ts": last.ts}
        price, ts = last.close, last.ts
        dk = day_key_of(ts)
        summary = {"ok": True, "ts": ts, "price": price, "decisions": [], "fired": [], "hired": []}

        for a in self.agents:
            if a.status in {"fired", "dropped"}:
                continue
            a.last_ts_seen = ts
            if a.hired_at > ts:          # создан «по часам сервера» раньше, чем пришла первая свеча
                a.hired_at = ts
            a.roll_day(dk, price)
            a.observe(price)

        # Стажёры торгуют в тени: без дневных пауз, но с отчислением за просадку.
        for a in [x for x in self.agents if is_intern(x)]:
            self._intern_step(a, candles, price, ts)

        ok, why = self.risk.check_department([a for a in self.agents if is_team(a)], price, dk)
        if not ok:
            for a in self.agents:
                if a.status in {"active", "paused"}:
                    t = a.account.flatten(price, ts, f"стоп отдела: {why}")
                    if t:
                        self.j.trade(t)
                    a.status = "paused"
                    self.j.equity(ts, a.name, a.equity(price), price)
            self.j.event("halt", f"Отдел остановлен: {why}", None, ts=ts)
            summary["halt"] = why
        else:
            for a in self.agents:
                if not is_team(a):
                    continue
                summary["decisions"].append(self._agent_step(a, candles, price, ts))
                if a.status == "fired":
                    summary["fired"].append(a.name)

        self.learner.after_tick(self.agents, candles)
        hired = self.head.hire_if_needed(self.agents, candles, ts)
        new_interns = self.head.fill_interns(self.agents, candles, ts)
        for h in hired + new_interns:
            h.last_ts_seen = ts
            h.roll_day(dk, price)
            h.observe(price)
            self.j.equity(ts, h.name, h.equity(price), price)
        self.agents.extend([h for h in hired if h not in self.agents])
        self.agents.extend(new_interns)
        summary["hired"] = [h.name for h in hired]
        summary["interns_added"] = [h.name for h in new_interns]
        self.head.review(self.agents, price, ts)

        self.last_tick_ts = ts
        self.save()
        return summary

    def _intern_step(self, a: Agent, candles: list[Candle], price: float, ts: int) -> None:
        ctx = {"exposure": a.account.exposure(price), "bars_in_position": a.bars_in_position}
        try:
            sig = a.strategy.decide(candles, ctx)
        except Exception as e:  # noqa: BLE001
            from .agents.base import hold
            sig = hold(f"ошибка стратегии: {e}", ctx["exposure"])
        a.last_signal = sig
        if a.drawdown(price) >= self.s.agent_max_drawdown:
            self.head.drop_intern(a, price, ts, f"просадка {a.drawdown(price)*100:.1f}%")
            return
        t = a.account.rebalance(sig.target_exposure, price, ts, sig.reason)
        if t:
            self.j.trade(t)
        eq = a.equity(price)
        self.j.decision(ts, a.name, a.strategy.family, sig.action.value, sig.target_exposure, sig.confidence,
                        sig.reason, price, eq, True, None)
        self.j.equity(ts, a.name, eq, price)
        a.observe(price)
        a.after_trade_tick(price)

    def _agent_step(self, a: Agent, candles: list[Candle], price: float, ts: int) -> dict:
        ctx = {"exposure": a.account.exposure(price), "lessons": a.notes, "bars_in_position": a.bars_in_position}
        try:
            sig = a.strategy.decide(candles, ctx)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: ошибка стратегии", a.name)
            from .agents.base import hold
            sig = hold(f"ошибка стратегии: {e}", ctx["exposure"])
        a.last_signal = sig
        verdict = self.risk.check_agent(a, sig, price)
        executed = False
        blocked = None
        if verdict.fire:
            self.head.fire(a, price, ts, verdict.reason)
            blocked = verdict.reason
        elif verdict.pause:
            t = a.account.flatten(price, ts, f"пауза: {verdict.reason}")
            if t:
                self.j.trade(t)
            a.status = "paused"
            self.j.event("pause", f"{a.name}: {verdict.reason}", a.name, ts=ts)
            blocked = verdict.reason
        elif verdict.allowed:
            t = a.account.rebalance(verdict.target_exposure, price, ts, sig.reason)
            if t:
                self.j.trade(t)
            executed = True
        else:
            blocked = verdict.reason
        eq = a.equity(price)
        self.j.decision(ts, a.name, a.strategy.family, sig.action.value, sig.target_exposure, sig.confidence,
                        sig.reason, price, eq, executed, blocked)
        if a.status != "fired":
            self.j.equity(ts, a.name, eq, price)
        a.observe(price)
        a.after_trade_tick(price)
        return {"agent": a.name, "action": sig.action.value, "target": sig.target_exposure,
                "reason": sig.reason, "executed": executed, "blocked": blocked, "equity": round(eq, 2)}

    # --- панель ---
    def state(self) -> dict:
        price = self.last_price
        snaps = [a.snapshot(price).__dict__ for a in self.agents if a.status not in {"intern", "dropped"}]
        interns = []
        for a in self.agents:
            if is_intern(a):
                d = a.snapshot(price).__dict__
                d["days"] = round(max(0.0, ((a.last_ts_seen or self.last_tick_ts) - a.hired_at) / 86400), 1)
                interns.append(d)
        interns.sort(key=lambda d: d["pnl_total"], reverse=True)
        alive = [s for s in snaps if s["status"] != "fired"]
        total = sum(s["equity"] for s in alive)
        start = sum(a.start_balance() for a in self.agents if is_team(a))
        return {
            "now": int(time.time()),
            "last_tick_ts": self.last_tick_ts,
            "price": price,
            "symbol": self.s.symbol,
            "timeframe": self.s.timeframe,
            "market": getattr(self.market, "name", "?"),
            "mode": "paper",
            "llm": self.client.enabled if self.client else False,
            "error": self.last_error,
            "department": {"equity": round(total, 2), "start": round(start, 2), "pnl": round(total - start, 2),
                           "agents_active": len([s for s in alive if s["status"] == "active"]),
                           "agents_paused": len([s for s in alive if s["status"] == "paused"]),
                           "halted": self.risk.dept_halted_day == day_key_of(self.last_tick_ts) if self.last_tick_ts else False},
            "agents": snaps,
            "interns": interns,
            "team_size": self.s.team_size,
            "intern_count": self.s.intern_count,
            "bench": self.j.bench()[:10],
            "approvals": self.j.pending_approvals(),
            "events": self.j.recent_events(40),
        }

    def apply_approval(self, approval_id: int, approve: bool) -> dict | None:
        r = self.j.decide_approval(approval_id, approve)
        if not r:
            return None
        if not approve:
            self.j.event("approval", f"Отклонено: {r['title']}")
            return r
        price = self.last_price
        ts = int(time.time())
        if r["kind"] == "fire":
            name = r["details"].get("agent")
            for a in self.agents:
                if a.name == name and a.status != "fired":
                    self.head.fire(a, price or a.last_price, ts, "решение владельца")
        elif r["kind"] == "hire":
            best = self.head.best_intern(self.agents, price)
            if best is not None:
                self.head.promote(best, price, ts, "одобрено владельцем")
            else:
                cand = self.j.take_from_bench(self.head._taken(self.agents))
                if cand:
                    a = self.head._create(cand["strategy"], cand["params"], self.agents, ts, "active")
                    self.j.event("hire", f"Нанят {a.name}", a.name, ts=ts)
                    self.agents.append(a)
        elif r["kind"] == "promote":
            worst = next((a for a in self.agents if a.name == r["details"].get("agent") and is_team(a)), None)
            intern = next((a for a in self.agents if a.name == r["details"].get("intern") and is_intern(a)), None)
            if worst is not None:
                self.head.fire(worst, price or worst.last_price, ts, "заменён стажёром по решению владельца")
            if intern is not None:
                self.head.promote(intern, price or intern.last_price, ts, "одобрено владельцем")
        self.j.event("approval", f"Одобрено: {r['title']}")
        self.save()
        return r

    def manual_fire(self, name: str) -> bool:
        price = self.last_price
        for a in self.agents:
            if a.name == name and is_intern(a):
                self.head.drop_intern(a, price or a.last_price, int(time.time()), "решение владельца")
                self.save()
                return True
            if a.name == name and is_team(a):
                self.head.fire(a, price or a.last_price, int(time.time()), "решение владельца")
                self.save()
                return True
        return False
