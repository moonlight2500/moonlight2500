"""research/ 하네스가 합성 시세로 끝까지(fetch -> backtest -> metrics -> grid) 도는지,
다음 봉 체결·"진행 중인 봉엔 진입 안 함" 규칙을 지키는지 빠르게(<60초) 검증한다.
네트워크 없이 돈다. `python tests/test_research_harness.py` 로 실행한다.
"""

from __future__ import annotations

import io
import os
import sys
import time

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CONFIG_PATH = os.path.join(ROOT, "config.yaml")

from daytrader.config import load_config  # noqa: E402
from research import data as data_mod  # noqa: E402
from research import grid as grid_mod  # noqa: E402
from research import metrics as metrics_mod  # noqa: E402
from research.backtest import run_backtest  # noqa: E402

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


def test_synthetic_generation():
    cfg = load_config(CONFIG_PATH)
    symbols = ["005930", "000660", "373220"]  # 서로 다른 테마 2개 + 대장주
    bars = data_mod.generate_synthetic(cfg, symbols, days=6)
    check("합성 시세 - 요청한 종목이 전부 있음", set(bars.keys()) == set(symbols))
    for sym in symbols:
        check(f"합성 시세 - {sym} 봉이 있음", len(bars[sym]) > 0, f"{len(bars[sym])}개")
        ts_sorted = [b.ts for b in bars[sym]]
        check(f"합성 시세 - {sym} 시간순 정렬됨", ts_sorted == sorted(ts_sorted))
        days = {t[:10] for t in ts_sorted}
        check(f"합성 시세 - {sym} 6영업일치", len(days) == 6, f"{len(days)}일")
    return bars, cfg


def test_backtest_end_to_end(bars, cfg):
    names = data_mod.default_universe(cfg)
    themes = data_mod.symbol_theme_map(cfg)
    result = run_backtest(cfg, bars, symbol_names=names, symbol_themes=themes)
    check("백테스트 - 결과 객체가 돌아옴", result is not None)
    check("백테스트 - 거래일이 채워짐", len(result.days) == 6, f"{len(result.days)}일")
    # ★ 합성 시세 6일 정도로는 거래가 아예 안 날 수도 있다(신호 조건이 꽤 까다롭다) -
    #   여기서는 "돌아간다"만 확인하고, 실제 거래 발생 검증은 조건을 낮춘 별도 테스트에서 한다.
    check("백테스트 - 예외 없이 완주", True)
    return result


def test_next_bar_fill_and_no_forming_bar_entry():
    """합성 시세만으론 신호가 안 날 수도 있으니, 반드시 신호가 나도록 손으로 만든 봉으로
    "신호가 난 봉의 종가가 아니라 다음 봉 시가(±슬리피지)에서 체결됐는지",
    "그 날 마지막 봉에서 신호가 나면 체결 없이 버려지는지"를 직접 확인한다."""
    from daytrader.playbook import Bar
    from research.backtest import _slip_price

    cfg = load_config(CONFIG_PATH)
    cfg.strategy.entry_order = ["breakout"]
    cfg.strategy.exit_enabled = ["fixed"]
    cfg.entry.use_closed_bars_only = True
    cfg.entry.volume_surge_ratio = 1.1
    cfg.entry.breakout_lookback = 5
    cfg.entry.volume_window = 5
    cfg.capital.max_positions = 5
    cfg.risk.daily_max_trades = 50
    cfg.risk.cooldown_minutes = 0

    sym = "TESTSYM"

    def bar(hh, mm, o, h, l, c, v):
        return Bar(ts=f"2026-09-10T{hh:02d}:{mm:02d}:00+09:00", open=o, high=h, low=l, close=c, volume=v)

    bars = []
    # 워밍업 - 평평하게 다진다(scan_start=09:20 이후부터 신호를 본다).
    for m in range(0, 40):
        bars.append(bar(9, m, 10000, 10020, 9980, 10000, 100))
    # 돌파 신호 봉(09:40) - 직전 5봉 고가를 거래량 급증과 함께 넘는 양봉.
    signal_bar = bar(9, 40, 10000, 10300, 9990, 10250, 1000)
    bars.append(signal_bar)
    next_open = 10260.0
    next_bar = bar(9, 41, next_open, 10400, 10200, 10300, 500)
    bars.append(next_bar)
    for m in range(42, 46):
        bars.append(bar(9, m, 10300, 10320, 10280, 10300, 100))

    names = {sym: sym}
    themes = {sym: "테스트"}
    # ★ [7-2] 스크리너 게이팅(기본 켜짐)은 "테마 동반상승 2종목 이상"을 요구한다 - 이 테스트는
    #   체결 타이밍만 보는 것이라 종목 하나뿐인 가짜 테마로는 게이팅을 절대 못 통과한다.
    #   그래서 여기서는 명시적으로 끈다(게이팅 자체는 test_screener_gate_* 에서 따로 검증한다).
    result = run_backtest(
        cfg, {sym: bars}, symbol_names=names, symbol_themes=themes, min_warmup=30, screener_gate=False,
    )

    entries = [t for t in result.trades if t.symbol == sym]
    check("직접 만든 돌파 신호 - 거래가 최소 1건 발생함", len(entries) >= 1, f"{len(entries)}건")
    if entries:
        t = entries[0]
        expected_fill = _slip_price(next_open, "BUY", cfg.risk.max_slippage_pct / 2.0)
        check(
            "체결가 = 신호 다음 봉 시가(±슬리피지), 신호 봉 종가가 아님",
            abs(t.entry_price - expected_fill) < 1.0 and abs(t.entry_price - signal_bar.close) > 1.0,
            f"체결가={t.entry_price} 예상={expected_fill} 신호봉종가={signal_bar.close}",
        )
        check("체결 시각이 신호 봉이 아니라 다음 봉(09:41)", t.entry_time[11:16] == "09:41", t.entry_time)

    # ★ 그 날 마지막 봉에서 신호가 나면(다음 봉이 없음) 체결 없이 버려져야 한다.
    bars_last = list(bars[:40])
    last_bar = bar(9, 40, 10000, 10300, 9990, 10250, 1000)
    bars_last.append(last_bar)
    sym2 = "TESTSYM2"
    result2 = run_backtest(
        cfg, {sym2: bars_last}, symbol_names={sym2: sym2}, symbol_themes={sym2: "테스트"},
        min_warmup=30, screener_gate=False,
    )
    check(
        "그 날 마지막 봉에서 난 신호는 체결되지 않음(다음 봉 없음)",
        len(result2.trades) == 0, f"{len(result2.trades)}건 체결됨(있으면 안 됨)",
    )


def test_screener_gate_candidate_selection():
    """[7-2] _score_themes_approx/_pick_candidates_approx 가 daytrader/screener.py 의
    규칙(동반상승 문턱·중앙값 강도·거래대금순 랭킹·top_themes×candidates_per_theme·가격대·
    거래대금 하한)을 그대로 근사하는지 직접 확인한다(캐시된 분봉만으로는 실제 스크리너를
    돌릴 수 없으니, 이 단위 테스트가 "감사 항목 4" 회귀 검증 역할을 한다)."""
    from research.backtest import _pick_candidates_approx, _score_themes_approx

    cfg = load_config(CONFIG_PATH)
    s = cfg.screen  # 기준값(config.yaml): min_change_rate=3%, max=12%, min_trading_amount=50억,
    # price 1000~200000, top_themes=2, candidates_per_theme=2, min_theme_members_up=2

    symbol_themes = {
        "A1": "테마A", "A2": "테마A", "A3": "테마A",  # 3종목 동반상승(문턱 2 이상 통과)
        "B1": "테마B",                                  # 혼자만 상승 - 동반상승 아님
        "C1": "테마C", "C2": "테마C",                    # 동반상승은 했지만 거래대금 미달
        "D1": "테마D", "D2": "테마D",                    # 동반상승 + 거래대금 충분하지만 등락률이 상한 초과
    }
    change_rate = {
        "A1": 0.05, "A2": 0.045, "A3": 0.035,
        "B1": 0.06,
        "C1": 0.04, "C2": 0.035,
        "D1": 0.20, "D2": 0.18,  # max_change_rate(12%) 초과 - 추격매수 구간이라 제외돼야 함
    }
    big_amount = s.min_trading_amount * 2
    small_amount = s.min_trading_amount * 0.1
    tiny_amount = 1_000_000  # 유동성항(log10) 최저치(0.1)로 클램프되게 일부러 아주 작게 잡는다.
    trading_amount = {
        "A1": big_amount * 3, "A2": big_amount * 2, "A3": big_amount,  # A1 이 거래대금 1위(대장주)
        "B1": big_amount,
        "C1": small_amount, "C2": small_amount,
        # D 는 등락률이 극단적으로 커서(20%) 거래대금이 충분하면 테마 점수(=breadth×중앙값×유동성)가
        # A 를 앞질러 "테마 순위 1위" 검증이 흔들린다 - 이 케이스가 보려는 건 오직 "등락률 상한 초과
        # 종목은 후보에서 빠진다"이므로 거래대금은 일부러 아주 작게 둬 테마 점수 경쟁에서 빠지게 한다.
        "D1": tiny_amount, "D2": tiny_amount,
    }
    last_price = {sym: 50000.0 for sym in symbol_themes}

    views = _score_themes_approx(cfg, list(symbol_themes.keys()), change_rate, trading_amount, symbol_themes)
    view_names = {v[1] for v in views}
    check("테마 채점 - 동반상승 2종목 이상인 테마만 통과(테마A)", "테마A" in view_names)
    check("테마 채점 - 혼자만 오른 테마는 탈락(테마B)", "테마B" not in view_names)
    check("테마 채점 - 거래대금 미달이어도 '동반상승'은 등락률만으로 판단(테마C 통과)", "테마C" in view_names)
    check("테마 채점 - 등락률 상한 초과 종목도 breadth 산정에는 포함되지만 후보에선 걸러짐(테마D는 breadth 통과)", "테마D" in view_names)

    candidates, meta = _pick_candidates_approx(cfg, list(symbol_themes.keys()), change_rate, trading_amount, last_price, symbol_themes)
    check("후보 선정 - 테마A 대장주(A1, 거래대금 1위)가 후보에 포함됨", "A1" in candidates)
    check(
        "후보 선정 - candidates_per_theme(2) 를 넘지 않음(A1·A2 만, A3 는 제외)",
        "A2" in candidates and "A3" not in candidates,
        f"candidates={sorted(candidates)}",
    )
    check("후보 선정 - 혼자만 오른 종목은 후보가 아님(B1)", "B1" not in candidates)
    check("후보 선정 - 동반상승했어도 거래대금 미달 종목은 후보가 아님(C1/C2)", "C1" not in candidates and "C2" not in candidates)
    check("후보 선정 - 등락률이 상한(12%)을 넘는 종목은 후보가 아님(D1/D2, 추격매수 제외)", "D1" not in candidates and "D2" not in candidates)
    if "A1" in meta:
        check("후보 메타 - A1 의 테마 순위가 1위(가장 점수 높은 테마)", meta["A1"][1] == 1.0, meta["A1"])
        check("후보 메타 - A1 의 테마내 순위가 1위(거래대금 1위)", meta["A1"][2] == 1.0, meta["A1"])
        check("후보 메타 - 테마A 의 동반상승 종목수(breadth)=3", meta["A1"][3] == 3.0, meta["A1"])


def test_screener_gate_blocks_ungated_universe():
    """[7-2] 통합 확인 - screener_gate=True(기본) 면 "테마 동반상승 2종목 이상 + 거래대금
    하한"을 통과 못 하는 종목은 아무리 강한 진입 신호를 내도 거래가 하나도 안 나야 하고,
    끄면(screener_gate=False, 예전 동작) 유니버스 전체가 그대로 평가돼 거래가 난다 -
    이 차이 자체가 "감사 항목 4"에서 찾은 편향(게이팅 없이 56종목 전체를 매매)의 회귀
    테스트다."""
    from daytrader.playbook import Bar
    from research.backtest import _slip_price

    cfg = load_config(CONFIG_PATH)
    cfg.strategy.entry_order = ["breakout"]
    cfg.strategy.exit_enabled = ["fixed"]
    cfg.entry.use_closed_bars_only = True
    cfg.entry.volume_surge_ratio = 1.1
    cfg.entry.breakout_lookback = 5
    cfg.entry.volume_window = 5
    cfg.capital.max_positions = 5
    cfg.risk.daily_max_trades = 50
    cfg.risk.cooldown_minutes = 0
    cfg.screen.min_trading_amount = 1  # 이 테스트는 "테마 동반상승" 게이팅만 본다(거래대금은 딴 테스트에서).
    # ★ 기본 rescreen_minutes(30)면 09:20 첫 재선정 때는 아직 아무도 안 올라(전부 평평한
    #   워밍업 구간) 후보가 하나도 안 뽑히고, 09:40 신호가 날 때까지 재선정이 다시 안 돌아
    #   PAIR1/PAIR2 도 걸러진다(그 자체로는 버그가 아니라 "재선정 주기가 길면 그 사이에 뜬
    #   종목을 놓친다"는 실전과 같은 특성이다). 이 테스트는 "동반상승했으면 후보가 된다"만
    #   보려는 것이므로 재선정을 매 분마다 돌게 짧게 잡는다.
    cfg.entry.rescreen_minutes = 1

    def bar(hh, mm, o, h, l, c, v):
        return Bar(ts=f"2026-09-11T{hh:02d}:{mm:02d}:00+09:00", open=o, high=h, low=l, close=c, volume=v)

    def make_breakout_bars(base=10000.0, target=10320.0):
        bars = [bar(9, m, base, base + 20, base - 20, base, 100) for m in range(0, 40)]
        bars.append(bar(9, 40, base, base + 300, base - 10, target, 1000))  # 신호 봉(+3.2%, 문턱 통과)
        bars.append(bar(9, 41, target + 10, target + 100, target - 100, target + 40, 500))
        for m in range(42, 46):
            bars.append(bar(9, m, target, target + 20, target - 20, target, 100))
        return bars

    # LONE = 혼자만 뛰는 테마(동반상승 아님, 실전이라면 후보에 못 낌) - 나머지 둘(PAIR1/PAIR2)은 같은
    # 테마에서 같이 뛰어 동반상승 문턱(2종목)을 통과한다.
    bars_by_symbol = {
        "LONE": make_breakout_bars(),
        "PAIR1": make_breakout_bars(),
        "PAIR2": make_breakout_bars(),
    }
    names = {s: s for s in bars_by_symbol}
    themes = {"LONE": "혼자테마", "PAIR1": "짝테마", "PAIR2": "짝테마"}

    gated = run_backtest(cfg, bars_by_symbol, symbol_names=names, symbol_themes=themes, min_warmup=30, screener_gate=True)
    ungated = run_backtest(cfg, bars_by_symbol, symbol_names=names, symbol_themes=themes, min_warmup=30, screener_gate=False)

    gated_symbols = {t.symbol for t in gated.trades}
    ungated_symbols = {t.symbol for t in ungated.trades}
    check(
        "스크리너 게이팅 켜짐 - 혼자만 뛴 종목(LONE)은 신호가 나도 거래되지 않음",
        "LONE" not in gated_symbols, f"게이팅 거래 종목={gated_symbols}",
    )
    check(
        "스크리너 게이팅 켜짐 - 동반상승한 짝(PAIR1/PAIR2)은 거래됨",
        "PAIR1" in gated_symbols or "PAIR2" in gated_symbols, f"게이팅 거래 종목={gated_symbols}",
    )
    check(
        "스크리너 게이팅 꺼짐(screener_gate=False) - 예전처럼 LONE 도 거래됨(유니버스 전체 평가)",
        "LONE" in ungated_symbols, f"비게이팅 거래 종목={ungated_symbols}",
    )


def test_metrics():
    from research.backtest import TradeRecord

    trades = [
        TradeRecord(
            symbol="A", name="A", theme="T", entry_technique="breakout", exit_technique="fixed",
            entry_time="2026-09-10T09:31:00+09:00", entry_price=10000, exit_time="2026-09-10T09:40:00+09:00",
            exit_price=10300 if i % 3 else 9800, qty=10, pnl_krw=(300 if i % 3 else -200) * 10,
            pnl_pct=(0.03 if i % 3 else -0.02), pnl_r=(0.03 if i % 3 else -0.02) / 0.035,
            hold_minutes=9.0, session="09:30~11:00", mae_pct=-0.005, mfe_pct=0.04,
            stopped_fast=(i % 3 == 0),
        )
        for i in range(12)
    ]
    cells = metrics_mod.by_technique_session(trades)
    check("metrics - 셀이 생성됨", len(cells) > 0)
    overall = metrics_mod.overall(trades)
    check("metrics - 전체 거래수 일치", overall.trades == 12, overall.trades)
    check("metrics - 기대값 부호가 맞음(이기는 쪽이 더 많음)", overall.expectancy_pct > 0, overall.expectancy_pct)
    lo, hi = metrics_mod.bootstrap_ci([t.pnl_pct for t in trades])
    check("metrics - 부트스트랩 CI 가 lo<=hi", lo <= hi, f"{lo}, {hi}")


def test_grid_walk_forward_split():
    days = [f"2026-09-{d:02d}" for d in range(1, 21)]
    is_days, oos_days = grid_mod.split_walk_forward(days, 0.7)
    check("워크포워드 - 70/30 분할", len(is_days) == 14 and len(oos_days) == 6, f"{len(is_days)}/{len(oos_days)}")
    check("워크포워드 - 시간순(겹치지 않음)", is_days[-1] < oos_days[0])


def test_grid_overrides_do_not_mutate_base_cfg():
    cfg = load_config(CONFIG_PATH)
    base_lookback = cfg.entry.breakout_lookback
    new_cfg = grid_mod.apply_overrides(cfg, {"entry.breakout_lookback": base_lookback + 100})
    check("그리드 오버라이드 - 새 cfg 에 반영됨", new_cfg.entry.breakout_lookback == base_lookback + 100)
    check("그리드 오버라이드 - 원본 cfg 는 그대로", cfg.entry.breakout_lookback == base_lookback)


def main() -> None:
    t0 = time.time()
    bars, cfg = test_synthetic_generation()
    test_backtest_end_to_end(bars, cfg)
    test_next_bar_fill_and_no_forming_bar_entry()
    test_screener_gate_candidate_selection()
    test_screener_gate_blocks_ungated_universe()
    test_metrics()
    test_grid_walk_forward_split()
    test_grid_overrides_do_not_mutate_base_cfg()
    elapsed = time.time() - t0
    print(f"총 {_total}건 중 실패 {len(_failures)}건 ({elapsed:.1f}초)")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    if elapsed > 60:
        print(f"경고: {elapsed:.1f}초로 60초를 넘었습니다.")
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
