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
