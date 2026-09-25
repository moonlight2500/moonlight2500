"""기존 JSONL(·일부 JSON) 기록을 daytrader.db 로 1회성으로 옮긴다.

★★★ 멱등·재개 가능 설계 - 파일 하나를 옮기는 작업 전체를 SQLite 트랜잭션 하나로 묶는다
(모든 줄을 넣는 것 + import_status 에 "이 파일은 끝났다"를 적는 것이 한 트랜잭션). 그래서:
- 트랜잭션 도중 죽으면(정전·강제종료) 아무것도 커밋되지 않는다 - 원본 파일도 그대로 남아
  있으니 다음 시작 때 처음부터 다시 옮기면 된다(중복이 안 생긴다).
- 커밋은 됐는데 그 다음 "원본을 .migrated 로 이름 바꾸기"에서 죽으면, import_status 에 이미
  끝났다고 적혀 있어 다시 옮기지 않고, 이름 바꾸기만 다시 시도한다.
원본은 절대 지우지 않는다 - 옮긴 뒤 `<이름>.migrated` 로 이름만 바꿔서 사람이 예전 파일을
직접 들여다봐야 할 때를 위해 남겨 둔다.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3

log = logging.getLogger(__name__)


def _iter_jsonl(path: str) -> tuple[list[dict], int]:
    rows: list[dict] = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                skipped += 1
    return rows, skipped


def _already_imported(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT 1 FROM import_status WHERE source_key = ?", (key,)).fetchone()
    return row is not None


def _mark_imported(conn: sqlite3.Connection, key: str, rows: int, skipped: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO import_status (source_key, imported_at, rows, skipped) "
        "VALUES (?, datetime('now'), ?, ?)",
        (key, rows, skipped),
    )


def _rename_migrated(path: str) -> None:
    dest = path + ".migrated"
    if os.path.exists(dest):
        # ★ 이미 예전에 이름을 바꿔놨는데 원본 자리에 또 파일이 생긴 드문 경우 -
        # 데이터를 잃지 않으려고 그냥 두고 경고만 남긴다(자동으로 덮어쓰지 않는다).
        log.warning("이미 %s 가 있어 %s 의 이름을 바꾸지 않았습니다 - 손으로 확인해 주세요.", dest, path)
        return
    try:
        os.replace(path, dest)
    except OSError as exc:
        log.warning("가져오기를 마친 파일 이름을 바꾸지 못했습니다(%s): %s", path, exc)


def _import_jsonl_table(
    conn: sqlite3.Connection, state_dir: str, rel_path: str, table: str, typed_fn, extra_typed: dict | None = None,
) -> tuple[int, int]:
    key = rel_path.replace(os.sep, "/")
    path = os.path.join(state_dir, rel_path)
    if _already_imported(conn, key):
        if os.path.exists(path):
            _rename_migrated(path)
        return 0, 0
    if not os.path.exists(path):
        return 0, 0

    rows, skipped = _iter_jsonl(path)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            typed = typed_fn(row)
            if extra_typed:
                typed.update(extra_typed)
            cols = list(typed.keys()) + ["data"]
            placeholders = ", ".join("?" for _ in cols)
            values = list(typed.values()) + [json.dumps(row, ensure_ascii=False)]
            conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",  # table 은 고정 상수만 온다
                values,
            )
        _mark_imported(conn, key, len(rows), skipped)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    _rename_migrated(path)
    return len(rows), skipped


def _import_news_guard_calls(conn: sqlite3.Connection, state_dir: str) -> tuple[int, int]:
    key = "news_guard_ai_calls.json"
    path = os.path.join(state_dir, key)
    if _already_imported(conn, key):
        if os.path.exists(path):
            _rename_migrated(path)
        return 0, 0
    if not os.path.exists(path):
        return 0, 0

    skipped = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = []
        skipped += 1
    if not isinstance(data, list):
        data = []

    values: list[float] = []
    for t in data:
        try:
            values.append(float(t))
        except (TypeError, ValueError):
            skipped += 1

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany("INSERT INTO news_guard_calls (ts) VALUES (?)", [(v,) for v in values])
        _mark_imported(conn, key, len(values), skipped)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    _rename_migrated(path)
    return len(values), skipped


# ── 표별 typed 컬럼 추출 ─────────────────────────────────────────────────

def _typed_trade(row: dict) -> dict:
    return {
        "mode": row.get("mode") or "", "date": row.get("date") or "",
        "symbol": row.get("symbol"), "technique": row.get("technique"),
        "exit_time": row.get("exit_time"), "pnl": row.get("pnl"),
    }


def _typed_equity(row: dict) -> dict:
    return {"mode": row.get("mode") or "", "date": row.get("date") or ""}


def _typed_journal(row: dict) -> dict:
    return {
        "at": row.get("at") or "", "date": row.get("date") or "",
        "mode": row.get("mode") or "", "kind": row.get("kind") or "",
        "symbol": row.get("symbol"),
    }


def _typed_order_intent(row: dict) -> dict:
    return {"coid": row.get("coid") or "", "at": row.get("at") or "", "status": row.get("status") or "intent"}


def _typed_technique_backtest_log(row: dict) -> dict:
    return {"at": row.get("at"), "market": row.get("market") or "", "trigger_": row.get("trigger")}


def _typed_market_review(row: dict) -> dict:
    return {"market": row.get("market") or "", "sent_at": row.get("sent_at") or 0.0, "text": row.get("text") or ""}


def _typed_optimize_history(row: dict) -> dict:
    return {"entry_id": row.get("id"), "at": row.get("at")}


def _typed_reconcile(row: dict) -> dict:
    return {"at": row.get("at") or "", "symbol": row.get("symbol"), "kind": row.get("kind")}


# ── 진입점 ─────────────────────────────────────────────────────────────

def import_legacy_files(conn: sqlite3.Connection, state_dir: str) -> dict:
    """이 db 파일에 대해 프로세스당(그리고 db 파일당) 한 번, 스키마 마이그레이션 직후 불린다."""
    summary: dict[str, tuple[int, int]] = {}

    jobs = [
        ("거래 원장(ledger.jsonl)", "ledger.jsonl", "trades", _typed_trade, None),
        ("잔고 이력(equity.jsonl)", "equity.jsonl", "equity", _typed_equity, None),
        ("매매일지(decisions.jsonl)", "decisions.jsonl", "journal", _typed_journal, None),
        ("주문의도-국내(orders.jsonl)", "orders.jsonl", "order_intents", _typed_order_intent, {"book": "domestic"}),
        (
            "주문의도-해외(overseas_orders/orders.jsonl)", os.path.join("overseas_orders", "orders.jsonl"),
            "order_intents", _typed_order_intent, {"book": "overseas"},
        ),
        (
            "주문의도-암호화폐(crypto_orders/orders.jsonl)", os.path.join("crypto_orders", "orders.jsonl"),
            "order_intents", _typed_order_intent, {"book": "crypto"},
        ),
        (
            "기법 백테스트 기록(technique_backtest_log.jsonl)", "technique_backtest_log.jsonl",
            "technique_backtest_log", _typed_technique_backtest_log, None,
        ),
        ("시장 평가 이력(market_reviews.jsonl)", "market_reviews.jsonl", "market_reviews", _typed_market_review, None),
        (
            "복기 반영 이력(optimize_history.jsonl)", "optimize_history.jsonl",
            "optimize_history", _typed_optimize_history, None,
        ),
        ("계좌 대조 기록(reconcile.jsonl)", "reconcile.jsonl", "reconcile_log", _typed_reconcile, None),
    ]

    for label, rel_path, table, typed_fn, extra in jobs:
        try:
            imported, skipped = _import_jsonl_table(conn, state_dir, rel_path, table, typed_fn, extra)
        except Exception:
            log.exception("%s 가져오기 중 오류가 발생했습니다 - 원본은 그대로 두고 다음 실행 때 다시 시도합니다.", label)
            continue
        if imported or skipped:
            summary[label] = (imported, skipped)

    try:
        imported, skipped = _import_news_guard_calls(conn, state_dir)
        if imported or skipped:
            summary["뉴스가드 AI 호출 기록(news_guard_ai_calls.json)"] = (imported, skipped)
    except Exception:
        log.exception("뉴스가드 호출 기록 가져오기 중 오류가 발생했습니다.")

    if summary:
        lines = [
            f"  - {label}: {imp}건 가져옴" + (f" ({skip}건은 손상되어 건너뜀)" if skip else "")
            for label, (imp, skip) in summary.items()
        ]
        log.info("기존 JSONL 기록을 SQLite(daytrader.db)로 옮겼습니다.\n%s", "\n".join(lines))
    return summary
