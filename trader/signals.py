"""Отдел внешних сигналов: проверка чужих сигналов на бумаге до того, как по ним торговать.

Владелец присылает сигнал из любого канала (направление, вход, стоп, цель, источник). Компания по нему
не торгует, а записывает и следит за ценой: сработала цель, сработал стоп или сигнал протух. По каждому
источнику копится рейтинг, как у аналитиков. Источник с проверенной точностью получает статус «кандидат»,
и для него создаётся стажёр-последователь с обычным счётом стажёра. Источники, которые не прошли
проверку, остаются в базе с пометкой, чтобы было видно, кто сливает.
"""
from __future__ import annotations

import logging
import re

from .agents.base import Agent
from .agents.registry import RANK_INTERN, build_strategy, new_account
from .config import Settings
from .journal import Journal

log = logging.getLogger(__name__)

SIDE_RU = {"long": "лонг", "short": "шорт"}
STATUS_RU = {"pending": "ждёт входа", "open": "в рынке", "hit": "цель", "stopped": "стоп", "expired": "по времени", "cancelled": "отменён"}
RATING_RU = {"checking": "проверка", "candidate": "кандидат", "failed": "не прошёл"}

_LONG = re.compile(r"\b(long|buy|лонг|покуп\w*|купить)\b", re.I)
_SHORT = re.compile(r"\b(short|sell|шорт|прода\w*|продать)\b", re.I)
_LABELS = {
    "entry": re.compile(r"(entry|enter|вход\w*|зона\s+входа|buy\s*zone|e\b)\s*[:=\-]?", re.I),
    "stop": re.compile(r"(stop\s*loss|stop|sl|стоп\w*|стоп-лосс)\s*[:=\-]?", re.I),
    "target": re.compile(r"(take\s*profit|target\w*|tp|цел\w*|тейк\w*|профит)(?:\s*\d(?=\s*[:=\-]))?\s*[:=\-]?", re.I),
}
_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:[  ,]\d{3})+|\d+)(?:[.,](\d+))?\s*([kк]\b)?", re.I)


def _numbers(text: str) -> list[tuple[int, float]]:
    """(позиция, число) для всех чисел, похожих на цену биткоина."""
    out = []
    for m in _NUM.finditer(text):
        whole = re.sub(r"[  ,]", "", m.group(1))
        frac = m.group(2) or ""
        try:
            val = float(f"{whole}.{frac}" if frac else whole)
        except ValueError:
            continue
        if m.group(3):
            val *= 1000
        if val >= 1000:
            out.append((m.start(), val))
    return out


def parse_signal(text: str, price: float | None = None) -> dict:
    """Разобрать сигнал из свободного текста: «LONG BTC entry 80500 sl 79800 tp 82000 / 84000».

    Возвращает {side, entry, stop, target, targets}. Чего нет в тексте, того нет и в ответе (None).
    """
    t = " ".join((text or "").split())
    side = None
    ml, ms = _LONG.search(t), _SHORT.search(t)
    if ml and (not ms or ml.start() < ms.start()):
        side = "long"
    elif ms:
        side = "short"
    nums = _numbers(t)
    fields: dict[str, list[float]] = {"entry": [], "stop": [], "target": []}
    used: set[int] = set()
    labels = sorted(((m.start(), m.end(), key) for key, rx in _LABELS.items() for m in rx.finditer(t)), key=lambda x: x[0])
    for i, (start, end, key) in enumerate(labels):
        limit = labels[i + 1][0] if i + 1 < len(labels) else len(t) + 1
        for pos, val in nums:
            if end <= pos < limit and pos not in used:
                fields[key].append(val)
                used.add(pos)
    entry = sum(fields["entry"]) / len(fields["entry"]) if fields["entry"] else None     # диапазон входа → середина
    stop = fields["stop"][0] if fields["stop"] else None
    targets = fields["target"]
    free = [v for p, v in nums if p not in used]
    if entry is None and stop is None and not targets and free:
        # без подписей: раскладываем по смыслу, для лонга стоп ниже входа, цель выше
        vals = sorted(free)
        if len(vals) >= 3 and side:
            entry = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] if side == "long" else vals[len(vals) // 2])
            stop, targets = (vals[0], [v for v in vals if v > entry]) if side == "long" else (vals[-1], [v for v in vals if v < entry])
        elif len(vals) == 2 and side:
            lo, hi = vals
            entry, stop, targets = (lo, None, [hi]) if side == "long" else (hi, None, [lo])
        elif len(vals) == 1:
            entry = vals[0]
    elif entry is None and free:
        entry = min(free, key=lambda v: abs(v - price)) if price else free[0]     # число без подписи: это вход
    if side is None and entry and stop:
        side = "long" if stop < entry else "short"
    if side is None and entry and targets:
        side = "long" if targets[0] > entry else "short"
    if targets and side:
        targets = sorted(targets, reverse=(side == "short"))
    return {"side": side, "entry": entry, "stop": stop, "target": targets[0] if targets else None, "targets": targets}


class SignalDesk:
    def __init__(self, settings: Settings, journal: Journal):
        self.s = settings
        self.j = journal

    # --- приём ---
    def add(self, ts: int, source: str, side: str, entry: float | None, stop: float | None, target: float | None,
            note: str, price: float) -> dict:
        source = " ".join((source or "").split())[:60]
        if not source:
            raise ValueError("укажи источник сигнала (канал, имя трейдера)")
        if side not in SIDE_RU:
            raise ValueError("направление должно быть лонг или шорт")
        if not price:
            raise ValueError("нет текущей цены, попробуй через минуту")
        entry = float(entry or 0.0) or float(price)
        stop, target = float(stop or 0.0), float(target or 0.0)
        if stop and target and ((side == "long" and not stop < entry < target) or (side == "short" and not target < entry < stop)):
            raise ValueError("для лонга стоп должен быть ниже входа, а цель выше; для шорта наоборот")
        if stop and ((side == "long" and stop >= entry) or (side == "short" and stop <= entry)):
            raise ValueError("стоп стоит не с той стороны от входа")
        if target and ((side == "long" and target <= entry) or (side == "short" and target >= entry)):
            raise ValueError("цель стоит не с той стороны от входа")
        band = self.s.signal_entry_band_pct / 100
        market_like = abs(entry - price) / price <= band
        already_through = (side == "long" and price <= entry) or (side == "short" and price >= entry)
        status = "open" if market_like or already_through else "pending"
        if status == "open":
            entry = price if market_like else entry
        sid = self.j.signal_add(ts, source, side, entry, stop, target, note[:200], price, status)
        levels = f"вход {entry:.0f}" + (f", стоп {stop:.0f}" if stop else ", без стопа") + (f", цель {target:.0f}" if target else ", без цели")
        self.j.event("signal", f"Новый сигнал от «{source}»: {SIDE_RU[side]}, {levels}" + (" · ждёт цены входа" if status == "pending" else ""), None,
                     {"id": sid}, ts=ts)
        return self.j.signal_by_id(sid)

    def cancel(self, sid: int, ts: int) -> bool:
        s = self.j.signal_by_id(sid)
        if not s or s["status"] not in {"pending", "open"}:
            return False
        self.j.signal_close(sid, "cancelled", None, ts, 0.0)
        self.j.event("signal", f"Сигнал от «{s['source']}» ({SIDE_RU[s['side']]} от {s['entry']:.0f}) отменён владельцем", None, ts=ts)
        return True

    # --- проверка ценой ---
    def fees_pct(self) -> float:
        return (2 * self.s.fee_rate + 2 * self.s.slippage_rate) * 100

    def evaluate(self, price: float, now_i: int, hi: float | None = None, lo: float | None = None) -> list[dict]:
        """Проверить живые сигналы по цене (и по максимуму/минимуму с прошлой проверки, если есть)."""
        hi, lo = max(price, hi or price), min(price, lo or price)
        fees = self.fees_pct()
        done = []
        for s in self.j.signals_live():
            side, entry = s["side"], float(s["entry"])
            sign = 1 if side == "long" else -1
            if s["status"] == "pending":
                touched = lo <= entry if side == "long" else hi >= entry
                if touched:
                    self.j.signal_open(s["id"], now_i)
                    self.j.event("signal", f"Сигнал от «{s['source']}»: цена дошла до входа {entry:.0f}, {SIDE_RU[side]} открыт", None, ts=now_i)
                elif now_i - int(s["ts"]) >= self.s.signal_entry_h * 3600:
                    self.j.signal_close(s["id"], "cancelled", None, now_i, price)
                    self.j.event("signal", f"Сигнал от «{s['source']}» ({SIDE_RU[side]} от {entry:.0f}) отменён: цена не дошла до входа за {self.s.signal_entry_h} ч", None, ts=now_i)
                continue
            stop, target = float(s["stop"] or 0), float(s["target"] or 0)
            status, at = None, price
            if stop and ((side == "long" and lo <= stop) or (side == "short" and hi >= stop)):
                status, at = "stopped", stop
            elif target and ((side == "long" and hi >= target) or (side == "short" and lo <= target)):
                status, at = "hit", target
            elif now_i - int(s["opened_ts"] or s["ts"]) >= self.s.signal_max_h * 3600:
                status, at = "expired", price
            if not status:
                continue
            pct = sign * (at / entry - 1) * 100 - fees
            self.j.signal_close(s["id"], status, round(pct, 3), now_i, at)
            what = {"stopped": "сработал стоп", "hit": "цель достигнута", "expired": f"закрыт по времени ({self.s.signal_max_h} ч)"}[status]
            self.j.event("signal", f"Сигнал от «{s['source']}» ({SIDE_RU[side]} от {entry:.0f}): {what}, итог {pct:+.2f}% с учётом комиссий", None,
                         {"id": s["id"], "pct": pct}, ts=now_i)
            done.append({**s, "status": status, "result_pct": pct})
        return done

    # --- рейтинг источников ---
    def rate(self, src: dict) -> str:
        if src["scored"] < self.s.signal_min_count:
            return "checking"
        if (src["accuracy"] or 0) >= self.s.signal_min_accuracy and src["total_pct"] > 0:
            return "candidate"
        return "failed"

    def sources(self, agents: list[Agent] | None = None) -> list[dict]:
        followers = {a.strategy.params.get("source"): a for a in (agents or []) if is_follower(a)}
        out = []
        for src in self.j.signal_sources():
            f = followers.get(src["source"])
            out.append({**src, "rating": self.rate(src), "rating_ru": RATING_RU[self.rate(src)],
                        "need": max(0, self.s.signal_min_count - src["scored"]),
                        "follower": {"name": f.name, "status": f.status, "rank": f.rank} if f else None})
        return out

    def sync_followers(self, agents: list[Agent], price: float, ts: int, director) -> list[Agent]:
        """Кандидат без последователя получает стажёра; источник, переставший проходить проверку, теряет его."""
        added: list[Agent] = []
        by_source = {a.strategy.params.get("source"): a for a in agents if is_follower(a) and a.status not in {"fired", "dropped"}}
        for src in self.j.signal_sources():
            rating = self.rate(src)
            name_src = src["source"]
            a = by_source.get(name_src)
            if rating == "candidate" and a is None:
                if ts - int(self.j.kv_get(f"signal_drop:{name_src}", 0) or 0) < 14 * 86400:
                    continue                                   # недавно отчисляли: ждём две недели
                name = f"Сигналы: {name_src}"
                taken = {x.name for x in agents + added} | {r["name"] for r in self.j.all_agents()}
                n = 2
                while name in taken:
                    name = f"Сигналы: {name_src} #{n}"
                    n += 1
                strat = build_strategy("signal_follower", {"source": name_src})
                agent = Agent(name=name, strategy=strat, account=new_account(self.s, name, allow_short=True), hired_at=ts,
                              status="intern", rank=RANK_INTERN)
                agent.last_price = price
                self.j.save_agent(agent)
                self.j.add_knowledge(ts, "insight", "сигналы", f"Источник «{name_src}» прошёл проверку: {src['wins']} из {src['scored']} сигналов "
                                     f"в плюсе ({src['accuracy']:.0f}%), суммарно {src['total_pct']:+.1f}%. Для него создан стажёр-последователь.",
                                     "отдел внешних сигналов", {"source": name_src})
                self.j.event("signal", f"Источник «{name_src}» прошёл проверку ({src['wins']} из {src['scored']} в плюсе, {src['total_pct']:+.1f}%): "
                                       f"создан стажёр «{name}» с счётом {self.s.agent_start_balance:.0f} $", name, ts=ts)
                added.append(agent)
            elif rating == "failed" and a is not None and a.status == "intern":
                self.j.kv_set(f"signal_drop:{name_src}", ts)
                director.drop_intern(a, price, ts, f"источник «{name_src}» перестал проходить проверку: "
                                                   f"точность {src['accuracy'] or 0:.0f}%, суммарно {src['total_pct']:+.1f}%")
        return added

    def open_for(self, source: str) -> list[dict]:
        return [s for s in self.j.signals_live() if s["source"] == source]

    def state(self, agents: list[Agent], price: float, now_i: int) -> dict:
        live = []
        for s in self.j.signals_live():
            sign = 1 if s["side"] == "long" else -1
            live.append({**s, "side_ru": SIDE_RU[s["side"]], "status_ru": STATUS_RU[s["status"]],
                         "pnl_pct": round(sign * (price / float(s["entry"]) - 1) * 100, 2) if s["status"] == "open" and price else None,
                         "hours": round((now_i - int(s["opened_ts"] or s["ts"])) / 3600, 1)})
        recent = [{**s, "side_ru": SIDE_RU[s["side"]], "status_ru": STATUS_RU.get(s["status"], s["status"])}
                  for s in self.j.signals(limit=60) if s["status"] not in {"pending", "open"}][:12]
        return {"sources": self.sources(agents), "live": live, "recent": recent,
                "min_count": self.s.signal_min_count, "min_accuracy": self.s.signal_min_accuracy,
                "max_h": self.s.signal_max_h, "entry_h": self.s.signal_entry_h, "fees_pct": round(self.fees_pct(), 2)}


def is_follower(a: Agent) -> bool:
    return getattr(a.strategy, "external", False)
