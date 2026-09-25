"""주문 전송 중 타임아웃이 나면 재시도할 수 없다. 첫 주문이 이미 접수됐을 수
있어서 재전송하면 2배 수량을 산다. 그래서 모든 주문은 보내기 전에 의도를
SQLite(daytrader.db)에 먼저 적고, 응답을 못 받으면 재전송 대신 조회로 확인한다.
확인도 안 되면 매매를 멈춘다 - 모르는 상태로 계속 사고팔지 않는다.

★★★ 왜 이 표만 synchronous=FULL 인가 - 이 표는 "이중 주문 방지"의 마지막 안전망이다.
의도를 적은 뒤 그게 디스크에(전원이 나가도 살아남게) 내려갔다는 확신 없이 주문을 보내면,
크래시 직후 재시작했을 때 "방금 주문을 보냈는지"를 영영 알 수 없어 재전송(이중 주문)하거나
누락(포지션을 잃어버림)할 위험이 있다. 나머지 표(매매기록·일지 등)는 WAL+NORMAL 로 충분히
빠르고 안전하지만, 이 표만은 db.durable_write() 로 fsync 까지 확인한다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from uuid import uuid4

from daytrader import db
from daytrader.timeutil import iso, now_kst

PENDING_STATES = ("intent", "sent", "unknown")

# ★ overseas_engine.py/crypto_engine.py 는 "국내주식 orders 와 절대 안 섞이게" 하려고
# OrderBook(os.path.join(cfg.state_dir, "overseas_orders"/"crypto_orders")) 형태로 부른다.
# 예전에는 그 하위 폴더 자체에 orders.jsonl 을 따로 뒀지만, db 는 state_dir 하나당 파일
# 하나(daytrader.db)만 두므로 이 폴더 이름을 book 구분자로만 쓰고 실제 db 는 부모(state_dir)
# 것을 그대로 연다 - 호출부(overseas_engine.py 등)는 한 글자도 안 고쳐도 된다.
_BOOK_DIR_MAP = {"overseas_orders": "overseas", "crypto_orders": "crypto"}


class OrderUncertainError(RuntimeError):
    """★★★ 주문을 보냈지만 접수 여부를 끝내 확인하지 못했을 때 던진다(해외주식·
    암호화폐 브로커도 이 모듈과 같은 설계를 따른다 - resolve_uncertain() 이
    None 을 돌려준 경우). 재전송하면 이중 주문, 그냥 넘어가면 포지션 기록
    누락(다음 스캔에서 또 산다)이 되니 둘 다 위험하다 - 호출한 브로커는
    이 예외를 받으면 그 시장 전체를 멈추고 사람에게 알려야 한다.
    """


@dataclass
class OrderIntent:
    coid: str
    at: str
    mode: str
    symbol: str
    name: str
    side: str
    order_type: str
    quantity: int
    price: float
    reason: str
    verdict_id: str | None
    status: str = "intent"
    order_id: str | None = None
    filled: int = 0
    avg_price: float = 0.0
    error: str = ""


def new_coid(side: str, symbol: str) -> str:
    """새 주문 식별자를 만든다.
    ★ 시간 기반 금지. 같은 초에 두 번 나가면 조회가 엉뚱한 주문을 집어온다.
    """
    return f"dt-{side[0].lower()}-{symbol}-{uuid4().hex[:10]}"[:36]


def is_ours(client_order_id: str) -> bool:
    """이 프로그램이 낸 주문인지 판별한다 - 남의 주문을 건드리지 않기 위해서다."""
    return bool(client_order_id) and client_order_id.startswith("dt-")


class OrderBook:
    """주문 의도와 그 결과를 order_intents 표(daytrader.db)에 append-only 로 남긴다.
    같은 coid 에 대한 최신 레코드가 그 주문의 현재 상태다.
    """

    def __init__(self, state_dir: str):
        norm = os.path.normpath(state_dir)
        base = os.path.basename(norm)
        if base in _BOOK_DIR_MAP:
            # ★ overseas_orders/crypto_orders 로 불리면 부모 폴더의 db 를 book 으로 구분해서 쓴다.
            self.state_dir = os.path.dirname(norm) or "."
            self.book = _BOOK_DIR_MAP[base]
        else:
            self.state_dir = state_dir
            self.book = "domestic"
        os.makedirs(self.state_dir, exist_ok=True)
        db.get_connection(self.state_dir)  # 스키마 준비 + 기존 orders.jsonl(3곳) 1회성 가져오기

    def record(self, intent: OrderIntent) -> None:
        self._append(intent)

    def update(self, coid: str, **fields) -> OrderIntent:
        cur = self.latest(coid)
        if cur is None:
            raise ValueError(f"알 수 없는 주문 coid 입니다: {coid}")
        data = asdict(cur)
        data.update(fields)
        data["at"] = iso(now_kst())
        updated = OrderIntent(**data)
        self._append(updated)
        return updated

    def latest(self, coid: str) -> OrderIntent | None:
        conn = db.get_connection(self.state_dir)
        row = conn.execute(
            "SELECT data FROM order_intents WHERE book = ? AND coid = ? ORDER BY id DESC LIMIT 1",
            (self.book, coid),
        ).fetchone()
        if row is None:
            return None
        return OrderIntent(**json.loads(row["data"]))

    def pending(self) -> list:
        conn = db.get_connection(self.state_dir)
        placeholders = ", ".join("?" for _ in PENDING_STATES)
        sql = (
            "SELECT t.data FROM order_intents t "
            "JOIN (SELECT coid, MAX(id) AS max_id FROM order_intents WHERE book = ? GROUP BY coid) m "
            "ON t.coid = m.coid AND t.id = m.max_id "
            f"WHERE t.book = ? AND t.status IN ({placeholders})"
        )
        cur = conn.execute(sql, (self.book, self.book, *PENDING_STATES))
        return [OrderIntent(**json.loads(r["data"])) for r in cur.fetchall()]

    def all(self, limit: int | None = None) -> list:
        """★ 생성 순서(먼저 만들어진 의도가 앞)로, 각 coid 의 최신 상태만 돌려준다.
        limit 을 주면 "가장 최근에 만들어진 limit 개"만(끝에서부터) - SQL LIMIT 으로
        전체를 다 읽지 않고 골라낸다."""
        conn = db.get_connection(self.state_dir)
        base_sql = (
            "SELECT t.data FROM order_intents t "
            "JOIN (SELECT coid, MIN(id) AS first_id, MAX(id) AS last_id "
            "      FROM order_intents WHERE book = ? GROUP BY coid) m "
            "ON t.coid = m.coid AND t.id = m.last_id "
            "WHERE t.book = ? "
        )
        if limit:
            cur = conn.execute(base_sql + "ORDER BY m.first_id DESC LIMIT ?", (self.book, self.book, limit))
            rows = list(reversed(cur.fetchall()))
        else:
            cur = conn.execute(base_sql + "ORDER BY m.first_id ASC", (self.book, self.book))
            rows = cur.fetchall()
        return [OrderIntent(**json.loads(r["data"])) for r in rows]

    def _append(self, intent: OrderIntent) -> None:
        row = asdict(intent)
        # ★★★ durable=True - 주문을 보내기 전에 이 줄이 디스크에(전원이 나가도) 있어야 한다.
        db.insert_json_row(self.state_dir, "order_intents", {
            "book": self.book, "coid": intent.coid, "at": intent.at, "status": intent.status,
        }, row, durable=True)


def resolve_uncertain(client, intent: OrderIntent, book: OrderBook, tries: int = 5, gap: float = 2.0) -> str | None:
    """★ 절대 재전송하지 않는다. get_orders() 에서 clientOrderId 를 찾는다.
    찾으면 order_id 를 기록하고 돌려준다. 못 찾으면 status="unknown" 으로 남기고
    None 을 돌려준다 - 매매를 멈추라는 신호다.

    ★★ status 는 API 필수 파라미터다(실제로 빠뜨려서 겪은 문제 - "요청 필드가
    올바르지 않습니다"). 주문이 아직 체결 안 됐으면 OPEN, 이미 체결·거부됐으면
    CLOSED 에 있으니 둘 다 확인해야 한다 - 하나만 보면 절반의 경우를 놓친다.
    """
    for _ in range(tries):
        row = None
        for status in ("OPEN", "CLOSED"):
            try:
                orders = client.get_orders(clientOrderId=intent.coid, status=status)
            except Exception:
                orders = None
            if orders:
                rows = orders if isinstance(orders, list) else orders.get("orders", [])
                row = next((r for r in rows if r.get("clientOrderId") == intent.coid), None)
                if row:
                    break
        if row:
            order_id = row.get("orderId") or row.get("id")
            book.update(intent.coid, status="sent", order_id=order_id)
            return order_id
        time.sleep(gap)

    book.update(intent.coid, status="unknown")
    return None
