"""엔진을 백그라운드 스레드에서 돌리고, 로그와 이벤트를 화면(웹)에 전달한다."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from itertools import count


class LogBuffer(logging.Handler):
    """최근 2000건의 로그를 들고 있다가 화면이 필요할 때 꺼내 준다."""

    def __init__(self, capacity: int = 2000, bus=None):
        super().__init__()
        self.capacity = capacity
        self.bus = bus
        self._buf: deque = deque(maxlen=capacity)
        self._id_counter = count(1)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        created = datetime.fromtimestamp(record.created)
        row = {
            "id": next(self._id_counter),
            "date": created.strftime("%Y-%m-%d"),
            "time": created.strftime("%H:%M:%S"),
            "level": record.levelname,
            "source": record.name,
            "text": record.getMessage(),
        }
        with self._lock:
            self._buf.append(row)
        if self.bus is not None:
            try:
                self.bus.publish("log", row)
            except Exception:
                pass

    def since(self, after: int = 0, limit: int | None = None) -> list:
        with self._lock:
            rows = [r for r in self._buf if r["id"] > after]
        if limit:
            rows = rows[-limit:]
        return rows

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._buf[-1]["id"] if self._buf else 0


class EventBus:
    """구독자마다 큐를 따로 둔다.
    ★ 큐가 넘치면 오래된 것부터 버리되 "일부 이벤트를 건너뛰었습니다" alert 를
    끼워 넣는다 - 조용히 잃으면 화면과 실제가 어긋난다.
    """

    def __init__(self, maxsize: int = 500):
        self.maxsize = maxsize
        self._subscribers: list = []
        self._lock = threading.Lock()

    def subscribe(self):
        q: deque = deque(maxlen=self.maxsize)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: str, data) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            # ★ deque(maxlen=...) 는 가득 차면 append 시 오래된 것을 조용히 버린다.
            # 조용히 잃으면 화면과 실제가 어긋나므로, 넘치기 직전이면 alert 를 먼저 넣는다.
            if len(q) >= q.maxlen and not (q and q[-1].get("event") == "alert"):
                q.append({"event": "alert", "data": "일부 이벤트를 건너뛰었습니다."})
            q.append({"event": event, "data": data})

    def wait(self, q, timeout: float = 1.0) -> list:
        """큐에 이벤트가 쌓일 때까지 기다렸다가 모아서 돌려준다."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if q:
                items = []
                while q:
                    items.append(q.popleft())
                return items
            time.sleep(0.05)
        return []


class EngineRunner:
    """엔진을 데몬 스레드에서 돌린다. 예외가 나면 죽지 않고 self.error 에 남는다."""

    def __init__(self, log_buffer: LogBuffer, bus: EventBus | None = None):
        self.log_buffer = log_buffer
        self.bus = bus
        self.engine = None
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self._stop_requested = False

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, cfg, client=None, client_factory=None, day_dates=None) -> None:
        """엔진을 백그라운드에서 시작한다.
        ★ 여러 날을 이어 돌려야 성과 곡선이 생긴다 - day_dates 가 있으면
        하루씩 순서대로 새 클라이언트로 이어 돈다.
        """
        if self.running:
            return
        self.error = None
        self._stop_requested = False

        def _run() -> None:
            from daytrader.engine import Engine
            try:
                days = day_dates or [None]
                for day in days:
                    if self._stop_requested:
                        break
                    c = client if client is not None else (client_factory(day) if client_factory else None)
                    if c is None:
                        raise RuntimeError("client 또는 client_factory 중 하나는 있어야 합니다.")
                    self.engine = Engine(cfg, c)
                    self.engine.run()
                    # ★★★ "모의매매가 안 된다"는 문의의 원인 - 예외 없이
                    # 정상 종료(예: 장 마감 후라 오늘 할 일이 없음)해도
                    # 화면엔 아무 설명이 없었다. 엔진이 남긴 사유를 error
                    # 필드에 그대로 실어 보낸다 - "왜 멈췄는지"를 사용자가
                    # 볼 수 있어야 한다(진짜 에러가 아니어도).
                    if self.engine.last_stop_reason and not self._stop_requested:
                        self.error = self.engine.last_stop_reason
            except Exception:
                # ★ 예외는 traceback.format_exc(limit=6) 을 self.error 에 남긴다 -
                # 스레드가 조용히 죽으면 사용자는 왜 멈췄는지 알 수 없다.
                self.error = traceback.format_exc(limit=6)
                if self.bus is not None:
                    try:
                        self.bus.publish("error", self.error)
                    except Exception:
                        pass

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()

    def stop(self, close_positions: bool = False) -> None:
        self._stop_requested = True
        if self.engine is not None:
            self.engine.request_stop(close_positions=close_positions)

    def status(self) -> dict:
        return {
            "running": self.running,
            "error": self.error,
            "snapshot": self.engine.snapshot() if self.engine is not None else None,
        }
