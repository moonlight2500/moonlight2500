"""스윙(며칠~몇 주 보유) 매매 브로커.

★★★ 핵심 안전 원칙은 국내 단타·해외주식·암호화폐와 완전히 동일하다: 이 엔진이
buy() 로 직접 사서 기록해 두지 않은 종목은 절대 매도하지 않는다. 계좌에 있는
종목이라도 이 엔진이 산 적 없으면 sell() 자체가 구조적으로 거부한다.

★ 계좌는 국내 단타와 완전히 같은 토스 계좌를 쓴다. 다만 이 엔진이 산 것과 국내
단타 엔진이 산 것을 절대 섞어 보지 않도록, 포지션 장부(SwingPositionBook)는
완전히 별도로 관리한다(해외주식이 OverseasPositionBook 을 따로 두는 것과 같다).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from daytrader import overseas_engine as _overseas_engine
from daytrader.sizing import split_quantity


class NotOwnedError(Exception):
    """이 엔진이 사지 않은 종목을 팔려고 할 때."""


def _fx_rate(market: str) -> float:
    """★★★ [1-11] 실제로 겪은 버그 - 스윙은 국내·해외·암호화폐 예산을 환산 없이
    하나의 원화 현금 풀로 섞어 쓰는데(SwingCfg.budget 설명 참고), 해외주식 가격은
    달러다. 원화 배정금액을 달러 주가로 그대로 나누면 수량이 환율 배수(약
    1,400배)만큼 부풀려진다(해외주식 엔진에서 먼저 겪은 것과 같은 버그).
    overseas_engine._usd_krw_rate() 를 그대로 재사용한다 - 환율 조회 실패 시
    보수적 기본값(1,400원)을 쓰는 안전장치도 그대로 물려받는다.
    ★ market 을 함수로 감싼 이유 - 테스트가 overseas_engine 모듈의
    _usd_krw_rate 를 통째로 바꿔치기해서 네트워크 호출 없이 검증할 수 있게 한다.
    """
    if market != "overseas":
        return 1.0
    return _overseas_engine._usd_krw_rate()


@dataclass
class SwingPosition:
    symbol: str
    quantity: float
    entry_price: float
    entry_time: float
    peak_price: float
    technique: str = ""
    order_id: str | None = None
    # ★ playbook.py 의 청산 판정이 pos.name/pos.theme 을 그대로 참조한다 -
    # 국내주식 Position 과 같은 필드셋을 갖춰야 Playbook 을 수정 없이 재사용할 수 있다.
    name: str = ""
    theme: str = "스윙"
    adds: int = 0
    last_fill_price: float = 0.0
    scaled_out: int = 0
    invested: float = 0.0
    realized: float = 0.0
    sold_qty: float = 0.0
    sold_value: float = 0.0
    conviction: float = 0.5
    # ★★★ "스윙매매 대상은 국내주식·해외주식·암호화폐 모두" - 한 장부(SwingPositionBook)에
    # 세 시장 포지션이 함께 담기므로, 어느 시장 종목인지(시세·주문 API 선택, 화면 통화 표시에
    # 쓴다) 포지션 자체에 남긴다.
    market: str = "domestic"


class SwingPositionBook:
    """★ 이 장부에 없는 종목은 이 엔진 소유가 아니다 - 매도 요청이 오면 무조건 거부한다."""

    def __init__(self):
        self._positions: dict[str, SwingPosition] = {}

    def owns(self, symbol: str) -> bool:
        return symbol in self._positions

    def get(self, symbol: str) -> SwingPosition | None:
        return self._positions.get(symbol)

    def all(self) -> dict:
        return dict(self._positions)

    def record_buy(self, pos: SwingPosition) -> None:
        self._positions[pos.symbol] = pos

    def record_sell(self, symbol: str) -> None:
        self._positions.pop(symbol, None)

    def update_peak(self, symbol: str, price: float) -> None:
        pos = self._positions.get(symbol)
        if pos is None or price is None:
            return
        if pos.peak_price is None or price > pos.peak_price:
            pos.peak_price = price


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN 제외


def _merge_buy(pos, new_qty: float, price: float, amount: float) -> None:
    total = (pos.quantity or 0.0) + new_qty
    if total > 0:
        pos.entry_price = ((pos.quantity or 0.0) * (pos.entry_price or 0.0) + new_qty * price) / total
    pos.quantity = total
    pos.invested = (pos.invested or 0.0) + amount
    pos.adds = (pos.adds or 0) + 1
    pos.last_fill_price = price


def _shrink(pos, sold_qty: float, held_qty: float, pnl: float) -> None:
    remain = held_qty - sold_qty
    if held_qty > 0:
        pos.invested = (pos.invested or 0.0) * (remain / held_qty)
    pos.quantity = remain
    pos.scaled_out = (pos.scaled_out or 0) + 1
    pos.realized = (pos.realized or 0.0) + pnl


class PaperSwingBroker:
    """모의매매 - 실제 주문을 절대 내지 않는다.

    ★★★ 국내·해외주식은 정수(주) 단위지만, 암호화폐는 소수점 수량을 산다 - market 인자로
    시장별 수량 단위를 구분한다("crypto"만 소수, 그 밖은 정수로 내림). 세 시장 예산을
    환산 없이 그대로 섞어 쓴다(SwingCfg.budget 설명 참고).
    """

    def __init__(self, starting_cash: float, book: SwingPositionBook | None = None, commission_pct: float = 0.0, tax_pct: float = 0.0):
        self.cash = starting_cash
        self.book = book or SwingPositionBook()
        self.commission_pct = commission_pct or 0.0
        self.tax_pct = tax_pct or 0.0

    def buy(self, symbol: str, name: str, amount: float, price: float, technique: str = "", market: str = "domestic") -> SwingPosition | None:
        price = _num(price)
        amount = _num(amount)
        cash = _num(self.cash) or 0.0
        if price is None or amount is None or price <= 0:
            return None
        # ★★★ [1-11] amount 는 원화 예산이지만 해외주식 price 는 달러다 - 환율로
        # 환산한 뒤 나눠야 한다(_fx_rate 설명 참고). 국내·암호화폐는 rate=1.0 이라
        # 기존과 동일하게 동작한다.
        rate = _fx_rate(market)
        local_amount = amount / rate
        quantity = (local_amount / price) if market == "crypto" else int(local_amount / price)
        if quantity <= 0 or (market != "crypto" and quantity < 1):
            return None
        # ★ 현금 풀은 항상 원화 기준이므로(SwingCfg.budget 설명 참고), 실제 비용도
        # 원화로 환산해 두고 그 기준으로 현금을 빼고 invested 를 기록한다 - sell() 에서
        # 같은 환산 기준으로 손익을 계산해야 서로 어긋나지 않는다.
        cost_local = quantity * price * (1 + self.commission_pct)
        cost = cost_local * rate
        if cost > cash:
            return None
        pos = SwingPosition(
            symbol=symbol, quantity=quantity, entry_price=price,
            entry_time=time.time(), peak_price=price, technique=technique,
            order_id=f"paper-{uuid.uuid4().hex[:10]}", name=name or symbol,
            last_fill_price=price, invested=cost, market=market,
        )
        self.cash = cash - cost
        self.book.record_buy(pos)
        return pos

    def add(self, symbol: str, amount: float, price: float) -> SwingPosition | None:
        pos = self.book.get(symbol)
        price, amount, cash = _num(price), _num(amount), _num(self.cash) or 0.0
        if pos is None or price is None or amount is None or price <= 0:
            return None
        market = getattr(pos, "market", "domestic")
        # ★ [1-11] buy() 와 같은 이유 - 원화 예산을 시장 통화로 환산한 뒤 나눈다.
        rate = _fx_rate(market)
        local_amount = amount / rate
        quantity = (local_amount / price) if market == "crypto" else int(local_amount / price)
        if quantity <= 0 or (market != "crypto" and quantity < 1):
            return None
        cost_local = quantity * price * (1 + self.commission_pct)
        cost = cost_local * rate
        if cost > cash:
            return None
        _merge_buy(pos, quantity, price, cost)
        self.cash = cash - cost
        return pos

    def sell(self, symbol: str, price: float, reason: str = "", fraction: float = 1.0) -> dict:
        pos = self.book.get(symbol)
        if pos is None:
            raise NotOwnedError(f"{symbol} 은(는) 이 엔진이 산 적 없는 종목입니다 - 매도를 거부합니다.")
        market = getattr(pos, "market", "domestic")
        integer_qty = market != "crypto"
        price = _num(price) or 0.0
        held = _num(pos.quantity) or 0.0
        entry = _num(pos.entry_price) or 0.0
        qty = held if fraction >= 1.0 else split_quantity(held, fraction, integer=integer_qty)
        if qty >= held:
            qty = held
        partial = qty < held
        # ★★★ [1-11] entry_price·price 는 시장 통화 그대로다(해외주식은 달러) -
        # buy()/add() 가 현금 풀(원화)에 반영한 것과 같은 환율로 환산해야
        # invested·pnl 이 같은 기준으로 맞는다. rate=1.0(국내·암호화폐)이면
        # 기존과 동일하게 동작한다.
        rate = _fx_rate(market)
        proceeds = qty * price * (1 - self.commission_pct - self.tax_pct) * rate
        # ★★★ 실제로 겪은 버그(작지만 실재함) - buy() 는 pos.invested 에 매수 수수료를 포함해
        # 기록하는데(cost = quantity*price*(1+commission_pct)), 여기 cost 는 수수료 없이
        # quantity*entry 로만 계산해서 둘이 어긋났다. 이 pnl 의 cost 기준과 재시작 시 현금을
        # 복원하는 공식(swing_engine.py 의 "locked = invested - sold_value")이 서로 다른
        # 기준을 쓰면, 보유 중 재시작은 맞다가도 매도 직후 재시작하면 몇 백 원 단위로 계속
        # 어긋난다. invested 와 같은 기준(수수료 포함·원화 환산)으로 맞춘다.
        cost = qty * entry * (1 + self.commission_pct) * rate
        pnl = proceeds - cost
        self.cash = (_num(self.cash) or 0.0) + proceeds
        if partial:
            _shrink(pos, qty, held, pnl)
        else:
            self.book.record_sell(symbol)
        return {"quantity": qty, "pnl": pnl, "proceeds": proceeds, "partial": partial, "remaining": pos.quantity if partial else 0.0,
                "total_pnl": pnl + (pos.realized if not partial else 0.0)}


class LiveSwingBroker:
    """★★★ 실거래 - 국내 단타와 완전히 같은 토스 계좌·API 를 그대로 쓴다.
    다만 매도 대상 확인은 이 엔진의 장부(SwingPositionBook)만 본다 - 계좌에
    실제로 있어도 이 장부에 없으면 거부한다.

    ★ 아직 재시작 시 계좌 잔고와 대조해 복구하는 reconcile 은 없다(암호화폐·
    해외주식엔 있다) - 스윙은 며칠~몇 주 보유라 그 사이 프로그램이 오래
    꺼져 있으면 로컬 기록과 실제 계좌가 어긋날 위험이 더 크다. 그래서 아직은
    web(관찰)·paper(모의매매)로 충분히 검증한 뒤에만 live 로 켜기를 권한다.
    """

    def __init__(self, client, book: SwingPositionBook | None = None):
        self.client = client
        self.book = book or SwingPositionBook()

    def cash(self) -> float:
        try:
            bp = self.client.buying_power()
            return float(bp.get("cash") or bp.get("buyingPower") or 0)
        except Exception:
            return 0.0

    def buy(self, symbol: str, name: str, amount: float, price: float, technique: str = "") -> SwingPosition | None:
        price = _num(price)
        amount = _num(amount)
        if price is None or amount is None or price <= 0:
            return None
        quantity = int(amount / price)
        if quantity < 1:
            return None
        result = self.client.create_order(symbol, "BUY", "MARKET", quantity)
        order_id = result.get("orderId") if isinstance(result, dict) else None
        pos = SwingPosition(
            symbol=symbol, quantity=quantity, entry_price=price,
            entry_time=time.time(), peak_price=price, technique=technique, order_id=order_id, name=name or symbol,
            last_fill_price=price, invested=quantity * price,
        )
        self.book.record_buy(pos)
        return pos

    def add(self, symbol: str, amount: float, price: float) -> SwingPosition | None:
        pos = self.book.get(symbol)
        price, amount = _num(price), _num(amount)
        if pos is None or price is None or amount is None or price <= 0:
            return None
        quantity = int(amount / price)
        if quantity < 1:
            return None
        self.client.create_order(symbol, "BUY", "MARKET", quantity)
        _merge_buy(pos, quantity, price, quantity * price)
        return pos

    def sell(self, symbol: str, price: float, reason: str = "", fraction: float = 1.0) -> dict:
        pos = self.book.get(symbol)
        if pos is None:
            raise NotOwnedError(f"{symbol} 은(는) 이 엔진이 산 적 없는 종목입니다 - 매도를 거부합니다.")
        held = _num(pos.quantity) or 0.0
        qty = held if fraction >= 1.0 else split_quantity(held, fraction, integer=True)
        if qty >= held:
            qty = held
        partial = qty < held
        self.client.create_order(symbol, "SELL", "MARKET", int(qty))
        price = _num(price) or 0.0
        entry = _num(pos.entry_price) or 0.0
        proceeds = qty * price
        cost = qty * entry
        pnl = proceeds - cost
        if partial:
            _shrink(pos, qty, held, pnl)
        else:
            self.book.record_sell(symbol)
        return {"quantity": qty, "pnl": pnl, "proceeds": proceeds, "partial": partial, "remaining": pos.quantity if partial else 0.0,
                "total_pnl": pnl + (pos.realized if not partial else 0.0)}
