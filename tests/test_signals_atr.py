"""ATR 이 SMA 가 아니라 Wilder(1978) 평활을 쓰는지 검증한다.
네트워크 없이 도는 검증. `python tests/test_signals_atr.py` 로 실행한다.
"""

from __future__ import annotations

import io
import os
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dataclasses import dataclass
from math import isnan

from daytrader.signals import NAN, atr

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


@dataclass
class Bar:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float


def _old_windowed_sma_atr(bars, n=14):
    """리팩터 이전 구현(직전 n개 TR 의 단순평균)을 그대로 재현해 비교용으로 둔다."""
    if len(bars) < n + 1:
        return NAN
    trs = []
    for i in range(len(bars) - n, len(bars)):
        pc = bars[i - 1].close
        tr = max(bars[i].high - bars[i].low, abs(bars[i].high - pc), abs(bars[i].low - pc))
        trs.append(tr)
    return sum(trs) / n


def _manual_wilder_atr(bars, n=14):
    """정의를 그대로 따라간 참조 구현(첫값=SMA seed, 이후 (prev*(n-1)+tr)/n)."""
    trs = []
    for i in range(1, len(bars)):
        pc = bars[i - 1].close
        tr = max(bars[i].high - bars[i].low, abs(bars[i].high - pc), abs(bars[i].low - pc))
        trs.append(tr)
    if len(trs) < n:
        return NAN
    val = sum(trs[:n]) / n
    for tr in trs[n:]:
        val = (val * (n - 1) + tr) / n
    return val


def make_bars():
    # 20~24번째 봉에서만 변동성이 크게 튀고(뉴스 등) 이후 다시 평상시로 돌아온다.
    # 마지막 n(=14)개 창에는 그 급변동이 더 이상 보이지 않아야
    # "직전 n개 SMA"와 "전체 이력을 반영하는 Wilder"의 차이가 드러난다.
    bars = []
    price = 10000.0
    for i in range(40):
        rng = 300.0 if 20 <= i < 25 else 40.0
        o = price
        h = price + rng * 0.7
        l = price - rng * 0.7
        c = price + (rng * 0.1 if i % 2 == 0 else -rng * 0.1)
        bars.append(Bar(ts=str(i), open=o, high=h, low=l, close=c, volume=1000))
        price = c
    return bars


def main() -> int:
    bars = make_bars()

    check("데이터 부족(n+1 미만)이면 nan", isnan(atr(bars[:10], 14)))

    new_val = atr(bars, 14)
    manual_val = _manual_wilder_atr(bars, 14)
    check(
        "atr()이 Wilder 평활 참조 구현과 일치",
        abs(new_val - manual_val) < 1e-9,
        f"atr={new_val} manual={manual_val}",
    )

    old_val = _old_windowed_sma_atr(bars, 14)
    check(
        "Wilder ATR 이 과거 변동성 스파이크(20~24번 봉)를 창 밖으로 밀려난 뒤에도 더 오래 반영해 SMA보다 크다",
        new_val > old_val,
        f"wilder={new_val:.2f} windowed_sma={old_val:.2f}",
    )

    # atr_multiple 기본값(설정 파일 기준 4.5)로 손절폭이 실제로 벌어지는지 확인.
    mult = 4.5
    entry = bars[-1].close
    old_line = entry - old_val * mult
    new_line = entry - new_val * mult
    check(
        "atr_multiple=4.5 적용 시 Wilder 손절선이 SMA 손절선보다 더 아래(더 넓음)",
        new_line < old_line,
        f"old_line={old_line:.2f} new_line={new_line:.2f}",
    )

    print(f"\n{_total - len(_failures)}/{_total} 통과")
    if _failures:
        print("실패:", ", ".join(_failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
