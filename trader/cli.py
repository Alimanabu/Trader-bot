"""Команды: serve (панель + планировщик), tick (один шаг), simulate (прогон на истории), research."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .config import load_settings
from .data.market import SyntheticMarket
from .engine import Engine
from .research import result_to_dict


def review(settings_port: int = 8080) -> int:
    """Текстовая сводка по работающей компании через её же API (для Termius и отправки в чат)."""
    import base64
    import urllib.request
    from datetime import datetime, timezone, timedelta

    pw = os.environ.get("PANEL_PASSWORD", "")
    req = urllib.request.Request(f"http://127.0.0.1:{settings_port}/api/state")
    if pw:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"x:{pw}".encode()).decode())
    try:
        s = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception as e:  # noqa: BLE001
        print(f"Не удалось получить состояние: {e}")
        return 1
    tz = timezone(timedelta(hours=5))
    t = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%d.%m %H:%M") if ts else "—"  # noqa: E731
    d, h = s["department"], s.get("head", {})
    reg = {"up": "рост", "flat": "боковик", "down": "падение"}
    team = [a for a in s["agents"] if a["status"] != "fired"]
    day = sum(a["pnl_day"] for a in team)
    print(f"=== Botz · обзор {t(s['now'])} (Астана) · BTC {s['price']:.0f} $ · источник {s['market']} ===")
    print(f"Капитал {d['equity']:.2f} $ · сегодня {day:+.2f} $ · всего {d['pnl']:+.2f} $ ({d['pnl']/d['start']*100:+.2f}%)"
          + (" · КОМПАНИЯ ОСТАНОВЛЕНА" if d.get("halted") else ""))
    dr = h.get("director", {})
    print(f"Режим: {reg.get(h.get('regime'), h.get('regime'))} ({h.get('source', '')}) · подход {'защитный' if h.get('mode') == 'defensive' else 'обычный'}"
          f" · {dr.get('name', 'директор')} рейтинг {dr.get('rating', 50)}, премия {dr.get('bonus', 0):+.2f} $")
    c = s.get("consensus") or {}
    if c.get("fresh"):
        print(f"Аналитики: {reg.get(c['regime'], c['regime'])} ({c['strength']*100:.0f}%)")
    print("\n--- Дески ---")
    for x in s.get("desks", []):
        print(f"{x['label']:<13} {x['agents']}/{x['size']} · капитал {x['equity']:.0f} $ · сегодня {x['pnl_day']:+.2f} · неделя {x['pnl_week']:+.2f}"
              f" · всего {x['pnl']:+.2f} · потолок {x['cap']*100:.0f}% · в позиции {x['in_position']}")
    pos = [p for p in s.get("positions", []) if p["kind"] != "intern"]
    kinds = {"trailing": "подтянутый", "breakeven": "безубыток", "initial": "стоп", "": "без стопа"}
    print(f"\n--- Открытые позиции трейдеров: {len(pos)} · на бумаге {sum(p['upnl'] for p in pos):+.2f} $ ---")
    for p in pos:
        print(f"{p['agent'][:26]:<26} {'лонг ' if p['side'] == 'long' else 'шорт '}{p['exposure']*100:>4.0f}% · вход {p['entry']:.0f} · "
              f"{p['upnl']:+.2f} $ ({p['upnl_pct']:+.2f}%) · {kinds.get(p['stop_kind'], '')} {p['stop'] or 0:.0f} ({p['stop_pct'] or 0:+.1f}%)"
              f" · {p['hours'] or 0:.1f} ч{' · половина зафикс.' if p.get('partial_taken') else ''}")
    out = [a for a in team if abs(a["exposure"]) < 1e-9]
    print(f"Вне рынка: {len(out)}: " + ", ".join(a["name"] for a in out))
    tr = [x for x in s.get("trades_24h", []) if x["kind"] != "intern"]
    closed = [x for x in tr if x.get("pnl") is not None]
    wins = [x for x in closed if x["pnl"] > 0]
    print(f"\n--- Сделки за 24 ч: {len(tr)} · закрыто {len(closed)}, в плюсе {len(wins)} · итог закрытых {sum(x['pnl'] for x in closed):+.2f} $ ---")
    for x in closed[:12]:
        print(f"{t(x['ts'])} {x['agent'][:24]:<24} {x['pnl']:+.2f} $ · {(x.get('reason') or '')[:60]}")
    worst = sorted(team, key=lambda a: a["pnl_total"])[:3]
    best = sorted(team, key=lambda a: -a["pnl_total"])[:3]
    print("\nЛучшие: " + "; ".join(f"{a['name']} {a['pnl_total']:+.2f}" for a in best))
    print("Худшие: " + "; ".join(f"{a['name']} {a['pnl_total']:+.2f} (просадка {a['drawdown']*100:.1f}%)" for a in worst))
    print("\n--- Последние события ---")
    for e in s.get("events", [])[:8]:
        print(f"{t(e['ts'])} [{e['kind']}] {e['message'][:140]}")
    ls = s.get("llm_spend") or {}
    print(f"\nНейросеть: {'есть ключ' if s.get('llm') else 'нет ключа'} · сегодня {ls.get('usd', 0):.2f} $ из {ls.get('budget', 0):.2f} $, вызовов {ls.get('calls', 0)}"
          + (f" · ошибка: {s['llm_error']['text'][:80]}" if s.get("llm_error") else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="trader", description="Отдел BTC-агентов на демосчёте")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="запустить веб-панель и планировщик")
    t = sub.add_parser("tick", help="выполнить один шаг вручную")
    t.add_argument("--force", action="store_true")
    s = sub.add_parser("simulate", help="прогнать отдел по синтетической истории (для проверки логики)")
    s.add_argument("--hours", type=int, default=240)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--db", default=":memory:")
    sub.add_parser("research", help="запустить отдел исследований и показать кандидатов")
    sub.add_parser("review", help="краткий обзор компании: капитал, дески, открытые позиции, сделки за сутки")
    args = p.parse_args(argv)

    if args.cmd == "review":
        return review(settings_port=int(os.environ.get("PORT", "8080")))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()

    if args.cmd == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(), host="0.0.0.0", port=settings.port)
        return 0

    if args.cmd == "tick":
        eng = Engine(settings)
        print(json.dumps(eng.tick(force=args.force), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "simulate":
        from .journal import Journal
        from .llm import ClaudeClient
        settings.db_path = args.db
        market = SyntheticMarket(seed=args.seed)
        eng = Engine(settings, market=market, journal=Journal(args.db), client=ClaudeClient(None))
        for _ in range(args.hours):
            last = market.candles(settings.symbol, settings.timeframe, 1)[-1]
            res = eng.tick(now=last.ts + 2 * 3600)
            market.advance(1)
            if res.get("fired") or res.get("hired") or res.get("halt"):
                print(json.dumps({k: res[k] for k in ("ts", "fired", "hired") if k in res} | ({"halt": res["halt"]} if "halt" in res else {}), ensure_ascii=False))
        st = eng.state()
        print(f"\nКапитал отдела: {st['department']['equity']:.2f} (старт {st['department']['start']:.2f})")
        for a in st["agents"]:
            print(f"  {a['name']:<24} {a['status']:<7} {a['equity']:>9.2f}  сделок={a['trades']:<3} просадка={a['drawdown']*100:.1f}%")
        return 0

    if args.cmd == "research":
        eng = Engine(settings)
        candles = eng.market.candles(settings.symbol, settings.timeframe, settings.history_candles)
        for r in eng.head.lab.research(candles[-settings.research_lookback:], top_n=8):
            print(json.dumps(result_to_dict(r), ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
