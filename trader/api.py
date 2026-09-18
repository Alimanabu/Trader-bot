"""Веб-панель и JSON API."""
from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import load_settings
from .engine import Engine
from .research import result_to_dict
from .scheduler import Scheduler

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(engine: Engine | None = None, start_scheduler: bool = True) -> FastAPI:
    settings = load_settings()
    engine = engine or Engine(settings)
    scheduler = Scheduler(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_scheduler:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="Botz", version="0.2.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.scheduler = scheduler

    PUBLIC = {"/manifest.json", "/static/icon.svg"}

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        """Пароль на панель. Задаётся переменной PANEL_PASSWORD; логин любой."""
        if not settings.panel_password or request.url.path in PUBLIC:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                ok = secrets.compare_digest(password, settings.panel_password)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response("Нужен пароль", status_code=401, headers={"WWW-Authenticate": 'Basic realm="trader"'})
        return await call_next(request)

    @app.get("/api/state")
    def state():
        return engine.state()

    @app.get("/api/agents/{name}")
    def agent_detail(name: str):
        for a in engine.agents:
            if a.name == name:
                price = engine.last_price or a.last_price
                return {"agent": a.snapshot(price).__dict__, "decisions": engine.j.recent_decisions(name, 50),
                        "equity": engine.j.equity_curve(name, 500), "lessons": engine.j.lessons_for(name, 20),
                        "description": a.strategy.description,
                        "memory": [m for m in engine.j.memory_table("family") if m["key"] == a.strategy.family]}
        raise HTTPException(404, "агент не найден")

    @app.get("/api/journal")
    def journal(limit: int = 100):
        return {"decisions": engine.j.recent_decisions(None, limit), "trades": engine.j.recent_trades(limit),
                "events": engine.j.recent_events(limit)}

    @app.get("/api/equity")
    def equity():
        team = {r["name"] for r in engine.j.all_agents() if r["status"] not in ("intern", "dropped")}
        return {k: v for k, v in engine.j.equity_all(500).items() if k in team}

    @app.get("/api/curve")
    def curve():
        team = {r["name"] for r in engine.j.all_agents() if r["status"] not in ("intern", "dropped")}
        return engine.j.department_curve(team)

    @app.get("/api/summary")
    def summary():
        team = {r["name"] for r in engine.j.all_agents() if r["status"] not in ("intern", "dropped")}
        reports = [e for e in engine.j.recent_events(400) if e["kind"] == "report"][:30]
        return {"daily": engine.j.daily_department(team), "reports": reports,
                "all_agents": engine.j.all_agents()}

    @app.get("/api/families")
    def families():
        from .agents.registry import STRATEGY_FAMILIES, SIDED_FAMILIES, family_label, build_strategy
        out = [{"family": f, "label": family_label(f), "description": cls.description,
                "llm": False, "params": cls.default_params(), "side": "long"}
               for f, cls in STRATEGY_FAMILIES.items() if not f.startswith("llm_")]
        for f in SIDED_FAMILIES:
            st = build_strategy(f)
            out.append({"family": f, "label": family_label(f), "description": st.description, "llm": False,
                        "params": st.params, "side": st.side})
        return out

    @app.get("/api/candles")
    def candles(limit: int = 200):
        return [c.__dict__ for c in engine.last_candles[-limit:]]

    @app.post("/api/tick")
    def tick(force: bool = False):
        return scheduler.run_once(force=force)

    @app.post("/api/approvals/{approval_id}/{decision}")
    def approval(approval_id: int, decision: str):
        if decision not in {"approve", "reject"}:
            raise HTTPException(400, "decision: approve | reject")
        r = engine.apply_approval(approval_id, decision == "approve")
        if not r:
            raise HTTPException(404, "заявка не найдена или уже решена")
        return r

    @app.post("/api/agents/{name}/fire")
    def fire(name: str):
        if not engine.manual_fire(name):
            raise HTTPException(404, "агент не найден")
        return {"ok": True}

    @app.get("/api/analytics")
    def analytics():
        return {"analysts": engine.analytics.stats(), "views": engine.j.recent_views(None, 40),
                "consensus": engine.analytics.consensus(int(__import__("time").time()))}

    @app.get("/api/positions")
    def positions():
        return engine.open_positions(int(__import__("time").time()))

    @app.get("/api/knowledge")
    def knowledge():
        return engine.knowledge_state()

    @app.post("/api/knowledge/{kid}/{status}")
    def knowledge_status(kid: int, status: str):
        if status not in {"active", "retired", "accepted", "rejected", "done"}:
            raise HTTPException(400, "status")
        if not engine.j.knowledge_by_id(kid):
            raise HTTPException(404, "запись не найдена")
        engine.j.set_knowledge_status(kid, status)
        engine.risk.set_rules(engine.j.active_rules())
        return {"ok": True}

    @app.post("/api/agents/{name}/close")
    def close_position(name: str):
        if not engine.manual_close(name):
            raise HTTPException(404, "у агента нет открытой позиции")
        return {"ok": True}

    @app.post("/api/agents/{name}/pause")
    def pause_agent(name: str):
        if not engine.manual_pause(name):
            raise HTTPException(404, "агент не найден")
        return {"ok": True}

    @app.post("/api/agents/{name}/resume")
    def resume_agent(name: str):
        if not engine.manual_pause(name, resume=True):
            raise HTTPException(404, "агент не найден")
        return {"ok": True}

    @app.get("/api/trades.csv")
    def trades_csv():
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time_utc", "agent", "side", "price", "qty", "fee", "pnl", "reason"])
        from datetime import datetime, timezone
        for t in engine.j.all_trades():
            w.writerow([datetime.fromtimestamp(t["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"), t["agent"], t["side"], f"{t['price']:.2f}",
                        f"{t['qty']:.6f}", f"{t['fee']:.4f}", "" if t.get("pnl") is None else f"{t['pnl']:.2f}", t.get("reason") or ""])
        return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=botz-trades.csv"})

    @app.get("/api/push/key")
    def push_key():
        if not engine.push:
            raise HTTPException(503, "push недоступен")
        return {"key": engine.push.keys.public_b64u()}

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request):
        body = await request.json()
        sub = body.get("subscription") or body
        if not sub.get("endpoint") or not (sub.get("keys") or {}).get("p256dh"):
            raise HTTPException(400, "нет подписки")
        engine.j.push_add(sub, str(body.get("label") or "")[:60])
        return {"ok": True, "subscribers": len(engine.j.push_subs())}

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(request: Request):
        body = await request.json()
        engine.j.push_remove(body.get("endpoint", ""))
        return {"ok": True}

    @app.post("/api/push/test")
    def push_test():
        return {"sent": engine.push_test()}

    @app.get("/api/alerts")
    def alerts_list():
        return {"alerts": engine.j.alerts(), "kinds": engine.ALERT_KINDS}

    @app.post("/api/alerts")
    async def alerts_add(request: Request):
        body = await request.json()
        kind = body.get("kind")
        if kind not in engine.ALERT_KINDS:
            raise HTTPException(400, "неизвестный вид сигнала")
        aid = engine.j.alert_add(kind, float(body.get("value") or 0), str(body.get("target") or ""), bool(body.get("repeat")),
                                 ts=engine.last_poll_ts or None)
        return {"ok": True, "id": aid}

    @app.delete("/api/alerts/{aid}")
    def alerts_delete(aid: int):
        engine.j.alert_delete(aid)
        return {"ok": True}

    @app.post("/api/briefing")
    def briefing_now():
        return engine.morning_briefing(int(__import__("time").time()))

    @app.post("/api/lab/backtest")
    async def lab_backtest(request: Request):
        body = await request.json()
        from .agents.registry import STRATEGY_FAMILIES, SIDED_FAMILIES
        fam = body.get("family")
        if fam not in STRATEGY_FAMILIES and fam not in SIDED_FAMILIES:
            raise HTTPException(400, "неизвестное семейство")
        params = body.get("params") or None
        try:
            return engine.lab_backtest(fam, params, int(body.get("days") or 30))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"не удалось прогнать: {e}")

    @app.get("/api/live")
    def live():
        return engine.live_state()

    @app.get("/api/at")
    def at(ts: int):
        return engine.snapshot_at(int(ts))

    @app.get("/api/correlation")
    def correlation(days: int = 7):
        return engine.correlation(days)

    @app.get("/api/stress")
    def stress():
        return engine.stress_test()

    @app.get("/api/m1")
    def m1(limit: int = 240):
        return [c.__dict__ for c in engine.m1[-limit:]]

    @app.post("/api/research")
    def research():
        if not engine.last_candles:
            engine.last_candles = engine.market.candles(settings.symbol, settings.timeframe, settings.history_candles)
        n = engine.head.refresh_bench(engine.last_candles, engine.last_candles[-1].ts, engine.agents)
        return {"ok": True, "candidates": n, "bench": engine.j.bench()}

    if WEB_DIR.exists():
        @app.get("/")
        def index():
            return FileResponse(WEB_DIR / "index.html")

        @app.get("/manifest.json")
        def manifest():
            return FileResponse(WEB_DIR / "manifest.json", media_type="application/manifest+json")

        @app.get("/sw.js")
        def sw():
            return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")

        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    return app
