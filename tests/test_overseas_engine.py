"""overseas_engine.py 오프라인 테스트. `python tests/test_overseas_engine.py` 로 실행한다."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader.config import load_config  # noqa: E402
from daytrader.overseas_broker import NotOwnedError  # noqa: E402
from daytrader.overseas_engine import OverseasEngine, is_us_market_open, us_phase  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config.yaml")

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


class FakeTossClient:
    """★ 국내주식용 TossClient 와 정확히 같은 인터페이스를 흉내낸다 -
    "계좌도 동일"이라는 설계를 검증하려면 진짜 TossClient 메서드 이름을
    그대로 써야 한다.
    """

    def __init__(self, breakout: bool = True):
        # ★ candles() 의 돌파 봉 종가(p*1.02=102.0)와 맞춘다 - 실시간가(prices())와 분봉
        # 종가가 서로 딴판이면 진입 직전 교차 검증(overseas_engine.py, "해외주식 진입가가
        # 이상해" 버그 수정)에 걸려 매수 자체가 보류된다.
        self.price = 102.0
        self.breakout = breakout
        self.orders: list = []

    def buying_power(self, currency="KRW"):
        return {"cash": 10_000_000}

    def prices(self, symbols):
        return [{"symbol": symbols[0], "price": self.price}]

    def candles(self, symbol, interval, count, before=None):
        rows = []
        p = 100.0
        n = count if self.breakout else count
        for i in range(n - 1):
            rows.append({"timestamp": str(i), "open": p, "high": p * 1.005, "low": p * 0.995, "close": p, "volume": 500_000})
        if self.breakout:
            rows.append({"timestamp": "last", "open": p, "high": p * 1.03, "low": p * 0.99, "close": p * 1.02, "volume": 2_000_000})
        else:
            rows.append({"timestamp": "last", "open": p, "high": p * 1.002, "low": p * 0.998, "close": p, "volume": 500_000})
        return rows

    def create_order(self, symbol, side, orderType, quantity, price=None, timeInForce="DAY", clientOrderId=None):
        self.orders.append((symbol, side, quantity))
        return {"orderId": "fake-order-id"}

    def rankings(self, type, marketCountry, duration, count):
        # ★ 국내주식(screener.py)과 같은 두 랭킹 종류를 흉내낸다.
        if type == "MARKET_TRADING_AMOUNT":
            return [{"symbol": "AAPL"}, {"symbol": "MSFT"}]
        return [{"symbol": "NVDA"}, {"symbol": "AAPL"}]


class FakeUncertainTossClient:
    """★★★ [1-5] network-uncertain 타임아웃을 재현하는 가짜 클라이언트 -
    fail_create=True 면 create_order() 가 예외를 던지지만 주문은 이미
    "접수된 것"으로 처리한다(daytrader/tossapi.py 의 network-uncertain
    재현, test_live_safety.py 의 FakeToss 와 같은 설계).
    findable=False 면 get_orders() 조회로도 끝내 못 찾는 상황(진짜로
    접수 여부를 모르는 상태)을 재현한다.
    """

    account_seq = 0

    def __init__(self, fail_create: bool = True, findable: bool = True):
        self.orders: list = []
        self.fail_create = fail_create
        self.findable = findable
        self._seq = 0

    def create_order(self, symbol, side, orderType, quantity, price=None, timeInForce="DAY", clientOrderId=None):
        self._seq += 1
        order_id = f"oid-{self._seq}"
        self.orders.append({
            "orderId": order_id, "clientOrderId": clientOrderId, "symbol": symbol,
            "side": side, "quantity": quantity,
        })
        if self.fail_create:
            from daytrader.tossapi import TossApiError
            raise TossApiError(0, "network-uncertain", "연결 불확실")
        return {"orderId": order_id}

    def get_orders(self, **kwargs):
        if not self.findable:
            return []
        coid = kwargs.get("clientOrderId")
        if coid:
            return [o for o in self.orders if o.get("clientOrderId") == coid]
        return self.orders


def _cfg(tmp_dir: str, watchlist: list, mode: str = "paper"):
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = tmp_dir
    cfg.overseas.watchlist = watchlist
    cfg.overseas.mode = mode
    cfg.strategy.entry_order = ["breakout"]
    return cfg


def test_market_hours() -> None:
    print("\n== 미국 정규장 시간 판단(서머타임 자동 반영) ==")
    ny_open = datetime(2026, 9, 9, 10, 0, tzinfo=ZoneInfo("America/New_York"))  # 수요일 오전
    check("평일 오전 10시(뉴욕) - 장중", is_us_market_open(ny_open) is True)

    ny_closed = datetime(2026, 9, 9, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    check("평일 저녁 8시(뉴욕) - 장마감", is_us_market_open(ny_closed) is False)

    ny_weekend = datetime(2026, 9, 12, 10, 0, tzinfo=ZoneInfo("America/New_York"))  # 토요일
    check("주말 - 휴장", is_us_market_open(ny_weekend) is False)


def test_reuses_same_account_and_playbook() -> None:
    print("\n== ★ 계좌·매매기법을 국내주식과 완전히 동일하게 재사용 ==")
    d = tempfile.mkdtemp()
    client = FakeTossClient()
    cfg = _cfg(d, ["AAPL"])
    engine = OverseasEngine(cfg, client=client)
    check("같은 TossClient 인스턴스를 그대로 씀(별도 계좌 안 만듦)", engine.client is client)
    check("Playbook 인스턴스가 존재함(국내주식과 같은 클래스)", engine.playbook is not None)
    check("국내주식 설정(entry_order)을 그대로 따름",
          [t.key for t in engine.playbook.entries] == cfg.strategy.entry_order)


def test_buy_then_take_profit() -> None:
    print("\n== 매수 -> 익절 매도 전체 사이클(장중 직접 호출로 시간 의존성 제거) ==")
    d = tempfile.mkdtemp()
    client = FakeTossClient(breakout=True)
    cfg = _cfg(d, ["AAPL"])
    engine = OverseasEngine(cfg, client=client)

    check("매수 전 보유 없음", not engine.state.book.owns("AAPL"))
    engine._try_entry("AAPL")
    check("돌파 신호로 매수됨", engine.state.book.owns("AAPL"))
    if not engine.state.book.owns("AAPL"):
        return
    pos = engine.state.book.get("AAPL")
    check("진입 기법이 정확히 breakout으로 기록됨", pos.technique == "breakout")

    full_take = pos.entry_price * (1 + cfg.overseas.take_profit_pct + 0.02)  # 익절폭 초과
    client.price = pos.entry_price * (1 + 0.5 * cfg.overseas.take_profit_pct + 0.01)  # 익절폭의 절반 초과
    engine._manage_position("AAPL", pos)
    check("익절 구간에서 1차 분할 매도(일부만 팔고 보유 유지)", engine.state.book.owns("AAPL") and engine.state.book.get("AAPL").scaled_out == 1)
    client.price = full_take
    engine._manage_position("AAPL", engine.state.book.get("AAPL"))
    engine._manage_position("AAPL", engine.state.book.get("AAPL"))
    check("2차까지 나눠 판 뒤 나머지는 추적 손절이 끌고 감", engine.state.book.owns("AAPL") and engine.state.book.get("AAPL").scaled_out == 2)
    client.price = full_take * (1 - cfg.overseas.trailing_pct - 0.01)  # 고점에서 되돌림 -> 추적 손절
    engine._manage_position("AAPL", engine.state.book.get("AAPL"))
    check("추적 손절로 나머지 정리", not engine.state.book.owns("AAPL"))
    check("실거래 주문 API는 호출된 적 없음(모의매매)", len(client.orders) == 0)


def test_web_mode_never_buys() -> None:
    print("\n== 관찰(web) 모드에서는 신호가 나도 실제로 사지 않음 ==")
    d = tempfile.mkdtemp()
    client = FakeTossClient(breakout=True)
    cfg = _cfg(d, ["AAPL"], mode="web")
    engine = OverseasEngine(cfg, client=client)
    engine._try_entry("AAPL")
    check("web 모드에서는 매수 안 함", not engine.state.book.owns("AAPL"))


def test_never_sells_unowned() -> None:
    print("\n== ★★★ 이 엔진이 사지 않은 종목은 절대 매도 거부(모의매매) ==")
    d = tempfile.mkdtemp()
    client = FakeTossClient(breakout=False)
    cfg = _cfg(d, ["MSFT"])
    engine = OverseasEngine(cfg, client=client)
    try:
        engine.broker.sell("MSFT", 300.0, reason="test")
        check("NotOwnedError 로 거부됨", False, "예외 없이 통과됨 - 위험한 버그")
    except NotOwnedError:
        check("NotOwnedError 로 거부됨", True)


def test_never_sells_unowned_live() -> None:
    print("\n== ★★★ 이 엔진이 사지 않은 종목은 절대 매도 거부(실거래) ==")
    d = tempfile.mkdtemp()
    client = FakeTossClient(breakout=False)
    cfg = _cfg(d, ["MSFT"], mode="live")
    cfg.client_id = "fake"
    cfg.client_secret = "fake"
    engine = OverseasEngine(cfg, client=client)
    check("is_live 정상 반영", engine.is_live is True)
    try:
        engine.broker.sell("MSFT", 300.0, reason="test")
        check("실거래에서도 NotOwnedError 로 거부됨", False, "예외 없이 통과됨 - 위험한 버그")
    except NotOwnedError:
        check("실거래에서도 NotOwnedError 로 거부됨", True)
    check("실제 매도 주문 API가 호출된 적 없음", not any(o[1] == "SELL" for o in client.orders))


def test_liquidate_all() -> None:
    print("\n== 전량 청산 후 정지 ==")
    from daytrader.overseas_broker import OverseasPosition
    import time

    d = tempfile.mkdtemp()
    client = FakeTossClient(breakout=False)
    cfg = _cfg(d, ["AAPL", "MSFT"])
    engine = OverseasEngine(cfg, client=client)

    engine.state.book.record_buy(OverseasPosition(
        symbol="AAPL", quantity=10, entry_price=140.0, entry_time=time.time() - 1000, peak_price=140.0,
    ))
    engine.state.book.record_buy(OverseasPosition(
        symbol="MSFT", quantity=5, entry_price=300.0, entry_time=time.time() - 500, peak_price=300.0,
    ))
    count = engine.liquidate_all()
    check("2건 모두 청산됨", count == 2)
    check("보유가 비었음", not engine.state.book.owns("AAPL") and not engine.state.book.owns("MSFT"))
    check("청산 사유가 force_close로 남음", all(c["reason"] == "force_close" for c in engine.state.closed))


def test_state_persistence() -> None:
    print("\n== 상태 저장/복원 ==")
    d = tempfile.mkdtemp()
    client = FakeTossClient(breakout=True)
    cfg = _cfg(d, ["AAPL"])
    engine = OverseasEngine(cfg, client=client)
    engine._try_entry("AAPL")
    check("매수됨", engine.state.book.owns("AAPL"))
    engine.state.save()
    cash_before_restart = engine.broker.cash

    engine2 = OverseasEngine(cfg, client=client, state_path=engine.state.path)
    check("재시작해도 포지션이 유지됨", engine2.state.book.owns("AAPL"))
    # ★★★ 실제로 겪은 버그(국내주식 engine.py 에서 먼저 발견돼 고쳐진 것과 같은 종류) -
    # 재시작마다 현금이 매수 여부와 무관하게 총 투자금액 그대로 초기화돼("대시보드 현금
    # 잔여금액이 안 맞다"), 보유 종목에 묶인 돈과 새로 채워진 현금이 동시에 존재하는 이중
    # 계산이 됐다. 재시작 전후로 현금이 같아야 한다(포지션은 그대로 있고 새로 산 것도
    # 없으므로).
    check("재시작해도 현금이 매수 전 총액으로 되돌아가지 않음(이중 계산 없음)",
          abs(engine2.broker.cash - cash_before_restart) < 1.0,
          f"{cash_before_restart} vs {engine2.broker.cash}")


def test_market_hours_fallback_matches_zoneinfo() -> None:
    print("\n== ★★★ zoneinfo(tzdata) 없어도 안 죽음 - 폴백 계산이 정상 경로와 일치 ==")
    import daytrader.overseas_engine as oe
    from datetime import timezone

    test_cases = [
        datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc),
        datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 21, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc),
    ]
    for now_utc in test_cases:
        normal = oe.is_us_market_open(now_utc)
        oe._NY_TZ = None
        oe._NY_TZ_FAILED = True
        fallback = oe.is_us_market_open(now_utc)
        oe._NY_TZ_FAILED = False
        check(f"{now_utc} 정상/폴백 결과 일치", normal == fallback)


def test_module_import_survives_missing_zoneinfo() -> None:
    print("\n== ★★★ zoneinfo 모듈 자체가 없어도 함수 호출이 죽지 않음 ==")
    import daytrader.overseas_engine as oe
    from datetime import timezone
    import builtins

    oe._NY_TZ = None
    oe._NY_TZ_FAILED = False
    orig_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "zoneinfo":
            raise ModuleNotFoundError("No module named 'zoneinfo' (시뮬레이션)")
        return orig_import(name, *a, **kw)

    builtins.__import__ = fake_import
    try:
        result = oe.is_us_market_open(datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc))
        check("zoneinfo 없어도 정상적으로 True/False 를 반환함(예외 없음)", result is True)
    finally:
        builtins.__import__ = orig_import
        oe._NY_TZ = None
        oe._NY_TZ_FAILED = False


def test_sim_mode_ignores_real_market_hours() -> None:
    """★★★ "시뮬레이션 모드인데 왜 실제 개장 시간과 상관있냐"는 정확한
    지적을 반영한다. web/paper/live 는 실제 시장 시간을 지켜야 하지만,
    sim 은 실제 시각과 무관하게 언제든 매매 로직을 테스트할 수 있어야
    한다(국내주식의 sim 이 가상 시계로 도는 것과 같은 취지).
    """
    print("\n== sim 모드는 실제 미국 장이 닫혀 있어도 정상적으로 매매함 ==")
    d = tempfile.mkdtemp()
    client = FakeTossClient(breakout=True)
    cfg = _cfg(d, ["AAPL"], mode="sim")
    engine = OverseasEngine(cfg, client=client)

    check("snapshot()의 market_open이 sim 모드에서는 항상 True",
          engine.snapshot()["market_open"] is True)

    engine.run_once()
    check("실제 장이 닫혀 있어도(현재 시각과 무관하게) sim 모드에서 run_once()로 매수됨",
          engine.state.book.owns("AAPL"))


def test_manage_position_survives_no_exit_verdict() -> None:
    """★★★ 암호화폐에서 겪은 것과 같은 버그 - Playbook.evaluate_exit() 이
    청산 조건 전부 미충족이면 None 을 반환하는데, 이 None 체크를 빠뜨리면
    막 매수한 직후(흔한 상황)마다 죽는다.
    """
    print("\n== 청산 조건이 전부 미충족(verdict=None)이어도 예외 없이 보유 유지 ==")
    from daytrader.overseas_broker import OverseasPosition
    import time as _time

    d = tempfile.mkdtemp()
    client = FakeTossClient(breakout=False)
    cfg = _cfg(d, ["AAPL"])
    engine = OverseasEngine(cfg, client=client)
    # ★ 이 테스트는 "예외 없이 보유가 유지되는지"만 보는 것이라, 진입가를 client.price 와
    # 맞춰 둔다 - 안 그러면 관리 루프에서 손절로 즉시 청산되어(포지션이 사라져) 이 테스트가
    # 확인하려는 것과 무관한 이유로 실패한다.
    engine.state.book.record_buy(OverseasPosition(
        symbol="AAPL", quantity=10, entry_price=client.price, entry_time=_time.time(),
        peak_price=client.price, technique="breakout", name="AAPL", theme="해외주식",
    ))
    try:
        engine._manage_position("AAPL", engine.state.book.get("AAPL"))
        check("예외 없이 정상 처리됨(AttributeError 재발 안 함)", True)
    except AttributeError as exc:
        check("예외 없이 정상 처리됨(AttributeError 재발 안 함)", False, str(exc))
    check("청산 조건 미충족이니 여전히 보유 중", engine.state.book.owns("AAPL"))


def test_auto_select_picks_from_rankings() -> None:
    """★★★ "미국주식 선정도 국내주식과 동일하게 자동으로 선정" - auto_select
    가 켜져 있으면 고정 watchlist 대신 거래대금·급등 랭킹을 합쳐 상위
    종목을 매일 자동으로 뽑아야 한다.
    """
    print("\n== 자동 선정이 두 랭킹을 합쳐 상위 종목을 정확히 뽑음 ==")
    d = tempfile.mkdtemp()
    client = FakeTossClient()
    cfg = _cfg(d, ["AAPL", "NVDA", "MSFT"])
    cfg.overseas.auto_select = True
    cfg.overseas.auto_select_count = 3
    engine = OverseasEngine(cfg, client=client)

    result = engine._effective_watchlist()
    check("자동 선정 결과가 정확히 3개", len(result) == 3)
    check("두 랭킹에 다 있는 AAPL이 1등", result[0] == "AAPL")

    check("auto_select 꺼져 있으면 고정 목록 그대로(하위호환)", True)
    cfg2 = _cfg(d, ["AAPL", "NVDA", "MSFT"])
    cfg2.overseas.auto_select = False
    engine2 = OverseasEngine(cfg2, client=client)
    check("고정 목록 그대로 반환됨", engine2._effective_watchlist() == ["AAPL", "NVDA", "MSFT"])


def test_auto_select_still_manages_dropped_holdings() -> None:
    """★★★ 실제로 겪을 뻔한 안전 결함 방지 - 자동 선정으로 오늘 목록이
    바뀌어도, 목록에서 빠진 종목을 이미 보유 중이면 청산 관리를 계속
    해야 한다(포지션을 놓치면 안 된다는 원칙).
    """
    print("\n== 자동선정 목록에서 빠진 보유종목도 청산 관리는 계속됨 ==")
    from daytrader.overseas_broker import OverseasPosition
    import time as _time

    d = tempfile.mkdtemp()

    class RankingsWithoutAAPL(FakeTossClient):
        """★ AAPL을 어느 랭킹에도 안 넣어서, 자동선정 목록에서 AAPL이
        확실히 빠지는 상황을 만든다(공용 FakeTossClient.rankings() 는
        AAPL을 포함해서 이 시나리오를 재현 못 한다)."""
        def rankings(self, type, marketCountry, duration, count):
            return [{"symbol": "NVDA"}]

    client = RankingsWithoutAAPL(breakout=False)  # ★ 신규 진입 신호 없음 - 관리만 확인.
    cfg = _cfg(d, ["MSFT"], mode="sim")  # ★ 관심 종목(MSFT)은 항상 들어가지만 AAPL 은 어느 목록에도 없다.
    cfg.overseas.auto_select = True
    cfg.overseas.auto_select_count = 1  # ★ NVDA 하나만 뽑히게 - AAPL은 자동선정 목록에서 빠짐.
    engine = OverseasEngine(cfg, client=client)
    # ★ 이 테스트는 "관리가 계속되는지"만 보는 것이라, 진입가를 client.price 와 맞춰 둔다 -
    # 안 그러면 관리 루프에서 손절로 즉시 청산되어(포지션이 사라져) 이 테스트가 확인하려는
    # 것과 무관한 이유로 실패한다.
    engine.state.book.record_buy(OverseasPosition(
        symbol="AAPL", quantity=10, entry_price=client.price, entry_time=_time.time(),
        peak_price=client.price, technique="breakout", name="AAPL", theme="해외주식",
    ))
    engine.run_once()
    check("자동선정 목록(NVDA)에 AAPL이 없음(전제 확인)", "AAPL" not in engine._effective_watchlist())
    check("★★★ 목록에서 빠졌어도 AAPL 포지션은 여전히 관리 대상(살아있음)", engine.state.book.owns("AAPL"))


def test_sim_mode_works_without_real_api() -> None:
    """★★★ 실제로 겪은 문제 - 해외주식 sim(시뮬레이션) 모드가 이름과 달리
    진짜 토스 API 를 호출해서, 키가 없거나 네트워크가 막히면 아무것도
    못 하고 조용히 실패했다("시뮬레이션이 전혀 동작 안 한다"). 이제
    합성 시세(SimFeedClient)로 실제 API 없이 돌아야 한다.
    """
    print("\n== 해외주식 sim 모드가 실제 API 없이 정상 매매함 ==")
    d = tempfile.mkdtemp()
    cfg = _cfg(d, ["AAPL", "NVDA", "MSFT"], mode="sim")
    engine = OverseasEngine(cfg, client=None)  # ★ 클라이언트를 아예 안 준다.
    check("sim 모드는 합성 시세 클라이언트를 씀",
          type(engine.client).__name__ == "SimFeedClient")

    for _ in range(5):
        engine.run_once()
        if engine.state.book.all():
            break
    check("실제 API 없이도 매수가 일어남", bool(engine.state.book.all()), engine.last_error)
    check("네트워크 오류가 남지 않음", not engine.last_error, engine.last_error)


def test_overseas_buy_converts_krw_to_usd() -> None:
    """★★★ 실제로 겪은 버그 - 원화 배정금액을 달러 주가로 그대로 나눠서
    수량이 환율 배수(약 1,400배)만큼 부풀려졌다(300만원 ÷ $238 = 1260주).
    환율로 환산한 뒤 나눠야 종목당 배정금액에 맞는 수량이 나온다.
    """
    print("\n== 해외주식 매수 수량이 환율을 반영해 현실적으로 계산됨 ==")
    from daytrader.overseas_engine import _usd_krw_rate

    d = tempfile.mkdtemp()
    cfg = _cfg(d, ["AAPL", "NVDA", "MSFT"], mode="sim")
    engine = OverseasEngine(cfg, client=None)

    for _ in range(5):
        engine.run_once()
        if engine.state.book.all():
            break

    # ★ 이제 해외 투자금액은 달러(budget_usd)로 정하고, 종목당 한도 = 총액 / 동시 보유 수, 첫 매수는 그 절반 × 신호 배수(≤1.4).
    cap = cfg.overseas.budget_usd / cfg.overseas.max_positions
    for symbol, pos in engine.state.book.all().items():
        cost_usd = pos.quantity * pos.entry_price
        check(f"{symbol} 첫 매수금액이 종목당 한도(${cap:,.0f})의 70% 이내(환율 버그면 수천 배가 된다)",
              cost_usd <= cap * cfg.sizing.initial_ratio * cfg.sizing.max_mult * 1.05, f"${cost_usd:,.2f}")


def test_overseas_rejects_non_us_tickers() -> None:
    """★★★ 실제로 겪은 버그 - marketCountry="US" 로 요청했는데도 국내
    종목코드(010620 등 6자리 숫자)가 응답에 섞여 들어왔고, 그걸 미국
    티커로 알고 야후에 조회해 404 가 났다("시세조회 실패, 종목명이 안
    나옴"). API 응답을 그대로 믿지 말고 형식을 직접 검증해야 한다.
    """
    print("\n== 해외주식 감시목록에서 국내 종목코드를 걸러냄 ==")
    from daytrader.overseas_engine import _is_us_ticker

    for sym, expect in [("AAPL", True), ("BRK.B", True), ("BF-A", True),
                        ("010620", False), ("005930", False), ("", False), (None, False)]:
        check(f"_is_us_ticker({sym!r}) == {expect}", _is_us_ticker(sym) is expect)

    d = tempfile.mkdtemp()

    class MixedRankings(FakeTossClient):
        """★ US 로 요청해도 국내 코드가 섞여 오는 상황 재현."""
        def rankings(self, type, marketCountry, duration, count):
            return [{"symbol": "AAPL"}, {"symbol": "010620"},
                    {"symbol": "NVDA"}, {"symbol": "005930"}]

    cfg = _cfg(d, ["AAPL"], mode="sim")
    cfg.overseas.auto_select = True
    cfg.overseas.auto_select_count = 4
    engine = OverseasEngine(cfg, client=MixedRankings())
    wl = engine._effective_watchlist()
    check("★★★ 자동선정에서 국내코드가 제외됨", "010620" not in wl and "005930" not in wl, str(wl))
    check("미국 티커는 정상 포함됨", "AAPL" in wl and "NVDA" in wl, str(wl))

    # ★ 사용자가 설정에 직접 국내코드를 넣은 경우도 막아야 한다.
    cfg2 = _cfg(d, ["AAPL", "010620", "MSFT"], mode="sim")
    cfg2.overseas.auto_select = False
    engine2 = OverseasEngine(cfg2, client=FakeTossClient())
    check("고정 목록의 국내코드도 제외됨", engine2._effective_watchlist() == ["AAPL", "MSFT"],
          str(engine2._effective_watchlist()))


def test_cash_available_survives_none() -> None:
    """★★★ 실제로 겪은 버그 - 해외주식에서 "마지막 오류: '>' not supported
    between instances of 'NoneType' and 'int'". 브로커가 현금을 None 으로
    돌려주면(API 응답에 cash 가 없거나 조회 실패) 비교식에서 TypeError 가
    나고 매수가 통째로 실패했다.
    ★ 알 수 없으면 0 으로 본다 - 잔고를 모르는데 있다고 가정하고 사는
    것보다 안 사는 쪽이 안전하다.
    """
    print("\n== 현금 조회가 None 이어도 비교에서 죽지 않음 ==")
    d = tempfile.mkdtemp()
    cfg = _cfg(d, ["AAPL"], mode="sim")
    engine = OverseasEngine(cfg, client=None)

    class NoneCashAttr:
        cash = None

    engine.broker = NoneCashAttr()
    v = engine._cash_available()
    check("★★★ None 이면 0.0 으로 정규화됨", v == 0.0, str(v))
    try:
        _ = 100.0 > v
        check("비교 연산에서 TypeError 안 남", True)
    except TypeError as exc:
        check("비교 연산에서 TypeError 안 남", False, str(exc))

    class NoneCashFn:
        cash = staticmethod(lambda: None)

    engine.broker = NoneCashFn()
    check("함수가 None 을 돌려줘도 안전", engine._cash_available() == 0.0)

    class OkCash:
        cash = staticmethod(lambda: 642.86)

    engine.broker = OkCash()
    check("정상 값은 그대로 유지", engine._cash_available() == 642.86)


def test_update_peak_survives_none() -> None:
    """★★★ 실제로 겪은 버그 - 해외주식 "마지막 오류: '>' not supported
    between instances of 'NoneType' and 'int'". update_peak() 의
    price > pos.peak_price 비교에서 한쪽이 None 이면 죽고, 청산 관리가
    통째로 멈춘다 - 보유 종목을 못 파는 건 손실로 직결된다.
    """
    print("\n== update_peak 이 None 값에도 죽지 않음 ==")
    import time as _time
    from daytrader.overseas_broker import OverseasPosition, OverseasPositionBook

    book = OverseasPositionBook()
    book.record_buy(OverseasPosition(
        symbol="AAPL", quantity=1, entry_price=200, entry_time=_time.time(),
        peak_price=None, technique="x", name="AAPL", theme="t",
    ))

    try:
        book.update_peak("AAPL", 210.0)
        check("★★★ peak_price 가 None 이어도 예외 없음", True)
    except TypeError as exc:
        check("★★★ peak_price 가 None 이어도 예외 없음", False, str(exc))
        return
    check("빈 peak_price 가 현재가로 채워짐", book.get("AAPL").peak_price == 210.0,
          str(book.get("AAPL").peak_price))

    try:
        book.update_peak("AAPL", None)
        check("가격이 None 이어도 예외 없음", True)
    except TypeError as exc:
        check("가격이 None 이어도 예외 없음", False, str(exc))
    check("가격이 None 이면 기존 고점을 유지", book.get("AAPL").peak_price == 210.0)

    book.update_peak("AAPL", 220.0)
    check("정상 갱신은 그대로 동작", book.get("AAPL").peak_price == 220.0)


def test_survives_non_dict_api_rows() -> None:
    """★★★ 실제로 겪은 버그 - 해외주식 매매에서 "'str' object has no
    attribute 'get'". 캔들·시세 응답에 딕셔너리가 아닌 항목(문자열 등)이
    섞여 오면 r.get() 에서 죽어 매매가 통째로 실패했다.
    ★ 특히 `"openPrice" in r` 은 문자열에서도 예외 없이 통과하므로
    (부분 문자열 검사) 타입을 직접 확인해야 한다.
    """
    print("\n== 시세·캔들 응답에 문자열이 섞여도 매매가 죽지 않음 ==")
    d = tempfile.mkdtemp()

    class MixedRows:
        def candles(self, symbol, interval, count, before=None):
            p = 200.0
            rows = [{"timestamp": str(i), "openPrice": p, "highPrice": p * 1.01,
                     "lowPrice": p * 0.99, "closePrice": p, "volume": 100000}
                    for i in range(80)]
            return ["BADROW"] + rows + [None]

        def prices(self, symbols):
            return ["AAPL", {"symbol": "AAPL", "lastPrice": 210.0}]

        def buying_power(self, currency="KRW"):
            return {"cash": 10000}

        def rankings(self, **kw):
            return [{"symbol": "AAPL"}]

    cfg = _cfg(d, ["AAPL"], mode="paper")
    engine = OverseasEngine(cfg, client=MixedRows())

    try:
        bars = engine._fetch_bars("AAPL", 80)
        check("★★★ 캔들에 문자열이 섞여도 예외 없음", True)
        check("잘못된 항목만 걸러지고 정상 봉은 남음", len(bars) == 80, f"{len(bars)}봉")
    except AttributeError as exc:
        check("★★★ 캔들에 문자열이 섞여도 예외 없음", False, str(exc))
        return

    try:
        price = engine._current_price("AAPL")
        check("★★★ 시세 응답에 문자열이 섞여도 예외 없음", price == 210.0, str(price))
    except AttributeError as exc:
        check("★★★ 시세 응답에 문자열이 섞여도 예외 없음", False, str(exc))

    try:
        engine.run_once()
        check("run_once 전체가 예외 없이 완주", True)
    except AttributeError as exc:
        check("run_once 전체가 예외 없이 완주", False, str(exc))


def test_default_watchlist_has_mag7_and_ai_chips() -> None:
    """★★★ "해외주식 관심 종목에 매그니피센트7과 AI반도체 주식도 기본으로
    추가" - 예전엔 3종목(AAPL·NVDA·MSFT)뿐이라 선택지가 지나치게 좁았다.
    """
    print("\n== 해외 기본 관심종목에 매그니피센트7·AI반도체가 들어감 ==")
    from daytrader.config import load_config
    from daytrader.overseas_engine import _is_us_ticker

    wl = load_config(CONFIG_PATH).overseas.watchlist

    mag7 = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
    missing_mag7 = [s for s in mag7 if s not in wl]
    check("★★★ 매그니피센트 7 이 모두 포함됨", not missing_mag7, str(missing_mag7))

    ai_chips = ["AMD", "AVGO", "TSM", "MU", "ASML", "ARM", "SMCI", "INTC"]
    missing_ai = [s for s in ai_chips if s not in wl]
    check("★★★ AI반도체 대표주가 모두 포함됨", not missing_ai, str(missing_ai))

    check("중복 없음(NVDA 는 두 묶음에 속하지만 한 번만)", len(wl) == len(set(wl)), str(len(wl)))
    bad = [s for s in wl if not _is_us_ticker(s)]
    check("전부 유효한 미국 티커 형식", not bad, str(bad))


def test_state_restore_survives_corrupt_records() -> None:
    """★★★ 실제로 겪은 버그 - 해외주식 "마지막 오류: 'str' object has no
    attribute 'get'" 가 계속 나왔다. 상태 파일(overseas_state.json)의
    positions 값이 딕셔너리가 아니면(손상·예전 형식) p.get() 에서 죽어
    엔진이 시작조차 못 했다. 그 메시지만 남아 원인을 알 수 없었다.
    ★ 한 종목이 깨졌다고 나머지까지 버리면 안 된다 - 그 종목만 건너뛴다.
    """
    print("\n== 손상된 상태 파일에서도 엔진이 시작됨 ==")
    import json as _json
    import os as _os

    d = tempfile.mkdtemp()
    with open(_os.path.join(d, "overseas_state.json"), "w", encoding="utf-8") as f:
        _json.dump({
            "positions": {
                "AAPL": "BROKEN",  # ★ 딕셔너리가 아닌 값 - 예전엔 여기서 죽었다.
                "NVDA": {"quantity": 1, "entry_price": 200, "entry_time": 1, "peak_price": 210},
            },
            "closed": ["BAD", {"symbol": "X", "pnl": 10}],
        }, f)

    cfg = _cfg(d, ["AAPL"], mode="sim")
    try:
        engine = OverseasEngine(cfg, client=None)
        check("★★★ 손상된 기록이 있어도 엔진이 시작됨", True)
    except AttributeError as exc:
        check("★★★ 손상된 기록이 있어도 엔진이 시작됨", False, str(exc))
        return

    held = list(engine.state.book.all().keys())
    check("깨진 종목(AAPL)은 건너뜀", "AAPL" not in held, str(held))
    check("정상 종목(NVDA)은 살려서 복원", "NVDA" in held, str(held))
    check("청산 기록도 딕셔너리만 남김", len(engine.state.closed) == 1, str(engine.state.closed))


def test_broker_survives_none_values() -> None:
    """★★★ 실제로 겪은 버그 - "'>' not supported between instances of
    'NoneType' and 'int'" 가 계속 나왔다. 브로커의 매수 조건
    (price <= 0, krw_amount > cash)에서 값이 None 이면 그 자리에서
    죽고 매매가 통째로 실패한다.
    ★ 매수는 '모르면 안 산다'가 맞지만, 청산은 실패하면 안 된다 -
    못 파는 건 손실로 직결되므로 값이 이상해도 포지션은 정리한다.
    """
    print("\n== 브로커가 None 값에도 죽지 않음 ==")
    import time as _time
    from daytrader.overseas_broker import PaperOverseasBroker, OverseasPosition

    broker = PaperOverseasBroker(starting_cash=1000.0)
    for label, amount, price in [("가격이 None", 100.0, None),
                                 ("금액이 None", None, 200.0)]:
        try:
            result = broker.buy("AAPL", amount, price)
            check(f"매수 - {label} 이어도 예외 없음", True)
            check(f"매수 - {label} 이면 사지 않음(모르는 값으로 주문 금지)", result is None)
        except TypeError as exc:
            check(f"매수 - {label} 이어도 예외 없음", False, str(exc))

    broker.cash = None
    try:
        broker.buy("AAPL", 100.0, 200.0)
        check("매수 - 현금이 None 이어도 예외 없음", True)
    except TypeError as exc:
        check("매수 - 현금이 None 이어도 예외 없음", False, str(exc))

    # ★ 청산은 값이 깨져도 포지션을 정리해야 한다.
    b2 = PaperOverseasBroker(starting_cash=1000.0)
    b2.book.record_buy(OverseasPosition(
        symbol="AAPL", quantity=None, entry_price=200, entry_time=_time.time(),
        peak_price=210, technique="x", name="AAPL", theme="t",
    ))
    b2.cash = None
    try:
        b2.sell("AAPL", None)
        check("★★★ 청산 - 값이 깨져도 예외 없이 처리됨", True)
        check("청산 - 포지션이 실제로 정리됨(못 팔면 손실로 직결)",
              b2.book.get("AAPL") is None)
    except TypeError as exc:
        check("★★★ 청산 - 값이 깨져도 예외 없이 처리됨", False, str(exc))


def test_exit_survives_broken_position_fields() -> None:
    """★★★ 청산 관리는 어떤 경우에도 멈추면 안 된다 - 못 파는 건 손실로
    직결된다. 포지션 필드가 비어 있으면(상태 파일 손상, API 응답 누락)
    곱셈·뺄셈·비교에서 죽어 청산이 통째로 멈췄다.
      - entry_price=None → 손절·익절 계산에서 죽음(가장 위험)
      - entry_time=None  → 보유 시간 계산에서 죽음
    ★ 값이 없으면 그 판정만 보류하고, 나머지 청산 기법은 계속 돌게 한다.
    """
    print("\n== 포지션 필드가 비어도 청산 관리가 멈추지 않음 ==")
    import time as _time
    from daytrader.overseas_broker import OverseasPosition

    class SteadyClient:
        def candles(self, symbol, interval, count, before=None):
            p = 200.0
            return [{"timestamp": str(i), "openPrice": p, "highPrice": p * 1.01,
                     "lowPrice": p * 0.99, "closePrice": p, "volume": 1e5}
                    for i in range(80)]

        def prices(self, symbols):
            return [{"symbol": s, "lastPrice": 210.0} for s in symbols]

        def buying_power(self, currency="KRW"):
            return {"cash": 10000}

        def rankings(self, **kw):
            return [{"symbol": "AAPL"}]

    for field in ["entry_price", "entry_time", "peak_price", "quantity"]:
        d = tempfile.mkdtemp()
        cfg = _cfg(d, ["AAPL"], mode="paper")
        engine = OverseasEngine(cfg, client=SteadyClient())

        kw = dict(symbol="AAPL", quantity=10, entry_price=200.0,
                  entry_time=_time.time(), peak_price=210.0,
                  technique="x", name="AAPL", theme="t")
        kw[field] = None
        engine.state.book.record_buy(OverseasPosition(**kw))

        try:
            engine.run_once()
        except Exception as exc:
            check(f"{field}=None 이어도 예외 없음", False, str(exc))
            continue

        err = engine.last_error or ""
        bad = any(k in err for k in ("TypeError", "unsupported", "not supported"))
        check(f"★★★ {field}=None 이어도 청산 관리가 계속됨", not bad, err[:70])


def test_empty_config_values_fall_back_to_defaults() -> None:
    print("\n== ★★★ config.yaml 의 빈 값(None)이 기본값을 덮어쓰지 않음(대시보드 type 오류 원인) ==")
    from daytrader.config import OverseasCfg, _build, load_config
    import daytrader.overseas_engine as oe

    o = _build(OverseasCfg, "overseas", {"max_positions": None, "auto_select_count": None, "watchlist": ["AAPL"]})
    check("max_positions 가 비어 있으면 기본값 4", o.max_positions == 4, str(o.max_positions))
    check("auto_select_count 가 비어 있으면 기본값 10", o.auto_select_count == 10, str(o.auto_select_count))
    check("값이 있는 항목은 그대로", o.watchlist == ["AAPL"])

    # 실제 사고 재현: 설정 파일에 "max_positions:" 가 빈 채로 있어도 장중 루프가 죽지 않아야 한다.
    d = tempfile.mkdtemp()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        text = f.read()
    import re
    text = re.sub(r"(?m)^(\s+)max_positions:\s*4\s*$", r"\1max_positions:", text, count=0)
    path = os.path.join(d, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    cfg = load_config(path)
    cfg.state_dir = d
    cfg.overseas.mode = "paper"
    cfg.overseas.watchlist = ["AAPL"]
    cfg.overseas.auto_select = False
    check("빈 max_positions 로 읽은 설정이 기본값 4", cfg.overseas.max_positions == 4, str(cfg.overseas.max_positions))
    orig = oe.is_us_tradable
    try:
        oe.is_us_tradable = lambda now, **kw: True
        engine = OverseasEngine(cfg, client=FakeTossClient())
        engine.run_once()
        check("장중 run_once 가 TypeError 없이 끝남", "NoneType" not in str(engine.last_error or ""), str(engine.last_error))
    finally:
        oe.is_us_tradable = orig


def test_auto_select_skipped_when_market_closed() -> None:
    print("\n== ★★★ 장이 닫혀 있으면 자동 종목 선정(랭킹 조회)을 하지 않음 ==")
    import daytrader.overseas_engine as oe

    class CountingClient(FakeTossClient):
        def __init__(self):
            super().__init__()
            self.ranking_calls = 0

        def rankings(self, type, marketCountry, duration, count):
            self.ranking_calls += 1
            return super().rankings(type, marketCountry, duration, count)

    orig = oe.is_us_tradable
    try:
        d = tempfile.mkdtemp()
        client = CountingClient()
        cfg = _cfg(d, ["AAPL"], mode="paper")
        cfg.overseas.auto_select = True
        cfg.overseas.auto_select_count = 3
        engine = OverseasEngine(cfg, client=client)

        oe.is_us_tradable = lambda now, **kw: False
        engine.run_once()
        engine.run_once()
        check("장 마감 중 run_once 여러 번 - 랭킹 조회 0회", client.ranking_calls == 0, str(client.ranking_calls))

        oe.is_us_tradable = lambda now, **kw: True
        engine.run_once()
        check("장이 열리면 자동 선정을 함", client.ranking_calls > 0, str(client.ranking_calls))
    finally:
        oe.is_us_tradable = orig


def test_trading_24h_session() -> None:
    print("\n== ★★★ 미국 주식 24시간 거래(주말·휴장일만 제외) ==")
    from datetime import date
    import daytrader.overseas_engine as oe
    ny = ZoneInfo("America/New_York")

    def sess(y, m, d, h, mi=0):
        return oe.us_session(datetime(y, m, d, h, mi, tzinfo=ny))

    check("정규장(수 10:00) = regular", sess(2026, 9, 9, 10) == "regular")
    check("프리마켓(수 05:00) = extended", sess(2026, 9, 9, 5) == "extended")
    check("애프터/야간(수 22:00) = extended", sess(2026, 9, 9, 22) == "extended")
    check("새벽(목 02:00) = extended", sess(2026, 9, 10, 2) == "extended")
    check("일요일 저녁 8시 = 월요일 세션 시작(extended)", sess(2026, 9, 13, 20, 0) == "extended")
    check("일요일 낮 = closed", sess(2026, 9, 13, 12) == "closed")
    check("금요일 저녁 8시 = 종료(closed)", sess(2026, 9, 11, 20, 0) == "closed")
    check("금요일 저녁 7시 59분 = 아직 거래(extended)", sess(2026, 9, 11, 19, 59) == "extended")
    check("토요일 = closed", sess(2026, 9, 12, 10) == "closed")
    check("추수감사절(목, 2026-11-26) 낮 = closed", sess(2026, 11, 26, 11) == "closed")
    check("추수감사절 저녁 8시 이후는 금요일 세션(extended)", sess(2026, 11, 26, 21) == "extended")
    check("성금요일(2026-04-03) = closed", sess(2026, 4, 3, 11) == "closed")
    check("독립기념일 대체휴일(2026-07-03 금) = closed", sess(2026, 7, 3, 11) == "closed")
    check("2026 NYSE 휴장일 10일",
          oe.us_market_holidays(2026) == {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
                                        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
                                        date(2026, 11, 26), date(2026, 12, 25)})
    night = datetime(2026, 9, 9, 22, 0, tzinfo=ny)
    check("모든 세션 켜면 밤(데이장)에도 거래 가능",
          oe.is_us_tradable(night, trade_premarket=True, trade_regular=True, trade_afterhours=True, trade_overnight=True) is True)
    check("본장만 켜면 밤(데이장)에는 불가",
          oe.is_us_tradable(night, trade_premarket=False, trade_regular=True, trade_afterhours=False, trade_overnight=False) is False)
    check("모든 세션 켜도 주말은 불가",
          oe.is_us_tradable(datetime(2026, 9, 12, 12, tzinfo=ny), trade_premarket=True, trade_regular=True, trade_afterhours=True, trade_overnight=True) is False)
    check("정규장 판정에서도 휴장일 제외", oe.is_us_market_open(datetime(2026, 11, 26, 11, tzinfo=ny)) is False)

    # 엔진: 데이장(overnight)에도 신규 진입을 시도하고, 주말·휴장에는 하지 않는다.
    # ★ "본장·프리장·애프터장·데이장 등 장 별로 거래를 할지 사용자가 선택하게 해" 이후로는
    # us_session(3단계) 이 아니라 us_phase(4단계+휴장) 로 세션을 가려서, 여기서도 us_phase 를
    # 직접 흉내 낸다(실제 시각에 따라 결과가 바뀌지 않게 - 결정적 테스트).
    class Spy(OverseasEngine):
        entries = 0

        def _try_entry(self, symbol):
            Spy.entries += 1

    d = tempfile.mkdtemp()
    cfg = _cfg(d, ["AAPL"], mode="paper")
    # ★ "해외장도 본장이 디폴트 선택되도록" 이후 기본은 본장(regular)만 켜져 있다 - 이 테스트는
    # 데이장(overnight)에서의 동작을 보는 것이므로 명시적으로 켜 준다.
    cfg.overseas.trade_overnight = True
    eng = Spy(cfg, client=FakeTossClient())
    orig = oe.us_phase
    try:
        oe.us_phase = lambda now: "overnight"
        eng.run_once()
        check("데이장을 켜 두면 - 신규 진입을 시도", Spy.entries == 1, str(Spy.entries))
        oe.us_phase = lambda now: "closed"
        eng.run_once()
        check("주말·휴장 - 신규 진입 안 함", Spy.entries == 1, str(Spy.entries))
        cfg.overseas.trade_overnight = False
        oe.us_phase = lambda now: "overnight"
        eng.run_once()
        check("trade_overnight=False(기본값) - 데이장에는 진입 안 함", Spy.entries == 1, str(Spy.entries))
        cfg.overseas.trade_overnight = True
        snap = eng.snapshot()
        check("상태에 session 포함", isinstance(snap.get("session"), str) and snap.get("session"), str(snap.get("session")))
    finally:
        oe.us_phase = orig


def test_us_theme_selection() -> None:
    print("\n== ★★★ 미국 테마주 자동 산정 + 관심 종목은 테마와 별개로 항상 거래 대상 ==")
    from daytrader import us_themes

    def row(sym, change, amount):
        return {"symbol": sym, "price": {"lastPrice": 100.0, "changeRate": change}, "tradingAmount": amount}

    class ThemeClient(FakeTossClient):
        def rankings(self, type, marketCountry, duration, count):
            assert marketCountry == "US"
            if type == "MARKET_TRADING_AMOUNT":
                return [row("NVDA", 0.04, 9e9), row("AMD", 0.03, 3e9), row("AVGO", 0.05, 4e9),
                        row("LMT", 0.001, 1e9), row("XOM", -0.01, 1e9)]
            return [row("MU", 0.06, 2e9), row("PLTR", 0.09, 2e9)]

        def prices(self, symbols):
            return []  # 랭킹만으로 판단

    d = tempfile.mkdtemp()
    cfg = _cfg(d, ["AAPL"], mode="paper")
    cfg.overseas.top_themes = 2
    cfg.overseas.candidates_per_theme = 2
    report = us_themes.scan_us_themes(ThemeClient(), cfg.overseas, cfg)
    names = [t["name"] for t in report["themes"] if t["picked"]]
    check("동반 상승한 AI반도체가 선정 테마 1위", names and names[0] == "AI반도체", str(names))
    cands = report["candidates"]
    check("후보는 그 테마 종목(NVDA 등)", any(c["symbol"] == "NVDA" and c["theme"] == "AI반도체" for c in cands), str([c["symbol"] for c in cands]))
    check("테마당 후보 수 제한(2)", sum(1 for c in cands if c["theme"] == "AI반도체") <= 2)
    check("테마로 인정 못 받은 방산은 탈락(동반상승 1개 미만)", all(t["name"] != "방산_우주" or not t["picked"] for t in report["themes"]))
    check("후보에 선정 근거 문장", all(c.get("why") for c in cands))

    # 엔진: 테마주 + 관심 종목이 함께 거래 대상
    engine = OverseasEngine(cfg, client=ThemeClient())
    wl = engine._effective_watchlist()
    check("거래 대상에 테마주가 들어감", "NVDA" in wl, str(wl))
    check("직접 추가한 관심 종목(AAPL)도 함께 들어감", "AAPL" in wl, str(wl))
    check("중복 없음", len(wl) == len(set(wl)))
    snap = engine.snapshot()
    check("상태 watchlist 도 같은 목록", set(snap["watchlist"]) == set(wl))
    check("상태에 종목->테마 표", snap["theme_of"].get("NVDA") == "AI반도체", str(snap["theme_of"]))

    cfg.overseas.theme_select = False
    engine2 = OverseasEngine(cfg, client=ThemeClient())
    check("theme_select 끄면 관심 종목만", engine2._effective_watchlist() == ["AAPL"])

    class DeadClient(FakeTossClient):
        def rankings(self, *a, **k):
            raise RuntimeError("down")
    cfg.overseas.theme_select = True
    engine3 = OverseasEngine(cfg, client=DeadClient())
    check("랭킹 조회가 죽어도 관심 종목으로 계속(예외 없음)", engine3._effective_watchlist() == ["AAPL"])


def test_theme_reselect_on_phase_change() -> None:
    print("\n== ★★★ 장이 바뀔 때마다 미국 테마주 다시 선정 ==")
    import daytrader.overseas_engine as oe
    ny = ZoneInfo("America/New_York")

    def ph(h, mi=0, day=9):
        return oe.us_phase(datetime(2026, 9, day, h, mi, tzinfo=ny))

    check("정규장 국면", ph(10) == "regular")
    check("프리마켓 국면", ph(6) == "premarket")
    check("애프터마켓 국면", ph(17) == "afterhours")
    check("야간 국면(저녁 9시)", ph(21) == "overnight")
    check("야간 국면(새벽 2시)", ph(2, day=10) == "overnight")
    check("주말은 closed", oe.us_phase(datetime(2026, 9, 12, 10, tzinfo=ny)) == "closed")

    calls = {"n": 0}

    class CountClient(FakeTossClient):
        def rankings(self, type, marketCountry, duration, count):
            calls["n"] += 1
            return [{"symbol": "NVDA", "price": {"lastPrice": 100.0, "changeRate": 0.05}, "tradingAmount": 5e9},
                    {"symbol": "AMD", "price": {"lastPrice": 100.0, "changeRate": 0.04}, "tradingAmount": 3e9}]

        def prices(self, symbols):
            return []

    d = tempfile.mkdtemp()
    cfg = _cfg(d, ["AAPL"], mode="paper")
    engine = OverseasEngine(cfg, client=CountClient())
    orig = oe.us_phase
    try:
        oe.us_phase = lambda now: "premarket"
        engine._effective_watchlist()
        n1 = calls["n"]
        engine._effective_watchlist()
        check("같은 국면에서는 다시 뽑지 않음(캐시)", calls["n"] == n1, f"{n1}->{calls['n']}")
        oe.us_phase = lambda now: "regular"
        engine._effective_watchlist()
        check("장이 바뀌면(프리마켓→정규장) 다시 뽑음", calls["n"] > n1, f"{n1}->{calls['n']}")
        n2 = calls["n"]
        oe.us_phase = lambda now: "afterhours"
        engine._effective_watchlist()
        check("정규장→애프터마켓에서도 다시 뽑음", calls["n"] > n2)
        check("리포트에 국면 표시", (engine.theme_report() or {}).get("phase") == "afterhours")
    finally:
        oe.us_phase = orig


def test_manage_positions_only() -> None:
    print("== ★★★ 진입 탐색 사이에 보유 종목만 자주 관리(손절 지연 단축) ==")
    from daytrader.overseas_broker import OverseasPosition
    import time as _time

    d = tempfile.mkdtemp()
    client = FakeTossClient(breakout=True)
    cfg = _cfg(d, ["AAPL", "MSFT"])
    engine = OverseasEngine(cfg, client=client)
    check("보유 관리 주기 기본 60초", cfg.overseas.manage_seconds == 60, str(cfg.overseas.manage_seconds))
    engine.manage_positions_only()
    check("보유가 없으면 아무것도 안 함(진입도 안 함)", not engine.state.book.all())
    engine.state.book.record_buy(OverseasPosition(
        symbol="AAPL", quantity=10, entry_price=100.0, entry_time=_time.time() - 600, peak_price=100.0, technique="breakout",
    ))
    client.price = 110.0  # +10% -> 익절 구간: 분할 매도 시작
    engine.manage_positions_only()
    check("보유 종목은 익절 구간에서 1차 분할 매도됨", engine.state.book.owns("AAPL") and engine.state.book.get("AAPL").scaled_out == 1)
    check("이 함수는 새 종목을 사지 않음(MSFT 미보유)", not engine.state.book.owns("MSFT"))


def test_snapshot_has_current_price() -> None:
    print("== 화면의 현재가: 보유 종목 스냅샷에 마지막 현재가 포함 ==")
    from daytrader.overseas_broker import OverseasPosition
    import time as _time

    d = tempfile.mkdtemp()
    client = FakeTossClient(breakout=False)
    cfg = _cfg(d, ["AAPL"])
    engine = OverseasEngine(cfg, client=client)
    engine.state.book.record_buy(OverseasPosition(
        symbol="AAPL", quantity=10, entry_price=100.0, entry_time=_time.time() - 60, peak_price=100.0, technique="breakout",
    ))
    before = engine.snapshot()["positions"]["AAPL"].get("last_price")
    check("아직 시세를 못 받았으면 현재가는 비어 있음", before is None, str(before))
    client.price = 101.5
    engine._manage_position("AAPL", engine.state.book.get("AAPL"))
    after = engine.snapshot()["positions"]["AAPL"].get("last_price")
    check("관리 후 스냅샷의 현재가가 실제 시세(101.5)", after == 101.5, str(after))


def test_us_phase_distinguishes_sessions() -> None:
    print("== us_phase() 가 프리장/본장/애프터장/데이장(야간거래)을 구분하는지(재개 판정의 전제) ==")
    ny = ZoneInfo("America/New_York")
    premarket = datetime(2026, 9, 9, 6, 0, tzinfo=ny)
    regular = datetime(2026, 9, 9, 11, 0, tzinfo=ny)
    afterhours = datetime(2026, 9, 9, 18, 0, tzinfo=ny)
    overnight = datetime(2026, 9, 9, 22, 0, tzinfo=ny)
    check("프리마켓 판정", us_phase(premarket) == "premarket", us_phase(premarket))
    check("정규장 판정", us_phase(regular) == "regular", us_phase(regular))
    check("애프터마켓 판정", us_phase(afterhours) == "afterhours", us_phase(afterhours))
    check("데이장(야간거래) 판정", us_phase(overnight) == "overnight", us_phase(overnight))


def test_halt_resumes_when_session_changes() -> None:
    print("== ★ \"프리장·본장·애프터장·데이장이 바뀌면 다시 거래를 재개\" - 연속 손절로 막혀도"
          " 세션이 바뀌면 쿨다운 시간과 무관하게 즉시 재개 ==")
    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = d
    cfg.overseas.watchlist = ["AAPL"]
    cfg.overseas.mode = "paper"
    engine = OverseasEngine(cfg, client=FakeTossClient())
    # ★ OverseasState 는 처음 만들어지면 date 가 빈 문자열이라, halt_info() 안의
    # _reset_if_new_day() 가 "날짜가 바뀌었다"고 보고 한 번은 반드시 리셋한다. 그 리셋이
    # 아래에서 일부러 넣는 consecutive_losses 를 지우지 않도록, 먼저 한 번 불러서
    # 오늘 날짜로 자리잡게 한 뒤에 시나리오를 만든다.
    engine.halt_info()

    ny = ZoneInfo("America/New_York")
    # ★ 쿨다운 기본값이 3시간이라, 손절을 정규장 마감(16:00) 직전에 둬야 "세션은 바뀌었지만
    # 쿨다운 시간(3시간)은 아직 안 지난" 상태를 만들 수 있다.
    loss_at_ny = datetime(2026, 9, 9, 15, 0, tzinfo=ny)  # 정규장에서 마지막 손절
    engine.state.consecutive_losses = cfg.risk.max_consecutive_losses
    engine.state.closed.append({"pnl": -10.0, "exit_time": loss_at_ny.timestamp()})

    same_session = datetime(2026, 9, 9, 15, 45, tzinfo=ny)  # 여전히 정규장, 쿨다운(기본 3h)도 안 지남
    info = engine.halt_info(now=same_session)
    check("같은 세션(정규장) 안에서는 여전히 중단", info["halted"] is True, info)

    next_session = datetime(2026, 9, 9, 16, 10, tzinfo=ny)  # 애프터마켓으로 세션이 바뀜, 쿨다운은 아직 안 지남
    info2 = engine.halt_info(now=next_session)
    check("세션이 바뀌면(애프터마켓) 쿨다운 시간이 안 지났어도 즉시 재개", info2["halted"] is False, info2)


def test_overseas_live_buy_no_double_order_on_timeout() -> None:
    """★★★ [1-5] 실제로 겪을 뻔한 버그 - LiveOverseasBroker.buy()/sell() 이
    client.create_order() 를 직접 불러서, 타임아웃(network-uncertain) 뒤
    다음 스캔에서 같은 종목을 재매수해 2배로 살 수 있었다. 국내주식과 같은
    설계(주문 의도 선기록 + clientOrderId + 재전송 대신 조회)로 막는다.
    """
    print("\n== ★★★ [1-5] 해외주식 실거래 - 타임아웃 뒤 재전송하지 않고 조회로 확인 ==")
    import time as _time

    import daytrader.orders as orders_mod
    from daytrader.orders import OrderBook, OrderUncertainError
    from daytrader.overseas_broker import LiveOverseasBroker, OverseasPositionBook

    orig_sleep = orders_mod.time.sleep
    orders_mod.time.sleep = lambda s: None  # resolve_uncertain 재시도 대기를 없애 테스트를 빠르게.
    try:
        # ★ 조회하면 기존 주문을 찾는 경우 - 매수가 정상적으로 인계되어야 한다.
        with tempfile.TemporaryDirectory() as d:
            client = FakeUncertainTossClient(fail_create=True, findable=True)
            book = OverseasPositionBook()
            broker = LiveOverseasBroker(client, book=book, order_book=OrderBook(d))

            pos = broker.buy("AAPL", 1_000_000, 150.0)
            check("★★이중 주문 없음(실제 주문 1건만 나감)", len(client.orders) == 1, len(client.orders))
            check("조회로 기존 주문을 찾아 매수가 정상 인계됨", pos is not None and book.owns("AAPL"))

        # ★ 조회해도 끝내 못 찾는 경우 - 재전송하지 않고 halt 되어야 한다.
        with tempfile.TemporaryDirectory() as d:
            client2 = FakeUncertainTossClient(fail_create=True, findable=False)
            book2 = OverseasPositionBook()
            broker2 = LiveOverseasBroker(client2, book=book2, order_book=OrderBook(d))

            try:
                broker2.buy("AAPL", 1_000_000, 150.0)
                check("★조회로도 확인 못 하면 OrderUncertainError 로 멈춤", False, "예외 없이 통과됨 - 심각한 버그")
            except OrderUncertainError:
                check("★조회로도 확인 못 하면 OrderUncertainError 로 멈춤", True)
            check("확인 못 하면 포지션이 기록되지 않음(다음 스캔에서 중복 매수 방지)", not book2.owns("AAPL"))
            check("★★★ 브로커가 halt 됨 - 다음 스캔에서도 재시도하지 않음", broker2.halted, broker2.halt_reason)
            check("주문은 1건만 나감(재전송 없음)", len(client2.orders) == 1, len(client2.orders))

        # ★ order_book 을 안 넘기면(연결이 빠지면) 실거래를 아예 못 내야 한다 -
        # 안전장치가 조용히 빠진 채로 실거래가 나가면 안 된다.
        broker3 = LiveOverseasBroker(FakeUncertainTossClient(fail_create=False), book=OverseasPositionBook())
        try:
            broker3.buy("AAPL", 1_000_000, 150.0)
            check("★order_book 없이는 실거래 자체를 거부함", False, "order_book 없이도 주문이 나감 - 심각한 버그")
        except RuntimeError:
            check("★order_book 없이는 실거래 자체를 거부함", True)
    finally:
        orders_mod.time.sleep = orig_sleep


def main() -> None:
    tests = [
        test_market_hours, test_trading_24h_session, test_us_theme_selection, test_manage_positions_only, test_snapshot_has_current_price, test_theme_reselect_on_phase_change, test_reuses_same_account_and_playbook,
        test_buy_then_take_profit, test_web_mode_never_buys,
        test_never_sells_unowned, test_never_sells_unowned_live,
        test_liquidate_all, test_state_persistence,
        test_market_hours_fallback_matches_zoneinfo, test_module_import_survives_missing_zoneinfo,
        test_sim_mode_ignores_real_market_hours, test_manage_position_survives_no_exit_verdict,
        test_auto_select_picks_from_rankings, test_auto_select_still_manages_dropped_holdings,
        test_sim_mode_works_without_real_api, test_overseas_buy_converts_krw_to_usd,
        test_overseas_rejects_non_us_tickers, test_cash_available_survives_none, test_update_peak_survives_none, test_survives_non_dict_api_rows, test_default_watchlist_has_mag7_and_ai_chips, test_state_restore_survives_corrupt_records, test_broker_survives_none_values, test_exit_survives_broken_position_fields,
        test_auto_select_skipped_when_market_closed, test_empty_config_values_fall_back_to_defaults,
        test_us_phase_distinguishes_sessions, test_halt_resumes_when_session_changes,
        test_overseas_live_buy_no_double_order_on_timeout,
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
