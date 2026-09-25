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
    result = run_backtest(cfg, {sym: bars}, symbol_names=names, symbol_themes=themes, min_warmup=30)

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
    result2 = run_backtest(cfg, {sym2: bars_last}, symbol_names={sym2: sym2}, symbol_themes={sym2: "테스트"}, min_warmup=30)
    check(
        "그 날 마지막 봉에서 난 신호는 체결되지 않음(다음 봉 없음)",
        len(result2.trades) == 0, f"{len(result2.trades)}건 체결됨(있으면 안 됨)",
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
