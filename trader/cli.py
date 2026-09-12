"""Команды: serve (панель + планировщик), tick (один шаг), simulate (прогон на истории), research."""
from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_settings
from .data.market import SyntheticMarket
from .engine import Engine
from .research import result_to_dict


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
    args = p.parse_args(argv)

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
