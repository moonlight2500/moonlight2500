"""빗썸 코인 매매 브로커.

★★★ 자동매매가 사지 않은 코인은 절대 매도하지 않는다.
계좌에 그 코인이 있어도, 이 브로커의 sell() 은 자신이 buy() 로 사서 직접
장부(CryptoPositionBook)에 기록해 둔 것만 판다. 기록에 없으면 매도 자체를
구조적으로 거부한다(NotOwnedError) - "루프가 그것만 본다"는 약속에 기대지
않고, 매도 함수 자신이 직접 확인한다. 국내주식 엔진은 이 약속을
state.positions 딕셔너리 구조로 지키지만, 코인은 아직 자동 루프가 없는
상태에서 만들기 때문에 처음부터 더 강하게(매도 함수 자체가 거부하도록) 짠다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from daytrader.bithumb_api import BithumbApiError
from daytrader.orders import OrderIntent, OrderUncertainError, new_coid
from daytrader.timeutil import iso, now_kst


@dataclass
class CryptoPosition:
    market: str  # "KRW-BTC" 형식
    quantity: float
    entry_price: float
    entry_time: float
    peak_price: float
    technique: str = ""
    order_id: str | None = None
    # ★ 국내주식 Playbook.evaluate_exit(특히 ForceCloseExit)이 pos.name/
    # pos.theme 을 그대로 참조한다 - 암호화폐가 국내와 같은 Playbook 을
    # 쓰게 되면서 필요해졌다(해외주식에서 먼저 겪은 문제와 동일).
    name: str = ""
    theme: str = "암호화폐"
    # ★ 분할 매수·분할 매도 상태(sizing.py). 추가 매수 횟수·마지막 매수가·나눠 판 횟수·누적 투입금·이미 챙긴 손익.
    adds: int = 0
    last_fill_price: float = 0.0
    scaled_out: int = 0
    invested: float = 0.0
    realized: float = 0.0
    sold_qty: float = 0.0    # 나눠 판 수량 합계
    sold_value: float = 0.0  # 나눠 판 금액 합계(수량×가격)
    conviction: float = 0.5  # 진입 시점 테마 근거의 크기(0~1) - 추가 매수·분할 매도 비율 조절에 쓴다

    @property
    def symbol(self) -> str:
        # ★ 국내주식 Playbook(ForceCloseExit 등)이 pos.symbol 을 참조한다.
        # 코인은 원래부터 market("KRW-BTC" 형식)이라는 이름을 써 왔고
        # 이걸 바꾸면 코드 곳곳(브로커·상태 저장 등)을 다 고쳐야 하니,
        # market 을 그대로 내보내는 별칭 프로퍼티로 호환시킨다.
        return self.market


class NotOwnedError(RuntimeError):
    """★★★ 자동매매가 사지 않은 코인을 팔려고 할 때 던진다.
    이 예외를 잡아서 조용히 넘어가면 안 된다 - 발생했다는 것 자체가 어딘가
    로직이 잘못 짜여 매도 대상을 잘못 골랐다는 뜻이다. 반드시 로그로 남기고
    사람에게 알려야 한다.
    """


def _merge_buy(pos, new_qty: float, price: float, amount: float) -> None:
    """추가 매수를 포지션에 합친다 - 평균 매수가·수량·투입금·횟수·마지막 매수가."""
    total = (pos.quantity or 0.0) + new_qty
    if total > 0:
        pos.entry_price = ((pos.quantity or 0.0) * (pos.entry_price or 0.0) + new_qty * price) / total
    pos.quantity = total
    pos.invested = (pos.invested or 0.0) + amount
    pos.adds = (pos.adds or 0) + 1
    pos.last_fill_price = price


def _shrink(pos, sold_qty: float, held_qty: float, pnl: float) -> None:
    """부분 매도 뒤 포지션 정리 - 수량·투입금을 줄이고, 챙긴 손익과 분할 매도 횟수를 기록한다."""
    remain = held_qty - sold_qty
    if held_qty > 0:
        pos.invested = (pos.invested or 0.0) * (remain / held_qty)
    pos.quantity = remain
    pos.scaled_out = (pos.scaled_out or 0) + 1
    pos.realized = (pos.realized or 0.0) + pnl


class CryptoPositionBook:
    """★ 이 객체가 "자동매매가 산 것"의 유일한 출처다. 여기 없는 심볼은
    브로커의 sell() 이 구조적으로 거부한다 - 계좌에 코인이 있어도 상관없다.
    """

    def __init__(self):
        self._positions: dict = {}

    def record_buy(self, position: CryptoPosition) -> None:
        self._positions[position.market] = position

    def get(self, market: str):
        return self._positions.get(market)

    def remove(self, market: str) -> None:
        self._positions.pop(market, None)

    def owns(self, market: str) -> bool:
        return market in self._positions

    def all(self) -> dict:
        return dict(self._positions)

    def update_peak(self, market: str, price: float) -> None:
        """★★★ 실제로 겪은 버그("'>' not supported between instances of
        'NoneType' and 'int'") - price 나 peak_price 중 하나라도 None 이면
        이 비교에서 죽고, 청산 관리가 통째로 멈춘다(보유 종목을 못 파는
        건 손실로 직결된다). 값이 온전할 때만 비교한다.
        ★ peak_price 가 비어 있으면 지금 가격으로 채워 둔다 - 고점을
        모르면 트레일링 스탑 같은 기법이 판단할 근거가 없다.
        """
        pos = self._positions.get(market)
        if pos is None or price is None:
            return
        if pos.peak_price is None or price > pos.peak_price:
            pos.peak_price = price


# ━━ 모의 매매 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _num(v):
    """★★★ 해외주식에서 겪은 것과 같은 버그 방지 - 값이 None 인 채로
    비교식에 들어가면 "'>' not supported between instances of 'NoneType'
    and 'int'" 로 죽고 매매가 통째로 실패한다.
    ★ 숫자가 아니면 None - 호출부가 판단을 건너뛸 수 있어야 한다.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


class PaperBithumbBroker:
    """실제 주문 없이 가상으로 체결한다."""

    def __init__(self, starting_cash: float, book: CryptoPositionBook | None = None, commission_pct: float = 0.0):
        self._cash = starting_cash
        self.book = book or CryptoPositionBook()
        # ★★★ 실제로 겪은 문제 - 모의매매가 수수료를 아예 반영하지 않아서
        # 성과가 실제 빗썸 계좌보다 좋게 보였다(빗썸 수수료는 편도 약
        # 0.04%). cfg.crypto.commission_pct 를 엔진이 넘겨준다.
        self.commission_pct = commission_pct or 0.0

    def cash(self) -> float:
        return self._cash

    def buy(self, market: str, krw_amount: float, ref_price: float, *, technique: str = ""):
        # ★ 값이 None 이면 비교에서 죽는다 - 모르는 값으로는 사지 않는다.
        krw_amount = _num(krw_amount)
        ref_price = _num(ref_price)
        cash = _num(self._cash) or 0.0
        if krw_amount is None or ref_price is None:
            return None
        if krw_amount <= 0 or krw_amount > cash or ref_price <= 0:
            return None
        quantity = krw_amount / ref_price
        self._cash = cash - krw_amount
        pos = CryptoPosition(
            market=market, quantity=quantity, entry_price=ref_price,
            entry_time=time.time(), peak_price=ref_price, technique=technique, name=market,
            last_fill_price=ref_price, invested=krw_amount,
        )
        self.book.record_buy(pos)
        return pos

    def add(self, market: str, krw_amount: float, ref_price: float):
        """이미 산 코인에 추가 매수(피라미딩). 평균 매수가를 다시 계산한다. 못 사면 None."""
        pos = self.book.get(market)
        krw_amount, ref_price, cash = _num(krw_amount), _num(ref_price), _num(self._cash) or 0.0
        if pos is None or krw_amount is None or ref_price is None or krw_amount <= 0 or ref_price <= 0 or krw_amount > cash:
            return None
        self._cash = cash - krw_amount
        _merge_buy(pos, krw_amount / ref_price, ref_price, krw_amount)
        return pos

    def sell(self, market: str, ref_price: float, *, reason: str = "", fraction: float = 1.0) -> dict:
        """★★★ 핵심 안전장치. 이 확인이 없으면 이 모듈의 존재 이유가 없다."""
        pos = self.book.get(market)
        if pos is None:
            raise NotOwnedError(
                f"{market} 은 자동매매가 산 적이 없습니다 - 매도를 거부합니다. "
                "계좌에 있는 코인이라도 이 프로그램이 직접 사지 않은 것은 절대 팔지 않습니다."
            )
        # ★★★ 청산은 실패하면 안 된다 - 못 팔면 손실로 직결된다.
        ref_price = _num(ref_price) or 0.0
        held = _num(pos.quantity) or 0.0
        _qty = held if fraction >= 1.0 else held * max(0.0, min(1.0, fraction))
        partial = _qty < held
        proceeds = _qty * ref_price
        cost = _qty * pos.entry_price
        # ★★★ 실제로 겪은 문제 - 모의매매가 수수료를 아예 반영하지 않아서
        # 성과가 실제 빗썸 계좌보다 좋게 보였다(빗썸 수수료는 편도 약
        # 0.04%). 매수 시점의 현금 가용성 검사(krw_amount > cash)를 건드리지
        # 않도록, 왕복 수수료(매수+매도 명목가 기준)를 매도 시점에 한 번에
        # 반영한다.
        round_trip_fee = (proceeds + cost) * self.commission_pct
        net_proceeds = proceeds - round_trip_fee
        self._cash += net_proceeds
        pnl = net_proceeds - cost
        remaining = 0.0
        if partial:
            _shrink(pos, _qty, held, pnl)
            remaining = pos.quantity
        else:
            self.book.remove(market)
        return {
            "market": market, "quantity": _qty, "entry_price": pos.entry_price,
            "exit_price": ref_price, "pnl": pnl, "reason": reason, "partial": partial, "remaining": remaining,
            "total_pnl": pnl + (0.0 if partial else (pos.realized or 0.0)),
        }


# ━━ 실거래 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _resolve_uncertain_bithumb(client, intent: OrderIntent, order_book, tries: int = 5, gap: float = 2.0):
    """★★★ [1-5] 타임아웃 뒤 재전송 대신 조회로 접수 여부를 확인한다
    (daytrader/orders.py 의 resolve_uncertain() 과 같은 설계를 빗썸 API
    모양에 맞춰 다시 짰다). 절대 재전송하지 않는다 - 재전송하면 이미
    접수된 주문에 겹쳐 2배로 사고/판다.
    ★ get_order() 는 예외를 던지지 않고 실패하면 None 을 준다 - 여기서도
    방어적으로 다시 감싼다(모르는 응답 형태가 와도 안전한 쪽으로 넘어가게).
    """
    for _ in range(tries):
        try:
            row = client.get_order(client_order_id=intent.coid)
        except Exception:
            row = None
        if isinstance(row, dict):
            order_id = row.get("uuid") or row.get("order_id")
            if order_id:
                order_book.update(intent.coid, status="sent", order_id=order_id)
                return order_id
        time.sleep(gap)
    order_book.update(intent.coid, status="unknown")
    return None


class LiveBithumbBroker:
    """★★★ 실제 주문을 낸다. 같은 안전장치(book 에 없으면 거부)가 여기도
    그대로 적용된다 - 실거래라고 완화하지 않는다.
    """

    def __init__(self, client, book: CryptoPositionBook | None = None, order_book=None):
        self.client = client
        self.book = book or CryptoPositionBook()
        # ★★★ [1-5] 국내주식(daytrader/orders.py)과 같은 설계 - order_book
        # 없이는 이중 주문 방지 장치가 통째로 빠지므로, 실거래에서는 반드시
        # 엔진이 넘겨줘야 한다(_send 가 강제한다).
        self.order_book = order_book
        self.halted = False
        self.halt_reason = ""

    def cash(self) -> float:
        for a in self.client.accounts():
            if a.get("currency") == "KRW":
                return float(a.get("balance", 0))
        return 0.0

    def _send(self, side: str, market: str, *, price: str | None = None, volume: str | None = None,
              reason: str = "") -> dict:
        """의도를 먼저 기록한 뒤 보낸다. place_order() 가 network_error(응답을
        못 받은 경우)로 실패하면 재전송 대신 조회로 확인한다 - 확인도 안 되면
        이 브로커를 halt 시키고 OrderUncertainError 를 던진다.
        """
        if self.order_book is None:
            raise RuntimeError("LiveBithumbBroker 는 order_book 없이 실거래를 낼 수 없습니다.")
        side_kr = "BUY" if side == "bid" else "SELL"
        coid = new_coid(side_kr, market)
        intent = OrderIntent(
            coid=coid, at=iso(now_kst()), mode="live", symbol=market, name="",
            side=side_kr, order_type="MARKET",
            quantity=float(volume) if volume else 0.0, price=float(price) if price else 0.0,
            reason=reason, verdict_id=None,
        )
        self.order_book.record(intent)
        order_type = "price" if side == "bid" else "market"
        try:
            result = self.client.place_order(market, side, order_type, price=price, volume=volume,
                                              client_order_id=coid)
            order_id = result.get("uuid") if isinstance(result, dict) else None
            self.order_book.update(coid, status="sent", order_id=order_id)
            return result or {}
        except BithumbApiError as exc:
            if exc.code == "network_error":
                # ★ _request() 가 응답을 아예 못 받았을 때만 이 코드를 준다 - 4xx/5xx
                # 처럼 서버가 명확히 거부한 응답은 여기 안 걸린다(재전송 없이 그대로 실패 처리).
                order_id = _resolve_uncertain_bithumb(self.client, intent, self.order_book)
                if order_id is not None:
                    return {"uuid": order_id}
                # ★★★ 접수 여부를 끝내 확인하지 못했다 - 재전송하면 이중 주문,
                # 그냥 넘어가면 포지션 기록 누락(다음 스캔에서 또 산다). 둘 다
                # 위험하니 이 시장 전체를 멈추고 사람에게 알린다.
                self.halted = True
                self.halt_reason = (
                    f"{market} {side_kr} 주문 접수 여부를 확인하지 못했습니다 - "
                    "빗썸에서 직접 확인한 뒤 매매를 재개하세요."
                )
                raise OrderUncertainError(self.halt_reason) from exc
            self.order_book.update(coid, status="unknown", error=str(exc))
            raise

    def buy(self, market: str, krw_amount: float, ref_price: float, *, technique: str = ""):
        result = self._send("bid", market, price=str(krw_amount), reason=technique)
        # ★ 시장가 매수 응답의 실제 체결가·수량은 별도 조회가 필요하다 - 여기서는
        # 주문 접수 시점의 근사치로 기록해두고, 실제 운용 시 체결 조회로 갱신해야 한다.
        quantity = krw_amount / ref_price if ref_price > 0 else 0.0
        pos = CryptoPosition(
            market=market, quantity=quantity, entry_price=ref_price,
            entry_time=time.time(), peak_price=ref_price, technique=technique,
            order_id=result.get("uuid") or result.get("order_id"), name=market,
            last_fill_price=ref_price, invested=krw_amount,
        )
        self.book.record_buy(pos)
        return pos

    def add(self, market: str, krw_amount: float, ref_price: float):
        """이미 산 코인에 추가 매수(시장가). 평균 매수가를 다시 계산한다."""
        pos = self.book.get(market)
        if pos is None or not ref_price or ref_price <= 0 or not krw_amount or krw_amount <= 0:
            return None
        self._send("bid", market, price=str(krw_amount), reason="add")
        _merge_buy(pos, krw_amount / ref_price, ref_price, krw_amount)
        return pos

    def sell(self, market: str, ref_price: float, *, reason: str = "", fraction: float = 1.0) -> dict:
        """★★★ 핵심 안전장치. buy() 로 기록해 두지 않은 심볼은 절대 팔지 않는다."""
        pos = self.book.get(market)
        if pos is None:
            raise NotOwnedError(
                f"{market} 은 자동매매가 산 적이 없습니다 - 매도를 거부합니다. "
                "계좌에 있는 코인이라도 이 프로그램이 직접 사지 않은 것은 절대 팔지 않습니다."
            )
        held = float(pos.quantity or 0.0)
        qty = held if fraction >= 1.0 else held * max(0.0, min(1.0, fraction))
        partial = qty < held
        result = self._send("ask", market, volume=str(qty), reason=reason)
        pnl = qty * ((ref_price or 0.0) - (pos.entry_price or 0.0))
        remaining = 0.0
        if partial:
            _shrink(pos, qty, held, pnl)
            remaining = pos.quantity
        else:
            self.book.remove(market)
        return {"market": market, "quantity": qty, "order_result": result, "reason": reason, "partial": partial,
                "remaining": remaining, "pnl": pnl, "total_pnl": pnl + (0.0 if partial else (pos.realized or 0.0))}
