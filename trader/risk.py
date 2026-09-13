"""Риск-менеджер. Жёсткие правила без нейросети. Его решение окончательное.

Правила:
1. Без плеча: позиция по модулю не больше капитала агента; быки только лонг, медведи только шорт.
2. Потолок доли по деску выставляет директор (распределение капитала по режиму рынка).
3. Дневной лимит убытка агента: при превышении позиция закрывается, агент на паузе до конца дня (UTC).
4. Максимальная просадка от пика: при превышении агент увольняется.
5. Дневной лимит убытка компании: при превышении все позиции закрываются, торговля стоит до конца дня.
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


class RiskManager:
    def __init__(self, settings: Settings):
        self.s = settings
        self.dept_halted_day: str = ""
        self.desk_caps: dict[str, float] = {"bulls": 1.0, "bears": 1.0, "both": 1.0}   # потолки долей от директора

    @property
    def policy_cap(self) -> float:
        return self.desk_caps.get("bulls", 1.0)

    @property
    def policy_cap_short(self) -> float:
        return self.desk_caps.get("bears", 1.0)

    def stop_distance(self, atr_pct: float) -> float:
        """Расстояние стопа от входа в долях цены. Не меньше 0.5%, не больше 10%."""
        return min(0.10, max(0.005, self.s.stop_atr_mult * atr_pct))

    def size(self, desired: float, atr_pct: float, desk: str = "bulls") -> float:
        """Размер позиции со знаком: желание стратегии × доля, при которой потеря до стопа = risk_per_trade,
        и не больше потолка agent_max_exposure и потолка деска (по модулю; шорт отрицательный)."""
        dist = self.stop_distance(atr_pct)
        by_risk = self.s.risk_per_trade / dist if dist > 0 else 1.0
        cap = min(self.s.agent_max_exposure, self.desk_caps.get(desk, 1.0))
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

    def check_agent(self, agent: Agent, signal: Signal, price: float, atr_pct: float = 0.01) -> RiskVerdict:
        eq = agent.equity(price)
        desired = signal.target_exposure if agent.account.allow_short else max(0.0, signal.target_exposure)
        target = self.size(desired, atr_pct, agent.desk)
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
        if abs(target - signal.target_exposure) > 1e-9:
            pc = self.desk_caps.get(agent.desk, 1.0)
            if pc < min(1.0, self.s.agent_max_exposure) and abs(abs(target) - pc * min(1.0, abs(signal.target_exposure))) < 1e-9:
                return RiskVerdict(True, target, f"потолок директора для деска {pc:.0%} по режиму рынка")
            return RiskVerdict(True, target, f"размер по риску: {target:.0%} (стоп {self.stop_distance(atr_pct)*100:.1f}%)")
        return RiskVerdict(True, target, "ok")
