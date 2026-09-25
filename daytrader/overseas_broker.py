"""해외주식 브로커.

★★★ 핵심 안전 원칙은 국내주식·암호화폐와 완전히 동일하다: 이 엔진이
buy() 로 직접 사서 기록해 두지 않은 종목은 절대 매도하지 않는다. 계좌에
있는 종목이라도 이 엔진이 산 적 없으면 sell() 자체가 구조적으로 거부한다.

★ 계좌는 국내주식과 완전히 같은 토스 계좌를 쓴다(사용자 요청 - "계좌도
동일"). 다만 이 엔진이 산 것과 국내주식 엔진이 산 것을 절대 섞어 보지
않도록, 포지션 장부(OverseasPositionBook)는 완전히 별도로 관리한다.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from daytrader.sizing import split_quantity

# ★★★ "ETF는 수수료 이외에 운용비도 감안해야 한다" 요청(2026-09-24) - 개별 주식과 달리 ETF는
# 보유 기간 내내 매일 조금씩 떼가는 연간 운용보수(expense ratio)가 있다. 매매 수수료
# (commission_pct)는 사고팔 때 한 번씩이지만, 운용비는 "얼마나 오래 들고 있었나"에 비례해서
# 커진다 - 스윙처럼 며칠~몇 주 들고 가면 무시 못 할 크기가 된다. 알려진 ETF만 표에 두고
# (모르는 티커는 0 = 영향 없음, 개별 주식은 이 표에 없으니 그대로 0), 청산 시점에 보유일수
# 비례로 손익에서 뺀다.
ETF_ANNUAL_EXPENSE_RATIO = {
    "SPY": 0.0009,   # SPDR S&P 500 ETF Trust - 0.09%
    "SMH": 0.0035,   # VanEck Semiconductor ETF - 0.35%
    "SOXX": 0.0035,  # iShares Semiconductor ETF - 0.35%
}


def _etf_expense_cost(symbol: str, cost_basis: float, entry_time: float, now: float | None = None) -> float:
    """보유 기간에 비례한 ETF 운용비(달러 기준). 알려진 ETF가 아니면 0 - 개별 주식엔 영향 없다."""
    annual = ETF_ANNUAL_EXPENSE_RATIO.get((symbol or "").upper())
    if not annual or cost_basis <= 0 or not entry_time:
        return 0.0
    held_days = max(0.0, ((now or time.time()) - entry_time) / 86400.0)
    return cost_basis * annual * (held_days / 365.0)


class NotOwnedError(Exception):
    """이 엔진이 사지 않은 종목을 팔려고 할 때."""


@dataclass
class OverseasPosition:
    symbol: str
    quantity: float
    entry_price: float
    entry_time: float
    peak_price: float
    technique: str = ""
    order_id: str | None = None
    # ★ playbook.py 의 청산 판정(특히 ForceCloseExit)이 pos.name/pos.theme
    # 을 그대로 참조한다 - 국내주식 Position 과 같은 필드셋을 갖춰야
    # Playbook 을 수정 없이 그대로 재사용할 수 있다.
    name: str = ""
    theme: str = "해외주식"
    # ★ 분할 매수·분할 매도 상태(sizing.py). 추가 매수 횟수·마지막 매수가·나눠 판 횟수·누적 투입금·이미 챙긴 손익.
    adds: int = 0
    last_fill_price: float = 0.0
    scaled_out: int = 0
    invested: float = 0.0
    realized: float = 0.0
    sold_qty: float = 0.0    # 나눠 판 수량 합계
    sold_value: float = 0.0  # 나눠 판 금액 합계(수량×가격)
    conviction: float = 0.5  # 진입 시점 테마 근거의 크기(0~1) - 추가 매수·분할 매도 비율 조절에 쓴다


class OverseasPositionBook:
    """★ 이 장부에 없는 종목은 이 엔진 소유가 아니다 - 매도 요청이 오면 무조건 거부한다."""

    def __init__(self):
        self._positions: dict[str, OverseasPosition] = {}

    def owns(self, symbol: str) -> bool:
        return symbol in self._positions

    def get(self, symbol: str) -> OverseasPosition | None:
        return self._positions.get(symbol)

    def all(self) -> dict:
        return dict(self._positions)

    def record_buy(self, pos: OverseasPosition) -> None:
        self._positions[pos.symbol] = pos

    def record_sell(self, symbol: str) -> None:
        self._positions.pop(symbol, None)

    def update_peak(self, symbol: str, price: float) -> None:
        """★★★ 실제로 겪은 버그("'>' not supported between instances of
        'NoneType' and 'int'") - price 나 peak_price 중 하나라도 None 이면
        이 비교에서 죽고, 청산 관리가 통째로 멈춘다(보유 종목을 못 파는
        건 손실로 직결된다). 값이 온전할 때만 비교한다.
        ★ peak_price 가 비어 있으면 지금 가격으로 채워 둔다 - 고점을
        모르면 트레일링 스탑 같은 기법이 판단할 근거가 없다.
        """
        pos = self._positions.get(symbol)
        if pos is None or price is None:
            return
        if pos.peak_price is None or price > pos.peak_price:
            pos.peak_price = price


def _num(v):
    """★★★ 실제로 겪은 버그("'>' not supported between instances of
    'NoneType' and 'int'") - 시세나 금액이 None 인 채로 비교식에 들어가면
    그 자리에서 죽고, 매수·매도가 통째로 실패한다. 숫자로 정규화한다.
    ★ 숫자가 아니면 None 을 돌려준다 - 호출부가 "값이 없다"를 보고
    판단을 건너뛸 수 있어야 한다(0 으로 바꾸면 '공짜'로 오해한다).
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN 제외


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


class PaperOverseasBroker:
    """모의매매 - 실제 주문을 절대 내지 않는다. 현금은 원화로 관리한다(환율은 무시한 근사)."""

    def __init__(self, starting_cash: float, book: OverseasPositionBook | None = None, commission_pct: float = 0.0):
        self.cash = starting_cash
        self.book = book or OverseasPositionBook()
        # ★★★ 실제로 겪은 문제 - 모의매매에 수수료가 전혀 반영되지 않아서
        # 39건씩 회전하는 단타 성과가 실제보다 좋게 보였다. cfg.costs.
        # commission_pct 를 엔진이 넘겨준다(국내주식과 같은 값을 근사로 씀 -
        # 미국은 브로커 수수료는 없어도 스프레드가 있어 0으로 두는 것보다 낫다).
        self.commission_pct = commission_pct or 0.0

    def buy(self, symbol: str, krw_amount: float, price: float, technique: str = "") -> OverseasPosition | None:
        # ★★★ 값이 None 이면 비교에서 죽는다 - 먼저 숫자로 만들고,
        # 알 수 없으면 사지 않는다(모르는 값으로 주문을 내면 안 된다).
        price = _num(price)
        krw_amount = _num(krw_amount)
        cash = _num(self.cash) or 0.0
        if price is None or krw_amount is None:
            return None
        if price <= 0 or krw_amount > cash:
            return None
        quantity = krw_amount / price
        pos = OverseasPosition(
            symbol=symbol, quantity=quantity, entry_price=price,
            entry_time=time.time(), peak_price=price, technique=technique,
            order_id=f"paper-{uuid.uuid4().hex[:10]}", name=symbol,
            last_fill_price=price, invested=krw_amount,
        )
        self.cash = cash - krw_amount  # ★ 위에서 숫자로 정규화한 cash 를 쓴다.
        self.book.record_buy(pos)
        return pos

    def add(self, symbol: str, amount: float, price: float) -> OverseasPosition | None:
        """이미 산 종목에 추가 매수(피라미딩). 평균 매수가를 다시 계산한다. 못 사면 None."""
        pos = self.book.get(symbol)
        price, amount, cash = _num(price), _num(amount), _num(self.cash) or 0.0
        if pos is None or price is None or amount is None or price <= 0 or amount <= 0 or amount > cash:
            return None
        _merge_buy(pos, amount / price, price, amount)
        self.cash = cash - amount
        return pos

    def sell(self, symbol: str, price: float, reason: str = "", fraction: float = 1.0) -> dict:
        pos = self.book.get(symbol)
        if pos is None:
            # ★★★ 이 브로커가 사지 않은 종목은 절대 팔 수 없다.
            raise NotOwnedError(f"{symbol} 은(는) 이 엔진이 산 적 없는 종목입니다 - 매도를 거부합니다.")
        # ★★★ 청산은 절대 실패하면 안 된다 - 못 팔면 손실로 직결된다.
        # 값이 이상하면 0 으로 두고서라도 포지션은 정리한다(손익 계산이
        # 틀리는 것보다 종목을 못 파는 쪽이 훨씬 위험하다).
        price = _num(price) or 0.0
        held = _num(pos.quantity) or 0.0
        entry = _num(pos.entry_price) or 0.0
        qty = held if fraction >= 1.0 else split_quantity(held, fraction, integer=False)
        if qty >= held:
            qty = held
        partial = qty < held
        proceeds = qty * price
        cost = qty * entry
        # ★★★ 실제로 겪은 문제 - 모의매매에 수수료가 전혀 반영되지 않아서
        # 39건씩 회전하는 단타 성과가 실제보다 좋게 보였다. 매수 시점의
        # 현금 가용성 검사(krw_amount > cash)를 건드리지 않도록, 왕복
        # 수수료(매수+매도 명목가 기준)를 매도 시점에 한 번에 반영한다.
        round_trip_fee = (proceeds + cost) * self.commission_pct
        expense_cost = _etf_expense_cost(symbol, cost, pos.entry_time)
        net_proceeds = proceeds - round_trip_fee - expense_cost
        pnl = net_proceeds - cost
        # ★ cash 가 None 이면 += 에서도 죽는다 - 숫자로 만들어 두고 더한다.
        self.cash = (_num(self.cash) or 0.0) + net_proceeds
        if partial:
            _shrink(pos, qty, held, pnl)
        else:
            self.book.record_sell(symbol)
        return {"quantity": qty, "pnl": pnl, "proceeds": proceeds, "partial": partial, "remaining": pos.quantity if partial else 0.0,
                "total_pnl": pnl + (pos.realized if not partial else 0.0)}


class LiveOverseasBroker:
    """★★★ 실거래 - 국내주식과 완전히 같은 토스 계좌·API를 그대로 쓴다.
    다만 매도 대상 확인은 이 엔진의 장부(OverseasPositionBook)만 본다 -
    계좌에 실제로 있어도 이 장부에 없으면 거부한다.
    """

    def __init__(self, client, book: OverseasPositionBook | None = None):
        self.client = client
        self.book = book or OverseasPositionBook()

    def cash(self) -> float:
        try:
            bp = self.client.buying_power(currency="USD")
            return float(bp.get("cash") or bp.get("cashBasedBuyingPower") or 0)
        except Exception:
            return 0.0

    def buy(self, symbol: str, krw_amount: float, price: float, technique: str = "") -> OverseasPosition | None:
        # ★ 위와 같은 이유 - 실거래라 더더욱 모르는 값으로 주문하면 안 된다.
        price = _num(price)
        krw_amount = _num(krw_amount)
        if price is None or krw_amount is None or price <= 0:
            return None
        quantity = int(krw_amount / price)  # ★ 해외주식 매수는 정수 수량만(공식 문서: 소수점은 미국 시장가 매도에만 허용).
        if quantity < 1:
            return None
        result = self.client.create_order(symbol, "BUY", "MARKET", quantity)
        order_id = result.get("orderId") if isinstance(result, dict) else None
        pos = OverseasPosition(
            symbol=symbol, quantity=quantity, entry_price=price,
            entry_time=time.time(), peak_price=price, technique=technique, order_id=order_id, name=symbol,
            last_fill_price=price, invested=krw_amount,
        )
        self.book.record_buy(pos)
        return pos

    def add(self, symbol: str, amount: float, price: float) -> OverseasPosition | None:
        """이미 산 종목에 추가 매수. 정수 수량만 살 수 있고, 1주도 못 사면 None."""
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
            # ★★★ 계좌에 실제로 있어도 이 엔진이 산 적 없으면 거부한다.
            raise NotOwnedError(f"{symbol} 은(는) 이 엔진이 산 적 없는 종목입니다 - 매도를 거부합니다.")
        held = _num(pos.quantity) or 0.0
        qty = held if fraction >= 1.0 else split_quantity(held, fraction, integer=True)
        if qty >= held:
            qty = held
        partial = qty < held
        self.client.create_order(symbol, "SELL", "MARKET", int(qty) if float(qty).is_integer() else qty)
        # ★★★ 청산은 절대 실패하면 안 된다 - 못 팔면 손실로 직결된다.
        # 값이 이상하면 0 으로 두고서라도 포지션은 정리한다(손익 계산이
        # 틀리는 것보다 종목을 못 파는 쪽이 훨씬 위험하다).
        price = _num(price) or 0.0
        entry = _num(pos.entry_price) or 0.0
        proceeds = qty * price
        cost = qty * entry
        expense_cost = _etf_expense_cost(symbol, cost, pos.entry_time)
        pnl = proceeds - cost - expense_cost
        if partial:
            _shrink(pos, qty, held, pnl)
        else:
            self.book.record_sell(symbol)
        return {"quantity": qty, "pnl": pnl, "proceeds": proceeds, "partial": partial, "remaining": pos.quantity if partial else 0.0,
                "total_pnl": pnl + (pos.realized if not partial else 0.0)}
