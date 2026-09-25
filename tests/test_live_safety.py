"""실거래 안전 테스트 - 가장 중요하다. 여기서 나는 버그는 실제 돈으로 갚는다.
가짜 TossClient(FakeToss)로 사고 상황을 만들어 막히는지 확인한다.
`python tests/test_live_safety.py` 로 실행한다.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
THEMES_PATH = os.path.join(ROOT, "themes.yaml")

from daytrader.tossapi import TossApiError  # noqa: E402

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


def _live_cfg():
    from daytrader.config import load_config
    cfg = load_config(CONFIG_PATH)
    cfg.mode = "live"
    return cfg


def _dummy_book():
    from daytrader.orders import OrderBook
    return OrderBook(tempfile.mkdtemp())


# ━━ 가짜 토스 클라이언트 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FakeToss:
    """조작 가능한 옵션으로 사고 상황을 재현하는 가짜 TossClient."""

    account_seq = 0

    def __init__(self):
        self.holdings_data: list = []
        self.open_orders_data: list = []
        self.cash = 10_000_000
        self.business_day = True
        self.fail_create = False  # True 면 create_order 가 예외를 던지되 주문은 접수된 것으로 처리한다.
        self.order_status = "FILLED"
        self.cancel_ok = True
        self.stocks_missing: list = []
        self.conditional_orders_data: list = []
        self.warnings_data: dict = {}
        self.orders: list = []  # "실제로 접수된" 주문들 (이중 주문 검사용)
        self._seq = 0

    # ━━ 계좌 ━━
    def accounts(self):
        return [{"accountSeq": 0, "accountType": "BROKERAGE", "accountNumber": "TEST-0"}]

    def resolve_account(self, prefer=None):
        return 0

    def buying_power(self):
        return {"cash": self.cash, "buyingPower": self.cash}

    def holdings(self):
        # ★★★ 실제 토스 API는 리스트가 아니라
        # {"items": [...], "totalPurchaseAmount":..., ...} 객체를 반환한다
        # (HoldingsOverview 모델, 공식 문서로 확인). 예전엔 이 가짜
        # 클라이언트가 리스트를 그대로 반환해서, 실제 코드가 잘못
        # 순회해도(딕셔너리 키를 도는 버그) 테스트가 못 잡았다.
        return {"items": self.holdings_data}

    def sellable_quantity(self, symbol):
        for h in self.holdings_data:
            if h.get("symbol") == symbol:
                return {"sellableQuantity": h.get("quantity", 0)}
        return {"sellableQuantity": 0}

    def market_calendar_kr(self):
        return {"open": self.business_day, "date": "2026-09-04"}

    # ━━ 종목 ━━
    def stocks(self, symbols):
        return [{"symbol": s, "name": s, "delisted": False, "tradingHalt": False,
                  "isPreferred": False, "isETF": False, "isETN": False,
                  "isLeveraged": False, "isInverse": False}
                for s in symbols if s not in self.stocks_missing]

    def warnings(self, symbol):
        return self.warnings_data.get(symbol, [])

    def prices(self, symbols):
        return [{"symbol": s, "price": 10000, "changeRate": 0.01} for s in symbols]

    def candles(self, symbol, timeframe, count, adjusted=True):
        return []

    def price_limits(self, symbol):
        return {"symbol": symbol, "upperLimit": 13000, "lowerLimit": 7000}

    # ━━ 주문 조회 ━━
    def get_orders(self, **kwargs):
        if "clientOrderId" in kwargs:
            coid = kwargs["clientOrderId"]
            return [o for o in self.orders if o.get("clientOrderId") == coid]
        rows = self.orders
        if "symbol" in kwargs:
            rows = [o for o in rows if o.get("symbol") == kwargs["symbol"]]
        if "side" in kwargs:
            rows = [o for o in rows if o.get("side") == kwargs["side"]]
        if kwargs.get("status") == "OPEN":
            return self.open_orders_data
        return rows

    def get_order(self, order_id):
        for o in self.orders:
            if o["orderId"] == order_id:
                return o
        raise TossApiError(404, "not-found", "주문을 찾을 수 없습니다.")

    # ━━ 주문 ━━
    def create_order(self, symbol, side, orderType, quantity, price=None, timeInForce="DAY", clientOrderId=None):
        self._seq += 1
        order_id = f"oid-{self._seq}"
        order = {
            "orderId": order_id, "clientOrderId": clientOrderId, "symbol": symbol, "side": side,
            "quantity": quantity, "status": self.order_status,
            "filledQuantity": quantity if self.order_status == "FILLED" else 0,
            "averageFilledPrice": price or 10000,
        }
        self.orders.append(order)
        if self.fail_create:
            # ★★ 예외를 던지되 주문은 이미 접수된 것으로 처리한다 (network-uncertain 재현).
            raise TossApiError(0, "network-uncertain", "연결 불확실")
        return {"orderId": order_id}

    def cancel_order(self, order_id):
        if not self.cancel_ok:
            raise TossApiError(500, "cancel-failed", "취소 실패")
        for o in self.orders:
            if o["orderId"] == order_id:
                o["status"] = "CANCELLED"
        for o in self.open_orders_data:
            if o.get("orderId") == order_id:
                o["status"] = "CANCELLED"
        return {"ok": True}

    def modify_order(self, *a, **kw):
        raise NotImplementedError

    def create_oco(self, symbol, quantity, expire_date, *, take_profit_trigger, take_profit_price,
                    stop_loss_trigger, stop_loss_price):
        self._seq += 1
        oco_id = f"oco-{self._seq}"
        self.conditional_orders_data.append({"conditionalOrderId": oco_id, "status": "OPEN", "symbol": symbol})
        return {"conditionalOrderId": oco_id}

    def cancel_conditional_order(self, conditional_order_id):
        if not self.cancel_ok:
            raise TossApiError(500, "cancel-failed", "취소 실패")
        for o in self.conditional_orders_data:
            if o["conditionalOrderId"] == conditional_order_id:
                o["status"] = "CANCELLED"
        return {"ok": True}

    def conditional_orders(self, **kwargs):
        return self.conditional_orders_data


# ━━ 주문 멱등성 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_idempotency() -> None:
    print("\n== 주문 멱등성 ==")
    from daytrader.broker import LiveBroker
    from daytrader.orders import OrderBook
    import daytrader.orders as orders_mod

    orig_sleep = orders_mod.time.sleep
    orders_mod.time.sleep = lambda s: None  # 테스트를 빠르게 - resolve_uncertain 재시도 대기를 없앤다.
    try:
        # ★★ fail_create=True 로 타임아웃을 만들고 buy() 호출 → 이중 주문이 없어야 한다.
        with tempfile.TemporaryDirectory() as d:
            client = FakeToss()
            client.fail_create = True
            book = OrderBook(d)
            broker = LiveBroker(_live_cfg(), client, book)

            fill = broker.buy("005930", 10, 10000, reason="test")
            check("★★이중 주문 없음(실제 주문 1건)", len(client.orders) == 1, f"실제 {len(client.orders)}건")
            check("조회로 기존 주문을 찾아 인계", fill.ok, fill.reason)

        # get_orders 가 끝내 못 찾는 경우 - 진짜로 접수 여부를 모르는 상태.
        with tempfile.TemporaryDirectory() as d:
            client2 = FakeToss()
            client2.fail_create = True
            client2.get_orders = lambda **kw: []  # 조회해도 절대 못 찾는다.
            book2 = OrderBook(d)
            broker2 = LiveBroker(_live_cfg(), client2, book2)

            fill2 = broker2.buy("005930", 10, 10000, reason="test")
            check("★get_orders 빈 배열이면 Fill(fatal=True)", fill2.fatal, fill2.reason)

            intents = book2.all()
            check("의도가 기록됨", len(intents) >= 1)
            if intents:
                latest = book2.latest(intents[0].coid)
                check("★status=unknown", latest is not None and latest.status == "unknown")
    finally:
        orders_mod.time.sleep = orig_sleep


# ━━ 서버 OCO ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_oco_is_open() -> None:
    print("\n== 서버 OCO ==")
    from daytrader.broker import LiveBroker

    client = FakeToss()
    broker = LiveBroker(_live_cfg(), client, _dummy_book())

    oco_id = "oco-1"
    client.conditional_orders_data = [{"conditionalOrderId": oco_id, "status": "OPEN"}]
    check("열려있으면 True", broker.oco_is_open(oco_id) is True)

    client.conditional_orders_data = [{"conditionalOrderId": oco_id, "status": "FILLED"}]
    check("체결됐으면 False", broker.oco_is_open(oco_id) is False)


def test_oco_cancel_fail() -> None:
    print("\n== OCO 취소 실패 ==")
    from daytrader.broker import LiveBroker

    client = FakeToss()
    client.cancel_ok = False
    broker = LiveBroker(_live_cfg(), client, _dummy_book())

    oco_id = "oco-2"
    client.conditional_orders_data = [{"conditionalOrderId": oco_id, "status": "OPEN"}]

    ok = broker.cancel_oco(oco_id)
    check("cancel_oco() 가 False 를 돌려준다", ok is False)
    check("취소 실패 시 oco_is_open() 은 True (안전한 쪽으로)", broker.oco_is_open(oco_id) is True)


# ━━ 매수 가능 금액 0원 처리 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_cash_zero_not_treated_as_missing() -> None:
    """★★★ 실제로 겪은 버그 - `bp.get("cash") or bp.get("buyingPower") or 0` 은
    cash 가 정확히 0원이어도(정상적으로 다 써서 없는 상태) "없는 값"으로 보고
    buyingPower 필드로 넘어간다. cash 와 buyingPower 가 다른 값을 담고 있으면
    0원인 계좌를 매수 가능한 것처럼 잘못 본다. None 과 0 을 구분해야 한다.
    """
    print("\n== 매수 가능 금액이 정확히 0원일 때 ==")
    from daytrader.broker import LiveBroker

    class _BuyingPowerClient:
        def __init__(self, cash, buying_power):
            self._cash = cash
            self._bp = buying_power

        def buying_power(self):
            return {"cash": self._cash, "buyingPower": self._bp}

    # cash=0(정상적으로 다 썼다) 인데 buyingPower 는 다른(더 큰) 값을 담고 있다.
    broker = LiveBroker(_live_cfg(), _BuyingPowerClient(0, 5_000_000), _dummy_book())
    check("★cash=0 이면 buyingPower 로 새지 않고 0을 그대로 씀", broker.cash() == 0, str(broker.cash()))

    # cash 필드가 아예 없으면(None) buyingPower 로 정상적으로 넘어가야 한다.
    broker2 = LiveBroker(_live_cfg(), _BuyingPowerClient(None, 5_000_000), _dummy_book())
    check("cash 필드가 없으면 buyingPower 로 대체", broker2.cash() == 5_000_000, str(broker2.cash()))

    # 둘 다 없으면 0.
    broker3 = LiveBroker(_live_cfg(), _BuyingPowerClient(None, None), _dummy_book())
    check("둘 다 없으면 0", broker3.cash() == 0, str(broker3.cash()))


# ━━ 계좌 대조 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _FakeState:
    def __init__(self):
        self.positions: dict = {}


def test_reconcile() -> None:
    print("\n== 계좌 대조 ==")
    from daytrader.safety import reconcile
    from daytrader.broker import Position
    from daytrader.ledger import Ledger
    from daytrader.journal import Journal
    from daytrader.timeutil import now_kst

    with tempfile.TemporaryDirectory() as d:
        cfg = _live_cfg()
        cfg.state_dir = d
        ledger = Ledger(d)
        journal = Journal(d, mode="live")

        # ghost: 상태엔 있는데 계좌엔 없다.
        state = _FakeState()
        state.positions["005930"] = Position(
            symbol="005930", name="삼성전자", theme="t", quantity=10, entry_price=10000,
            entry_time=now_kst(), peak_price=10500, oco_id="oco-1", entry_volume=1000,
            verdict_id="v1", why="", technique="fixed",
        )
        client = FakeToss()
        client.holdings_data = []  # 계좌엔 없음 - 이미 팔렸다.
        client.orders = [{
            "orderId": "oid-1", "clientOrderId": None, "symbol": "005930", "side": "SELL",
            "status": "FILLED", "filledQuantity": 10, "averageFilledPrice": 10800,
        }]

        diffs = reconcile(client, state, cfg, journal, ledger, last_prices={"005930": 10900})
        ghosts = [d_ for d_ in diffs if d_.kind == "ghost"]
        check("★ghost 잡아냄", len(ghosts) == 1)
        check("포지션 닫음", "005930" not in state.positions)

        trades = ledger.trades(modes=["live"])
        check("원장 반영", len(trades) == 1)
        if trades:
            check("★체결가를 주문이력에서(10800원)", trades[0]["exit"] == 10800)
            check("★estimated=False (주문이력에서 찾았으므로)", trades[0]["estimated"] is False)

        # unknown_holding: 계좌엔 있는데 상태엔 없다 - 사용자가 직접 산 것일 수 있다.
        client2 = FakeToss()
        # ★★★ 실제 API 필드명은 averagePurchasePrice 다(HoldingsItem 모델) -
        # 예전엔 이 테스트가 존재하지 않는 필드(averagePrice)를 써서,
        # safety.py 의 같은 오타를 들키지 못했다.
        client2.holdings_data = [{"symbol": "999999", "quantity": 5, "name": "모르는종목", "averagePurchasePrice": 5000}]
        state2 = _FakeState()
        diffs2 = reconcile(client2, state2, cfg, journal, ledger)
        unknown = [d_ for d_ in diffs2 if d_.kind == "unknown_holding"]
        check("★남의 보유 발견하고 건드리지 않음", len(unknown) == 1 and "999999" not in state2.positions)

        diffs3 = reconcile(client2, state2, cfg, journal, ledger, adopt=True)
        check("adopt=True 면 입양", "999999" in state2.positions)
        if "999999" in state2.positions:
            check("★★★ 입양된 진입가가 실제 평균단가(5000원)로 정확히 반영됨(averagePrice 오타였으면 0원)",
                  state2.positions["999999"].entry_price == 5000)

        # 수량 불일치 - 계좌 기준으로 보정한다.
        state3 = _FakeState()
        state3.positions["005930"] = Position(
            symbol="005930", name="삼성전자", theme="t", quantity=20, entry_price=10000,
            entry_time=now_kst(), peak_price=10500, oco_id=None, entry_volume=1000,
            verdict_id="v1", why="", technique="fixed",
        )
        client3 = FakeToss()
        client3.holdings_data = [{"symbol": "005930", "quantity": 15}]
        reconcile(client3, state3, cfg, journal, ledger)
        check("★수량 불일치를 계좌 기준으로 보정(20->15)", state3.positions["005930"].quantity == 15)

        # ★★★ 실제로 겪은 버그 - 수량만 내부적으로 맞추고 서버 OCO(조건부
        # 손절/익절)는 옛 수량 그대로 방치했다. broker 를 넘기면 engine.py
        # 청산/분할매도와 같은 패턴(취소 후 재등록)으로 서버 OCO 도 새
        # 수량으로 다시 걸어야 한다.
        from daytrader.broker import LiveBroker

        state4 = _FakeState()
        state4.positions["005930"] = Position(
            symbol="005930", name="삼성전자", theme="t", quantity=20, entry_price=10000,
            entry_time=now_kst(), peak_price=10500, oco_id="existing-oco", entry_volume=1000,
            verdict_id="v1", why="", technique="fixed",
        )
        client4 = FakeToss()
        client4.holdings_data = [{"symbol": "005930", "quantity": 15}]
        # ★ id 를 FakeToss.create_oco() 가 만드는 형식(oco-N)과 겹치지 않게 둔다 -
        # 겹치면 재등록으로 새로 생긴 주문이 옛 주문과 같은 id 로 보여 테스트가
        # 착각할 수 있다.
        client4.conditional_orders_data = [{"conditionalOrderId": "existing-oco", "status": "OPEN", "symbol": "005930"}]
        broker4 = LiveBroker(cfg, client4, _dummy_book())

        diffs4 = reconcile(client4, state4, cfg, journal, ledger, broker=broker4)
        check("수량은 여전히 계좌 기준으로 보정됨(20->15)", state4.positions["005930"].quantity == 15)
        check(
            "★수량 불일치 시 broker 를 넘기면 서버 OCO 도 새 수량으로 재등록",
            state4.positions["005930"].oco_id is not None and state4.positions["005930"].oco_id != "existing-oco",
            str(state4.positions["005930"].oco_id),
        )
        old_oco = next(o for o in client4.conditional_orders_data if o["conditionalOrderId"] == "existing-oco")
        check("옛 OCO는 취소됨", old_oco["status"] == "CANCELLED")
        new_ocos = [o for o in client4.conditional_orders_data if o["conditionalOrderId"] != "existing-oco"]
        check("새 OCO가 생성됨", len(new_ocos) == 1 and new_ocos[0]["status"] == "OPEN")
        mismatch_diff4 = next(d_ for d_ in diffs4 if d_.kind == "qty_mismatch")
        check("action에 OCO 재등록이 기록됨(조용히 넘기지 않음)", "OCO" in mismatch_diff4.action, mismatch_diff4.action)

        # OCO 취소가 실패하면(서버 주문이 여전히 살아있다) - 조용히 넘어가지 않고
        # 옛 OCO를 그대로 둔 채(이중 주문 방지) 실패를 detail·journal(halt)에 남긴다.
        state5 = _FakeState()
        state5.positions["005930"] = Position(
            symbol="005930", name="삼성전자", theme="t", quantity=20, entry_price=10000,
            entry_time=now_kst(), peak_price=10500, oco_id="existing-oco-2", entry_volume=1000,
            verdict_id="v1", why="", technique="fixed",
        )
        client5 = FakeToss()
        client5.holdings_data = [{"symbol": "005930", "quantity": 15}]
        client5.conditional_orders_data = [{"conditionalOrderId": "existing-oco-2", "status": "OPEN", "symbol": "005930"}]
        client5.cancel_ok = False  # 취소 자체가 실패한다.
        broker5 = LiveBroker(cfg, client5, _dummy_book())

        diffs5 = reconcile(client5, state5, cfg, journal, ledger, broker=broker5)
        check("취소 실패 시 옛 OCO를 그대로 둠(이중 매도/이중 주문 방지)",
              state5.positions["005930"].oco_id == "existing-oco-2")
        mismatch_diff5 = next(d_ for d_ in diffs5 if d_.kind == "qty_mismatch")
        check("★취소 실패를 조용히 넘기지 않고 detail에 남김", "취소하지 못해" in mismatch_diff5.detail, mismatch_diff5.detail)


# ━━ 고아 주문 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_cleanup_orphans() -> None:
    print("\n== 고아 주문 ==")
    from daytrader.safety import cleanup_orphans
    from daytrader.broker import LiveBroker

    client = FakeToss()
    client.open_orders_data = [
        {"orderId": "1", "clientOrderId": "dt-b-005930-abc123", "symbol": "005930"},
        {"orderId": "2", "clientOrderId": "manual-order-1", "symbol": "000660"},
        {"orderId": "3", "clientOrderId": None, "symbol": "035420"},
    ]
    broker = LiveBroker(_live_cfg(), client, _dummy_book())

    diffs = cleanup_orphans(broker, _FakeState())
    cancelled_symbols = {d_.symbol for d_ in diffs}
    check("★dt- 만 취소", cancelled_symbols == {"005930"}, str(cancelled_symbols))
    check("manual-order 는 남김", "000660" not in cancelled_symbols)
    check("clientOrderId=None 은 남김", "035420" not in cancelled_symbols)


# ━━ 사전 점검 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_preflight() -> None:
    print("\n== 사전 점검 ==")
    from daytrader.safety import preflight
    from daytrader.ledger import Ledger

    with tempfile.TemporaryDirectory() as d:
        cfg = _live_cfg()
        cfg.state_dir = d
        ledger = Ledger(d)

        result = preflight(cfg, FakeToss(), ledger=ledger)
        check("정상이면 통과", result["ok"], "; ".join(c["detail"] for c in result["blocking"]))
        check("12항목 이상", len(result["checks"]) >= 12, str(len(result["checks"])))
        check("모든 항목에 설명", all(c.get("detail") for c in result["checks"]))

        client_holiday = FakeToss()
        client_holiday.business_day = False
        check("★휴장일 차단", not preflight(cfg, client_holiday, ledger=ledger)["ok"])

        client_open_order = FakeToss()
        client_open_order.open_orders_data = [{"orderId": "1", "clientOrderId": "x"}]
        check("★미체결주문 차단", not preflight(cfg, client_open_order, ledger=ledger)["ok"])

        # ★★★ 실제로 겪은 버그 - 보유 종목이 있는 상태로 재시작(정상적인
        # 사용법 - allow_overnight 기본값 True)하면 그 종목의 서버 OCO 가
        # 미체결로 남아 있는 게 당연한데, 예전엔 "미체결 주문이 하나라도
        # 있으면 무조건 차단"이라 실거래를 아예 시작할 수 없었다. 지금 보유
        # 중인 종목의 주문·조건부 주문은 통과시키고, 그 종목과 무관한(고아)
        # 것만 막아야 한다.
        from daytrader.broker import Position
        from daytrader.timeutil import now_kst

        held_positions = {
            "005930": Position(
                symbol="005930", name="삼성전자", theme="t", quantity=10, entry_price=70000,
                entry_time=now_kst(), peak_price=70000, oco_id="oco-held", entry_volume=0,
                verdict_id="v", why="", technique="fixed",
            )
        }

        client_own_position_order = FakeToss()
        client_own_position_order.open_orders_data = [{"orderId": "1", "clientOrderId": "x", "symbol": "005930"}]
        client_own_position_order.conditional_orders_data = [
            {"conditionalOrderId": "oco-held", "status": "OPEN", "symbol": "005930"}
        ]
        result_held = preflight(cfg, client_own_position_order, ledger=ledger, positions=held_positions)
        check(
            "★보유 중인 종목과 연결된 미체결 주문·조건부 주문은 재시작을 막지 않음",
            result_held["ok"], "; ".join(c["detail"] for c in result_held["blocking"]),
        )

        client_orphan_order = FakeToss()
        client_orphan_order.open_orders_data = [{"orderId": "2", "clientOrderId": "y", "symbol": "000660"}]
        client_orphan_order.conditional_orders_data = [
            {"conditionalOrderId": "oco-held", "status": "OPEN", "symbol": "005930"}
        ]
        result_orphan = preflight(cfg, client_orphan_order, ledger=ledger, positions=held_positions)
        check(
            "보유 종목과 무관한(고아) 미체결 주문은 여전히 차단",
            not result_orphan["ok"], "; ".join(c["detail"] for c in result_orphan["blocking"]),
        )

        client_orphan_cond = FakeToss()
        client_orphan_cond.conditional_orders_data = [
            {"conditionalOrderId": "oco-unknown", "status": "OPEN", "symbol": "035420"}
        ]
        result_orphan_cond = preflight(cfg, client_orphan_cond, ledger=ledger, positions=held_positions)
        check(
            "보유 종목과 무관한(고아) 조건부 주문은 여전히 차단",
            not result_orphan_cond["ok"], "; ".join(c["detail"] for c in result_orphan_cond["blocking"]),
        )

        client_no_cash = FakeToss()
        client_no_cash.cash = 0
        check("★잔고부족 차단", not preflight(cfg, client_no_cash, ledger=ledger)["ok"])

        client_bad_theme = FakeToss()
        client_bad_theme.stocks_missing = ["005930"]
        check("★테마코드실패 차단", not preflight(cfg, client_bad_theme, ledger=ledger)["ok"])

        # ★★★ "배정 자금이 실제 계좌 잔여 현금을 초과하면 안 된다" - 배정
        # 금액보다 계좌 잔고가 훨씬 적으면(잔고부족 자체는 이미 위에서
        # per_trade_amount 기준으로 걸리므로, 여기서는 1회 매매 금액은
        # 충분히 확보하되 총 배정액만 잔고를 넘도록 만든다.
        client_over_allocation = FakeToss()
        client_over_allocation.cash = cfg.capital.allocation - 1  # 배정금액보다 딱 1원 적은 잔고
        result_over = preflight(cfg, client_over_allocation, ledger=ledger)
        alloc_check = next((c for c in result_over["checks"] if c["key"] == "allocation_within_cash"), None)
        check("★★★ 배정금액이 계좌 잔고를 초과하면 차단됨", not result_over["ok"] and alloc_check is not None and not alloc_check["ok"])

        client_within_allocation = FakeToss()
        client_within_allocation.cash = cfg.capital.allocation + 1_000_000  # 배정금액보다 넉넉한 잔고
        result_within = preflight(cfg, client_within_allocation, ledger=ledger)
        alloc_check2 = next((c for c in result_within["checks"] if c["key"] == "allocation_within_cash"), None)
        check("배정금액이 잔고 이내면 통과됨", alloc_check2 is not None and alloc_check2["ok"])

        cfg_bad_cost = _live_cfg()
        cfg_bad_cost.state_dir = d
        cfg_bad_cost.risk.take_profit_pct = 0.0001
        check("★비용설정 차단", not preflight(cfg_bad_cost, FakeToss(), ledger=ledger)["ok"])


# ━━ 저하 모드 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_degraded_mode() -> None:
    print("\n== 저하 모드 ==")
    from daytrader.engine import Engine
    from daytrader.simulator import SimClient
    from daytrader.clock import SimClock

    cfg = _live_cfg()
    cfg.mode = "sim"  # 엔진 자체는 sim 으로 - 저하 모드 로직만 확인한다.
    cfg.live.degrade_after_failures = 3

    with tempfile.TemporaryDirectory() as d:
        cfg.state_dir = d
        clock = SimClock(start="10:00", speed=1, day="2026-09-04")
        client = SimClient(cfg, clock=clock, themes_path=THEMES_PATH)
        eng = Engine(cfg, client)

        for _ in range(3):
            eng._api_fail(RuntimeError("네트워크 오류"))
        check("연속 실패 N회에 degraded", eng.degraded)

        eng._api_ok()
        check("회복되면 해제", not eng.degraded)


# ━━ 실매매 가드 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_never_sell_unowned() -> None:
    print("\n== 매수 기록 없는 종목은 절대 매도 금지 ==")
    from daytrader.broker import LiveBroker, NotOwnedError, PaperBroker

    # ── PaperBroker ──
    cfg = _live_cfg()
    cfg.mode = "sim"
    broker = PaperBroker(cfg, None, starting_cash=10_000_000)

    try:
        broker.sell("005930", 10, 70000)
        check("★★★ PaperBroker - 산 적 없는 종목 매도 거부", False, "거부되지 않고 팔림")
    except NotOwnedError:
        check("★★★ PaperBroker - 산 적 없는 종목 매도 거부", True)

    fill = broker.buy("005930", 10, 70000)
    check("직접 산 종목은 매수 성공", fill.ok)
    sell_fill = broker.sell("005930", 10, 71000)
    check("직접 산 종목은 정상 매도됨", sell_fill.ok)

    try:
        broker.sell("005930", 10, 71000)
        check("★이미 청산된 포지션 재매도 거부(PaperBroker)", False)
    except NotOwnedError:
        check("★이미 청산된 포지션 재매도 거부(PaperBroker)", True)

    # ── LiveBroker (실제 create_order 호출 여부까지 확인) ──
    client = FakeToss()
    live_broker = LiveBroker(_live_cfg(), client, _dummy_book())
    # ★ 계좌엔 실제로 000660(하이닉스)이 있을 수 있지만, 이 브로커는 산 적이 없다.
    client.holdings_data = [{"symbol": "000660", "quantity": 5, "name": "SK하이닉스"}]

    orders_before = len(client.orders)
    try:
        live_broker.sell("000660", 5, 150000)
        check("★★★ LiveBroker - 산 적 없는 종목 매도 거부", False, "거부되지 않고 팔림")
    except NotOwnedError:
        check("★★★ LiveBroker - 산 적 없는 종목 매도 거부", True)
    check("실제 매도 주문(create_order)이 호출된 적 없음", len(client.orders) == orders_before)

    buy_fill = live_broker.buy("005930", 10, 70000)
    check("LiveBroker로 직접 산 종목은 매수 성공", buy_fill.ok)
    sell_fill2 = live_broker.sell("005930", buy_fill.quantity, 71000)
    check("LiveBroker로 직접 산 종목은 정상 매도됨", sell_fill2.ok)
    check("이번엔 실제로 매도 주문이 호출됨", any(o["side"] == "SELL" for o in client.orders))


def test_live_guard() -> None:
    print("\n== 실매매 가드 ==")
    from daytrader.broker import LiveBroker

    cfg = _live_cfg()
    cfg.mode = "sim"
    try:
        LiveBroker(cfg, FakeToss(), _dummy_book())
        check("★실거래 모드 아니면 LiveBroker 생성 거부", False)
    except RuntimeError:
        check("★실거래 모드 아니면 LiveBroker 생성 거부", True)


def test_config_save_locked_per_market() -> None:
    """★★★ "국내주식 거래 중일 때 국내 설정만 잠기고 암호화폐·해외주식
    설정은 그대로 할 수 있어야 한다"는 요청을 검증한다. 예전엔
    runner.running 하나만 보고 저장 전체를 막아서, 암호화폐만 거래
    중이어도 국내 설정을 못 고치는 등 실제로 거래 중인 시장과 무관한
    설정까지 막혔었다.
    """
    print("\n== 설정 저장이 시장별로 세분화되어 잠김 ==")
    import asyncio
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server

    tmpdir = _tempfile.mkdtemp()
    orig_config_path = server.CONFIG_PATH
    orig_runner = server.runner
    orig_crypto_engine = server._crypto_engine
    orig_overseas_engine = server._overseas_engine
    try:
        server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)

        class FakeRunner:
            running = True
            error = None
        server.runner = FakeRunner()
        server._crypto_engine = None
        server._overseas_engine = None

        async def scenario():
            cfg_data = asyncio.get_event_loop()
            data = await server.get_config()
            check("locked.domestic == True", data["locked"]["domestic"] is True)
            check("locked.crypto == False", data["locked"]["crypto"] is False)
            check("locked.overseas == False", data["locked"]["overseas"] is False)

            try:
                await server.post_config({"crypto": {"k": 0.66}})
                check("국내 거래 중에도 암호화폐 설정 저장은 성공", True)
            except Exception as exc:
                check("국내 거래 중에도 암호화폐 설정 저장은 성공", False, str(exc))

            try:
                await server.post_config({"risk": {"stop_loss_pct": 0.05}})
                check("★★★ 국내 거래 중에는 국내 설정(risk) 저장이 거부됨", False, "거부됐어야 하는데 통과함")
            except Exception:
                check("★★★ 국내 거래 중에는 국내 설정(risk) 저장이 거부됨", True)

        asyncio.run(scenario())
    finally:
        server.CONFIG_PATH = orig_config_path
        server.runner = orig_runner
        server._crypto_engine = orig_crypto_engine
        server._overseas_engine = orig_overseas_engine


def test_paper_mode_is_not_restricted_by_cash() -> None:
    """★★★ "모의매매는 제한하지 말고 실거래만 제한하면 된다"는 요청 -
    모의매매(web/paper)는 실제 돈이 안 나가니 배정금액을 계좌 잔고보다
    크게 잡아도 자유롭게 전략을 테스트할 수 있어야 한다. 이 제한은
    실거래(preflight)에만 있어야 한다.
    """
    print("\n== 모의매매는 배정금액이 계좌 잔고를 초과해도 제한 없이 시작됨 ==")
    import asyncio
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server

    tmpdir = _tempfile.mkdtemp()
    orig_config_path = server.CONFIG_PATH
    orig_get_client = server.get_client
    orig_runner = server.runner
    try:
        server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)

        import yaml
        raw = yaml.safe_load(open(server.CONFIG_PATH, encoding="utf-8"))
        raw["mode"] = "paper"
        raw["capital"]["allocation"] = 5_000_000
        yaml.dump(raw, open(server.CONFIG_PATH, "w", encoding="utf-8"), allow_unicode=True)

        class LowCashClient:
            def buying_power(self):
                return {"cash": 1_000_000}  # 배정금액(500만)보다 훨씬 적은 잔고(100만)

        server.get_client = lambda force_new=False: LowCashClient()

        from daytrader.runner import EngineRunner, LogBuffer
        server.runner = EngineRunner(LogBuffer())

        async def scenario():
            try:
                result = await server.engine_start(server.EngineStartIn())
                check("★★★ 배정금액이 잔고를 초과해도 모의매매는 제한 없이 시작됨", result.get("ok") is True)
            except Exception as exc:
                check("★★★ 배정금액이 잔고를 초과해도 모의매매는 제한 없이 시작됨", False, str(exc))
            await asyncio.sleep(0.3)
            server.runner.stop(close_positions=False)

        asyncio.run(scenario())
    finally:
        server.CONFIG_PATH = orig_config_path
        server.get_client = orig_get_client
        server.runner = orig_runner


def test_account_holdings_uses_real_api_regardless_of_engine_mode() -> None:
    """★★★ 실제로 겪은 버그 - "내 계좌 현황"이 get_client() 를 그대로
    썼는데, 이 함수는 국내주식 엔진 모드가 sim/replay 면 실제 토스 API가
    아니라 SimClient(가상 클라이언트)를 돌려준다. 그 결과 시뮬레이션
    모드로 설정해 두면 계좌 현황 화면에 실제 계좌 대신 텅 빈 시뮬레이터
    데이터가 나왔다("연계 테스트만 있고 실제 계좌 현황이 안 나온다"는
    문의의 정체). 이 라우트는 엔진 모드와 완전히 무관하게 항상 진짜
    API를 써야 한다.
    """
    print("\n== 계좌 현황은 국내주식 엔진이 sim 모드여도 실제 토스 API를 씀 ==")
    import asyncio
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server
    import daytrader.router as router_mod

    tmpdir = _tempfile.mkdtemp()
    orig_config_path = server.CONFIG_PATH
    orig_build_router = router_mod.build_router
    try:
        server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)

        import yaml
        raw = yaml.safe_load(open(server.CONFIG_PATH, encoding="utf-8"))
        raw["mode"] = "sim"  # ★ 국내주식 엔진 모드가 sim 이어도
        yaml.dump(raw, open(server.CONFIG_PATH, "w", encoding="utf-8"), allow_unicode=True)
        os.environ["TOSS_CLIENT_ID"] = "fake"
        os.environ["TOSS_CLIENT_SECRET"] = "fake"

        call_log = []

        class FakeRealTossClient:
            def resolve_account(self):
                call_log.append("resolve_account")
                return 1

            def holdings(self):
                call_log.append("holdings")
                return {"items": [{
                    "symbol": "005930", "name": "삼성전자", "quantity": 10,
                    "averagePurchasePrice": 70000, "lastPrice": 75000,
                    "profitLoss": {"amount": 50000, "rate": 0.0714},
                }]}

            def buying_power(self, currency="KRW"):
                call_log.append(f"buying_power({currency})")
                return {"cash": 1_200_000}

        router_mod.build_router = lambda cfg: FakeRealTossClient()

        async def scenario():
            result = server.get_account_holdings()
            check("sim 모드여도 실제(가짜 아님) 토스 데이터가 나옴",
                  result["rows"] and result["rows"][0]["symbol"] == "005930")
            check("현금 잔고도 실제 API에서 정확히 옴", result["cash_krw"] == 1_200_000)
            check("resolve_account/holdings/buying_power 모두 호출됨",
                  call_log == ["resolve_account", "holdings", "buying_power(KRW)"])

        asyncio.run(scenario())
    finally:
        server.CONFIG_PATH = orig_config_path
        router_mod.build_router = orig_build_router
        os.environ.pop("TOSS_CLIENT_ID", None)
        os.environ.pop("TOSS_CLIENT_SECRET", None)


def test_account_holdings_splits_domestic_overseas_with_profit() -> None:
    """★★★ "국내계좌와 해외계좌로 구분하고 수익금액도 산출해달라"는 요청 -
    marketCountry 기준으로 domestic_rows/overseas_rows 를 나누고, 각각
    수익금액 합계(domestic_profit_total/overseas_profit_total)를 정확히
    계산해야 한다.
    """
    print("\n== 계좌 현황이 국내/해외로 구분되고 수익금액이 정확히 계산됨 ==")
    import asyncio
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server
    import daytrader.router as router_mod

    tmpdir = _tempfile.mkdtemp()
    orig_config_path = server.CONFIG_PATH
    orig_build_router = router_mod.build_router
    try:
        server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)
        os.environ["TOSS_CLIENT_ID"] = "fake"
        os.environ["TOSS_CLIENT_SECRET"] = "fake"

        class FakeRealTossClient:
            def resolve_account(self):
                return 1

            def holdings(self):
                return {"items": [
                    {"symbol": "005930", "name": "삼성전자", "marketCountry": "KR", "quantity": 10,
                     "averagePurchasePrice": 70000, "lastPrice": 75000, "profitLoss": {"amount": 50000, "rate": 0.0714}},
                    {"symbol": "AAPL", "name": "애플", "marketCountry": "US", "quantity": 5,
                     "averagePurchasePrice": 150.0, "lastPrice": 160.0, "profitLoss": {"amount": 50.0, "rate": 0.0667}},
                ]}

            def buying_power(self, currency="KRW"):
                return {"cash": 1_200_000}

        router_mod.build_router = lambda cfg: FakeRealTossClient()

        async def scenario():
            result = server.get_account_holdings()
            check("국내 종목이 domestic_rows 에 정확히 분류됨",
                  len(result["domestic_rows"]) == 1 and result["domestic_rows"][0]["symbol"] == "005930")
            check("해외 종목이 overseas_rows 에 정확히 분류됨",
                  len(result["overseas_rows"]) == 1 and result["overseas_rows"][0]["symbol"] == "AAPL")
            check("국내 수익금액 합계 정확함", result["domestic_profit_total"] == 50000)
            check("해외 수익금액 합계 정확함", result["overseas_profit_total"] == 50.0)

        asyncio.run(scenario())
    finally:
        server.CONFIG_PATH = orig_config_path
        router_mod.build_router = orig_build_router
        os.environ.pop("TOSS_CLIENT_ID", None)
        os.environ.pop("TOSS_CLIENT_SECRET", None)


def test_crypto_account_holdings() -> None:
    """★★★ "빗썸 API 연결 시 암호화폐 계좌현황도 똑같이 만들어달라"는
    요청 - 국내/해외 계좌와 같은 형태(매수가·수량·현재가·수익률·
    수익금액)로 빗썸 실제 계좌를 보여줘야 한다. 잔고 0인 코인은 제외하고,
    KRW 는 현금 잔고로 별도 취급해야 한다.
    """
    print("\n== 빗썸 API 연결 시 암호화폐 계좌 현황이 같은 형태로 나옴 ==")
    import asyncio
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server
    import daytrader.bithumb_api as bithumb_mod

    tmpdir = _tempfile.mkdtemp()
    orig_config_path = server.CONFIG_PATH
    orig_bithumb_client = bithumb_mod.BithumbClient
    try:
        server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)
        os.environ["BITHUMB_ACCESS_KEY"] = "fake"
        os.environ["BITHUMB_SECRET_KEY"] = "fake"

        class FakeBithumbClient:
            def accounts(self):
                return [
                    {"currency": "KRW", "balance": "500000"},
                    {"currency": "BTC", "balance": "0.01", "avg_buy_price": "95000000"},
                    {"currency": "ETH", "balance": "0", "avg_buy_price": "4000000"},
                ]

            def ticker(self, markets):
                return [{"market": "KRW-BTC", "trade_price": 100000000}]

        bithumb_mod.BithumbClient = lambda access, secret: FakeBithumbClient()

        async def scenario():
            result = server.get_account_crypto_holdings()
            check("KRW은 현금 잔고로 정확히 분리됨", result["cash_krw"] == 500000)
            check("잔고 0인 코인(ETH)은 제외됨", len(result["rows"]) == 1)
            check("BTC가 정확히 조회됨", result["rows"][0]["symbol"] == "KRW-BTC")
            expected_profit = (100_000_000 - 95_000_000) * 0.01
            check("수익금액이 정확히 계산됨",
                  result["rows"][0]["profit_loss_amount"] is not None
                  and abs(result["rows"][0]["profit_loss_amount"] - expected_profit) < 0.01)

        asyncio.run(scenario())
    finally:
        server.CONFIG_PATH = orig_config_path
        bithumb_mod.BithumbClient = orig_bithumb_client
        os.environ.pop("BITHUMB_ACCESS_KEY", None)
        os.environ.pop("BITHUMB_SECRET_KEY", None)


def test_account_holdings_survives_string_numbers() -> None:
    """★★★ 실제로 겪은 버그("unsupported operand types") - 증권사·거래소
    API 는 정밀도 보존을 위해 숫자를 문자열로 주는 경우가 흔하다(토스의
    BigDecimal 필드가 대표적). 이 상태로 합계·수익 계산을 하면
    TypeError(문자열 + 숫자)로 죽는다. 모든 숫자 필드를 안전하게
    정규화해서 이 문제가 재발하지 않아야 한다.
    """
    print("\n== 계좌 API가 숫자를 문자열로 줘도 죽지 않고 정확히 계산됨 ==")
    import asyncio
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server
    import daytrader.router as router_mod

    tmpdir = _tempfile.mkdtemp()
    orig_config_path = server.CONFIG_PATH
    orig_build_router = router_mod.build_router
    try:
        server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)
        os.environ["TOSS_CLIENT_ID"] = "fake"
        os.environ["TOSS_CLIENT_SECRET"] = "fake"

        class FakeStringyTossClient:
            """★ 모든 숫자 필드를 문자열로 준다 - 실제 BigDecimal 직렬화 재현."""
            def resolve_account(self):
                return 1

            def holdings(self):
                return {"items": [
                    {"symbol": "005930", "name": "삼성전자", "marketCountry": "KR", "quantity": "10",
                     "averagePurchasePrice": "70000.0", "lastPrice": "75000.0",
                     "profitLoss": {"amount": "50000.0", "rate": "0.0714"}},
                    {"symbol": "AAPL", "name": "애플", "marketCountry": "US", "quantity": "5",
                     "averagePurchasePrice": "150.00", "lastPrice": "160.00",
                     "profitLoss": {"amount": "50.00", "rate": "0.0667"}},
                ]}

            def buying_power(self, currency="KRW"):
                return {"cash": "1200000"}

        router_mod.build_router = lambda cfg: FakeStringyTossClient()

        async def scenario():
            try:
                result = server.get_account_holdings()
                check("예외 없이 정상 처리됨(TypeError 재발 안 함)", True)
            except TypeError as exc:
                check("예외 없이 정상 처리됨(TypeError 재발 안 함)", False, str(exc))
                return
            check("국내 수익금액 합계가 숫자로 정확히 계산됨", result["domestic_profit_total"] == 50000.0)
            check("해외 수익금액 합계가 숫자로 정확히 계산됨", result["overseas_profit_total"] == 50.0)
            check("현금 잔고도 숫자로 정규화됨", isinstance(result["cash_krw"], float) and result["cash_krw"] == 1_200_000.0)

        asyncio.run(scenario())
    finally:
        server.CONFIG_PATH = orig_config_path
        router_mod.build_router = orig_build_router
        os.environ.pop("TOSS_CLIENT_ID", None)
        os.environ.pop("TOSS_CLIENT_SECRET", None)


def test_crypto_account_holdings_survives_string_ticker() -> None:
    """★★★ 같은 종류의 버그 - 빗썸 ticker() 의 trade_price 가 문자열로
    와도 수익금액 계산이 죽으면 안 된다.
    """
    print("\n== 빗썸 ticker가 문자열을 줘도 수익금액이 정확히 계산됨 ==")
    import asyncio
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server
    import daytrader.bithumb_api as bithumb_mod

    tmpdir = _tempfile.mkdtemp()
    orig_config_path = server.CONFIG_PATH
    orig_bithumb_client = bithumb_mod.BithumbClient
    try:
        server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)
        os.environ["BITHUMB_ACCESS_KEY"] = "fake"
        os.environ["BITHUMB_SECRET_KEY"] = "fake"

        class FakeStringyBithumbClient:
            def accounts(self):
                return [
                    {"currency": "KRW", "balance": "500000"},
                    {"currency": "BTC", "balance": "0.01", "avg_buy_price": "95000000"},
                ]

            def ticker(self, markets):
                return [{"market": "KRW-BTC", "trade_price": "100000000"}]  # ★ 문자열

        bithumb_mod.BithumbClient = lambda access, secret: FakeStringyBithumbClient()

        async def scenario():
            try:
                result = server.get_account_crypto_holdings()
                check("예외 없이 정상 처리됨(TypeError 재발 안 함)", True)
            except TypeError as exc:
                check("예외 없이 정상 처리됨(TypeError 재발 안 함)", False, str(exc))
                return
            expected_profit = (100_000_000 - 95_000_000) * 0.01
            check("수익금액이 정확히 계산됨",
                  abs(result["rows"][0]["profit_loss_amount"] - expected_profit) < 0.01)

        asyncio.run(scenario())
    finally:
        server.CONFIG_PATH = orig_config_path
        bithumb_mod.BithumbClient = orig_bithumb_client
        os.environ.pop("BITHUMB_ACCESS_KEY", None)
        os.environ.pop("BITHUMB_SECRET_KEY", None)


def test_overseas_status_exposes_auto_select_when_engine_off() -> None:
    """★★★ "미국주식 자동선정 미해결" - 엔진이 아직 안 돌고 있을 때의
    /api/overseas/status 폴백이 auto_select 여부를 전혀 반영하지 않고
    있었다. 화면이 "자동 선정이 켜져 있다"는 사실을 엔진 시작 전에도
    정확히 알 수 있어야 한다.
    """
    print("\n== 엔진이 꺼져 있어도 /api/overseas/status가 auto_select를 정확히 노출 ==")
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server

    tmpdir = _tempfile.mkdtemp()
    orig_config_path = server.CONFIG_PATH
    orig_engine = server._overseas_engine
    try:
        server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)
        server._overseas_engine = None

        import yaml
        raw = yaml.safe_load(open(server.CONFIG_PATH, encoding="utf-8"))
        raw["overseas"]["auto_select"] = True
        yaml.dump(raw, open(server.CONFIG_PATH, "w", encoding="utf-8"), allow_unicode=True)

        # ★★ [8-1] overseas_status() 는 이제 일반 def(동기) 라우트다(FastAPI 가
        # 스레드풀에서 돌린다) - 더 이상 코루틴이 아니라서 await 없이 직접 부른다.
        status = server.overseas_status()
        check("엔진 꺼진 상태에서도 auto_select=True가 정확히 노출됨", status.get("auto_select") is True)
    finally:
        server.CONFIG_PATH = orig_config_path
        server._overseas_engine = orig_engine


def test_overseas_ticker_uses_auto_selected_watchlist() -> None:
    """★★★ "미국주식 자동선정 미해결" - /api/overseas/ticker(종목선정
    화면의 시세 조회)가 auto_select 결과를 무시하고 항상 고정
    cfg.overseas.watchlist 만 조회하고 있었다. 자동으로 뽑힌 종목의
    실시간 시세를 종목선정 화면에서 아예 볼 수 없었던 문제다.
    """
    print("\n== /api/overseas/ticker가 고정 목록이 아니라 자동선정 결과를 조회함 ==")
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server
    import daytrader.market as market_mod
    from daytrader.config import load_config
    from daytrader.overseas_engine import OverseasEngine

    tmpdir = _tempfile.mkdtemp()
    orig_config_path = server.CONFIG_PATH
    orig_engine = server._overseas_engine
    orig_fetch = market_mod._fetch_yahoo_session_group
    try:
        server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)

        cfg = load_config(server.CONFIG_PATH)
        cfg.state_dir = _tempfile.mkdtemp()
        cfg.overseas.auto_select = True
        cfg.overseas.auto_select_count = 2
        cfg.overseas.watchlist = ["AAPL", "NVDA", "MSFT"]  # ★ 관심 종목은 자동선정과 별개로 항상 함께 쓰인다.

        class FakeClient:
            def rankings(self, type, marketCountry, duration, count):
                return [{"symbol": "TSLA"}, {"symbol": "GOOGL"}]

        engine = OverseasEngine(cfg, client=FakeClient())
        engine._effective_watchlist()  # ★ 자동 선정을 미리 한 번 트리거.
        server._overseas_engine = engine

        captured = {}

        def fake_fetch(sess, items, now):
            captured["items"] = items
            return []

        market_mod._fetch_yahoo_session_group = fake_fetch

        # ★★ [8-1] overseas_ticker() 도 이제 일반 def(동기) 라우트다 - await 없이 직접 부른다.
        server.overseas_ticker()
        symbols = [t[0] for t in captured.get("items", [])]
        check("★★★ ticker가 자동선정 결과(TSLA, GOOGL)를 조회함", symbols[:2] == ["TSLA", "GOOGL"], str(symbols))
        # ★ 직접 추가한 관심 종목은 자동선정·테마와 별개로 항상 거래 대상이라 함께 조회한다.
        check("관심 종목(AAPL 등)도 함께 조회함", all(x in symbols for x in ("AAPL", "NVDA", "MSFT")), str(symbols))
    finally:
        server.CONFIG_PATH = orig_config_path
        server._overseas_engine = orig_engine
        market_mod._fetch_yahoo_session_group = orig_fetch


def test_journal_shows_only_trades() -> None:
    """★★★ "매매일지에 매매한 이력 외에 종목선정 같은 항목은 보이지 않도록" -
    journal 파일에는 매수·매도 말고도 종목선정 거절(reject)·세션(session)·
    중단(halt)·계좌대조(reconcile) 가 함께 쌓인다. 매매일지 화면에는
    실제 매매만 나와야 하되, 기록 자체는 지우지 않고 all_kinds=True 로
    전부 볼 수 있어야 한다(진단·감사용).
    """
    print("\n== 매매일지에는 매수·매도만 나오고 종목선정 등은 안 나옴 ==")
    import asyncio
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server
    from daytrader.journal import Journal

    from daytrader import config as config_mod

    tmpdir = _tempfile.mkdtemp()
    state_dir = os.path.join(tmpdir, "state")
    orig_config_path = server.CONFIG_PATH
    orig_app_dir = config_mod.app_dir
    try:
        server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)
        # ★ [2-3] state_dir 은 이제 app_dir() 밖(절대경로·..)을 가리킬 수 없다 - 여기서는
        # config.yaml 에는 상대경로("state")만 적고, app_dir() 자체를 이 tmpdir 로 바꿔
        # 격리한다(실제 서버는 app_dir() 을 바꾸지 않는다 - 테스트 전용 격리 방법이다).
        config_mod.app_dir = lambda: tmpdir

        import yaml
        raw = yaml.safe_load(open(server.CONFIG_PATH, encoding="utf-8"))
        raw["state_dir"] = "state"
        yaml.dump(raw, open(server.CONFIG_PATH, "w", encoding="utf-8"), allow_unicode=True)

        j = Journal(state_dir, mode="sim")
        j.write("buy", "삼성전자 매수", symbol="005930", name="삼성전자")
        j.log("reject", symbol="000660", theme="반도체", reason="거래대금 미달")
        j.write("session", "[사전점검] 계좌 목록: 1개 계좌")
        j.write("halt", "보유 한도 도달")
        j.write("reconcile", "005930 유령 포지션 정리")
        j.write("sell", "삼성전자 매도", symbol="005930", name="삼성전자")

        async def scenario():
            result = await server.get_journal(group="virtual")
            kinds = set(r.get("kind") for r in result["rows"])
            check("★★★ 매수·매도만 나옴", kinds == {"buy", "sell"}, str(sorted(kinds)))
            check("종목선정 거절(reject)은 안 나옴", "reject" not in kinds)
            check("세션·중단·계좌대조도 안 나옴", not ({"session", "halt", "reconcile"} & kinds))

            full = await server.get_journal(group="virtual", all_kinds=True)
            full_kinds = set(r.get("kind") for r in full["rows"])
            check("all_kinds=True 면 전부 볼 수 있음(기록은 안 지워짐)",
                  {"reject", "session", "halt", "reconcile"} <= full_kinds)

        asyncio.run(scenario())
    finally:
        server.CONFIG_PATH = orig_config_path
        config_mod.app_dir = orig_app_dir


# ━━ 조건부 오버나이트 - 재시작(2일차) [9-1] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_restart_with_carried_position() -> None:
    """(6) 오버나이트로 넘긴 포지션을 들고 재시작해도(2일차 아침) preflight 가
    막지 않고, 시간손절이 개장하자마자 잘못 걸리지도 않는다.
    ★★★ "재시작이 정상적인 사용법인데 넘긴 포지션 때문에 실거래를 아예 시작
    못 하면" - preflight()가 보유 종목과 연결된 조건부 주문(서버 OCO)까지
    고아로 오인해 차단하면 안 된다(이미 [1-2]로 고쳐짐 - 여기선 "오버나이트로
    넘긴" 포지션 특유의 carry_date 가 있어도 여전히 통과하는지 함께 본다).
    """
    print("\n== 조건부 오버나이트: 2일차 재시작 - preflight 통과 · 시간손절 오탐 없음 ==")
    from daytrader.broker import Position
    from daytrader.ledger import Ledger
    from daytrader.safety import preflight
    from daytrader.timeutil import now_kst

    with tempfile.TemporaryDirectory() as d:
        cfg = _live_cfg()
        cfg.state_dir = d
        ledger = Ledger(d)

        # 어제 오버나이트로 넘긴 포지션 - 재시작 시 daily_state.json 에서 그대로 복원된
        # 상태를 흉내낸다(carry_date 가 어제 날짜로 찍혀 있고, 서버 OCO 는 재시작
        # 시점에 [9-1]의 _rearm_carried_oco() 가 이미 오늘 날짜로 다시 걸어 뒀다고 본다).
        pos = Position(
            symbol="005930", name="삼성전자", theme="t", quantity=10, entry_price=70000,
            entry_time=now_kst(), peak_price=73000, oco_id="oco-carried", entry_volume=0,
            verdict_id="v", why="", technique="breakout", carry_date="2026-09-08",
        )
        positions = {"005930": pos}

        client = FakeToss()
        client.open_orders_data = []
        client.conditional_orders_data = [
            {"conditionalOrderId": "oco-carried", "status": "OPEN", "symbol": "005930"}
        ]

        result = preflight(cfg, client, ledger=ledger, positions=positions)
        check(
            "★오버나이트로 넘긴 포지션이 있어도 preflight 통과",
            result["ok"], "; ".join(c["detail"] for c in result["blocking"]),
        )

        # 시간손절: 밤새 쉰 시간까지 그대로 더해진 held_minutes(예: 하루 가까이)로
        # 개장 직후 평가해도, carry_date 가 있으면 시간손절 대상에서 빠져야 한다.
        from types import SimpleNamespace
        from daytrader.playbook import TimeStopExit

        exit_ = TimeStopExit(cfg, cfg.strategy.p("time_stop"))
        ctx = SimpleNamespace(held_minutes=1200.0, force_close=False, now=now_kst(), prev_verdict=None, cfg=cfg)
        verdict = exit_.evaluate(pos, [], pos.peak_price, ctx)
        check(
            "★밤새 쉰 시간이 더해져도 시간손절이 걸리지 않음(다음 장마감에 정리됨)",
            not verdict.ok, str(verdict.to_dict() if hasattr(verdict, "to_dict") else verdict),
        )

        # 대조군: carry_date 가 없는 보통 포지션이었다면 같은 held_minutes 에서
        # (max_hold_minutes 를 넘겼다면) 정상적으로 시간손절이 걸려야 한다 - 이
        # 예외가 "전부 다 끄는" 게 아니라 오버나이트 포지션에만 좁게 적용됨을 함께 본다.
        pos_normal = Position(
            symbol="000660", name="SK하이닉스", theme="t", quantity=10, entry_price=70000,
            entry_time=now_kst(), peak_price=73000, oco_id=None, entry_volume=0,
            verdict_id="v", why="", technique="breakout",
        )
        held_minutes = float(cfg.exit.max_hold_minutes) + 10
        ctx_normal = SimpleNamespace(held_minutes=held_minutes, force_close=False, now=now_kst(), prev_verdict=None, cfg=cfg)
        verdict_normal = exit_.evaluate(pos_normal, [], pos_normal.peak_price, ctx_normal)
        check(
            "일반 포지션은 max_hold_minutes 를 넘기면 그대로 시간손절이 걸림",
            verdict_normal.ok, str(verdict_normal.to_dict() if hasattr(verdict_normal, "to_dict") else verdict_normal),
        )


def main() -> None:
    tests = [
        test_idempotency, test_oco_is_open, test_oco_cancel_fail, test_cash_zero_not_treated_as_missing, test_reconcile,
        test_cleanup_orphans, test_preflight, test_degraded_mode, test_never_sell_unowned, test_live_guard,
        test_config_save_locked_per_market, test_paper_mode_is_not_restricted_by_cash,
        test_account_holdings_uses_real_api_regardless_of_engine_mode,
        test_account_holdings_splits_domestic_overseas_with_profit, test_crypto_account_holdings,
        test_account_holdings_survives_string_numbers, test_crypto_account_holdings_survives_string_ticker,
        test_overseas_status_exposes_auto_select_when_engine_off, test_overseas_ticker_uses_auto_selected_watchlist,
        test_journal_shows_only_trades, test_restart_with_carried_position,
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
