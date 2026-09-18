"""Журнал: всё, что сделала система, с обоснованием и результатом. SQLite, один файл."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    agent TEXT NOT NULL,
    strategy TEXT NOT NULL,
    action TEXT NOT NULL,
    target_exposure REAL NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    price REAL NOT NULL,
    equity_before REAL NOT NULL,
    executed INTEGER NOT NULL DEFAULT 0,
    blocked_by TEXT,
    outcome_pct REAL,          -- изменение цены через горизонт оценки (заполняется позже)
    outcome_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_decisions_agent_ts ON decisions(agent, ts);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, agent TEXT NOT NULL, side TEXT NOT NULL,
    price REAL NOT NULL, qty REAL NOT NULL, fee REAL NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    ts INTEGER NOT NULL, agent TEXT NOT NULL, equity REAL NOT NULL, price REAL NOT NULL,
    PRIMARY KEY (ts, agent)
);
CREATE INDEX IF NOT EXISTS idx_equity_agent_ts ON equity(agent, ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, kind TEXT NOT NULL, agent TEXT, message TEXT NOT NULL, data TEXT
);
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY, strategy TEXT NOT NULL, params TEXT NOT NULL, status TEXT NOT NULL,
    hired_at INTEGER NOT NULL, fired_at INTEGER, cash REAL NOT NULL, btc REAL NOT NULL,
    peak_equity REAL NOT NULL, day_start_equity REAL NOT NULL, day_key TEXT NOT NULL,
    start_balance REAL NOT NULL, realized_pnl REAL NOT NULL DEFAULT 0, avg_entry REAL NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS bench (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, strategy TEXT NOT NULL, params TEXT NOT NULL,
    score REAL NOT NULL, stats TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, details TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', decided_ts INTEGER
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, agent TEXT NOT NULL, lesson TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, analyst TEXT NOT NULL, regime TEXT NOT NULL, confidence REAL NOT NULL,
    summary TEXT NOT NULL, price REAL NOT NULL, horizon_h INTEGER NOT NULL DEFAULT 24,
    outcome_pct REAL, hit INTEGER
);
CREATE INDEX IF NOT EXISTS idx_views_analyst_ts ON views(analyst, ts);
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, kind TEXT NOT NULL, topic TEXT NOT NULL, text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
    data TEXT NOT NULL DEFAULT '{}', uses INTEGER NOT NULL DEFAULT 0, updated_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_knowledge_kind ON knowledge(kind, status);
CREATE TABLE IF NOT EXISTS regime_memory (
    scope TEXT NOT NULL, key TEXT NOT NULL, regime TEXT NOT NULL,
    days INTEGER NOT NULL DEFAULT 0, pct_sum REAL NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0, updated_ts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, key, regime)
);
"""


class Journal:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(agents)").fetchall()}
        for col, ddl in (("last_decided_ts", "INTEGER NOT NULL DEFAULT 0"), ("slot_minute", "INTEGER NOT NULL DEFAULT 0"),
                         ("next_check_ts", "INTEGER NOT NULL DEFAULT 0"), ("alert_above", "REAL NOT NULL DEFAULT 0"),
                         ("alert_below", "REAL NOT NULL DEFAULT 0"), ("stop_price", "REAL NOT NULL DEFAULT 0"),
                         ("week_start_equity", "REAL NOT NULL DEFAULT 0"), ("week_key", "TEXT NOT NULL DEFAULT ''"),
                         ("streak_weeks", "INTEGER NOT NULL DEFAULT 0"), ("trial_weeks", "INTEGER NOT NULL DEFAULT 0"),
                         ("live_ready", "INTEGER NOT NULL DEFAULT 0"), ("rank", "INTEGER NOT NULL DEFAULT 1"),
                         ("best_price", "REAL NOT NULL DEFAULT 0"), ("partial_taken", "INTEGER NOT NULL DEFAULT 0"),
                         ("exit_price", "REAL NOT NULL DEFAULT 0"), ("exit_side", "TEXT NOT NULL DEFAULT ''"), ("exit_ts", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {ddl}")
        tcols = {r[1] for r in self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        for col in ("pnl", "cost", "pos_after"):
            if col not in tcols:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
        dcols = {r[1] for r in self._conn.execute("PRAGMA table_info(decisions)").fetchall()}
        for col, ddl in (("trade_side", "TEXT"), ("trade_qty", "REAL"), ("trade_price", "REAL"), ("exposure_before", "REAL")):
            if col not in dcols:
                self._conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} {ddl}")
        self._conn.commit()

    # --- служебное ---
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def kv_get(self, key: str, default=None):
        rows = self._rows("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def kv_set(self, key: str, value) -> None:
        self._exec("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))

    # --- записи ---
    def decision(self, ts, agent, strategy, action, target, confidence, reason, price, equity, executed, blocked_by=None,
                 trade=None, exposure_before=None) -> int:
        cur = self._exec(
            "INSERT INTO decisions(ts,agent,strategy,action,target_exposure,confidence,reason,price,equity_before,executed,blocked_by,"
            "trade_side,trade_qty,trade_price,exposure_before) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, agent, strategy, action, target, confidence, reason, price, equity, int(executed), blocked_by,
             trade.side if trade else None, trade.qty if trade else None, trade.price if trade else None, exposure_before))
        return cur.lastrowid

    def trade(self, t) -> None:
        self._exec("INSERT INTO trades(ts,agent,side,price,qty,fee,reason,pnl,cost,pos_after) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (t.ts, t.agent, t.side, t.price, t.qty, t.fee, t.reason, getattr(t, "pnl", None), getattr(t, "cost", None),
                    getattr(t, "pos_after", 0.0)))

    def equity(self, ts, agent, equity, price) -> None:
        self._exec("INSERT OR REPLACE INTO equity(ts,agent,equity,price) VALUES(?,?,?,?)", (ts, agent, equity, price))

    def event(self, kind: str, message: str, agent: str | None = None, data: dict | None = None, ts: int | None = None) -> None:
        self._exec("INSERT INTO events(ts,kind,agent,message,data) VALUES(?,?,?,?,?)",
                   (ts or int(time.time()), kind, agent, message, json.dumps(data or {}, ensure_ascii=False)))

    def lesson(self, agent: str, text: str, ts: int | None = None) -> None:
        self._exec("INSERT INTO lessons(ts,agent,lesson) VALUES(?,?,?)", (ts or int(time.time()), agent, text))

    def lessons_for(self, agent: str, limit: int = 8) -> list[str]:
        rows = self._rows("SELECT lesson FROM lessons WHERE agent=? ORDER BY id DESC LIMIT ?", (agent, limit))
        return [r["lesson"] for r in reversed(rows)]

    def fill_outcomes(self, price_now: float, ts_now: int, horizon_s: int = 4 * 3600) -> int:
        """Проставить результат решениям, чей горизонт оценки уже наступил."""
        rows = self._rows("SELECT id, price FROM decisions WHERE outcome_pct IS NULL AND ts <= ?", (ts_now - horizon_s,))
        for r in rows:
            pct = (price_now / r["price"] - 1) * 100 if r["price"] else 0.0
            self._exec("UPDATE decisions SET outcome_pct=?, outcome_ts=? WHERE id=?", (pct, ts_now, r["id"]))
        return len(rows)

    # --- запросы ---
    def recent_decisions(self, agent: str | None = None, limit: int = 50) -> list[dict]:
        if agent:
            return self._rows("SELECT * FROM decisions WHERE agent=? ORDER BY id DESC LIMIT ?", (agent, limit))
        return self._rows("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))

    def mistakes(self, agent: str, limit: int = 10) -> list[dict]:
        """Решения, оказавшиеся неверными: купил перед падением или продал перед ростом."""
        return self._rows(
            "SELECT * FROM decisions WHERE agent=? AND executed=1 AND outcome_pct IS NOT NULL AND "
            "((action='BUY' AND outcome_pct < -0.5) OR (action='SELL' AND outcome_pct > 0.5)) "
            "ORDER BY id DESC LIMIT ?", (agent, limit))

    def trades_for(self, agent: str) -> list[dict]:
        return self._rows("SELECT * FROM trades WHERE agent=? ORDER BY id", (agent,))

    def backfill_trade_pnl(self, fee_rate: float = 0.001) -> int:
        """Дописать итог (pnl, cost) продажам спотовых агентов, записанным до появления этих полей.
        Продажа без открытого лонга (открытие шорта) итога не имеет и не трогается."""
        futures = self._futures_agents()
        missing = self._rows("SELECT DISTINCT agent FROM trades WHERE side='SELL' AND pnl IS NULL")
        fixed = 0
        for r in missing:
            if r["agent"] in futures:
                continue
            qty_held, avg = 0.0, 0.0
            for t in self._rows("SELECT id, side, price, qty, fee, pnl FROM trades WHERE agent=? ORDER BY id", (r["agent"],)):
                if t["side"] == "BUY":
                    total = avg * qty_held + t["price"] * t["qty"] + (t["fee"] or 0.0)
                    qty_held += t["qty"]
                    avg = total / qty_held if qty_held else 0.0
                else:
                    if qty_held < 1e-12:
                        continue
                    q = min(t["qty"], qty_held)
                    if t["pnl"] is None:
                        pnl = (t["price"] - avg) * q - (t["fee"] or 0.0)
                        self._exec("UPDATE trades SET pnl=?, cost=? WHERE id=?", (pnl, avg * q, t["id"]))
                        fixed += 1
                    qty_held = max(0.0, qty_held - t["qty"])
                    if qty_held < 1e-12:
                        qty_held, avg = 0.0, 0.0
        return fixed

    def _futures_agents(self) -> set[str]:
        return {r["name"] for r in self._rows("SELECT name, strategy FROM agents")
                if r["strategy"].endswith("_short") or r["strategy"].endswith("_both")}

    def repair_short_pnl(self) -> int:
        """Разовая починка: у фьючерсных агентов пересчитать итог каждой сделки по позиции со знаком.
        Открывающие сделки получают пустой итог, закрывающие — реальный заработок."""
        if self.kv_get("repair_short_pnl_v1"):
            return 0
        fixed = 0
        for name in self._futures_agents():
            pos, avg = 0.0, 0.0
            for t in self._rows("SELECT id, side, price, qty, fee, pnl, cost FROM trades WHERE agent=? ORDER BY id", (name,)):
                fee = t["fee"] or 0.0
                q = t["qty"]
                pnl = cost = None
                if t["side"] == "BUY":
                    if pos < -1e-12:                       # закрываем шорт
                        cq = min(q, -pos)
                        pnl = (avg - t["price"]) * cq - fee * (cq / q)
                        cost = avg * cq
                        pos += cq
                        rest = q - cq
                        if rest > 1e-12:
                            avg = t["price"] + fee * (rest / q) / rest
                            pos += rest
                    else:
                        total = avg * pos + t["price"] * q + fee
                        pos += q
                        avg = total / pos if pos else 0.0
                else:
                    if pos > 1e-12:                        # закрываем лонг
                        cq = min(q, pos)
                        pnl = (t["price"] - avg) * cq - fee * (cq / q)
                        cost = avg * cq
                        pos -= cq
                        rest = q - cq
                        if rest > 1e-12:
                            avg = t["price"] - fee * (rest / q) / rest
                            pos -= rest
                    else:
                        total = avg * (-pos) + t["price"] * q - fee
                        pos -= q
                        avg = total / (-pos) if pos else 0.0
                if abs(pos) < 1e-12:
                    pos, avg = 0.0, 0.0
                if (t["pnl"] is None) != (pnl is None) or (pnl is not None and abs((t["pnl"] or 0) - pnl) > 1e-6):
                    self._exec("UPDATE trades SET pnl=?, cost=?, pos_after=? WHERE id=?", (pnl, cost, pos, t["id"]))
                    fixed += 1
        self.kv_set("repair_short_pnl_v1", 1)
        return fixed

    def trade_stats(self, names: set[str], since_ts: int = 0) -> dict:
        """Статистика закрытых сделок: число, доля прибыльных, средние, профит-фактор, комиссии, лучшая и худшая."""
        if not names:
            return {"trades": 0, "closed": 0, "wins": 0, "win_rate": None, "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": None,
                    "fees": 0.0, "pnl": 0.0, "best": None, "worst": None}
        marks = ",".join("?" * len(names))
        rows = self._rows(f"SELECT agent, ts, pnl, fee, reason FROM trades WHERE ts>=? AND agent IN ({marks})", (since_ts, *names))
        closed = [r for r in rows if r["pnl"] is not None]
        wins = [r for r in closed if r["pnl"] > 0]
        losses = [r for r in closed if r["pnl"] <= 0]
        gw = sum(r["pnl"] for r in wins)
        gl = -sum(r["pnl"] for r in losses)
        best = max(closed, key=lambda r: r["pnl"], default=None)
        worst = min(closed, key=lambda r: r["pnl"], default=None)
        return {
            "trades": len(rows), "closed": len(closed), "wins": len(wins),
            "win_rate": (len(wins) / len(closed)) if closed else None,
            "avg_win": (gw / len(wins)) if wins else 0.0, "avg_loss": (-gl / len(losses)) if losses else 0.0,
            "profit_factor": (gw / gl) if gl > 0 else (None if not wins else float("inf")),
            "fees": sum(r["fee"] or 0.0 for r in rows), "pnl": sum(r["pnl"] for r in closed),
            "best": {"agent": best["agent"], "pnl": best["pnl"], "ts": best["ts"]} if best else None,
            "worst": {"agent": worst["agent"], "pnl": worst["pnl"], "ts": worst["ts"]} if worst else None,
        }

    def all_trades(self) -> list[dict]:
        return self._rows("SELECT * FROM trades ORDER BY id")

    def fees_since(self, ts: int, names: set[str]) -> float:
        if not names:
            return 0.0
        marks = ",".join("?" * len(names))
        rows = self._rows(f"SELECT COALESCE(SUM(fee),0) AS f FROM trades WHERE ts>=? AND agent IN ({marks})", (ts, *names))
        return float(rows[0]["f"] or 0.0)

    def trades_since(self, ts: int, limit: int = 200) -> list[dict]:
        return self._rows("SELECT * FROM trades WHERE ts>=? ORDER BY id DESC LIMIT ?", (ts, limit))

    def recent_trades(self, limit: int = 100) -> list[dict]:
        return self._rows("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))

    def recent_events(self, limit: int = 100) -> list[dict]:
        return self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def event_counts(self, since_ts: int) -> dict[str, int]:
        """Сколько событий каждого вида с момента since_ts (для KPI отделов)."""
        rows = self._rows("SELECT kind, COUNT(*) AS n FROM events WHERE ts>=? GROUP BY kind", (since_ts,))
        return {r["kind"]: int(r["n"]) for r in rows}

    def events_of(self, kinds: tuple[str, ...], limit: int = 50) -> list[dict]:
        marks = ",".join("?" * len(kinds))
        return self._rows(f"SELECT * FROM events WHERE kind IN ({marks}) ORDER BY id DESC LIMIT ?", (*kinds, limit))

    # --- аналитический отдел ---
    def add_view(self, ts: int, analyst: str, regime: str, confidence: float, summary: str, price: float, horizon_h: int = 24) -> int:
        cur = self._exec("INSERT INTO views(ts,analyst,regime,confidence,summary,price,horizon_h) VALUES(?,?,?,?,?,?,?)",
                         (ts, analyst, regime, confidence, summary, price, horizon_h))
        return cur.lastrowid

    def latest_views(self) -> list[dict]:
        """Последний взгляд каждого аналитика."""
        return self._rows("SELECT v.* FROM views v JOIN (SELECT analyst, MAX(id) AS id FROM views GROUP BY analyst) m ON v.id=m.id ORDER BY v.analyst")

    def recent_views(self, analyst: str | None = None, limit: int = 30) -> list[dict]:
        if analyst:
            return self._rows("SELECT * FROM views WHERE analyst=? ORDER BY id DESC LIMIT ?", (analyst, limit))
        return self._rows("SELECT * FROM views ORDER BY id DESC LIMIT ?", (limit,))

    def score_views(self, price_now: float, ts_now: int, flat_band_pct: float = 0.5) -> int:
        """Проставить результат взглядам, чей горизонт наступил: попал ли аналитик в направление."""
        rows = self._rows("SELECT id, regime, price, horizon_h FROM views WHERE outcome_pct IS NULL AND ts + horizon_h*3600 <= ?", (ts_now,))
        for r in rows:
            pct = (price_now / r["price"] - 1) * 100 if r["price"] else 0.0
            hit = (r["regime"] == "up" and pct > flat_band_pct / 2) or (r["regime"] == "down" and pct < -flat_band_pct / 2) \
                or (r["regime"] == "flat" and abs(pct) <= flat_band_pct)
            self._exec("UPDATE views SET outcome_pct=?, hit=? WHERE id=?", (pct, int(hit), r["id"]))
        return len(rows)

    def analyst_stats(self) -> dict[str, dict]:
        rows = self._rows("SELECT analyst, COUNT(*) AS n, SUM(CASE WHEN outcome_pct IS NOT NULL THEN 1 ELSE 0 END) AS scored, "
                          "SUM(COALESCE(hit,0)) AS hits FROM views GROUP BY analyst")
        return {r["analyst"]: {"views": int(r["n"]), "scored": int(r["scored"] or 0), "hits": int(r["hits"] or 0),
                               "accuracy": (int(r["hits"] or 0) / int(r["scored"])) if r["scored"] else None} for r in rows}

    def analyst_mistakes(self, analyst: str, limit: int = 6) -> list[dict]:
        return self._rows("SELECT * FROM views WHERE analyst=? AND hit=0 ORDER BY id DESC LIMIT ?", (analyst, limit))

    def analyst_accuracy_between(self, analyst: str, ts_from: int, ts_to: int) -> tuple[int, int]:
        """(оценённых взглядов, попаданий) аналитика в промежутке времени."""
        rows = self._rows("SELECT COUNT(*) AS n, SUM(COALESCE(hit,0)) AS h FROM views WHERE analyst=? AND ts>? AND ts<=? AND outcome_pct IS NOT NULL",
                          (analyst, ts_from, ts_to))
        return int(rows[0]["n"] or 0), int(rows[0]["h"] or 0)

    # --- база знаний компании ---
    def add_knowledge(self, ts: int, kind: str, topic: str, text: str, source: str = "", data: dict | None = None,
                      status: str = "active") -> int:
        cur = self._exec("INSERT INTO knowledge(ts,kind,topic,text,source,status,data,updated_ts) VALUES(?,?,?,?,?,?,?,?)",
                         (ts, kind, topic, text, source, status, json.dumps(data or {}, ensure_ascii=False), ts))
        return cur.lastrowid

    def knowledge(self, kind: str | None = None, status: str | None = "active", topic: str | None = None, limit: int = 100) -> list[dict]:
        sql, params = "SELECT * FROM knowledge WHERE 1=1", []
        if kind:
            sql += " AND kind=?"; params.append(kind)
        if status:
            sql += " AND status=?"; params.append(status)
        if topic:
            sql += " AND topic=?"; params.append(topic)
        sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
        rows = self._rows(sql, tuple(params))
        for r in rows:
            try:
                r["data"] = json.loads(r["data"] or "{}")
            except Exception:  # noqa: BLE001
                r["data"] = {}
        return rows

    def knowledge_by_id(self, kid: int) -> dict | None:
        rows = self._rows("SELECT * FROM knowledge WHERE id=?", (kid,))
        if not rows:
            return None
        r = rows[0]
        r["data"] = json.loads(r["data"] or "{}")
        return r

    def set_knowledge_status(self, kid: int, status: str, ts: int | None = None) -> None:
        self._exec("UPDATE knowledge SET status=?, updated_ts=? WHERE id=?", (status, ts or int(time.time()), kid))

    def knowledge_hit(self, kid: int) -> None:
        self._exec("UPDATE knowledge SET uses=uses+1 WHERE id=?", (kid,))

    def knowledge_counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self._rows("SELECT kind, status, COUNT(*) AS n FROM knowledge GROUP BY kind, status"):
            out.setdefault(r["kind"], {})[r["status"]] = int(r["n"])
        return out

    def active_rules(self) -> list[dict]:
        return [r for r in self.knowledge("rule", "active", limit=50)]

    # --- память по режимам рынка ---
    def memory_add(self, scope: str, key: str, regime: str, pct: float, ts: int) -> None:
        self._exec("INSERT INTO regime_memory(scope,key,regime,days,pct_sum,wins,losses,updated_ts) VALUES(?,?,?,1,?,?,?,?) "
                   "ON CONFLICT(scope,key,regime) DO UPDATE SET days=days+1, pct_sum=pct_sum+excluded.pct_sum, "
                   "wins=wins+excluded.wins, losses=losses+excluded.losses, updated_ts=excluded.updated_ts",
                   (scope, key, regime, pct, 1 if pct > 0 else 0, 1 if pct < 0 else 0, ts))

    def memory_get(self, scope: str, key: str, regime: str) -> dict | None:
        rows = self._rows("SELECT * FROM regime_memory WHERE scope=? AND key=? AND regime=?", (scope, key, regime))
        if not rows:
            return None
        r = rows[0]
        r["avg"] = r["pct_sum"] / r["days"] if r["days"] else 0.0
        return r

    def memory_table(self, scope: str | None = None) -> list[dict]:
        rows = self._rows("SELECT * FROM regime_memory" + (" WHERE scope=?" if scope else "") + " ORDER BY scope, key, regime",
                          (scope,) if scope else ())
        for r in rows:
            r["avg"] = round(r["pct_sum"] / r["days"], 3) if r["days"] else 0.0
        return rows

    def equity_at(self, agent: str, ts: int) -> float | None:
        """Последнее известное значение капитала агента на момент ts (или None)."""
        rows = self._rows("SELECT equity FROM equity WHERE agent=? AND ts<=? ORDER BY ts DESC LIMIT 1", (agent, ts))
        if rows:
            return rows[0]["equity"]
        rows = self._rows("SELECT equity FROM equity WHERE agent=? ORDER BY ts ASC LIMIT 1", (agent,))
        return rows[0]["equity"] if rows else None

    def equity_curve(self, agent: str, limit: int = 500) -> list[dict]:
        rows = self._rows("SELECT ts, equity, price FROM equity WHERE agent=? ORDER BY ts DESC LIMIT ?", (agent, limit))
        return list(reversed(rows))

    def department_curve(self, names: set[str], bucket: int = 3600, limit: int = 2000) -> list[dict]:
        """Результат отдела по часам: сумма (капитал − стартовый баланс) по агентам.

        Каждому агенту в каждом часе берётся последнее известное значение (тянется вперёд),
        поэтому добавление или увольнение агента не даёт ложных скачков от самого капитала.
        """
        if not names:
            return []
        marks = ",".join("?" * len(names))
        starts = {r["name"]: r["start_balance"] for r in self._rows(f"SELECT name, start_balance FROM agents WHERE name IN ({marks})", tuple(names))}
        rows = self._rows(f"SELECT ts, agent, equity FROM equity WHERE agent IN ({marks}) ORDER BY ts", tuple(names))
        if not rows:
            return []
        buckets: dict[int, dict[str, float]] = {}
        for r in rows:
            b = r["ts"] // bucket * bucket
            buckets.setdefault(b, {})[r["agent"]] = r["equity"] - starts.get(r["agent"], 0.0)
        out = []
        carry: dict[str, float] = {}
        for b in sorted(buckets):
            carry.update(buckets[b])
            out.append({"ts": b, "pnl": round(sum(carry.values()), 2), "agents": len(carry)})
        return out[-limit:]

    def daily_department(self, names: set[str]) -> list[dict]:
        """Капитал команды по дням: сумма последних за день значений каждого агента."""
        if not names:
            return []
        marks = ",".join("?" * len(names))
        rows = self._rows(
            f"SELECT date(ts,'unixepoch') AS day, agent, equity, ts FROM equity WHERE agent IN ({marks}) ORDER BY ts",
            tuple(names))
        per_day: dict[str, dict[str, float]] = {}
        for r in rows:
            per_day.setdefault(r["day"], {})[r["agent"]] = r["equity"]
        out = []
        prev = None
        for day in sorted(per_day):
            total = sum(per_day[day].values())
            out.append({"day": day, "equity": round(total, 2), "agents": len(per_day[day]),
                        "change": None if prev is None else round(total - prev, 2)})
            prev = total
        return out

    def equity_all(self, limit_per_agent: int = 500) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in self._rows("SELECT DISTINCT agent FROM equity"):
            out[r["agent"]] = self.equity_curve(r["agent"], limit_per_agent)
        return out

    # --- агенты (сохранение состояния) ---
    def save_agent(self, a) -> None:
        self._exec(
            "INSERT INTO agents(name,strategy,params,status,hired_at,cash,btc,peak_equity,day_start_equity,day_key,"
            "start_balance,realized_pnl,avg_entry,notes,last_decided_ts,slot_minute,next_check_ts,alert_above,alert_below,stop_price,"
            "week_start_equity,week_key,streak_weeks,trial_weeks,live_ready,rank,best_price,partial_taken,exit_price,exit_side,exit_ts)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET strategy=excluded.strategy, params=excluded.params, status=excluded.status,"
            " hired_at=excluded.hired_at, cash=excluded.cash, btc=excluded.btc, peak_equity=excluded.peak_equity,"
            " day_start_equity=excluded.day_start_equity, day_key=excluded.day_key, start_balance=excluded.start_balance,"
            " realized_pnl=excluded.realized_pnl, avg_entry=excluded.avg_entry, notes=excluded.notes,"
            " last_decided_ts=excluded.last_decided_ts, slot_minute=excluded.slot_minute, next_check_ts=excluded.next_check_ts,"
            " alert_above=excluded.alert_above, alert_below=excluded.alert_below, stop_price=excluded.stop_price,"
            " week_start_equity=excluded.week_start_equity, week_key=excluded.week_key, streak_weeks=excluded.streak_weeks,"
            " trial_weeks=excluded.trial_weeks, live_ready=excluded.live_ready, rank=excluded.rank,"
            " best_price=excluded.best_price, partial_taken=excluded.partial_taken, exit_price=excluded.exit_price,"
            " exit_side=excluded.exit_side, exit_ts=excluded.exit_ts",
            (a.name, a.strategy.family, json.dumps(a.strategy.params), a.status, a.hired_at, a.account.cash,
             a.account.btc, a.peak_equity, a.day_start_equity, a.day_key, a.start_balance(),
             a.account.realized_pnl, a.account._avg_entry, json.dumps(a.notes, ensure_ascii=False),
             a.last_decided_ts, a.slot_minute, a.next_check_ts, a.alert_above, a.alert_below, a.stop_price,
             a.week_start_equity, a.week_key, a.streak_weeks, a.trial_weeks, int(a.live_ready), int(a.rank),
             float(a.best_price), int(a.partial_taken), float(a.exit_price), a.exit_side, int(a.exit_ts)))

    def mark_fired(self, name: str, ts: int) -> None:
        self._exec("UPDATE agents SET status='fired', fired_at=? WHERE name=?", (ts, name))

    def load_agents(self) -> list[dict]:
        return self._rows("SELECT * FROM agents WHERE status NOT IN ('fired', 'dropped', 'moved')")

    def set_status(self, name: str, status: str, ts: int | None = None) -> None:
        if status in {"fired", "dropped", "moved"}:
            self._exec("UPDATE agents SET status=?, fired_at=? WHERE name=?", (status, ts or int(time.time()), name))
        else:
            self._exec("UPDATE agents SET status=? WHERE name=?", (status, name))

    def all_agents(self) -> list[dict]:
        return self._rows("SELECT * FROM agents ORDER BY hired_at")

    # --- скамейка запасных ---
    def add_bench(self, strategy: str, params: dict, score: float, stats: dict, ts: int | None = None) -> None:
        self._exec("INSERT INTO bench(ts,strategy,params,score,stats) VALUES(?,?,?,?,?)",
                   (ts or int(time.time()), strategy, json.dumps(params), score, json.dumps(stats)))

    def clear_bench(self) -> None:
        self._exec("DELETE FROM bench WHERE used=0")

    def bench(self) -> list[dict]:
        rows = self._rows("SELECT * FROM bench WHERE used=0 ORDER BY score DESC")
        for r in rows:
            r["params"] = json.loads(r["params"])
            r["stats"] = json.loads(r["stats"])
        return rows

    def mark_bench_used(self, bench_id: int) -> None:
        self._exec("UPDATE bench SET used=1 WHERE id=?", (bench_id,))

    def take_from_bench(self, exclude: set[str] | None = None) -> dict | None:
        """Взять лучшего кандидата. exclude — множество ключей "семейство|params", уже занятых."""
        for r in self.bench():
            key = f"{r['strategy']}|{json.dumps(r['params'], sort_keys=True)}"
            if exclude and key in exclude:
                self._exec("UPDATE bench SET used=1 WHERE id=?", (r["id"],))
                continue
            self._exec("UPDATE bench SET used=1 WHERE id=?", (r["id"],))
            return r
        return None

    # --- одобрения ---
    def request_approval(self, kind: str, title: str, details: dict, ts: int | None = None) -> int:
        cur = self._exec("INSERT INTO approvals(ts,kind,title,details) VALUES(?,?,?,?)",
                         (ts or int(time.time()), kind, title, json.dumps(details, ensure_ascii=False)))
        return cur.lastrowid

    def pending_approvals(self) -> list[dict]:
        rows = self._rows("SELECT * FROM approvals WHERE status='pending' ORDER BY id")
        for r in rows:
            r["details"] = json.loads(r["details"])
        return rows

    def decide_approval(self, approval_id: int, approve: bool) -> dict | None:
        rows = self._rows("SELECT * FROM approvals WHERE id=? AND status='pending'", (approval_id,))
        if not rows:
            return None
        self._exec("UPDATE approvals SET status=?, decided_ts=? WHERE id=?",
                   ("approved" if approve else "rejected", int(time.time()), approval_id))
        r = rows[0]
        r["details"] = json.loads(r["details"])
        r["status"] = "approved" if approve else "rejected"
        return r
