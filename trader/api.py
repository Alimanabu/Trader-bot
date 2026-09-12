"""Веб-панель и JSON API."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
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

    app = FastAPI(title="Отдел BTC-агентов", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.scheduler = scheduler

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
                        "description": a.strategy.description}
        raise HTTPException(404, "агент не найден")

    @app.get("/api/journal")
    def journal(limit: int = 100):
        return {"decisions": engine.j.recent_decisions(None, limit), "trades": engine.j.recent_trades(limit),
                "events": engine.j.recent_events(limit)}

    @app.get("/api/equity")
    def equity():
        return engine.j.equity_all(500)

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

    @app.post("/api/research")
    def research():
        if not engine.last_candles:
            engine.last_candles = engine.market.candles(settings.symbol, settings.timeframe, settings.history_candles)
        n = engine.head.refresh_bench(engine.last_candles, engine.last_candles[-1].ts)
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
