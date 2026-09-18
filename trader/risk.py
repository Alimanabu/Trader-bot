"""Риск-менеджер. Жёсткие правила без нейросети. Его решение окончательное.

Правила:
1. Без плеча: позиция по модулю не больше капитала агента; быки только лонг, медведи только шорт.
2. Потолок доли по деску выставляет директор (распределение капитала по режиму рынка).
3. Дневной лимит убытка агента: при превышении позиция закрывается, агент на паузе до конца дня (UTC).
4. Максимальная просадка от пика: при превышении агент увольняется.
5. Дневной лимит убытка компании: при превышении все позиции закрываются, торговля стоит до конца дня.
6. Правила из базы знаний (предложены ревизором, одобрены владельцем): потолок деска в режиме, запрет входа
   в определённые часы, запрет семейства в режиме, лимит сделок в день, множитель стопа, пауза после стопа.
"""
from __future__ import annotations

from dataclasses import dataclass

from .agents.base import Agent
from .config import Settings
from .models import Signal


@dataclass
class RiskVerdict:
    allowed: bool
    target_exposure: float
    reason: str
    fire: bool = False
    pause: bool = False


RULE_TYPES = {
    "desk_cap": "потолок доли деска в режиме рынка",
    "no_entry_hours": "запрет новых входов в часы (UTC)",
    "family_ban": "семейство вне рынка в режиме",
    "max_trades_day": "не больше N сделок в день на трейдера",
    "stop_mult": "стоп на расстоянии N·ATR",
    "cooldown_min": "пауза после стопа, минут",
}
# кроме правил из базы знаний действуют настройки: max_entries_day (входов в день), reentry_move_pct (повторный вход
# в ту же сторону только после ухода цены от точки выхода), exit_cooldown_min (пауза после выхода в плюс/безубыток)


class RiskManager:
    def __init__(self, settings: Settings):
        self.s = settings
        self.dept_halted_day: str = ""
        self.desk_caps: dict[str, float] = {"bulls": 1.0, "bears": 1.0, "both": 1.0}   # потолки долей от директора
        self.rules: list[dict] = []          # активные правила из базы знаний
        self.rule_hits: list[int] = []       # id правил, сработавших на последней проверке

    def set_rules(self, rules: list[dict]) -> None:
        self.rules = [r for r in rules if isinstance(r.get("data"), dict) and r["data"].get("type") in RULE_TYPES]

    def _rule_values(self, rtype: str) -> list[tuple[int, dict]]:
        return [(r["id"], r["data"]) for r in self.rules if r["data"].get("type") == rtype]

    @property
    def stop_atr_mult(self) -> float:
        vals = self._rule_values("stop_mult")
        return float(vals[-1][1].get("value", self.s.stop_atr_mult)) if vals else self.s.stop_atr_mult

    @property
    def stop_cooldown_min(self) -> int:
        vals = self._rule_values("cooldown_min")
        return int(vals[-1][1].get("minutes", self.s.stop_cooldown_min)) if vals else self.s.stop_cooldown_min

    def desk_cap(self, desk: str, regime: str | None = None) -> float:
        cap = self.desk_caps.get(desk, 1.0)
        for _rid, d in self._rule_values("desk_cap"):
            if d.get("desk") == desk and (not d.get("regime") or d.get("regime") == regime):
                cap = min(cap, float(d.get("cap", 1.0)))
        return cap

    @property
    def policy_cap(self) -> float:
        return self.desk_caps.get("bulls", 1.0)

    @property
    def policy_cap_short(self) -> float:
        return self.desk_caps.get("bears", 1.0)

    def stop_distance(self, atr_pct: float) -> float:
        """Расстояние стопа от входа в долях цены. Не меньше 0.5%, не больше 10%."""
        return min(0.10, max(0.005, self.stop_atr_mult * atr_pct))

    def size(self, desired: float, atr_pct: float, desk: str = "bulls", regime: str | None = None) -> float:
        """Размер позиции со знаком: желание стратегии × доля, при которой потеря до стопа = risk_per_trade,
        и не больше потолка agent_max_exposure и потолка деска (по модулю; шорт отрицательный)."""
        dist = self.stop_distance(atr_pct)
        by_risk = self.s.risk_per_trade / dist if dist > 0 else 1.0
        cap = min(self.s.agent_max_exposure, self.desk_cap(desk, regime))
        mag = min(cap, abs(desired) * min(1.0, by_risk))
        return max(0.0, mag) * (1 if desired >= 0 else -1)

    def check_department(self, agents: list[Agent], price: float, day_key: str) -> tuple[bool, str]:
        active = [a for a in agents if a.status in {"active", "paused"}]
        if not active:
            return True, ""
        if self.dept_halted_day == day_key:
            return False, "компания остановлена до конца дня"
        day_start = sum(a.day_start_equity for a in active)
        now = sum(a.equity(price) for a in active)
        if day_start > 0 and (day_start - now) / day_start >= self.s.dept_daily_loss_limit:
            self.dept_halted_day = day_key
            return False, f"дневной убыток компании {((day_start-now)/day_start)*100:.2f}% ≥ лимита {self.s.dept_daily_loss_limit*100:.1f}%"
        return True, ""

    def apply_rules(self, agent: Agent, target: float, ctx: dict) -> tuple[float, str | None]:
        """Правила из базы знаний. Возвращает (новая цель, пояснение или None). Заполняет rule_hits."""
        self.rule_hits = []
        regime = ctx.get("regime")
        hour = ctx.get("hour")
        family = ctx.get("family", "")
        current = float(ctx.get("exposure", 0.0))
        note = None
        for rid, d in self._rule_values("family_ban"):
            if d.get("family") == family and (not d.get("regime") or d.get("regime") == regime) and abs(target) > 1e-9:
                self.rule_hits.append(rid)
                return 0.0, f"правило: семейство {family} вне рынка в режиме «{regime}»"
        increasing = abs(target) > abs(current) + 1e-9
        opening = increasing and abs(current) < 1e-9
        # защита от повторного входа: после выхода из позиции та же сторона открывается только когда цена ушла
        if opening and self.s.reentry_move_pct > 0 and ctx.get("exit_price") and ctx.get("exit_side"):
            side = "long" if target > 0 else "short"
            age_h = (float(ctx.get("ts", 0)) - float(ctx.get("exit_ts", 0))) / 3600
            moved = abs(float(ctx.get("price", 0)) / float(ctx["exit_price"]) - 1) * 100
            if side == ctx["exit_side"] and age_h < self.s.reentry_guard_h and moved < self.s.reentry_move_pct:
                return current, f"защита от повторного входа: после выхода по {float(ctx['exit_price']):.0f} цена ушла лишь на {moved:.2f}%"
        if opening and self.s.max_entries_day and int(ctx.get("entries_today", 0)) >= self.s.max_entries_day:
            return current, f"лимит входов: уже {ctx.get('entries_today')} за день"
        if increasing and hour is not None:
            for rid, d in self._rule_values("no_entry_hours"):
                if int(hour) in {int(h) for h in d.get("hours", [])} and (not d.get("desk") or d.get("desk") == agent.desk):
                    self.rule_hits.append(rid)
                    return current, f"правило: нет новых входов в {hour:02d}:00 UTC"
        if increasing:
            for rid, d in self._rule_values("max_trades_day"):
                if int(ctx.get("trades_today", 0)) >= int(d.get("n", 99)):
                    self.rule_hits.append(rid)
                    return current, f"правило: не больше {d.get('n')} сделок в день"
        return target, note

    def check_agent(self, agent: Agent, signal: Signal, price: float, atr_pct: float = 0.01, ctx: dict | None = None) -> RiskVerdict:
        ctx = ctx or {}
        eq = agent.equity(price)
        desired = signal.target_exposure if agent.account.allow_short else max(0.0, signal.target_exposure)
        target = self.size(desired, atr_pct, agent.desk, ctx.get("regime"))
        target, rule_note = self.apply_rules(agent, target, {**ctx, "exposure": agent.account.exposure(price), "family": agent.strategy.family})
        if agent.status == "fired":
            return RiskVerdict(False, 0.0, "агент уволен", fire=False)
        dd = agent.drawdown(price)
        if dd >= self.s.agent_max_drawdown:
            return RiskVerdict(False, 0.0, f"просадка {dd*100:.1f}% ≥ лимита {self.s.agent_max_drawdown*100:.0f}%: увольнение", fire=True)
        if agent.day_start_equity > 0:
            day_loss = (agent.day_start_equity - eq) / agent.day_start_equity
            if day_loss >= self.s.agent_daily_loss_limit:
                return RiskVerdict(False, 0.0, f"дневной убыток {day_loss*100:.2f}% ≥ лимита {self.s.agent_daily_loss_limit*100:.1f}%: пауза до конца дня", pause=True)
        if agent.status == "paused":
            return RiskVerdict(False, 0.0, "агент на паузе до конца дня")
        if rule_note:
            return RiskVerdict(True, target, ("правило: " if not rule_note.startswith("правило") else "") + rule_note)
        if abs(target - signal.target_exposure) > 1e-9:
            pc = self.desk_cap(agent.desk, ctx.get("regime"))
            if pc < min(1.0, self.s.agent_max_exposure) and abs(abs(target) - pc * min(1.0, abs(signal.target_exposure))) < 1e-9:
                return RiskVerdict(True, target, f"потолок директора для деска {pc:.0%} по режиму рынка")
            return RiskVerdict(True, target, f"размер по риску: {target:.0%} (стоп {self.stop_distance(atr_pct)*100:.1f}%)")
        return RiskVerdict(True, target, "ok")
