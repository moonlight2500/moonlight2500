"""market_commentary.compose() 가 같은 입력(헤드라인·가격)에 대해 Groq 를 다시 부르지
않는지 확인한다(미리보기 화면을 여러 번 열어도 호출 한 번). `python
tests/test_market_commentary_cache.py` 로 실행한다."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DAYTRADER_AUTH_PASSWORD"] = "482913"
os.environ["DAYTRADER_AUTH_SESSION_SECRET"] = "unit-test-session-secret"

from daytrader import llm  # noqa: E402
from daytrader import market_commentary as mc  # noqa: E402
from daytrader.config import load_config  # noqa: E402

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


HEADLINES = [{"publisher": "구글뉴스", "title": "코스피 2% 상승 마감"}]
PRICES = [{"symbol": "SPY", "close": 500.0, "change_pct": 1.2, "date": "2026-09-24"}]


def with_fake_complete(reply="• 시장이 올랐다."):
    calls = {"n": 0}
    orig = llm.complete

    def fake(cfg, system, user, max_tokens=300, temperature=0.2, **kw):
        calls["n"] += 1
        return reply

    llm.complete = fake
    return calls, orig


def test_same_input_hits_cache_once() -> None:
    print("== 같은 입력이면 Groq 를 한 번만 부름(미리보기 반복 클릭 대응) ==")
    mc.reset_cache_for_tests()
    cfg = make_cfg()
    calls, orig = with_fake_complete()
    try:
        r1 = mc.compose(cfg, HEADLINES, market="domestic", price_snapshot=PRICES)
        r2 = mc.compose(cfg, HEADLINES, market="domestic", price_snapshot=PRICES)
        r3 = mc.compose(cfg, list(HEADLINES), market="domestic", price_snapshot=list(PRICES))
        check("결과가 있음", bool(r1))
        check("두 번째 호출도 같은 텍스트를 그대로 돌려줌", r1 == r2)
        check("입력이 같으면(새 리스트여도) Groq 는 딱 한 번만 부름", calls["n"] == 1, str(calls["n"]))
        check("세 번째 호출도 캐시에서(내용 동일)", r1 == r3)
    finally:
        llm.complete = orig


def test_different_input_calls_again() -> None:
    print("== 입력(헤드라인·가격)이 바뀌면 다시 부름 ==")
    mc.reset_cache_for_tests()
    cfg = make_cfg()
    calls, orig = with_fake_complete()
    try:
        mc.compose(cfg, HEADLINES, market="domestic", price_snapshot=PRICES)
        other_headlines = [{"publisher": "구글뉴스", "title": "코스피 3% 급락"}]
        mc.compose(cfg, other_headlines, market="domestic", price_snapshot=PRICES)
        check("헤드라인이 바뀌면 다시 호출", calls["n"] == 2)

        other_price = [{"symbol": "SPY", "close": 500.0, "change_pct": -2.5, "date": "2026-09-24"}]
        mc.compose(cfg, HEADLINES, market="domestic", price_snapshot=other_price)
        check("가격이 바뀌면 다시 호출", calls["n"] == 3)

        mc.compose(cfg, HEADLINES, market="overseas", price_snapshot=PRICES)
        check("같은 헤드라인·가격이어도 시장 구분이 다르면 다시 호출(캐시 키에 market 포함)", calls["n"] == 4)
    finally:
        llm.complete = orig


def test_cache_expires() -> None:
    print("== 캐시도 시간이 지나면 새로 만듦 ==")
    mc.reset_cache_for_tests()
    cfg = make_cfg()
    calls, orig = with_fake_complete()
    try:
        mc.compose(cfg, HEADLINES, market="domestic", price_snapshot=PRICES)
        check("첫 호출", calls["n"] == 1)
        # ★ 캐시 항목의 시각을 TTL 이전으로 되돌려 만료를 흉내낸다.
        digest = mc._review_digest(HEADLINES, PRICES)
        key = ("domestic", digest)
        text, ts = mc._CACHE[key]
        mc._CACHE[key] = (text, ts - mc.CACHE_TTL_SEC - 1)
        mc.compose(cfg, HEADLINES, market="domestic", price_snapshot=PRICES)
        check("TTL 이 지나면 같은 입력이어도 다시 부름", calls["n"] == 2)
    finally:
        llm.complete = orig


def test_no_llm_or_no_data_skips_cache() -> None:
    print("== 키가 없거나 자료가 없으면 캐시도 호출도 없음 ==")
    mc.reset_cache_for_tests()
    cfg = make_cfg()
    cfg.groq_api_key = ""
    calls, orig = with_fake_complete()
    try:
        check("Groq 키가 없으면 None(캐시 조회 없이)", mc.compose(cfg, HEADLINES, price_snapshot=PRICES) is None and calls["n"] == 0)
        cfg2 = make_cfg()
        check("헤드라인·가격이 둘 다 없으면 None", mc.compose(cfg2, [], price_snapshot=None) is None and calls["n"] == 0)
    finally:
        llm.complete = orig


def main() -> None:
    for t in (test_same_input_hits_cache_once, test_different_input_calls_again, test_cache_expires,
              test_no_llm_or_no_data_skips_cache):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
