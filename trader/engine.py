"""Движок компании Botz: один проход планировщика раз в минуту.

Порядок на каждом тике:
1. взять свечи и живую цену → 2. обновить день/неделю/пики → 3. проверка лимитов компании →
4. аналитический отдел (если подошло время) → директор распределяет капитал по дескам →
5. стопы и ликвидации → 6. трейдеры и стажёры, у кого подошло время, решают → риск-менеджер →
   демосчёт исполняет → журнал → 7. обучение → 8. директор (разбор, найм, ротация) → 9. сохранить.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from .agents.base import Agent
from .agents.registry import (DESKS, EXPERIMENTS, RANK_INTERN, RANK_TRADER, DEFAULT_NAMES, build_strategy, default_department,
                              default_families, desk_of, family_label, family_side, new_account)
from .analytics import AnalyticsDept
from .broker import BinanceSpotBroker, BrokerError
from .data import indicators as ind
from .data.macro import describe as macro_describe, fetch_macro
from .push import PushSender, VapidKeys
from .config import Settings
from .data.market import MarketData, get_market
from .journal import Journal
from .learning import Learner
from .llm import ClaudeClient
from .manager import Director, is_intern, is_team
from .models import Candle, Trade
from .paper import PaperAccount
from .risk import RiskManager

log = logging.getLogger(__name__)


def day_key_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def is_experiment(a: Agent) -> bool:
    return a.status == "experiment"


class Engine:
    def __init__(self, settings: Settings, market: MarketData | None = None, journal: Journal | None = None,
                 client: ClaudeClient | None = None):
        self.s = settings
        self.market = market or get_market(settings.market_source)
        self.j = journal or Journal(settings.db_path)
        self.client = client if client is not None else ClaudeClient(
            settings.anthropic_api_key, settings.llm_model, daily_budget_usd=settings.llm_daily_budget_usd, spend_store=self.j,
            strong_model=settings.llm_model_strong)
        if self.client and self.client.spend_store is None:
            self.client.spend_store = self.j
        self.risk = RiskManager(settings)
        self.director = Director(settings, self.j, self.client)
        self.head = self.director          # старое имя, используется в тестах и cli
        self.analytics = AnalyticsDept(settings, self.j, self.client)
        self.learner = Learner(settings, self.j, self.client)
        self.agents: list[Agent] = []
        self.last_candles: list[Candle] = []
        self.last_tick_ts: int = self.j.kv_get("last_tick_ts", 0)
        self.last_price: float = float(self.j.kv_get("last_price", 0.0))
        self.last_poll_ts: int = 0
        self.last_error: str = ""
        self.m1: list[Candle] = [Candle(*row) if len(row) == 6 else Candle(row[0], row[1], row[2], row[3], row[4], 0.0)
                                 for row in (self.j.kv_get("m1", []) or [])]
        self._m1_seed_try: int = 0
        self._macro_try: int = 0
        self.push: PushSender | None = None
        try:
            pem = self.j.kv_get("vapid_private_pem")
            keys = VapidKeys(pem) if pem else VapidKeys.generate()
            if not pem:
                self.j.kv_set("vapid_private_pem", keys.private_pem)
            self.push = PushSender(keys, settings.push_subject)
        except Exception as e:  # noqa: BLE001
            log.warning("push-уведомления недоступны: %s", e)
        self.broker = BinanceSpotBroker(settings.live_api_key, settings.live_api_secret, settings.live_testnet) if settings.live_enabled else None
        self.live_pos: dict[str, float] = self.j.kv_get("live_pos", {}) or {}
        self._load()
        n = self.j.repair_short_pnl()
        if n:
            log.info("исправлен итог %d сделок фьючерсных агентов", n)
            self.j.event("start", f"Починка журнала: у {n} сделок медведей и двусторонних исправлен итог (открытие шорта не считается закрытием лонга)")
        n = self.j.backfill_trade_pnl(settings.fee_rate)
        if n:
            log.info("дописан итог %d старым продажам", n)

    # --- состояние ---
    def _load(self) -> None:
        rows = self.j.load_agents()
        if not rows:
            self.agents = default_department(self.s, self.client)
            for a in self.agents:
                self.j.save_agent(a)
            self.j.event("start", f"Компания Botz создана: три деска, {len(self.agents)} трейдеров")
            self._ensure_defaults()
            return
        for r in rows:
            if r["strategy"].startswith("llm_") or r["strategy"] in {"llm_regime_both"}:
                # нейро-агенты больше не торгуют: они стали аналитическим отделом
                self.j.set_status(r["name"], "moved", int(time.time()))
                self.j.event("analytics", f"{r['name']} переведён из трейдеров в аналитический отдел: нейросеть теперь советует директору, а не торгует", r["name"])
                continue
            strat = build_strategy(r["strategy"], json.loads(r["params"]), self.client)
            acc = PaperAccount(owner=r["name"], cash=r["cash"], btc=r["btc"], fee_rate=self.s.fee_rate,
                               slippage_rate=self.s.slippage_rate, allow_short=strat.side != "long")
            acc.realized_pnl = r["realized_pnl"]
            acc._avg_entry = r["avg_entry"]
            # сделки только текущего «срока»: после перевода в команду или в стажёры счёт начинается заново
            acc.trades = [Trade(t["ts"], t["agent"], t["side"], t["price"], t["qty"], t["fee"], t["reason"] or "",
                                pnl=t.get("pnl"), cost=t.get("cost"), pos_after=float(t.get("pos_after") or 0.0))
                          for t in self.j.trades_for(r["name"]) if t["ts"] >= int(r["hired_at"] or 0)]
            a = Agent(name=r["name"], strategy=strat, account=acc, status=r["status"], hired_at=r["hired_at"],
                      peak_equity=r["peak_equity"], day_start_equity=r["day_start_equity"], day_key=r["day_key"],
                      notes=json.loads(r["notes"] or "[]"), last_decided_ts=int(r.get("last_decided_ts") or 0),
                      slot_minute=int(r.get("slot_minute") or 0), next_check_ts=int(r.get("next_check_ts") or 0),
                      alert_above=float(r.get("alert_above") or 0), alert_below=float(r.get("alert_below") or 0),
                      stop_price=float(r.get("stop_price") or 0), week_start_equity=float(r.get("week_start_equity") or 0),
                      week_key=r.get("week_key") or "", streak_weeks=int(r.get("streak_weeks") or 0),
                      trial_weeks=int(r.get("trial_weeks") or 0), live_ready=bool(r.get("live_ready") or 0),
                      rank=int(r.get("rank") if r.get("rank") is not None else (RANK_INTERN if r["status"] == "intern" else RANK_TRADER)),
                      best_price=float(r.get("best_price") or 0), partial_taken=bool(r.get("partial_taken") or 0),
                      exit_price=float(r.get("exit_price") or 0), exit_side=r.get("exit_side") or "", exit_ts=int(r.get("exit_ts") or 0))
            a._start_balance = r["start_balance"]
            a.last_price = self.last_price
            self.agents.append(a)
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        """Если на деске есть свободные места, а в коде появились новые штатные агенты, добавить их.
        Если деск переполнен (после перестройки компании), лишние худшие уходят в стажёры."""
        known = {r["strategy"] for r in self.j.all_agents()}
        for family, name in default_families(self.s):
            desk = desk_of(family_side(family))
            if family in known or len(self.director.desk_members(self.agents, desk)) >= self.director.desk_size:
                continue
            strat = build_strategy(family, None, self.client)
            a = Agent(name=name, strategy=strat, account=new_account(self.s, name, allow_short=strat.side != "long"))
            a.last_price = self.last_price
            self.agents.append(a)
            self.j.save_agent(a)
            self.j.event("hire", f"В деск «{DESKS[desk]['label']}» добавлен новый штатный трейдер: {name}", name)
        if self.last_price:
            self.director.trim_desks(self.agents, self.last_price, int(time.time()))
        if self.s.experiments:
            for family, name in EXPERIMENTS.items():
                if family in known:
                    continue
                strat = build_strategy(family, None, self.client)
                a = Agent(name=name, strategy=strat, account=new_account(self.s, name, allow_short=strat.side != "long"), status="experiment", rank=RANK_INTERN)
                a.last_price = self.last_price
                self.agents.append(a)
                self.j.save_agent(a)
                self.j.event("hire", f"Запущен эксперимент: {name} ({strat.description}). Торгует на своём счёте вне десков", name)

    def save(self) -> None:
        for a in self.agents:
            self.j.save_agent(a)
        self.j.kv_set("last_tick_ts", self.last_tick_ts)
        self.j.kv_set("last_price", self.last_price)
        self.j.kv_set("m1", [[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in self.m1[-1440:]])

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

    def _update_m1(self, price: float, now_i: int) -> None:
        """Минутные свечи из живой цены (для скальперов). При первом запуске подтягиваются с биржи."""
        if not self.m1 and now_i - self._m1_seed_try > 1800:
            self._m1_seed_try = now_i
            try:
                seed = self.market.candles(self.s.symbol, "1m", 300)
                self.m1 = [c for c in seed if c.ts < now_i - now_i % 60]
            except Exception as e:  # noqa: BLE001
                log.warning("минутные свечи с биржи недоступны: %s", e)
        minute = now_i - now_i % 60
        if self.m1 and self.m1[-1].ts == minute:
            last = self.m1[-1]
            self.m1[-1] = Candle(minute, last.open, max(last.high, price), min(last.low, price), price, last.volume)
        elif not self.m1 or self.m1[-1].ts < minute:
            self.m1.append(Candle(minute, price, price, price, price, 0.0))
        if len(self.m1) > 1500:
            self.m1 = self.m1[-1440:]

    def _atr_pct(self, candles: list[Candle]) -> float:
        tail = candles[-60:]
        if len(tail) < 20:
            return 0.01
        a = ind.atr([c.high for c in tail], [c.low for c in tail], [c.close for c in tail], 14)[-1]
        return (a / tail[-1].close) if a and tail[-1].close else 0.01

    def _after_trade(self, a: Agent, trade, price: float, atr_pct: float) -> None:
        """После сделки: выставить или снять стоп-лосс (для шорта стоп выше входа).
        Уже подтянутый стоп при доборе позиции назад не опускается."""
        pos = a.account.btc
        if abs(pos) < 1e-12:
            if trade is not None and trade.pnl is not None:
                a.exit_price, a.exit_side, a.exit_ts = trade.price, ("long" if trade.side == "SELL" else "short"), trade.ts
            a.stop_price = 0.0
            a.best_price = 0.0
            a.partial_taken = False
        elif trade is not None:
            entry = a.account._avg_entry or price
            dist = self.risk.stop_distance(atr_pct)
            opening = trade.pnl is None and (a.stop_price <= 0 or abs(trade.pos_after - (trade.qty if trade.side == "BUY" else -trade.qty)) < 1e-12)
            if pos > 0 and trade.side == "BUY":
                new_stop = entry * (1 - dist)
                a.stop_price = new_stop if opening else max(a.stop_price, new_stop)
                if opening:
                    a.best_price, a.partial_taken = price, False
            elif pos < 0 and trade.side == "SELL":
                new_stop = entry * (1 + dist)
                a.stop_price = new_stop if opening else min(a.stop_price, new_stop)
                if opening:
                    a.best_price, a.partial_taken = price, False

    def _manage_position(self, a: Agent, price: float, now_i: int, atr_pct: float) -> str | None:
        """Сопровождение открытой позиции: стоп в безубыток, подтягивающийся стоп, частичная фиксация.

        Возвращает подпись сработавшего стопа («стоп-лосс», «стоп в безубыток», «подтянутый стоп») или None.
        """
        pos = a.account.btc
        entry = a.account._avg_entry or price
        if abs(pos) < 1e-12 or not entry or atr_pct <= 0:
            return None
        sign = 1 if pos > 0 else -1
        a.best_price = max(a.best_price or price, price) if sign > 0 else min(a.best_price or price, price)
        gain_atr = sign * (price / entry - 1) / atr_pct            # прибыль в единицах ATR
        fee2 = 2 * self.s.fee_rate + 2 * self.s.slippage_rate      # издержки входа и выхода
        # частичная фиксация прибыли
        if self.s.partial_tp_atr > 0 and not a.partial_taken and gain_atr >= self.s.partial_tp_atr:
            exp = a.account.exposure(price)
            t = a.account.rebalance(exp * (1 - self.s.partial_tp_frac), price, now_i,
                                    f"фиксация {self.s.partial_tp_frac:.0%} позиции: прибыль {sign * (price / entry - 1) * 100:+.2f}% ({gain_atr:.1f}·ATR)")
            a.partial_taken = True
            if t:
                self.j.trade(t)
                self.j.decision(now_i, a.name, a.strategy.family, t.side, a.account.exposure(price), 1.0,
                                f"фиксация части прибыли: {sign * (price / entry - 1) * 100:+.2f}% от входа, остаток идёт с подтянутым стопом",
                                price, a.equity(price), True, None, trade=t, exposure_before=exp)
                self.j.equity(now_i, a.name, a.equity(price), price)
                self.j.event("stop", f"{a.name}: зафиксировал {self.s.partial_tp_frac:.0%} позиции с прибылью {sign * (price / entry - 1) * 100:+.2f}%", a.name, ts=now_i)
                a.last_target = a.account.exposure(price)
                if abs(a.account.btc) < 1e-12:
                    a.stop_price, a.best_price, a.partial_taken = 0.0, 0.0, False
                    return None
        # подтягивающийся стоп: только в сторону прибыли
        if self.s.trailing_stop and a.stop_price:
            dist = self.risk.stop_distance(atr_pct)
            trail = a.best_price * (1 - sign * dist)
            candidate = trail
            if gain_atr >= self.s.trail_breakeven_atr or a.partial_taken:
                breakeven = entry * (1 + sign * fee2)
                candidate = max(candidate, breakeven) if sign > 0 else min(candidate, breakeven)
            a.stop_price = max(a.stop_price, candidate) if sign > 0 else min(a.stop_price, candidate)
        return None

    def stop_kind(self, a: Agent) -> str:
        """Подпись стопа: обычный, безубыток или подтянутый (прибыль защищена)."""
        pos = a.account.btc
        entry = a.account._avg_entry
        if not a.stop_price or not entry or abs(pos) < 1e-12:
            return ""
        sign = 1 if pos > 0 else -1
        edge = sign * (a.stop_price / entry - 1)
        if edge > 2 * self.s.fee_rate + 2 * self.s.slippage_rate + 1e-9:
            return "trailing"
        if edge >= -1e-9:
            return "breakeven"
        return "initial"

    def _apply_funding(self, alive: list[Agent], price: float, candle_ts: int) -> None:
        """Финансирование фьючерсов раз в 8 часов (00:00, 08:00, 16:00 UTC) по текущей ставке биржи."""
        step = self._step_seconds()
        if (candle_ts + step) % 28800 != 0:
            return
        futures = [a for a in alive if a.account.allow_short and abs(a.account.btc) > 1e-12]
        if not futures:
            return
        try:
            rate = float(self.market.funding_rate(self.s.symbol))
        except Exception:  # noqa: BLE001
            rate = 0.0001
        self.j.kv_set("funding_rate", rate)
        total = 0.0
        for a in futures:
            total += a.account.apply_funding(price, 8.0, rate_8h=rate)
        self.j.event("funding", f"Финансирование фьючерсов: ставка {rate*100:+.4f}%, отдел {'заплатил' if total > 0 else 'получил'} {abs(total):.2f} $",
                     None, {"rate": rate, "total": total}, ts=candle_ts + step)

    def _check_liquidations(self, alive: list[Agent], price: float, now_i: int) -> list[str]:
        out = []
        for a in alive:
            acc = a.account
            if acc.allow_short and abs(acc.btc) > 1e-12 and acc.maintenance_ratio(price) <= self.s.liquidation_ratio:
                t = acc.liquidate(price, now_i)
                if t:
                    self.j.trade(t)
                    self.j.decision(now_i, a.name, a.strategy.family, t.side, 0.0, 1.0, "ликвидация: капитал ниже поддерживающей маржи",
                                    price, a.equity(price), True, None, trade=t, exposure_before=0.0)
                    self.j.event("liquidation", f"{a.name}: позиция ликвидирована биржей", a.name, ts=now_i)
                a.stop_price = 0.0
                a.last_target = 0.0
                a.next_check_ts = max(a.next_check_ts, now_i + self.risk.stop_cooldown_min * 60)
                out.append(a.name)
        return out

    def _check_stops(self, alive: list[Agent], price: float, now_i: int, atr_1h: float = 0.01) -> list[str]:
        """Каждую минуту: сопроводить позицию (безубыток, подтягивание, частичная фиксация) и,
        если цена дошла до стопа, закрыть позицию и дать агенту паузу перед новым входом."""
        hit = []
        KIND = {"trailing": "подтянутый стоп", "breakeven": "стоп в безубыток", "initial": "стоп-лосс", "": "стоп-лосс"}
        for a in alive:
            hf = getattr(a.strategy, "timeframe", "1h") == "1m"
            atr_pct = getattr(self, "atr_m1", 0.003) if hf else atr_1h
            pos = a.account.btc
            if abs(pos) * price >= 1.0 and not a.stop_price:
                # страховка: позиция без стопа (после сбоя или перезапуска) получает стоп от цены входа
                entry = a.account._avg_entry or price
                dist = self.risk.stop_distance(atr_pct)
                a.stop_price = entry * (1 - dist) if pos > 0 else entry * (1 + dist)
                a.best_price = a.best_price or price
                last = a.account.trades[-1].reason if a.account.trades else "—"
                self.j.event("stop", f"{a.name}: у открытой позиции не было стопа, выставлен {a.stop_price:.0f} от входа {entry:.0f} "
                                     f"(последняя сделка: {last[:60]})", a.name, ts=now_i)
            self._manage_position(a, price, now_i, atr_pct)
            pos = a.account.btc
            triggered = a.stop_price and ((pos > 0 and price <= a.stop_price) or (pos < 0 and price >= a.stop_price))
            if triggered:
                kind = self.stop_kind(a)
                label = KIND[kind]
                fill_px = price * (1 - self.s.stop_slippage) if pos > 0 else price * (1 + self.s.stop_slippage)
                t = a.account.flatten(fill_px, now_i, f"{label} {a.stop_price:.0f}")
                if t:
                    a.exit_price, a.exit_side, a.exit_ts = t.price, ("long" if pos > 0 else "short"), now_i
                    self._mirror_live(a, t, price, now_i)
                    self.j.trade(t)
                    self.j.decision(now_i, a.name, a.strategy.family, t.side, 0.0, 1.0, f"{label}: цена {price:.0f} {'ниже' if pos > 0 else 'выше'} {a.stop_price:.0f}",
                                    price, a.equity(price), True, None, trade=t, exposure_before=a.account.exposure(price))
                    self.j.equity(now_i, a.name, a.equity(price), price)
                    self.j.event("stop", f"{a.name}: сработал {label} на {a.stop_price:.0f}" + (f", итог {t.pnl:+.2f} $" if t.pnl is not None else ""), a.name, ts=now_i)
                    hit.append(a.name)
                a.best_price = 0.0
                a.partial_taken = False
                a.stop_price = 0.0
                a.last_target = 0.0
                cooldown = self.s.exit_cooldown_min if kind in {"trailing", "breakeven"} else self.risk.stop_cooldown_min
                if hf:
                    cooldown = 1
                a.next_check_ts = max(a.next_check_ts, now_i + cooldown * 60)
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
        self._update_m1(price, now_i)
        self.atr_m1 = self._atr_pct(self.m1) if len(self.m1) >= 20 else 0.003
        new_candle = last.ts > self.last_tick_ts
        if new_candle:
            self.last_tick_ts = last.ts
        dk = day_key_of(now_i)
        summary = {"ok": True, "ts": last.ts, "price": price, "new_candle": new_candle, "decisions": [], "fired": [], "hired": []}

        alive = [a for a in self.agents if a.status not in {"fired", "dropped"}]
        wk = self.director.week_key(now_i)
        new_week = self.j.kv_get("week_key") != wk
        day_results: list[tuple[Agent, float]] = []     # итоги вчерашнего дня для памяти компании
        for a in alive:
            a.last_ts_seen = now_i
            if a.hired_at > now_i:
                a.hired_at = now_i
            if a.day_key and a.day_key != dk and a.day_start_equity > 0:
                day_results.append((a, (a.equity(price) - a.day_start_equity) / a.day_start_equity * 100))
            a.roll_day(dk, price)
            a.observe(price)
        if day_results:
            yesterday = datetime.fromtimestamp(now_i - 86400, tz=timezone.utc).strftime("%Y-%m-%d")
            regime_y = self.director.regime_for_day(yesterday)
            n = self.director.remember_day(day_results, regime_y, now_i)
            kc = self.j.knowledge_counts()
            self.j.event("knowledge", f"База знаний: память пополнена итогами {n} трейдеров и стажёров за {yesterday} (режим: "
                                      f"{ {'up': 'рост', 'flat': 'боковик', 'down': 'падение'}.get(regime_y, regime_y)}); "
                                      f"правил действует {kc.get('rule', {}).get('active', 0)}, уроков {kc.get('lesson', {}).get('active', 0) + kc.get('lesson', {}).get('verified', 0)}, "
                                      f"наблюдений стратега {kc.get('insight', {}).get('active', 0)}", None, ts=now_i)
        if new_week:
            if self.j.kv_get("week_key"):        # не при самом первом запуске
                summary["weekly"] = self.director.weekly_review(self.agents, price, now_i, candles)
            self.j.kv_set("week_key", wk)
            for a in self.agents:
                if a.status not in {"fired", "dropped"}:
                    a.roll_week(wk, price)

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
                self.j.event("halt", f"Компания остановлена: {why}", None, ts=now_i)
            summary["halt"] = why

        new_views = []
        try:
            new_views = self.analytics.run_due(candles, price, now_i)
        except Exception as e:  # noqa: BLE001
            log.exception("аналитический отдел: %s", e)
        if new_views:
            summary["views"] = new_views
        if self.j.kv_get("head_day") != dk or new_views:
            self.j.kv_set("head_day", dk)
            self.director.daily_policy(candles, now_i, self.analytics.consensus(now_i))
        intraday = self.director.intraday_check(price, now_i)
        if intraday:
            summary["intraday"] = intraday
        pol = self.director.policy()
        self.risk.desk_caps = {k: float(v) for k, v in (pol.get("caps") or {}).items()} or {"bulls": 1.0, "bears": 1.0, "both": 1.0}
        self.risk.set_rules(self.j.active_rules())
        self.regime = pol.get("regime", "flat")
        for a in alive:
            a.account.min_rebalance_frac = float(pol.get("min_rebalance", 0.05))
        atr_pct = self._atr_pct(candles)
        summary["liquidations"] = self._check_liquidations(alive, price, now_i)   # биржа ликвидирует раньше, чем сработает наш стоп
        summary["stops"] = self._check_stops(alive, price, now_i, atr_pct)
        view = None
        for a in alive:
            due, why_due = self._is_due(a, now_i, price, force)
            if not due:
                continue
            if view is None:
                view = self._view(candles, price, now_i)
            a.last_decided_ts = now_i
            hf = getattr(a.strategy, "timeframe", "1h") == "1m"
            a_view, a_atr = (self.m1, self.atr_m1) if hf else (view, atr_pct)
            if is_intern(a) or is_experiment(a):
                self._intern_step(a, a_view, price, now_i, a_atr)
            elif is_team(a) and ok:
                summary["decisions"].append(self._agent_step(a, a_view, price, now_i, why_due, a_atr))
                if a.status == "fired":
                    summary["fired"].append(a.name)
            else:
                a.next_check_ts = now_i + 60

        if new_candle:
            self._apply_funding(alive, price, last.ts)
            self.learner.after_tick(self.agents, candles)
            self.director.review(self.agents, price, now_i)     # ежедневный разбор до найма: освободившиеся места займут сразу
            if self.j.kv_get("verify_day") != dk:
                self.j.kv_set("verify_day", dk)
                try:
                    self.analytics.verify_lessons(now_i)
                except Exception as e:  # noqa: BLE001
                    log.exception("проверка уроков: %s", e)
            if summary.get("weekly") is not None:
                try:
                    self.analytics.weekly_rule_proposal(self._worst_trades(now_i), now_i)
                except Exception as e:  # noqa: BLE001
                    log.exception("ревизор: %s", e)
            if self.analytics.strategist_due(now_i):
                try:
                    self.analytics.run_strategist(candles, price, now_i,
                                                  self.director.company_summary(self.agents, price, self.analytics.stats(), now_i))
                except Exception as e:  # noqa: BLE001
                    log.exception("стратег развития: %s", e)
        hired = self.director.hire_if_needed(self.agents, candles, now_i)
        new_interns = self.director.fill_interns(self.agents, candles, now_i)
        for h in hired + new_interns:
            h.last_ts_seen = now_i
            h.roll_day(dk, price)
            h.roll_week(wk, price)
            h.observe(price)
            self.j.equity(now_i, h.name, h.equity(price), price)
        self.agents.extend([h for h in hired if h not in self.agents])
        self.agents.extend(new_interns)
        summary["hired"] = [h.name for h in hired]
        summary["interns_added"] = [h.name for h in new_interns]
        self._refresh_macro(now_i)
        summary["alerts"] = self._check_alerts(price, now_i)
        if datetime.fromtimestamp(now_i, tz=timezone.utc).hour >= self.s.briefing_hour_utc and self.j.kv_get("briefing_day") != dk:
            self.j.kv_set("briefing_day", dk)
            try:
                self.morning_briefing(now_i)
            except Exception as e:  # noqa: BLE001
                log.exception("брифинг: %s", e)
        if not new_candle and not summary["decisions"] and view is None and not hired and not new_interns and not summary["alerts"]:
            summary["skipped"] = True
        self.save()
        self._push_new_events(now_i)
        return summary

    def _should_log(self, a: Agent, sig, trade, now_i: int) -> bool:
        """Не засорять журнал: пишем сделки, смену цели и часовой контрольный отпечаток."""
        if trade is not None or a.strategy.uses_llm():
            return True
        if abs(sig.target_exposure - a.last_target) > 1e-9:
            return True
        return now_i - a.last_logged_ts >= 3600

    def _risk_ctx(self, a: Agent, price: float, ts: int) -> dict:
        day_start_ts = ts - ts % 86400
        return {"hour": datetime.fromtimestamp(ts, tz=timezone.utc).hour, "regime": getattr(self, "regime", "flat"), "price": price, "ts": ts,
                "trades_today": sum(1 for t in a.account.trades if t.ts >= day_start_ts),
                "entries_today": sum(1 for t in a.account.trades if t.ts >= day_start_ts and t.pnl is None),
                "exit_price": a.exit_price, "exit_side": a.exit_side, "exit_ts": a.exit_ts}

    def _intern_step(self, a: Agent, candles: list[Candle], price: float, ts: int, atr_pct: float = 0.01) -> None:
        ctx = {"exposure": a.account.exposure(price), "bars_in_position": a.bars_in_position, "entry": a.account._avg_entry}
        try:
            sig = a.strategy.decide(candles, ctx)
        except Exception as e:  # noqa: BLE001
            from .agents.base import hold
            sig = hold(f"ошибка стратегии: {e}", ctx["exposure"])
        a.last_signal = sig
        if a.drawdown(price) >= self.s.agent_max_drawdown:
            if is_experiment(a):
                dd = a.drawdown(price) * 100
                a.account.flatten(price, ts, "перезапуск эксперимента")
                a.reset_account(self.s.agent_start_balance, ts)
                self.j.event("drop", f"Эксперимент {a.name}: просадка {dd:.1f}%, счёт перезапущен с {self.s.agent_start_balance:.0f} $", a.name, ts=ts)
                return
            self.director.drop_intern(a, price, ts, f"просадка {a.drawdown(price)*100:.1f}%")
            return
        exp_before = a.account.exposure(price)
        desired = sig.target_exposure if a.account.allow_short else max(0.0, sig.target_exposure)
        sized = self.risk.size(desired, atr_pct, a.desk, getattr(self, "regime", None))
        sized, note = self.risk.apply_rules(a, sized, {**self._risk_ctx(a, price, ts), "exposure": exp_before, "family": a.strategy.family})
        for rid in self.risk.rule_hits:
            self.j.knowledge_hit(rid)
        t = a.account.rebalance(sized, price, ts, sig.reason + (f" · {note}" if note else ""))
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
        ctx = {"exposure": a.account.exposure(price), "lessons": a.notes, "bars_in_position": a.bars_in_position, "entry": a.account._avg_entry,
               "llm_min_interval": self.s.llm_min_interval_min, "llm_max_interval": self.s.llm_max_interval_min, "woke_by": why_due}
        try:
            sig = a.strategy.decide(candles, ctx)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: ошибка стратегии", a.name)
            from .agents.base import hold
            sig = hold(f"ошибка стратегии: {e}", ctx["exposure"])
        a.last_signal = sig
        rctx = self._risk_ctx(a, price, ts)
        verdict = self.risk.check_agent(a, sig, price, atr_pct, rctx)
        for rid in self.risk.rule_hits:
            self.j.knowledge_hit(rid)
        executed = False
        blocked = None
        trade = None
        exp_before = a.account.exposure(price)
        if verdict.fire:
            self.director.fire(a, price, ts, verdict.reason)
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
                self._mirror_live(a, trade, price, ts)
            self._after_trade(a, trade, price, atr_pct)
            executed = True
        else:
            blocked = verdict.reason
        eq = a.equity(price)
        reason = sig.reason
        rule_block = verdict.reason.startswith("правило")
        if verdict.allowed and (rule_block or (abs(verdict.target_exposure - sig.target_exposure) > 1e-9 and abs(sig.target_exposure) > 0)):
            reason = f"{sig.reason} · {verdict.reason}"
        if blocked or rule_block or self._should_log(a, sig, trade, ts):
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

        def enrich(a: Agent) -> dict:
            d = a.snapshot(price).__dict__
            d["next_decision_ts"] = self.next_decision_ts(a, now_i) if a.status in {"active", "paused", "intern"} else 0
            d["decided_at"] = a.last_decided_ts
            base = self.j.equity_at(a.name, now_i - 86400)
            if base is None or a.hired_at > now_i - 86400:
                base = a.start_balance()
            d["pnl_24h"] = round(a.equity(price) - base, 2)
            d["days"] = round(max(0.0, (now_i - a.hired_at) / 86400), 1)
            d["start_balance"] = round(a.start_balance(), 2)
            return d

        snaps = [enrich(a) for a in self.agents if a.status not in {"intern", "dropped", "experiment"}]
        interns = [enrich(a) for a in self.agents if is_intern(a)]
        experiments = [enrich(a) for a in self.agents if is_experiment(a)]
        team_names = {a.name for a in self.agents if is_team(a)}
        desk_names = {d: {a.name for a in self.agents if is_team(a) and a.desk == d} for d in DESKS}
        stats = {}
        for key, since in (("7d", now_i - 7 * 86400), ("all", 0)):
            st = self.j.trade_stats(team_names, since)
            st["desks"] = {d: self.j.trade_stats(desk_names[d], since) for d in DESKS}
            stats[key] = st
        interns.sort(key=lambda d: d["pnl_total"], reverse=True)
        alive = [s for s in snaps if s["status"] != "fired"]
        total = sum(s["equity"] for s in alive)
        start = sum(a.start_balance() for a in self.agents if is_team(a))
        upcoming = sorted(({"name": d["name"], "ts": d["next_decision_ts"]} for d in snaps if d["next_decision_ts"]), key=lambda x: x["ts"])
        pol = self.director.policy()
        desks = []
        for key, meta in DESKS.items():
            members = [s for s in alive if s["desk"] == key]
            d_start = sum(a.start_balance() for a in self.agents if is_team(a) and a.desk == key)
            desks.append({
                "key": key, "label": meta["label"], "side": meta["side"], "description": meta["description"],
                "size": self.director.desk_size, "agents": len(members),
                "equity": round(sum(s["equity"] for s in members), 2), "start": round(d_start, 2),
                "pnl": round(sum(s["equity"] for s in members) - d_start, 2),
                "pnl_day": round(sum(s["pnl_day"] for s in members), 2),
                "pnl_24h": round(sum(s["pnl_24h"] for s in members), 2),
                "pnl_week": round(sum(s["pnl_week"] for s in members), 2),
                "cap": float((pol.get("caps") or {}).get(key, 1.0)),
                "in_position": len([s for s in members if abs(s["exposure"]) > 1e-9]),
                "interns": len([i for i in interns if i["desk"] == key]),
            })
        week_ago = now_i - 7 * 86400
        ev7 = self.j.event_counts(week_ago)
        return {
            "now": now_i,
            "company": "Botz",
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
            "llm_error": {"text": self.client.last_error, "ts": self.client.last_error_ts} if self.client and self.client.last_error else None,
            "llm_model": self.s.llm_model,
            "error": self.last_error,
            "department": {"equity": round(total, 2), "start": round(start, 2), "pnl": round(total - start, 2),
                           "agents_active": len([s for s in alive if s["status"] == "active"]),
                           "agents_paused": len([s for s in alive if s["status"] == "paused"]),
                           "halted": self.risk.dept_halted_day == day_key_of(self.last_tick_ts) if self.last_tick_ts else False},
            "desks": desks,
            "agents": snaps,
            "interns": interns,
            "experiments": experiments,
            "stats": stats,
            "m1_count": len(self.m1),
            "team_size": self.director.team_size,
            "desk_size": self.director.desk_size,
            "intern_count": self.s.intern_count,
            "bench": self.j.bench()[:10],
            "approvals": self.j.pending_approvals(),
            "events": self.j.recent_events(40),
            "decisions": self.j.recent_decisions(None, 60),
            "trades_24h": self._trades_24h(now_i),
            "positions": self.open_positions(now_i),
            "last_poll_ts": self.last_poll_ts,
            "max_exposure": self.s.agent_max_exposure,
            "risk_per_trade": self.s.risk_per_trade,
            "head": {**pol, "director": self.director.director(), "senior_weeks": self.s.senior_weeks,
                     "strategist_interval_h": self.s.strategist_interval_h, "bonus_pct": self.s.director_bonus_pct},
            "directors_history": self.j.knowledge("director", "retired", limit=10),
            "analysts": self.analytics.stats(),
            "consensus": self.analytics.consensus(now_i),
            "funding_rate": self.j.kv_get("funding_rate", None),
            "risk": {
                "limits": {"agent_daily_loss": self.s.agent_daily_loss_limit, "agent_max_drawdown": self.s.agent_max_drawdown,
                           "dept_daily_loss": self.s.dept_daily_loss_limit, "stop_atr_mult": self.s.stop_atr_mult,
                           "liquidation_ratio": self.s.liquidation_ratio, "max_exposure": self.s.agent_max_exposure},
                "week": {"stops": ev7.get("stop", 0), "liquidations": ev7.get("liquidation", 0), "pauses": ev7.get("pause", 0),
                         "fires": ev7.get("fire", 0), "halts": ev7.get("halt", 0)},
                "max_drawdown": round(max([s["drawdown"] for s in alive], default=0.0), 4),
                "in_position": len([s for s in alive if abs(s["exposure"]) > 1e-9]),
            },
            "science": {
                "families": len(self.director.lab.families_count()),
                "bench": len(self.j.bench()),
                "week": {"interns": ev7.get("intern", 0), "promoted": ev7.get("hire", 0), "dropped": ev7.get("drop", 0),
                         "research": ev7.get("research", 0)},
            },
            "learning": {
                "week": {"lessons": ev7.get("lesson", 0), "retunes": ev7.get("retune", 0)},
                "events": self.j.events_of(("lesson", "retune"), 12),
            },
            "knowledge": self.knowledge_state(),
            "stress": self.stress_test(),
            "macro": self.macro(),
            "alerts": self.j.alerts(),
            "alert_kinds": self.ALERT_KINDS,
            "briefing": (self.j.knowledge("briefing", "active", limit=1) or [None])[0],
            "push": {"enabled": bool(self.push), "subscribers": len(self.j.push_subs())},
            "live": self.live_state(),
            "heatmap": {"7d": self.j.heatmap(team_names, now_i - 7 * 86400), "all": self.j.heatmap(team_names, 0)},
        }

    def _save_policy_via_director(self, pol: dict) -> None:
        self.director._save_policy(pol)

    def _worst_trades(self, now_i: int, limit: int = 12) -> list[dict]:
        """Худшие закрытые сделки за неделю с контекстом для ревизора."""
        meta = {a.name: (a.desk, a.strategy.family) for a in self.agents}
        out = []
        for t in self.j.trades_since(now_i - 7 * 86400, 2000):
            if t.get("pnl") is None or t["pnl"] >= 0:
                continue
            desk, fam = meta.get(t["agent"], ("bulls", "?"))
            day = datetime.fromtimestamp(t["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
            out.append({"agent": t["agent"], "desk": desk, "family": fam, "hour": datetime.fromtimestamp(t["ts"], tz=timezone.utc).hour,
                        "regime": self.director.regime_for_day(day), "pnl": float(t["pnl"]), "reason": t.get("reason") or ""})
        out.sort(key=lambda x: x["pnl"])
        return out[:limit]

    # --- внешние данные ---
    def _refresh_macro(self, now_i: int) -> None:
        if now_i - self._macro_try < 3600 or getattr(self.market, "name", "") == "synthetic":
            return
        self._macro_try = now_i
        try:
            m = fetch_macro(self.s.symbol)
            if len(m) > 1:
                self.j.kv_set("macro", m)
        except Exception as e:  # noqa: BLE001
            log.warning("внешние данные: %s", e)

    def macro(self) -> dict | None:
        return self.j.kv_get("macro", None)

    # --- сигналы владельца ---
    ALERT_KINDS = {
        "price_above": "цена выше", "price_below": "цена ниже",
        "company_day_below": "компания за день хуже, %", "company_day_above": "компания за день лучше, %",
        "desk_day_below": "деск за день хуже, %", "agent_day_below": "трейдер за день хуже, %",
        "agent_entry": "трейдер открыл позицию", "agent_exit": "трейдер закрыл позицию",
    }

    def _check_alerts(self, price: float, now_i: int) -> list[dict]:
        fired = []
        alerts = self.j.alerts(active_only=True)
        if not alerts:
            return fired
        team = [a for a in self.agents if is_team(a)]
        by_name = {a.name: a for a in self.agents}
        start = sum(a.day_start_equity for a in team) or 1.0
        company_pct = (sum(a.equity(price) for a in team) - start) / start * 100
        for al in alerts:
            k, v, t = al["kind"], float(al["value"]), al["target"]
            hit, text = False, ""
            if k == "price_above" and price >= v:
                hit, text = True, f"цена {price:.0f} $ выше {v:.0f} $"
            elif k == "price_below" and price <= v:
                hit, text = True, f"цена {price:.0f} $ ниже {v:.0f} $"
            elif k == "company_day_below" and company_pct <= -abs(v):
                hit, text = True, f"компания за день {company_pct:+.2f}%"
            elif k == "company_day_above" and company_pct >= abs(v):
                hit, text = True, f"компания за день {company_pct:+.2f}%"
            elif k == "desk_day_below":
                members = [a for a in team if a.desk == t]
                ds = sum(a.day_start_equity for a in members) or 1.0
                pct = (sum(a.equity(price) for a in members) - ds) / ds * 100
                if members and pct <= -abs(v):
                    hit, text = True, f"деск «{DESKS.get(t, {}).get('label', t)}» за день {pct:+.2f}%"
            elif k in {"agent_day_below", "agent_entry", "agent_exit"}:
                a = by_name.get(t)
                if a is not None:
                    if k == "agent_day_below" and a.day_start_equity > 0 and a.pnl_day(price) / a.day_start_equity * 100 <= -abs(v):
                        hit, text = True, f"{a.name} за день {a.pnl_day(price) / a.day_start_equity * 100:+.2f}%"
                    elif k == "agent_entry" and a.account.trades and a.account.trades[-1].ts > al["fired_ts"] and a.account.trades[-1].ts >= al["ts"] and a.account.trades[-1].pnl is None:
                        hit, text = True, f"{a.name} открыл позицию по {a.account.trades[-1].price:.0f} $"
                    elif k == "agent_exit" and a.account.trades and a.account.trades[-1].ts > al["fired_ts"] and a.account.trades[-1].ts >= al["ts"] and a.account.trades[-1].pnl is not None:
                        hit, text = True, f"{a.name} закрыл позицию, итог {a.account.trades[-1].pnl:+.2f} $"
            if hit:
                if al["repeat"] and now_i - al["fired_ts"] < 3600:
                    continue
                self.j.alert_fired(al["id"], now_i, keep=bool(al["repeat"]))
                self.j.event("alert", f"Сигнал: {text}", t or None, {"alert_id": al["id"], "kind": k}, ts=now_i)
                fired.append({"id": al["id"], "text": text})
        return fired

    # --- утренний брифинг ---
    def morning_briefing(self, now_i: int) -> dict | None:
        price = self.last_price
        team = [a for a in self.agents if is_team(a)]
        day_events = [e for e in self.j.recent_events(120) if e["ts"] >= now_i - 86400 and e["kind"] in {"stop", "fire", "hire", "demote", "head", "halt", "liquidation", "weekly", "capital", "rule", "owner"}]
        pos = self.open_positions(now_i)
        st = self.j.trade_stats({a.name for a in team}, now_i - 86400)
        pol = self.director.policy()
        facts = (f"Цена BTC {price:.0f} $. Капитал компании {sum(a.equity(price) for a in team):.0f} $. За сутки закрыто {st['closed']} сделок, "
                 f"в плюсе {st['wins']}, итог {st['pnl']:+.2f} $, комиссии {st['fees']:.2f} $. Открытых позиций {len([p for p in pos if p['kind'] == 'team'])}, "
                 f"на бумаге {sum(p['upnl'] for p in pos if p['kind'] == 'team'):+.2f} $. Режим рынка: {pol.get('regime')} ({pol.get('source', '')}), "
                 f"потолки {pol.get('caps')}. Внешние данные: {macro_describe(self.macro())}.\n"
                 + "Аналитики: " + "; ".join(f"{a['name']}: {a['last']['regime']} ({a['last']['confidence']:.0%})" for a in self.analytics.stats() if a.get("last")) + "\n"
                 + "События за сутки:\n" + "\n".join(f"- {e['message'][:160]}" for e in day_events[:25]))
        text = None
        if self.client and self.client.enabled:
            try:
                data = self.client.structured(
                    "Ты директор торговой компании Botz. Напиши владельцу утренний брифинг из трёх коротких абзацев на русском: "
                    "1) что произошло за ночь и сутки (с цифрами), 2) что делает компания сейчас и почему, 3) на что смотреть сегодня и какие риски. "
                    "Без воды, без общих слов, только факты из сводки и выводы из них.",
                    facts, {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
                    max_tokens=1500, strong=True)
                text = str(data["text"]).strip()
            except Exception as e:  # noqa: BLE001
                log.warning("брифинг без нейросети: %s", e)
        if not text:
            text = (f"За сутки закрыто {st['closed']} сделок, в плюсе {st['wins']}, итог {st['pnl']:+.2f} $ при комиссиях {st['fees']:.2f} $. "
                    f"Сейчас открыто {len([p for p in pos if p['kind'] == 'team'])} позиций, на бумаге {sum(p['upnl'] for p in pos if p['kind'] == 'team'):+.2f} $. "
                    f"Режим рынка: {pol.get('regime')}, потолки: " + ", ".join(f"{DESKS[k]['label'].lower()} {v:.0%}" for k, v in (pol.get('caps') or {}).items()) + ". "
                    + (f"Внешние данные: {macro_describe(self.macro())}. " if self.macro() else "")
                    + (f"Главные события: " + "; ".join(e["message"][:80] for e in day_events[:3]) + "." if day_events else ""))
        kid = self.j.add_knowledge(now_i, "briefing", day_key_of(now_i), text, "директор")
        self.j.event("briefing", text[:300] + ("…" if len(text) > 300 else ""), None, {"id": kid}, ts=now_i)
        return {"id": kid, "text": text}

    # --- push-уведомления ---
    PUSH_KINDS = {"stop": "Стоп", "approval": "Решение", "head": "Директор", "fire": "Увольнение", "halt": "Стоп компании",
                  "liquidation": "Ликвидация", "live_ready": "Кандидат на реальный счёт", "weekly": "Недельная ротация",
                  "alert": "Сигнал", "briefing": "Утренний брифинг", "capital": "Капитал", "rule": "Правило", "owner": "Владелец", "live": "Реальный счёт"}

    def _push_new_events(self, now_i: int) -> None:
        if not self.push:
            return
        subs = self.j.push_subs()
        if not subs:
            return
        last_id = int(self.j.kv_get("push_last_event_id", 0) or 0)
        events = [e for e in self.j.recent_events(40) if e["id"] > last_id]
        if not events:
            return
        new_max = max(e["id"] for e in events)
        if last_id == 0:
            self.j.kv_set("push_last_event_id", new_max)
            return
        self.j.kv_set("push_last_event_id", new_max)
        picked = [e for e in reversed(events) if e["kind"] in self.PUSH_KINDS and not (e["kind"] == "stop" and "зафиксировал" in e["message"])]
        if not picked:
            return
        pending = [p["title"] for p in self.j.pending_approvals()]
        payloads = [{"title": f"Botz · {self.PUSH_KINDS[e['kind']]}", "body": e["message"][:180], "tag": f"botz-{e['id']}", "url": "/"} for e in picked[-4:]]
        if pending and any(e["kind"] in {"live_ready", "weekly", "rule", "strategist", "head"} for e in picked):
            payloads.append({"title": "Botz · нужно ваше решение", "body": pending[0][:180], "tag": "botz-approval", "url": "/#company"})
        threading.Thread(target=self._send_push, args=(subs, payloads), daemon=True).start()

    def _send_push(self, subs: list[dict], payloads: list[dict]) -> None:
        for sub in subs:
            for p in payloads:
                try:
                    code = self.push.send(sub["sub"], p)
                    if code in (404, 410):
                        self.j.push_remove(sub["endpoint"])
                        break
                except Exception as e:  # noqa: BLE001
                    log.warning("push не отправлен: %s", e)
                    break

    def push_test(self) -> int:
        subs = self.j.push_subs()
        if not self.push or not subs:
            return 0
        n = 0
        for sub in subs:
            try:
                if self.push.send(sub["sub"], {"title": "Botz", "body": "Уведомления включены. Так будут приходить стопы, решения и брифинг.", "tag": "botz-test", "url": "/"}) < 400:
                    n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("push-тест: %s", e)
        return n

    # --- зеркало реального счёта ---
    def _mirror_live(self, a: Agent, trade, price: float, ts: int) -> None:
        """Сделка трейдера со званием «Реальный счёт» повторяется на бирже пропорционально live_capital_usd."""
        if not self.broker or not self.broker.enabled or trade is None or a.rank != 3:
            return
        if a.strategy.side != "long":
            return                                   # шорты на споте не зеркалятся
        eq = a.equity(price) or 1.0
        frac = trade.qty * trade.price / eq
        try:
            if trade.side == "BUY":
                usd = round(frac * self.s.live_capital_usd, 2)
                if usd < 10:
                    return
                r = self.broker.market_buy(self.s.symbol, usd)
                self.live_pos[a.name] = self.live_pos.get(a.name, 0.0) + r["qty"]
            else:
                held = self.live_pos.get(a.name, 0.0)
                qty = min(held, held * (trade.qty / max(trade.qty + a.account.btc, 1e-12)) if a.account.btc > 1e-12 else held)
                qty = round(qty, 5)
                if qty * price < 10:
                    return
                r = self.broker.market_sell(self.s.symbol, qty)
                self.live_pos[a.name] = max(0.0, held - r["qty"])
            self.j.kv_set("live_pos", self.live_pos)
            self.j.live_order(ts, a.name, r["side"], r["qty"], r["quote"], r["price"], r["status"], r["order_id"], self.broker.testnet)
            self.j.event("live", f"{a.name}: {'куплено' if r['side'] == 'BUY' else 'продано'} {r['qty']:.5f} BTC на {r['quote']:.2f} $ "
                                 f"({'тестовая сеть' if self.broker.testnet else 'реальный счёт'})", a.name, r, ts=ts)
        except BrokerError as e:
            self.j.live_order(ts, a.name, trade.side, 0.0, 0.0, 0.0, "error", None, self.broker.testnet, str(e))
            self.j.event("error", f"{a.name}: ордер на бирже не прошёл: {e}", a.name, ts=ts)

    def live_state(self) -> dict:
        out = {"enabled": bool(self.broker and self.broker.enabled), "testnet": bool(self.broker.testnet) if self.broker else True,
               "capital_usd": self.s.live_capital_usd, "positions": self.live_pos, "orders": self.j.live_orders(15),
               "agents": [a.name for a in self.agents if a.rank == 3 and is_team(a)]}
        return out

    # --- лаборатория ---
    def lab_backtest(self, family: str, params: dict | None, days: int = 30) -> dict:
        from .research import BacktestResult, backtest, result_to_dict
        candles = self.last_candles or self.market.candles(self.s.symbol, self.s.timeframe, self.s.history_candles)
        hist = candles[-max(48, min(len(candles), days * 24)):]
        strat = build_strategy(family, params or None, self.client)
        curve: list[dict] = []
        r = backtest(strat, hist, fee_rate=self.s.fee_rate, curve=curve)
        step = max(1, len(curve) // 400)
        return {"result": result_to_dict(r), "curve": curve[::step], "params": strat.params, "days": days, "bars": len(hist)}

    # --- аналитика терминала: стресс-тест, похожесть, машина времени ---
    def stress_test(self, moves: tuple[float, ...] = (-0.05, -0.02, 0.02, 0.05)) -> dict:
        """Что будет с открытыми позициями при резком движении цены: убыток до стопа по каждому деску."""
        price = self.last_price
        team = [a for a in self.agents if is_team(a)]
        equity = sum(a.equity(price) for a in team) or 1.0
        out = {"equity": round(equity, 2), "moves": {}, "unprotected": 0, "exposure_pct": 0.0}
        gross = 0.0
        for a in team:
            if abs(a.account.btc) * price >= 1.0:
                gross += abs(a.account.btc) * price
                if not a.stop_price:
                    out["unprotected"] += 1
        out["exposure_pct"] = round(gross / equity * 100, 1)
        for m in moves:
            total, desks = 0.0, {d: 0.0 for d in DESKS}
            for a in team:
                qty = a.account.btc
                if abs(qty) * price < 1.0 or not price:
                    continue
                new_price = price * (1 + m)
                pnl = (new_price - price) * qty
                if a.stop_price:
                    hit = (qty > 0 and new_price <= a.stop_price) or (qty < 0 and new_price >= a.stop_price)
                    if hit:
                        pnl = (a.stop_price * (1 - self.s.stop_slippage if qty > 0 else 1 + self.s.stop_slippage) - price) * qty
                total += pnl
                desks[a.desk] += pnl
            out["moves"][f"{m*100:+.0f}"] = {"pnl": round(total, 2), "pct": round(total / equity * 100, 2), "desks": {d: round(v, 2) for d, v in desks.items()}}
        worst = min((v["pnl"] for v in out["moves"].values()), default=0.0)
        out["at_risk_pct"] = round(-worst / equity * 100, 2) if worst < 0 else 0.0
        return out

    def correlation(self, days: int = 7) -> dict:
        """Матрица похожести трейдеров по часовым приращениям капитала."""
        now_i = int(time.time())
        team = [a for a in self.agents if is_team(a)]
        series = self.j.hourly_pnl_series({a.name for a in team}, now_i - days * 86400)
        names = [a.name for a in team if len(series.get(a.name, {})) >= 24]
        if len(names) < 2:
            return {"names": names, "matrix": [], "pairs": []}
        hours = sorted(set().union(*(series[n].keys() for n in names)))
        vec = {n: [series[n].get(h, 0.0) for h in hours] for n in names}

        def corr(x, y):
            n = len(x); mx, my = sum(x) / n, sum(y) / n
            sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
            sxx = sum((a - mx) ** 2 for a in x); syy = sum((b - my) ** 2 for b in y)
            return sxy / ((sxx * syy) ** 0.5) if sxx > 0 and syy > 0 else 0.0
        matrix = [[round(corr(vec[a], vec[b]), 2) for b in names] for a in names]
        pairs = sorted(({"a": names[i], "b": names[j], "r": matrix[i][j]} for i in range(len(names)) for j in range(i + 1, len(names))),
                       key=lambda p: -p["r"])
        desk_of_name = {a.name: a.desk for a in team}
        return {"names": names, "desks": [desk_of_name[n] for n in names], "matrix": matrix, "pairs": pairs[:8], "hours": len(hours)}

    def snapshot_at(self, ts: int) -> dict:
        """Машина времени: позиции, решения, режим директора и взгляды аналитиков на момент ts."""
        team = [a for a in self.agents if a.status not in {"dropped", "experiment"}]
        trades = self.j.trades_until({a.name for a in team}, ts)
        cndl = next((c for c in reversed(self.last_candles) if c.ts <= ts), None)
        price = cndl.close if cndl else self.last_price
        positions = []
        for a in team:
            pos, avg = 0.0, 0.0
            for t in trades.get(a.name, []):
                q = t["qty"]; fee = t["fee"] or 0.0
                if t["side"] == "BUY":
                    if pos < -1e-12:
                        cq = min(q, -pos); pos += cq; rest = q - cq
                        if rest > 1e-12:
                            avg = t["price"]; pos += rest
                    else:
                        total = avg * pos + t["price"] * q + fee; pos += q; avg = total / pos if pos else 0.0
                else:
                    if pos > 1e-12:
                        cq = min(q, pos); pos -= cq; rest = q - cq
                        if rest > 1e-12:
                            avg = t["price"]; pos -= rest
                    else:
                        total = avg * (-pos) + t["price"] * q - fee; pos -= q; avg = total / (-pos) if pos else 0.0
                if abs(pos) < 1e-12:
                    pos, avg = 0.0, 0.0
            if abs(pos) * price >= 1.0:
                positions.append({"agent": a.name, "desk": a.desk, "side": "long" if pos > 0 else "short", "qty": round(abs(pos), 6),
                                  "entry": round(avg, 2), "upnl": round((price - avg) * pos, 2)})
        decs = self.j.decisions_around(ts, 3600, 80)
        seen, latest = set(), []
        for d in decs:
            if d["agent"] in seen:
                continue
            seen.add(d["agent"]); latest.append(d)
        head = self.j.events_before("head", ts, 1)
        return {"ts": ts, "price": round(price, 2), "candle": cndl.__dict__ if cndl else None, "positions": positions, "decisions": latest[:30],
                "head": head[0] if head else None, "views": self.j.views_before(ts),
                "events": self.j._rows("SELECT * FROM events WHERE ts<=? AND ts>? AND kind IN ('stop','fire','hire','demote','halt','head','liquidation','owner','capital') ORDER BY ts DESC LIMIT 12", (ts, ts - 3600))}

    def knowledge_state(self) -> dict:
        kc = self.j.knowledge_counts()
        return {
            "counts": kc,
            "memory": self.j.memory_table("desk"),
            "families": [m for m in self.j.memory_table("family") if m["days"] >= 3],
            "rules": self.j.active_rules(),
            "lessons": self.j.knowledge("lesson", None, limit=10),
            "insights": self.j.knowledge("insight", "active", limit=8),
            "proposals": self.j.knowledge("proposal", None, limit=12),
            "staff": self.analytics.staff(),
            "memory_min_days": self.s.memory_min_days,
        }

    def open_positions(self, now_i: int) -> list[dict]:
        """Монитор открытых позиций: кто в рынке, с какого уровня, сколько заработал на бумаге, где стоп."""
        price = self.last_price
        out = []
        for a in self.agents:
            if a.status in {"fired", "dropped"} or not price or abs(a.account.btc) * price < 1.0:
                continue
            qty = a.account.btc
            entry = a.account._avg_entry or price
            upnl = (price - entry) * qty
            notional = abs(qty) * price
            opened_ts = 0
            for t in reversed(a.account.trades):
                if abs(t.pos_after) < 1e-12:
                    break
                opened_ts = t.ts
            stop = a.stop_price
            out.append({
                "agent": a.name, "desk": a.desk, "kind": "intern" if is_intern(a) else ("experiment" if is_experiment(a) else "team"), "rank": a.rank,
                "side": "long" if qty > 0 else "short", "qty": round(abs(qty), 6), "notional": round(notional, 2),
                "entry": round(entry, 2), "price": round(price, 2), "upnl": round(upnl, 2),
                "upnl_pct": round(upnl / (entry * abs(qty)) * 100, 2) if entry and qty else 0.0,
                "exposure": round(a.account.exposure(price), 3), "opened_ts": opened_ts,
                "hours": round((now_i - opened_ts) / 3600, 1) if opened_ts else None,
                "stop": round(stop, 2) if stop else None,
                "stop_pct": round((stop - price) / price * 100, 2) if stop else None,
                "stop_kind": self.stop_kind(a), "partial_taken": a.partial_taken,
                "best_price": round(a.best_price, 2) if a.best_price else None,
                "reason": a.last_signal.reason if a.last_signal else "",
            })
        out.sort(key=lambda x: x["upnl"], reverse=True)
        return out

    def _trades_24h(self, now_i: int) -> list[dict]:
        kinds = {a.name: ("intern" if a.status in {"intern", "dropped"} else ("experiment" if is_experiment(a) else "team")) for a in self.agents}
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
            if r["kind"] == "proposal" and r["details"].get("knowledge_id"):
                self.j.set_knowledge_status(int(r["details"]["knowledge_id"]), "rejected")
            if r["kind"] == "director":                 # владелец оставил директора: даём ещё срок
                pol = self.director.policy()
                if pol.get("director"):
                    pol["director"]["low_weeks"] = 0
                    self._save_policy_via_director(pol)
            self.j.event("approval", f"Отклонено: {r['title']}")
            return r
        price = self.last_price
        ts = int(time.time())
        if r["kind"] == "fire":
            name = r["details"].get("agent")
            for a in self.agents:
                if a.name == name and a.status != "fired":
                    self.director.fire(a, price or a.last_price, ts, "решение владельца")
        elif r["kind"] == "hire":
            desk = r["details"].get("desk")
            best = self.director.best_intern(self.agents, price, desk=desk) or self.director.best_intern(self.agents, price)
            if best is not None:
                self.director.promote(best, price, ts, "одобрено владельцем")
            else:
                cand = self.director._take_candidate(self.agents, desk)
                if cand:
                    a = self.director._create(cand["strategy"], cand["params"], self.agents, ts, "active")
                    self.j.event("hire", f"Нанят {a.name}", a.name, ts=ts)
                    self.agents.append(a)
        elif r["kind"] == "director":
            self.director.replace_director(ts, r["details"].get("next_style"))
            pol = self.director.policy()
            self.risk.desk_caps = {k: float(v) for k, v in (pol.get("caps") or {}).items()}
        elif r["kind"] == "rule":
            self.director.approve_rule(r["details"], ts)
            self.risk.set_rules(self.j.active_rules())
        elif r["kind"] == "proposal":
            if r["details"].get("knowledge_id"):
                self.j.set_knowledge_status(int(r["details"]["knowledge_id"]), "accepted", ts)
        elif r["kind"] == "live":
            for a in self.agents:
                if a.name == r["details"].get("agent") and is_team(a):
                    self.director.approve_live(a, ts)
        elif r["kind"] == "promote":
            worst = next((a for a in self.agents if a.name == r["details"].get("agent") and is_team(a)), None)
            intern = next((a for a in self.agents if a.name == r["details"].get("intern") and is_intern(a)), None)
            if worst is not None:
                self.director.fire(worst, price or worst.last_price, ts, "заменён стажёром по решению владельца")
            if intern is not None:
                self.director.promote(intern, price or intern.last_price, ts, "одобрено владельцем")
        self.j.event("approval", f"Одобрено: {r['title']}")
        self.save()
        return r

    def manual_close(self, name: str) -> bool:
        """Владелец закрыл позицию руками: закрываем по рынку, пауза как после выхода, запись в события."""
        price = self.last_price
        ts = int(time.time())
        for a in self.agents:
            if a.name == name and a.status not in {"fired", "dropped"} and abs(a.account.btc) > 1e-12:
                pos = a.account.btc
                t = a.account.flatten(price or a.last_price, ts, "закрыто владельцем")
                if t:
                    self.j.trade(t)
                    self.j.decision(ts, a.name, a.strategy.family, t.side, 0.0, 1.0, "решение владельца: закрыть позицию", price, a.equity(price), True, None,
                                    trade=t, exposure_before=a.account.exposure(price))
                    self.j.equity(ts, a.name, a.equity(price), price)
                    a.exit_price, a.exit_side, a.exit_ts = t.price, ("long" if pos > 0 else "short"), ts
                    self.j.event("owner", f"{a.name}: владелец закрыл позицию" + (f", итог {t.pnl:+.2f} $" if t.pnl is not None else ""), a.name, ts=ts)
                a.stop_price, a.best_price, a.partial_taken, a.last_target = 0.0, 0.0, False, 0.0
                a.next_check_ts = max(a.next_check_ts, ts + self.s.exit_cooldown_min * 60)
                self.save()
                return True
        return False

    def manual_pause(self, name: str, resume: bool = False) -> bool:
        """Пауза до конца дня (позиция закрывается) или снятие паузы по решению владельца."""
        price = self.last_price
        ts = int(time.time())
        for a in self.agents:
            if a.name != name or a.status not in {"active", "paused"}:
                continue
            if resume:
                a.status = "active"
                self.j.event("owner", f"{a.name}: владелец снял паузу", a.name, ts=ts)
            else:
                t = a.account.flatten(price or a.last_price, ts, "пауза по решению владельца")
                if t:
                    self.j.trade(t)
                    self.j.equity(ts, a.name, a.equity(price), price)
                a.status = "paused"
                a.stop_price, a.best_price, a.partial_taken = 0.0, 0.0, False
                self.j.event("owner", f"{a.name}: владелец поставил на паузу до конца дня", a.name, ts=ts)
            self.save()
            return True
        return False

    def manual_fire(self, name: str) -> bool:
        price = self.last_price
        for a in self.agents:
            if a.name == name and is_intern(a):
                self.director.drop_intern(a, price or a.last_price, int(time.time()), "решение владельца")
                self.save()
                return True
            if a.name == name and is_team(a):
                self.director.fire(a, price or a.last_price, int(time.time()), "решение владельца")
                self.save()
                return True
        return False
