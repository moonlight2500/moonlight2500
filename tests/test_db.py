"""daytrader/db.py(SQLite 저장소) 검증 - 마이그레이션·기존 JSONL 가져오기(깨진 줄 포함)·
동시성·주문의도 크래시 안전성·페이지네이션·app_log 보관기간·성능(20만 행)을 다룬다.
`python tests/test_db.py` 로 실행한다."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader import db  # noqa: E402
from daytrader import db_import  # noqa: E402

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    mark = "OK  " if cond else "FAIL"
    line = f"[{mark}] {name}"
    if extra:
        line += f" - {extra}"
    print(line)
    if not cond:
        _failures.append(name)


def tmpdir() -> str:
    return tempfile.mkdtemp()


# ━━ 스키마·마이그레이션 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_schema() -> None:
    print("\n== 스키마·마이그레이션 ==")
    d = tmpdir()
    conn = db.get_connection(d)

    check("daytrader.db 파일이 생김", os.path.exists(db.db_path(d)))
    version = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()["version"]
    check("schema_meta 에 버전이 기록됨", version == db.SCHEMA_MIGRATIONS[-1][0], str(version))

    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    expected = {
        "trades", "equity", "journal", "order_intents", "technique_backtest_log",
        "market_reviews", "optimize_history", "reconcile_log", "news_guard_calls", "app_log",
        "import_status", "schema_meta",
    }
    check("필요한 표가 모두 만들어짐", expected <= tables, str(expected - tables))

    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    check("★WAL 모드", mode.lower() == "wal", mode)
    sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    check("기본 synchronous=NORMAL(1)", sync == 1, str(sync))

    # ★ 같은 db 파일을 다시 열어도(재시작 흉내) 멱등 - 다시 만들려다 에러 나지 않는다.
    db.close_all()
    conn2 = db.get_connection(d)
    version2 = conn2.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()["version"]
    check("재연결해도 버전이 그대로", version2 == version)


# ━━ 기존 JSONL 가져오기(깨진 줄 포함) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _write_lines(path: str, lines: list) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def section_import_jsonl() -> None:
    print("\n== 기존 JSONL 가져오기(마이그레이션) ==")
    d = tmpdir()

    _write_lines(os.path.join(d, "ledger.jsonl"), [
        json.dumps({"mode": "paper", "date": "2026-09-01", "symbol": "005930", "technique": "breakout",
                    "exit_time": "2026-09-01T10:00:00+09:00", "pnl": 1000}),
        "이건 json이 아님",  # ★ 깨진 줄
        json.dumps({"mode": "paper", "date": "2026-09-02", "symbol": "000660", "technique": "atr_stop",
                    "exit_time": "2026-09-02T10:00:00+09:00", "pnl": -500}),
        "",  # 빈 줄은 그냥 건너뛴다(깨진 것으로 세지 않는다)
    ])
    _write_lines(os.path.join(d, "decisions.jsonl"), [
        json.dumps({"at": "2026-09-01T09:00:00+09:00", "date": "2026-09-01", "time": "09:00:00",
                    "mode": "paper", "kind": "buy", "symbol": "005930", "name": "삼성전자",
                    "theme": "", "explain": "매수", "detail": {}}),
        "[1, 2, 3]",  # ★ JSON 이지만 dict 가 아님 - 이것도 건너뛴다
    ])
    _write_lines(os.path.join(d, "orders.jsonl"), [
        json.dumps({"coid": "dt-b-005930-aaaa", "at": "2026-09-01T09:00:00+09:00", "mode": "live",
                    "symbol": "005930", "name": "", "side": "BUY", "order_type": "LIMIT",
                    "quantity": 10, "price": 10000, "reason": "", "verdict_id": None,
                    "status": "intent", "order_id": None, "filled": 0, "avg_price": 0.0, "error": ""}),
    ])
    _write_lines(os.path.join(d, "news_guard_ai_calls.json"), [json.dumps([time.time(), time.time() - 10])])

    conn = db.get_connection(d)  # 이 시점에 전부 가져와진다

    trades = db.load_data_rows(conn.execute("SELECT data FROM trades ORDER BY id").fetchall())
    check("ledger.jsonl 2건 가져옴(깨진 줄 1건 제외)", len(trades) == 2, str(len(trades)))

    journal_rows = db.load_data_rows(conn.execute("SELECT data FROM journal").fetchall())
    check("decisions.jsonl 1건 가져옴(dict 아닌 줄 제외)", len(journal_rows) == 1, str(len(journal_rows)))

    orders = db.load_data_rows(conn.execute("SELECT data FROM order_intents WHERE book='domestic'").fetchall())
    check("orders.jsonl 1건 가져옴", len(orders) == 1, str(len(orders)))

    calls = conn.execute("SELECT COUNT(*) AS n FROM news_guard_calls").fetchone()["n"]
    check("news_guard_ai_calls.json 2건 가져옴", calls == 2, str(calls))

    status_rows = {r["source_key"]: r for r in conn.execute("SELECT * FROM import_status").fetchall()}
    check("import_status 에 ledger.jsonl 기록", status_rows.get("ledger.jsonl") is not None)
    check("★손상 1건이 skipped 로 남음", status_rows["ledger.jsonl"]["skipped"] == 1,
          str(status_rows["ledger.jsonl"]["skipped"]))

    check("원본은 지워지지 않고 .migrated 로 이름이 바뀜",
          os.path.exists(os.path.join(d, "ledger.jsonl.migrated"))
          and not os.path.exists(os.path.join(d, "ledger.jsonl")))

    # ★ 멱등성 - 가져오기를 다시 시켜도 두 번 들어가지 않는다.
    db_import.import_legacy_files(conn, d)
    trades_again = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
    check("★다시 가져와도 중복되지 않음(멱등)", trades_again == 2, str(trades_again))


def section_import_resumable_after_interrupt() -> None:
    """트랜잭션 도중 죽으면(끝까지 못 가면) 원본이 그대로 남아 다음 실행 때 처음부터 다시
    옮길 수 있어야 한다 - 여기서는 "롤백된 것처럼" import_status 없이 원본만 있는 상태를
    재현해 다시 불러도 정상적으로 끝까지 가져와지는지 본다."""
    print("\n== 가져오기 중단 후 재개 ==")
    d = tmpdir()
    _write_lines(os.path.join(d, "reconcile.jsonl"), [
        json.dumps({"at": "2026-09-01T09:00:00+09:00", "kind": "ghost", "symbol": "005930",
                    "name": "삼성전자", "state_qty": 10, "account_qty": 0, "detail": "", "action": "정리"}),
    ])
    conn = db.get_connection(d)
    rows = conn.execute("SELECT COUNT(*) AS n FROM reconcile_log").fetchone()["n"]
    check("정상적으로 1건 가져옴", rows == 1, str(rows))
    check("원본이 .migrated 로 남음", os.path.exists(os.path.join(d, "reconcile.jsonl.migrated")))

    # ★ 이름 바꾸기 전에 죽은 상황(커밋은 됐지만 rename 이 안 된 경우)을 흉내 - 원본 자리에
    # 파일을 다시 놔둬도 import_status 가 이미 있어 두 번 넣지 않고, 이름만 다시 바꿔 본다.
    _write_lines(os.path.join(d, "reconcile.jsonl"), [
        json.dumps({"at": "2026-09-02T09:00:00+09:00", "kind": "ghost", "symbol": "999999",
                    "name": "x", "state_qty": 1, "account_qty": 0, "detail": "", "action": "정리"}),
    ])
    db_import.import_legacy_files(conn, d)
    rows2 = conn.execute("SELECT COUNT(*) AS n FROM reconcile_log").fetchone()["n"]
    check("이미 끝난 소스는 다시 넣지 않음(원본이 다시 생겨도)", rows2 == 1, str(rows2))


# ━━ 모듈 API(Ledger/Journal/OrderBook)를 통한 가져오기 - 한글 요약 로그 ━━━━━━━━━━━

def section_import_via_modules_logs_korean_summary() -> None:
    print("\n== 모듈을 통한 가져오기 + 한글 요약 로그 ==")
    d = tmpdir()
    _write_lines(os.path.join(d, "ledger.jsonl"), [
        json.dumps({"mode": "paper", "date": "2026-09-01", "symbol": "005930", "technique": "breakout",
                    "exit_time": "2026-09-01T10:00:00+09:00", "pnl": 1000}),
    ])

    import logging
    records: list = []

    class _Collector(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Collector()
    logging.getLogger("daytrader.db_import").addHandler(handler)
    logging.getLogger("daytrader.db_import").setLevel(logging.INFO)
    try:
        from daytrader.ledger import Ledger
        lg = Ledger(d)
        check("모듈(Ledger) 을 통해서도 가져와짐", len(lg.trades()) == 1)
    finally:
        logging.getLogger("daytrader.db_import").removeHandler(handler)

    joined = "\n".join(records)
    check("가져오기 요약이 한글로 로그에 남음", "가져옴" in joined and "거래 원장" in joined, joined[:200])


# ━━ 동시성 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_concurrency() -> None:
    print("\n== 여러 스레드에서 동시에 쓰기 ==")
    from daytrader.journal import Journal

    d = tmpdir()
    j = Journal(d, mode="sim")
    n_threads = 8
    per_thread = 50
    errors: list = []

    def worker(idx: int) -> None:
        try:
            jj = Journal(d, mode="sim")  # ★ 스레드마다 자기 연결(threading.local) - 실제 엔진과 같은 패턴
            for i in range(per_thread):
                jj.write("watch", f"t{idx}-{i}", symbol=f"S{idx}")
        except Exception as exc:  # pragma: no cover - 실패하면 아래서 잡는다
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("★스레드 오류 없음", not errors, str(errors))
    rows = j.read(kinds=["watch"])
    check("★모든 기록이 유실 없이 남음", len(rows) == n_threads * per_thread, str(len(rows)))


# ━━ 주문의도 - 크래시 안전성 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_order_intent_crash_safety() -> None:
    print("\n== 주문의도 크래시 안전성 ==")
    from daytrader.orders import OrderBook, OrderIntent
    from daytrader.timeutil import iso, now_kst

    d = tmpdir()
    book = OrderBook(d)
    intent = OrderIntent(
        coid="dt-b-005930-crash1", at=iso(now_kst()), mode="live", symbol="005930", name="삼성전자",
        side="BUY", order_type="LIMIT", quantity=10, price=10000, reason="test", verdict_id=None,
    )
    # ★★★ "의도를 적고 나서 주문을 보내기 전에 죽었다"를 흉내낸다 - record() 뒤에는
    # 절대 아무것도 더 하지 않는다(주문 전송을 흉내내지 않음).
    book.record(intent)

    # ★★ "재시작"을 진짜로 흉내낸다 - 이 스레드가 들고 있던 연결을 닫아서, 다음 조회가
    # 메모리가 아니라 디스크에서 다시 읽게 만든다(synchronous=FULL 로 커밋됐는지 확인).
    db.close_all()

    book2 = OrderBook(d)
    recovered = book2.latest(intent.coid)
    check("★재시작 후에도 의도가 디스크에 남아있음", recovered is not None)
    check("상태가 intent 로 남아 복구 가능", recovered is not None and recovered.status == "intent")

    pending = book2.pending()
    check("pending() 에서 찾아짐(다음에 조회로 확인할 대상)", any(p.coid == intent.coid for p in pending))

    # ★ 이제 "재시작 후 조회로 확인해 order_id 를 인계받았다"를 흉내 - update() 도 durable.
    book2.update(intent.coid, status="sent", order_id="order-123")
    db.close_all()
    book3 = OrderBook(d)
    updated = book3.latest(intent.coid)
    check("update() 이후 상태도 재시작 후 그대로 남음", updated is not None and updated.status == "sent")
    check("update() 이후 pending() 에서 빠지지 않음(sent 도 pending 상태)",
          any(p.coid == intent.coid for p in book3.pending()))

    conn = db.get_connection(d)
    sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    check("주문의도를 다 쓴 뒤에는 synchronous 가 다시 NORMAL(1)로 돌아옴", sync == 1, str(sync))


def section_order_intents_separate_books() -> None:
    print("\n== 국내·해외·암호화폐 주문의도가 안 섞임 ==")
    from daytrader.orders import OrderBook, OrderIntent
    from daytrader.timeutil import iso, now_kst

    d = tmpdir()
    domestic = OrderBook(d)
    overseas = OrderBook(os.path.join(d, "overseas_orders"))
    crypto = OrderBook(os.path.join(d, "crypto_orders"))

    check("★같은 daytrader.db 파일 하나를 공유(overseas_orders/ 밑에 따로 안 생김)",
          os.path.exists(db.db_path(d))
          and not os.path.exists(os.path.join(d, "overseas_orders", "daytrader.db"))
          and not os.path.exists(os.path.join(d, "crypto_orders", "daytrader.db")))

    for book, tag in ((domestic, "dom"), (overseas, "ovs"), (crypto, "cry")):
        book.record(OrderIntent(
            coid=f"dt-b-{tag}-1", at=iso(now_kst()), mode="live", symbol="X", name="",
            side="BUY", order_type="LIMIT", quantity=1, price=1, reason="", verdict_id=None,
        ))

    check("국내 book 에는 국내 의도만", len(domestic.all()) == 1 and domestic.all()[0].coid == "dt-b-dom-1")
    check("해외 book 에는 해외 의도만", len(overseas.all()) == 1 and overseas.all()[0].coid == "dt-b-ovs-1")
    check("암호화폐 book 에는 암호화폐 의도만", len(crypto.all()) == 1 and crypto.all()[0].coid == "dt-b-cry-1")


# ━━ 페이지네이션 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_pagination() -> None:
    print("\n== 페이지네이션(LIMIT/keyset) ==")
    from daytrader.journal import Journal

    d = tmpdir()
    j = Journal(d, mode="sim")
    for i in range(50):
        j.write("session", f"이벤트 {i}", at=f"2026-09-01T00:{i:02d}:00+09:00")

    last10 = j.read(limit=10)
    check("limit=10 이면 정확히 10건", len(last10) == 10, str(len(last10)))
    check("★마지막 10건이 시간순으로 옴(가장 최근이 끝)", [r["explain"] for r in last10] == [f"이벤트 {i}" for i in range(40, 50)])

    from daytrader.orders import OrderBook, OrderIntent
    from daytrader.timeutil import iso, now_kst
    book = OrderBook(d)
    for i in range(20):
        book.record(OrderIntent(
            coid=f"dt-b-P{i}", at=iso(now_kst()), mode="live", symbol="X", name="",
            side="BUY", order_type="LIMIT", quantity=1, price=1, reason="", verdict_id=None,
        ))
    last5 = book.all(limit=5)
    check("OrderBook.all(limit=5) 도 정확히 5건, 생성순", [o.coid for o in last5] == [f"dt-b-P{i}" for i in range(15, 20)])


def section_applog_pagination_and_retention() -> None:
    print("\n== app_log 페이지네이션·보관기간 ==")
    from daytrader import applog

    d = tmpdir()
    conn = db.get_connection(d)
    now = time.time()
    # ★ 100일 전(보관기간 90일보다 오래된) 행을 맨 먼저(가장 과거) 넣는다.
    conn.execute(
        "INSERT INTO app_log (ts, date, time, level, source, text) VALUES (?, ?, ?, ?, ?, ?)",
        (now - 100 * 86400, "2026-05-01", "00:00:00", "ERROR", "test", "오래된 오류"),
    )
    # ★ id 순서(삽입 순서)가 시간 순서와 같게 - 메시지 0 이 가장 과거, 29 가 가장 최근.
    for i in range(30):
        ts = now - (29 - i)
        conn.execute(
            "INSERT INTO app_log (ts, date, time, level, source, text) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, "2026-09-01", "00:00:00", "WARNING", "test", f"메시지 {i}"),
        )

    page1 = applog.query(d, limit=10)
    check("최신순 10건", len(page1) == 10 and page1[0]["text"] == "메시지 29", page1[0]["text"])
    page2 = applog.query(d, before_id=page1[-1]["id"], limit=10)
    check("★before_id 로 다음 페이지(중복·누락 없음)", page2[0]["text"] == "메시지 19", page2[0]["text"])

    removed = applog.prune(d, keep_days=90)
    check("90일 넘은 1건만 정리됨", removed == 1, str(removed))
    remaining = conn.execute("SELECT COUNT(*) AS n FROM app_log").fetchone()["n"]
    check("나머지는 그대로 남음", remaining == 30, str(remaining))


def section_db_log_handler() -> None:
    print("\n== DBLogHandler(WARNING 이상만 DB 로) ==")
    import logging
    from daytrader import applog

    d = tmpdir()
    handler = applog.DBLogHandler(d)
    logger = logging.getLogger("daytrader.test_db_log_handler")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        logger.info("이건 안 남아야 한다")
        logger.warning("경고는 남는다")
        logger.error("오류도 남는다")
    finally:
        logger.removeHandler(handler)

    conn = db.get_connection(d)
    rows = conn.execute("SELECT level, text FROM app_log ORDER BY id").fetchall()
    check("WARNING 이상 2건만 저장(INFO 는 제외)", len(rows) == 2, str(len(rows)))
    check("내용이 정확히 담김", rows[0]["text"] == "경고는 남는다" and rows[1]["level"] == "ERROR")


def section_db_log_handler_does_not_raise_on_failure() -> None:
    print("\n== app_log 쓰기 실패해도 예외를 내지 않음(non-blocking) ==")
    import logging
    from daytrader import applog

    handler = applog.DBLogHandler("/이런/경로는/존재하지/않고/만들수도/없다/\0")
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "메시지", None, None)
    try:
        handler.emit(record)
        ok = True
    except Exception:
        ok = False
    check("★잘못된 경로여도 emit() 이 예외를 던지지 않음", ok)


# ━━ 성능(20만 행) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_performance() -> None:
    print("\n== 성능: 대량 데이터에서도 빠른가(20만 행) ==")
    from daytrader.journal import Journal
    from daytrader.ledger import Ledger

    d = tmpdir()
    conn = db.get_connection(d)
    N = 200_000

    # ── journal 20만 행을 트랜잭션 하나로 채운다(합성 데이터 생성 - 실제 매매 쓰기 패턴과는
    #    다르다. 실제 쓰기는 한 번에 한 줄씩이고, 그 성능은 아래 "실제 쓰기 패턴" 항목에서 잰다) ──
    t0 = time.time()
    rows = []
    for i in range(N):
        day = f"2026-{1 + (i // 9000) % 9:02d}-{1 + (i // 300) % 28:02d}"
        at = f"{day}T{(i % 24):02d}:{(i % 60):02d}:00+09:00"
        kind = "buy" if i % 7 == 0 else ("sell" if i % 11 == 0 else "watch")
        row = {"at": at, "date": day, "time": at[11:19], "mode": "live", "kind": kind,
               "symbol": f"{i % 500:06d}", "name": "", "theme": "", "explain": f"이벤트 {i}", "detail": {}}
        rows.append((at, day, "live", kind, row["symbol"], json.dumps(row, ensure_ascii=False)))
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT INTO journal (at, date, mode, kind, symbol, data) VALUES (?, ?, ?, ?, ?, ?)", rows,
    )
    conn.execute("COMMIT")
    insert_sec = time.time() - t0
    print(f"  journal {N:,}행 삽입: {insert_sec:.2f}초")
    check("20만 행 삽입이 30초 이내", insert_sec < 30.0, f"{insert_sec:.2f}초")

    j = Journal(d, mode="live")

    t0 = time.time()
    last200 = j.read(limit=200)
    dt = time.time() - t0
    print(f"  최근 로그 200줄 조회: {dt * 1000:.1f}ms")
    check("최근 200줄 조회가 200ms 이내", dt < 0.2, f"{dt * 1000:.1f}ms")
    check("실제로 200줄을 돌려줌", len(last200) == 200, str(len(last200)))

    # ── trades 5만 행(여러 기법·날짜) - "오늘 거래", "이번 달 기법별 통계" 조회용 ──
    lg = Ledger(d)
    t0 = time.time()
    trade_rows = []
    techniques = ["breakout", "vwap_pullback", "orb", "atr_stop", "time_stop"]
    for i in range(50_000):
        day = f"2026-09-{1 + (i % 28):02d}"
        trade_rows.append((
            "live", day, f"{i % 500:06d}", techniques[i % len(techniques)],
            f"{day}T{(i % 24):02d}:00:00+09:00", (i % 2000) - 1000,
            json.dumps({
                "mode": "live", "date": day, "symbol": f"{i % 500:06d}", "name": "", "theme": "",
                "qty": 1, "entry": 1000, "exit": 1010, "pnl": (i % 2000) - 1000, "reason": "",
                "entry_time": "", "exit_time": f"{day}T{(i % 24):02d}:00:00+09:00",
                "technique": techniques[i % len(techniques)], "entry_technique": "", "adds": 0,
                "scaled_out": 0, "verdict_id": None, "estimated": False,
            }, ensure_ascii=False),
        ))
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT INTO trades (mode, date, symbol, technique, exit_time, pnl, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
        trade_rows,
    )
    conn.execute("COMMIT")
    print(f"  trades 50,000행 삽입: {time.time() - t0:.2f}초")

    t0 = time.time()
    today_count = conn.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE mode = ? AND date = ?", ("live", "2026-09-15"),
    ).fetchone()["n"]
    dt = time.time() - t0
    print(f"  '오늘(2026-09-15)' 거래 건수 조회: {dt * 1000:.1f}ms ({today_count}건)")
    check("오늘 거래 조회가 50ms 이내(인덱스 사용)", dt < 0.05, f"{dt * 1000:.1f}ms")

    t0 = time.time()
    by_tech = lg.by_technique(modes=["live"])
    dt = time.time() - t0
    print(f"  기법별 통계(Ledger.by_technique, 5만 행 전체 집계): {dt * 1000:.1f}ms")
    # ★ by_technique() 는 Ledger 의 기존 설계 그대로 파이썬에서 집계한다(JSON 파싱 포함) -
    # 느린 기기에서도 여유 있게 통과하도록 넉넉히 잡는다. 아래 SQL GROUP BY 항목이
    # "한 달만" 볼 때 얼마나 더 빨라지는지(인덱스 활용) 보여준다.
    check("기법별 통계가 3초 이내", dt < 3.0, f"{dt * 1000:.1f}ms")
    check("5개 기법 모두 집계됨", set(by_tech.keys()) == set(techniques))

    t0 = time.time()
    month_rows = conn.execute(
        "SELECT technique, COUNT(*) AS n, SUM(pnl) AS pnl FROM trades "
        "WHERE mode = ? AND date BETWEEN ? AND ? GROUP BY technique",
        ("live", "2026-09-01", "2026-09-30"),
    ).fetchall()
    dt = time.time() - t0
    print(f"  이번 달 기법별 통계(SQL GROUP BY, 인덱스 활용): {dt * 1000:.1f}ms")
    check("SQL 기법별 월간 집계가 300ms 이내", dt < 0.3, f"{dt * 1000:.1f}ms")
    check("결과가 있음", len(month_rows) > 0)

    # ── 실제 쓰기 패턴 - 한 줄씩 append_trade()/write() (order_intents 와 달리 durable=False) ──
    t0 = time.time()
    for i in range(200):
        lg.append_trade("live", {
            "date": "2026-09-20", "symbol": "005930", "name": "삼성전자", "theme": "",
            "qty": 1, "entry": 1000, "exit": 1010, "pnl": 10, "reason": "",
            "entry_time": "", "exit_time": f"2026-09-20T10:{i % 60:02d}:00+09:00",
            "technique": "breakout", "verdict_id": None, "estimated": False,
        })
    dt = (time.time() - t0) / 200
    print(f"  실제 쓰기 패턴(append_trade 1건당 평균): {dt * 1000:.2f}ms")
    check("한 줄 쓰기가 평균 20ms 이내(WAL+NORMAL)", dt < 0.02, f"{dt * 1000:.2f}ms")


# ━━ 진입점 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main() -> None:
    sections = [
        section_schema, section_import_jsonl, section_import_resumable_after_interrupt,
        section_import_via_modules_logs_korean_summary,
        section_concurrency, section_order_intent_crash_safety, section_order_intents_separate_books,
        section_pagination, section_applog_pagination_and_retention,
        section_db_log_handler, section_db_log_handler_does_not_raise_on_failure,
        section_performance,
    ]
    for sec in sections:
        sec()

    print(f"\n총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        print("실패한 검증:")
        for name in _failures:
            print(f"  - {name}")
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
