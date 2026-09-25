"""technique_backtest.py 의 체결 시점·슬리피지(3-4)와, best_technique 선정에
최소 거래 수 문턱(strategy.min_trades_for_weight)이 적용되는지 검증한다.
`python tests/test_backtest_fill_timing.py` 로 실행한다.
"""

from __future__ import annotations

import io
import os
import sys
from unittest.mock import patch

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CONFIG_PATH = os.path.join(ROOT, "config.yaml")

from daytrader import technique_backtest as tb  # noqa: E402
from daytrader.config import load_config  # noqa: E402
from daytrader.playbook import Bar  # noqa: E402

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


def _flat_then_breakout_bars(n_flat=60, extra_after=40):
    """평평하게 다지다가 거래량과 함께 크게 뚫는 봉을 만들고, 그 뒤로 extra_after 개 더 이어 붙인다."""
    bars = [_bar(i, 10000, 10050, 9950, 10000, 500) for i in range(n_flat)]
    bars.append(_bar(n_flat, 10000, 10300, 9990, 10280, 5000))  # 돌파 봉(거래량 급증·양봉)
    price = 10280
    for i in range(n_flat + 1, n_flat + 1 + extra_after):
        price += 60
        bars.append(_bar(i, price - 60, price + 20, price - 20, price, 400))
    return bars


def section_fill_timing() -> None:
    print("\n== 체결 시점: 신호가 난 봉(i)이 아니라 다음 봉(i+1) 시가 + 슬리피지 ==")
    cfg = load_config(CONFIG_PATH)
    n_flat = 60
    bars = _flat_then_breakout_bars(n_flat=n_flat, extra_after=40)
    result = tb.simulate_technique(cfg, "domestic", "breakout", bars, "005930", "삼성전자", "반도체")
    check("거래가 생김", result["trades"] >= 1, str(result))

    breakout_bar = bars[n_flat]
    fill_bar = bars[n_flat + 1]
    entry_ts = result["sample"][0]["entry_ts"] if result["sample"] else None
    # ★ 신호를 낸 돌파 봉의 종가/시각이 아니라 그 다음 봉의 시각으로 체결돼야 한다.
    check(
        "체결 시각이 신호가 난 봉(i)이 아니라 다음 봉(i+1)",
        entry_ts == fill_bar.ts and entry_ts != breakout_bar.ts,
        f"entry_ts={entry_ts} breakout_bar.ts={breakout_bar.ts} fill_bar.ts={fill_bar.ts}",
    )

    # ★ 슬리피지 반영 확인 - 체결가가 다음 봉 시가보다 살짝 높아야(매수는 불리한 방향) 한다.
    slip_pct = cfg.risk.max_slippage_pct
    expected_fill_price = fill_bar.open * (1 + slip_pct)
    # pnl_pct 로 역산: exit_price 는 익절가(entry*(1+take_profit_pct))이므로 직접 재구성하기보다
    # entry_price 를 직접 볼 수 있는 _slip_price 헬퍼로 교차 검증한다.
    check(
        "_slip_price(BUY) 가 다음 봉 시가보다 max_slippage_pct 만큼 높은 값을 냄",
        abs(tb._slip_price(fill_bar.open, "BUY", cfg) - expected_fill_price) < 1e-9,
    )
    check("슬리피지가 0보다 크면 체결가가 원래 시가보다 비쌈", tb._slip_price(100.0, "BUY", cfg) > 100.0)
    check("매도 슬리피지는 반대 방향(더 싸게)", tb._slip_price(100.0, "SELL", cfg) < 100.0)


def section_no_fill_on_last_bar() -> None:
    print("\n== 마지막 봉에서 신호가 나면 체결할 다음 봉이 없어 거래로 잡지 않는다 ==")
    cfg = load_config(CONFIG_PATH)
    # 돌파 봉을 데이터의 맨 마지막에 둔다(extra_after=0) - 체결을 검증할 다음 봉이 없다.
    bars = _flat_then_breakout_bars(n_flat=60, extra_after=0)
    result = tb.simulate_technique(cfg, "domestic", "breakout", bars, "005930", "삼성전자", "반도체")
    check("마지막 봉의 신호는 체결되지 않아 거래 0건", result["trades"] == 0, str(result))


# ── best_technique 최소 거래 수 문턱 ────────────────────────────────────────

def _fake_simulate_factory(table: dict):
    """(symbol, technique) -> (trades, total_pnl_pct) 표를 그대로 흉내내는 simulate_technique 대역."""
    def _fake(cfg, market, key, bars, symbol, name, theme, risk=None):
        trades, total = table.get((symbol, key), (0, 0.0))
        if trades == 0:
            return tb._empty_result(key, key)
        return {
            "technique": key, "label": key, "trades": trades,
            "wins": trades, "losses": 0, "win_rate": 1.0,
            "total_pnl_pct": total, "avg_pnl_pct": total / trades,
            "profit_factor": float("inf"), "sample": [],
        }
    return _fake


def section_min_trades_gate_by_symbol() -> None:
    print("\n== run(): 거래 수가 min_trades_for_weight 미만이면 best_technique 를 안 뽑는다(종목 단위) ==")
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = __import__("tempfile").mkdtemp()
    min_trades = cfg.strategy.min_trades_for_weight
    candidates = [{"symbol": "005930", "name": "삼성전자", "theme": "반도체"}]

    # breakout 이 가장 높은 total_pnl_pct 를 내지만 거래가 min_trades_for_weight 미만이다 -
    # 예전에는 거래가 1건만 있어도 1등으로 뽑혔다.
    table_under = {("005930", "breakout"): (min_trades - 1, 0.5)}
    with patch("daytrader.technique_backtest.fetch_bars", return_value=[object()] * 200), \
         patch("daytrader.technique_backtest.simulate_technique", side_effect=_fake_simulate_factory(table_under)):
        out = tb.run(cfg, client=None, market="domestic", candidates=candidates, save=False)
    row = out["by_symbol"][0]
    check(
        f"거래 {min_trades - 1}건(문턱 {min_trades}건 미만)이면 best_technique 는 None",
        row["best_technique"] is None, str(row),
    )
    check(
        "그래도 각 기법의 개별 결과(results)는 그대로 남아 화면에서 볼 수 있음",
        any(r["technique"] == "breakout" and r["trades"] == min_trades - 1 for r in row["results"]),
    )

    # 거래 수가 문턱 이상이면 정상적으로 뽑힌다.
    table_over = {("005930", "breakout"): (min_trades, 0.5)}
    with patch("daytrader.technique_backtest.fetch_bars", return_value=[object()] * 200), \
         patch("daytrader.technique_backtest.simulate_technique", side_effect=_fake_simulate_factory(table_over)):
        out2 = tb.run(cfg, client=None, market="domestic", candidates=candidates, save=False)
    check(
        f"거래가 문턱({min_trades}건) 이상이면 best_technique 로 뽑힘",
        out2["by_symbol"][0]["best_technique"] == "breakout", str(out2["by_symbol"][0]),
    )


def section_min_trades_gate_by_theme() -> None:
    print("\n== run(): 테마 단위 best_technique 도 합산 거래 수 문턱을 적용한다 ==")
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = __import__("tempfile").mkdtemp()
    min_trades = cfg.strategy.min_trades_for_weight
    candidates = [
        {"symbol": "005930", "name": "삼성전자", "theme": "반도체"},
        {"symbol": "000660", "name": "SK하이닉스", "theme": "반도체"},
    ]

    # 두 종목 합쳐도 breakout 거래 수가 문턱 미만이다.
    half = (min_trades - 1) // 2 or 1
    table_under = {
        ("005930", "breakout"): (half, 0.3),
        ("000660", "breakout"): (half, 0.3),
    }
    with patch("daytrader.technique_backtest.fetch_bars", return_value=[object()] * 200), \
         patch("daytrader.technique_backtest.simulate_technique", side_effect=_fake_simulate_factory(table_under)):
        out = tb.run(cfg, client=None, market="domestic", candidates=candidates, save=False)
    theme_row = next((r for r in out["by_theme"] if r["theme"] == "반도체"), None)
    check("테마 합산 거래 수가 문턱 미만이면 best_technique 는 None", theme_row is not None and theme_row["best_technique"] is None, str(theme_row))

    # 합쳐서 문턱을 넘기면 뽑힌다.
    table_over = {
        ("005930", "breakout"): (min_trades, 0.3),
        ("000660", "breakout"): (min_trades, 0.3),
    }
    with patch("daytrader.technique_backtest.fetch_bars", return_value=[object()] * 200), \
         patch("daytrader.technique_backtest.simulate_technique", side_effect=_fake_simulate_factory(table_over)):
        out2 = tb.run(cfg, client=None, market="domestic", candidates=candidates, save=False)
    theme_row2 = next((r for r in out2["by_theme"] if r["theme"] == "반도체"), None)
    check("합산 거래 수가 문턱 이상이면 best_technique 로 뽑힘", theme_row2 is not None and theme_row2["best_technique"] == "breakout", str(theme_row2))


def main() -> int:
    section_fill_timing()
    section_no_fill_on_last_bar()
    section_min_trades_gate_by_symbol()
    section_min_trades_gate_by_theme()

    print(f"\n{_total - len(_failures)}/{_total} 통과")
    if _failures:
        print("실패:", ", ".join(_failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
