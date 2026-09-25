"""crypto_engine.py 오프라인 테스트. `python tests/test_crypto_engine.py` 로 실행한다."""

from __future__ import annotations

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader.config import load_config  # noqa: E402
from daytrader.crypto_engine import CryptoEngine  # noqa: E402

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


class FakeBithumbClient:
    """★ BithumbClient.candles() 의 계약(과거→최신 순)을 그대로 지킨다."""

    def __init__(self, price_map: dict, breakout_markets: set):
        self.price_map = dict(price_map)
        self.breakout_markets = breakout_markets
        self.sell_calls: list = []
        self.buy_calls: list = []

    def candles(self, market, unit=1, count=200, to=None):
        base = self.price_map[market] * 0.99
        step = self.price_map[market] * 0.003 if market in self.breakout_markets else 0.0
        # ★ 거래대금(종가×거래량)이 충분하게 - crypto.min_trading_value_krw(기본 500만원) 필터에
        # 걸려 매수 자체가 막히지 않도록, 가격 수준과 무관하게 항상 넉넉한 거래대금이 나오게 한다.
        volume = 100_000_000.0 / max(base, 1.0)
        rows = []
        for _ in range(count // 2):
            rows.append({"opening_price": base, "high_price": base * 1.005, "low_price": base * 0.995,
                         "trade_price": base, "candle_acc_trade_volume": volume})
        for _ in range(count - count // 2):
            base += step
            rows.append({"opening_price": base, "high_price": base * 1.004, "low_price": base * 0.999,
                         "trade_price": base, "candle_acc_trade_volume": volume})
        return rows

    def ticker(self, markets):
        return [{"market": m, "trade_price": self.price_map[m]} for m in markets]

    def accounts(self):
        return [{"currency": "KRW", "balance": "1000000"}]

    def place_order(self, market, side, order_type, **kw):
        if side == "ask":
            self.sell_calls.append((market, kw))
        else:
            self.buy_calls.append((market, kw))
        return {"uuid": "fake-order-id"}


def _cfg(tmp_dir: str, watchlist: list, k: float = 0.3):
    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    cfg.state_dir = tmp_dir
    cfg.crypto.watchlist = watchlist
    cfg.crypto.allocation_per_coin = 100000
    cfg.crypto.poll_seconds = 60
    cfg.crypto.k = k
    return cfg


def test_buy_then_take_profit() -> None:
    print("\n== 매수 -> 익절 매도 전체 사이클 ==")
    d = tempfile.mkdtemp()
    client = FakeBithumbClient({"KRW-BTC": 100_000_000.0}, breakout_markets={"KRW-BTC"})
    cfg = _cfg(d, ["KRW-BTC"])
    cfg.sizing.scale_out = False  # ★ 이 테스트는 분할 매도가 아니라 FixedExit 의 "전량 익절" 경로를 검증한다.
    engine = CryptoEngine(cfg, client=client)

    check("매수 전 보유 없음", not engine.state.book.owns("KRW-BTC"))

    engine.run_once()
    check("변동성 돌파로 매수됨", engine.state.book.owns("KRW-BTC"))
    if not engine.state.book.owns("KRW-BTC"):
        return
    entry_price = engine.state.book.get("KRW-BTC").entry_price

    client.price_map["KRW-BTC"] = entry_price * (1 + cfg.crypto.take_profit_pct + 0.03)  # 익절(cfg.crypto.take_profit_pct) 초과
    engine.run_once()
    check("익절 조건에서 매도됨", not engine.state.book.owns("KRW-BTC"))
    snap = engine.snapshot()
    check("청산 기록 1건", snap["closed_count"] == 1)
    if snap["closed"]:
        # ★ 이제 국내 Playbook의 FixedExit(손절+익절 통합)을 그대로 쓰므로
        # 청산 사유는 "fixed" 하나다 - 익절인지 손절인지는 pnl 부호로 구분한다.
        check("청산 사유가 fixed(국내와 동일한 기법)", snap["closed"][0]["reason"] == "fixed")
        check("실제로 이익이 남", (snap["closed"][0].get("pnl") or 0) > 0)


def test_stop_loss() -> None:
    print("\n== 손절 ==")
    d = tempfile.mkdtemp()
    client = FakeBithumbClient({"KRW-BTC": 100_000_000.0}, breakout_markets={"KRW-BTC"})
    engine = CryptoEngine(_cfg(d, ["KRW-BTC"]), client=client)
    engine.run_once()
    check("매수됨", engine.state.book.owns("KRW-BTC"))
    entry_price = engine.state.book.get("KRW-BTC").entry_price

    client.price_map["KRW-BTC"] = entry_price * 0.90  # -10% -> 손절(국내 cfg.risk.stop_loss_pct) 초과
    engine.run_once()
    check("손절 조건에서 매도됨", not engine.state.book.owns("KRW-BTC"))
    snap = engine.snapshot()
    if snap["closed"]:
        check("청산 사유가 fixed(국내와 동일한 기법)", snap["closed"][-1]["reason"] == "fixed")
        check("실제로 손실이 남", (snap["closed"][-1].get("pnl") or 0) < 0)


def test_state_persistence() -> None:
    print("\n== 상태 저장/복원 ==")
    d = tempfile.mkdtemp()
    client = FakeBithumbClient({"KRW-BTC": 100_000_000.0}, breakout_markets={"KRW-BTC"})
    engine = CryptoEngine(_cfg(d, ["KRW-BTC"]), client=client)
    engine.run_once()
    check("매수됨", engine.state.book.owns("KRW-BTC"))
    cash_before_restart = engine.broker.cash()

    engine2 = CryptoEngine(_cfg(d, ["KRW-BTC"]), client=client, state_path=engine.state.path)
    check("재시작해도 포지션이 유지됨", engine2.state.book.owns("KRW-BTC"))
    # ★★★ 실제로 겪은 버그 - 재시작마다 현금이 매수 여부와 무관하게 총 투자금액 그대로
    # 초기화돼("대시보드 현금 잔여금액이 안 맞다"), 보유 종목에 묶인 돈과 새로 채워진 현금이
    # 동시에 존재하는 이중 계산이 됐다. 재시작 전후로 현금이 같아야 한다(포지션은 그대로
    # 있고 새로 산 것도 없으므로).
    check("재시작해도 현금이 매수 전 총액으로 되돌아가지 않음(이중 계산 없음)",
          abs(engine2.broker.cash() - cash_before_restart) < 1.0,
          f"{cash_before_restart} vs {engine2.broker.cash()}")


def test_never_sells_preexisting_holding() -> None:
    print("\n== ★★★ 기존 보유 코인 절대 매도 금지(엔진 레벨) ==")
    d = tempfile.mkdtemp()
    # KRW-BTC 는 변동성 돌파로 사고팔릴 수 있게, KRW-XRP 는 계좌에 이미
    # 있다고 가정하되(accounts() 참고) 변동성 신호는 없게 만든다.
    client = FakeBithumbClient(
        {"KRW-BTC": 100_000_000.0, "KRW-XRP": 800.0}, breakout_markets={"KRW-BTC"},
    )
    client.accounts = lambda: [
        {"currency": "KRW", "balance": "1000000"}, {"currency": "XRP", "balance": "500"},
    ]
    engine = CryptoEngine(_cfg(d, ["KRW-BTC", "KRW-XRP"]), client=client)

    for _ in range(5):
        engine.run_once()

    check("계좌에 실제로 있는 XRP 는 장부에 절대 들어가지 않음", not engine.state.book.owns("KRW-XRP"))
    check("★★★ XRP 는 5바퀴를 돌려도 청산 기록에 전혀 없음",
          not any(c["market"] == "KRW-XRP" for c in engine.state.closed))
    check("XRP 매도 주문 API가 호출된 적 없음", not any(c[0] == "KRW-XRP" for c in client.sell_calls))


def test_mode_independent_from_stock() -> None:
    print("\n== ★★★ 코인 실거래는 국내주식 mode와 완전히 독립 ==")
    d = tempfile.mkdtemp()
    client = FakeBithumbClient({"KRW-BTC": 100_000_000.0}, breakout_markets=set())

    cfg = _cfg(d, ["KRW-BTC"])
    cfg.mode = "live"          # 국내주식은 실거래인데
    cfg.crypto.live = False    # 코인 실거래 스위치는 꺼둠
    cfg.bithumb_access_key = "fake"
    cfg.bithumb_secret_key = "fake"
    engine = CryptoEngine(cfg, client=client)
    check("★★★ 국내주식 live여도 crypto.live=false면 코인은 모의매매", engine.is_live is False)

    d2 = tempfile.mkdtemp()
    cfg2 = _cfg(d2, ["KRW-BTC"])
    cfg2.mode = "sim"          # 국내주식은 연습 모드인데
    cfg2.crypto.live = True    # 코인만 실거래로 켬
    cfg2.bithumb_access_key = "fake"
    cfg2.bithumb_secret_key = "fake"
    engine2 = CryptoEngine(cfg2, client=client)
    check("국내주식이 연습 모드여도 코인은 독립적으로 실거래 가능", engine2.is_live is True)


def test_daily_risk_limits() -> None:
    print("\n== 24시간 손실한도 / 연속손절 중단 ==")
    d = tempfile.mkdtemp()
    client = FakeBithumbClient({"KRW-BTC": 100_000_000.0}, breakout_markets=set())
    cfg = _cfg(d, ["KRW-BTC"])
    cfg.crypto.consecutive_loss_halt = 2
    cfg.crypto.daily_loss_limit_pct = 0.5
    engine = CryptoEngine(cfg, client=client)
    now = time.time()

    engine.state.closed = [
        {"market": "KRW-BTC", "quantity": 0.01, "entry_price": 100000, "pnl": -30, "exit_time": now - 100},
        {"market": "KRW-ETH", "quantity": 0.01, "entry_price": 100000, "pnl": -30, "exit_time": now - 50},
    ]
    check("연속 손절 2회면 중단 사유가 뜸", "연속 손절" in engine._halted_reason())

    engine.state.closed[-1] = {"market": "KRW-XRP", "quantity": 0.01, "entry_price": 100000, "pnl": +30, "exit_time": now - 50}
    check("중간에 익절이 끼면 연속 카운트가 끊김", engine._halted_reason() == "")

    engine.state.closed = [
        {"market": "KRW-BTC", "quantity": 0.01, "entry_price": 100000, "pnl": -30, "exit_time": now - 25 * 3600},
    ]
    check("24시간 넘은 청산은 카운트 안 함", engine._halted_reason() == "")

    cfg2 = _cfg(tempfile.mkdtemp(), ["KRW-BTC"])
    cfg2.crypto.daily_loss_limit_pct = 0.05
    cfg2.crypto.consecutive_loss_halt = 10
    engine2 = CryptoEngine(cfg2, client=client)
    engine2.state.closed = [
        {"market": "KRW-BTC", "quantity": 0.01, "entry_price": 100000, "pnl": -100, "exit_time": now - 10},
    ]
    check("일일 손실 한도(투자금 대비 10% 손실 vs 5% 한도) 도달 감지", "손실 한도" in engine2._halted_reason())

    from daytrader.bithumb_broker import CryptoPosition
    engine2.state.book.record_buy(CryptoPosition(
        market="KRW-BTC", quantity=1, entry_price=200, entry_time=now - 10000, peak_price=200,
    ))
    client2 = FakeBithumbClient({"KRW-BTC": 100.0}, breakout_markets=set())  # -50% -> 손절
    engine2.client = client2
    engine2.run_once()
    check("★신규진입 중단 중에도 기존 포지션 청산은 계속됨", not engine2.state.book.owns("KRW-BTC"))


def test_web_mode_never_buys() -> None:
    print("\n== 관찰(web) 모드에서는 신호가 나도 실제로 사지 않음 ==")
    d = tempfile.mkdtemp()
    client = FakeBithumbClient({"KRW-BTC": 100_000_000.0}, breakout_markets={"KRW-BTC"})
    cfg = _cfg(d, ["KRW-BTC"])
    cfg.crypto.mode = "web"
    engine = CryptoEngine(cfg, client=client)
    engine.run_once()
    check("web 모드에서는 보유 포지션이 생기지 않음", not engine.state.book.owns("KRW-BTC"))
    check("실제 매수 주문 API가 호출된 적 없음", len(client.buy_calls) == 0)


def test_liquidate_all() -> None:
    print("\n== 전량 청산 후 정지 ==")
    from daytrader.bithumb_broker import CryptoPosition

    d = tempfile.mkdtemp()
    client = FakeBithumbClient({"KRW-BTC": 100_000_000.0, "KRW-ETH": 4_000_000.0}, breakout_markets=set())
    cfg = _cfg(d, ["KRW-BTC", "KRW-ETH"])
    engine = CryptoEngine(cfg, client=client)

    engine.state.book.record_buy(CryptoPosition(
        market="KRW-BTC", quantity=0.001, entry_price=95_000_000, entry_time=time.time() - 1000, peak_price=95_000_000,
    ))
    engine.state.book.record_buy(CryptoPosition(
        market="KRW-ETH", quantity=0.01, entry_price=3_900_000, entry_time=time.time() - 500, peak_price=3_900_000,
    ))

    count = engine.liquidate_all()
    check("2건 모두 청산됨", count == 2)
    check("보유가 비었음", not engine.state.book.owns("KRW-BTC") and not engine.state.book.owns("KRW-ETH"))
    check("청산 사유가 force_close로 남음", all(c["reason"] == "force_close" for c in engine.state.closed))

    # ★ 전량 청산도 "이 엔진이 사지 않은 코인"은 절대 안 건드려야 한다.
    client2 = FakeBithumbClient({"KRW-XRP": 800.0}, breakout_markets=set())
    engine2 = CryptoEngine(_cfg(tempfile.mkdtemp(), ["KRW-XRP"]), client=client2)
    count2 = engine2.liquidate_all()
    check("★보유 장부가 비어 있으면 청산할 것도 없음(0건)", count2 == 0)
    check("매도 API가 호출된 적 없음", len(client2.sell_calls) == 0)


def test_playbook_selects_correct_technique() -> None:
    print("\n== CryptoPlaybook - 여러 기법 중 실제로 통과한 기법이 정확히 기록됨 ==")
    d = tempfile.mkdtemp()

    class FlatClient(FakeBithumbClient):
        """완전히 평평한 시세 - 어떤 진입 기법도 통과하면 안 된다."""
        def candles(self, market, unit=1, count=200, to=None):
            base = self.price_map[market] * 0.99
            return [{"opening_price": base, "high_price": base * 1.001, "low_price": base * 0.999, "trade_price": base}] * count

    client = FlatClient({"KRW-BTC": 100_000_000.0}, breakout_markets=set())
    cfg = _cfg(d, ["KRW-BTC"])
    engine = CryptoEngine(cfg, client=client)
    engine.run_once()
    check("평평한 시세에서는 어떤 기법도 통과 못해 매수 안 함", not engine.state.book.owns("KRW-BTC"))

    d2 = tempfile.mkdtemp()
    client2 = FakeBithumbClient({"KRW-BTC": 100_000_000.0}, breakout_markets={"KRW-BTC"})
    cfg2 = _cfg(d2, ["KRW-BTC"], k=0.3)
    engine2 = CryptoEngine(cfg2, client=client2)
    engine2.run_once()
    pos = engine2.state.book.get("KRW-BTC")
    check("명확한 돌파에서는 매수됨", pos is not None)
    if pos:
        check("진입 기법명이 정확히 volatility_breakout으로 기록됨", pos.technique == "volatility_breakout")


def test_crypto_entry_order_validation() -> None:
    print("\n== crypto.entry_order 검증 ==")
    from daytrader.config import load_config
    import yaml

    # ★ encoding 을 안 주면 Windows 에서 시스템 로케일(cp949)로 읽어
    # config.yaml 의 한글 주석에서 UnicodeDecodeError 가 난다.
    raw = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    raw["crypto"]["entry_order"] = []
    tmp_path = os.path.join(tempfile.mkdtemp(), "bad.yaml")
    yaml.dump(raw, open(tmp_path, "w"), allow_unicode=True)
    try:
        load_config(tmp_path)
        check("빈 entry_order 거부", False, "통과됨 - 버그")
    except ValueError:
        check("빈 entry_order 거부", True)

    raw["crypto"]["entry_order"] = ["존재하지_않는_기법"]
    yaml.dump(raw, open(tmp_path, "w"), allow_unicode=True)
    try:
        load_config(tmp_path)
        check("알 수 없는 기법 거부", False, "통과됨 - 버그")
    except ValueError:
        check("알 수 없는 기법 거부", True)


def test_manage_position_survives_no_exit_verdict() -> None:
    """★★★ 실제로 겪은 버그 - Playbook.evaluate_exit() 은 모든 청산
    기법이 조건 미충족이면 verdict 자체가 None 이다(막 매수해서 아직
    손절·익절·추적·시간청산 어느 것도 안 닿은 흔한 상황). None 체크 없이
    verdict.ok 를 읽으면 "'NoneType' object has no attribute 'ok'" 로
    죽는다 - 국내 Playbook 을 재사용하게 되면서 이 체크를 빠뜨렸었다.
    """
    print("\n== 청산 조건이 전부 미충족(verdict=None)이어도 예외 없이 보유 유지 ==")
    from daytrader.bithumb_broker import CryptoPosition
    import time as _time

    d = tempfile.mkdtemp()

    class FlatClient(FakeBithumbClient):
        """완전히 평평한 시세 - 손절/익절/추적/시간청산 전부 조건 미충족."""
        def candles(self, market, unit=1, count=200, to=None):
            base = self.price_map[market]
            return [{"opening_price": base, "high_price": base * 1.001, "low_price": base * 0.999, "trade_price": base}] * count

    client = FlatClient({"KRW-BTC": 100_000_000.0}, breakout_markets=set())
    cfg = _cfg(d, ["KRW-BTC"])
    engine = CryptoEngine(cfg, client=client)
    engine.state.book.record_buy(CryptoPosition(
        market="KRW-BTC", quantity=0.001, entry_price=100_000_000.0, entry_time=_time.time(),
        peak_price=100_000_000.0, technique="volatility_breakout", name="KRW-BTC", theme="암호화폐",
    ))
    try:
        engine._manage_position("KRW-BTC", engine.state.book.get("KRW-BTC"))
        check("예외 없이 정상 처리됨(AttributeError 재발 안 함)", True)
    except AttributeError as exc:
        check("예외 없이 정상 처리됨(AttributeError 재발 안 함)", False, str(exc))
    check("청산 조건 미충족이니 여전히 보유 중", engine.state.book.owns("KRW-BTC"))


def test_crypto_sim_mode_works_without_real_api() -> None:
    """★★★ 실제로 겪은 문제 - 암호화폐 sim(시뮬레이션) 모드가 이름과 달리
    진짜 빗썸 API 를 호출해서, 키가 없거나 네트워크가 막히면 아무것도
    못 하고 조용히 실패했다("시뮬레이션이 전혀 동작 안 한다"). 이제
    합성 시세(SimFeedClient)로 실제 API 없이 돌아야 한다.
    """
    print("\n== 암호화폐 sim 모드가 실제 API 없이 정상 매매함 ==")
    d = tempfile.mkdtemp()
    cfg = _cfg(d, ["KRW-BTC", "KRW-ETH", "KRW-XRP"])
    cfg.crypto.mode = "sim"
    engine = CryptoEngine(cfg, client=None)  # ★ 클라이언트를 아예 안 준다.
    check("sim 모드는 합성 시세 클라이언트를 씀",
          type(engine.client).__name__ == "SimFeedClient")

    for _ in range(5):
        engine.run_once()
        if engine.state.book.all():
            break
    check("실제 API 없이도 매수가 일어남", bool(engine.state.book.all()), engine.last_error)
    check("네트워크 오류가 남지 않음", not engine.last_error, engine.last_error)


def test_crypto_evaluates_all_enabled_techniques() -> None:
    """★★★ "여러 진입기법을 켰는데 화면엔 변동성 돌파만 나온다 - 실제로
    변동성 돌파만 쓰는 거냐"는 질문에서 나온 검증. 실제 동작은 켜진
    기법을 전부 평가하는 게 맞는데, 화면 문구가 암호화폐 초기(변동성
    돌파 하나뿐이던 시절) 그대로 남아 오해를 부르고 있었다.
    """
    print("\n== 암호화폐가 켜진 진입기법을 전부 평가하고 화면에도 알림 ==")
    d = tempfile.mkdtemp()
    cfg = _cfg(d, ["KRW-BTC"])
    # ★ 여러 기법을 켠 상황
    cfg.crypto.entry_order = ["volatility_breakout", "breakout", "rsi_pullback"]
    engine = CryptoEngine(cfg, client=FakeBithumbClient({"KRW-BTC": 100_000_000.0}, breakout_markets=set()))

    check("Playbook 에 켠 기법이 전부 들어감", len(engine.playbook.entries) == 3,
          str([t.key for t in engine.playbook.entries]))

    snap = engine.snapshot()
    labels = snap.get("entry_techniques") or []
    check("★★★ snapshot 이 켜진 기법을 전부 알려줌(화면이 정확히 표시할 수 있게)",
          len(labels) == 3, str(labels))
    check("변동성 돌파 하나로 단정하지 않음", "변동성 돌파" in labels and len(labels) > 1, str(labels))


def test_closed_trade_records_entry_technique() -> None:
    """★★★ "거래내역의 기법이 매도 기법인 것 같다 - 매수 기법도 추가해
    달라"는 지적. 맞았다 - 청산 기록의 technique/reason 은 청산 판정
    결과라 매도 기법이고, 어떤 기법으로 샀는지는 Position 에만 있고
    거래 기록에 옮기지 않아 화면에서 볼 수 없었다.
    """
    print("\n== 청산 기록에 매수 기법이 함께 남음 ==")
    import time as _time
    from daytrader.bithumb_broker import CryptoPosition

    d = tempfile.mkdtemp()
    cfg = _cfg(d, ["KRW-BTC"])
    cfg.crypto.mode = "sim"
    engine = CryptoEngine(cfg, client=None)

    engine.state.book.record_buy(CryptoPosition(
        market="KRW-BTC", quantity=0.01, entry_price=95_000_000, entry_time=_time.time(),
        peak_price=96_000_000, technique="breakout", name="KRW-BTC", theme="암호화폐",
    ))
    engine._execute_exit("KRW-BTC", engine.state.book.get("KRW-BTC"), 100_000_000, "fixed")

    rec = engine.state.closed[-1]
    check("매도 사유(reason)가 기록됨", rec.get("reason") == "fixed", str(rec.get("reason")))
    check("★★★ 매수 기법(entry_technique)이 함께 기록됨",
          rec.get("entry_technique") == "breakout", str(rec.get("entry_technique")))
    check("둘이 서로 다른 값(매수/매도 구분됨)",
          rec.get("entry_technique") != rec.get("reason"))


def test_loss_halt_cooldown_and_resume_time() -> None:
    """★★★ "언제 매매가 재개되는지 시간을 표기하고, 재개 간격을 설정하게
    해달라"는 요청. 예전엔 "직전 24시간 창에서 손절이 빠질 때까지"라는
    암묵적 규칙뿐이라 재개 시점을 알 수 없었다.
    ★★★ "연속 손절후 다음 진입시기가 너무 늦어. 모든 장에서 3시간 또는
    세션이 바뀌면 진입 가능하게 변경해" 요청으로 4개 시장 모두 기본 3시간.
    """
    print("\n== 연속 손절 후 설정한 시간이 지나면 자동 재개 ==")
    import time as _time

    d = tempfile.mkdtemp()
    cfg = _cfg(d, ["KRW-BTC"])
    cfg.crypto.mode = "sim"
    check("암호화폐 기본 재개 간격이 3시간", cfg.crypto.loss_halt_cooldown_hours == 3,
          str(cfg.crypto.loss_halt_cooldown_hours))

    engine = CryptoEngine(cfg, client=None)
    now = _time.time()
    # ★ 손실 한도(5%)에는 안 걸리도록 소액 손실로 3연속을 만든다.
    engine.state.closed = [
        {"market": "A", "pnl": -3000, "exit_time": now - 3600, "entry_price": 1_000_000, "quantity": 1},
        {"market": "B", "pnl": -3000, "exit_time": now - 1800, "entry_price": 1_000_000, "quantity": 1},
        {"market": "C", "pnl": -3000, "exit_time": now - 600, "entry_price": 1_000_000, "quantity": 1},
    ]
    info = engine.halt_info()
    check("연속 손절 3회로 중단됨", info["halted"], info.get("reason", ""))
    check("★★★ 사유에 재개 예정 시각이 표기됨", "재개 예정" in info.get("reason", ""), info.get("reason", ""))
    remain = (info["resume_at"] - now) / 3600
    check("재개까지 남은 시간이 정확함(3시간 - 10분)", 2.7 < remain < 2.9, f"{remain:.2f}시간")

    # ★ 설정한 시간이 지나면 자동 재개돼야 한다.
    for i, c in enumerate(engine.state.closed):
        c["exit_time"] = now - (4.5 - i * 0.5) * 3600
    check("★★★ 3시간이 지나면 자동 재개됨", not engine.halt_info()["halted"])

    # ★ 설정을 바꾸면 그대로 반영돼야 한다.
    cfg.crypto.loss_halt_cooldown_hours = 2.0
    engine2 = CryptoEngine(cfg, client=None)
    engine2.state.closed = [
        {"market": "A", "pnl": -3000, "exit_time": now - 600, "entry_price": 1_000_000, "quantity": 1},
        {"market": "B", "pnl": -3000, "exit_time": now - 500, "entry_price": 1_000_000, "quantity": 1},
        {"market": "C", "pnl": -3000, "exit_time": now - 400, "entry_price": 1_000_000, "quantity": 1},
    ]
    info2 = engine2.halt_info()
    remain2 = (info2["resume_at"] - now) / 3600
    check("설정을 2시간으로 바꾸면 그대로 반영됨", 1.8 < remain2 < 2.0, f"{remain2:.2f}시간")

    # ★ snapshot 이 화면에 재개 정보를 전달해야 한다.
    snap = engine2.snapshot()
    check("snapshot 이 중단·재개 정보를 전달함",
          snap.get("halt", {}).get("halted") is True and snap["halt"].get("resume_at"))


def test_reentry_cooldown_after_loss() -> None:
    print("\n== ★ 손절 직후 재진입 쿨다운(실제 이력: 손절 후 30분 내 재진입 10건 전부 손실) ==")
    import time as _t
    d = tempfile.mkdtemp()
    client = FakeBithumbClient({"KRW-BTC": 100_000_000.0}, breakout_markets={"KRW-BTC"})
    engine = CryptoEngine(_cfg(d, ["KRW-BTC"]), client=client)
    engine.state.closed.append({"market": "KRW-BTC", "pnl": -500.0, "exit_time": _t.time() - 300})
    engine.run_once()
    check("5분 전 손절이면 신호가 나도 재진입하지 않음", not engine.state.book.owns("KRW-BTC"))
    engine.state.closed[-1]["exit_time"] = _t.time() - 31 * 60
    engine.run_once()
    check("30분이 지나면 다시 진입 가능", engine.state.book.owns("KRW-BTC"))

    d2 = tempfile.mkdtemp()
    engine2 = CryptoEngine(_cfg(d2, ["KRW-BTC"]), client=client)
    engine2.state.closed.append({"market": "KRW-BTC", "pnl": 900.0, "exit_time": _t.time() - 60})
    engine2.run_once()
    check("직전 청산이 이익이면 쿨다운 없음", engine2.state.book.owns("KRW-BTC"))


def test_top_volume_watchlist_expansion() -> None:
    print("\n== 전날 거래대금 상위 10종목만 감시 ==")
    d = tempfile.mkdtemp()
    client = FakeBithumbClient({"KRW-BTC": 100_000_000.0}, breakout_markets=set())
    names = [f"KRW-C{i:02d}" for i in range(40)] + ["KRW-USDT"]
    client.markets = lambda: [{"market": n} for n in names] + [{"market": "BTC-ETH"}]
    client.ticker = lambda ms: [{"market": m, "acc_trade_price_24h": 1000 - i} for i, m in enumerate(ms)]
    vol = {n: (10_000 - i * 10) for i, n in enumerate(names)}
    vol["KRW-USDT"] = 10 ** 9  # 거래대금 1등이지만 스테이블코인
    client.day_candles = lambda m, count=2: [
        {"candle_date_time_kst": "2026-09-22T00:00:00", "candle_acc_trade_price": 1},
        {"candle_date_time_kst": "2026-09-21T00:00:00", "candle_acc_trade_price": vol[m]},
    ]
    engine = CryptoEngine(_cfg(d, ["KRW-BTC"]), client=client)
    picked = engine._fetch_top_volume_markets("2026-09-22")
    check("상위 10종목만(설정 top_volume_count=10)", len(picked) == 10, str(picked))
    check("전날(오늘 이전) 일봉의 거래대금 순위대로", picked == [f"KRW-C{i:02d}" for i in range(10)], str(picked))
    check("스테이블코인(USDT)은 제외", "KRW-USDT" not in picked)
    engine._auto_watchlist = picked
    w = engine.effective_watchlist()
    check("선정된 10종목만 감시하고 기본 목록(BTC)은 제외", set(picked) == set(w) and "KRW-BTC" not in w, str(w))
    engine._auto_watchlist = []
    check("선정에 실패하면 기본 목록으로 되돌아감", engine.effective_watchlist() == ["KRW-BTC"])
    engine._auto_watchlist = picked
    from daytrader.bithumb_broker import PaperBithumbBroker  # noqa: F401
    engine.state.book.record_buy(__import__("daytrader.bithumb_broker", fromlist=["CryptoPosition"]).CryptoPosition(
        market="KRW-ZZZ", quantity=1.0, entry_price=100.0, entry_time=time.time(), peak_price=100.0,
    ))
    check("보유 중인 코인은 목록에서 빠져도 계속 관리 대상", "KRW-ZZZ" in engine.effective_watchlist())


def test_low_volume_market_not_bought() -> None:
    print("\n== ★ \"암호화폐는 거래량이 적은 경우 거래를 하지 않도록 보완해\" - 거래대금이 얇으면 신호가 나도 매수 안 함 ==")
    d = tempfile.mkdtemp()
    client = FakeBithumbClient({"KRW-BTC": 100_000_000.0}, breakout_markets={"KRW-BTC"})
    # ★ FakeBithumbClient 는 기본적으로 넉넉한 거래대금(1억원 상당)을 흉내내므로, 이 테스트만
    # 거래대금을 아주 얇게(1원어치) 바꿔서 필터가 실제로 막는지 확인한다.
    orig_candles = client.candles

    def thin_candles(market, unit=1, count=200, to=None):
        rows = orig_candles(market, unit=unit, count=count, to=to)
        for r in rows:
            r["candle_acc_trade_volume"] = 1.0 / max(r["trade_price"], 1.0)  # 봉당 거래대금 약 1원
        return rows

    client.candles = thin_candles
    cfg = _cfg(d, ["KRW-BTC"])
    check("설정 기본값 500만원", cfg.crypto.min_trading_value_krw == 5_000_000.0, cfg.crypto.min_trading_value_krw)
    engine = CryptoEngine(cfg, client=client)
    engine.run_once()
    check("거래대금이 얇으면 돌파 신호가 나도 매수하지 않음", not engine.state.book.owns("KRW-BTC"))

    cfg2 = _cfg(d, ["KRW-BTC"])
    cfg2.crypto.min_trading_value_krw = 0  # 꺼두면 예전처럼 거래대금과 무관하게 매수
    engine2 = CryptoEngine(cfg2, client=client)
    engine2.run_once()
    check("필터를 꺼두면(0) 거래대금이 얇아도 매수함", engine2.state.book.owns("KRW-BTC"))


def main() -> None:
    tests = [
        test_buy_then_take_profit, test_low_volume_market_not_bought, test_stop_loss, test_state_persistence,
        test_never_sells_preexisting_holding, test_mode_independent_from_stock, test_daily_risk_limits,
        test_web_mode_never_buys, test_liquidate_all,
        test_playbook_selects_correct_technique, test_crypto_entry_order_validation,
        test_manage_position_survives_no_exit_verdict, test_crypto_sim_mode_works_without_real_api,
        test_crypto_evaluates_all_enabled_techniques, test_closed_trade_records_entry_technique,
        test_loss_halt_cooldown_and_resume_time,
        test_reentry_cooldown_after_loss, test_top_volume_watchlist_expansion,
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
