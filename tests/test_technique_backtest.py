"""종목별·테마별 기법 백테스트(technique_backtest.py)·선호도 저장(technique_prefs.py)·
실전 채점 반영(playbook.py 의 _preference_multiplier) 테스트. `python tests/test_technique_backtest.py`로 실행한다.
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

from daytrader import technique_backtest as tb  # noqa: E402
from daytrader import technique_prefs as tp  # noqa: E402
from daytrader.config import load_config  # noqa: E402
from daytrader.playbook import Bar, Playbook  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config.yaml")

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


def _bar(i, o, h, l, c, v, date="2026-09-10"):
    hh = 9 + i // 60
    mm = i % 60
    return Bar(ts=f"{date}T{hh:02d}:{mm:02d}:00+09:00", open=o, high=h, low=l, close=c, volume=v)


def _flat_then_breakout_bars(n_flat=60, take_hit=True):
    """평평하게 다지다가 마지막에 거래량을 동반해 크게 뚫는 봉을 만든다(breakout 기법이 반응하도록).
    take_hit=True 면 이후 봉들이 계속 올라 익절까지 닿게 하고, False 면 계속 내려 손절에 닿게 한다."""
    bars = [_bar(i, 10000, 10050, 9950, 10000, 500) for i in range(n_flat)]
    bars.append(_bar(n_flat, 10000, 10300, 9990, 10280, 5000))  # 돌파 봉(거래량 급증·양봉)
    step = 60 if take_hit else -60
    price = 10280
    for i in range(n_flat + 1, n_flat + 40):
        price += step
        bars.append(_bar(i, price - step, price + 20, price - 20, price, 400))
    return bars


def test_pnl_and_aggregate() -> None:
    print("== pnl%·집계 ==")
    p = tb._pnl_pct(10000, 10500, 0.001, 0.002)
    check("이익 거래는 양수", p > 0, str(p))
    p2 = tb._pnl_pct(10000, 9500, 0.001, 0.002)
    check("손실 거래는 음수", p2 < 0, str(p2))
    check("수수료·세금 반영(익절해도 비용만큼 덜 남음)",
          tb._pnl_pct(10000, 10000, 0.001, 0.002) < 0, "본전 가격에 팔면 비용만큼 손실")

    empty = tb._aggregate("breakout", "돌파 추종", [])
    check("거래 없으면 0건·0 손익", empty["trades"] == 0 and empty["total_pnl_pct"] == 0.0)

    trades = [
        {"entry_ts": "a", "exit_ts": "b", "pnl_pct": 0.02, "reason": "익절"},
        {"entry_ts": "c", "exit_ts": "d", "pnl_pct": -0.01, "reason": "손절"},
        {"entry_ts": "e", "exit_ts": "f", "pnl_pct": 0.03, "reason": "익절"},
    ]
    agg = tb._aggregate("breakout", "돌파 추종", trades)
    check("건수·승패 집계", agg["trades"] == 3 and agg["wins"] == 2 and agg["losses"] == 1)
    check("승률 2/3", abs(agg["win_rate"] - 2 / 3) < 1e-9)
    check("총 손익 = 0.04", abs(agg["total_pnl_pct"] - 0.04) < 1e-9)
    check("손익비 = 이익합/손실합 = 0.05/0.01 = 5", abs(agg["profit_factor"] - 5.0) < 1e-9)

    all_win = tb._aggregate("breakout", "돌파 추종", [{"entry_ts": "a", "exit_ts": "b", "pnl_pct": 0.01, "reason": "익절"}])
    check("손실이 하나도 없으면 손익비는 무한대(inf)", all_win["profit_factor"] == float("inf"))


def test_fetch_router_bars_avoids_over_200_limit() -> None:
    print("== ★ 실제로 겪은 문제: 200개 넘게 요청하면 조용히 0개가 오는 API - 200개씩 나눠 받아야 함 ==")

    class FakeRouterNoBefore:
        """QuoteRouter 흉내 - candles() 에 before 인자가 아예 없고, count>200 이면 빈 배열을 준다."""
        def candles(self, symbol, timeframe, count):
            if count > 200:
                return []
            return [{"timestamp": f"2026-09-2{2 + i // 300}T{(i % 300) // 60:02d}:{i % 60:02d}:00+09:00",
                     "openPrice": 100, "highPrice": 101, "lowPrice": 99, "closePrice": 100, "volume": 10}
                    for i in range(count)]

    bars = tb.fetch_bars("overseas", FakeRouterNoBefore(), "AAPL", days=7)
    check("before 를 아예 못 받는 클라이언트는 첫 페이지(<=200개)만 받고 멈춤(0개로 실패하지 않음)",
          0 < len(bars) <= 200, len(bars))

    class FakeToss:
        """실제 TossClient 흉내 - before 를 받아 진짜로 과거 페이지를 준다."""
        def __init__(self):
            self.calls = []

        def candles(self, symbol, interval, count, before=None):
            self.calls.append((count, before))
            if count > 200:
                return []
            # before 가 있으면 그보다 더 과거 페이지, 없으면 최신 페이지.
            base = 0 if before is None else -200
            return [{"timestamp": f"2026-09-2{2 + (base + i) // 300}T{((base + i) % 300) // 60:02d}:{(base + i) % 60:02d}:00+09:00",
                     "openPrice": 100, "highPrice": 101, "lowPrice": 99, "closePrice": 100, "volume": 10}
                    for i in range(count)]

    class FakeRouterWithPrimary:
        def __init__(self):
            self.primary = FakeToss()

        def candles(self, *a, **kw):
            raise AssertionError("래퍼(candles)가 아니라 primary 를 직접 불러야 한다")

    router = FakeRouterWithPrimary()
    bars2 = tb.fetch_bars("overseas", router, "AAPL", days=7)
    check("실제 TossClient(.primary)가 있으면 그걸 직접 불러 200개씩 이어 받음", len(bars2) > 200, len(bars2))
    check("모든 요청이 200개 이하로만 나감(200 넘게 요청해 0개 받는 실수를 반복하지 않음)",
          all(c <= 200 for c, _ in router.primary.calls), router.primary.calls)


def test_overseas_candidates_fall_back_to_watchlist() -> None:
    print("== ★ 해외 테마 후보가 텅 비어도(조용한 시간) 관심 종목은 항상 후보에 들어감 ==")
    from unittest.mock import patch
    cfg = load_config(CONFIG_PATH)
    cfg.overseas.watchlist = ["AAPL", "MSFT"]

    with patch("daytrader.us_themes.scan_us_themes", return_value={"candidates": []}):
        rows = tb.get_candidates("overseas", object(), cfg)
        check("테마 후보가 없어도 관심 종목이 후보로 들어감", {r["symbol"] for r in rows} == {"AAPL", "MSFT"}, rows)
        check("관심 종목의 테마 표시는 '관심 종목'", all(r["theme"] == "관심 종목" for r in rows))

    with patch("daytrader.us_themes.scan_us_themes",
               return_value={"candidates": [{"symbol": "NVDA", "name": "엔비디아", "theme": "AI반도체"}]}):
        rows2 = tb.get_candidates("overseas", object(), cfg)
        symbols = {r["symbol"] for r in rows2}
        check("테마 후보와 관심 종목이 중복 없이 합쳐짐", symbols == {"NVDA", "AAPL", "MSFT"}, rows2)
        check("이미 테마로 뽑힌 종목은 관심 종목으로 다시 안 붙음(테마 이름 유지)",
              next(r for r in rows2 if r["symbol"] == "NVDA")["theme"] == "AI반도체")


def test_applicable_techniques() -> None:
    print("== 시장별로 의미 있는 기법 목록 ==")
    dom = tb.applicable_techniques("domestic")
    ov = tb.applicable_techniques("overseas")
    cr = tb.applicable_techniques("crypto")
    check("국내는 시초 갭·장 막판 기법 포함", "open_gap" in dom and "close_squeeze" in dom)
    check("해외는 국내 전용 기법 제외", "open_gap" not in ov and "close_squeeze" not in ov)
    check("암호화폐도 국내 전용 기법 제외", "open_gap" not in cr and "close_squeeze" not in cr)
    check("알 수 없는 시장이면 예외", _raises(lambda: tb.get_candidates("mars", None, None)))


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except ValueError:
        return True


def test_simulate_technique_profitable_and_losing() -> None:
    print("== 기법 하나를 실제 봉처럼 생긴 데이터에 재생 ==")
    cfg = load_config(CONFIG_PATH)
    win_bars = _flat_then_breakout_bars(take_hit=True)
    result = tb.simulate_technique(cfg, "domestic", "breakout", win_bars, "005930", "삼성전자", "반도체")
    check("돌파 후 계속 오르면 거래가 생기고 이익", result["trades"] >= 1 and result["total_pnl_pct"] > 0, str(result))
    check("이긴 거래가 있음", result["wins"] >= 1)

    lose_bars = _flat_then_breakout_bars(take_hit=False)
    result2 = tb.simulate_technique(cfg, "domestic", "breakout", lose_bars, "005930", "삼성전자", "반도체")
    check("돌파 후 바로 꺾이면 거래가 생기고 손실", result2["trades"] >= 1 and result2["total_pnl_pct"] < 0, str(result2))

    flat = [_bar(i, 10000, 10010, 9990, 10000, 500) for i in range(120)]
    result3 = tb.simulate_technique(cfg, "domestic", "breakout", flat, "005930", "삼성전자", "반도체")
    check("아무 신호도 없으면 거래 0건", result3["trades"] == 0)

    too_short = flat[:10]
    result4 = tb.simulate_technique(cfg, "domestic", "breakout", too_short, "005930", "삼성전자", "반도체")
    check("봉이 워밍업보다 적으면 즉시 빈 결과(예외 없음)", result4["trades"] == 0)


def test_json_safe_strips_inf() -> None:
    print("== ★ 손익비 무한대(손실 없음)가 API 응답에서 JSON 오류를 내지 않음 ==")
    import json
    raw = {"profit_factor": float("inf"), "nested": {"x": float("-inf"), "y": [1, float("inf"), "s", float("nan")]}}
    safe = tb._json_safe(raw)
    check("inf 는 None 으로", safe["profit_factor"] is None)
    check("중첩된 dict·list 안의 inf·-inf·nan 도 모두 None 으로", safe["nested"]["x"] is None and safe["nested"]["y"][1] is None and safe["nested"]["y"][3] is None)
    check("일반 값은 그대로", safe["nested"]["y"][0] == 1 and safe["nested"]["y"][2] == "s")
    check("실제로 json.dumps 가 예외 없이 됨(Starlette JSONResponse 가 이 실패로 500 을 냈었음)",
          bool(json.dumps(safe)))


def test_merge_with_watchlist() -> None:
    print("== 관심 종목은 테마와 별개로 항상 후보에 합쳐짐 ==")
    themed = [{"symbol": "NVDA", "name": "엔비디아", "theme": "AI반도체"}]
    out = tb.merge_with_watchlist(themed, ["NVDA", "AAPL"])
    check("이미 테마로 뽑힌 종목은 중복 없이 테마 이름 유지",
          next(r for r in out if r["symbol"] == "NVDA")["theme"] == "AI반도체")
    check("관심 종목이 추가됨", any(r["symbol"] == "AAPL" and r["theme"] == "관심 종목" for r in out))
    check("빈 테마 후보 + 관심 종목만 있어도 동작", len(tb.merge_with_watchlist([], ["MSFT"])) == 1)
    check("워치리스트가 없으면 원본 그대로", tb.merge_with_watchlist(themed, None) == themed)


def test_prefs_history_and_throttle() -> None:
    print("== 실행 기록 저장·조회, 같은 종목 조합은 하루에 한 번만(자동 실행 중복 방지) ==")
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = tempfile.mkdtemp()

    check("기록이 없으면 빈 목록", tp.history(cfg) == [])
    sig = tp.candidate_signature([{"symbol": "005930"}, {"symbol": "000660"}])
    check("아직 안 돌렸으면 False", not tp.already_ran_today(cfg, "domestic", sig))
    check("빈 조합은 항상 False(돌 것도 없음)", not tp.already_ran_today(cfg, "domestic", ""))

    from daytrader.timeutil import now_kst  # noqa: E402
    result = {
        "at": now_kst().isoformat(), "days": 7, "candidates": 2,
        "by_symbol": [
            {"symbol": "005930", "name": "삼성전자", "theme": "반도체", "best_technique": "breakout",
             "results": [{"technique": "breakout", "total_pnl_pct": 0.03, "win_rate": 0.6, "trades": 5}]},
            {"symbol": "000660", "name": "SK하이닉스", "theme": "반도체", "best_technique": None, "results": []},
        ],
        "by_theme": [{"theme": "반도체", "best_technique": "breakout"}], "errors": [],
    }
    tp.save_from_backtest(cfg, "domestic", result, trigger="auto")
    check("저장 후에는 같은 조합이면 True(오늘은 다시 안 돎)", tp.already_ran_today(cfg, "domestic", sig))
    check("종목별 저장에 손익·승률·거래수까지 남음",
          tp.summary(cfg)["domestic"]["by_symbol"]["005930"]["total_pnl_pct"] == 0.03)

    h = tp.history(cfg)
    check("기록이 한 건 남음", len(h) == 1 and h[0]["trigger"] == "auto" and h[0]["market"] == "domestic")
    check("기록에 종목별 결과 요약이 남음", h[0]["by_symbol"][0]["best_technique"] == "breakout")

    # 다른 종목 조합이면 다시 돌 수 있어야 한다.
    other_sig = tp.candidate_signature([{"symbol": "999999"}])
    check("다른 종목 조합은 오늘 처음이라 False", not tp.already_ran_today(cfg, "domestic", other_sig))
    # 다른 시장은 서로 안 섞인다.
    check("다른 시장은 기록이 안 섞임", not tp.already_ran_today(cfg, "crypto", sig))


def test_auto_backtest_trigger_and_throttle() -> None:
    print("== ★ 종목 재선정 시 자동 백테스트 트리거 - 백그라운드로 돌고, 같은 날 중복 실행 안 함 ==")
    import time
    from unittest.mock import patch
    from daytrader import auto_backtest

    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = tempfile.mkdtemp()
    candidates = [{"symbol": "005930", "name": "삼성전자", "theme": "반도체"}]

    check("빈 후보면 아무 것도 안 함", not auto_backtest.maybe_trigger(cfg, object(), "domestic", []))

    calls = []

    def fake_run(cfg_, client_, market_, **kw):
        calls.append((market_, kw.get("trigger")))
        return {"market": market_, "by_symbol": []}

    with patch("daytrader.technique_backtest.run", side_effect=fake_run):
        started = auto_backtest.maybe_trigger(cfg, object(), "domestic", candidates)
        check("정상 후보면 트리거를 건다(백그라운드 시작)", started)
        for _ in range(50):  # 백그라운드 스레드가 끝날 때까지 잠깐 기다린다(최대 0.5초)
            if calls:
                break
            time.sleep(0.01)
        check("백그라운드에서 technique_backtest.run 이 trigger=auto 로 호출됨",
              calls and calls[0] == ("domestic", "auto"), calls)

    # 실제로는 저 백그라운드 run() 호출이 technique_prefs.save_from_backtest() 까지 마쳐야
    # already_ran_today 가 True 로 바뀐다(여기서는 run 을 가짜로 바꿔 두었으니 그 저장 단계를
    # 직접 재현해 "오늘 이미 이 조합을 기록해 뒀다면" 다음 트리거가 실제로 걸러지는지 확인한다.
    from daytrader import technique_prefs
    from daytrader.timeutil import day_str, now_kst
    technique_prefs.save_from_backtest(cfg, "domestic", {
        "at": day_str(now_kst()) + "T09:00:00+09:00", "days": 7, "candidates": 1,
        "by_symbol": [{"symbol": "005930", "name": "삼성전자", "theme": "반도체", "best_technique": "breakout",
                       "results": [{"technique": "breakout", "total_pnl_pct": 0.01, "win_rate": 1.0, "trades": 1}]}],
        "by_theme": [], "errors": [],
    }, trigger="auto")
    with patch("daytrader.technique_backtest.run", side_effect=fake_run) as m:
        again2 = auto_backtest.maybe_trigger(cfg, object(), "domestic", candidates)
        check("오늘 이미 이 조합으로 기록이 있으면 트리거 안 걸림", not again2 and not m.called)


def test_technique_prefs_roundtrip() -> None:
    print("== 종목·테마 선호 기법 저장·조회 ==")
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = tempfile.mkdtemp()

    check("아직 아무 것도 없으면 None", tp.best_for(cfg, "domestic", "005930", "반도체") is None)

    fake_result = {
        "at": "now", "days": 7,
        "by_symbol": [
            {"symbol": "005930", "name": "삼성전자", "theme": "반도체", "best_technique": "breakout"},
            {"symbol": "000660", "name": "SK하이닉스", "theme": "반도체", "best_technique": "ma_pullback"},
        ],
        "by_theme": [{"theme": "반도체", "best_technique": "breakout"}],
    }
    tp.save_from_backtest(cfg, "domestic", fake_result)
    check("종목 우선 - 저장한 종목은 그 기법", tp.best_for(cfg, "domestic", "005930", "반도체") == "breakout")
    check("다른 종목은 그 종목대로", tp.best_for(cfg, "domestic", "000660", "반도체") == "ma_pullback")
    check("모르는 종목은 테마 값으로 대신함", tp.best_for(cfg, "domestic", "999999", "반도체") == "breakout")
    check("시장이 다르면 안 섞임", tp.best_for(cfg, "crypto", "005930", "반도체") is None)

    s = tp.summary(cfg)
    check("summary 에 시장 키가 남음", "domestic" in s)

    tp.clear(cfg, "domestic")
    check("clear 후에는 다시 None", tp.best_for(cfg, "domestic", "005930", "반도체") is None)


def test_playbook_preference_boosts_matching_technique() -> None:
    print("== ★ 백테스트에서 이긴 기법이 실전 채점에서 가산점을 받아 이김 ==")
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = tempfile.mkdtemp()
    cfg.strategy.best_signal = True

    # ma_pullback 과 breakout 이 둘 다 통과하지만 breakout 점수가 살짝 더 높게 만든 뒤,
    # 선호도가 없으면 breakout 이 이기고, ma_pullback 을 선호로 저장하면 뒤집히는지 본다.
    from unittest.mock import patch
    from daytrader.playbook import Verdict

    def fake_evaluate(self, bars, ctx):
        # ★ 점수를 가깝게 둔다(0.75 vs 0.70) - 가산점(1.15배)이 0.70을 0.805 로 밀어 올려야
        # 역전이 일어난다. 너무 벌어져 있으면(예: 0.9 vs 0.7) 가산점으로도 못 뒤집혀 이 테스트가
        # "가산점이 전혀 안 먹힌 것"과 "가산점이 부족했던 것"을 구분하지 못한다.
        score = 0.75 if self.key == "breakout" else 0.70
        return Verdict(
            id="x", at="t", symbol=ctx.symbol, name=ctx.name, theme=ctx.theme, phase="entry",
            technique=self.key, technique_label=self.key, ok=True, score=score, terms=[],
            blocked_by=[], headline="", narrative="", changes=[], inputs={}, price=100.0,
        )

    cfg.strategy.entry_order = ["breakout", "ma_pullback"]
    pb = Playbook(cfg, market="domestic")
    from types import SimpleNamespace
    ctx = SimpleNamespace(symbol="005930", name="삼성전자", theme="반도체", window="main", prev_verdict=None)

    with patch("daytrader.playbook.BreakoutEntry.evaluate", fake_evaluate), \
         patch("daytrader.playbook.MaPullbackEntry.evaluate", fake_evaluate):
        winner, _ = pb.evaluate_entry([], ctx)
        check("선호도가 없으면 점수가 더 높은 breakout 이 이김", winner.technique == "breakout", winner.technique)

        tp.save_from_backtest(cfg, "domestic", {
            "at": "now", "days": 7,
            "by_symbol": [{"symbol": "005930", "name": "삼성전자", "theme": "반도체", "best_technique": "ma_pullback"}],
            "by_theme": [],
        })
        winner2, _ = pb.evaluate_entry([], ctx)
        check("★ 이 종목은 ma_pullback 이 더 잘 맞았다는 선호가 있으면 점수가 낮아도 역전해서 이김",
              winner2.technique == "ma_pullback", winner2.technique)
        check("선택 이유에 백테스트 가산점이 남음", "백테스트" in (winner2.selection_reason or ""), winner2.selection_reason)


def main() -> None:
    for t in (test_pnl_and_aggregate, test_fetch_router_bars_avoids_over_200_limit,
              test_overseas_candidates_fall_back_to_watchlist, test_merge_with_watchlist,
              test_applicable_techniques, test_simulate_technique_profitable_and_losing,
              test_json_safe_strips_inf, test_prefs_history_and_throttle, test_auto_backtest_trigger_and_throttle,
              test_technique_prefs_roundtrip, test_playbook_preference_boosts_matching_technique):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
