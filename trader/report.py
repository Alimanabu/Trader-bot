"""Полный отчёт для разбора: всё, что нужно, чтобы оценить компанию за весь срок работы.

Отчёт собирается из журнала и текущего состояния движка в один Markdown-файл. Его можно скачать
кнопкой в терминале или получить командой `python -m trader report` и приложить в чат для анализа.
Итог каждой сделки в журнале уже очищен от комиссий входа и выхода.
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .agents.registry import DESKS, EXPERIMENTS, EXTERNAL_FAMILIES, RANK_LABELS, desk_of, family_label, family_side

TZ = timezone(timedelta(hours=5))          # Астана
REG_RU = {"up": "рост", "flat": "боковик", "down": "падение"}
STATUS_RU = {"active": "в команде", "paused": "пауза", "fired": "уволен", "intern": "стажёр", "dropped": "отчислен",
             "experiment": "эксперимент", "moved": "в аналитике"}


def _t(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M") if ts else "—"


def _d(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _pct(x: float | None, d: int = 1) -> str:
    return "—" if x is None else f"{x:+.{d}f}%"


def _usd(x: float | None, d: int = 2) -> str:
    return "—" if x is None else f"{x:+,.{d}f}".replace(",", " ")


def _table(head: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["_нет данных_", ""]
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out + [""]


def exit_kind(reason: str) -> str:
    r = (reason or "").lower()
    for key, label in (("ликвидац", "ликвидация"), ("стоп отдела", "стоп компании"), ("увольнение", "увольнение"),
                       ("отчисление", "отчисление"), ("перевод в стажёры", "перевод в стажёры"), ("пауза", "пауза"),
                       ("фиксация", "фиксация части"), ("подтянут", "подтянутый стоп"), ("безубыт", "стоп в безубыток"),
                       ("стоп-лосс", "стоп-лосс"), ("стоп", "стоп-лосс"), ("владелец", "закрыл владелец"),
                       ("перезапуск", "перезапуск эксперимента"), ("пыль", "остаток")):
        if key in r:
            return label
    return "сигнал стратегии"


def _round_trips(trades: list[dict]) -> list[dict]:
    """Сделки агента → круги «вход … полный выход» с длительностью и итогом."""
    out = []
    open_ts, side, pnl = None, None, 0.0
    for t in trades:
        before = (t["pos_after"] or 0.0) + (-t["qty"] if t["side"] == "BUY" else t["qty"])
        if open_ts is None and abs(t["pos_after"] or 0.0) > 1e-12:
            open_ts, side, pnl = t["ts"], ("long" if (t["pos_after"] or 0) > 0 else "short"), 0.0
        if t["pnl"] is not None:
            pnl += t["pnl"]
        if open_ts is not None and abs(t["pos_after"] or 0.0) < 1e-12 and abs(before) > 1e-12:
            out.append({"agent": t["agent"], "open": open_ts, "close": t["ts"], "hours": (t["ts"] - open_ts) / 3600,
                        "side": side, "pnl": pnl, "exit": exit_kind(t["reason"])})
            open_ts, side, pnl = None, None, 0.0
    return out


def _stats(rows: list[dict]) -> dict:
    closed = [r for r in rows if r["pnl"] is not None]
    wins = [r["pnl"] for r in closed if r["pnl"] > 0]
    losses = [r["pnl"] for r in closed if r["pnl"] <= 0]
    gl = -sum(losses)
    return {"trades": len(rows), "closed": len(closed), "wins": len(wins),
            "wr": len(wins) / len(closed) * 100 if closed else None,
            "pnl": sum(r["pnl"] for r in closed), "fees": sum(r["fee"] or 0 for r in rows),
            "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "pf": (sum(wins) / gl) if gl > 0 else None}


def build_report(eng, now: int | None = None) -> str:
    now = int(now or eng.last_poll_ts or time.time())
    j, price = eng.j, eng.last_price
    agents_db = {r["name"]: r for r in j.all_agents()}
    live = {a.name: a for a in eng.agents}
    trades = j.all_trades()
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_agent[t["agent"]].append(t)
    fam_of = {n: r["strategy"] for n, r in agents_db.items()}
    desk_by_name = {n: desk_of(family_side(f)) if not f.startswith("llm_") and f not in EXPERIMENTS else "—" for n, f in fam_of.items()}
    eq_first = j._rows("SELECT MIN(ts) AS ts FROM equity")[0]["ts"] or now
    start_price_row = j._rows("SELECT price FROM equity ORDER BY ts ASC LIMIT 1")
    start_price = start_price_row[0]["price"] if start_price_row else price
    days = max(1e-9, (now - eq_first) / 86400)
    st = eng.state()
    dep = st["department"]
    L: list[str] = []
    L += [f"# Botz · отчёт для разбора · {_t(now)} (Астана)", ""]
    L += [f"Период: {_t(eq_first)} — {_t(now)}, {days:.1f} дн. Режим: бумажная торговля. Источник котировок: {st['market']}.", ""]

    # --- 1. главное ---
    btc_pct = (price / start_price - 1) * 100 if start_price else None
    team_pct = dep["pnl"] / dep["start"] * 100 if dep["start"] else None
    all_closed = [t for t in trades if t["pnl"] is not None]
    fees_all = sum(t["fee"] or 0 for t in trades)
    L += ["## 1. Главное", ""]
    L += _table(["Показатель", "Значение"], [
        ["BTC в начале → сейчас", f"{start_price:,.0f} → {price:,.0f} $ ({_pct(btc_pct)})".replace(",", " ")],
        ["Капитал команды сейчас", f"{dep['equity']:,.2f} $ из {dep['start']:,.2f} $ ({_pct(team_pct, 2)})".replace(",", " ")],
        ["Итог команды", _usd(dep["pnl"]) + " $"],
        ["Все закрытые сделки (все счета, включая стажёров)", f"{len(all_closed)}, итог {_usd(sum(t['pnl'] for t in all_closed))} $"],
        ["Все комиссии (все счета)", f"{fees_all:,.2f} $".replace(",", " ")],
        ["Сделок в день (все счета)", f"{len(trades) / days:.1f}"],
        ["Трейдеров в команде · стажёров · экспериментов", f"{len([a for a in st['agents'] if a['status'] != 'fired'])} · {len(st['interns'])} · {len(st.get('experiments', []))}"],
        ["Компания останавливалась сегодня", "да" if dep.get("halted") else "нет"],
    ])

    # --- 2. по дням ---
    regime_log = j.kv_get("regime_log", {}) or {}
    day_price: dict[str, list[float]] = {}
    for r in j._rows("SELECT date(ts,'unixepoch') AS day, MIN(price) AS lo, MAX(price) AS hi FROM equity GROUP BY day"):
        day_price[r["day"]] = [r["lo"], r["hi"]]
    first_last = {r["day"]: (r["o"], r["c"]) for r in j._rows(
        "SELECT day, (SELECT price FROM equity e2 WHERE date(e2.ts,'unixepoch')=d.day ORDER BY ts ASC LIMIT 1) AS o, "
        "(SELECT price FROM equity e3 WHERE date(e3.ts,'unixepoch')=d.day ORDER BY ts DESC LIMIT 1) AS c "
        "FROM (SELECT DISTINCT date(ts,'unixepoch') AS day FROM equity) d")}
    per_day: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "n": 0, "fees": 0.0, "desk": defaultdict(float), "team": 0.0, "interns": 0.0})
    for t in trades:
        d = per_day[_d(t["ts"])]
        d["fees"] += t["fee"] or 0
        d["n"] += 1
        if t["pnl"] is not None:
            d["pnl"] += t["pnl"]
            d["desk"][desk_by_name.get(t["agent"], "?")] += t["pnl"]
    rows = []
    hits = total_calls = 0
    for day in sorted(set(per_day) | set(first_last)):
        o, c = first_last.get(day, (None, None))
        mv = (c / o - 1) * 100 if o and c else None
        reg = regime_log.get(day)
        if reg and mv is not None:
            total_calls += 1
            hits += (reg == "up" and mv > 0.5) or (reg == "down" and mv < -0.5) or (reg == "flat" and abs(mv) <= 1.5)
        d = per_day.get(day)
        rows.append([day, REG_RU.get(reg, reg or "—"), _pct(mv, 2), d["n"] if d else 0, _usd(d["pnl"]) if d else "—",
                     *(_usd(d["desk"].get(k, 0.0)) if d else "—" for k in DESKS), f"{d['fees']:.2f}" if d else "—"])
    L += ["## 2. По дням (UTC)", "",
          "Итог это закрытые за день сделки всех счетов, включая стажёров, за вычетом комиссий. Режим это решение директора на тот день.", ""]
    L += _table(["День", "Режим", "BTC за день", "Сделок", "Итог $", *(DESKS[k]["label"] for k in DESKS), "Комиссии $"], rows)
    if total_calls:
        L += [f"Режим директора совпал с движением дня в {hits} из {total_calls} дней ({hits / total_calls * 100:.0f}%). "
              f"Совпадение: рост при BTC > +0,5%, падение при < −0,5%, боковик при |движении| ≤ 1,5%.", ""]

    # --- 3. дески ---
    L += ["## 3. Дески: команда сейчас и все сделки деска", ""]
    rows = []
    for dk in st["desks"]:
        names_all = {n for n, d in desk_by_name.items() if d == dk["key"]}
        team_names = {a["name"] for a in st["agents"] if a["desk"] == dk["key"] and a["status"] != "fired"}
        s_all = _stats([t for n in names_all for t in by_agent.get(n, [])])
        s_team = _stats([t for n in team_names for t in by_agent.get(n, [])])
        rows.append([dk["label"], f"{dk['agents']}/{dk['size']}", f"{dk['equity']:.0f}", _usd(dk["pnl"]), _usd(dk["pnl_week"]),
                     f"{dk['cap'] * 100:.0f}%", f"{s_team['closed']} · {_pct(s_team['wr'], 0).lstrip('+')}",
                     f"{s_all['closed']} · {_pct(s_all['wr'], 0).lstrip('+')}", _usd(s_all["pnl"]), f"{s_all['fees']:.2f}",
                     "—" if s_all["pf"] is None else f"{s_all['pf']:.2f}"])
    L += _table(["Деск", "Мест", "Капитал $", "Итог команды $", "За неделю $", "Потолок", "Команда: закрыто · доля плюсовых",
                 "Все счета деска: закрыто · доля плюсовых", "Итог всех $", "Комиссии $", "Профит-фактор"], rows)

    # --- 4. семейства ---
    fam_rows: dict[str, list[dict]] = defaultdict(list)
    fam_agents: dict[str, set] = defaultdict(set)
    for n, ts_ in by_agent.items():
        f = fam_of.get(n, "?")
        fam_rows[f] += ts_
        fam_agents[f].add(n)
    rows = []
    for f, rs in fam_rows.items():
        s = _stats(rs)
        now_in = [live[n] for n in fam_agents[f] if n in live and live[n].status not in {"fired", "dropped"}]
        desk_label = ("нейро" if f.startswith("llm_") else "эксперимент" if f in EXPERIMENTS else "сигналы" if f in EXTERNAL_FAMILIES
                      else DESKS.get(desk_of(family_side(f)), {}).get("label", "—"))
        rows.append((s["pnl"], [family_label(f), f, desk_label,
                                len(fam_agents[f]), len(now_in), s["closed"], _pct(s["wr"], 0).lstrip("+"), _usd(s["pnl"]),
                                _usd(s["avg_win"]), _usd(s["avg_loss"]), "—" if s["pf"] is None else f"{s['pf']:.2f}", f"{s['fees']:.2f}"]))
    rows.sort(key=lambda x: -x[0])
    L += ["## 4. Семейства стратегий (все счета за весь срок)", "",
          "Отсортировано по итогу. «Счетов» это сколько агентов этого семейства было всего, «сейчас» это сколько работает.", ""]
    L += _table(["Семейство", "Код", "Деск", "Счетов", "Сейчас", "Закрыто", "Доля плюсовых", "Итог $", "Средний плюс $",
                 "Средний минус $", "Профит-фактор", "Комиссии $"], [r for _, r in rows])

    # --- 5. круги сделок: выходы, стороны, длительность ---
    trips = [x for n, ts_ in by_agent.items() for x in _round_trips(sorted(ts_, key=lambda t: (t["ts"], t["id"])))]
    L += ["## 5. Круги сделок: как выходили, в какую сторону, сколько держали", "",
          f"Круг это вход и полный выход. Всего кругов: {len(trips)}.", ""]
    by_exit: dict[str, list[dict]] = defaultdict(list)
    for x in trips:
        by_exit[x["exit"]].append(x)
    L += _table(["Выход", "Кругов", "В плюсе", "Итог $", "Средний $", "Средняя длительность, ч"],
                sorted(([k, len(v), sum(1 for x in v if x["pnl"] > 0), _usd(sum(x["pnl"] for x in v)), _usd(sum(x["pnl"] for x in v) / len(v)),
                         f"{sum(x['hours'] for x in v) / len(v):.1f}"] for k, v in by_exit.items()), key=lambda r: -r[1]))
    for side, label in (("long", "Лонги"), ("short", "Шорты")):
        v = [x for x in trips if x["side"] == side]
        if v:
            w = [x for x in v if x["pnl"] > 0]
            lo = [x for x in v if x["pnl"] <= 0]
            L.append(f"- {label}: {len(v)} кругов, в плюсе {len(w)} ({len(w) / len(v) * 100:.0f}%), итог {_usd(sum(x['pnl'] for x in v))} $, "
                     f"плюсовые держали в среднем {sum(x['hours'] for x in w) / len(w) if w else 0:.1f} ч, "
                     f"минусовые {sum(x['hours'] for x in lo) / len(lo) if lo else 0:.1f} ч.")
    buckets = [(0, 1, "до 1 ч"), (1, 4, "1–4 ч"), (4, 12, "4–12 ч"), (12, 24, "12–24 ч"), (24, 72, "1–3 дня"), (72, 1e9, "больше 3 дней")]
    L += ["", "По длительности:", ""]
    L += _table(["Держали", "Кругов", "В плюсе", "Итог $"],
                [[lab, len(v), sum(1 for x in v if x["pnl"] > 0), _usd(sum(x["pnl"] for x in v))]
                 for lo_, hi_, lab in buckets for v in [[x for x in trips if lo_ <= x["hours"] < hi_]] if v])

    # --- 6. часы и дни недели ---
    all_names = set(by_agent)
    hm = j.heatmap(all_names, 0)
    L += ["## 6. Итог закрытых сделок по часам (UTC, Астана = UTC+5) и дням недели", ""]
    L += _table(["Час UTC", *[f"{h:02d}" for h in range(24)]],
                [["итог $", *[f"{c['pnl']:.0f}" for c in hm.get("hours", [])]], ["сделок", *[c["n"] for c in hm.get("hours", [])]]])
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    L += _table(["День", *wd], [["итог $", *[f"{c['pnl']:.0f}" for c in hm.get("weekdays", [])]],
                                ["сделок", *[c["n"] for c in hm.get("weekdays", [])]]])

    # --- 7. люди ---
    L += ["## 7. Команда сейчас", ""]
    rows = []
    for a in sorted(st["agents"], key=lambda x: -x["pnl_total"]):
        if a["status"] == "fired":
            continue
        s = _stats(by_agent.get(a["name"], []))
        rows.append([a["name"], a["strategy"], DESKS[a["desk"]]["label"], RANK_LABELS.get(a["rank"], a["rank"]), STATUS_RU.get(a["status"], a["status"]),
                     f"{a['days']:.1f}", f"{a['start_balance']:.0f}", _usd(a["pnl_total"]), _usd(a["pnl_week"]), f"{a['drawdown'] * 100:.1f}%",
                     f"{s['closed']} · {_pct(s['wr'], 0).lstrip('+')}", a.get("streak_weeks", 0), f"{a['exposure'] * 100:.0f}%"])
    L += _table(["Трейдер", "Стратегия", "Деск", "Звание", "Статус", "Дней", "Счёт $", "Итог $", "Неделя $", "Просадка",
                 "Закрыто · доля плюсовых", "Недель в плюсе", "В позиции"], rows)
    L += ["Стажёры (лучшие 12 и худшие 5 по итогу):", ""]
    ints = sorted(st["interns"], key=lambda x: -x["pnl_total"])
    pick = ints[:12] + [x for x in ints[-5:] if x not in ints[:12]]
    L += _table(["Стажёр", "Стратегия", "Деск", "Дней", "Итог $", "Сделок", "Доля плюсовых"],
                [[a["name"], a["strategy"], DESKS[a["desk"]]["label"], f"{a['days']:.1f}", _usd(a["pnl_total"]), a["trades"],
                  _pct(a["win_rate"] * 100 if a.get("win_rate") is not None else None, 0).lstrip("+")] for a in pick])

    # --- 8. кадровые события ---
    counts = {r["kind"]: r["n"] for r in j._rows("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind")}
    L += ["## 8. События за весь срок", ""]
    names = {"stop": "стопы и фиксации", "liquidation": "ликвидации", "pause": "паузы", "halt": "остановки компании", "fire": "увольнения",
             "hire": "найм в команду", "demote": "переводы в стажёры", "drop": "отчисления стажёров", "intern": "наборы стажёров",
             "rank": "повышения звания", "live_ready": "кандидаты на реальный счёт", "weekly": "недельные ротации", "head": "решения директора",
             "capital": "перераспределения капитала", "retune": "переобучения", "lesson": "уроки", "rule": "правила", "strategist": "стратег",
             "signal": "внешние сигналы", "alert": "сработавшие сигналы владельца", "error": "ошибки", "research": "исследования"}
    L += _table(["Событие", "Сколько"], [[v, counts.get(k, 0)] for k, v in names.items() if counts.get(k)])
    stops = j._rows("SELECT message FROM events WHERE kind='stop'")
    if stops:
        c = defaultdict(int)
        for e in stops:
            c[exit_kind(e["message"]) if "зафиксировал" not in e["message"] else "фиксация части"] += 1
        L += ["Разбивка стопов: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda x: -x[1])), ""]

    # --- 9. директор ---
    h = st["head"]
    dr = h.get("director", {})
    L += ["## 9. Директор и аналитики", ""]
    L += [f"Директор сейчас: {dr.get('name', '—')}, стиль {dr.get('style', '—')}, рейтинг {dr.get('rating', '—')}, "
          f"премия {dr.get('bonus', 0):+.2f} $, недель {dr.get('weeks', 0)}. Режим {REG_RU.get(h.get('regime'), h.get('regime'))}, "
          f"подход {'защитный' if h.get('mode') == 'defensive' else 'обычный'}, потолки: "
          + ", ".join(f"{DESKS[k]['label']} {float(v) * 100:.0f}%" for k, v in (h.get("caps") or {}).items() if k in DESKS) + ".", ""]
    for d in st.get("directors_history") or []:
        L.append(f"- В прошлом: {d['text']}")
    wk = j.events_of(("weekly",), 6)
    if wk:
        L += ["", "Недельные итоги директора (последние):", ""] + [f"- {_t(e['ts'])}: {e['message'][:400]}" for e in wk]
    L += [""]
    an = st.get("analysts") or []
    L += _table(["Аналитик", "Взглядов", "Проверено", "Точность", "Последний взгляд"],
                [[a["name"], a.get("views", 0), a.get("scored", 0), _pct(a["accuracy"] * 100 if a.get("accuracy") is not None else None, 0).lstrip("+"),
                  (REG_RU.get(a["last"]["regime"], a["last"]["regime"]) + " " + _t(a["last"]["ts"])) if a.get("last") else "—"] for a in an])
    c = st.get("consensus") or {}
    if c:
        L += [f"Консенсус аналитиков сейчас: {REG_RU.get(c.get('regime'), c.get('regime'))}, сила {float(c.get('strength') or 0) * 100:.0f}%, "
              f"{'свежий' if c.get('fresh') else 'устарел'}.", ""]
    sp = st.get("llm_spend") or {}
    L += [f"Нейросеть: {'ключ есть' if st.get('llm') else 'ключа нет'}, модель {st.get('llm_model')}, сегодня {sp.get('usd', 0):.2f} $ из {sp.get('budget', 0):.2f} $, вызовов {sp.get('calls', 0)}.", ""]

    # --- 10. база знаний ---
    L += ["## 10. База знаний", ""]
    kc = j.knowledge_counts()
    L += ["Записей: " + "; ".join(f"{k}: " + ", ".join(f"{s} {n}" for s, n in v.items()) for k, v in kc.items()), ""]
    rules = j.knowledge("rule", "active", limit=30)
    if rules:
        L += ["Действующие правила:", ""] + [f"- {r['text']} (сработало {r['uses']} раз, с {_t(r['ts'])})" for r in rules] + [""]
    for kind, title in (("lesson", "Уроки"), ("insight", "Наблюдения стратега"), ("proposal", "Предложения стратега")):
        items = j.knowledge(kind, None, limit=8)
        if items:
            L += [f"{title} (последние):", ""] + [f"- [{i['status']}] {i['topic']}: {i['text'][:300]}" for i in items] + [""]

    # --- 11. эксперименты, сигналы, реальный счёт ---
    L += ["## 11. Эксперименты, внешние сигналы, реальный счёт", ""]
    for a in st.get("experiments", []):
        s = _stats(by_agent.get(a["name"], []))
        L.append(f"- {a['name']}: итог {_usd(a['pnl_total'])} $, сделок {s['trades']}, закрыто {s['closed']}, в плюсе {s['wins']}, "
                 f"комиссии {s['fees']:.2f} $, профит-фактор {'—' if s['pf'] is None else round(s['pf'], 2)}.")
    for x in (st.get("signals") or {}).get("sources", []):
        L.append(f"- Источник «{x['source']}»: {x['rating_ru']}, проверено {x['scored']} из {x['signals']}, "
                 f"точность {_pct(x['accuracy'], 0).lstrip('+')}, итог {x['total_pct']:+.1f}%.")
    lv = st.get("live") or {}
    L.append(f"- Реальный счёт: {'включён' if lv.get('enabled') else 'выключен'}" + (", тестовая сеть" if lv.get("testnet") else "") + ".")
    L += [""]

    # --- 12. открытые позиции ---
    pos = [p for p in st.get("positions", []) if p["kind"] == "team"]
    L += [f"## 12. Открытые позиции команды: {len(pos)}, на бумаге {_usd(sum(p['upnl'] for p in pos))} $", ""]
    L += _table(["Трейдер", "Сторона", "Вход", "Итог $", "Итог %", "Стоп", "Вид стопа", "Часов"],
                [[p["agent"], "лонг" if p["side"] == "long" else "шорт", f"{p['entry']:.0f}", _usd(p["upnl"]), _pct(p["upnl_pct"], 2),
                  f"{p['stop']:.0f}" if p["stop"] else "нет", p["stop_kind"] or "—", p["hours"] or 0] for p in pos])
    stress = st.get("stress") or {}
    if stress.get("moves"):
        L += ["Стресс-тест: " + ", ".join(f"BTC {k}% → {_usd(v['pnl'])} $" for k, v in stress["moves"].items()) + ".", ""]

    # --- 13. настройки ---
    s = eng.s
    L += ["## 13. Ключевые настройки", ""]
    L += [", ".join(f"{k}={getattr(s, k)}" for k in (
        "fee_rate", "slippage_rate", "stop_atr_mult", "trailing_stop", "trail_breakeven_atr", "partial_tp_atr", "partial_tp_frac",
        "exit_cooldown_min", "reentry_move_pct", "max_entries_day", "agent_daily_loss_limit", "agent_max_drawdown", "dept_daily_loss_limit",
        "team_size", "intern_count", "capital_scaling", "llm_model", "llm_model_strong", "llm_daily_budget_usd")), ""]
    return "\n".join(L)
