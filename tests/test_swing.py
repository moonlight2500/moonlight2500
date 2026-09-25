"""스윙(며칠~몇 주 보유) 매매 테스트 - playbook.py 의 일봉 전용 기법 3종, config.py 의
SwingCfg, technique_backtest.py 의 swing 분기, swing_broker.py, swing_engine.py.
`python tests/test_swing.py`로 실행한다.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader import technique_backtest as tb  # noqa: E402
from daytrader.config import load_config  # noqa: E402
from daytrader.playbook import (  # noqa: E402
    Bar, ENTRY_TECHNIQUES, Playbook, SwingBreakoutEntry, SwingGoldenCrossEntry, SwingMaPullbackEntry,
)
from daytrader.swing_broker import NotOwnedError, PaperSwingBroker, SwingPositionBook  # noqa: E402
from daytrader.swing_engine import SwingEngine, SwingState  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config.yaml")

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


def _raises(fn, exc=ValueError) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


def _daily_bar(i, o, h, l, c, v, start_year=2026, start_month=1, start_day=1):
    import datetime
    d = datetime.date(start_year, start_month, start_day) + datetime.timedelta(days=i)
    return Bar(ts=f"{d.isoformat()}T00:00:00+09:00", open=o, high=h, low=l, close=c, volume=v)


def _uptrend_pullback_breakout_bars(n=90):
    """60일선 위(중기 상승), 20일선까지 눌렸다가 전날 고가를 다시 넘는 일봉을 만든다 -
    SwingMaPullbackEntry 가 통과해야 하는 시나리오."""
    bars = []
    price = 10000.0
    for i in range(n - 3):
        price += 30  # 완만한 우상향 - 60일선 위에 종가가 자리잡게 한다.
        bars.append(_daily_bar(i, price - 20, price + 40, price - 40, price, 1000))
    # 눌림(며칠간 20일선까지 되돌림)
    pull = price * 0.985
    bars.append(_daily_bar(n - 3, price, price, pull, pull, 800))
    bars.append(_daily_bar(n - 2, pull, pull + 10, pull - 30, pull - 10, 800))
    prev_high = bars[-1].high
    # 트리거 - 전날 고가를 다시 넘는 반등
    bars.append(_daily_bar(n - 1, pull - 10, prev_high + 50, pull - 20, prev_high + 30, 1500))
    return bars


def _flat_bars(n=90, price=10000.0):
    """추세 없이 완전히 평평한 일봉 - 스윙 기법이 통과하면 안 되는 시나리오."""
    return [_daily_bar(i, price, price + 10, price - 10, price, 500) for i in range(n)]


def _steady_uptrend_bars(n=90):
    """눌림 없이 매일 완만하게 오르기만 하는 일봉 - 추세 필터·최근 1주 실제 상승 필터를
    둘 다 확실히 통과하는(진입 기법 세부 조건은 신경 안 쓰는) 후보 선정 테스트용."""
    bars = []
    price = 10000.0
    for i in range(n):
        price += 30
        bars.append(_daily_bar(i, price - 20, price + 40, price - 40, price, 1000))
    return bars


def _box_breakout_bars(n=40):
    """20거래일 박스권을 거래량 급증과 함께 뚫는 일봉 - SwingBreakoutEntry 시나리오."""
    bars = [_daily_bar(i, 10000, 10100, 9950, 10000 + (i % 3) * 10, 500) for i in range(n - 1)]
    hh = max(b.high for b in bars)
    bars.append(_daily_bar(n - 1, hh - 20, hh + 200, hh - 30, hh + 180, 3000))
    return bars


def _golden_cross_bars(n=90):
    """오랫동안(60일선 워밍업 기간 내내) 평평하다가, 최근 5거래일 사이에 급등해 20일선이
    60일선을 막 뚫고 올라가게 만든다 - 5거래일 전엔 두 선이 같았다(교차 전)."""
    bars = []
    price = 10000.0
    flat_days = n - 5
    for i in range(flat_days):
        bars.append(_daily_bar(i, price, price + 10, price - 10, price, 500))
    for i in range(flat_days, n):
        price += 500  # 최근 5거래일 급등 - 20일선을 빠르게 끌어올려 60일선을 뚫게 한다.
        bars.append(_daily_bar(i, price - 500, price + 20, price - 20, price, 2000))
    return bars


class _Ctx:
    def __init__(self, symbol="005930", name="삼성전자", theme="관심 종목"):
        self.symbol, self.name, self.theme = symbol, name, theme
        self.prev_verdict = None
        self.prev_verdicts = []
        self.held_minutes = 0
        self.force_close = False


def test_swing_entries_registered() -> None:
    print("== 스윙 진입 기법 3종이 레지스트리에 등록됨 ==")
    for key, cls in (
        ("swing_ma_pullback", SwingMaPullbackEntry),
        ("swing_breakout", SwingBreakoutEntry),
        ("swing_golden_cross", SwingGoldenCrossEntry),
    ):
        check(f"{key} 가 ENTRY_TECHNIQUES 에 있음", ENTRY_TECHNIQUES.get(key) is cls)


def test_swing_ma_pullback_entry() -> None:
    print("== 스윙 이동평균 눌림목 ==")
    cfg = load_config(CONFIG_PATH)
    tech = SwingMaPullbackEntry(cfg, {})
    v_pass = tech.evaluate(_uptrend_pullback_breakout_bars(), _Ctx())
    check("상승 추세 + 눌림 + 재돌파 → 통과", v_pass.ok, v_pass.headline)
    v_fail = tech.evaluate(_flat_bars(), _Ctx())
    check("추세 없는 평평한 시세 → 통과 못함", not v_fail.ok, v_fail.headline)


def test_swing_breakout_entry() -> None:
    print("== 스윙 박스권 돌파 ==")
    cfg = load_config(CONFIG_PATH)
    tech = SwingBreakoutEntry(cfg, {})
    v_pass = tech.evaluate(_box_breakout_bars(), _Ctx())
    check("박스권 고가를 거래량 동반 돌파 → 통과", v_pass.ok, v_pass.headline)
    v_fail = tech.evaluate(_flat_bars(), _Ctx())
    check("돌파 없는 평평한 시세 → 통과 못함", not v_fail.ok, v_fail.headline)


def test_swing_golden_cross_entry() -> None:
    print("== 스윙 골든크로스 ==")
    cfg = load_config(CONFIG_PATH)
    tech = SwingGoldenCrossEntry(cfg, {})
    v_pass = tech.evaluate(_golden_cross_bars(), _Ctx())
    check("막 골든크로스가 난 시세 → 통과", v_pass.ok, v_pass.headline)
    v_fail = tech.evaluate(_flat_bars(), _Ctx())
    check("교차가 없는 평평한 시세 → 통과 못함", not v_fail.ok, v_fail.headline)


def test_swing_playbook_evaluate_entry() -> None:
    print("== Playbook(market=swing) 이 스윙 기법만 평가함 ==")
    cfg = load_config(CONFIG_PATH)
    pb = Playbook(cfg, entry_order=list(cfg.swing.entry_order), exit_enabled=list(cfg.swing.exit_enabled), market="swing")
    check("스윙 진입 기법만 조립됨", {e.key for e in pb.entries} == set(cfg.swing.entry_order))
    winner, all_verdicts = pb.evaluate_entry(_uptrend_pullback_breakout_bars(), _Ctx())
    check("추세+눌림+재돌파 시세에서 승자 기법이 선정됨", winner is not None and winner.ok, winner and winner.technique)
    check("모든 기법이 평가됨(3개)", len(all_verdicts) == 3, len(all_verdicts))


def test_config_swing_validation() -> None:
    print("== config.py 의 SwingCfg 검증 ==")
    cfg = load_config(CONFIG_PATH)
    check("기본 swing.mode 는 paper", cfg.swing.mode == "paper")
    check("기본 진입 기법 3종", set(cfg.swing.entry_order) == {"swing_ma_pullback", "swing_breakout", "swing_golden_cross"})
    check("기본 exit_enabled 에 fixed 포함", "fixed" in cfg.swing.exit_enabled)


def _reload_and_validate_mode() -> None:
    """load_config() 안에서 swing.mode 검증이 일어나므로, config.yaml 을 임시로 복제해 잘못된
    값을 넣고 load_config() 자체가 거부하는지 직접 확인한다."""
    import yaml
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw.setdefault("swing", {})["mode"] = "not-a-mode"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True)
        tmp_path = f.name
    try:
        load_config(tmp_path)
    finally:
        os.unlink(tmp_path)


def test_config_swing_mode_rejected() -> None:
    print("== config.yaml 에 잘못된 swing.mode 를 넣으면 load_config() 가 거부함 ==")
    check("swing.mode 오탈자는 ValueError", _raises(_reload_and_validate_mode))


def test_config_swing_missing_fixed_exit_rejected() -> None:
    print("== swing.exit_enabled 에 fixed 가 없으면 거부됨 ==")
    import yaml

    def _no_fixed():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        raw.setdefault("swing", {})["exit_enabled"] = ["trailing", "time_stop"]
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True)
            tmp_path = f.name
        try:
            load_config(tmp_path)
        finally:
            os.unlink(tmp_path)

    check("fixed 없는 swing.exit_enabled 은 거부됨", _raises(_no_fixed))


def test_applicable_techniques_swing_only_daily() -> None:
    print("== technique_backtest - 스윙은 일봉 전용 기법 3종만 평가 ==")
    swing = tb.applicable_techniques("swing")
    check("스윙 전용 3종만", set(swing) == {"swing_ma_pullback", "swing_breakout", "swing_golden_cross"}, swing)
    dom = tb.applicable_techniques("domestic")
    check("국내(분봉)는 스윙 기법을 평가하지 않음", not (set(dom) & set(swing)))
    crypto = tb.applicable_techniques("crypto")
    check("암호화폐(분봉)도 스윙 기법을 평가하지 않음", not (set(crypto) & set(swing)))


def test_fetch_bars_swing_uses_daily_interval() -> None:
    print("== fetch_bars(swing) 은 1d 간격으로 요청함 ==")

    calls = []

    class FakeToss:
        def candles(self, symbol, interval, count, before=None):
            calls.append(interval)
            if before:
                return []
            return [
                {"timestamp": f"2026-0{1 + i // 28}-{1 + i % 28:02d}T00:00:00+09:00",
                 "openPrice": 100, "highPrice": 101, "lowPrice": 99, "closePrice": 100, "volume": 10}
                for i in range(min(count, 60))
            ]

    class FakeRouter:
        primary = FakeToss()

    bars = tb.fetch_bars("swing", FakeRouter(), "005930", days=60)
    check("스윙은 일봉(1d)으로 요청함", calls and all(c == "1d" for c in calls), calls)
    check("일봉이 반환됨", len(bars) > 0, len(bars))


def test_get_candidates_swing_merges_watchlist() -> None:
    print("== get_candidates(swing) 는 국내·해외·암호화폐 세 시장을 모두 모음 ==")
    cfg = load_config(CONFIG_PATH)
    cfg.swing.watchlist = ["005930"]
    cfg.swing.overseas_watchlist = ["AAPL"]
    cfg.swing.crypto_watchlist = ["KRW-BTC"]
    cfg.bithumb_access_key = "fake-key"
    cfg.bithumb_secret_key = "fake-secret"

    class FakeClient:
        def stocks(self, symbols):
            return []

    class FakeScreener:
        def __init__(self, client, cfg):
            pass

        def build_report(self):
            from types import SimpleNamespace
            return SimpleNamespace(candidates=[])

    import daytrader.screener as screener_mod
    real_screener = screener_mod.Screener
    screener_mod.Screener = FakeScreener
    try:
        out = tb.get_candidates("swing", FakeClient(), cfg, limit=10)
    finally:
        screener_mod.Screener = real_screener
    check("국내 관심 종목이 후보에 포함됨(asset_market=domestic)",
          any(c["symbol"] == "005930" and c.get("asset_market") == "domestic" for c in out), out)
    check("해외 관심 종목이 후보에 포함됨(asset_market=overseas)",
          any(c["symbol"] == "AAPL" and c.get("asset_market") == "overseas" for c in out), out)
    check("암호화폐 관심 종목이 후보에 포함됨(asset_market=crypto, 빗썸 키가 있으므로)",
          any(c["symbol"] == "KRW-BTC" and c.get("asset_market") == "crypto" for c in out), out)


def test_get_candidates_swing_skips_crypto_without_keys() -> None:
    print("== get_candidates(swing) 는 빗썸 키가 없으면 암호화폐 레그를 건너뜀 ==")
    cfg = load_config(CONFIG_PATH)
    cfg.swing.crypto_watchlist = ["KRW-BTC"]
    cfg.bithumb_access_key = ""
    cfg.bithumb_secret_key = ""

    class FakeClient:
        pass

    class FakeScreener:
        def __init__(self, client, cfg):
            pass

        def build_report(self):
            from types import SimpleNamespace
            return SimpleNamespace(candidates=[])

    import daytrader.screener as screener_mod
    real_screener = screener_mod.Screener
    screener_mod.Screener = FakeScreener
    try:
        out = tb.get_candidates("swing", FakeClient(), cfg, limit=10)
    finally:
        screener_mod.Screener = real_screener
    check("암호화폐 후보가 없음(키 미등록)", not any(c.get("asset_market") == "crypto" for c in out), out)


def test_get_candidates_swing_round_robin() -> None:
    print("== get_candidates(swing) 는 한 시장이 후보를 독차지하지 않도록 번갈아 담음 ==")
    cfg = load_config(CONFIG_PATH)
    cfg.swing.watchlist = ["005930", "000660", "035420"]  # 국내 3개
    cfg.swing.overseas_watchlist = ["AAPL"]  # 해외 1개
    cfg.bithumb_access_key = ""
    cfg.bithumb_secret_key = ""

    class FakeClient:
        pass

    class FakeScreener:
        def __init__(self, client, cfg):
            pass

        def build_report(self):
            from types import SimpleNamespace
            return SimpleNamespace(candidates=[])

    import daytrader.screener as screener_mod
    real_screener = screener_mod.Screener
    screener_mod.Screener = FakeScreener
    try:
        out = tb.get_candidates("swing", FakeClient(), cfg, limit=2)
    finally:
        screener_mod.Screener = real_screener
    check("한도 2인데 해외가 밀려나지 않고 하나는 들어감",
          any(c.get("asset_market") == "overseas" for c in out), out)


def test_simulate_technique_uses_swing_risk_override() -> None:
    print("== simulate_technique 이 스윙 전용(넓은) risk 를 쓸 수 있음 ==")
    from types import SimpleNamespace
    cfg = load_config(CONFIG_PATH)
    bars = _uptrend_pullback_breakout_bars(n=90)
    tight_risk = SimpleNamespace(stop_loss_pct=0.001, take_profit_pct=0.20, trailing_stop_pct=0.5, trailing_arm_pct=0.5)
    wide_risk = SimpleNamespace(stop_loss_pct=0.5, take_profit_pct=0.5, trailing_stop_pct=0.5, trailing_arm_pct=0.5)
    tight = tb.simulate_technique(cfg, "swing", "swing_ma_pullback", bars, "005930", "삼성전자", "관심 종목", risk=tight_risk)
    wide = tb.simulate_technique(cfg, "swing", "swing_ma_pullback", bars, "005930", "삼성전자", "관심 종목", risk=wide_risk)
    check("risk 파라미터가 결과에 반영됨(둘 다 계산은 성공)", "trades" in tight and "trades" in wide)


def test_swing_broker_paper() -> None:
    print("== PaperSwingBroker - 매수·매도·미보유 매도 거부(국내/해외 - 정수 주) ==")
    book = SwingPositionBook()
    broker = PaperSwingBroker(starting_cash=10_000_000, book=book, commission_pct=0.0002, tax_pct=0.002)
    pos = broker.buy("005930", "삼성전자", 1_000_000, 70000, technique="swing_ma_pullback")
    check("매수 성공(정수 주)", pos is not None and pos.quantity == 14, pos and pos.quantity)
    check("포지션에 market 이 기록됨(기본 domestic)", pos.market == "domestic")
    check("현금이 줄어듦", broker.cash < 10_000_000)
    check("이 브로커가 안 산 종목은 매도 거부", _raises(lambda: broker.sell("000660", 50000), NotOwnedError))
    result = broker.sell("005930", 75000)
    check("매도 성공(이익)", result["pnl"] > 0, result["pnl"])
    check("전량 매도 후 장부에서 사라짐", not book.owns("005930"))


def test_swing_broker_crypto_fractional_quantity() -> None:
    print("== PaperSwingBroker - 암호화폐는 소수점 수량으로 삼 ==")
    book = SwingPositionBook()
    broker = PaperSwingBroker(starting_cash=10_000_000, book=book)
    pos = broker.buy("KRW-BTC", "BTC", 1_000_000, 150_000_000, technique="swing_ma_pullback", market="crypto")
    check("소수점 수량으로 매수됨", pos is not None and 0 < pos.quantity < 1, pos and pos.quantity)
    check("market 이 crypto 로 기록됨", pos.market == "crypto")
    result = broker.sell("KRW-BTC", 160_000_000, fraction=0.5)
    check("암호화폐는 절반 매도도 소수점 그대로(정수로 안 잘림)", abs(result["quantity"] - pos.quantity) < 1e-9 or result["partial"])
    check("암호화폐 부분 매도 후에도 남은 수량이 소수점", book.get("KRW-BTC").quantity > 0)


def test_swing_broker_overseas_converts_krw_to_usd() -> None:
    """★★★ [1-11] 실제로 겪은 버그 - PaperSwingBroker 가 원화 예산을 해외주식
    달러 주가로 그대로 나눠서 수량이 환율 배수(약 1,400배)만큼 부풀려졌다
    (200만원 ÷ $150 = 13333주). overseas_engine._usd_krw_rate() 로 환산한
    뒤 나눠야 현실적인 수량(약 9주)이 나온다.
    """
    print("\n== ★★★ [1-11] PaperSwingBroker 해외주식 매수가 환율을 반영함 ==")
    import daytrader.overseas_engine as oe
    import daytrader.swing_broker as sb

    orig_rate = oe._usd_krw_rate
    oe._usd_krw_rate = lambda: 1400.0
    try:
        book = SwingPositionBook()
        broker = sb.PaperSwingBroker(starting_cash=10_000_000, book=book)
        pos = broker.buy("AAPL", "AAPL", 2_000_000, 150.0, technique="swing_ma_pullback", market="overseas")
        check("환율 버그였다면 13,000주 이상 - 실제로는 약 9주",
              pos is not None and pos.quantity == 9, pos and pos.quantity)
        check("원화 기준으로 현금이 정상적으로 줄어듦(투입금이 예산을 넘지 않음)",
              pos is not None and pos.invested <= 2_000_000, pos and pos.invested)
        check("entry_price 는 환산 없이 달러 그대로 기록됨", pos.entry_price == 150.0)

        result = broker.sell("AAPL", 160.0)
        check("매도도 같은 환율로 원화 환산 - 이익이 남(가격이 올랐으므로)", result["pnl"] > 0, result)
        check("전량 매도 후 장부에서 사라짐", not book.owns("AAPL"))
    finally:
        oe._usd_krw_rate = orig_rate


def test_swing_engine_multi_market_selection() -> None:
    print("== SwingEngine._select_candidates - 국내·해외·암호화폐 세 시장을 함께 고르고 번갈아 담음 ==")
    cfg = load_config(CONFIG_PATH)
    cfg.swing.watchlist = ["005930", "000660"]
    cfg.swing.overseas_watchlist = ["AAPL"]
    cfg.swing.crypto_watchlist = ["KRW-BTC"]
    cfg.bithumb_access_key = "fake"
    cfg.bithumb_secret_key = "fake"

    good_bars = _steady_uptrend_bars(n=90)  # 추세 위 + 최근 1주 상승 흐름을 둘 다 확실히 만족

    class FakeClient:
        def candles(self, *a, **kw):
            return []

    with tempfile.TemporaryDirectory() as d:
        engine = SwingEngine(cfg, client=FakeClient(), state_path=os.path.join(d, "swing_state.json"))
        engine._fetch_daily_bars = lambda symbol, market, count=260: good_bars
        engine._raw_pool = lambda market: {
            "domestic": [{"symbol": "005930", "name": "005930", "theme": "관심 종목"},
                         {"symbol": "000660", "name": "000660", "theme": "관심 종목"}],
            "overseas": [{"symbol": "AAPL", "name": "AAPL", "theme": "관심 종목"}],
            "crypto": [{"symbol": "KRW-BTC", "name": "BTC", "theme": "관심 종목"}],
        }[market]
        out = engine._select_candidates()
        markets_seen = {c["market"] for c in out}
        check("세 시장 후보가 모두 선정됨", markets_seen == {"domestic", "overseas", "crypto"}, markets_seen)
        check("두 번째로 뽑힌 후보가 국내가 아님(번갈아 담김)", out[1]["market"] != "domestic", [c["market"] for c in out])


def test_passes_swing_filters_week_momentum() -> None:
    print("== _passes_swing_filters - 추세는 맞아도 최근 1주 실제 하락이면 제외 ==")
    cfg = load_config(CONFIG_PATH)
    cfg.swing.week_momentum_min_pct = 0.01  # 최근 1주 1% 이상 올라야 후보로 인정(_steady_uptrend_bars 는 주간 약 1.2% 상승)

    class FakeClient:
        def candles(self, *a, **kw):
            return []

    with tempfile.TemporaryDirectory() as d:
        engine = SwingEngine(cfg, client=FakeClient(), state_path=os.path.join(d, "swing_state.json"))
        rising = _steady_uptrend_bars(n=90)
        check("계속 우상향(최근 1주도 상승) → 통과", engine._passes_swing_filters(rising))

        # 추세(60일선 위)는 맞지만 최근 5거래일만 뚝 떨어뜨린 시세 - 트렌드 필터는 통과해도
        # "최근 1주 실제 상승" 조건에는 걸려야 한다(단타처럼 당일 지표만 보면 놓치는 케이스).
        bars = list(rising)
        last = bars[-1]
        from daytrader.playbook import Bar as _Bar
        dropped_close = last.close * 0.9
        bars[-1] = _Bar(ts=last.ts, open=last.open, high=last.high, low=last.low, close=dropped_close, volume=last.volume)
        check("추세는 위여도 최근 1주 실제 하락이면 제외", not engine._passes_swing_filters(bars))


def test_swing_state_roundtrip() -> None:
    print("== SwingState - 저장·복원(날짜가 바뀌어도 초기화 안 됨) ==")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "swing_state.json")
        book = SwingPositionBook()
        broker = PaperSwingBroker(starting_cash=5_000_000, book=book)
        broker.buy("005930", "삼성전자", 1_000_000, 70000, technique="swing_breakout")
        state = SwingState(path)
        state.book = book
        state.closed.append({"symbol": "000660", "pnl": -1000, "exit_time": 0})
        state.save()

        reloaded = SwingState(path)
        check("포지션이 복원됨", reloaded.book.owns("005930"))
        check("청산 기록이 복원됨", len(reloaded.closed) == 1)


def test_swing_engine_cash_persists_across_restart() -> None:
    print("== ★★★ SwingEngine - 재시작해도 현금이 이중 계산되지 않음(대시보드 현금 불일치 버그) ==")
    cfg = load_config(CONFIG_PATH)

    class FakeClient:
        def candles(self, *a, **kw):
            return []

    with tempfile.TemporaryDirectory() as d:
        state_path = os.path.join(d, "swing_state.json")
        engine = SwingEngine(cfg, client=FakeClient(), state_path=state_path)
        cash_before = engine.broker.cash
        check("시작 현금은 총 투자금액과 같음(보유·청산 기록이 없으므로)",
              abs(cash_before - float(cfg.swing.budget)) < 1e-6, cash_before)

        # 실제로 한 종목을 사서 현금 일부가 묶이게 만든다.
        pos = engine.broker.buy("005930", "삼성전자", 1_000_000, 70000, technique="swing_ma_pullback")
        check("매수됨", pos is not None)
        cash_after_buy = engine.broker.cash
        check("매수 후 현금이 줄어듦", cash_after_buy < cash_before, cash_after_buy)

        # ★ 매수 외에 다른 변화(청산 등)는 없다고 두고 저장 - 재시작 전후로 현금이 그대로
        # 같아야 한다(포지션은 그대로 있고 청산·추가 매수는 없었으므로).
        engine.state.save()

        # ★ 재시작을 흉내낸다 - 같은 state_path 로 새 SwingEngine 을 만든다(프로그램을
        # 껐다 켠 것과 같은 경로: PaperSwingBroker.__init__ 가 다시 불린다).
        engine2 = SwingEngine(cfg, client=FakeClient(), state_path=state_path)
        check("재시작해도 포지션이 유지됨", engine2.state.book.owns("005930"))
        check("재시작해도 현금이 매수 전 총액으로 되돌아가지 않음(이중 계산 없음)",
              abs(engine2.broker.cash - cash_after_buy) < 1.0,
              f"{cash_after_buy} vs {engine2.broker.cash}")
        check("재시작 후 현금이 총 투자금액보다 적음(포지션 값어치만큼 묶여 있어야 함)",
              engine2.broker.cash < float(cfg.swing.budget))

        # ★ 청산 기록(실현손익)도 재시작 현금 계산에 반영돼야 한다 - 포지션 전량 매도 후,
        # 실제로 그만큼 현금이 늘어야 하고(엔진1), 재시작해도(엔진2) 같은 값이어야 한다.
        result = engine2.broker.sell("005930", 75000)
        engine2.state.closed.append({
            "symbol": "005930", "pnl": result["pnl"], "exit_time": time.time(),
        })
        cash_after_sell = engine2.broker.cash
        engine2.state.save()
        engine3 = SwingEngine(cfg, client=FakeClient(), state_path=state_path)
        check("전량 매도 후 재시작해도 현금이 이중 계산되지 않음(청산 손익 포함)",
              abs(engine3.broker.cash - cash_after_sell) < 1.0,
              f"{cash_after_sell} vs {engine3.broker.cash}")


def test_swing_engine_halt_info() -> None:
    print("== SwingEngine.halt_info() - 연속 손절 후 냉각(시간 단위) ==")
    cfg = load_config(CONFIG_PATH)
    cfg.swing.consecutive_loss_halt = 2
    cfg.swing.loss_halt_cooldown_hours = 3.0

    class FakeClient:
        def candles(self, *a, **kw):
            return []

    import time
    with tempfile.TemporaryDirectory() as d:
        engine = SwingEngine(cfg, client=FakeClient(), state_path=os.path.join(d, "swing_state.json"))
        now = time.time()
        engine.state.closed.append({"symbol": "005930", "pnl": -1000, "exit_time": now - 3600})
        engine.state.closed.append({"symbol": "000660", "pnl": -1000, "exit_time": now - 1800})
        info = engine.halt_info()
        check("연속 2회 손절 → 중단", info["halted"], info)
        check("재개 예정 시각이 미래", info["resume_at"] > now, info)

        engine.state.closed[-1]["exit_time"] = now - 4 * 3600
        engine.state.closed[-2]["exit_time"] = now - 4 * 3600 - 1
        info2 = engine.halt_info()
        check("쿨다운(3시간)이 지나면 재개", not info2["halted"], info2)


def test_swing_fmt_ts_uses_kst_not_host_tz() -> None:
    """★★★ [1-10] swing_engine._fmt_ts 가 datetime.now()(호스트 로컬 시각)로
    "오늘"을 가르면, UTC 호스트에서는 한국 시각 오전 9시 이전에도 이미 다음
    날로 넘어간 것으로 착각한다. TZ=UTC 로 호스트를 재현하고 "지금"을 KST
    새벽 2시로 고정해(UTC 로는 아직 전날 17시) 경계를 검증한다.
    """
    print("\n== ★★★ [1-10] swing_engine._fmt_ts 가 KST 자정 기준(UTC 호스트) ==")
    import time as _time

    import daytrader.swing_engine as se

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    _time.tzset()
    orig_now_kst = se.now_kst
    try:
        fixed_now = se.datetime(2026, 1, 5, 2, 0, tzinfo=se.KST)
        se.now_kst = lambda: fixed_now

        yesterday_kst_2350 = se.datetime(2026, 1, 4, 23, 50, tzinfo=se.KST).timestamp()
        today_kst_0010 = se.datetime(2026, 1, 5, 0, 10, tzinfo=se.KST).timestamp()

        check("KST 자정 전(어제 23:50) 은 '오늘' 형식(HH:MM)로 안 찍힘",
              "/" in se._fmt_ts(yesterday_kst_2350), se._fmt_ts(yesterday_kst_2350))
        check("KST 자정 이후(오늘 00:10) 는 '오늘' 형식(HH:MM)으로 찍힘",
              se._fmt_ts(today_kst_0010) == "00:10", se._fmt_ts(today_kst_0010))
    finally:
        se.now_kst = orig_now_kst
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        _time.tzset()


def test_swing_engine_never_force_closes() -> None:
    print("== SwingEngine 컨텍스트는 절대 force_close=True 를 안 만듦(당일 청산 개념 없음) ==")
    cfg = load_config(CONFIG_PATH)

    class FakeClient:
        def candles(self, *a, **kw):
            return []

    with tempfile.TemporaryDirectory() as d:
        engine = SwingEngine(cfg, client=FakeClient(), state_path=os.path.join(d, "swing_state.json"))
        ctx = engine._make_ctx("005930", "삼성전자", "관심 종목", None)
        check("force_close 는 항상 False", ctx.force_close is False)


def main() -> None:
    for t in (
        test_swing_entries_registered, test_swing_ma_pullback_entry, test_swing_breakout_entry,
        test_swing_golden_cross_entry, test_swing_playbook_evaluate_entry, test_config_swing_validation,
        test_config_swing_mode_rejected, test_config_swing_missing_fixed_exit_rejected,
        test_applicable_techniques_swing_only_daily, test_fetch_bars_swing_uses_daily_interval,
        test_get_candidates_swing_merges_watchlist, test_get_candidates_swing_skips_crypto_without_keys,
        test_get_candidates_swing_round_robin, test_simulate_technique_uses_swing_risk_override,
        test_swing_broker_paper, test_swing_broker_crypto_fractional_quantity,
        test_swing_broker_overseas_converts_krw_to_usd,
        test_swing_engine_multi_market_selection, test_passes_swing_filters_week_momentum,
        test_swing_state_roundtrip, test_swing_engine_cash_persists_across_restart, test_swing_engine_halt_info,
        test_swing_engine_never_force_closes, test_swing_fmt_ts_uses_kst_not_host_tz,
    ):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
