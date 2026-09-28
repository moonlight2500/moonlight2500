"""WARNING 이상의 애플리케이션 로그를 SQLite(daytrader.db 의 app_log 표)에도 남긴다.

기존 RotatingFileHandler(회전 텍스트 로그 - 장애 진단용, server.py/run.py 에 그대로 남아있다)와
별개로 동작한다. 화면의 실시간 로그(LogBuffer, daytrader/runner.py)는 최근 2000건만 메모리에
들고 있어서 그보다 오래된 로그는 화면에서 사라지는데, app_log 표는 보관 기간(기본 90일)까지
날짜·레벨로 다시, 페이지 단위로 조회할 수 있게 한다(db.py 상단 "왜 SQLite 인가" 참고).

★★★ 절대 매매를 막지 않는다 - db 쓰기가 실패해도(디스크 꽉 참·잠김 등) 이 핸들러는 예외를
삼키고 그 줄만 버린다(non-blocking, drop on failure). 로그를 남기려다 매매가 멈추면 본말전도다.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from daytrader import db

_ATTACHED: set[str] = set()
_ATTACH_LOCK = threading.Lock()

DEFAULT_LEVEL = logging.WARNING
DEFAULT_RETENTION_DAYS = 90


class DBLogHandler(logging.Handler):
    """WARNING 이상만 받는다 - INFO 이하(장중 폴링 등)까지 db 에 넣으면 표가 텍스트
    로그만큼 빨리 자라 db 를 쓰는 의미가 없어진다. 진단에 정말 필요한 것만 db 로 보낸다.
    """

    def __init__(self, state_dir: str, level: int = DEFAULT_LEVEL):
        super().__init__(level=level)
        self.state_dir = state_dir

    def emit(self, record: logging.LogRecord) -> None:
        try:
            created = datetime.fromtimestamp(record.created)
            conn = db.get_connection(self.state_dir)
            conn.execute(
                "INSERT INTO app_log (ts, date, time, level, source, text) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.created, created.strftime("%Y-%m-%d"), created.strftime("%H:%M:%S"),
                    record.levelname, record.name, record.getMessage(),
                ),
            )
        except Exception:
            # ★ 조용히 버린다 - 로그 저장 실패가 매매·요청 처리를 막으면 안 된다.
            pass


def attach(state_dir: str, level: int = DEFAULT_LEVEL) -> DBLogHandler | None:
    """루트 로거에 DB 핸들러를 state_dir 하나당 한 번만 붙인다.
    ★ 붙인 핸들러를 돌려준다 - 호출부(server.py)가 비밀정보 필터(_RedactFilter)를
    file_handler/log_buffer 와 똑같이 이 핸들러에도 걸 수 있게 하기 위해서다."""
    if not state_dir:
        return None
    key = db.db_path(state_dir)
    with _ATTACH_LOCK:
        if key in _ATTACHED:
            return None
        try:
            db.get_connection(state_dir)  # 표가 준비돼 있는지 먼저 확인(실패하면 핸들러를 안 붙인다)
        except Exception:
            logging.getLogger(__name__).warning("app_log DB 핸들러를 붙이지 못했습니다: %s", state_dir)
            return None
        handler = DBLogHandler(state_dir, level=level)
        logging.getLogger().addHandler(handler)
        _ATTACHED.add(key)
        return handler


def prune(state_dir: str, keep_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """keep_days 보다 오래된 app_log 행을 지운다. 지운 행 수를 돌려준다."""
    try:
        return db.prune_app_log(state_dir, keep_days=keep_days)
    except Exception:
        logging.getLogger(__name__).warning("app_log 정리에 실패했습니다: %s", state_dir)
        return 0


def query(state_dir: str, *, level: str | None = None, before_id: int | None = None, limit: int = 200) -> list:
    """UI 로그 화면의 "더 옛날 로그 보기"용 - id 기준 keyset 페이지네이션(최신순)."""
    conn = db.get_connection(state_dir)
    where = []
    params: list = []
    if level:
        where.append("level = ?")
        params.append(level)
    if before_id is not None:
        where.append("id < ?")
        params.append(before_id)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT id, ts, date, time, level, source, text FROM app_log {where_sql} "  # noqa: S608
        "ORDER BY id DESC LIMIT ?"
    )
    cur = conn.execute(sql, (*params, limit))
    return [dict(r) for r in cur.fetchall()]
