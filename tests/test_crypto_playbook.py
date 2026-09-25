"""crypto_playbook.py 오프라인 테스트. `python tests/test_crypto_playbook.py` 로 실행한다."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader import crypto_playbook as cp  # noqa: E402

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


def test_volatility_breakout() -> None:
    print("\n== 변동성 돌파 ==")
    target = cp.volatility_breakout_target(prev_high=105, prev_low=95, today_open=100, k=0.5)
    check("목표가 계산 (100+10*0.5=105)", target == 105.0)

    candles = [
        {"open": 90, "high": 105, "low": 95, "close": 100},
        {"open": 100, "high": 110, "low": 99, "close": 108},
    ]
    v_below = cp.check_volatility_breakout(candles, current_price=104, k=0.5)
    check("목표가 미달이면 진입 안 함", v_below.ok is False)

    v_above = cp.check_volatility_breakout(candles, current_price=106, k=0.5)
    check("목표가 돌파하면 진입", v_above.ok is True)
    check("기법명이 volatility_breakout", v_above.technique == "volatility_breakout")

    v_short = cp.check_volatility_breakout([candles[0]], current_price=999)
    check("★캔들 2개 미만이면 표본 부족으로 보류(진입 안 함)", v_short.ok is False and "표본" in v_short.reason)


def test_ma_cross() -> None:
    print("\n== 이동평균 추세 확인 ==")
    up = list(range(1, 21))
    check("상승 추세 인식", cp.ma_cross_confirm(up, short=5, long=20).ok is True)

    down = list(range(20, 0, -1))
    check("하락 추세는 거부", cp.ma_cross_confirm(down, short=5, long=20).ok is False)

    short_sample = cp.ma_cross_confirm([1, 2, 3], short=5, long=20)
    check("★표본 부족이면 보류", short_sample.ok is False and "표본" in short_sample.reason)


def test_rsi() -> None:
    print("\n== RSI ==")
    all_up = [100 + i for i in range(16)]
    check("계속 상승만 하면 RSI=100", cp.rsi(all_up, period=14) == 100.0)

    all_down = [100 - i for i in range(16)]
    check("계속 하락만 하면 RSI=0", cp.rsi(all_down, period=14) == 0.0)

    check("표본 부족이면 None", cp.rsi([1, 2, 3], period=14) is None)

    check("과매도(RSI=0)면 반등 진입", cp.check_rsi_oversold_bounce(all_down, period=14, oversold=30).ok is True)
    check("과매도 아니면(RSI=100) 진입 안 함", cp.check_rsi_oversold_bounce(all_up, period=14, oversold=30).ok is False)


def test_exit_priority() -> None:
    print("\n== 청산 우선순위(손절 > 익절 > 추적청산 > 시간청산) ==")

    v = cp.check_exit(entry_price=100, current_price=94, peak_price=100, held_hours=1)
    check("손절 진입가 -5% 이하", v.ok and v.technique == "stop_loss")

    v = cp.check_exit(entry_price=100, current_price=111, peak_price=111, held_hours=1)
    check("익절 진입가 +10% 이상", v.ok and v.technique == "take_profit")

    v = cp.check_exit(entry_price=100, current_price=108, peak_price=112, held_hours=1)
    check("추적청산 - 익절 미달이지만 고점 대비 3%↓", v.ok and v.technique == "trailing_stop")

    v = cp.check_exit(entry_price=100, current_price=102, peak_price=103, held_hours=25)
    check("시간청산 - 24시간 초과", v.ok and v.technique == "time_stop")

    v = cp.check_exit(entry_price=100, current_price=101, peak_price=101, held_hours=2)
    check("아무 조건도 해당 없으면 보유 유지", v.ok is False and v.technique == "hold")

    v = cp.check_exit(entry_price=100, current_price=90, peak_price=100, held_hours=48)
    check("★손절과 시간청산 동시 해당 시 손절이 우선", v.technique == "stop_loss")

    v = cp.check_exit(entry_price=100, current_price=111, peak_price=120, held_hours=1)
    check("★익절과 추적청산 동시 해당 시 익절이 우선", v.technique == "take_profit")


def test_techniques_metadata() -> None:
    print("\n== 기법 메타데이터(원전·표준값) ==")
    check("진입 3종 + 청산 4종 = 7개", len(cp.TECHNIQUES) == 7)
    for t in cp.TECHNIQUES:
        keys_ok = all(k in t for k in ("key", "phase", "label", "description", "origin", "standard"))
        check(f"{t.get('label', '?')} 필드 완전함", keys_ok)
        if keys_ok:
            check(f"{t['label']} origin 비어있지 않음", bool(t["origin"].strip()))
            check(f"{t['label']} standard 비어있지 않음", bool(t["standard"].strip()))


def main() -> None:
    tests = [test_volatility_breakout, test_ma_cross, test_rsi, test_exit_priority, test_techniques_metadata]
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
