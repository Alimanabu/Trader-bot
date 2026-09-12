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
from .data import indicators as ind
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
        self.client = client if client is not None else ClaudeClient(
            settings.anthropic_api_key, settings.llm_model, daily_budget_usd=settings.llm_daily_budget_usd, spend_store=self.j)
        if self.client and self.client.spend_store is None:
            self.client.spend_store = self.j
        self.risk = RiskManager(settings)
        self.head = DepartmentHead(settings, self.j, self.client)
        self.learner = Learner(settings, self.j, self.client)
        self.agents: list[Agent] = []
        self.last_candles: list[Candle] = []
        self.last_tick_ts: int = self.j.kv_get("last_tick_ts", 0)
        self.last_price: float = float(self.j.kv_get("last_price", 0.0))
        self.last_poll_ts: int = 0
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
            acc.trades = [Trade(t["ts"], t["agent"], t["side"], t["price"], t["qty"], t["fee"], t["reason"] or "",
                                pnl=t.get("pnl"), cost=t.get("cost"))
                          for t in self.j.trades_for(r["name"])]
            a = Agent(name=r["name"], strategy=strat, account=acc, status=r["status"], hired_at=r["hired_at"],
                      peak_equity=r["peak_equity"], day_start_equity=r["day_start_equity"], day_key=r["day_key"],
                      notes=json.loads(r["notes"] or "[]"), last_decided_ts=int(r.get("last_decided_ts") or 0),
                      slot_minute=int(r.get("slot_minute") or 0), next_check_ts=int(r.get("next_check_ts") or 0),
                      alert_above=float(r.get("alert_above") or 0), alert_below=float(r.get("alert_below") or 0),
                      stop_price=float(r.get("stop_price") or 0))
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
    def _step_seconds(self) -> int:
        from .data.market import TIMEFRAME_SECONDS
        return TIMEFRAME_SECONDS.get(self.s.timeframe, 3600)

    def next_decision_ts(self, a: Agent, now: int | None = None) -> int:
        return a.next_check_ts

    def _is_due(self, a: Agent, now_i: int, price: float, force: bool) -> tuple[bool, str]:
        if force or a.next_check_ts <= now_i:
            return True, "по расписанию"
        if a.alert_above and price >= a.alert_above:
            return True, f"цена выше будильника {a.alert_above:.0f}"
        if a.alert_below and price <= a.alert_below:
            return True, f"цена ниже будильника {a.alert_below:.0f}"
        return False, ""

    def _schedule_next(self, a: Agent, sig, now_i: int) -> None:
        """Агент сам говорит, когда смотреть на рынок в следующий раз."""
        if a.strategy.uses_llm():
            minutes = int((sig.meta or {}).get("next_check_minutes") or a.strategy.cadence_minutes())
            floor_min = self.s.llm_min_interval_min
            if a.strategy.family == "llm_news":
                floor_min = max(floor_min, self.s.llm_news_min_interval_min)
            minutes = max(floor_min, min(self.s.llm_max_interval_min, minutes))
            a.alert_above = float((sig.meta or {}).get("wake_if_above") or 0)
            a.alert_below = float((sig.meta or {}).get("wake_if_below") or 0)
        else:
            minutes = max(1, int(a.strategy.cadence_minutes()))
        a.next_check_ts = now_i + minutes * 60

    def _atr_pct(self, candles: list[Candle]) -> float:
        tail = candles[-60:]
        if len(tail) < 20:
            return 0.01
        a = ind.atr([c.high for c in tail], [c.low for c in tail], [c.close for c in tail], 14)[-1]
        return (a / tail[-1].close) if a and tail[-1].close else 0.01

    def _after_trade(self, a: Agent, trade, price: float, atr_pct: float) -> None:
        """После сделки: выставить или снять стоп-лосс."""
        if a.account.btc <= 0:
            a.stop_price = 0.0
        elif trade is not None and trade.side == "BUY":
            entry = a.account._avg_entry or price
            a.stop_price = entry * (1 - self.risk.stop_distance(atr_pct))

    def _check_stops(self, alive: list[Agent], price: float, now_i: int) -> list[str]:
        """Каждую минуту: если цена ушла ниже стопа, закрыть позицию и дать агенту паузу перед новым входом."""
        hit = []
        for a in alive:
            if a.account.btc > 0 and a.stop_price and price <= a.stop_price:
                t = a.account.flatten(price, now_i, f"стоп-лосс {a.stop_price:.0f}")
                if t:
                    self.j.trade(t)
                    self.j.decision(now_i, a.name, a.strategy.family, "SELL", 0.0, 1.0, f"стоп-лосс: цена {price:.0f} ниже {a.stop_price:.0f}",
                                    price, a.equity(price), True, None, trade=t, exposure_before=a.account.exposure(price))
                    self.j.equity(now_i, a.name, a.equity(price), price)
                    self.j.event("stop", f"{a.name}: сработал стоп-лосс на {a.stop_price:.0f}", a.name, ts=now_i)
                    hit.append(a.name)
                a.stop_price = 0.0
                a.last_target = 0.0
                a.next_check_ts = max(a.next_check_ts, now_i + self.s.stop_cooldown_min * 60)
        return hit

    def _view(self, candles: list[Candle], price: float, now_i: int) -> list[Candle]:
        """Закрытые свечи плюс формирующаяся, доведённая до текущей цены."""
        step = self._step_seconds()
        last = candles[-1]
        f = None
        try:
            f = self.market.forming(self.s.symbol, self.s.timeframe)
        except Exception:  # noqa: BLE001
            f = None
        if f is None or f.ts <= last.ts:
            f = Candle(last.ts + step, last.close, last.close, last.close, last.close, 0.0)
        f = Candle(f.ts, f.open, max(f.high, price), min(f.low, price), price, f.volume)
        return candles + [f]

    def tick(self, candles: list[Candle] | None = None, force: bool = False, now: float | None = None) -> dict:
        """Один проход планировщика (раз в минуту).

        Свободный режим: каждый агент сам задаёт, когда ему смотреть на рынок. Стратегии на
        правилах — своим темпом (от минуты до часа), нейро-агенты — по своему решению и по
        будильникам на цену. Смотрят они на закрытые свечи плюс текущую, доведённую до живой цены.
        """
        now_i = int(now or time.time())
        step = self._step_seconds()
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
        try:
            price = float(self.market.price(self.s.symbol))
        except Exception as e:  # noqa: BLE001
            log.warning("текущая цена недоступна (%s), беру закрытие свечи", e)
            price = last.close
        self.last_candles = candles
        self.last_price = price
        self.last_error = ""
        self.last_poll_ts = now_i
        new_candle = last.ts > self.last_tick_ts
        if new_candle:
            self.last_tick_ts = last.ts
        dk = day_key_of(now_i)
        summary = {"ok": True, "ts": last.ts, "price": price, "new_candle": new_candle, "decisions": [], "fired": [], "hired": []}

        alive = [a for a in self.agents if a.status not in {"fired", "dropped"}]
        for a in alive:
            a.last_ts_seen = now_i
            if a.hired_at > now_i:
                a.hired_at = now_i
            a.roll_day(dk, price)
            a.observe(price)

        ok, why = self.risk.check_department([a for a in self.agents if is_team(a)], price, dk)
        if not ok:
            halted_now = False
            for a in self.agents:
                if is_team(a):
                    t = a.account.flatten(price, now_i, f"стоп отдела: {why}")
                    if t:
                        self.j.trade(t)
                        halted_now = True
                    if a.status != "paused":
                        a.status = "paused"
                        halted_now = True
                    self.j.equity(now_i, a.name, a.equity(price), price)
            if halted_now:
                self.j.event("halt", f"Отдел остановлен: {why}", None, ts=now_i)
            summary["halt"] = why

        atr_pct = self._atr_pct(candles)
        summary["stops"] = self._check_stops(alive, price, now_i)
        view = None
        for a in alive:
            due, why_due = self._is_due(a, now_i, price, force)
            if not due:
                continue
            if view is None:
                view = self._view(candles, price, now_i)
            a.last_decided_ts = now_i
            if is_intern(a):
                self._intern_step(a, view, price, now_i, atr_pct)
            elif is_team(a) and ok:
                summary["decisions"].append(self._agent_step(a, view, price, now_i, why_due, atr_pct))
                if a.status == "fired":
                    summary["fired"].append(a.name)
            else:
                a.next_check_ts = now_i + 60

        if new_candle:
            self.learner.after_tick(self.agents, candles)
        hired = self.head.hire_if_needed(self.agents, candles, now_i)
        new_interns = self.head.fill_interns(self.agents, candles, now_i)
        for h in hired + new_interns:
            h.last_ts_seen = now_i
            h.roll_day(dk, price)
            h.observe(price)
            self.j.equity(now_i, h.name, h.equity(price), price)
        self.agents.extend([h for h in hired if h not in self.agents])
        self.agents.extend(new_interns)
        summary["hired"] = [h.name for h in hired]
        summary["interns_added"] = [h.name for h in new_interns]
        if new_candle:
            self.head.review(self.agents, price, now_i)
        if not new_candle and not summary["decisions"] and view is None and not hired and not new_interns:
            summary["skipped"] = True
        self.save()
        return summary

    def _should_log(self, a: Agent, sig, trade, now_i: int) -> bool:
        """Не засорять журнал: пишем сделки, смену цели и часовой контрольный отпечаток."""
        if trade is not None or a.strategy.uses_llm():
            return True
        if abs(sig.target_exposure - a.last_target) > 1e-9:
            return True
        return now_i - a.last_logged_ts >= 3600

    def _intern_step(self, a: Agent, candles: list[Candle], price: float, ts: int, atr_pct: float = 0.01) -> None:
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
        exp_before = a.account.exposure(price)
        sized = self.risk.size(sig.target_exposure, atr_pct)
        t = a.account.rebalance(sized, price, ts, sig.reason)
        if t:
            self.j.trade(t)
        self._after_trade(a, t, price, atr_pct)
        eq = a.equity(price)
        if self._should_log(a, sig, t, ts):
            self.j.decision(ts, a.name, a.strategy.family, sig.action.value, sig.target_exposure, sig.confidence,
                            sig.reason, price, eq, True, None, trade=t, exposure_before=exp_before)
            self.j.equity(ts, a.name, eq, price)
            a.last_logged_ts = ts
        a.last_target = sig.target_exposure
        self._schedule_next(a, sig, ts)
        a.observe(price)
        if t:
            a.after_trade_tick(price)

    def _agent_step(self, a: Agent, candles: list[Candle], price: float, ts: int, why_due: str = "", atr_pct: float = 0.01) -> dict:
        ctx = {"exposure": a.account.exposure(price), "lessons": a.notes, "bars_in_position": a.bars_in_position,
               "llm_min_interval": self.s.llm_min_interval_min, "llm_max_interval": self.s.llm_max_interval_min, "woke_by": why_due}
        try:
            sig = a.strategy.decide(candles, ctx)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: ошибка стратегии", a.name)
            from .agents.base import hold
            sig = hold(f"ошибка стратегии: {e}", ctx["exposure"])
        a.last_signal = sig
        verdict = self.risk.check_agent(a, sig, price, atr_pct)
        executed = False
        blocked = None
        trade = None
        exp_before = a.account.exposure(price)
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
            trade = a.account.rebalance(verdict.target_exposure, price, ts, sig.reason)
            if trade:
                self.j.trade(trade)
            self._after_trade(a, trade, price, atr_pct)
            executed = True
        else:
            blocked = verdict.reason
        eq = a.equity(price)
        reason = sig.reason
        if verdict.allowed and abs(verdict.target_exposure - sig.target_exposure) > 1e-9 and sig.target_exposure > 0:
            reason = f"{sig.reason} · {verdict.reason}"
        if blocked or self._should_log(a, sig, trade, ts):
            self.j.decision(ts, a.name, a.strategy.family, sig.action.value, sig.target_exposure, sig.confidence,
                            reason, price, eq, executed, blocked, trade=trade, exposure_before=exp_before)
            if a.status != "fired":
                self.j.equity(ts, a.name, eq, price)
            a.last_logged_ts = ts
        a.last_target = sig.target_exposure
        if a.status != "fired":
            self._schedule_next(a, sig, ts)
        a.observe(price)
        if trade:
            a.after_trade_tick(price)
        return {"agent": a.name, "action": sig.action.value, "target": sig.target_exposure,
                "reason": sig.reason, "executed": executed, "blocked": blocked, "equity": round(eq, 2)}

    # --- панель ---
    def state(self) -> dict:
        price = self.last_price
        now_i = int(time.time())
        step = self._step_seconds()

        def enrich(a: Agent) -> dict:
            d = a.snapshot(price).__dict__
            d["next_decision_ts"] = self.next_decision_ts(a, now_i) if a.status in {"active", "paused", "intern"} else 0
            d["decided_at"] = a.last_decided_ts
            base = self.j.equity_at(a.name, now_i - 86400)
            if base is None or a.hired_at > now_i - 86400:
                base = a.start_balance()
            d["pnl_24h"] = round(a.equity(price) - base, 2)
            d["days"] = round(max(0.0, (now_i - a.hired_at) / 86400), 1)
            return d

        snaps = [enrich(a) for a in self.agents if a.status not in {"intern", "dropped"}]
        interns = [enrich(a) for a in self.agents if is_intern(a)]
        interns.sort(key=lambda d: d["pnl_total"], reverse=True)
        alive = [s for s in snaps if s["status"] != "fired"]
        total = sum(s["equity"] for s in alive)
        start = sum(a.start_balance() for a in self.agents if is_team(a))
        upcoming = sorted(({"name": d["name"], "ts": d["next_decision_ts"]} for d in snaps if d["next_decision_ts"]), key=lambda x: x["ts"])
        return {
            "now": now_i,
            "last_tick_ts": self.last_tick_ts,
            "candle_close_ts": (self.last_tick_ts + self._step_seconds()) if self.last_tick_ts else 0,
            "upcoming": upcoming[:6],
            "price": price,
            "symbol": self.s.symbol,
            "timeframe": self.s.timeframe,
            "market": getattr(self.market, "name", "?"),
            "mode": "paper",
            "llm": self.client.enabled if self.client else False,
            "llm_spend": self.client.spend_today() if self.client else None,
            "llm_model": self.s.llm_model,
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
            "decisions": self.j.recent_decisions(None, 60),
            "trades_24h": self._trades_24h(now_i),
            "last_poll_ts": self.last_poll_ts,
            "max_exposure": self.s.agent_max_exposure,
            "risk_per_trade": self.s.risk_per_trade,
        }

    def _trades_24h(self, now_i: int) -> list[dict]:
        kinds = {a.name: ("intern" if a.status in {"intern", "dropped"} else "team") for a in self.agents}
        out = []
        for t in self.j.trades_since(now_i - 86400, 200):
            t["kind"] = kinds.get(t["agent"], "team")
            t["usd"] = round(t["price"] * t["qty"], 2)
            t["pnl_pct"] = round(t["pnl"] / t["cost"] * 100, 2) if t.get("pnl") is not None and t.get("cost") else None
            out.append(t)
        return out

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
