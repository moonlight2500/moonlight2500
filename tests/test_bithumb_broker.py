"""bithumb_broker.py 오프라인 테스트. `python tests/test_bithumb_broker.py` 로 실행한다."""

from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader.bithumb_broker import (  # noqa: E402
    CryptoPosition, CryptoPositionBook, LiveBithumbBroker, NotOwnedError, PaperBithumbBroker,
)
from daytrader.orders import OrderBook  # noqa: E402

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


class FakeBithumbClient:
    def __init__(self):
        self.sell_calls: list = []
        self.buy_calls: list = []

    def accounts(self):
        return [{"currency": "KRW", "balance": "500000"}, {"currency": "ETH", "balance": "1.5"}]

    def place_order(self, market, side, order_type, **kw):
        if side == "ask":
            self.sell_calls.append((market, kw))
        else:
            self.buy_calls.append((market, kw))
        return {"uuid": "fake-order-id"}


class FakeUncertainBithumbClient:
    """★★★ [1-5] network_error(응답을 못 받음) 타임아웃을 재현하는 가짜
    클라이언트 - fail_place=True 면 place_order() 가 예외를 던지지만 주문은
    이미 "접수된 것"으로 처리한다(daytrader/bithumb_api.py 의 _request() 가
    연결 자체가 실패했을 때 BithumbApiError(code="network_error") 를 던지는
    것과 같은 상황). findable=False 면 get_order() 조회로도 끝내 못 찾는
    상황(진짜로 접수 여부를 모르는 상태)을 재현한다.
    """

    def __init__(self, fail_place: bool = True, findable: bool = True):
        self.orders: list = []
        self.fail_place = fail_place
        self.findable = findable
        self._seq = 0

    def place_order(self, market, side, order_type, **kw):
        self._seq += 1
        order_id = f"uuid-{self._seq}"
        self.orders.append({"uuid": order_id, "client_order_id": kw.get("client_order_id"), "market": market, "side": side})
        if self.fail_place:
            from daytrader.bithumb_api import BithumbApiError
            raise BithumbApiError(None, "network_error", "연결 실패")
        return {"uuid": order_id}

    def get_order(self, order_id=None, client_order_id=None):
        if not self.findable:
            return None
        if client_order_id:
            return next((o for o in self.orders if o.get("client_order_id") == client_order_id), None)
        return next((o for o in self.orders if o.get("uuid") == order_id), None)


def test_paper_never_sells_preexisting() -> None:
    print("\n== PaperBithumbBroker - 기존 보유 코인 매도 금지 ==")
    broker = PaperBithumbBroker(starting_cash=1_000_000)

    check("장부에 없는 코인 확인", broker.book.get("KRW-XRP") is None)

    try:
        broker.sell("KRW-XRP", ref_price=800)
        check("★★★ 기존 보유 코인 매도 거부", False, "거부되지 않고 팔림 - 심각한 버그")
    except NotOwnedError:
        check("★★★ 기존 보유 코인 매도 거부", True)

    pos = broker.buy("KRW-BTC", krw_amount=100000, ref_price=95000000, technique="volatility_breakout")
    check("자동매매가 산 코인은 장부에 기록됨", pos is not None and broker.book.owns("KRW-BTC"))

    result = broker.sell("KRW-BTC", ref_price=100000000, reason="take_profit")
    check("자동매매가 산 코인은 정상 매도됨", result["pnl"] > 0)
    check("매도 후 장부에서 제거됨", not broker.book.owns("KRW-BTC"))

    try:
        broker.sell("KRW-BTC", ref_price=100000000)
        check("★이미 청산된 포지션 재매도 거부", False, "재매도가 허용됨 - 버그")
    except NotOwnedError:
        check("★이미 청산된 포지션 재매도 거부", True)


def test_live_never_sells_preexisting() -> None:
    print("\n== LiveBithumbBroker - 기존 보유 코인 매도 금지(실제 API 호출 여부까지 확인) ==")
    client = FakeBithumbClient()
    broker = LiveBithumbBroker(client, order_book=OrderBook(tempfile.mkdtemp()))

    # ★ 계좌엔 실제로 ETH 1.5개가 있지만(FakeBithumbClient.accounts 참고),
    # 이 브로커는 그것을 산 적이 없다 - 절대 팔면 안 된다.
    try:
        broker.sell("KRW-ETH", ref_price=3000000)
        check("★★★ 실거래 브로커도 기존 보유 코인 매도 거부", False, "거부되지 않음 - 심각한 버그")
    except NotOwnedError:
        check("★★★ 실거래 브로커도 기존 보유 코인 매도 거부", True)

    check("매도 주문 API가 실제로 호출된 적 없음", len(client.sell_calls) == 0)

    broker.buy("KRW-BTC", krw_amount=50000, ref_price=95000000, technique="ma_cross")
    check("매수 주문 API 호출됨", len(client.buy_calls) == 1)

    broker.sell("KRW-BTC", ref_price=96000000, reason="take_profit")
    check("직접 산 코인은 매도 주문 API 호출됨", len(client.sell_calls) == 1)
    check("매도 요청 market 이 정확함", client.sell_calls[0][0] == "KRW-BTC")


def test_position_book() -> None:
    print("\n== CryptoPositionBook ==")
    book = CryptoPositionBook()
    check("빈 장부는 아무것도 소유하지 않음", not book.owns("KRW-BTC"))

    pos = CryptoPosition(market="KRW-BTC", quantity=0.01, entry_price=90000000,
                          entry_time=time.time(), peak_price=90000000)
    book.record_buy(pos)
    check("기록 후 소유함", book.owns("KRW-BTC"))

    book.update_peak("KRW-BTC", 95000000)
    check("고점 갱신됨", book.get("KRW-BTC").peak_price == 95000000)

    book.update_peak("KRW-BTC", 92000000)  # 고점보다 낮으면 갱신 안 됨
    check("고점보다 낮은 값은 갱신 안 됨", book.get("KRW-BTC").peak_price == 95000000)

    book.remove("KRW-BTC")
    check("제거 후 소유 안 함", not book.owns("KRW-BTC"))


def test_live_buy_no_double_order_on_timeout() -> None:
    """★★★ [1-5] 실제로 겪을 뻔한 버그 - LiveBithumbBroker.buy()/sell() 이
    client.place_order() 를 클라이언트 아이디 없이 직접 불러서, 타임아웃
    뒤 다음 스캔에서 같은 코인을 재매수해 2배로 살 수 있었다. 국내주식과
    같은 설계(주문 의도 선기록 + client_order_id + 재전송 대신 조회)로 막는다.
    """
    print("\n== ★★★ [1-5] 빗썸 실거래 - 타임아웃 뒤 재전송하지 않고 조회로 확인 ==")
    import daytrader.bithumb_broker as broker_mod
    from daytrader.orders import OrderUncertainError

    orig_sleep = broker_mod.time.sleep
    broker_mod.time.sleep = lambda s: None  # 조회 재시도 대기를 없애 테스트를 빠르게.
    try:
        # ★ 조회하면 기존 주문을 찾는 경우 - 매수가 정상적으로 인계되어야 한다.
        client = FakeUncertainBithumbClient(fail_place=True, findable=True)
        book = CryptoPositionBook()
        broker = LiveBithumbBroker(client, book=book, order_book=OrderBook(tempfile.mkdtemp()))

        pos = broker.buy("KRW-BTC", krw_amount=50000, ref_price=95000000, technique="ma_cross")
        check("★★이중 주문 없음(실제 주문 1건만 나감)", len(client.orders) == 1, len(client.orders))
        check("조회로 기존 주문을 찾아 매수가 정상 인계됨", pos is not None and book.owns("KRW-BTC"))

        # ★ 조회해도 끝내 못 찾는 경우 - 재전송하지 않고 halt 되어야 한다.
        client2 = FakeUncertainBithumbClient(fail_place=True, findable=False)
        book2 = CryptoPositionBook()
        broker2 = LiveBithumbBroker(client2, book=book2, order_book=OrderBook(tempfile.mkdtemp()))

        try:
            broker2.buy("KRW-BTC", krw_amount=50000, ref_price=95000000, technique="ma_cross")
            check("★조회로도 확인 못 하면 OrderUncertainError 로 멈춤", False, "예외 없이 통과됨 - 심각한 버그")
        except OrderUncertainError:
            check("★조회로도 확인 못 하면 OrderUncertainError 로 멈춤", True)
        check("확인 못 하면 포지션이 기록되지 않음(다음 스캔에서 중복 매수 방지)", not book2.owns("KRW-BTC"))
        check("★★★ 브로커가 halt 됨 - 다음 스캔에서도 재시도하지 않음", broker2.halted, broker2.halt_reason)
        check("주문은 1건만 나감(재전송 없음)", len(client2.orders) == 1, len(client2.orders))

        # ★ order_book 을 안 넘기면 실거래를 아예 못 내야 한다.
        broker3 = LiveBithumbBroker(FakeUncertainBithumbClient(fail_place=False), book=CryptoPositionBook())
        try:
            broker3.buy("KRW-BTC", krw_amount=50000, ref_price=95000000)
            check("★order_book 없이는 실거래 자체를 거부함", False, "order_book 없이도 주문이 나감 - 심각한 버그")
        except RuntimeError:
            check("★order_book 없이는 실거래 자체를 거부함", True)
    finally:
        broker_mod.time.sleep = orig_sleep


def main() -> None:
    tests = [
        test_paper_never_sells_preexisting, test_live_never_sells_preexisting, test_position_book,
        test_live_buy_no_double_order_on_timeout,
    ]
    for t in tests:
        t()

    print(f"\n총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        print("실패한 검증:")
        for name in _failures:
            print(f"  - {name}")
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
