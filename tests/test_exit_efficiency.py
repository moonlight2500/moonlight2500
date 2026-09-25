"""종목별 청산 효율 추적(exit_efficiency.py) 테스트. `python tests/test_exit_efficiency.py`로 실행한다."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader import exit_efficiency as ee  # noqa: E402

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


def _cfg():
    """exit_efficiency.py 는 cfg.state_dir 만 읽으므로 진짜 config 대신 가벼운 가짜로 격리한다."""
    return SimpleNamespace(state_dir=tempfile.mkdtemp())


def _store_path(cfg) -> str:
    return os.path.join(cfg.state_dir, "exit_efficiency.json")


def test_record_ignores_non_positive_mfe() -> None:
    print("== mfe_pct <= 0 이면 기록하지 않는다(이익 구간을 못 가봤던 거래는 효율을 잴 의미가 없음) ==")
    cfg = _cfg()
    ee.record(cfg, "domestic", "005930", realized_pct=0.01, mfe_pct=0.0)
    check("mfe_pct == 0 이면 파일도 안 생김", not os.path.exists(_store_path(cfg)))
    check("summary 도 비어 있음", ee.summary(cfg) == {})

    ee.record(cfg, "domestic", "005930", realized_pct=0.01, mfe_pct=-0.02)
    check("mfe_pct < 0 이어도 마찬가지로 기록 안 됨", ee.summary(cfg) == {})


def test_record_ignores_none_symbol() -> None:
    print("== symbol=None 이면 아무 것도 안 하고 조용히 리턴한다 ==")
    cfg = _cfg()
    ee.record(cfg, "domestic", None, realized_pct=0.03, mfe_pct=0.05)
    check("symbol=None 이면 파일 생성 없이 무시됨(예외도 없음)", not os.path.exists(_store_path(cfg)))
    check("summary 도 비어 있음", ee.summary(cfg) == {})


def test_widen_multiplier_needs_min_samples() -> None:
    print(f"== 표본이 MIN_SAMPLES({ee.MIN_SAMPLES}) 미만이면 아직 반영 안 함 ==")
    cfg = _cfg()
    for _ in range(ee.MIN_SAMPLES - 1):
        ee.record(cfg, "domestic", "005930", realized_pct=0.001, mfe_pct=1.0)
    mult, why = ee.widen_multiplier(cfg, "domestic", "005930")
    check(f"표본 {ee.MIN_SAMPLES - 1}건으로는 (1.0, '')", mult == 1.0 and why == "", (mult, why))


def test_widen_multiplier_widens_for_poor_efficiency() -> None:
    print("== 계속 효율이 나쁘면(도달 가능 이익을 거의 못 챙김) 익절 목표를 넓힌다 ==")
    # ★ realized_pct 는 아주 작은 양수(0.001) - 이익으로는 끝났지만(그래야 기록됨, exit_efficiency.py
    # 참고) 도달 가능했던 이익(mfe_pct=1.0)의 극히 일부만 챙긴 상황을 흉내낸다.
    cfg = _cfg()
    for _ in range(ee.MIN_SAMPLES):
        ee.record(cfg, "domestic", "005930", realized_pct=0.001, mfe_pct=1.0)
    mult, why = ee.widen_multiplier(cfg, "domestic", "005930")
    check("효율이 거의 0이면 배수가 1.0 초과", mult > 1.0, mult)
    check("아무리 나빠도 MAX_WIDEN을 넘지 않음", mult <= ee.MAX_WIDEN + 1e-9, mult)
    check("효율이 거의 0(전부 못 챙김)이면 MAX_WIDEN 에 매우 근접", abs(mult - ee.MAX_WIDEN) < 0.01, mult)
    check("설명 문자열이 채워짐", isinstance(why, str) and len(why) > 0, why)


def test_widen_multiplier_no_widen_for_good_efficiency() -> None:
    print("== 효율이 충분히 좋으면(EFFICIENCY_FLOOR 이상) 넓히지 않는다 ==")
    cfg = _cfg()
    for _ in range(ee.MIN_SAMPLES):
        ee.record(cfg, "domestic", "005930", realized_pct=1.0, mfe_pct=1.0)
    mult, why = ee.widen_multiplier(cfg, "domestic", "005930")
    check("도달 가능했던 이익을 거의 다 챙겼으면 (1.0, '')", mult == 1.0 and why == "", (mult, why))


def test_record_skips_trades_that_ended_in_loss() -> None:
    print("== ★ 손실로 끝난 거래(realized_pct <= 0)는 mfe_pct > 0 이어도 전혀 기록하지 않는다 ==")
    # ★ 실제로 겪은 버그 - 예전엔 이런 거래도(잠깐 올랐다 반전한 휩쏘) mfe_pct>0 이면 기록해서
    # realized 를 0으로 취급했는데, 그러면 휩쏘가 잦은 종목이 "효율이 낮다"고 잘못 학습돼
    # 다음에 익절 목표를 넓혀 더 크게 잃는 악순환이 됐다(비교 시뮬레이션으로 발견). 이제는
    # 최종적으로 이익이었던 거래만 기록해야 한다.
    cfg = _cfg()
    ee.record(cfg, "domestic", "005930", realized_pct=-0.02, mfe_pct=0.05)
    s = ee.summary(cfg)
    check("손실로 끝난 거래는 표본에 전혀 안 남음", "domestic:005930" not in s, s)

    # 여러 번 반복해도(모두 손실, mfe 는 있음) 계속 기록되지 않고 크래시도 없어야 한다.
    for _ in range(ee.MIN_SAMPLES + 2):
        ee.record(cfg, "domestic", "005930", realized_pct=-0.5, mfe_pct=0.05)
    mult, why = ee.widen_multiplier(cfg, "domestic", "005930")
    check("계속된 손실 거래는 표본이 하나도 안 쌓여 넓히지 않는다", (mult, why) == (1.0, ""), (mult, why))
    check("summary 에도 계속 안 나타남", "domestic:005930" not in ee.summary(cfg))


def test_tracks_symbols_independently() -> None:
    print("== 시장·종목 조합별로 서로 다른 키로 독립 추적된다 ==")
    cfg = _cfg()
    for _ in range(ee.MIN_SAMPLES):
        ee.record(cfg, "crypto", "KRW-BTC", realized_pct=0.001, mfe_pct=1.0)

    mult_btc, _why = ee.widen_multiplier(cfg, "crypto", "KRW-BTC")
    check("기록한 시장:종목은 넓혀짐", mult_btc > 1.0, mult_btc)

    mult_other_market, why1 = ee.widen_multiplier(cfg, "domestic", "KRW-BTC")
    check("같은 심볼이라도 다른 시장이면 영향 없음(1.0, '')", mult_other_market == 1.0 and why1 == "", (mult_other_market, why1))

    mult_other_symbol, why2 = ee.widen_multiplier(cfg, "crypto", "KRW-ETH")
    check("같은 시장이라도 다른 종목이면 영향 없음(1.0, '')", mult_other_symbol == 1.0 and why2 == "", (mult_other_symbol, why2))


def test_summary_arithmetic() -> None:
    print("== summary() 의 효율 계산이 직접 계산한 값과 일치하는지 ==")
    cfg = _cfg()
    ee.record(cfg, "domestic", "005930", realized_pct=0.02, mfe_pct=0.04)
    ee.record(cfg, "domestic", "005930", realized_pct=0.01, mfe_pct=0.02)
    s = ee.summary(cfg)
    key = "domestic:005930"
    check("표본 1건 이상이면 summary 에 나타남", key in s, s)
    check("n = 2", s[key]["n"] == 2, s[key])
    expected_efficiency = (0.02 + 0.01) / (0.04 + 0.02)  # = 0.5
    check("efficiency = sum(realized)/sum(mfe) 와 정확히 일치",
          abs(s[key]["efficiency"] - expected_efficiency) < 1e-9, (s[key]["efficiency"], expected_efficiency))


def test_clear_resets_everything() -> None:
    print("== clear() 는 저장소를 완전히 비운다 ==")
    cfg = _cfg()
    for _ in range(ee.MIN_SAMPLES):
        ee.record(cfg, "domestic", "005930", realized_pct=0.001, mfe_pct=1.0)
    mult_before, _ = ee.widen_multiplier(cfg, "domestic", "005930")
    check("clear 전에는 강한 신호(넓힘)가 있음", mult_before > 1.0, mult_before)

    ee.clear(cfg)
    check("clear 후 summary 는 빈 dict", ee.summary(cfg) == {})
    mult_after, why_after = ee.widen_multiplier(cfg, "domestic", "005930")
    check("clear 후에는 이전에 강했던 신호도 (1.0, '')로 리셋", mult_after == 1.0 and why_after == "", (mult_after, why_after))


def test_persistence_round_trip() -> None:
    print("== 메모리에만 있는 게 아니라 실제로 디스크에 JSON 으로 저장되는지 ==")
    cfg = _cfg()
    ee.record(cfg, "overseas", "AAPL", realized_pct=0.03, mfe_pct=0.06)

    path = _store_path(cfg)
    check("state_dir 아래에 exit_efficiency.json 파일이 생김", os.path.exists(path), path)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    key = "overseas:AAPL"
    check("파일 내용이 dict 이고 키가 '시장:종목' 형태", isinstance(raw, dict) and key in raw, raw)
    check("파일에 n·sum_realized·sum_mfe 가 그대로 저장됨",
          raw[key]["n"] == 1 and abs(raw[key]["sum_realized"] - 0.03) < 1e-9 and abs(raw[key]["sum_mfe"] - 0.06) < 1e-9,
          raw[key])

    # 같은 state_dir 을 가리키는 새 summary() 호출 - exit_efficiency 는 매번 파일에서 새로 읽으므로
    # 이것이 곧 "새로 로드해도 남아있는지"에 대한 검증이다.
    s = ee.summary(cfg)
    check("다시 읽어도(summary) 값이 그대로", s[key]["n"] == 1 and abs(s[key]["efficiency"] - 0.5) < 1e-9, s)


def main() -> None:
    for t in (test_record_ignores_non_positive_mfe, test_record_ignores_none_symbol,
              test_widen_multiplier_needs_min_samples, test_widen_multiplier_widens_for_poor_efficiency,
              test_widen_multiplier_no_widen_for_good_efficiency, test_record_skips_trades_that_ended_in_loss,
              test_tracks_symbols_independently, test_summary_arithmetic, test_clear_resets_everything,
              test_persistence_round_trip):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
