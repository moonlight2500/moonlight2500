"""market_commentary.compose() 가 헤드라인 발행 시각을 AI 에 알려주고, 결과에 작성
시각(KST)을 남기는지 확인한다. `python tests/test_market_commentary_freshness.py` 로
실행한다."""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DAYTRADER_AUTH_PASSWORD"] = "482913"
os.environ["DAYTRADER_AUTH_SESSION_SECRET"] = "unit-test-session-secret"

from daytrader import llm  # noqa: E402
from daytrader import market_commentary as mc  # noqa: E402
from daytrader.config import load_config  # noqa: E402
from daytrader.timeutil import now_kst  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config.yaml")
_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


def make_cfg():
    cfg = load_config(CONFIG_PATH)
    cfg.groq_api_key = "gsk_TestKey1234567890abcdefXYZ"
    cfg.groq_api_key2 = ""
    return cfg


def test_headline_timestamps_sent_to_llm() -> None:
    print("== 헤드라인 발행 시각이 AI 프롬프트에 포함됨 ==")
    mc.reset_cache_for_tests()
    cfg = make_cfg()
    seen = {}
    orig = llm.complete
    llm.complete = lambda cfg, system, user, max_tokens=300, temperature=0.2, **kw: (seen.update(system=system, user=user) or "• 요약")
    try:
        headlines = [
            {"publisher": "구글뉴스", "title": "코스피 상승 마감", "published": "2026-09-24T15:30:00+09:00"},
            {"publisher": "연합뉴스", "title": "시각 없는 기사"},  # published 없음
        ]
        text = mc.compose(cfg, headlines, market="domestic")
        check("결과가 만들어짐", bool(text))
        check("발행 시각이 사용자 메시지에 포함(한국시간)", "09/24 15:30" in seen["user"], seen["user"])
        check("시각이 없는 기사는 '시각 미상'으로 표시(추측하지 않음)", "시각 미상" in seen["user"])
        check("시스템 프롬프트가 오래된 기사를 오늘 일처럼 쓰지 말라고 함", "오늘 일어난 일처럼" in seen["system"])
    finally:
        llm.complete = orig


def test_output_has_generation_timestamp() -> None:
    print("== 결과 메시지에 작성 시각(KST)이 남음 ==")
    mc.reset_cache_for_tests()
    cfg = make_cfg()
    orig = llm.complete
    llm.complete = lambda cfg, system, user, max_tokens=300, temperature=0.2, **kw: "• 요약"
    try:
        before = now_kst()
        text = mc.compose(cfg, [{"publisher": "구글뉴스", "title": "코스피 상승"}], market="domestic")
        m = re.search(r"작성 시각: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) KST", text)
        check("작성 시각 줄이 있음", m is not None, text)
        check("KST 라고 명시됨", "KST" in text)
        if m:
            check("작성 시각이 방금(생성 시점)과 같은 분", m.group(1) == before.strftime("%Y-%m-%d %H:%M") or
                  m.group(1) == now_kst().strftime("%Y-%m-%d %H:%M"))
    finally:
        llm.complete = orig


def test_cached_result_keeps_original_timestamp() -> None:
    print("== 캐시로 재사용된 결과는 '재사용 시각'이 아니라 '원래 생성 시각'을 유지함 ==")
    mc.reset_cache_for_tests()
    cfg = make_cfg()
    orig = llm.complete
    llm.complete = lambda cfg, system, user, max_tokens=300, temperature=0.2, **kw: "• 요약"
    try:
        headlines = [{"publisher": "구글뉴스", "title": "코스피 상승"}]
        first = mc.compose(cfg, headlines, market="domestic")
        second = mc.compose(cfg, headlines, market="domestic")  # 캐시 히트
        check("캐시 히트여도 같은 텍스트(같은 작성 시각)를 돌려줌 - 조회할 때마다 시각이 바뀌지 않음", first == second)
    finally:
        llm.complete = orig


def main() -> None:
    for t in (test_headline_timestamps_sent_to_llm, test_output_has_generation_timestamp, test_cached_result_keeps_original_timestamp):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
