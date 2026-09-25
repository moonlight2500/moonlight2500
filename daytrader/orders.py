"""주문 전송 중 타임아웃이 나면 재시도할 수 없다. 첫 주문이 이미 접수됐을 수
있어서 재전송하면 2배 수량을 산다. 그래서 모든 주문은 보내기 전에 의도를
파일에 먼저 적고, 응답을 못 받으면 재전송 대신 조회로 확인한다.
확인도 안 되면 매매를 멈춘다 - 모르는 상태로 계속 사고팔지 않는다.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from uuid import uuid4

from daytrader.timeutil import iso, now_kst

PENDING_STATES = ("intent", "sent", "unknown")


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
    """주문 의도와 그 결과를 append-only JSONL 로 남긴다.
    같은 coid 에 대한 최신 레코드가 그 주문의 현재 상태다.
    """

    def __init__(self, state_dir: str):
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, "orders.jsonl")

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
        found = None
        for rec in self._read_all():
            if rec.get("coid") == coid:
                found = rec
        return OrderIntent(**found) if found else None

    def pending(self) -> list:
        latest_by_coid: dict[str, dict] = {}
        for rec in self._read_all():
            latest_by_coid[rec["coid"]] = rec
        return [OrderIntent(**r) for r in latest_by_coid.values() if r.get("status") in PENDING_STATES]

    def all(self, limit: int | None = None) -> list:
        order: list[str] = []
        latest_by_coid: dict[str, dict] = {}
        for rec in self._read_all():
            if rec["coid"] not in latest_by_coid:
                order.append(rec["coid"])
            latest_by_coid[rec["coid"]] = rec
        items = [OrderIntent(**latest_by_coid[c]) for c in order]
        if limit:
            items = items[-limit:]
        return items

    def _append(self, intent: OrderIntent) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(intent), ensure_ascii=False) + "\n")

    def _read_all(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # JSONL 읽기는 깨진 줄을 건너뛴다.


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
