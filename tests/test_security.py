"""server.py 보안 로직 오프라인 테스트. `python tests/test_security.py` 로 실행한다.

세션 토큰 만료·서명, Host/Origin 검증, 재확인 토큰(범위 분리), 비밀번호 강도, 로컬 제어 토큰,
비밀값 마스킹을 검증한다. 환경변수로 비밀번호·세션 키를 주입하므로 실제 secrets.yaml 은 건드리지 않는다.
"""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ★ secrets.get() 은 환경변수가 파일보다 우선이다 - 파일을 읽거나 쓰지 않고 테스트한다.
os.environ["DAYTRADER_AUTH_PASSWORD"] = "482913"
os.environ["DAYTRADER_AUTH_SESSION_SECRET"] = "unit-test-session-secret"

from starlette.requests import Request  # noqa: E402

from daytrader import server as S  # noqa: E402

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


def make_request(headers=None, client=("127.0.0.1", 50000), method="GET", scheme="http", cookies=None) -> Request:
    hdrs = dict(headers or {})
    if cookies:
        hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    scope = {
        "type": "http", "method": method, "scheme": scheme, "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), str(v).encode()) for k, v in hdrs.items()], "client": client,
    }
    return Request(scope)


def test_session_token() -> None:
    print("\n== 세션 토큰: 서명·만료·비밀번호 변경 ==")
    tok = S._make_session_token()
    check("방금 발급한 토큰은 유효", S._session_token_valid(tok))
    ts, sig = tok.split(".", 1)
    check("서명을 바꾸면 무효", not S._session_token_valid(f"{ts}.{'0' * len(sig)}"))
    check("발급시각을 바꾸면 무효(서명이 시각을 포함)", not S._session_token_valid(f"{int(ts) + 5}.{sig}"))
    old_ts = int(time.time()) - S.AUTH_SESSION_MAX_AGE - 10
    check("30일이 지난 토큰은 만료", not S._session_token_valid(f"{old_ts}.{S._sign_session(old_ts)}"))
    future_ts = int(time.time()) + 3600
    check("미래 시각 토큰은 무효", not S._session_token_valid(f"{future_ts}.{S._sign_session(future_ts)}"))
    check("빈 값·형식 오류는 무효", not S._session_token_valid("") and not S._session_token_valid("abc")
          and not S._session_token_valid("x.y") and not S._session_token_valid(None))
    os.environ["DAYTRADER_AUTH_PASSWORD"] = "135790"
    try:
        check("★ 비밀번호를 바꾸면 예전 토큰이 전부 무효", not S._session_token_valid(tok))
    finally:
        os.environ["DAYTRADER_AUTH_PASSWORD"] = "482913"
    check("되돌리면 다시 유효(같은 비밀번호로 서명됨)", S._session_token_valid(tok))


def test_host_and_origin() -> None:
    print("\n== Host·Origin 검증(DNS 리바인딩·CSRF) ==")
    for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000", "ak2plus.tailfd3514.ts.net:8443",
                 "100.71.171.41:8000", "192.168.0.5", ""):
        check(f"허용: Host={host!r}", S._host_allowed(host))
    for host in ("evil.example.com", "evil.example.com:8000", "localhost.evil.com", "ts.net.evil.com"):
        check(f"거절: Host={host!r}", not S._host_allowed(host))
    os.environ["DAYTRADER_ALLOWED_HOSTS"] = "trader.home.lan"
    try:
        check("환경변수로 추가한 이름은 허용", S._host_allowed("trader.home.lan:8000"))
    finally:
        os.environ.pop("DAYTRADER_ALLOWED_HOSTS", None)
    check("추가하지 않은 이름은 거절", not S._host_allowed("trader.home.lan:8000"))

    check("GET 은 Origin 없이 통과", S._origin_ok(make_request()))
    check("POST + 같은 출처 Origin 통과",
          S._origin_ok(make_request({"origin": "http://127.0.0.1:8000"}, method="POST")))
    check("POST + tailnet Origin 통과",
          S._origin_ok(make_request({"origin": "https://ak2plus.tailfd3514.ts.net:8443"}, method="POST")))
    check("★ POST + 다른 사이트 Origin 은 거절",
          not S._origin_ok(make_request({"origin": "https://evil.example.com"}, method="POST")))
    check("POST + Origin 없고 Sec-Fetch-Site=cross-site 는 거절",
          not S._origin_ok(make_request({"sec-fetch-site": "cross-site"}, method="POST")))
    check("POST + Origin 없고 Sec-Fetch-Site=same-origin 은 통과",
          S._origin_ok(make_request({"sec-fetch-site": "same-origin"}, method="POST")))


def test_confirm_tokens() -> None:
    print("\n== 재확인 토큰: 범위 분리·만료 ==")
    trade_dep = S._require_confirm("trade")
    settings_dep = S._require_confirm("settings")

    def passes(dep, headers) -> bool:
        try:
            dep(make_request(headers, method="POST"))
            return True
        except S.ConfirmRequired:
            return False

    check("토큰 없으면 거절(trade)", not passes(trade_dep, {}))
    S._confirm_tokens["tok-trade"] = ("trade", time.time() + 60)
    S._confirm_tokens["tok-settings"] = ("settings", time.time() + 60)
    check("trade 토큰으로 trade 통과", passes(trade_dep, {"x-confirm-token": "tok-trade"}))
    check("★ trade 토큰으로 settings 작업은 거절", not passes(settings_dep, {"x-confirm-token": "tok-trade"}))
    check("settings 토큰으로 settings 통과", passes(settings_dep, {"x-confirm-token": "tok-settings"}))
    check("★ settings 토큰으로 trade 작업은 거절", not passes(trade_dep, {"x-confirm-token": "tok-settings"}))
    check("여러 토큰을 쉼표로 같이 보내도 맞는 것을 찾음", passes(trade_dep, {"x-confirm-token": "tok-settings,tok-trade"}))
    check("모르는 토큰은 거절", not passes(trade_dep, {"x-confirm-token": "guessed"}))
    S._confirm_tokens["tok-old"] = ("trade", time.time() - 1)
    check("★ 만료된 토큰은 거절", not passes(trade_dep, {"x-confirm-token": "tok-old"}))
    check("만료된 토큰은 목록에서 치워짐", "tok-old" not in S._confirm_tokens)
    err = None
    try:
        trade_dep(make_request({}, method="POST"))
    except S.ConfirmRequired as exc:
        err = exc
    check("거절 예외에 필요한 범위가 실림", err is not None and err.scope == "trade")
    for k in ("tok-trade", "tok-settings"):
        S._confirm_tokens.pop(k, None)


def test_local_control() -> None:
    print("\n== 트레이 로컬 제어 토큰 ==")
    os.environ.pop("DAYTRADER_LOCAL_TOKEN", None)
    check("토큰이 설정되지 않으면 헤더가 있어도 무효",
          not S._is_local_control(make_request({"x-local-control": "abc"})))
    os.environ["DAYTRADER_LOCAL_TOKEN"] = "run-secret-123"
    try:
        check("올바른 토큰 + loopback 은 인정", S._is_local_control(make_request({"x-local-control": "run-secret-123"})))
        check("틀린 토큰은 거절", not S._is_local_control(make_request({"x-local-control": "nope"})))
        check("토큰 없음은 거절", not S._is_local_control(make_request()))
        check("★ 올바른 토큰이라도 loopback 이 아닌 곳에서 온 요청은 거절",
              not S._is_local_control(make_request({"x-local-control": "run-secret-123"}, client=("100.64.1.2", 9))))
        check("로컬 제어는 재확인 없이 통과(트레이의 매매 중단)",
              S._require_confirm("trade")(make_request({"x-local-control": "run-secret-123"}, method="POST")) is None)
        check("인증도 통과", S._is_authenticated(make_request({"x-local-control": "run-secret-123"})))
    finally:
        os.environ.pop("DAYTRADER_LOCAL_TOKEN", None)
    check("일반 요청은 쿠키 없으면 미인증", not S._is_authenticated(make_request()))


def test_same_machine() -> None:
    print("\n== 같은 PC 판정(로그인 자동 승인·관리자 페이지 1차 관문이 공유) ==")
    check("loopback + 프록시 흔적 없음 -> 같은 PC", S._is_same_machine(make_request()))
    check("loopback 이 아니면 거절", not S._is_same_machine(make_request(client=("8.8.8.8", 9))))
    check("★ loopback 이라도 X-Forwarded-For 가 있으면(프록시를 거쳐 온 원격) 거절",
          not S._is_same_machine(make_request({"x-forwarded-for": "1.2.3.4"})))


def test_admin_local() -> None:
    print("\n== 새 기기 승인 관리자 페이지(/admin) 접근 ==")
    os.environ.pop("DAYTRADER_LOCAL_TOKEN", None)
    check("트레이 없이(토큰 없음) 개발 환경이면 loopback 만으로 허용",
          S._is_admin_local(make_request()))
    check("토큰이 없어도 loopback 이 아니면 거절",
          not S._is_admin_local(make_request(client=("8.8.8.8", 9))))
    os.environ["DAYTRADER_LOCAL_TOKEN"] = "admin-secret-456"
    try:
        check("토큰 설정 후에는 헤더 토큰 없이는 거절(loopback 이어도)", not S._is_admin_local(make_request()))
        check("올바른 토큰(헤더) + loopback 은 인정",
              S._is_admin_local(make_request({"x-admin-token": "admin-secret-456"})))
        check("틀린 토큰은 거절", not S._is_admin_local(make_request({"x-admin-token": "nope"})))
        check("올바른 토큰이라도 loopback 이 아니면 거절",
              not S._is_admin_local(make_request({"x-admin-token": "admin-secret-456"}, client=("100.64.1.2", 9))))
        check("★ 프록시를 거친 흔적(X-Forwarded-For)이 있으면 loopback 이어도 거절(Tailscale 등으로 원격이 우회 못하게)",
              not S._is_admin_local(make_request({"x-admin-token": "admin-secret-456", "x-forwarded-for": "1.2.3.4"})))
        check("쿼리 파라미터로 받은 토큰도 인정(/admin 페이지 자체는 헤더를 못 실으므로)",
              S._is_admin_local(make_request(), token="admin-secret-456"))
        check("쿼리 토큰이 틀리면 거절", not S._is_admin_local(make_request(), token="nope"))
    finally:
        os.environ.pop("DAYTRADER_LOCAL_TOKEN", None)
    good = S._make_session_token()
    check("유효한 세션 쿠키는 인증(기본 테스트 클라이언트는 loopback이라 같은 PC 취급)",
          S._is_authenticated(make_request(cookies={S.AUTH_COOKIE_NAME: good})))


def test_stale_cookie_needs_trusted_device() -> None:
    print("\n== ★ 실제로 겪은 문제: 이 기능이 생기기 전에 발급된 세션 쿠키로 신뢰 안 된 기기가 계속 드나들던 구멍 ==")
    import tempfile
    from daytrader import devices
    tmp_path = os.path.join(tempfile.mkdtemp(), "devices.json")
    orig = S._devices_path
    S._devices_path = lambda: tmp_path
    try:
        good = S._make_session_token()
        device_tok = devices.new_device_token()
        remote_req = make_request(
            {"user-agent": "OldPhone/1.0"}, client=("9.9.9.9", 1234),
            cookies={S.AUTH_COOKIE_NAME: good, S.DEVICE_COOKIE_NAME: device_tok},
        )
        check("세션 쿠키가 유효해도, 그 기기(쿠키 토큰)가 신뢰 목록에 없으면 매 요청마다 거절",
              not S._is_authenticated(remote_req))
        key = devices.key_for_token(device_tok)
        devices.DeviceStore(tmp_path).trust_directly(key, "9.9.9.9", "OldPhone/1.0")
        check("관리자가 승인(신뢰 등록)한 뒤에는 같은 쿠키로도 다시 통과", S._is_authenticated(remote_req))

        # ★ IP 가 바뀌어도(휴대폰이 흔히 그렇다) 같은 기기 쿠키면 계속 신뢰돼야 한다 - 이게 이번
        # 요청("같은 기기는 중복 승인할 필요 없다")의 핵심이다.
        same_device_new_ip = make_request(
            {"user-agent": "OldPhone/1.0"}, client=("203.0.113.5", 1),
            cookies={S.AUTH_COOKIE_NAME: good, S.DEVICE_COOKIE_NAME: device_tok},
        )
        check("★ IP 가 바뀌어도 같은 기기 쿠키면 재승인 없이 통과", S._is_authenticated(same_device_new_ip))

        # 기기 쿠키가 없거나(다른 브라우저·기기 쿠키를 지움) 다른 토큰이면 여전히 거절.
        no_device_cookie = make_request({"user-agent": "OldPhone/1.0"}, client=("9.9.9.9", 1234),
                                         cookies={S.AUTH_COOKIE_NAME: good})
        check("기기 쿠키가 아예 없으면 거절", not S._is_authenticated(no_device_cookie))
        other_device = make_request(
            {"user-agent": "Unknown/9"}, client=("8.8.4.4", 1),
            cookies={S.AUTH_COOKIE_NAME: good, S.DEVICE_COOKIE_NAME: devices.new_device_token()},
        )
        check("다른(등록 안 된) 기기 쿠키는 여전히 거절", not S._is_authenticated(other_device))
    finally:
        S._devices_path = orig


def test_password_and_lockout() -> None:
    print("\n== 비밀번호 강도·잠금 ==")
    for pw in ("123456", "111111", "000000", "654321", "012345", "121212"):
        check(f"약한 비밀번호 거절: {pw}", S._weak_password(pw))
    for pw in ("482913", "907315", "260418"):
        check(f"보통 비밀번호는 허용: {pw}", not S._weak_password(pw))
    check("상수 시간 비교가 같은 값은 True", S._same("482913", "482913"))
    check("상수 시간 비교가 다른 값은 False", not S._same("482913", "482914") and not S._same("", "482913"))

    import tempfile
    tmp_login_path = os.path.join(tempfile.mkdtemp(), "login_attempts.json")
    orig_login_path_fn = S._login_state_path
    S._login_state_path = lambda: tmp_login_path
    S._login_attempts.clear()
    S._global_login_fails.clear()
    S._global_login_locked_until = 0.0
    S._login_state_loaded = False
    try:
        key = "203.0.113.9"
        for _ in range(2):
            S._login_record_fail(key)
        S._login_check_lock(key)  # 2번 실패까지는 안 잠김
        check("2번 틀려도 아직 안 잠김", True)
        S._login_record_fail(key)
        locked = False
        try:
            S._login_check_lock(key)
        except Exception as exc:
            locked = getattr(exc, "status_code", None) == 429
        check("3번째 실패부터 429 로 잠김", locked)
        S._login_record_success(key)
        S._login_check_lock(key)
        check("성공하면 초기화", True)
    finally:
        S._login_state_path = orig_login_path_fn
        S._login_attempts.clear()
        S._global_login_fails.clear()
        S._global_login_locked_until = 0.0
        S._login_state_loaded = False

    proxied = make_request({"x-forwarded-for": "100.64.0.7, 10.0.0.1"})
    check("★ DAYTRADER_TRUSTED_PROXY 를 켜지 않으면 loopback 이어도 X-Forwarded-For 를 무시한다"
          "(안 그러면 헤더만 바꿔가며 IP 별 잠금을 우회할 수 있다)",
          S._login_client_key(proxied) == "127.0.0.1")
    os.environ["DAYTRADER_TRUSTED_PROXY"] = "1"
    try:
        check("신뢰할 수 있는 프록시라고 명시하면 loopback 프록시 뒤에서 X-Forwarded-For 원래 주소로 센다",
              S._login_client_key(proxied) == "100.64.0.7")
    finally:
        os.environ.pop("DAYTRADER_TRUSTED_PROXY", None)
    spoof = make_request({"x-forwarded-for": "1.2.3.4"}, client=("198.51.100.4", 5))
    check("★ loopback 이 아닌 곳이 보낸 X-Forwarded-For 는 믿지 않는다(주소 위조로 잠금 회피 방지)",
          S._login_client_key(spoof) == "198.51.100.4")
    S._login_attempts.clear()


def test_global_login_lockout_and_persistence() -> None:
    print("\n== ★ 로그인 실패 기록 영속화 + 전체(여러 주소 합산) 잠금 ==")
    import tempfile
    tmp_path = os.path.join(tempfile.mkdtemp(), "login_attempts.json")
    orig_path_fn = S._login_state_path
    S._login_state_path = lambda: tmp_path
    S._login_attempts.clear()
    S._global_login_fails.clear()
    S._login_state_loaded = False
    S._global_login_locked_until = 0.0
    try:
        check("실패가 적을 때는 전체 잠금 없음", S._global_login_check_lock() is None)
        for i in range(S._GLOBAL_LOGIN_FAIL_LIMIT):
            S._login_record_fail(f"203.0.113.{i}")  # 서로 다른 주소 - 개별 잠금은 안 걸림

        def is_globally_locked() -> bool:
            try:
                S._global_login_check_lock()
                return False
            except Exception as exc:
                return getattr(exc, "status_code", None) == 429

        check("★ 서로 다른 주소에서 나눠 실패해도, 전체 실패 수가 한도를 넘으면 잠김(단일 IP 잠금 우회 방지)",
              is_globally_locked())
        check("실패 기록이 파일로 저장됨", os.path.exists(tmp_path))

        # 서버 재시작을 흉내낸다: 메모리 상태를 지우고 파일에서 다시 불러온다.
        S._login_attempts.clear()
        S._global_login_fails.clear()
        S._global_login_locked_until = 0.0
        S._login_state_loaded = False
        check("★ 재시작(메모리 초기화) 직후에도 파일에서 실패 기록을 복원해 전체 잠금이 유지됨",
              is_globally_locked())
    finally:
        S._login_state_path = orig_path_fn
        S._login_attempts.clear()
        S._global_login_fails.clear()
        S._global_login_locked_until = 0.0
        S._login_state_loaded = False


def test_confirm_shares_login_lockout() -> None:
    print("\n== /api/confirm 도 /api/login 과 같은 실패 카운터·잠금을 공유 ==")
    import tempfile
    tmp_path = os.path.join(tempfile.mkdtemp(), "login_attempts.json")
    orig_path_fn = S._login_state_path
    S._login_state_path = lambda: tmp_path
    S._login_attempts.clear()
    S._global_login_fails.clear()
    S._login_state_loaded = False
    S._global_login_locked_until = 0.0
    try:
        key = "203.0.113.50"
        req = make_request(client=(key, 1))
        try:
            S._check_password_or_raise(req, "000000")
        except Exception:
            pass
        check("틀린 비밀번호 시도(로그인·확인 공용 함수)가 실패 횟수에 반영됨",
              S._login_attempts.get(key, {}).get("fails") == 1)
        for _ in range(2):
            try:
                S._check_password_or_raise(req, "000000")
            except Exception:
                pass
        check("★ 같은 카운터이므로 /api/confirm 쪽에서 틀려도 /api/login 잠금에 그대로 합산됨",
              S._login_attempts.get(key, {}).get("fails", 0) >= 3)
        locked = False
        try:
            S._login_check_lock(key)
        except Exception as exc:
            locked = getattr(exc, "status_code", None) == 429
        check("잠긴 뒤에는 login/confirm 어느 쪽으로도 더 시도할 수 없음", locked)
    finally:
        S._login_state_path = orig_path_fn
        S._login_attempts.clear()
        S._global_login_fails.clear()
        S._global_login_locked_until = 0.0
        S._login_state_loaded = False


def test_notify_test_requires_confirm_for_caller_supplied_creds() -> None:
    print("\n== ★ [2-4] /api/notify/test - 호출자가 직접 넣은 토큰·채팅ID 는 재확인이 필요 ==")
    import asyncio
    from daytrader import notify as notify_mod

    orig_post_raw = notify_mod._post_raw_sync
    notify_mod._post_raw_sync = lambda token, chat_id, text: (True, None)  # 실제 텔레그램으로 안 나가게.

    async def call(token, chat_id, extra_headers=None):
        req = make_request(extra_headers or {}, method="POST")
        body = S.NotifyTestIn(token=token, chat_id=chat_id)
        return await S.notify_test(body, req)

    try:
        blocked = False
        try:
            asyncio.run(call("attacker-token", "attacker-chat"))
        except S.ConfirmRequired as exc:
            blocked = exc.scope == "settings"
        check("★ 호출자가 새 토큰·채팅ID 를 직접 넣으면 재확인 없이는 거절됨(임의 목적지로 발송 방지)", blocked)

        S._confirm_tokens["tok-notify-test"] = ("settings", time.time() + 60)
        try:
            result = asyncio.run(call("attacker-token", "attacker-chat", {"x-confirm-token": "tok-notify-test"}))
            check("설정 재확인 토큰을 실으면 통과됨", result.get("ok") is True)
        finally:
            S._confirm_tokens.pop("tok-notify-test", None)
    finally:
        notify_mod._post_raw_sync = orig_post_raw


def test_redaction() -> None:
    print("\n== 오류·로그의 비밀값 마스킹 ==")
    tg = "https://api.telegram.org/bot8719150887:AAF9JAZVWA5F8kRGi4_X5dkx44SlTcm09X8/sendMessage"
    out = S._redact(f"HTTPSConnectionPool: Max retries exceeded with url: {tg}")
    check("텔레그램 봇 토큰(URL 안)이 가려짐", "AAF9JAZVWA5F8kRGi4" not in out and "bot***" in out, out)
    check("token=... 형태가 가려짐", "abc123secret" not in S._redact("request failed token=abc123secret retry"))
    check("Authorization Bearer 값이 가려짐", "eyJhbGciOiJIUzI1NiJ9" not in S._redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x"))
    long_key = "YzE0ZDdjNzRkM2QxYTBkM2Q5YTA1YzljYjQ5MDc5NTY1ZGMwNGQ3YmNmODZjOGM0NTJiODYxYTg5NzM0Mg"
    check("40자 넘는 긴 키가 가려짐", long_key not in S._redact(f"bad secret {long_key} rejected"))
    os.environ["TOSS_CLIENT_SECRET"] = "short-but-real"
    try:
        check("등록된 실제 키 값은 짧아도 그대로 찾아서 가림", "short-but-real" not in S._redact("bad key short-but-real!"))
    finally:
        os.environ.pop("TOSS_CLIENT_SECRET", None)
    check("일반 문장은 그대로", S._redact("종목 005930 조회 실패(404)") == "종목 005930 조회 실패(404)")
    check("로그인 비밀번호 숫자는 지우지 않음(다른 숫자를 망가뜨리지 않기 위해)", "482913" in S._redact("주문번호 482913 접수"))


def test_app_surface() -> None:
    print("\n== 앱 노출면 ==")
    check("/docs 가 꺼져 있음", S.app.docs_url is None and S.app.redoc_url is None and S.app.openapi_url is None)
    routes = {getattr(r, "path", ""): r for r in S.app.routes}
    sensitive_trade = ["/api/engine/start", "/api/engine/stop", "/api/crypto/start", "/api/crypto/stop",
                       "/api/overseas/start", "/api/overseas/stop", "/api/preflight/fix"]
    sensitive_settings = ["/api/credentials", "/api/bithumb/credentials", "/api/notify/credentials", "/api/config",
                          "/api/themes", "/api/review/apply", "/api/review/revert", "/api/performance/reset",
                          "/api/auth/password"]
    for path in sensitive_trade + sensitive_settings:
        deps = getattr(routes.get(path), "dependant", None)
        n = len(deps.dependencies) if deps is not None else 0
        check(f"재확인 필요 라우트: {path}", n >= 1)
    check("로그인·확인 경로는 인증 예외", "/api/login" in S._AUTH_EXEMPT_PATHS and "/api/confirm" not in S._AUTH_EXEMPT_PATHS)


def test_symbol_validation_and_new_routes() -> None:
    print("\n== 시세·선정 라우트: 종목 식별자 검증 / 로그인 필요 ==")
    from fastapi import HTTPException

    def ok(market, sym):
        try:
            S._check_symbol(market, sym)
            return True
        except HTTPException:
            return False

    check("정상 코인 마켓 통과", ok("crypto", "KRW-BTC"))
    check("정상 미국 티커(BRK.B) 통과", ok("overseas", "BRK.B"))
    check("정상 국내 종목코드 통과", ok("domestic", "005930"))
    check("경로 조작(../) 차단", not ok("crypto", "KRW-BTC/../x"))
    check("쿼리 주입(?x=1) 차단", not ok("overseas", "AAPL?x=1"))
    check("소문자 코인 마켓 차단", not ok("crypto", "krw-btc"))
    check("너무 긴 식별자 차단", not ok("domestic", "A" * 40))
    check("빈 값 차단", not ok("overseas", ""))
    routes = {r.path for r in S.app.routes}
    check("새 시세 라우트 등록됨", "/api/market-chart/{market}/{symbol}" in routes)
    check("미국 선정 라우트 등록됨", "/api/overseas/selection" in routes)
    src = open(S.__file__, encoding="utf-8").read()
    check("빗썸 조회 실패 메시지는 마스킹을 거침", "_redact(f\"빗썸 계좌 조회 실패" in src)


def test_idle_timeout() -> None:
    print("== 자리를 비우면 세션 만료(조작 없이 자동 갱신만으로는 연장 안 됨) ==")
    import time as _t
    orig = dict(S._idle_cache)
    try:
        S._idle_cache.update(at=_t.time(), minutes=30)
        fresh = S._make_session_token()
        check("방금 발급한 세션은 유효", S._session_token_valid(fresh))
        ts = int(_t.time()) - 31 * 60
        old = f"{ts}.{S._sign_session(ts)}"
        check("30분 넘게 조작이 없던 세션은 무효", not S._session_token_valid(old))
        ts = int(_t.time()) - 29 * 60
        ok = f"{ts}.{S._sign_session(ts)}"
        check("29분 전 조작한 세션은 아직 유효", S._session_token_valid(ok))
        S._idle_cache.update(at=_t.time(), minutes=0)
        ts = int(_t.time()) - 5 * 24 * 3600
        long_ago = f"{ts}.{S._sign_session(ts)}"
        check("타임아웃 0(끔)이면 예전처럼 30일까지 유효", S._session_token_valid(long_ago))
        check("서명이 틀린 토큰은 나이와 무관하게 무효", S._session_token_age(f"{int(_t.time())}.deadbeef") is None)
    finally:
        S._idle_cache.update(orig)


def test_config_path_traversal_guard() -> None:
    print("\n== ★ config.yaml 의 state_dir/log_dir/themes_file 경로 탈출 방지 ==")
    from daytrader import config as C
    base = os.path.join(os.sep, "app", "daytrader")

    def rejects(value) -> bool:
        try:
            C._safe_user_path(base, value, "state", "state_dir")
            return False
        except ValueError:
            return True

    check("절대경로(POSIX)는 거절", rejects("/etc/passwd"))
    check("윈도우 드라이브 절대경로는 거절", rejects("C:/Windows/System32"))
    check("UNC 경로(\\\\server\\share)는 거절", rejects("//server/share"))
    check("상위 폴더 탈출(..)은 거절", rejects("../../etc"))
    check("중간에 ..가 섞여도 거절", rejects("a/../../b"))
    check("빈 값은 거절", rejects(""))
    check("보통 상대경로는 허용", not rejects("state"))
    check("하위 폴더가 있는 상대경로도 허용", not rejects("data/state"))
    resolved = C._safe_user_path(base, "state", "state", "state_dir")
    check("허용된 값은 base 밑으로 정규화되어 반환됨",
          resolved == os.path.normpath(os.path.join(base, "state")))

    # ★ 실제 config.yaml 을 복사해 state_dir 을 조작한 뒤 load_config() 전체 경로로도 막히는지 확인.
    import tempfile
    import yaml as _yaml
    with open(C.app_path("config.yaml"), "r", encoding="utf-8") as f:
        raw_cfg = _yaml.safe_load(f)
    tmp_dir = tempfile.mkdtemp()
    for bad_value, label in (("../../etc/evil-state", "state_dir"), ("/tmp/evil-log", "log_dir")):
        raw_cfg2 = dict(raw_cfg)
        raw_cfg2[label] = bad_value
        tmp_cfg = os.path.join(tmp_dir, f"bad-{label}.yaml")
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            _yaml.safe_dump(raw_cfg2, f, allow_unicode=True)
        blocked = False
        try:
            C.load_config(tmp_cfg)
        except ValueError:
            blocked = True
        check(f"★ config.yaml 의 {label} 에 경로 탈출 값을 넣으면 load_config() 가 거절함: {bad_value!r}", blocked)


def test_config_post_rejects_unknown_keys() -> None:
    print("\n== ★ POST /api/config 는 알려진 최상위 항목만 받음 ==")
    import asyncio
    from daytrader.config import KNOWN_TOP_LEVEL_KEYS

    async def call(body):
        return await S.post_config(body)

    rejected = False
    try:
        asyncio.run(call({"state_dir": "state", "not_a_real_setting": {"x": 1}}))
    except S.HTTPException as exc:
        rejected = exc.status_code == 400
    check("★ 알 수 없는 최상위 키가 섞여 있으면 400 으로 거절", rejected)
    check("state_dir·mode 같은 알려진 키는 목록에 있음",
          {"state_dir", "log_dir", "themes_file", "mode"} <= KNOWN_TOP_LEVEL_KEYS)


def test_first_run_default_password() -> None:
    print("\n== ★ 최초 실행 시 임시 비밀번호 자동 생성 + 원격 로그인 차단 ==")
    import re
    import tempfile
    from daytrader import secrets as sec
    tmp_path = os.path.join(tempfile.mkdtemp(), "secrets.yaml")
    orig_path = sec.SECRETS_PATH
    orig_env_pw = os.environ.pop("DAYTRADER_AUTH_PASSWORD", None)
    orig_env_flag = os.environ.pop("DAYTRADER_AUTH_PASSWORD_IS_DEFAULT", None)
    sec.SECRETS_PATH = tmp_path
    try:
        check("secrets.yaml 이 비어 있으면 아직 비밀번호 없음", sec.get("auth_password") == "")
        pw = S._auth_password()
        check("무작위 숫자 6자리가 생성됨", bool(re.fullmatch(r"\d{6}", pw)))
        check("생성된 비밀번호가 secrets.yaml 에 저장됨", sec.get("auth_password") == pw)
        check("생성 직후엔 '임시 비밀번호' 상태로 표시됨", S._is_default_password())
        check("다시 불러도 같은 값을 씀(재생성 안 됨)", S._auth_password() == pw)

        local_req = make_request()
        remote_req = make_request(client=("8.8.8.8", 1234))
        check("★ 임시 비밀번호인 동안 원격 요청은 이 컴퓨터가 아님",
              S._is_default_password() and not S._is_same_machine(remote_req))
        check("이 컴퓨터에서 온 요청은 통과 조건을 만족", S._is_default_password() and S._is_same_machine(local_req))

        # 비밀번호를 바꾸면(change_password 가 하는 일과 동일) 더는 "임시 비밀번호"가 아니다.
        sec.save({"auth_password": "482913", "auth_password_is_default": ""})
        check("비밀번호를 바꾸면 임시 비밀번호 표시가 사라짐", not S._is_default_password())
        check("★ 표시가 사라지면 원격 요청도 더는 차단 조건에 걸리지 않음",
              not (S._is_default_password() and not S._is_same_machine(remote_req)))
    finally:
        sec.SECRETS_PATH = orig_path
        if orig_env_pw is not None:
            os.environ["DAYTRADER_AUTH_PASSWORD"] = orig_env_pw
        else:
            os.environ.pop("DAYTRADER_AUTH_PASSWORD", None)
        if orig_env_flag is not None:
            os.environ["DAYTRADER_AUTH_PASSWORD_IS_DEFAULT"] = orig_env_flag
        else:
            os.environ.pop("DAYTRADER_AUTH_PASSWORD_IS_DEFAULT", None)


def main() -> None:
    for t in (test_session_token, test_host_and_origin, test_confirm_tokens, test_local_control,
              test_same_machine, test_admin_local, test_stale_cookie_needs_trusted_device,
              test_password_and_lockout, test_redaction, test_app_surface, test_symbol_validation_and_new_routes,
              test_idle_timeout, test_config_path_traversal_guard, test_config_post_rejects_unknown_keys,
              test_first_run_default_password, test_global_login_lockout_and_persistence,
              test_confirm_shares_login_lockout, test_notify_test_requires_confirm_for_caller_supplied_creds):
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
