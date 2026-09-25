"""news_guard.py 의 하루 AI 호출 상한이 서버 재시작 후에도 유지되는지 확인한다.
`python tests/test_news_guard_call_persist.py` 로 실행한다."""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DAYTRADER_AUTH_PASSWORD"] = "482913"
os.environ["DAYTRADER_AUTH_SESSION_SECRET"] = "unit-test-session-secret"

from daytrader import news_guard as ng  # noqa: E402
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


def make_cfg(state_dir: str, cap: int = 2) -> object:
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = state_dir
    cfg.groq_api_key = "gsk_TestKey1234567890abcdefXYZ"
    cfg.groq_api_key2 = ""
    cfg.news.ai_filter = True
    cfg.news.mode = "view"
    cfg.news.ai_max_calls_per_day = cap
    return cfg


def make_guard(cfg, titles=None, reply='{"level":"none","reason":""}'):
    calls = {"post": 0}

    def head(market, symbol, name):
        return list(titles or ["뉴스1"])

    def post(system, user, max_tokens):
        calls["post"] += 1
        return reply

    return ng.NewsGuard(cfg, headlines_fn=head, complete_fn=post), calls


def test_persist_across_restart() -> None:
    print("== 하루 호출 상한이 재시작 후에도 유지됨 ==")
    state_dir = tempfile.mkdtemp()
    cfg = make_cfg(state_dir, cap=2)

    g1, c1 = make_guard(cfg)
    g1.gate("A", "A사")
    g1.gate("B", "B사")
    check("첫 인스턴스에서 상한(2회)까지 호출", c1["post"] == 2)
    check("호출 기록 파일이 남음", os.path.exists(os.path.join(state_dir, ng.CALLS_FILE)))

    # ★ "서버 재시작" 흉내 - 같은 state_dir 로 새 NewsGuard 를 만든다(메모리는 새로 시작하지만
    # 디스크의 호출 기록은 그대로 남아 있어야 한다).
    g2, c2 = make_guard(cfg)
    check("재시작 후 오늘 호출 수를 그대로 이어받음", g2.calls_today() == 2, str(g2.calls_today()))
    g2.gate("C", "C사")
    check("재시작 후에도 하루 한도를 넘기면 더 부르지 않음", c2["post"] == 0 and "한도" in g2.last_error, g2.last_error)


def test_old_calls_expire() -> None:
    print("== 24시간이 지난 호출 기록은 상한에서 빠짐 ==")
    import json
    import time

    state_dir = tempfile.mkdtemp()
    cfg = make_cfg(state_dir, cap=2)
    path = os.path.join(state_dir, ng.CALLS_FILE)
    os.makedirs(state_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([time.time() - 90000, time.time() - 90000], f)  # 25시간 전

    g, c = make_guard(cfg)
    check("오래된 기록은 세지 않음", g.calls_today() == 0)
    g.gate("X", "A사")
    check("오래된 기록이 있어도 정상적으로 새로 호출 가능", c["post"] == 1)


def test_no_state_dir_does_not_crash() -> None:
    print("== state_dir 이 쓰기 불가능해도 예외 없이 동작(디스크 오류가 판정을 막지 않음) ==")
    # ★ 부모 경로 자리에 파일을 둬서 os.makedirs 가 항상(권한과 무관하게) 실패하게 만든다.
    blocker = tempfile.NamedTemporaryFile(delete=False)
    blocker.close()
    cfg = make_cfg(tempfile.mkdtemp(), cap=5)
    cfg.state_dir = os.path.join(blocker.name, "state")
    g, c = make_guard(cfg)
    ok, _ = g.gate("X", "A사")
    check("state_dir 을 못 써도 판정은 정상 수행됨", ok is True and c["post"] == 1)


def main() -> None:
    for t in (test_persist_across_restart, test_old_calls_expire, test_no_state_dir_does_not_crash):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
