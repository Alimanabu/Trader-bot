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
                         ("live_ready", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {ddl}")
        tcols = {r[1] for r in self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        for col in ("pnl", "cost"):
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
        self._exec("INSERT INTO trades(ts,agent,side,price,qty,fee,reason,pnl,cost) VALUES(?,?,?,?,?,?,?,?,?)",
                   (t.ts, t.agent, t.side, t.price, t.qty, t.fee, t.reason, getattr(t, "pnl", None), getattr(t, "cost", None)))

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
        """Дописать итог (pnl, cost) продажам, записанным до появления этих полей."""
        missing = self._rows("SELECT DISTINCT agent FROM trades WHERE side='SELL' AND pnl IS NULL")
        fixed = 0
        for r in missing:
            qty_held, avg = 0.0, 0.0
            for t in self._rows("SELECT id, side, price, qty, fee, pnl FROM trades WHERE agent=? ORDER BY id", (r["agent"],)):
                if t["side"] == "BUY":
                    total = avg * qty_held + t["price"] * t["qty"] + (t["fee"] or 0.0)
                    qty_held += t["qty"]
                    avg = total / qty_held if qty_held else 0.0
                else:
                    q = min(t["qty"], qty_held) if qty_held else t["qty"]
                    if t["pnl"] is None:
                        pnl = (t["price"] - avg) * q - (t["fee"] or 0.0)
                        self._exec("UPDATE trades SET pnl=?, cost=? WHERE id=?", (pnl, avg * q, t["id"]))
                        fixed += 1
                    qty_held = max(0.0, qty_held - t["qty"])
                    if qty_held < 1e-12:
                        qty_held, avg = 0.0, 0.0
        return fixed

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
            "week_start_equity,week_key,streak_weeks,trial_weeks,live_ready)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET strategy=excluded.strategy, params=excluded.params, status=excluded.status,"
            " hired_at=excluded.hired_at, cash=excluded.cash, btc=excluded.btc, peak_equity=excluded.peak_equity,"
            " day_start_equity=excluded.day_start_equity, day_key=excluded.day_key, start_balance=excluded.start_balance,"
            " realized_pnl=excluded.realized_pnl, avg_entry=excluded.avg_entry, notes=excluded.notes,"
            " last_decided_ts=excluded.last_decided_ts, slot_minute=excluded.slot_minute, next_check_ts=excluded.next_check_ts,"
            " alert_above=excluded.alert_above, alert_below=excluded.alert_below, stop_price=excluded.stop_price,"
            " week_start_equity=excluded.week_start_equity, week_key=excluded.week_key, streak_weeks=excluded.streak_weeks,"
            " trial_weeks=excluded.trial_weeks, live_ready=excluded.live_ready",
            (a.name, a.strategy.family, json.dumps(a.strategy.params), a.status, a.hired_at, a.account.cash,
             a.account.btc, a.peak_equity, a.day_start_equity, a.day_key, a.start_balance(),
             a.account.realized_pnl, a.account._avg_entry, json.dumps(a.notes, ensure_ascii=False),
             a.last_decided_ts, a.slot_minute, a.next_check_ts, a.alert_above, a.alert_below, a.stop_price,
             a.week_start_equity, a.week_key, a.streak_weeks, a.trial_weeks, int(a.live_ready)))

    def mark_fired(self, name: str, ts: int) -> None:
        self._exec("UPDATE agents SET status='fired', fired_at=? WHERE name=?", (ts, name))

    def load_agents(self) -> list[dict]:
        return self._rows("SELECT * FROM agents WHERE status NOT IN ('fired', 'dropped')")

    def set_status(self, name: str, status: str, ts: int | None = None) -> None:
        if status in {"fired", "dropped"}:
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
