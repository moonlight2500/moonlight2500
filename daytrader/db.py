"""매매기록·일지·주문의도·로그를 담는 SQLite 저장소.

★★★ 왜 SQLite 인가(사용자 요청 - "무료 sql db 로 구현해서 로그가 쌓여도 빠르게") - 이
프로그램은 서버 하나 없이 exe 하나로 배포된다(paths.py 의 exe 배포 원칙). Postgres·MySQL
같은 서버형 DB 는 별도 프로세스·설치·포트가 필요해 그 원칙과 정면으로 부딪힌다. SQLite 는
파이썬 표준 라이브러리(sqlite3)에 포함돼 있어 새 의존성이 전혀 없고, 파일 하나
(state\\daytrader.db)라 build_exe.bat 의 기존 state\\ 백업에 자동으로 딸려 들어가며,
WAL(Write-Ahead Log) 모드를 쓰면 쓰는 동안에도 대시보드(읽기 스레드)가 막히지 않는다.
JSONL 은 "읽을 때마다 파일을 통째로 열어 한 줄씩 파싱"하는 구조라 쌓일수록 선형으로
느려지지만(실제로 decisions.jsonl 이 1.8MB까지 자랐다), SQLite 는 인덱스를 걸면 수십만
줄이 쌓여도 필요한 몇 줄만 밀리초 단위로 찾는다.

★ 설계 원칙
- state_dir 하나당 daytrader.db 파일 하나. 여러 state_dir(실험실 참가자마다 따로 - lab.py)이면
  각자 자기 db 파일을 따로 가진다 - 실제 매매와 절대 안 섞인다(원래 JSONL 구조와 같은 격리).
- 연결은 스레드마다 하나씩(threading.local) - sqlite3 연결은 만든 스레드에서만 쓰는 게 안전하다.
- WAL + synchronous=NORMAL 이 기본. 다만 "이중 주문 방지"의 마지막 안전망인 order_intents 는
  주문을 보내기 전에 반드시 디스크에 내려가 있어야 하므로, 그 표에 쓸 때만 synchronous=FULL 로
  올려 fsync 까지 확인한 뒤 되돌린다(durable_write 참고).
- 모든 표는 원본 레코드 전체를 JSON 그대로 담는 data 컬럼을 갖는다 - 스키마를 조금씩 넓혀가며
  옮기는 중에도 어떤 필드도 잃지 않는다. 검색·정렬에 쓰는 필드만 타입이 있는 컬럼으로 따로 뽑는다.
- SQL 문자열에 값을 직접 이어붙이지 않는다 - 표·컬럼 이름은 이 파일 안의 고정 상수뿐이고,
  사용자·시세 데이터는 전부 ? 자리표시자로 넘긴다.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

DB_FILENAME = "daytrader.db"

# ── 연결 관리 ──────────────────────────────────────────────────────────

_LOCAL = threading.local()
_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()  # 스키마·마이그레이션·1회성 가져오기를 이미 끝낸 db 파일 경로


def db_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(state_dir), DB_FILENAME)


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def get_connection(state_dir: str) -> sqlite3.Connection:
    """이 스레드에서 이 state_dir 의 db 에 쓸 연결을 돌려준다(없으면 만든다).

    ★ isolation_level=None(오토커밋) 으로 열어 트랜잭션 경계를 이 파일이 직접 통제한다 -
    "주문 의도를 보내기 전에 반드시 커밋돼 있어야 한다"는 요구를 정확히 지키기 위해서다.
    """
    path = db_path(state_dir)
    conns = getattr(_LOCAL, "conns", None)
    if conns is None:
        conns = {}
        _LOCAL.conns = conns
    conn = conns.get(path)
    if conn is None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        conns[path] = conn
    _ensure_ready(path, conn, state_dir)
    return conn


def close_all() -> None:
    """이 스레드가 들고 있는 모든 연결을 닫는다(테스트 정리용)."""
    conns = getattr(_LOCAL, "conns", None)
    if not conns:
        return
    for conn in conns.values():
        try:
            conn.close()
        except Exception:
            pass
    _LOCAL.conns = {}


def forget(state_dir: str) -> None:
    """테스트에서 같은 경로를 새로 초기화하고 싶을 때 초기화 여부 기록을 지운다."""
    path = db_path(state_dir)
    with _INIT_LOCK:
        _INITIALIZED.discard(path)


# ── 스키마 ─────────────────────────────────────────────────────────────

# (버전, [DDL...]) - 버전이 schema_meta 에 남은 값보다 크면 순서대로 적용한다.
# 새 마이그레이션은 항상 리스트 끝에 더한다 - 이미 배포된 버전의 SQL 은 절대 고치지 않는다
# (이미 그 버전을 적용한 사용자의 db 와 새로 만드는 db 가 똑같은 순서를 거치게 하기 위해서다).
SCHEMA_MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [
        """CREATE TABLE IF NOT EXISTS schema_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS import_status (
            source_key TEXT PRIMARY KEY,
            imported_at TEXT NOT NULL,
            rows INTEGER NOT NULL,
            skipped INTEGER NOT NULL DEFAULT 0
        )""",
        # ── 매매기록 ──
        """CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            date TEXT NOT NULL,
            symbol TEXT,
            technique TEXT,
            exit_time TEXT,
            pnl REAL,
            data TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_trades_mode_date ON trades(mode, date)",
        "CREATE INDEX IF NOT EXISTS idx_trades_mode_exit_time ON trades(mode, exit_time)",
        "CREATE INDEX IF NOT EXISTS idx_trades_mode_technique ON trades(mode, technique)",
        """CREATE TABLE IF NOT EXISTS equity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            date TEXT NOT NULL,
            data TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_equity_mode_date ON equity(mode, date)",
        # ── 매매일지(journal / decisions) ──
        """CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            date TEXT NOT NULL,
            mode TEXT NOT NULL,
            kind TEXT NOT NULL,
            symbol TEXT,
            data TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_journal_date ON journal(date)",
        "CREATE INDEX IF NOT EXISTS idx_journal_mode_kind ON journal(mode, kind)",
        "CREATE INDEX IF NOT EXISTS idx_journal_at ON journal(at)",
        # ── 주문 의도(이중 주문 방지 안전망) ──
        """CREATE TABLE IF NOT EXISTS order_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book TEXT NOT NULL,
            coid TEXT NOT NULL,
            at TEXT NOT NULL,
            status TEXT NOT NULL,
            data TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_order_intents_book_coid ON order_intents(book, coid, id)",
        "CREATE INDEX IF NOT EXISTS idx_order_intents_book_status ON order_intents(book, status)",
        # ── 기법 백테스트 실행 기록 ──
        """CREATE TABLE IF NOT EXISTS technique_backtest_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT,
            market TEXT NOT NULL,
            trigger_ TEXT,
            data TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_technique_backtest_log_market_at ON technique_backtest_log(market, at)",
        # ── 시장 평가(market_commentary) 발송 이력 ──
        """CREATE TABLE IF NOT EXISTS market_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market TEXT NOT NULL,
            sent_at REAL NOT NULL,
            text TEXT NOT NULL,
            data TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_market_reviews_market_sent ON market_reviews(market, sent_at)",
        # ── 복기 반영 이력(review.OptimizeHistory) ──
        """CREATE TABLE IF NOT EXISTS optimize_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT,
            at TEXT,
            data TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_optimize_history_entry_id ON optimize_history(entry_id)",
        "CREATE INDEX IF NOT EXISTS idx_optimize_history_at ON optimize_history(at)",
        # ── 계좌 대조(safety.reconcile) 기록 ──
        """CREATE TABLE IF NOT EXISTS reconcile_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            symbol TEXT,
            kind TEXT,
            data TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_reconcile_log_at ON reconcile_log(at)",
        # ── 뉴스가드 AI 호출 시각(하루 한도 유지용) ──
        """CREATE TABLE IF NOT EXISTS news_guard_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_news_guard_calls_ts ON news_guard_calls(ts)",
        # ── 애플리케이션 로그(WARNING 이상) - UI 로그 화면의 과거 조회용 ──
        """CREATE TABLE IF NOT EXISTS app_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            level TEXT NOT NULL,
            source TEXT,
            text TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_app_log_ts ON app_log(ts)",
        "CREATE INDEX IF NOT EXISTS idx_app_log_level_ts ON app_log(level, ts)",
    ]),
]


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
    current = row["version"] if row else 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        applied_any = False
        for version, statements in SCHEMA_MIGRATIONS:
            if version <= current:
                continue
            for stmt in statements:
                conn.execute(stmt)
            current = version
            applied_any = True
        if applied_any:
            if row is None:
                conn.execute("INSERT INTO schema_meta (id, version) VALUES (1, ?)", (current,))
            else:
                conn.execute("UPDATE schema_meta SET version = ? WHERE id = 1", (current,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _ensure_ready(path: str, conn: sqlite3.Connection, state_dir: str) -> None:
    if path in _INITIALIZED:
        return
    with _INIT_LOCK:
        if path in _INITIALIZED:
            return
        _migrate(conn)
        from daytrader import db_import  # 순환 임포트를 피하려고 여기서 늦게 불러온다.
        db_import.import_legacy_files(conn, state_dir)
        _INITIALIZED.add(path)


# ── 주문 의도 - 내구성 있는 쓰기(FULL) ────────────────────────────────

def durable_write(conn: sqlite3.Connection, sql: str, params: tuple) -> sqlite3.Cursor:
    """order_intents 처럼 "보내기 전에 반드시 디스크에 있어야" 하는 쓰기.
    synchronous=FULL 로 올려 커밋이 fsync 까지 확인하게 한 뒤 되돌린다(그 사이 이 연결의
    다른 쓰기까지 같이 튼튼해지는 것은 부작용일 뿐 해가 없다 - 주문 관련 쓰기는 드물다).
    """
    conn.execute("PRAGMA synchronous=FULL")
    try:
        return conn.execute(sql, params)
    finally:
        conn.execute("PRAGMA synchronous=NORMAL")


# ── 공통 헬퍼 ──────────────────────────────────────────────────────────

def insert_json_row(state_dir: str, table: str, typed: dict, full_row: dict, *, durable: bool = False) -> int:
    """typed 의 키를 컬럼으로, 전체 레코드를 data(JSON) 컬럼으로 한 줄 넣는다."""
    conn = get_connection(state_dir)
    cols = list(typed.keys()) + ["data"]
    placeholders = ", ".join("?" for _ in cols)
    values = list(typed.values()) + [json.dumps(full_row, ensure_ascii=False)]
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"  # table 은 고정 상수만 온다
    if durable:
        cur = durable_write(conn, sql, tuple(values))
    else:
        cur = conn.execute(sql, tuple(values))
    return cur.lastrowid


def load_data_rows(cursor_rows) -> list[dict]:
    """data 컬럼(JSON)을 파싱해 원본 레코드 리스트로 되돌린다. 깨진 줄은 건너뛴다."""
    out = []
    for r in cursor_rows:
        try:
            out.append(json.loads(r["data"]))
        except (TypeError, ValueError, KeyError):
            continue
    return out


# ── 유지보수 ───────────────────────────────────────────────────────────

def prune_app_log(state_dir: str, keep_days: int = 90) -> int:
    """오래된 애플리케이션 로그를 지운다. 지운 행 수를 돌려준다."""
    conn = get_connection(state_dir)
    cutoff = time.time() - keep_days * 86400
    cur = conn.execute("DELETE FROM app_log WHERE ts < ?", (cutoff,))
    return cur.rowcount if cur.rowcount is not None else 0


def vacuum_analyze(state_dir: str) -> None:
    """유휴 시(주 1회 정도)에 돌리는 정리 - VACUUM 은 다른 연결이 없을 때만 효과가 있다."""
    conn = get_connection(state_dir)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("ANALYZE")
        conn.execute("VACUUM")
    except sqlite3.OperationalError as exc:
        log.info("VACUUM 을 건너뜀(다른 연결이 db 를 쓰는 중일 수 있음): %s", exc)


def backup_to(state_dir: str, dest_path: str) -> None:
    """실행 중에도 안전한 복사본을 만든다(sqlite Online Backup API).
    ★ 프로그램이 켜져 있는 동안 daytrader.db 파일을 그냥 복사하면(cp, robocopy 등) WAL 이
    아직 합쳐지지 않은 최근 기록이 빠지거나 파일이 중간 상태로 찍힐 수 있다 - 이 함수나
    `python run.py db-backup` 를 대신 써야 한다.
    """
    conn = get_connection(state_dir)
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)) or ".", exist_ok=True)
    dest = sqlite3.connect(dest_path)
    try:
        conn.backup(dest)
    finally:
        dest.close()
