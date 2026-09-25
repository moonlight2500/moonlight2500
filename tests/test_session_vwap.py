"""VWAP·당일 고가가 09:00 개장부터 누적되는지 검증한다(3-2).
네트워크 없이 도는 검증. `python tests/test_session_vwap.py` 로 실행한다.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from math import isnan
from types import SimpleNamespace

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
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


def make_bar(ts, o, h, l, c, v):
    from daytrader.playbook import Bar
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v)


def make_engine():
    from daytrader.config import load_config
    from daytrader.engine import Engine

    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = tempfile.mkdtemp()
    return Engine(cfg, None)


def full_vwap(bars):
    num = sum((b.high + b.low + b.close) / 3 * b.volume for b in bars)
    den = sum(b.volume for b in bars)
    return num / den if den else float("nan")


def section_accumulation() -> None:
    print("\n== 세션 누적 VWAP·고가 ==")
    engine = make_engine()

    # 09:00 부터 09:39 까지 40개 봉을 만든다. 실제 엔진처럼 "최근 20개짜리
    # 굴러가는 창"만 여러 번에 걸쳐 넘겨서, 초반 봉이 창 밖으로 밀려나도
    # 세션 누적치는 안 잃는지 확인한다.
    all_bars = []
    for i in range(40):
        base_high = 10020 + i * 5
        # 5~9번째 봉(개장 직후)에 하루 중 가장 높은 고점(스파이크)을 만들어 둔다 -
        # 그 뒤로는 계속 낮은 고가만 나오게 해, "최근 20봉 창"만 보면 그 고점을
        # 영영 놓치는지 확인할 수 있게 한다.
        high = 11000 if 5 <= i < 10 else base_high
        all_bars.append(make_bar(
            f"2026-09-04T09:{i:02d}:00", 10000 + i * 5, high,
            9990 + i * 5, 10010 + i * 5, 1000 + i * 10,
        ))

    window = 20
    for i in range(0, len(all_bars), 5):
        chunk = all_bars[max(0, i + 5 - window):i + 5]
        engine._session_stats_for("005930", chunk)

    v, h = engine._session_stats_for("005930", [])
    expected_v = full_vwap(all_bars)
    expected_h = max(b.high for b in all_bars)
    check(
        "굴러가는 창으로 여러 번 나눠 넘겨도 세션 VWAP 이 전체 40봉 VWAP 과 일치",
        abs(v - expected_v) < 1e-6, f"got={v} expected={expected_v}",
    )
    check(
        "세션 고가가 전체 40봉 중 최고가(초반 봉 포함)와 일치",
        h == expected_h, f"got={h} expected={expected_h}",
    )

    # 굴러가는 창만 봤다면(옛 방식) 개장 직후 스파이크가 창 밖으로 밀려나
    # 마지막 20개봉의 최고가만 남아 더 낮았을 것이다.
    windowed_high = max(b.high for b in all_bars[-window:])
    check(
        "옛 굴러가는-창 방식(마지막 20봉만)보다 세션 고가가 더 높다(개장 직후 고점을 놓치지 않음)",
        expected_h > windowed_high, f"session={expected_h} windowed={windowed_high}",
    )

    # 같은 타임스탬프의 봉을 다시 넘겨도(같은 루프에서 재조회) 중복 반영되지 않는다.
    v2, _ = engine._session_stats_for("005930", all_bars[-5:])
    check("이미 반영한 봉을 다시 넘겨도 누적치가 그대로", abs(v2 - expected_v) < 1e-6, f"v2={v2}")

    # 다른 종목은 독립적으로 누적된다.
    other_bars = [make_bar("2026-09-04T09:00:00", 500, 520, 490, 510, 100)]
    v_other, h_other = engine._session_stats_for("000660", other_bars)
    check("종목별로 세션 누적치가 독립적", abs(h_other - 520) < 1e-9 and h_other != h)


def section_rollover() -> None:
    print("\n== 날짜가 바뀌면 세션 누적치 초기화 ==")
    engine = make_engine()
    engine._session_stats_for("005930", [make_bar("2026-09-04T09:00:00", 100, 110, 90, 105, 10)])
    check("첫날 누적됨", "005930" in engine._session_stats)

    class _FakeClock:
        def now(self):
            from datetime import datetime
            return datetime(2026, 9, 5, 9, 0, 0)

    engine.clock = _FakeClock()
    engine._rollover_if_new_day()
    check("날짜가 바뀌면 세션 누적치를 비운다", engine._session_stats == {})


def section_playbook_fallback() -> None:
    print("\n== playbook 의 session_vwap_or/session_high_or 폴백 ==")
    from daytrader.playbook import session_high_or, session_vwap_or

    bars = [
        make_bar("09:00", 100, 110, 90, 100, 10),
        make_bar("09:01", 100, 130, 95, 120, 20),
    ]
    windowed_v = full_vwap(bars)
    windowed_h = max(b.high for b in bars)

    ctx_with = SimpleNamespace(session_vwap=999.0, session_high=888.0)
    check(
        "ctx.session_vwap 이 있으면 그 값을 그대로 쓴다",
        session_vwap_or(bars, ctx_with) == 999.0,
    )
    check(
        "ctx.session_high 가 있으면 그 값을 그대로 쓴다",
        session_high_or(bars, ctx_with) == 888.0,
    )

    ctx_without = SimpleNamespace()
    check(
        "ctx 에 session_vwap 이 없으면 bars 로 근사한 vwap 을 쓴다",
        abs(session_vwap_or(bars, ctx_without) - windowed_v) < 1e-9,
    )
    check(
        "ctx 에 session_high 가 없으면 bars 최고가로 근사한다",
        session_high_or(bars, ctx_without) == windowed_h,
    )

    ctx_nan = SimpleNamespace(session_vwap=float("nan"), session_high=float("nan"))
    check(
        "ctx.session_vwap 이 nan(세션 데이터 아직 없음)이면 bars 로 근사한다",
        abs(session_vwap_or(bars, ctx_nan) - windowed_v) < 1e-9,
    )


def main() -> int:
    section_accumulation()
    section_rollover()
    section_playbook_fallback()

    print(f"\n{_total - len(_failures)}/{_total} 통과")
    if _failures:
        print("실패:", ", ".join(_failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
