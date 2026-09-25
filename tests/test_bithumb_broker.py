"""bithumb_broker.py 오프라인 테스트. `python tests/test_bithumb_broker.py` 로 실행한다."""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader.bithumb_broker import (  # noqa: E402
    CryptoPosition, CryptoPositionBook, LiveBithumbBroker, NotOwnedError, PaperBithumbBroker,
)

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
    broker = LiveBithumbBroker(client)

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


def main() -> None:
    tests = [test_paper_never_sells_preexisting, test_live_never_sells_preexisting, test_position_book]
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
