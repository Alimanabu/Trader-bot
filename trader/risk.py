"""Риск-менеджер. Жёсткие правила без нейросети. Его решение окончательное.

Правила:
1. Спот без плеча: доля BTC от 0 до 100% капитала агента.
2. Дневной лимит убытка агента: при превышении позиция закрывается, агент на паузе до конца дня (UTC).
3. Максимальная просадка от пика: при превышении агент увольняется.
4. Дневной лимит убытка отдела: при превышении все позиции закрываются, торговля стоит до конца дня.
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

    def check_department(self, agents: list[Agent], price: float, day_key: str) -> tuple[bool, str]:
        active = [a for a in agents if a.status in {"active", "paused"}]
        if not active:
            return True, ""
        if self.dept_halted_day == day_key:
            return False, "отдел остановлен до конца дня"
        day_start = sum(a.day_start_equity for a in active)
        now = sum(a.equity(price) for a in active)
        if day_start > 0 and (day_start - now) / day_start >= self.s.dept_daily_loss_limit:
            self.dept_halted_day = day_key
            return False, f"дневной убыток отдела {((day_start-now)/day_start)*100:.2f}% ≥ лимита {self.s.dept_daily_loss_limit*100:.1f}%"
        return True, ""

    def check_agent(self, agent: Agent, signal: Signal, price: float) -> RiskVerdict:
        eq = agent.equity(price)
        target = min(1.0, max(0.0, signal.target_exposure))
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
        if target != signal.target_exposure:
            return RiskVerdict(True, target, "доля ограничена диапазоном 0..100% (без плеча)")
        return RiskVerdict(True, target, "ok")
