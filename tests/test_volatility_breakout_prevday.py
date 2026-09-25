"""volatility_breakout 가 국내·해외주식에서는 전일 실제 고가·저가를 쓰고,
암호화폐(또는 그 값을 안 주는 호출자)에서는 기존 반반 근사를 쓰는지 검증한다(3-3).
네트워크 없이 도는 검증. `python tests/test_volatility_breakout_prevday.py` 로 실행한다.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
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


def make_bars(n=20, base=100.0):
    from daytrader.playbook import Bar
    # 앞 절반(직전 근사 구간)은 변동폭이 작고, 뒤 절반(오늘 근사 구간)은 변동폭이 크다 -
    # 반반 근사와 전일 실제 레인지가 서로 다른 target 을 내도록 일부러 갈라 둔다.
    bars = []
    for i in range(n):
        if i < n // 2:
            h, l = base + 1, base - 1
        else:
            h, l = base + 10, base - 10
        bars.append(Bar(ts=f"t{i}", open=base, high=h, low=l, close=base, volume=100))
    return bars


def section_playbook_branch() -> None:
    print("\n== VolatilityBreakoutEntry: ctx.prev_day_* 유무에 따른 분기 ==")
    from daytrader.playbook import Playbook
    from daytrader.config import load_config

    cfg = load_config(CONFIG_PATH)
    pb = Playbook(cfg, market="domestic", learning_mode="none")
    technique = next(t for t in pb.entries if t.key == "volatility_breakout") if any(
        t.key == "volatility_breakout" for t in pb.entries
    ) else None
    if technique is None:
        from daytrader.playbook import VolatilityBreakoutEntry
        technique = VolatilityBreakoutEntry(cfg)

    bars = make_bars()

    # ① ctx 에 전일 실제 레인지가 있으면 그걸 쓴다(반반 근사와는 다른 값이어야 한다).
    ctx_daily = SimpleNamespace(
        symbol="005930", name="삼성전자", theme=None, prev_verdict=None,
        prev_day_high=200.0, prev_day_low=50.0,
    )
    v_daily = technique.evaluate(bars, ctx_daily)
    check(
        "ctx.prev_day_high/low 가 있으면 그 값을 prev_high/prev_low 로 씀",
        v_daily.inputs["prev_high"] == 200.0 and v_daily.inputs["prev_low"] == 50.0,
        f"inputs={v_daily.inputs}",
    )

    # ② ctx 에 없으면(암호화폐, 백테스트 등) 기존 반반 근사로 되돌아간다.
    ctx_approx = SimpleNamespace(symbol="KRW-BTC", name="비트코인", theme=None, prev_verdict=None)
    v_approx = technique.evaluate(bars, ctx_approx)
    mid = len(bars) // 2
    expected_prev_high = max(b.high for b in bars[:mid])
    expected_prev_low = min(b.low for b in bars[:mid])
    check(
        "ctx 에 prev_day_high/low 가 없으면 반반 근사(기존 동작)를 그대로 씀",
        v_approx.inputs["prev_high"] == expected_prev_high and v_approx.inputs["prev_low"] == expected_prev_low,
        f"inputs={v_approx.inputs}",
    )
    check(
        "두 분기가 서로 다른 target 을 낸다(테스트 데이터가 일부러 갈라 놓음)",
        v_daily.inputs["target"] != v_approx.inputs["target"],
    )

    # ③ nan 이 채워진 경우(값을 못 구했음)도 근사로 되돌아간다.
    ctx_nan = SimpleNamespace(
        symbol="X", name="X", theme=None, prev_verdict=None,
        prev_day_high=float("nan"), prev_day_low=float("nan"),
    )
    v_nan = technique.evaluate(bars, ctx_nan)
    check(
        "prev_day_high/low 가 nan 이면 근사로 되돌아감",
        v_nan.inputs["prev_high"] == expected_prev_high,
    )


class _FakeDailyClient:
    """1d 캔들 조회 횟수를 세어 캐시가 실제로 동작하는지 확인한다."""

    def __init__(self):
        self.calls = 0

    def candles(self, symbol, interval, count, before=None):
        self.calls += 1
        # 뒤에서 두 번째(어제)가 200/50, 마지막(오늘, 미완성)이 다른 값 -
        # bars[-2] 를 쓰는지 확인할 수 있게 서로 다르게 만든다.
        return [
            {"timestamp": "yesterday", "openPrice": 100, "highPrice": 200, "lowPrice": 50, "closePrice": 150, "volume": 10},
            {"timestamp": "today", "openPrice": 150, "highPrice": 999, "lowPrice": 999, "closePrice": 150, "volume": 10},
        ]


def section_engine_prev_day_range() -> None:
    print("\n== engine.py Engine._prev_day_range (국내) ==")
    from daytrader.config import load_config
    from daytrader.engine import Engine

    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = tempfile.mkdtemp()
    client = _FakeDailyClient()
    engine = Engine(cfg, client)

    h, l = engine._prev_day_range("005930")
    check("전일(뒤에서 두 번째) 고가·저가를 쓴다(오늘 미완성 봉이 아님)", h == 200 and l == 50, f"h={h} l={l}")

    engine._prev_day_range("005930")
    check("심볼당 하루 한 번만 조회(캐시)", client.calls == 1, f"calls={client.calls}")

    h2, l2 = engine._prev_day_range("000660")
    check("다른 종목은 별도로 조회됨", client.calls == 2 and h2 == 200 and l2 == 50)


def section_overseas_prev_day_range() -> None:
    print("\n== overseas_engine.py OverseasEngine._make_ctx (해외) ==")
    from daytrader.config import load_config
    from daytrader.overseas_engine import OverseasEngine

    class _FakeOverseasClient:
        def __init__(self):
            self.calls = 0

        def buying_power(self, currency="KRW"):
            return {"cash": 10_000_000}

        def prices(self, symbols):
            return [{"symbol": symbols[0], "price": 100.0}]

        def candles(self, symbol, interval, count, before=None):
            if interval == "1d":
                self.calls += 1
                return [
                    {"timestamp": "yesterday", "open": 90, "high": 120, "low": 80, "close": 100, "volume": 10},
                    {"timestamp": "today", "open": 100, "high": 999, "low": 999, "close": 100, "volume": 10},
                ]
            return [{"timestamp": str(i), "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10} for i in range(count)]

    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = d
    cfg.overseas.mode = "paper"
    cfg.strategy.entry_order = ["breakout"]  # volatility_breakout 이 꺼진 기본 상태
    client = _FakeOverseasClient()
    engine = OverseasEngine(cfg, client=client)

    ctx_off = engine._make_ctx("AAPL", "AAPL", None)
    check(
        "volatility_breakout 이 entry_order 에 없으면 일봉을 조회하지 않음(불필요한 API 호출 방지)",
        not hasattr(ctx_off, "prev_day_high") and client.calls == 0,
    )

    cfg.strategy.entry_order = ["volatility_breakout"]
    ctx_on = engine._make_ctx("AAPL", "AAPL", None)
    check(
        "volatility_breakout 이 켜져 있으면 전일 고가·저가를 ctx 에 채움",
        getattr(ctx_on, "prev_day_high", None) == 120 and getattr(ctx_on, "prev_day_low", None) == 80,
        f"ctx.prev_day_high={getattr(ctx_on, 'prev_day_high', None)}",
    )
    check("일봉 조회는 한 번만 일어남", client.calls == 1, f"calls={client.calls}")

    engine._make_ctx("AAPL", "AAPL", None)
    check("같은 날 다시 불러도 캐시를 써서 추가 조회 없음", client.calls == 1, f"calls={client.calls}")


def main() -> int:
    section_playbook_branch()
    section_engine_prev_day_range()
    section_overseas_prev_day_range()

    print(f"\n{_total - len(_failures)}/{_total} 통과")
    if _failures:
        print("실패:", ", ".join(_failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
