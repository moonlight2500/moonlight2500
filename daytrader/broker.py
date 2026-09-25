from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from daytrader.orders import OrderIntent, is_ours, new_coid, resolve_uncertain
from daytrader.playbook import oco_levels
from daytrader.ticks import buy_cost, round_to_tick, sell_proceeds
from daytrader.timeutil import day_str, iso, minutes_between, now_kst
from daytrader.tossapi import TossApiError

log = logging.getLogger(__name__)

FILLED_STATES = {"FILLED", "EXECUTED", "COMPLETED", "FULLY_EXECUTED", "DONE"}
DEAD_STATES = {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
FATAL_CODES = {
    "insufficient-cash", "account-suspended", "invalid-account",
    "trading-not-allowed", "edge-blocked", "network-uncertain",
}


class NotOwnedError(RuntimeError):
    """★★★ 이 브로커가 buy() 로 사서 직접 기록해 두지 않은 종목을 팔려고 할
    때 던진다. 계좌에 그 종목이 있어도 상관없다 - 이 프로그램이 직접 사지
    않은 것은 절대 팔지 않는다. 이 예외를 잡아서 조용히 넘어가면 안 된다 -
    발생했다는 것 자체가 어딘가 로직이 잘못 짜여 매도 대상을 잘못 골랐다는
    뜻이다.
    """


@dataclass
class Fill:
    ok: bool
    symbol: str
    side: str
    quantity: int
    price: float
    order_id: str | None
    reason: str
    fatal: bool
    price_estimated: bool


@dataclass
class Position:
    symbol: str
    name: str
    theme: str
    quantity: int
    entry_price: float
    entry_time: object  # KST-aware datetime
    peak_price: float
    oco_id: str | None
    entry_volume: float
    verdict_id: str | None
    why: str
    technique: str
    last_price: float = 0.0  # 마지막으로 받은 현재가(화면의 현재가·평가손익용). 0 이면 아직 못 받음.
    # ★ 분할 매수·분할 매도 상태(sizing.py)
    adds: int = 0
    last_fill_price: float = 0.0
    scaled_out: int = 0
    invested: float = 0.0
    realized: float = 0.0
    sold_qty: float = 0.0
    sold_value: float = 0.0
    conviction: float = 0.5  # 진입 시점 테마 근거의 크기(0~1) - 추가 매수·분할 매도 비율 조절에 쓴다

    def held_minutes(self, now=None) -> float:
        return minutes_between(self.entry_time, now or now_kst())

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "name": self.name, "theme": self.theme,
            "quantity": self.quantity, "entry_price": self.entry_price,
            "entry_time": iso(self.entry_time) if self.entry_time else None,
            "peak_price": self.peak_price, "oco_id": self.oco_id,
            "entry_volume": self.entry_volume, "verdict_id": self.verdict_id,
            "why": self.why, "technique": self.technique, "last_price": self.last_price,
            "adds": self.adds, "last_fill_price": self.last_fill_price, "scaled_out": self.scaled_out,
            "invested": self.invested, "realized": self.realized,
            "sold_qty": self.sold_qty, "sold_value": self.sold_value, "conviction": self.conviction,
        }


class BaseBroker:
    """모든 브로커가 구현해야 하는 최소 인터페이스."""

    def cash(self) -> float:
        raise NotImplementedError

    def buy(self, symbol, quantity, ref_price, *, urgent=False, reason="", verdict_id=None) -> Fill:
        raise NotImplementedError

    def sell(self, symbol, quantity, ref_price, *, urgent=False, reason="", verdict_id=None) -> Fill:
        raise NotImplementedError

    def place_oco(self, pos: Position, cfg):
        raise NotImplementedError

    def cancel_oco(self, oco_id) -> bool:
        raise NotImplementedError

    def oco_is_open(self, oco_id) -> bool:
        raise NotImplementedError

    def open_orders(self) -> list:
        raise NotImplementedError

    def cancel_all_ours(self) -> int:
        raise NotImplementedError


class PaperBroker(BaseBroker):
    """실제 주문 없이 가상으로 체결한다.
    ★ 실전에서 지정가는 대체로 붙지만 시장가는 호가를 먹고 들어간다.
    연습에서 이걸 무시하면 성적이 실제보다 좋게 나온다 - 그래서 슬리피지를 반영한다.
    """

    def __init__(self, cfg, client, starting_cash: float | None = None):
        self.cfg = cfg
        self.client = client  # 지금은 쓰지 않지만 LiveBroker 와 인터페이스를 맞춰 둔다.
        self._cash = starting_cash if starting_cash is not None else float(cfg.capital.allocation)
        self._oco_open: dict[str, bool] = {}
        # ★★★ 이 브로커가 buy() 로 직접 산 수량만 기록한다 - sell() 은
        # 이 장부에 없는 종목을 절대 팔지 않는다.
        self._bought_qty: dict[str, int] = {}

    def cash(self) -> float:
        return self._cash

    def register_holding(self, symbol: str, quantity: int) -> None:
        """★ buy() 를 거치지 않은 종목의 매도를 예외적으로 허용할 때만 쓴다
        (adopt_unknown_holdings 용). LiveBroker 와 인터페이스를 맞춘다.
        """
        self._bought_qty[symbol] = self._bought_qty.get(symbol, 0) + quantity

    def _slip(self, price: float, side: str, urgent: bool) -> int:
        pct = self.cfg.risk.max_slippage_pct * (1.0 if urgent else 0.4)
        if side == "BUY":
            return round_to_tick(price * (1 + pct), "up")
        return round_to_tick(price * (1 - pct), "down")

    def buy(self, symbol, quantity, ref_price, *, urgent=False, reason="", verdict_id=None) -> Fill:
        price = self._slip(ref_price, "BUY", urgent)
        cost = buy_cost(price, quantity, self.cfg.costs.commission_pct)
        if cost > self._cash:
            return Fill(
                ok=False, symbol=symbol, side="BUY", quantity=0, price=price, order_id=None,
                reason=f"현금 부족 (필요 {cost:,}원 / 보유 {int(self._cash):,}원)",
                fatal=False, price_estimated=True,
            )
        self._cash -= cost
        self._bought_qty[symbol] = self._bought_qty.get(symbol, 0) + quantity
        return Fill(
            ok=True, symbol=symbol, side="BUY", quantity=quantity, price=price,
            order_id=f"paper-{uuid4().hex[:8]}", reason=reason, fatal=False, price_estimated=True,
        )

    def sell(self, symbol, quantity, ref_price, *, urgent=False, reason="", verdict_id=None) -> Fill:
        # ★★★ 핵심 안전장치 - 이 브로커가 산 적 없는 종목은 절대 팔지 않는다.
        if self._bought_qty.get(symbol, 0) <= 0:
            raise NotOwnedError(
                f"{symbol} 은 이 브로커가 산 적이 없습니다 - 매도를 거부합니다. "
                "계좌에 있는 종목이라도 이 프로그램이 직접 사지 않은 것은 절대 팔지 않습니다."
            )
        price = self._slip(ref_price, "SELL", urgent)
        proceeds = sell_proceeds(price, quantity, self.cfg.costs.commission_pct, self.cfg.costs.tax_pct)
        self._cash += proceeds
        remaining = self._bought_qty.get(symbol, 0) - quantity
        if remaining <= 0:
            self._bought_qty.pop(symbol, None)
        else:
            self._bought_qty[symbol] = remaining
        return Fill(
            ok=True, symbol=symbol, side="SELL", quantity=quantity, price=price,
            order_id=f"paper-{uuid4().hex[:8]}", reason=reason, fatal=False, price_estimated=True,
        )

    def place_oco(self, pos: Position, cfg):
        oco_id = f"paper-oco-{uuid4().hex[:8]}"
        self._oco_open[oco_id] = True
        return oco_id

    def cancel_oco(self, oco_id) -> bool:
        return self._oco_open.pop(oco_id, None) is not None

    def oco_is_open(self, oco_id) -> bool:
        return self._oco_open.get(oco_id, False)

    def open_orders(self) -> list:
        return []

    def cancel_all_ours(self) -> int:
        return 0


class LiveBroker(BaseBroker):
    """실제 계좌로 실주문을 낸다. 이 엔진은 언제나 하나뿐이어야 한다 (원칙 11)."""

    FILL_WAIT_SECONDS = 20

    def __init__(self, cfg, client, order_book):
        if not cfg.is_live:
            raise RuntimeError("LiveBroker 는 mode=live 에서만 쓸 수 있습니다.")
        self.cfg = cfg
        self.client = client
        self.order_book = order_book
        self._logged_first_fill = False
        # ★★★ 이 브로커가 buy() 로 직접 산 수량만 기록한다 - sell() 은
        # 이 장부에 없는 종목을 절대 팔지 않는다. adopt_unknown_holdings 로
        # 사용자가 명시적으로 관리를 허락한 기존 보유는 register_holding() 을
        # 거쳐야 매도가 허용된다 - buy() 를 거치지 않은 채 몰래 팔리지 않는다.
        self._bought_qty: dict[str, int] = {}

    def register_holding(self, symbol: str, quantity: int) -> None:
        """★ buy() 를 거치지 않은 종목의 매도를 예외적으로 허용할 때만 쓴다
        (예: 사용자가 [설정] 에서 adopt_unknown_holdings 를 명시적으로 켜서
        기존 보유 종목의 관리를 프로그램에 맡기기로 동의한 경우). 이름 자체가
        "buy 가 아니라 등록"임을 분명히 한다 - 기본 경로가 아니다.
        """
        self._bought_qty[symbol] = self._bought_qty.get(symbol, 0) + quantity

    def cash(self) -> float:
        bp = self.client.buying_power()
        return float(bp.get("cash") or bp.get("buyingPower") or 0)

    def _wait_fill(self, order_id, timeout: float):
        """1.5초 폴링. 필드명이 문서와 다를 수 있어 방어적으로 여러 이름을 시도한다."""
        import time as _time

        deadline = _time.monotonic() + timeout
        filled, avg, status = 0, 0.0, "UNKNOWN"
        while _time.monotonic() < deadline:
            try:
                row = self.client.get_order(order_id)
            except Exception:
                _time.sleep(1.5)
                continue
            if not self._logged_first_fill:
                # ★ 실계좌 첫 주문 때 실제 필드명을 확인할 수 있게 응답 전문을 한 번 남긴다.
                log.info("첫 주문 응답 전문: %s", row)
                self._logged_first_fill = True
            status = row.get("status") or row.get("orderStatus") or "UNKNOWN"
            filled = row.get("filledQuantity") or row.get("executedQuantity") or 0
            avg = row.get("averageFilledPrice") or row.get("averagePrice") or 0.0
            if status in FILLED_STATES or status in DEAD_STATES:
                return filled, avg, status
            _time.sleep(1.5)
        return filled, avg, status

    def _send(self, intent: OrderIntent, fn, *args, **kw):
        """의도를 먼저 기록한 뒤 실제로 보낸다.
        network-uncertain 이면 재전송 대신 resolve_uncertain() 으로 확인한다.
        실패하면 Fill(ok=False) 를 돌려주고, 성공하면 API 응답 dict 를 돌려준다.
        """
        self.order_book.record(intent)
        try:
            result = fn(*args, **kw)
            order_id = result.get("orderId") or result.get("id")
            self.order_book.update(intent.coid, status="sent", order_id=order_id)
            return result
        except TossApiError as exc:
            if exc.code == "network-uncertain":
                order_id = resolve_uncertain(self.client, intent, self.order_book)
                if order_id is None:
                    return Fill(
                        ok=False, symbol=intent.symbol, side=intent.side, quantity=0,
                        price=0.0, order_id=None,
                        reason=(
                            "주문을 보냈지만 접수 여부를 확인하지 못했습니다. "
                            "토스 앱에서 직접 확인하세요. 매매를 멈춥니다."
                        ),
                        fatal=True, price_estimated=False,
                    )
                return {"orderId": order_id}

            self.order_book.update(intent.coid, status="unknown", error=str(exc))
            return Fill(
                ok=False, symbol=intent.symbol, side=intent.side, quantity=0, price=0.0,
                order_id=None, reason=f"주문 실패: {exc.message}",
                fatal=exc.code in FATAL_CODES, price_estimated=False,
            )

    def buy(self, symbol, quantity, ref_price, *, urgent=False, reason="", verdict_id=None) -> Fill:
        price = round_to_tick(ref_price * (1 + self.cfg.risk.max_slippage_pct), "up")
        coid = new_coid("BUY", symbol)
        intent = OrderIntent(
            coid=coid, at=iso(now_kst()), mode=self.cfg.mode, symbol=symbol, name="",
            side="BUY", order_type="LIMIT", quantity=quantity, price=price,
            reason=reason, verdict_id=verdict_id,
        )
        result = self._send(
            intent, self.client.create_order, symbol, "BUY", "LIMIT", quantity,
            price=price, clientOrderId=coid,
        )
        if isinstance(result, Fill):
            return result

        order_id = result.get("orderId") or result.get("id")
        filled, avg, status = self._wait_fill(order_id, self.FILL_WAIT_SECONDS)

        if filled == 0 and status not in DEAD_STATES:
            # ★ 시장가로 추격하지 않는다. 20초 미체결이면 취소하고 실패로 처리한다.
            try:
                self.client.cancel_order(order_id)
            except Exception:
                pass
            self.order_book.update(coid, status="sent", filled=0, error="20초 미체결로 취소")
            return Fill(
                ok=False, symbol=symbol, side="BUY", quantity=0, price=price, order_id=order_id,
                reason="20초 안에 체결되지 않아 주문을 취소했습니다.", fatal=False, price_estimated=False,
            )

        if 0 < filled < quantity and status not in DEAD_STATES:
            # 부분체결 - 잔량은 취소하고 체결된 만큼만 인정한다.
            try:
                self.client.cancel_order(order_id)
            except Exception:
                pass

        self.order_book.update(coid, status="sent", filled=filled, avg_price=avg)
        if filled > 0:
            self._bought_qty[symbol] = self._bought_qty.get(symbol, 0) + filled
        return Fill(
            ok=filled > 0, symbol=symbol, side="BUY", quantity=filled, price=avg or price,
            order_id=order_id, reason="" if filled > 0 else "체결되지 않았습니다.",
            fatal=False, price_estimated=False,
        )

    def sell(self, symbol, quantity, ref_price, *, urgent=False, reason="", verdict_id=None) -> Fill:
        # ★★★ 핵심 안전장치 - 이 브로커가 산 적 없는(또는 register_holding
        # 으로 명시적으로 허락받지 않은) 종목은 절대 팔지 않는다. 계좌에
        # 그 종목이 있어도 상관없다.
        if self._bought_qty.get(symbol, 0) <= 0:
            raise NotOwnedError(
                f"{symbol} 은 이 브로커가 산 적이 없습니다 - 매도를 거부합니다. "
                "계좌에 있는 종목이라도 이 프로그램이 직접 사지 않은 것은 절대 팔지 않습니다."
            )
        try:
            sellable = self.client.sellable_quantity(symbol)
            max_qty = sellable.get("sellableQuantity") or sellable.get("quantity") or quantity
        except Exception:
            max_qty = quantity
        quantity = min(quantity, int(max_qty))
        if quantity <= 0:
            return Fill(
                ok=False, symbol=symbol, side="SELL", quantity=0, price=0.0, order_id=None,
                reason="매도 가능 수량이 없습니다.", fatal=False, price_estimated=False,
            )

        order_type = "MARKET" if urgent else "LIMIT"
        price = None if urgent else round_to_tick(ref_price * (1 - self.cfg.risk.max_slippage_pct), "down")
        coid = new_coid("SELL", symbol)
        intent = OrderIntent(
            coid=coid, at=iso(now_kst()), mode=self.cfg.mode, symbol=symbol, name="",
            side="SELL", order_type=order_type, quantity=quantity, price=price or 0.0,
            reason=reason, verdict_id=verdict_id,
        )
        result = self._send(
            intent, self.client.create_order, symbol, "SELL", order_type, quantity,
            price=price, clientOrderId=coid,
        )
        if isinstance(result, Fill):
            return result

        order_id = result.get("orderId") or result.get("id")
        filled, avg, status = self._wait_fill(order_id, self.FILL_WAIT_SECONDS)

        if filled < quantity and status not in DEAD_STATES:
            # ★ 청산은 반드시 되어야 한다. 여기서 실패하면 오버나이트다.
            # 지정가가 안 붙으면 취소하고 잔량을 시장가로 확실히 털어낸다.
            try:
                self.client.cancel_order(order_id)
            except Exception:
                pass
            remaining = quantity - filled
            if remaining > 0:
                coid2 = new_coid("SELL", symbol)
                intent2 = OrderIntent(
                    coid=coid2, at=iso(now_kst()), mode=self.cfg.mode, symbol=symbol, name="",
                    side="SELL", order_type="MARKET", quantity=remaining, price=0.0,
                    reason="지정가 미체결 잔량 시장가 청산", verdict_id=verdict_id,
                )
                result2 = self._send(
                    intent2, self.client.create_order, symbol, "SELL", "MARKET", remaining,
                    clientOrderId=coid2,
                )
                if not isinstance(result2, Fill):
                    order_id2 = result2.get("orderId") or result2.get("id")
                    filled2, avg2, _status2 = self._wait_fill(order_id2, self.FILL_WAIT_SECONDS)
                    if filled2 > 0:
                        total = filled + filled2
                        avg = ((avg * filled) + (avg2 * filled2)) / total if total else avg
                        filled = total

        self.order_book.update(coid, status="sent", filled=filled, avg_price=avg)
        if filled > 0:
            remaining = self._bought_qty.get(symbol, 0) - filled
            if remaining <= 0:
                self._bought_qty.pop(symbol, None)
            else:
                self._bought_qty[symbol] = remaining
        return Fill(
            ok=filled > 0, symbol=symbol, side="SELL", quantity=filled, price=avg or (price or 0.0),
            order_id=order_id, reason="" if filled > 0 else "매도 체결에 실패했습니다.",
            fatal=(filled == 0), price_estimated=False,
        )

    def place_oco(self, pos: Position, cfg):
        """실패해도 예외를 던지지 않는다. 실패하면 프로그램 내부 손절만 작동한다는
        것을 호출부가 알 수 있도록 경고를 남기고 None 을 돌려준다.
        """
        levels = oco_levels(pos.entry_price, cfg.risk, round_to_tick)
        today = day_str(now_kst())
        try:
            result = self.client.create_oco(
                pos.symbol, pos.quantity, today,
                take_profit_trigger=levels["take_profit_trigger"],
                take_profit_price=levels["take_profit_price"],
                stop_loss_trigger=levels["stop_loss_trigger"],
                stop_loss_price=levels["stop_loss_price"],
            )
            return result.get("conditionalOrderId") or result.get("id")
        except Exception as exc:
            log.warning("OCO 주문 실패 - 프로그램 내부 손절만 작동합니다: %s", exc)
            return None

    def cancel_oco(self, oco_id) -> bool:
        try:
            self.client.cancel_conditional_order(oco_id)
            return True
        except Exception:
            return False

    def oco_is_open(self, oco_id) -> bool:
        """취소 실패 시 실제로 살아있는지 확인한다.
        모르면 True 로 본다 - 이중 매도보다 살아있다고 보는 편이 안전하다.
        """
        try:
            # ★★★ 실제로 겪은 버그 - 다른 곳(screener.py 의 rankings 등)은
            # 이 계열 API 호출 시 필수 파라미터를 항상 명시하는데, 여기는
            # status 없이 부르고 있었다. "살아있는지"를 확인하는 목적과도
            # status="OPEN" 이 정확히 맞는다.
            rows = self.client.conditional_orders(status="OPEN")
            rows = rows if isinstance(rows, list) else rows.get("orders", [])
            for r in rows:
                rid = r.get("conditionalOrderId") or r.get("id")
                if rid == oco_id:
                    status = r.get("status", "")
                    # ★ 체결(FILLED)됐거나 죽은 상태(DEAD_STATES)면 더 이상 열려있지 않다.
                    return status not in DEAD_STATES and status not in FILLED_STATES
            return False  # 목록에 없으면 이미 끝난 것으로 본다.
        except Exception:
            return True

    def open_orders(self) -> list:
        try:
            rows = self.client.get_orders(status="OPEN")
            return rows if isinstance(rows, list) else rows.get("orders", [])
        except Exception:
            return []

    def cancel_all_ours(self) -> int:
        """★ is_ours 인 것만 취소한다. 남의 주문은 절대 건드리지 않는다."""
        count = 0
        for o in self.open_orders():
            if not is_ours(o.get("clientOrderId", "")):
                continue
            order_id = o.get("orderId") or o.get("id")
            try:
                self.client.cancel_order(order_id)
                count += 1
            except Exception:
                pass
        return count
