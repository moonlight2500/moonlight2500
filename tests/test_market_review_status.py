"""서버: 오늘의 시장 평가(market_commentary)를 못 만들 때 화면에 보여주는 사유가
실제 원인(키 없음 vs 키는 있는데 막힘 vs 자료 없음)을 구분하는지 확인한다.
`python tests/test_market_review_status.py` 로 실행한다."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DAYTRADER_AUTH_PASSWORD"] = "482913"
os.environ["DAYTRADER_AUTH_SESSION_SECRET"] = "unit-test-session-secret"

import daytrader.server as S  # noqa: E402
from daytrader import llm  # noqa: E402
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


def make_cfg(key: str = "") -> object:
    cfg = load_config(CONFIG_PATH)
    cfg.groq_api_key = key
    cfg.groq_api_key2 = ""
    return cfg


def test_no_key_reason() -> None:
    print("== 키가 아예 없으면: 키 등록 안내 ==")
    cfg = make_cfg(key="")
    reason = S._market_review_unavailable_reason(cfg)
    check("키 등록 안내 문구", "등록되어 있지 않습니다" in reason, reason)


def test_key_registered_but_failing_reason() -> None:
    print("== 키는 있는데 두 키 모두 막혀 있으면: 실제 사유(예: 한도 초과)를 보여줌 ==")
    cfg = make_cfg(key="gsk_TestKey1234567890abcdefXYZ")
    llm.reset_for_tests()

    def post_quota(url, headers, body, timeout):
        class R:
            status_code = 429
        return R()

    llm.complete(cfg, "s", "u", post=post_quota)  # 실패시켜서 llm._state.last_error 를 채운다
    reason = S._market_review_unavailable_reason(cfg)
    check("키가 없다는 문구가 아님(실제로는 등록돼 있으므로)", "등록되어 있지 않습니다" not in reason, reason)
    check("실제 실패 사유(한도 초과)가 드러남", "한도 초과" in reason or "429" in reason, reason)
    llm.reset_for_tests()


def test_no_data_reason() -> None:
    print("== 키도 있고 AI 도 정상인데 자료 자체가 없으면: 자료 없음 안내 ==")
    cfg = make_cfg(key="gsk_TestKey1234567890abcdefXYZ")
    llm.reset_for_tests()  # last_error 없음(아직 실패한 적 없음)
    reason = S._market_review_unavailable_reason(cfg)
    check("자료 없음/빈 응답 안내", "자료가 없거나" in reason, reason)


def test_preview_endpoint_uses_reason_helper() -> None:
    print("== /api/review/market/preview 가 위 사유를 그대로 씀 ==")
    orig_build = S.build_market_review
    orig_cfg_now = S.cfg_now
    S.build_market_review = lambda market="domestic": None
    S.cfg_now = lambda: make_cfg(key="")
    try:
        out = S.preview_market_review(market="domestic")
        check("text 는 빈 문자열", out["text"] == "")
        check("reason 에 키 등록 안내가 담김", "등록되어 있지 않습니다" in out["reason"], out["reason"])
    finally:
        S.build_market_review = orig_build
        S.cfg_now = orig_cfg_now


def main() -> None:
    for t in (test_no_key_reason, test_key_registered_but_failing_reason, test_no_data_reason, test_preview_endpoint_uses_reason_helper):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
