"""[8-1] async 라우트가 블로킹 네트워크 호출로 이벤트 루프를 막지 않는지 검증하는
회귀 테스트. `python tests/test_async_routes.py` 로 실행한다.

★★★ 실제로 겪은 버그(py-spy 로 확인) - daytrader/server.py 의 overseas_status() 는
async def 인데, 그 안에서 (해외 엔진이 꺼져 있을 때) market.snapshot() 을 거쳐 실제
네트워크 호출을 동기(블로킹)로 했다. asyncio 이벤트 루프 안에서 await 없이 블로킹
호출을 하면 그 요청 하나가 아니라 서버 전체(다른 모든 동시 요청)가 멈춘다.

이 파일은 두 가지 방식으로 회귀를 막는다.
  1) 정적 검사: server.py 소스를 읽어, market.*·news.*·webquote.*·llm.*·requests.* 를
     직접(= await asyncio.to_thread(...) 로 감싸지 않고) 호출하던 라우트들이 지금은
     ``async def`` 가 아니라 일반 ``def`` 로 선언돼 있는지 확인한다(그래야 FastAPI 가
     스레드풀에서 돌려 이벤트 루프를 막지 않는다).
  2) 동적 검사: market.snapshot() 을 일부러 느리게(sleep) 만든 가짜로 바꿔 끼우고,
     실제 ASGI 앱(단일 이벤트 루프) 위에서 /api/market 요청과 /api/auth/check 요청을
     asyncio.gather 로 "동시에" 보낸다. 이벤트 루프가 막히지 않는다면 /api/auth/check
     는 /api/market 이 끝나길 기다리지 않고 빨리 끝나야 한다.
"""

from __future__ import annotations

import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ★ secrets.get() 은 환경변수가 파일보다 우선이다 - 파일을 읽거나 쓰지 않고 테스트한다.
os.environ.setdefault("DAYTRADER_AUTH_PASSWORD", "482913")
os.environ.setdefault("DAYTRADER_AUTH_SESSION_SECRET", "unit-test-session-secret")

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    line = f"[{'OK  ' if cond else 'FAIL'}] {name}"
    if extra:
        line += f" - {extra}"
    print(line)
    if not cond:
        _failures.append(name)


# ★ [8-1] 에서 "async def -> def" 로 고친 라우트들 - 전부 시세·뉴스 등 블로킹
# 네트워크 호출을 함수 본문에서 직접(await 없이) 한다.
_FIXED_ROUTES = [
    "overseas_status",
    "get_market",
    "get_news",
    "refresh_news",
    "test_news",
    "overseas_ticker",
    "bithumb_ticker",
]


def test_previously_blocking_routes_are_plain_def() -> None:
    print("\n== ★★★ [8-1] 블로킹 네트워크 호출을 하던 라우트가 async def 가 아님(스레드풀에서 실행됨) ==")
    src = open(os.path.join(ROOT, "daytrader", "server.py"), encoding="utf-8").read()

    for name in _FIXED_ROUTES:
        m = re.search(rf"^(async )?def {re.escape(name)}\(", src, re.M)
        check(f"{name}() 가 server.py 에 있음", m is not None, name)
        if not m:
            continue
        check(f"{name}() 는 async def 가 아님(일반 def - FastAPI 스레드풀에서 실행됨)",
              m.group(1) is None, m.group(0))

    # ★ 혹시 나중에 다시 async def 로 바뀌면서 그 안에서 블로킹 호출이 부활하는 걸 막는다 -
    # "여전히 남아 있는 async def 라우트가 market.*/news.*/webquote.*/llm.*/requests.* 를
    # await 없이 직접 부르면 안 된다"는 일반 규칙을 대략적으로 점검한다.
    blocking_call = re.compile(
        r"(?<!await )(?<!await asyncio\.to_thread\()"
        r"\b(?:market|market_mod|news|webquote|llm|requests)\.\w+\(|feed\.fetch\(|\.self_test\("
    )
    for m in re.finditer(r"^async def (\w+)\(.*?\n(?P<body>(?:^(?:[ \t].*)?\n)*)", src, re.M):
        name = m.group(1)
        body = m.group("body")
        # 다음 top-level def/class/데코레이터가 나오기 전까지만 본문으로 본다(위 정규식은
        # 들여쓰기 된 줄만 모으므로 이미 그렇게 잘려 있다).
        hits = [ln.strip() for ln in body.splitlines() if blocking_call.search(ln)]
        check(f"async def {name}() 안에 (await 없는) 블로킹 시세/뉴스 호출이 없음",
              not hits, f"{name}: {hits}")


def test_slow_market_snapshot_does_not_block_other_requests() -> None:
    print("\n== ★★★ [8-1] market.snapshot() 이 느려도(가짜로 1초 sleep) 동시 요청이 안 밀림 ==")
    import asyncio

    import httpx

    import daytrader.server as server
    from daytrader import market as market_mod

    SLEEP_S = 1.0

    def slow_snapshot(*_a, **_kw):
        time.sleep(SLEEP_S)
        return {"groups": [], "errors": [], "total": 0}

    orig_snapshot = market_mod.snapshot
    market_mod.snapshot = slow_snapshot

    # ★★ [5-2] 패턴과 동일 - Host/Origin 검증(DNS 리바인딩 방어)을 loopback 주소로
    # 통과시키고, /api/market 은 로그인 벽 뒤에 있으므로 트레이가 쓰는 로컬 제어
    # 통로(DAYTRADER_LOCAL_TOKEN + X-Local-Control)로 인증한다.
    local_token = "test-local-control-token-8-1"
    orig_local_token = os.environ.get("DAYTRADER_LOCAL_TOKEN")
    os.environ["DAYTRADER_LOCAL_TOKEN"] = local_token
    headers = {"x-local-control": local_token}

    try:
        async def scenario():
            transport = httpx.ASGITransport(app=server.app, client=("127.0.0.1", 51100))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
                t0 = time.monotonic()

                async def slow_call():
                    r = await client.get("/api/market?ttl=0", headers=headers)
                    return time.monotonic() - t0, r.status_code

                async def fast_call():
                    # ★ 느린 요청이 먼저 이벤트 루프에 올라가도록 살짝 기다린다.
                    await asyncio.sleep(0.2)
                    r = await client.get("/api/auth/check", headers=headers)
                    return time.monotonic() - t0, r.status_code

                return await asyncio.gather(slow_call(), fast_call())

        (slow_elapsed, slow_status), (fast_elapsed, fast_status) = asyncio.run(scenario())

        check("느린 /api/market 요청 자체는 정상 완료(200)", slow_status == 200, f"status={slow_status}")
        check("동시에 보낸 /api/auth/check 는 정상 완료(200)", fast_status == 200, f"status={fast_status}")
        check(
            "★★★ /api/auth/check 가 느린 /api/market(1초 블로킹) 뒤에 밀리지 않고 빨리 끝남"
            "(이벤트 루프가 막히지 않음)",
            fast_elapsed < SLEEP_S * 0.7,
            f"fast={fast_elapsed:.2f}s slow={slow_elapsed:.2f}s (블로킹이면 fast ≈ slow 가 됨)",
        )
    finally:
        market_mod.snapshot = orig_snapshot
        if orig_local_token is None:
            os.environ.pop("DAYTRADER_LOCAL_TOKEN", None)
        else:
            os.environ["DAYTRADER_LOCAL_TOKEN"] = orig_local_token


def main() -> None:
    for t in (test_previously_blocking_routes_are_plain_def,
              test_slow_market_snapshot_does_not_block_other_requests):
        t()
    print(f"\n총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        print("실패한 검증:")
        for name in _failures:
            print(f"  - {name}")
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
