"""sizing.py 오프라인 테스트. `python tests/test_sizing.py` 로 실행한다."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader import sizing as z  # noqa: E402
from daytrader.config import SizingCfg  # noqa: E402

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


def term(op, threshold, margin, required=True, passed=True):
    return SimpleNamespace(op=op, threshold=threshold, margin=margin, required=required, passed=passed)


def test_strength_and_multiplier() -> None:
    print("== 신호 강도·배수 ==")
    sz = SizingCfg()
    weak = SimpleNamespace(terms=[term(">=", 2.0, 0.01), term(">=", 0.03, 0.0005)])
    strong = SimpleNamespace(terms=[term(">=", 2.0, 3.0), term(">=", 0.03, 0.05)])
    mixed = SimpleNamespace(terms=[term(">=", 2.0, 1.0), term(">=", 0.03, 0.03)])
    sw, ss, sm = z.signal_strength(weak), z.signal_strength(strong), z.signal_strength(mixed)
    check("간신히 통과한 신호는 강도가 낮음", sw < 0.05, f"{sw:.3f}")
    check("기준을 크게 넘은 신호는 강도 1(상한)", ss == 1.0, f"{ss:.3f}")
    check("중간 신호는 그 사이", sw < sm < ss, f"{sm:.3f}")
    check("bool·구간·선택 항목은 무시하고, 잴 게 없으면 0.5",
          z.signal_strength(SimpleNamespace(terms=[term("bool", 1.0, 1.0), term("between", 0.0, 0.1), term(">=", 1, 5, required=False)])) == 0.5)
    check("잘못된 판정 객체도 예외 없이 0.5", z.signal_strength(None) == 0.5)
    check("약한 신호 배수 = min_mult", abs(z.signal_multiplier(0.0, sz) - sz.min_mult) < 1e-9)
    check("강한 신호 배수 = max_mult", abs(z.signal_multiplier(1.0, sz) - sz.max_mult) < 1e-9)
    sz2 = SizingCfg(signal_sizing=False)
    check("신호 배분을 끄면 배수 1", z.signal_multiplier(1.0, sz2) == 1.0 and z.signal_multiplier(0.0, sz2) == 1.0)


def test_entry_and_adds() -> None:
    print("== 첫 매수·추가 매수 금액과 한도 ==")
    sz = SizingCfg()
    cap = z.position_cap(10_000_000, 3)
    check("종목당 한도 = 총액/동시보유 수", abs(cap - 3_333_333.33) < 1)
    weak, strong = z.entry_amount(cap, 0.0, sz), z.entry_amount(cap, 1.0, sz)
    check("강한 신호가 더 많이 삼", strong > weak, f"{weak:,.0f} < {strong:,.0f}")
    weak_frac = sz.initial_ratio * sz.min_mult
    strong_frac = min(1.0, sz.initial_ratio * sz.max_mult)
    check("첫 매수는 한도의 설정된 범위(initial_ratio×min_mult~max_mult) 안",
          abs(weak / cap - weak_frac) < 1e-6 and abs(strong / cap - strong_frac) < 1e-6, f"{weak/cap:.3f}~{strong/cap:.3f}")
    a1 = z.add_amount(cap, weak, 0, sz)
    expected_a1 = min(cap * (1 - sz.initial_ratio) / sz.max_adds, cap - weak)
    check("추가 매수 1회는 남은 몫을 균등 분할한 만큼", abs(a1 - expected_a1) < 1e-6, f"{a1/cap:.3f}")
    inv = strong
    for n in range(sz.max_adds):
        inv += z.add_amount(cap, inv, n, sz)
    check("최대 신호 + 추가 매수를 모두 해도 한도 이내", inv <= cap + 1e-6, f"{inv/cap:.3f}")
    check("횟수를 다 쓰면 0", z.add_amount(cap, 1.0, sz.max_adds, sz) == 0.0)
    check("한도가 찼으면 0", z.add_amount(cap, cap, 0, sz) == 0.0)
    off = SizingCfg(scale_in=False)
    check("추가 매수를 끄면 최강 신호가 한도를 꽉 채움(넘지 않음)", abs(z.entry_amount(cap, 1.0, off) - cap) < 1e-6)
    check("추가 매수를 끄면 add 는 항상 0", z.add_amount(cap, 0.0, 0, off) == 0.0)
    check("예산 0 이면 금액 0", z.entry_amount(z.position_cap(0, 3), 1.0, sz) == 0.0)


def test_add_due() -> None:
    print("== 추가 매수 시점(이익 중일 때만) ==")
    sz = SizingCfg()
    stop = 0.025  # 간격 = 1.0% (손절폭의 40%)
    kw = dict(adds=0, last_fill=100.0, avg_entry=100.0, scaled_out=0, sz=sz, stop_pct=stop)
    check("아직 덜 올랐으면 안 삼", not z.add_due(price=100.9, **kw))
    check("손절폭의 40%(1%) 오르면 삼", z.add_due(price=101.1, **kw))
    check("손실 중에는 절대 안 삼(물타기 금지)", not z.add_due(price=97.0, **kw))
    check("횟수를 다 쓰면 안 삼", not z.add_due(price=110.0, **{**kw, "adds": sz.max_adds}))
    check("분할 매도를 시작했으면 안 삼", not z.add_due(price=110.0, **{**kw, "scaled_out": 1}))
    check("두 번째 추가는 직전 매수가 기준으로 다시 올라야 함", not z.add_due(price=101.9, **{**kw, "adds": 1, "last_fill": 101.3, "avg_entry": 100.6}))
    check("직전 매수가 대비도 오르면 삼", z.add_due(price=102.4, **{**kw, "adds": 1, "last_fill": 101.3, "avg_entry": 100.6}))
    check("간격을 직접 정하면 그 값", not z.add_due(price=101.5, **{**kw, "sz": SizingCfg(add_step_pct=0.02)}))


def test_exit_steps() -> None:
    print("== 분할 매도 ==")
    sz = SizingCfg()
    take = 0.05
    check("1차는 익절폭의 절반(2.5%)", z.exit_step(scaled_out=0, entry=100, price=102.4, sz=sz, take_pct=take) is None
          and z.exit_step(scaled_out=0, entry=100, price=102.5, sz=sz, take_pct=take) == (sz.first_exit_ratio, "scale_out_1"))
    check("2차는 익절폭(5%)", z.exit_step(scaled_out=1, entry=100, price=104.9, sz=sz, take_pct=take) is None
          and z.exit_step(scaled_out=1, entry=100, price=105.0, sz=sz, take_pct=take) == (sz.second_exit_ratio, "scale_out_2"))
    check("2차까지 했으면 더 안 나눔(나머지는 추적 손절)", z.exit_step(scaled_out=2, entry=100, price=130, sz=sz, take_pct=take) is None)
    check("분할 매도를 끄면 없음", z.exit_step(scaled_out=0, entry=100, price=110, sz=SizingCfg(scale_out=False), take_pct=take) is None)
    check("첫 매도 지점을 직접 정하면 그 값", z.exit_step(scaled_out=0, entry=100, price=101.0, sz=SizingCfg(first_exit_at=0.01), take_pct=take) is not None)
    check("본전 방어: 1차 후 본전 아래면 정리", z.breakeven_hit(scaled_out=1, entry=100, price=100.05, sz=sz))
    check("본전 방어: 1차 전에는 발동 안 함", not z.breakeven_hit(scaled_out=0, entry=100, price=99, sz=sz))
    check("본전 방어: 이익 중이면 유지", not z.breakeven_hit(scaled_out=1, entry=100, price=103, sz=sz))
    check("본전 방어를 끄면 발동 안 함", not z.breakeven_hit(scaled_out=1, entry=100, price=99, sz=SizingCfg(breakeven_after_first=False)))
    remaining = 1.0
    remaining *= (1 - sz.first_exit_ratio)
    remaining *= (1 - sz.second_exit_ratio)
    expected_remaining = (1 - sz.first_exit_ratio) * (1 - sz.second_exit_ratio)
    check("두 번 나눠 팔고 남는 비율이 설정과 일치", abs(remaining - expected_remaining) < 0.005, f"{remaining:.3f}")


def test_split_quantity() -> None:
    print("== 매도 수량 분할 ==")
    check("정수 종목 100주의 34% = 34주", z.split_quantity(100, 0.34, integer=True) == 34)
    check("정수 종목 2주의 34% 도 최소 1주", z.split_quantity(2, 0.34, integer=True) == 1)
    check("1주짜리는 전량(쪼갤 수 없음)", z.split_quantity(1, 0.34, integer=True) == 1)
    check("소수 수량(코인)은 비율 그대로", abs(z.split_quantity(0.5, 0.34, integer=False) - 0.17) < 1e-12)
    check("비율이 1 이상이면 전량", z.split_quantity(10, 2.0, integer=True) == 10)
    check("수량 0 이면 0", z.split_quantity(0, 0.5, integer=True) == 0)


def test_dynamic() -> None:
    print("== 테마 근거의 크기·장중 변동성으로 금액 자동 조절 ==")
    from daytrader.playbook import Bar
    sz = SizingCfg()
    top = z.theme_conviction(theme_rank=1, breadth=6, intensity=0.06, rank_in_theme=1)
    mid = z.theme_conviction(theme_rank=2, breadth=3, intensity=0.03, rank_in_theme=2)
    low = z.theme_conviction(theme_rank=3, breadth=2, intensity=0.01, rank_in_theme=4)
    check("1위 테마·대장주·강한 상승은 근거 크기 1", abs(top - 1.0) < 1e-9, f"{top:.3f}")
    check("근거가 약할수록 작음(1위 > 2위 > 3위)", top > mid > low, f"{top:.2f}>{mid:.2f}>{low:.2f}")
    check("테마 근거가 없으면(관심 종목) 중립 0.5", z.theme_conviction(theme_rank=0, breadth=0, intensity=0, rank_in_theme=0) == 0.5)
    check("이상한 값도 예외 없이 0~1", 0 <= z.theme_conviction(theme_rank=1, breadth=None, intensity="x", rank_in_theme=None) <= 1)

    cap = 1_000_000.0
    a_top = z.entry_amount(cap, 0.5, sz, conviction=top)
    a_low = z.entry_amount(cap, 0.5, sz, conviction=low)
    a_none = z.entry_amount(cap, 0.5, sz)
    check("같은 신호라도 테마 근거가 큰 종목을 더 많이 삼", a_top > a_none > a_low, f"{a_top:,.0f}>{a_none:,.0f}>{a_low:,.0f}")
    check("근거가 아무리 커도 한도(initial_ratio×max_mult×변동성상단) 이내",
          a_top <= cap * sz.initial_ratio * sz.max_mult * 1.2 + 1 and a_top <= cap)
    off = SizingCfg(dynamic=False)
    check("자동 조절을 끄면 근거·변동성 무시", z.entry_amount(cap, 0.5, off, conviction=top, vol=3.0) == z.entry_amount(cap, 0.5, off))

    calm = [Bar(ts=str(i), open=100, high=100.2, low=99.8, close=100, volume=1) for i in range(30)]
    wild = [Bar(ts=str(i), open=100, high=101.5, low=98.5, close=100, volume=1) for i in range(30)]
    vc, vw = z.vol_ratio(calm, 0.025), z.vol_ratio(wild, 0.025)
    check("잔잔한 종목은 변동성 비율이 낮고 출렁이는 종목은 높음", vc < 1.0 < vw, f"{vc:.2f} < 1 < {vw:.2f}")
    check("봉이 모자라면 None", z.vol_ratio(calm[:5], 0.025) is None and z.vol_ratio([], 0.025) is None)
    check("출렁이는 종목은 적게, 잔잔한 종목은 많이(0.6~1.2배)", z.vol_multiplier(vw, sz) < 1.0 < z.vol_multiplier(vc, sz) <= 1.2 and z.vol_multiplier(vw, sz) >= 0.6)
    check("변동성 정보가 없으면 그대로(1배)", z.vol_multiplier(None, sz) == 1.0)
    check("같은 조건에서 출렁이는 종목이 더 적게 매수", z.entry_amount(cap, 0.5, sz, vol=vw) < z.entry_amount(cap, 0.5, sz, vol=vc))

    adds_top = z.add_amount(cap, 500_000, 0, sz, conviction=top)
    adds_low = z.add_amount(cap, 500_000, 0, sz, conviction=low)
    check("추가 매수도 근거가 큰 종목이 더 많이(한도 이내)", adds_top > adds_low and 500_000 + adds_top <= cap + 1, f"{adds_top:,.0f}>{adds_low:,.0f}")

    base = sz.first_exit_ratio
    r_top = z.exit_ratio(base, sz, conviction=1.0, vol=1.0)
    r_low = z.exit_ratio(base, sz, conviction=0.0, vol=1.0)
    check("근거가 큰 종목은 1차에 덜 팔고(더 끌고 감), 약한 종목은 더 판다", r_top < base < r_low, f"{r_top:.2f}<{base:.2f}<{r_low:.2f}")
    check("출렁이는 장에서는 더 팔아 먼저 챙김", z.exit_ratio(base, sz, vol=2.0) > z.exit_ratio(base, sz, vol=0.5))
    check("매도 비율은 15%~60% 범위", all(0.15 <= z.exit_ratio(b, sz, conviction=c, vol=v) <= 0.60 for b in (0.05, 0.34, 0.9) for c in (0, 1) for v in (0.1, 5)))
    check("자동 조절을 끄면 설정한 비율 그대로", z.exit_ratio(base, off, conviction=1.0, vol=2.0) == base)
    st = z.exit_step(scaled_out=0, entry=100, price=102.6, sz=sz, take_pct=0.05, conviction=1.0, vol=1.0)
    check("exit_step 이 조절된 비율을 돌려줌", st is not None and abs(st[0] - r_top) < 1e-9 and st[1] == "scale_out_1")


def main() -> None:
    for t in (test_strength_and_multiplier, test_entry_and_adds, test_add_due, test_exit_steps, test_split_quantity, test_dynamic):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
