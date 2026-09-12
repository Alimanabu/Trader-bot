from fastapi.testclient import TestClient

from trader.api import create_app
from trader.data.market import SyntheticMarket
from trader.engine import Engine
from trader.journal import Journal
from trader.llm import ClaudeClient


def test_api_state_and_tick(settings):
    eng = Engine(settings, market=SyntheticMarket(seed=5), journal=Journal(":memory:"), client=ClaudeClient(None))
    app = create_app(engine=eng, start_scheduler=False)
    with TestClient(app) as c:
        r = c.post("/api/tick")
        assert r.status_code == 200 and r.json()["ok"]
        st = c.get("/api/state").json()
        assert st["mode"] == "paper" and len(st["agents"]) == 10
        name = st["agents"][0]["name"]
        d = c.get(f"/api/agents/{name}").json()
        assert d["agent"]["name"] == name and d["decisions"]
        assert c.get("/").status_code == 200
