"""Планировщик: ждёт закрытия очередной часовой свечи и запускает тик."""
from __future__ import annotations

import logging
import threading
import time

from .data.market import TIMEFRAME_SECONDS
from .engine import Engine

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, engine: Engine, poll_seconds: int = 60):
        self.engine = engine
        self.poll = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def run_once(self, force: bool = False) -> dict:
        with self._lock:
            return self.engine.tick(force=force)

    def _loop(self) -> None:
        step = TIMEFRAME_SECONDS.get(self.engine.s.timeframe, 3600)
        while not self._stop.is_set():
            try:
                res = self.run_once()
                if not res.get("skipped"):
                    log.info("тик %s: %s", res.get("ts"), "ok" if res.get("ok") else res.get("error"))
            except Exception:  # noqa: BLE001
                log.exception("ошибка тика")
            now = int(time.time())
            next_close = (now // step + 1) * step + 15   # 15 секунд после закрытия свечи
            wait = min(self.poll, max(5, next_close - now))
            self._stop.wait(wait)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
