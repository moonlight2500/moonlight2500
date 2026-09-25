"""국내주식 테마 스크리닝(screener.py) 테스트 - 특히 "오늘 종목 선정이 없습니다" 안내 문구가
실제 판정과 앞뒤가 맞는지(daytrader/screener.py의 build_report() "테마 없음" 분기).
`python tests/test_screener.py`로 실행한다.
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

import yaml  # noqa: E402

from daytrader.config import load_config  # noqa: E402
from daytrader.screener import Screener  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config.yaml")

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


def _cfg_with_themes(themes: dict):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.safe_dump({"themes": themes}, f, allow_unicode=True)
        path = f.name
    cfg = load_config(CONFIG_PATH)
    cfg.themes_file = path
    return cfg, path


def _fake_client(rows: list):
    class FakeClient:
        def rankings(self, type, marketCountry, duration, count):  # noqa: A002
            if type == "TOP_GAINERS":
                return rows
            return []
    return FakeClient()


def test_no_theme_message_ignores_untracked_symbols() -> None:
    print("== ★ '오늘 종목 선정 없음' 안내가 themes.yaml 에 없는 종목(워런트 등)을 무시함 ==")
    cfg, path = _cfg_with_themes({"2차전지": ["005930", "000660"]})
    try:
        # ★★★ 실제로 겪은 버그 - "0010S0"(워런트로 추정, themes.yaml 에 없음)가 +288.33%로
        # 거래소 전체 등락률 1위였는데, 옛 코드는 이걸 그대로 "가장 많이 오른 종목"으로 뽑아서
        # "상승 인정 기준(3%)에 못 미칩니다"라고 말했다 - 288% 가 3% 에 못 미친다는 건 명백히
        # 말이 안 된다. themes.yaml 에 등록된 종목만 후보가 될 수 있으므로, 그 안에서만
        # "가장 많이 오른 종목"을 찾아야 한다.
        client = _fake_client([
            {"symbol": "0010S0", "price": {"lastPrice": 5000, "changeRate": 2.8833}, "tradingAmount": 1_000_000},
            {"symbol": "005930", "price": {"lastPrice": 70000, "changeRate": 0.01}, "tradingAmount": 500_000},
        ])
        report = Screener(client, cfg).build_report()
        check("테마 없음으로 판정됨", not any(t.picked for t in report.themes))
        check("워런트(0010S0)가 아니라 실제 테마 종목(005930)을 언급함",
              "005930" in report.summary and "0010S0" not in report.summary, report.summary)
        check("표시한 상승률이 실제로 기준(3%)보다 낮음(모순 없음)", "+1.00%" in report.summary, report.summary)
        check("'기준에 못 미칩니다'가 실제로 참인 경우에만 쓰임", "못 미칩니다" in report.summary, report.summary)
    finally:
        os.unlink(path)


def test_no_theme_message_explains_insufficient_companions() -> None:
    print("== ★ 개별 종목은 기준을 넘겨도 동반 상승 종목 수가 부족하면 '기준 미달'이라 말하지 않음 ==")
    cfg, path = _cfg_with_themes({"2차전지": ["005930", "000660"]})
    try:
        # 005930 은 +5%(기준 3% 초과)지만, 같은 테마의 000660 은 +0.5%(미달) - 동반 상승
        # 종목 수(1)가 min_theme_members_up(2)에 못 미쳐 테마로 인정되지 않는다. 이때 문구는
        # "기준 미달"이 아니라 "함께 오른 종목이 부족하다"로 정확해야 한다.
        client = _fake_client([
            {"symbol": "005930", "price": {"lastPrice": 70000, "changeRate": 0.05}, "tradingAmount": 500_000},
            {"symbol": "000660", "price": {"lastPrice": 70000, "changeRate": 0.005}, "tradingAmount": 500_000},
        ])
        report = Screener(client, cfg).build_report()
        check("테마 없음으로 판정됨", not any(t.picked for t in report.themes))
        check("기준을 넘긴 종목 언급함", "+5.00%" in report.summary, report.summary)
        check("'기준에 못 미칩니다'라고 말하지 않음(실제로는 넘겼으므로)",
              "기준" not in report.summary or "못 미칩니다" not in report.summary, report.summary)
        check("동반 상승 종목 부족을 설명함", "함께 오른 종목" in report.summary, report.summary)
    finally:
        os.unlink(path)


def test_no_theme_message_handles_no_candidates_at_all() -> None:
    print("== 테마 종목의 시세를 하나도 못 받았을 때도 죽지 않음 ==")
    cfg, path = _cfg_with_themes({"2차전지": ["005930", "000660"]})
    try:
        client = _fake_client([])
        report = Screener(client, cfg).build_report()
        check("테마 없음으로 판정됨", not any(t.picked for t in report.themes))
        check("요약 문구가 비어 있지 않음", bool(report.summary), report.summary)
    finally:
        os.unlink(path)


def main() -> None:
    for t in (
        test_no_theme_message_ignores_untracked_symbols,
        test_no_theme_message_explains_insufficient_companions,
        test_no_theme_message_handles_no_candidates_at_all,
    ):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
