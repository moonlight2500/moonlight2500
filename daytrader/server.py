from __future__ import annotations

import asyncio
import inspect
import json
import yaml
import logging
import os
import sys
import re
import shutil
import threading
import time
from dataclasses import asdict
from functools import wraps
from logging.handlers import RotatingFileHandler

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.scalarfloat import ScalarFloat
from ruamel.yaml.scalarint import ScalarInt
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from daytrader.config import KNOWN_TOP_LEVEL_KEYS, load_config
from daytrader.paths import app_dir, app_path, ensure_user_files, web_dir
from daytrader.playbook import Playbook
from daytrader.runner import EngineRunner, EventBus, LogBuffer
from daytrader.ticks import breakeven_pct
from daytrader.tossapi import TossApiError

# ★ ROOT/CONFIG_PATH/ENV_PATH 는 paths.app_dir()/app_path() 를 쓴다.
# WEB 은 paths.web_dir() (묶여 들어간 정적 파일).
# exe 로 묶으면 __file__ 이 임시 폴더를 가리켜 설정과 기록이 날아간다.
ROOT = app_dir()
CONFIG_PATH = app_path("config.yaml")
ENV_PATH = app_path(".env")
WEB_DIR = web_dir()

bus = EventBus()
log_buffer = LogBuffer(bus=bus)
runner = EngineRunner(log_buffer, bus=bus)
_lab = None


def get_lab():
    global _lab
    if _lab is None:
        from daytrader.lab import Lab
        _lab = Lab(cfg_now().state_dir)
    return _lab

_client_cache = None
# ★ 엔진이 꺼진 상태에서 종목선정 화면이 반복 호출하는 것을 막는 짧은 캐시.
#   (랭킹 API 한도 초당 3회를 넘기지 않기 위한 것)
_selection_cache = None

# ★★★ "매매일지에 매매한 이력 외에 종목선정 같은 항목은 보이지 않도록" -
# journal 파일에는 여러 종류가 함께 쌓인다:
#   buy/sell     - 실제 매매 (이것만 매매일지에 보여준다)
#   reject       - 종목선정에서 걸러진 종목 (선정 화면의 몫)
#   session      - 세션 전환·사전점검·마감 요약
#   halt         - 매매 중단 사유·진입 보류
#   reconcile    - 계좌 대조 결과
# 뒤의 넷은 진단·감사에 필요해서 기록은 계속 남기되, "매매일지" 화면에는
# 안 보이게 한다 - 실제 매매가 그 사이에 파묻히기 때문이다.
TRADE_JOURNAL_KINDS = ("buy", "sell")

# ★ /docs·/redoc·/openapi.json 은 로그인 없이 API 목록 전체를 보여 주므로 끈다.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def setup_logging() -> None:
    """RotatingFileHandler(10MB × 5, 장애 진단용 회전 텍스트 로그) + WARNING 이상을
    SQLite(daytrader.db 의 app_log 표)에도 남기는 핸들러(daytrader/applog.py) - 화면의
    최근 2000건(LogBuffer)보다 오래된 경고·오류를 나중에도 페이지 단위로 다시 찾을 수 있게 한다."""
    log_dir = app_path("logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "daytrader.log"), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    file_handler.addFilter(_RedactFilter())
    log_buffer.addFilter(_RedactFilter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(log_buffer)  # 화면(웹)에서도 최근 로그를 볼 수 있게 같이 붙인다.

    try:
        from daytrader import applog
        db_handler = applog.attach(cfg_now().state_dir)
        if db_handler is not None:
            db_handler.addFilter(_RedactFilter())
    except Exception:
        logging.getLogger(__name__).warning("app_log DB 핸들러를 켜지 못했습니다 - 회전 텍스트 로그는 정상 동작합니다.")


_secrets_migrated = False


def _migrate_secrets_once() -> None:
    """★★★ 예전 위치(.env, config.yaml)의 키를 secrets.yaml 로 옮긴다.
    이미 쓰던 사람이 새 버전으로 바꿨다는 이유만으로 키를 다시 입력하게
    만들면 안 된다 - 그건 고장이다. 프로세스당 한 번만 한다.
    """
    global _secrets_migrated
    if _secrets_migrated:
        return
    _secrets_migrated = True
    try:
        from daytrader import secrets
        raw = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        secrets.migrate_from_legacy(ENV_PATH, raw if isinstance(raw, dict) else {})
        secrets.ensure_file()
        secrets.migrate_plaintext_to_encrypted()
    except Exception as exc:
        logging.getLogger(__name__).warning("API 키 이전 중 문제가 있었습니다: %s", exc)


def cfg_now():
    """매 요청마다 다시 읽는다 - 설정을 고치면 즉시 반영되게 하기 위해서다."""
    _migrate_secrets_once()
    return load_config(CONFIG_PATH)


def get_client(force_new: bool = False):
    """★ 엔진이 돌고 있으면 엔진의 클라이언트를 재사용한다 - 화면과 엔진이
    다른 시장을 보면 화면을 믿을 수 없다.
    sim/replay 는 각자의 클라이언트를 새로 만들고, 그 밖은 QuoteRouter
    (토스 우선, 인터넷 폴백)를 쓴다.
    """
    global _client_cache

    if not force_new and runner.engine is not None:
        return runner.engine.client

    cfg = cfg_now()

    if cfg.mode in ("sim", "replay"):
        from daytrader.clock import make_clock
        from daytrader.simulator import SimClient
        return SimClient(cfg, clock=make_clock(cfg))

    if not force_new and _client_cache is not None:
        return _client_cache

    from daytrader.router import build_router
    _client_cache = build_router(cfg)
    return _client_cache


def api_guard(fn):
    """★★ functools.wraps 필수. 없으면 FastAPI 가 원래 시그니처를 못 읽어
    모든 엔드포인트가 *a, **kw 를 쿼리 파라미터로 요구하게 된다.
    """
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def async_wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except HTTPException:
                raise
            except ConfirmRequired:
                # ★ [2-4] 라우트 dependencies=[...] 가 아니라 함수 본문에서 조건부로
                # _require_confirm(...)(request) 를 직접 부르는 라우트(예: 호출자가 새
                # 값을 넣었을 때만 재확인을 요구하는 /api/notify/test)가 있다 - 그대로
                # 삼켜 500 으로 바꾸면 안 되고, 원래의 403 재확인 응답이 나가야 한다.
                raise
            except TossApiError as exc:
                raise HTTPException(status_code=502, detail=_redact(exc))
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=_redact(exc))
            except Exception as exc:
                raise HTTPException(status_code=500, detail=_redact(exc))
        return async_wrapper

    @wraps(fn)
    def sync_wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except HTTPException:
            raise
        except ConfirmRequired:
            raise
        except TossApiError as exc:
            raise HTTPException(status_code=502, detail=_redact(exc))
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=_redact(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_redact(exc))
    return sync_wrapper


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    # 화면이 항상 같은 모양의 오류를 받게 한다.
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


# ━━ 로그인 · 보안 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★★★ Tailscale 등으로 외부에서 접속할 수 있게 되면서 추가한 안전장치. 이 서버는 인증이 없으면
# 누구든 계좌를 조작할 수 있다(주문·설정·자동매매 시작/정지 전부). 계정 여러 개나 아이디는
# 필요 없어서 - 이 프로그램을 쓰는 사람은 이 컴퓨터의 주인 한 명뿐이다 - 숫자 6자리
# 비밀번호 하나로 잠근다. 그 위에 아래 방어를 겹쳐 둔다:
#   ① 세션 쿠키에 만료(30일)와 서명을 넣고, HTTPS 로 들어오면 Secure, 항상 HttpOnly·SameSite=Strict.
#   ② Host 헤더 검증(DNS 리바인딩 방어) + 쓰기 요청의 Origin 검증(CSRF 방어).
#   ③ 보안 헤더(CSP 포함) - 화면에 어떤 문자열이 섞여 들어와도 스크립트가 실행되지 않게 한다.
#   ④ 민감한 작업(자동매매 시작·정지, 키·설정·비밀번호 변경)은 로그인 세션만으로는 부족하고
#      "방금 비밀번호를 다시 입력했다"는 짧은 수명 토큰이 있어야 서버가 받아 준다
#      (예전엔 화면에서만 재확인해서, 세션 쿠키만 있으면 API 를 직접 불러 우회할 수 있었다).
#   ⑤ 오류 응답·로그에서 API 키·토큰을 가린다.
AUTH_COOKIE_NAME = "daytrader_session"
DEFAULT_AUTH_PASSWORD = "123456"
AUTH_SESSION_MAX_AGE = 30 * 24 * 3600  # 30일(예전 365일 - 쿠키가 새어도 영원히 통하지 않게)
# ★★★ 실제로 겪은 문제 - 새 기기 승인을 IP+User-Agent 로 식별했더니, 휴대폰은 와이파이↔데이터
# 전환 등으로 접속할 때마다 IP 가 자주 바뀌어 같은 휴대폰인데도 매번 "새 기기"로 보여 계속 승인해야
# 했다. 그래서 IP 와 무관하게 오래 남는 쿠키(daytrader_device)의 무작위 토큰으로 식별한다 - 브라우저가
# 쿠키를 지우지 않는 한 IP 가 바뀌어도 같은 기기로 인식된다.
DEVICE_COOKIE_NAME = "daytrader_device"
DEVICE_COOKIE_MAX_AGE = 400 * 24 * 3600  # ~400일(주요 브라우저의 쿠키 최대 수명 관행에 맞춤)
# ★ 로그인 자체와, 로그인 여부만 확인하는 엔드포인트는 당연히 잠기지 않아야 한다.
_AUTH_EXEMPT_PATHS = {"/api/login", "/api/auth/check"}
# ★ 새 기기 승인 대기 폴링과 관리자 API 는 비밀번호 세션이 아니라 각자 다른 문(대기 토큰 / 로컬 접속)으로
#   지킨다 - 세션 쿠키가 없는 시점(로그인 전)에도, 또는 애초에 비밀번호 문이 아닌 화면에서 불러야 한다.
_AUTH_EXEMPT_PREFIXES = ("/api/login/pending/", "/api/admin/")
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _generate_default_password() -> str:
    """★★★ 실제로 겪은 문제 - 예전엔 secrets.yaml 에 비밀번호가 없으면 하드코딩된
    "123456"으로 그냥 로그인이 됐다(새로 설치하면 누구나 아는 비밀번호로 열려 있는 셈).
    이제는 최초 실행 때 무작위 6자리를 지어 저장하고, 콘솔·로그에 한 번 보여 준다.
    이 임시 비밀번호로 바꾸기 전까지는 원격에서 로그인을 아예 막는다(아래 login() 참고)."""
    import secrets as pysecrets
    from daytrader import secrets
    pw = f"{pysecrets.randbelow(1_000_000):06d}"
    secrets.save({"auth_password": pw, "auth_password_is_default": "1"})
    banner = (
        f"\n{'=' * 60}\n"
        f"  최초 실행: 임시 로그인 비밀번호는 {pw} 입니다.\n"
        f"  이 비밀번호는 이 컴퓨터에서만 로그인할 수 있습니다.\n"
        f"  대시보드에 접속해 반드시 새 비밀번호로 바꿔 주세요.\n"
        f"{'=' * 60}\n"
    )
    print(banner)
    logging.getLogger(__name__).warning("최초 실행 - 임시 로그인 비밀번호: %s (이 컴퓨터에서만 로그인 가능, 곧 변경 필요)", pw)
    return pw


def _auth_password() -> str:
    from daytrader import secrets
    return secrets.get("auth_password") or _generate_default_password()


def _is_default_password() -> bool:
    """★ 사용자가 아직 최초 생성된 임시 비밀번호를 그대로 쓰고 있는가.
    (더는 하드코딩된 "123456"과 비교하지 않는다 - 이제 기본값도 설치마다 무작위다.)"""
    from daytrader import secrets
    return bool(secrets.get("auth_password_is_default"))


def _auth_session_secret() -> str:
    """★ 쿠키 서명에 쓰는 비밀키 - 처음 로그인할 때 한 번만 만들어 저장해 둔다.
    매번 새로 만들면 서버를 재시작할 때마다 모든 세션이 끊긴다."""
    from daytrader import secrets
    value = secrets.get("auth_session_secret")
    if not value:
        value = os.urandom(32).hex()
        secrets.save({"auth_session_secret": value})
    return value


def _same(a: str, b: str) -> bool:
    """상수 시간 비교 - 비밀번호·토큰을 ==/!= 로 비교하면 응답 시간 차이로 한 글자씩 추측할 수 있다."""
    import hmac as hmac_mod
    return hmac_mod.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _sign_session(ts: int) -> str:
    import hashlib
    import hmac as hmac_mod
    msg = f"{ts}:{_auth_password()}".encode()
    return hmac_mod.new(_auth_session_secret().encode(), msg, hashlib.sha256).hexdigest()


def _make_session_token() -> str:
    """★★ 세션 쿠키 = "발급시각.서명". 서명에 현재 비밀번호가 들어가서 비밀번호를 바꾸면 예전에 발급된
    쿠키가 전부 무효가 되고(세션 목록 없이도 "바꾸면 다른 곳은 로그아웃"이 보장된다), 발급시각이
    들어 있어 오래된 쿠키는 스스로 만료된다."""
    ts = int(time.time())
    return f"{ts}.{_sign_session(ts)}"


_idle_cache: dict = {"at": 0.0, "minutes": 30}


def _session_idle_seconds() -> int:
    """★ 화면을 안 만진 채 이만큼 지나면 세션이 끝난다(설정 ui.session_idle_minutes, 0=사용 안 함).
    요청마다 설정 파일을 읽지 않도록 10초 캐시한다."""
    now = time.time()
    if now - _idle_cache["at"] > 10:
        try:
            _idle_cache["minutes"] = int(getattr(cfg_now().ui, "session_idle_minutes", 30) or 0)
        except Exception:
            _idle_cache["minutes"] = 30
        _idle_cache["at"] = now
    return max(0, int(_idle_cache["minutes"])) * 60


def _session_token_ts(token: str | None) -> int | None:
    """서명이 유효하면 발급시각(정수, 초 단위 유닉스 타임)을 돌려주고, 아니면 None."""
    if not token or "." not in token:
        return None
    ts_s, sig = token.split(".", 1)
    try:
        ts = int(ts_s)
    except ValueError:
        return None
    if not _same(sig, _sign_session(ts)):
        return None
    return ts


def _session_token_age(token: str | None):
    """유효한 서명이면 발급(=마지막 사용자 조작) 후 지난 초, 아니면 None."""
    ts = _session_token_ts(token)
    if ts is None:
        return None
    return time.time() - ts


def _session_token_valid(token: str | None) -> bool:
    age = _session_token_age(token)
    if age is None:
        return False
    idle = _session_idle_seconds()
    limit = min(AUTH_SESSION_MAX_AGE, idle) if idle > 0 else AUTH_SESSION_MAX_AGE
    return -60 <= age <= limit


def _is_local_control(request: Request) -> bool:
    """★ 트레이(같은 PC 의 부모 프로세스)가 상태를 읽고 "매매 중단"을 보낼 때 쓰는 통로 -
    트레이가 서버를 띄우면서 실행마다 새로 만든 비밀 토큰을 환경변수로 넘겨 주고, 그 토큰과
    같은 PC(loopback)에서 온 요청만 인정한다. 로그인 벽을 넣은 뒤 트레이의 상태 갱신과 매매
    중단 메뉴가 401 로 막혀 있었다(비상 정지 경로라 반드시 살아 있어야 한다)."""
    token = os.environ.get("DAYTRADER_LOCAL_TOKEN", "")
    given = request.headers.get("x-local-control", "")
    host = request.client.host if request.client else ""
    return bool(token and given) and host in _LOOPBACK and _same(given, token)


def _is_same_machine(request: Request) -> bool:
    """★ 이 서버가 도는 PC에서 직접 온 요청인가(순수 네트워크 위치만 본다 - 토큰은 안 본다).
    Tailscale serve 같은 프록시를 거치면 원격 요청도 서버 입장에서는 127.0.0.1 로 보인다
    (_login_client_key 의 사정과 같다) - 그래서 프록시를 거친 흔적(X-Forwarded-For)이 있으면
    겉보기엔 loopback 이어도 원격으로 본다. 로그인 화면의 "새 기기 자동 승인"과 /admin 페이지의
    1차 관문(네트워크 위치)이 이 하나를 같이 쓴다."""
    host = request.client.host if request.client else ""
    return host in _LOOPBACK and not request.headers.get("x-forwarded-for")


def _admin_token() -> str:
    """트레이가 서버를 띄울 때마다 새로 만들어 넘기는 비밀 토큰(DAYTRADER_LOCAL_TOKEN) - _is_local_control
    과 같은 값을 재사용한다. 트레이 없이(개발용으로 직접) 띄웠으면 비어 있다."""
    return os.environ.get("DAYTRADER_LOCAL_TOKEN", "")


# ★★ [2-8] 실제로 겪을 수 있는 문제 - 트레이가 /admin 을 열 때 비밀 토큰을 ?token= 으로 URL 에
# 그대로 실어 보낸다. 그 URL 은 브라우저 방문기록·세션 복원 등에 평문으로 오래 남는다(쿠키와
# 달리 만료가 없다). 그래서 URL 의 토큰은 "한 번 쓰고 버리는" 부트스트랩으로만 쓰고, 확인되는
# 즉시 짧게 사는 HttpOnly 쿠키로 바꿔치기한 뒤 토큰이 없는 URL(/admin)로 리다이렉트한다 -
# 그 뒤로는 주소창·방문기록 어디에도 토큰이 남지 않는다.
ADMIN_BOOT_COOKIE_NAME = "daytrader_admin_boot"
ADMIN_BOOT_COOKIE_MAX_AGE = 600  # 10분 - 이 페이지를 열어 둔 채 오래 방치하면 다시 트레이로 열어야 한다.


def _sign_admin_boot(ts: int) -> str:
    import hashlib
    import hmac as hmac_mod
    msg = f"admin-boot:{ts}:{_admin_token()}".encode()
    return hmac_mod.new(_auth_session_secret().encode(), msg, hashlib.sha256).hexdigest()


def _admin_boot_cookie_valid(request: Request) -> bool:
    value = request.cookies.get(ADMIN_BOOT_COOKIE_NAME, "")
    if not value or "." not in value:
        return False
    ts_s, sig = value.split(".", 1)
    try:
        ts = int(ts_s)
    except ValueError:
        return False
    if not _same(sig, _sign_admin_boot(ts)):
        return False
    return -60 <= (time.time() - ts) <= ADMIN_BOOT_COOKIE_MAX_AGE


def _is_admin_local(request: Request, *, token: str | None = None) -> bool:
    """★ 새 기기 승인 관리자 페이지(/admin) - 이중으로 지킨다.
    1) 네트워크: _is_same_machine() - 이 서버가 도는 PC에서 연 브라우저만.
    2) 토큰: 1)만으로는 "같은 PC 안의 다른 어떤 프로그램(악성 코드 포함)"까지 다 허용하는 셈이라,
       트레이가 실행마다 새로 만드는 비밀 토큰까지 같이 요구한다(트레이 메뉴로 열 때만 URL 에 실려
       온다) - _is_local_control 과 같은 토큰을 그대로 쓴다. 트레이 없이 개발용으로 직접 띄운
       경우(토큰이 아예 없음)는 1)만으로 허용한다.
    토큰이 URL·헤더로 안 왔어도, 위에서 URL 토큰을 한 번 확인하고 심어 둔 부트스트랩 쿠키가
    아직 유효하면 그것으로도 통과한다(admin_page 가 리다이렉트한 뒤의 /admin 재요청이 이 경로다).
    """
    if not _is_same_machine(request):
        return False
    expected = _admin_token()
    if not expected:
        return True
    given = token if token is not None else request.headers.get("x-admin-token", "")
    if given and _same(given, expected):
        return True
    return _admin_boot_cookie_valid(request)


def _devices_path() -> str:
    return os.path.join(cfg_now().state_dir, "devices.json")


# ★★★ [2-6] 실제로 겪을 수 있는 구멍 - /api/logout 은 예전엔 쿠키만 지웠다. 세션 쿠키 자체는
# HMAC(발급시각:비밀번호) 로 서명된 "무상태" 토큰이라, 로그아웃해도 그 문자열 자체는 계속 유효한
# 서명으로 남는다 - 누군가 그 쿠키 값을 미리 빼내 뒀다면(기기 도난·백업 등) 로그아웃 후에도
# 그 값으로 계속 들어올 수 있었다. 그래서 "이 기기(쿠키)로 발급된 세션은 이 시각 이전이면
# 더 이상 인정하지 않는다"는 기준시각을 기기별로, 그리고 전체 기기 공통으로 저장해 둔다.
# 비밀번호를 바꾸면(서명 자체가 바뀌어) 이미 모든 기기가 로그아웃되는 것과 같은 효과를 내지만,
# 여기서는 비밀번호를 바꾸지 않고도 "이 기기만" 또는 "전체 기기" 로그아웃을 가능하게 한다.
_session_logout_lock = threading.Lock()


def _session_logout_path() -> str:
    return os.path.join(cfg_now().state_dir, "session_logout.json")


def _load_session_logout_state() -> dict:
    try:
        with open(_session_logout_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_session_logout_state(data: dict) -> None:
    try:
        path = _session_logout_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _device_key_of(request: Request) -> str:
    from daytrader import devices
    token = request.cookies.get(DEVICE_COOKIE_NAME, "")
    return devices.key_for_token(token) if token else ""


def _record_logout(request: Request) -> None:
    """이 기기(쿠키)로 발급된, 지금까지의 모든 세션을 무효화한다."""
    key = _device_key_of(request)
    if not key:
        return
    with _session_logout_lock:
        data = _load_session_logout_state()
        now = time.time()
        per_device = {
            k: v for k, v in (data.get("devices") or {}).items()
            if isinstance(v, (int, float)) and now - v < AUTH_SESSION_MAX_AGE
        }
        per_device[key] = now
        data["devices"] = per_device
        _save_session_logout_state(data)


def _record_logout_all_devices() -> None:
    """모든 기기의 세션을 한 번에 무효화한다("로그아웃 - 모든 기기")."""
    with _session_logout_lock:
        data = _load_session_logout_state()
        data["all_at"] = time.time()
        data["devices"] = {}  # 전체 기준시각 하나가 개별 기록을 다 덮으니 정리해 둔다.
        _save_session_logout_state(data)


def _session_revoked_for(request: Request, issued_at: int) -> bool:
    """서명은 유효한 세션 토큰이라도, 그 발급시각 이후에(이 기기 또는 전체 기기) 로그아웃 기록이
    있으면 더는 인정하지 않는다."""
    with _session_logout_lock:
        data = _load_session_logout_state()
    all_at = data.get("all_at")
    if isinstance(all_at, (int, float)) and issued_at <= all_at:
        return True
    key = _device_key_of(request)
    if not key:
        return False
    device_at = (data.get("devices") or {}).get(key)
    return isinstance(device_at, (int, float)) and issued_at <= device_at


def _is_device_trusted(request: Request) -> bool:
    """★★★ 실제로 겪은 구멍 - 세션 쿠키가 유효하다고 해서 그 요청을 보낸 기기가 지금도
    신뢰 목록에 있는지는 따로 확인하지 않았다. 그래서 이 기능이 생기기 "전에" 로그인해 둔 쿠키를
    들고 있는 기기(예: 휴대폰)는 /api/login 을 다시 안 거치니 새 기기 승인 절차를 아예 타지 않고
    계속 드나들 수 있었다(관리자 페이지 대기 목록에도 안 뜸). 그래서 세션 쿠키가 있어도 매 요청마다
    지금 이 기기가 신뢰 목록에 있는지(또는 이 서버가 도는 PC에서 온 요청인지) 다시 확인한다.
    기기 식별은 daytrader_device 쿠키 토큰으로 한다(IP 로 하면 휴대폰이 통신망을 바꿀 때마다
    "새 기기"로 보여 매번 재승인해야 했다) - 그 쿠키가 없는 요청은 신뢰할 수 없다."""
    if _is_same_machine(request):
        return True
    from daytrader import devices
    token = request.cookies.get(DEVICE_COOKIE_NAME, "")
    if not token:
        return False
    key = devices.key_for_token(token)
    return devices.DeviceStore(_devices_path()).is_trusted(key)


def _is_authenticated(request: Request) -> bool:
    if _is_local_control(request):
        return True
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not _session_token_valid(token):
        return False
    issued_at = _session_token_ts(token)
    if issued_at is not None and _session_revoked_for(request, issued_at):
        return False  # ★ [2-6] 로그아웃(이 기기 또는 전체 기기) 이후 발급된 적 없는 오래된 토큰.
    return _is_device_trusted(request)


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def _set_session_cookie(resp: Response, request: Request) -> None:
    resp.set_cookie(
        AUTH_COOKIE_NAME, _make_session_token(), max_age=AUTH_SESSION_MAX_AGE,
        httponly=True, samesite="strict", secure=_is_https(request), path="/",
    )


def _device_token(request: Request) -> str:
    """기기 쿠키에서 토큰을 읽는다. 없으면(첫 방문) 새로 만든다 - 응답에 심는 건 호출부 몫이다."""
    from daytrader import devices
    token = request.cookies.get(DEVICE_COOKIE_NAME, "")
    return token if len(token) >= 20 else devices.new_device_token()


def _set_device_cookie(resp: Response, request: Request, token: str) -> None:
    """볼 때마다 만료를 늘려 준다(계속 쓰는 기기가 400일 뒤 갑자기 잘리지 않게)."""
    resp.set_cookie(
        DEVICE_COOKIE_NAME, token, max_age=DEVICE_COOKIE_MAX_AGE,
        httponly=True, samesite="strict", secure=_is_https(request), path="/",
    )


# ── ② Host · Origin 검증 ──
def _split_netloc(netloc: str) -> tuple[str, str | None]:
    """호스트[:포트] 를 (호스트, 포트-문자열-또는-None) 으로 나눈다. IPv6 대괄호([::1]:8000) 도 다룬다."""
    n = (netloc or "").strip().lower()
    if n.startswith("["):
        if "]" in n:
            end = n.index("]")
            host = n[1:end]
            rest = n[end + 1:]
            return host, (rest[1:] if rest.startswith(":") else None)
        return n, None
    if n.count(":") == 1:
        host, port = n.rsplit(":", 1)
        return host, port
    return n, None


def _host_only(netloc: str) -> str:
    return _split_netloc(netloc)[0]


def _default_port(scheme: str) -> str:
    return "443" if scheme == "https" else "80"


def _host_allowed(netloc: str) -> bool:
    """이 서버는 localhost·IP 주소·Tailscale 이름(*.ts.net)으로만 부른다. 그 밖의 이름(공격자
    도메인이 내 PC 를 가리키게 만드는 DNS 리바인딩)은 거절한다. 다른 이름을 쓰려면 환경변수
    DAYTRADER_ALLOWED_HOSTS 에 쉼표로 적는다."""
    if not netloc:
        return True
    h = _host_only(netloc)
    if h in _LOOPBACK or h.endswith(".ts.net"):
        return True
    try:
        import ipaddress
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    extra = {x.strip().lower() for x in os.environ.get("DAYTRADER_ALLOWED_HOSTS", "").split(",") if x.strip()}
    return h in extra


def _origin_ok(request: Request) -> bool:
    """CSRF 방어 - 상태를 바꾸는 요청(POST 등)이 "이 서버 자신"에서 나왔는지 확인한다.
    ★★★ 실제로 겪을 수 있는 구멍 두 가지를 여기서 고친다.
      ① 예전엔 Origin 의 호스트 이름만 보고(_host_allowed - "localhost·IP·Tailscale 이름인가")
         허용했다. 그러면 Origin: http://localhost:9999(전혀 다른 포트, 즉 전혀 다른 웹앱)도
         "localhost"라는 이유로 통과한다 - 브라우저의 동일 출처 정책은 포트까지 같아야 같은
         출처로 보는데, 이 검증은 포트를 아예 무시했다. 그래서 이제 Origin 의 스킴·호스트·포트를
         이 요청이 실제로 도착한 Host 헤더(+ 프록시 뒤라면 X-Forwarded-Proto)와 정확히 비교한다.
      ② Origin 도 Sec-Fetch-Site 도 없는 요청을 "판단할 근거가 없으니 통과"로 취급했다 - 오래된
         모든 브라우저가 아니라, 브라우저를 거치지 않고 임의로 만든 요청(예: 자동화 스크립트)도
         이 상태와 똑같이 보인다. GET/HEAD 는 원래 안전한 메서드라 그대로 통과시키되, 상태를
         바꾸는 요청에서 둘 다 없으면 이제 거절한다.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    if _is_local_control(request):
        # ★ 트레이가 "매매 중단" 등을 보낼 때 쓰는 urllib 요청은 브라우저가 아니라서
        # Origin·Sec-Fetch-Site 를 아예 안 보낸다 - 이미 loopback + 실행마다 새로 만드는
        # 비밀 토큰(X-Local-Control)으로 지키고 있으니 이 검증까지 요구하지 않는다.
        return True
    origin = request.headers.get("origin")
    fetch_site = request.headers.get("sec-fetch-site")
    if origin:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        if parsed.scheme.lower() != ("https" if _is_https(request) else "http"):
            return False
        origin_host, origin_port = _split_netloc(parsed.netloc)
        request_host, request_port = _split_netloc(request.headers.get("host", ""))
        origin_port = origin_port or _default_port(parsed.scheme.lower())
        request_port = request_port or _default_port("https" if _is_https(request) else "http")
        return origin_host == request_host and origin_port == request_port
    if fetch_site:
        return fetch_site in ("same-origin", "same-site", "none")
    return False  # ★ 상태를 바꾸는 요청인데 Origin·Sec-Fetch-Site 가 둘 다 없으면 거절.


_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


# ── ④ 민감한 작업의 비밀번호 재확인 토큰 ──
_CONFIRM_TTL = {"trade": 90, "settings": 600}  # 초. 자동매매 조작은 짧게, 설정 화면은 길게.
_confirm_tokens: dict[str, tuple[str, float]] = {}
_confirm_lock = threading.Lock()


class ConfirmRequired(Exception):
    def __init__(self, scope: str):
        self.scope = scope


def _require_confirm(scope: str):
    def dep(request: Request) -> None:
        if _is_local_control(request):  # 트레이의 "매매 중단"은 사람이 화면 앞에 없어도 돼야 한다.
            return
        now = time.time()
        given = [t.strip() for t in request.headers.get("x-confirm-token", "").split(",") if t.strip()]
        with _confirm_lock:
            for t in [t for t, (_s, exp) in _confirm_tokens.items() if exp < now]:
                del _confirm_tokens[t]
            for t in given:
                entry = _confirm_tokens.get(t)
                if entry and entry[0] == scope:
                    return
        raise ConfirmRequired(scope)
    return dep


_CONFIRM_TRADE = Depends(_require_confirm("trade"))
_CONFIRM_SETTINGS = Depends(_require_confirm("settings"))


@app.exception_handler(ConfirmRequired)
async def _confirm_required_handler(request: Request, exc: ConfirmRequired):
    return JSONResponse(
        status_code=403,
        content={"error": "비밀번호를 다시 확인해야 합니다.", "confirm_required": exc.scope},
    )


# ── ⑤ 비밀값 가리기 ──
_REDACT_PATTERNS = [
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot***"),                                   # 텔레그램 URL
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "gsk_***"),                                             # Groq 키
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer ***"),                         # Authorization 헤더
    (re.compile(r"(?i)\b(bearer|token|secret|password|passwd|api[_-]?key|access[_-]?key|authorization)\b(\s*[=:]\s*)[^\s,;'\"&]+"), r"\1\2***"),
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "***"),                                            # 긴 토큰·키
]


def _redact(text) -> str:
    """오류 메시지·로그에 API 키가 섞여 나가지 않게 한다(요청 실패 메시지는 URL 째로 들어 있는 경우가
    많다 - 텔레그램은 URL 자체에 봇 토큰이 들어간다). 저장된 실제 키 값도 그대로 찾아 지운다."""
    out = str(text)
    try:
        from daytrader import secrets
        for name, v in secrets.all_values().items():
            # 로그인 비밀번호·세션 키는 응답·로그에 나올 일이 없고, 6자리 숫자를 지우면 다른 숫자까지 가려진다.
            if v and len(v) >= 6 and not name.startswith("auth_"):
                out = out.replace(v, "***")
    except Exception:
        pass
    for pat, rep in _REDACT_PATTERNS:
        out = pat.sub(rep, out)
    return out


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            red = _redact(msg)
            if red != msg:
                record.msg, record.args = red, ()
        except Exception:
            pass
        return True


@app.middleware("http")
async def _require_login(request: Request, call_next):
    # ★ HTML·JS·CSS 는 잠그지 않는다 - 로그인 화면 자체도 이 정적 파일로 그려지므로, 여기를 잠그면
    # 로그인 화면조차 못 띄운다. 실제 데이터·조작은 전부 /api/* 뒤에 있으니 그것만 지키면 충분하다.
    path = request.url.path
    if (path.startswith("/api/") and path not in _AUTH_EXEMPT_PATHS and not path.startswith(_AUTH_EXEMPT_PREFIXES)
            and not _is_authenticated(request)):
        return JSONResponse(status_code=401, content={"error": "로그인이 필요합니다."})
    resp = await call_next(request)
    # ★ 사용자가 실제로 화면을 조작했을 때만(화면이 X-User-Active 헤더를 붙인다) 세션을 연장한다. 자동 갱신이
    #   계속 세션을 살려 두면 자리를 비워도 영영 잠기지 않는다. 트레이(로컬 제어)는 쿠키가 없으니 대상이 아니다.
    if (request.headers.get("x-user-active") == "1" and path.startswith("/api/") and path not in _AUTH_EXEMPT_PATHS
            and not _is_local_control(request)):
        age = _session_token_age(request.cookies.get(AUTH_COOKIE_NAME))
        if age is not None and age > 20:
            _set_session_cookie(resp, request)
    return resp


@app.middleware("http")
async def _harden(request: Request, call_next):
    # 이 미들웨어를 나중에 등록해서 가장 바깥에서 돈다 - 위 로그인 검사가 만든 401 에도 헤더가 붙는다.
    if not _host_allowed(request.headers.get("host", "")):
        return JSONResponse(status_code=400, content={"error": "허용되지 않은 접속 주소입니다."})
    if not _origin_ok(request):
        return JSONResponse(status_code=403, content={"error": "다른 사이트에서 보낸 요청은 받지 않습니다."})
    resp = await call_next(request)
    h = resp.headers
    h["Content-Security-Policy"] = _CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "no-referrer"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        h["Cache-Control"] = "no-store"
    if _is_https(request):
        h["Strict-Transport-Security"] = "max-age=31536000"
    return resp


class LoginIn(BaseModel):
    password: str = ""


@app.get("/api/auth/check")
async def auth_check(request: Request):
    authed = _is_authenticated(request)
    out = {"authenticated": authed, "idle_minutes": _session_idle_seconds() // 60}
    if authed:
        out["default_password"] = _is_default_password()
    return out


# ★ 무차별 대입(brute-force) 방어. 접속 IP별로 틀린 횟수를 세다가 3번째 실패부터 잠그기 시작해,
# 틀릴 때마다 잠금 시간을 1분씩 늘린다(3번째=1분, 4번째=2분...).
# ★★★ 실제로 겪은 구멍 두 가지를 여기서 같이 막는다.
#   ① 이 카운터가 메모리에만 있으면 서버를 재시작(또는 재시작을 유도)하는 것만으로 잠금이
#      풀린다 - 그래서 상태 폴더(state_dir)의 파일에 실패 횟수를 같이 저장해 재시작에도 남는다.
#   ② _login_client_key 가 loopback 에서 온 X-Forwarded-For 를 무조건 믿었다 - Tailscale serve
#      처럼 이 PC 안의 신뢰할 수 있는 프록시를 거치는 배포에서는 맞는 가정이지만, 그런 프록시가
#      없는 보통 배포에서는 loopback 에서 원격으로 요청을 보낼 수 있는 사람이 매 시도마다 헤더 값만
#      바꿔 개별 IP 별 잠금을 통째로 우회할 수 있었다(전체 실패 횟수는 그대로다). 그래서 이제
#      X-Forwarded-For 는 DAYTRADER_TRUSTED_PROXY=1 환경변수로 이 PC 앞에 신뢰할 수 있는 프록시가
#      있다고 명시적으로 밝힌 배포에서만 믿는다 - 기본값(없음)은 항상 실제 접속 주소 하나로 센다.
#   ③ ①·②와 별개로, 키를 계속 바꿔가며(다른 IP 여러 개, 또는 위 우회) 시도해도 뚫리지 않도록
#      "누가 보냈든" 최근 10분간 실패가 너무 많으면(기본 20회) 전체를 잠깐 잠근다.
_login_attempts: dict[str, dict] = {}
_global_login_fails: list[float] = []
_global_login_locked_until: float = 0.0
_login_state_lock = threading.Lock()
_login_state_loaded = False
_GLOBAL_LOGIN_FAIL_WINDOW = 600  # 10분
_GLOBAL_LOGIN_FAIL_LIMIT = 20
_GLOBAL_LOGIN_LOCK_SECONDS = 600  # 10분


def _login_state_path() -> str:
    return os.path.join(cfg_now().state_dir, "login_attempts.json")


def _load_login_state() -> None:
    """서버가 막 뜬 뒤 처음 로그인 시도가 들어올 때 한 번, 저장돼 있던 실패 기록을 불러온다."""
    global _login_attempts, _global_login_fails, _global_login_locked_until, _login_state_loaded
    if _login_state_loaded:
        return
    _login_state_loaded = True
    try:
        with open(_login_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            attempts = data.get("attempts")
            if isinstance(attempts, dict):
                _login_attempts = {k: v for k, v in attempts.items() if isinstance(v, dict)}
            fails = data.get("global_fails")
            if isinstance(fails, list):
                _global_login_fails = [float(t) for t in fails if isinstance(t, (int, float))]
            _global_login_locked_until = float(data.get("global_locked_until") or 0.0)
    except Exception:
        pass  # 파일이 없거나 깨졌으면 "실패 기록 없음"으로 시작한다 - 로그인 자체가 막히면 안 된다.


def _save_login_state() -> None:
    try:
        path = _login_state_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "attempts": _login_attempts,
                "global_fails": _global_login_fails,
                "global_locked_until": _global_login_locked_until,
            }, f)
    except Exception:
        pass  # 저장 실패는(디스크 문제 등) 로그인 기능 자체를 막을 이유가 아니다.


def _login_client_key(request: Request) -> str:
    """무차별 대입 잠금을 셀 때 쓰는 키 = 보통은 접속 주소 그 자체.
    DAYTRADER_TRUSTED_PROXY=1 일 때만(이 PC 안의 신뢰할 수 있는 프록시, 예: Tailscale serve, 를
    직접 구성해 뒀다고 사용자가 명시한 경우) loopback 에서 온 요청의 X-Forwarded-For 원래 주소를
    대신 쓴다 - 그런 설정이 없는 보통 배포에서 이 헤더는 요청을 보내는 쪽이 마음대로 넣을 수 있는
    값이라 그대로 믿으면 헤더만 바꿔가며 개별 잠금을 피할 수 있다."""
    host = request.client.host if request.client else "unknown"
    if host in _LOOPBACK and os.environ.get("DAYTRADER_TRUSTED_PROXY", "").strip() == "1":
        fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if fwd:
            return fwd[:64]
    return host


def _login_check_lock(key: str) -> None:
    _load_login_state()
    entry = _login_attempts.get(key)
    if not entry:
        return
    remaining = entry["locked_until"] - time.time()
    if remaining > 0:
        wait_min = int(remaining // 60) + 1
        raise HTTPException(
            status_code=429,
            detail=f"비밀번호를 너무 여러 번 틀렸습니다. {wait_min}분 후 다시 시도하세요.",
        )


def _global_login_check_lock() -> None:
    _load_login_state()
    cutoff = time.time() - _GLOBAL_LOGIN_FAIL_WINDOW
    _global_login_fails[:] = [t for t in _global_login_fails if t > cutoff]
    remaining = _global_login_locked_until - time.time()
    if remaining > 0:
        wait_min = int(remaining // 60) + 1
        raise HTTPException(
            status_code=429,
            detail=f"로그인 실패가 너무 많았습니다. {wait_min}분 후 다시 시도하세요.",
        )


def _login_record_fail(key: str) -> None:
    _load_login_state()
    entry = _login_attempts.setdefault(key, {"fails": 0, "locked_until": 0.0})
    entry["fails"] += 1
    if entry["fails"] >= 3:
        lockout_minutes = entry["fails"] - 2
        entry["locked_until"] = time.time() + lockout_minutes * 60
    global _global_login_locked_until
    now = time.time()
    _global_login_fails.append(now)
    cutoff = now - _GLOBAL_LOGIN_FAIL_WINDOW
    _global_login_fails[:] = [t for t in _global_login_fails if t > cutoff]
    if len(_global_login_fails) >= _GLOBAL_LOGIN_FAIL_LIMIT:
        _global_login_locked_until = now + _GLOBAL_LOGIN_LOCK_SECONDS
    _save_login_state()


def _login_record_success(key: str) -> None:
    _load_login_state()
    if _login_attempts.pop(key, None) is not None:
        _save_login_state()


def _check_password_or_raise(request: Request, password: str) -> None:
    """/api/login 과 /api/confirm 이 함께 쓴다 - 둘 다 "비밀번호를 아는가"를 확인하는 관문이라
    잠금·전역 잠금을 공유해야 한다(그렇지 않으면 한쪽이 잠겨도 다른 쪽으로 계속 시도할 수 있다)."""
    key = _login_client_key(request)
    _global_login_check_lock()
    _login_check_lock(key)
    if not _same(password.strip(), _auth_password()):
        _login_record_fail(key)
        raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다.")
    _login_record_success(key)


@app.post("/api/login")
@api_guard
async def login(body: LoginIn, request: Request):
    # ★★★ 최초 실행으로 만들어진 임시 비밀번호가 아직 그대로라면, 그 값이 무작위라 해도
    # 원격에서는 아예 로그인을 받지 않는다 - 콘솔에 뜬 값을 어깨너머로 보거나 관리자가
    # 실수로 남에게 전달했을 수도 있으니, 비밀번호를 바꾸기 전까지는 이 컴퓨터 앞이 아니면
    # 통과시키지 않는 것이 안전하다(_is_same_machine 은 다른 원격 우회 방어와 같은 기준).
    if _is_default_password() and not _is_same_machine(request):
        raise HTTPException(
            status_code=403,
            detail="아직 최초 실행 때 만들어진 임시 비밀번호입니다. 이 컴퓨터에서 대시보드에 접속해 먼저 비밀번호를 바꿔 주세요.",
        )
    _check_password_or_raise(request, body.password)
    from daytrader import devices
    ip = _login_client_key(request)  # 이제 식별에는 안 쓴다 - 관리자 화면에 보여줄 참고 정보로만.
    ua = request.headers.get("user-agent", "")
    device_token = _device_token(request)
    key = devices.key_for_token(device_token)
    store = devices.DeviceStore(_devices_path())
    if not store.is_trusted(key):
        if _is_same_machine(request):
            # ★ 서버가 도는 이 PC에서 직접 연 브라우저(트레이의 "대시보드 열기" 등)는 승인 절차 없이
            # 곧바로 신뢰한다 - 이미 그 PC 앞에 있다는 사실 자체가 승인과 같은 수준의 신뢰이고,
            # 어차피 그 사람은 /admin 을 직접 열어 스스로 승인할 수 있다(대기시켜도 막을 수 없다).
            store.trust_directly(key, ip, ua)
        else:
            # ★ 최근에 관리자가 이 기기를 거부했으면 얼마간(DENIED_COOLDOWN) 다시 승인 대기를 만들지
            # 않는다 - 비밀번호를 맞힌 원격 상대가 거부당하자마자 또 두드려 관리자 페이지에 계속 새
            # 요청을 띄우는 것을 막는다(관리자가 다시 승인하면 이 쉬는 시간은 풀린다).
            cooldown = store.denied_cooldown_remaining(key)
            if cooldown > 0:
                raise HTTPException(
                    status_code=403,
                    detail=f"이 기기는 접속이 거부되었습니다. {int(cooldown // 60) + 1}분 후 다시 시도할 수 있습니다.",
                )
            # ★★★ 처음 보는 기기 - 비밀번호가 맞아도 곧바로 들여보내지 않는다. 서버가 도는 PC에서만
            # 열리는 관리자 페이지(/admin)에서 승인해야 한다(한 번 승인되면 이후 계속 통과 - IP 가
            # 바뀌어도 이 기기 쿠키가 그대로면 다시 승인받을 필요 없다).
            pending_token = store.find_pending_for(key) or store.create_pending(key, ip, ua)
            resp = JSONResponse(
                {"ok": False, "pending": True, "token": pending_token, "ttl": devices.PENDING_TTL},
                status_code=202,
            )
            # ★ 대기 중에도 기기 쿠키를 심어 둔다 - 나중에 승인되면 폴링하는 이 브라우저가 곧바로
            # 같은 기기로 인식되게(그 사이 쿠키가 없어 매번 새 pending 이 생기는 것도 막는다).
            _set_device_cookie(resp, request, device_token)
            return resp
    store.touch(key)
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, request)
    _set_device_cookie(resp, request, device_token)
    return resp


@app.get("/api/login/pending/{token}")
@api_guard
async def login_pending(token: str, request: Request):
    """대기 화면이 몇 초마다 불러 승인/거부 여부를 확인한다. 승인되면 여기서 바로 세션 쿠키를 내준다
    (관리자 페이지를 누른 그 브라우저가 아니라, 원래 로그인을 시도한 이 브라우저가 로그인되어야 한다)."""
    from daytrader import devices
    store = devices.DeviceStore(_devices_path())
    p = store.get_pending(token)
    if not p:
        return JSONResponse({"status": "expired"})
    if p["status"] == "pending":
        return JSONResponse({"status": "pending"})
    store.pop(token)
    if p["status"] == "approved":
        resp = JSONResponse({"status": "approved"})
        _set_session_cookie(resp, request)
        _set_device_cookie(resp, request, _device_token(request))
        _login_record_success(_login_client_key(request))
        return resp
    return JSONResponse({"status": p["status"]})


# ━━ 새 기기 승인 관리자 페이지 (서버 PC에서, 트레이 메뉴로만 열림) ━━━━━━━━━━━━━━━━━━━━━━━━
# ★ 비밀번호가 아니라 _is_admin_local() 하나로 지킨다(네트워크 위치 + 트레이 전용 토큰) - 그래서
#   세션 쿠키·확인 토큰이 없어도 된다. 그 대신 /api/ 로 시작하는 다른 모든 것과 달리 원격에서는
#   존재 자체가 보이지 않는다(401 대신 403, 토큰 없이는 로컬에서도 내용이 안 보임).

_ADMIN_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>새 기기 승인</title>
{admin_token_meta}
<style>
  body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif; max-width: 720px; margin: 32px auto; padding: 0 16px; color: #1f2328; }
  h1 { font-size: 20px; } h2 { font-size: 15px; color: #57606a; margin-top: 32px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #eaeef2; }
  button { padding: 5px 12px; border-radius: 6px; border: 1px solid #d0d7de; background: #f6f8fa; cursor: pointer; margin-right: 4px; }
  button.ok { background: #2563eb; color: #fff; border-color: #2563eb; }
  button.no { background: #fff; color: #b42318; border-color: #f3d0cc; }
  .empty { color: #8a949e; padding: 12px 0; }
  .mono { font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; word-break: break-all; }
  #admin-error { color: #b42318; display: none; margin-bottom: 12px; }
  .tabbar { display: flex; align-items: center; gap: 4px; border-bottom: 1px solid #eaeef2; margin-top: 20px; }
  .tab { border: none; background: none; border-radius: 0; margin: 0; padding: 8px 14px; font-size: 14px; color: #57606a; cursor: pointer; border-bottom: 2px solid transparent; }
  .tab.active { color: #1f2328; font-weight: 600; border-bottom-color: #2563eb; }
  .tab .count { color: #8a949e; font-weight: 400; }
  #refresh-btn { margin-left: auto; }
  #refresh-btn.spin { opacity: 0.6; }
  .panel { padding-top: 14px; }
</style></head>
<body>
<h1>🔐 새 기기 접속 승인</h1>
<p class="empty">이 페이지는 서버가 도는 이 PC의 브라우저에서, 트레이 메뉴로 열었을 때만 내용이 보입니다. 새 IP·기기가 비밀번호를 맞게 입력하면 "승인 대기" 탭에 나타납니다.</p>
<div id="admin-error"></div>
<div class="tabbar">
  <button id="tab-pending" class="tab active">승인 대기 <span id="pending-count" class="count"></span></button>
  <button id="tab-trusted" class="tab">승인된 기기 <span id="trusted-count" class="count"></span></button>
  <button id="refresh-btn">↻ 새로고침</button>
</div>
<div id="pending-panel" class="panel"><div id="pending"></div></div>
<div id="trusted-panel" class="panel" style="display:none"><div id="trusted"></div></div>
<script src="/static/admin.js?v=2"></script>
</body></html>"""


def _require_admin_local(request: Request) -> None:
    if not _is_admin_local(request):
        raise HTTPException(status_code=403, detail="이 기능은 서버가 도는 PC에서, 트레이 메뉴의 '새 기기 승인 관리'로 열어야 사용할 수 있습니다.")


@app.get("/admin")
async def admin_page(request: Request, token: str = ""):
    if token:
        # ★ [2-8] URL 로 받은 토큰은 검증되는 즉시 소모한다 - 짧게 사는 HttpOnly 쿠키를 심고,
        # 토큰이 안 보이는 URL 로 리다이렉트한다(그 뒤로 주소창·방문기록에 토큰이 남지 않는다).
        if not _is_admin_local(request, token=token):
            return HTMLResponse(
                "<h3>이 페이지는 서버가 도는 PC에서, 트레이 메뉴의 '새 기기 승인 관리'로 열어야 볼 수 있습니다.</h3>",
                status_code=403,
            )
        resp = RedirectResponse(url="/admin", status_code=302)
        ts = int(time.time())
        resp.set_cookie(
            ADMIN_BOOT_COOKIE_NAME, f"{ts}.{_sign_admin_boot(ts)}",
            max_age=ADMIN_BOOT_COOKIE_MAX_AGE, httponly=True, samesite="strict",
            secure=_is_https(request), path="/admin",
        )
        return resp
    if not _is_admin_local(request):
        return HTMLResponse(
            "<h3>이 페이지는 서버가 도는 PC에서, 트레이 메뉴의 '새 기기 승인 관리'로 열어야 볼 수 있습니다.</h3>",
            status_code=403,
        )
    # ★ 토큰은 16진수(secrets.token_hex)뿐이라 그대로 속성에 넣어도 안전하다(따옴표·꺾쇠 없음).
    # .format() 은 안 쓴다 - 아래 HTML 안의 CSS 중괄호({ })를 전부 자리표시자로 착각해 깨진다.
    meta = f'<meta name="admin-token" content="{_admin_token()}">' if _admin_token() else ""
    return HTMLResponse(_ADMIN_HTML.replace("{admin_token_meta}", meta))


@app.get("/api/admin/pending")
@api_guard
async def admin_pending(request: Request):
    _require_admin_local(request)
    from daytrader import devices
    return {"pending": devices.DeviceStore(_devices_path()).list_pending()}


@app.get("/api/admin/trusted")
@api_guard
async def admin_trusted(request: Request):
    _require_admin_local(request)
    from daytrader import devices
    return {"trusted": devices.DeviceStore(_devices_path()).list_trusted()}


class AdminTokenIn(BaseModel):
    token: str


@app.post("/api/admin/approve")
@api_guard
async def admin_approve(body: AdminTokenIn, request: Request):
    _require_admin_local(request)
    from daytrader import devices
    return {"ok": devices.DeviceStore(_devices_path()).decide(body.token, approve=True)}


@app.post("/api/admin/deny")
@api_guard
async def admin_deny(body: AdminTokenIn, request: Request):
    _require_admin_local(request)
    from daytrader import devices
    return {"ok": devices.DeviceStore(_devices_path()).decide(body.token, approve=False)}


class AdminKeyIn(BaseModel):
    key: str


@app.post("/api/admin/revoke")
@api_guard
async def admin_revoke(body: AdminKeyIn, request: Request):
    _require_admin_local(request)
    from daytrader import devices
    return {"ok": devices.DeviceStore(_devices_path()).revoke(body.key)}


class ConfirmIn(BaseModel):
    password: str = ""
    scope: str = "trade"


@app.post("/api/confirm")
@api_guard
async def confirm_password(body: ConfirmIn, request: Request):
    """자동매매 조작·설정 변경 직전의 비밀번호 재확인 - 맞으면 짧게 쓰는 토큰을 준다."""
    import secrets as pysecrets
    ttl = _CONFIRM_TTL.get(body.scope)
    if ttl is None:
        raise HTTPException(status_code=400, detail="알 수 없는 확인 범위입니다.")
    _check_password_or_raise(request, body.password)
    token = pysecrets.token_urlsafe(24)
    with _confirm_lock:
        _confirm_tokens[token] = (body.scope, time.time() + ttl)
    return {"token": token, "scope": body.scope, "ttl": ttl}


@app.post("/api/logout")
@api_guard
async def logout(request: Request):
    # ★ [2-6] 쿠키만 지우면 그 값 자체(HMAC 서명)는 여전히 유효해, 미리 빼돌려진 쿠키로는 로그아웃
    # 후에도 계속 들어올 수 있었다. 이 기기(daytrader_device 쿠키)로 지금까지 발급된 세션은 여기서
    # 서버 쪽에도 무효로 기록해 둔다.
    _record_logout(request)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return resp


@app.post("/api/logout/all")
@api_guard
async def logout_all(request: Request):
    """[2-6] 모든 기기에서 로그아웃 - 기기를 잃어버렸거나 낯선 세션이 의심될 때 쓴다."""
    _record_logout_all_devices()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return resp


class AuthPasswordIn(BaseModel):
    password: str = ""


def _weak_password(pw: str) -> bool:
    """뻔한 6자리(같은 숫자 반복, 연속 숫자, 기본값)는 쓰지 못하게 한다 - 무차별 대입에서 제일 먼저 시도되는 값들이다."""
    if pw == DEFAULT_AUTH_PASSWORD or len(set(pw)) == 1:
        return True
    return pw in ("012345", "123456", "234567", "345678", "456789", "567890",
                  "654321", "543210", "987654", "876543", "098765", "121212", "123123")


@app.post("/api/auth/password", dependencies=[_CONFIRM_SETTINGS])
@api_guard
async def change_password(body: AuthPasswordIn, request: Request):
    from daytrader import secrets
    pw = body.password.strip()
    if not re.fullmatch(r"\d{6}", pw):
        raise HTTPException(status_code=400, detail="비밀번호는 숫자 6자리여야 합니다.")
    if _weak_password(pw):
        raise HTTPException(status_code=400, detail="너무 쉬운 비밀번호입니다(같은 숫자 반복·연속 숫자·기본값 불가). 다른 6자리를 골라 주세요.")
    # ★ 사용자가 직접 고른 비밀번호이니, 최초 실행 때의 "임시 비밀번호" 딱지를 뗀다
    # (이게 남아 있으면 login() 이 계속 원격 로그인을 막는다).
    secrets.save({"auth_password": pw, "auth_password_is_default": ""})
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, request)  # ★ 방금 바꾼 사람까지 로그아웃되면 안 되니 새 쿠키를 바로 심어 준다.
    return resp


@app.on_event("startup")
async def _on_startup() -> None:
    ensure_user_files()  # exe 첫 실행이면 config.yaml·themes.yaml 을 꺼내 놓는다.
    setup_logging()
    global _review_started
    if not _review_started:
        _review_started = True
        threading.Thread(target=_review_loop, daemon=True, name="daily-review").start()
    global _maintenance_started
    if not _maintenance_started:
        _maintenance_started = True
        threading.Thread(target=_db_maintenance_loop, daemon=True, name="db-maintenance").start()


# ━━ 설정 읽기·쓰기 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_yaml_rt = YAML()
_yaml_rt.preserve_quotes = True
_yaml_safe = YAML(typ="safe")


def _read_config_raw(round_trip: bool = False):
    """★ A-14. ruamel.yaml 라운드트립으로 주석을 보존한 채 읽는다."""
    yaml_obj = _yaml_rt if round_trip else _yaml_safe
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml_obj.load(f)


def _plain(obj):
    """ruamel 의 스칼라 래퍼를 순수 파이썬 타입으로 벗겨낸다.
    ★ ScalarFloat/ScalarInt 는 float/int 를 상속하지만 타입이 달라 PyYAML·JSON
    인코더가 표현하지 못한다. ScalarString 도 isinstance(str) 이 True 인데
    타입이 다르므로 `type(obj) is not str` 까지 봐야 걸러진다.
    """
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    if isinstance(obj, ScalarFloat):
        return float(obj)
    if isinstance(obj, ScalarInt):
        return int(obj)
    if isinstance(obj, str) and type(obj) is not str:
        return str(obj)
    return obj


_TIME_LIKE = re.compile(r"^\d{1,2}:\d{2}$")


def _quote_time_like(obj):
    """★★ "14:00" 같은 HH:MM 문자열이 따옴표 없이 저장되면, load_config() 가
    쓰는 PyYAML 로더는 YAML 1.1 60진법 규칙에 따라 이를 정수(14:00 → 840)로
    잘못 읽는다 - 저장 직후 검증(_validate_raw)이 깨지는 실제 버그였다.
    raw.update(body) 로 서브트리를 순수 dict 로 병합하면 ruamel 이 기억하던
    따옴표 스타일이 사라지므로, 저장 직전에 이런 문자열을 전부 강제로
    따옴표 붙은 스칼라로 바꿔 둔다.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            obj[k] = _quote_time_like(v)
        return obj
    if isinstance(obj, list):
        return [_quote_time_like(v) for v in obj]
    if isinstance(obj, str) and _TIME_LIKE.match(obj):
        return DoubleQuotedScalarString(obj)
    return obj


def _write_config_raw(raw) -> None:
    """.tmp 에 쓰고 os.replace 로 원자적으로 교체한다.
    ★ raw 는 ruamel 라운드트립 객체(주석·따옴표 보존) 그대로 받아 그대로 덤프한다.
    plain dict 로 바꿔서 다시 쓰면 "09:20" 같은 문자열이 YAML 60진법 해석 규칙에
    걸려 정수로 둔갑할 수 있다 - ruamel 의 스칼라 타입이 원래 표현을 기억한다.
    """
    _quote_time_like(raw)  # ★ 병합 과정에서 따옴표가 사라진 시간 문자열을 다시 보호한다.
    tmp_path = f"{CONFIG_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        _yaml_rt.dump(raw, f)

    # ★★★ 실제로 겪은 사고 - 주석을 보존하며 쓰는 과정에서 리스트 형식이
    # 깨져("watchlist: - AAPL") 설정 파일을 아예 못 읽게 됐고, 프로그램이
    # 시작조차 안 됐다. 설정 저장은 흔한 동작인데 한 번 망가지면 사용자가
    # 직접 YAML 을 고쳐야 한다 - 그건 받아들일 수 없다.
    # ★ 바꿔치기 전에 임시 파일을 실제로 읽어 본다. 못 읽으면 원본을
    #   그대로 두고 실패로 알린다(망가진 파일로 덮어쓰지 않는다).
    try:
        with open(tmp_path, "r", encoding="utf-8") as f:
            yaml.safe_load(f)
    except Exception as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise HTTPException(
            500,
            f"설정을 저장하지 못했습니다(파일 형식이 깨질 뻔해 되돌렸습니다): {exc}",
        )

    # ★ [4-3] 지금 파일을 덮어쓰기 직전, config.yaml.bak 으로 한 벌 남겨 둔다 -
    # 저장이 검증까지 통과해도 사용자가 원치 않는 값을 저장했을 수 있으니,
    # 되돌릴 수단은 있어야 한다. 백업 자체가 실패해도 저장은 막지 않는다.
    if os.path.exists(CONFIG_PATH):
        try:
            shutil.copyfile(CONFIG_PATH, f"{CONFIG_PATH}.bak")
        except OSError as exc:
            logging.getLogger(__name__).warning("config.yaml.bak 백업 실패(저장은 계속 진행): %s", exc)

    os.replace(tmp_path, CONFIG_PATH)


def _validate_raw(raw) -> None:
    """★ 임시 파일에 써서 load_config() 로 검증한 뒤에만 저장한다.
    말이 안 되는 설정을 원본에 남기면 다음 실행이 아예 안 된다.
    raw 는 ruamel 라운드트립 객체 그대로 받는다 (위와 같은 이유).
    """
    _quote_time_like(raw)  # ★ _write_config_raw 와 동일한 이유로 검증 단계에서도 보호해야 한다.
    tmp_path = f"{CONFIG_PATH}.validate-tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        _yaml_rt.dump(raw, f)
    try:
        load_config(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


# ━━ 라우트: 설정 · 준비 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CredentialsIn(BaseModel):
    client_id: str
    client_secret: str


class ThemeAddIn(BaseModel):
    theme: str
    symbol: str


class NotifyTestIn(BaseModel):
    token: str
    chat_id: str


class NotifyCredentialsIn(BaseModel):
    token: str
    chat_id: str  # 빈 값도 받는다 - 지우는 것도 설정이다.


class NotifySendIn(BaseModel):
    kind: str  # now | daily | monthly | yearly


def _technique_labels(keys) -> list:
    """★ 기법 키(volatility_breakout)를 사람이 읽는 라벨(변동성 돌파)로.
    ★ 실패하면 키를 그대로 돌려준다 - 라벨 하나 때문에 화면이 죽으면 안 된다."""
    try:
        from daytrader.playbook import ENTRY_TECHNIQUES
        out = []
        for k in keys or []:
            cls = ENTRY_TECHNIQUES.get(k)
            out.append(getattr(cls, "label", None) or k)
        return out
    except Exception:
        return list(keys or [])


def _session_info() -> dict:
    """★★★ "장이 열려 있는데 왜 종목 선정도 매매도 안 하냐"는 물음에
    답하기 위한 정보. 이 프로그램은 국내 장 시간(09:00~15:30) 전체가 아니라
    설정된 진입 구간(기본 09:20~14:00)에만 신규 진입한다 - 장 막판 변동성을
    피하려는 의도된 설계다. 그런데 화면에는 그 사유가 전혀 안 나와서
    "아직 스크리닝 결과가 없습니다"만 보이고 고장으로 오해하게 됐다.
    ★ 조회에 실패해도 화면이 죽으면 안 되니 예외를 밖으로 내지 않는다.
    """
    try:
        from daytrader import session
        from daytrader.timeutil import now_kst
        cfg = cfg_now()
        ph = session.phase(cfg, now=now_kst(), client=None)
        return {
            "phase": ph.get("phase"), "label": ph.get("label"),
            "trading": ph.get("trading"), "why": ph.get("why"),
            "live": ph.get("live"),
            "scan_start": ph.get("scan_start"), "scan_end": ph.get("scan_end"),
            "next_open": ph.get("next_open"),
        }
    except Exception:
        return {}


def _screening_idle() -> bool:
    """★★★ 장이 닫혀 있으면(주말·휴장일·장 마감 후) 종목 선정을 하지 않는다 -
    신규 진입이 없으니 결과를 쓸 곳이 없고, 랭킹 API 호출만 낭비된다.
    시뮬레이션·리플레이는 실제 장과 무관하게 도니 예외. 판정에 실패하면
    (모르면) 기존처럼 선정한다."""
    try:
        from daytrader import session
        from daytrader.timeutil import now_kst
        cfg = cfg_now()
        if cfg.uses_fake_data:
            return False
        return not session.phase(cfg, now=now_kst(), client=None).get("live")
    except Exception:
        return False


def _num(v):
    """★★★ 실제로 겪은 버그("unsupported operand types") - 증권사·거래소
    API 는 정밀도 보존을 위해 숫자를 문자열로 주는 경우가 흔하다(토스의
    BigDecimal 필드, 빗썸/업비트 계열의 balance·avg_buy_price 등). 문자열
    상태로 두면 이후 합계·수익 계산(숫자 - 문자열)에서 그대로 죽는다 -
    여기서 한 번에 안전하게 float 로 정규화하고, 변환 불가능하면 None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@app.get("/api/debug/rankings")
@api_guard
async def debug_rankings():
    """★★★ "연계 테스트는 통과하는데 종목선정만 시세를 못 받는다"를 끝내기
    위한 진단.

    ★★★ 실제로 겪은 함정 - 처음 만든 진단은 rankings 를 6번이나 불렀다
    (케이스 3 + 파싱추적 1 + 스크리너 2). RANKING 한도는 초당 3회라
    뒷부분이 제한에 걸려, "랭킹은 2건인데 스크리너는 0개"라는 모순된
    결과가 나왔다 - 진단 도구가 스스로 문제를 만든 것이다.
    지금은 호출을 한 번만 하고 그 결과를 재사용한다.
    """
    cfg = cfg_now()
    client = get_client()
    out = {"mode": cfg.mode, "ranking_count": cfg.screen.ranking_count, "cases": []}

    # ★ 딱 한 번만 부른다 - 실제 스크리닝과 같은 조건으로.
    data, err = None, None
    try:
        data = client.rankings(type="MARKET_TRADING_AMOUNT", marketCountry="KR",
                               duration="realtime", count=cfg.screen.ranking_count)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"

    row = {"label": "거래대금 랭킹(스크리닝과 동일 조건)",
           "type": "MARKET_TRADING_AMOUNT", "duration": "realtime",
           "count": cfg.screen.ranking_count}
    if err:
        row["ok"] = False
        row["error"] = err
    else:
        row["ok"] = True
        row["response_type"] = type(data).__name__
        row["length"] = len(data) if hasattr(data, "__len__") else None
        if isinstance(data, list):
            row["symbols"] = [f'{x.get("symbol","?")}({x.get("name","")})'
                              for x in data if isinstance(x, dict)][:20]
        elif isinstance(data, dict):
            row["dict_keys"] = list(data.keys())[:10]
    out["cases"].append(row)

    # ★ 같은 응답으로 파싱을 직접 돌려 본다 - API 를 다시 부르지 않는다.
    # ★★★ 실제로 겪은 버그 - data 가 딕셔너리면 [:5] 에서
    # "KeyError: slice(None, 5, None)" 로 죽어 진단 자체가 안 됐다.
    # 리스트일 때만 자른다(딕셔너리면 위 dict_keys 로 이미 보여 준다).
    trace = []
    items = data if isinstance(data, list) else []
    for r in items[:5]:
        step = {"is_dict": isinstance(r, dict),
                "raw_keys": list(r.keys()) if isinstance(r, dict) else None}
        if isinstance(r, dict):
            raw_sym = r.get("symbol", "")
            step["symbol_raw"] = raw_sym
            step["symbol_zfill"] = str(raw_sym).zfill(6)
            step["passes_filter"] = bool(str(raw_sym).zfill(6).strip("0"))
            step["price"] = r.get("price") or r.get("lastPrice")
            step["changeRate"] = r.get("changeRate")
            step["tradingAmount"] = r.get("tradingAmount") or r.get("tradingValue")
        trace.append(step)
    out["parse_trace"] = trace

    # ★ 같은 응답을 스크리너 파싱 규칙에 그대로 통과시켜 본다.
    #   여기서 0 이면 파싱 문제, 위 length 가 0 이면 API 문제로 갈린다.
    parsed = 0
    for r in items:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol", "")).zfill(6)
        if not sym.strip("0"):
            continue
        parsed += 1
    out["parsed_count"] = parsed
    # ★★★ 화면이 "스크리너가 실제로 받은 종목"을 표시하는데, 진단을 1회
    # 호출로 바꾸면서 이 항목이 빠져 None 으로 나왔다(판정 줄이 비어 보임).
    # ★ API 를 다시 부르지 않는다 - 이미 받은 응답을 스크리너와 같은
    #   규칙으로 세어서 보여주면 충분하고, 한도도 지킬 수 있다.
    out["screener"] = {
        "market_size": parsed,
        "errors": [row["error"]] if row.get("error") else [],
        "note": "같은 응답을 스크리너 파싱 규칙으로 센 값입니다(API 재호출 없음).",
    }
    out["note"] = (
        "이 진단은 랭킹 API 를 한 번만 호출합니다(한도 초당 3회). "
        "받은 건수와 파싱 통과 건수가 같으면 프로그램은 정상이고, "
        "받은 건수 자체가 적으면 토스 계정·시장 쪽 문제입니다."
    )
    return out


@app.get("/api/setup")
@api_guard
async def get_setup():
    cfg = cfg_now()
    from daytrader.screener import load_themes
    themes = load_themes(cfg.themes_file)
    symbol_count = len({s for codes in themes.values() for s in codes})
    return {
        "mode": cfg.mode,
        "theme_count": len(themes),
        "symbol_count": symbol_count,
        "has_keys": bool(cfg.client_id and cfg.client_secret),
        "pid": os.getpid(),
    }


@app.get("/api/account/holdings")
@api_guard
def get_account_holdings():
    """★★★ 토스 계좌에서 가져온 실제 보유 현황 - 종목별 매수가·매수수량·
    현재가·수익률과 현금 잔고를 그대로 보여주는 조회 전용 화면이다.
    이 엔진이 산 것과 완전히 무관하게 "지금 계좌에 실제로 뭐가 있는지"를
    그대로 보여준다 - 화면에 반드시 "이 종목은 본 프로그램에서 매매
    불가"를 명시한다(이 프로그램의 매매 대상은 오직 이 엔진이 직접 산
    종목뿐이라는 원칙 그대로).
    """
    cfg = cfg_now()
    if not (cfg.client_id and cfg.client_secret):
        raise HTTPException(status_code=400, detail="토스 API 키가 등록되어 있지 않습니다. [준비·연결]에서 먼저 등록하세요.")

    # ★★★ 실제로 겪은 버그 - get_client() 는 cfg.mode 가 sim/replay 면
    # 실제 토스 API 가 아니라 SimClient(가상 클라이언트)를 돌려준다.
    # "내 계좌 현황"은 지금 국내주식 엔진이 어떤 모드로 도는지와 완전히
    # 무관하게 항상 실제 계좌를 보여줘야 하는데, get_client() 를 그대로
    # 쓰면 시뮬레이션 모드일 때 가짜(대부분 텅 빈) 데이터가 나온다 -
    # 사용자가 "연계 테스트만 있고 실제 계좌 현황이 안 나온다"고 느낀
    # 정확한 원인이다. 여기서는 모드와 무관하게 항상 진짜 API 를 쓴다.
    from daytrader.router import build_router
    client = build_router(cfg)

    try:
        client.resolve_account()
    except Exception:
        pass  # ★ 계좌가 이미 지정돼 있으면 재조회 실패해도 무시한다.

    # ★ 실제 스펙(HoldingsOverview 모델) 확인 결과 - holdings() 는 리스트가
    # 아니라 {"items": [...], "totalPurchaseAmount":..., ...} 객체를
    # 반환한다(공식 문서로 확인, 예전엔 이 프로그램 곳곳이 리스트로
    # 착각하고 있었다 - safety.py 도 함께 고쳤다).
    holdings = client.holdings()
    items = holdings.get("items", []) if isinstance(holdings, dict) else (holdings or [])

    rows = []
    for h in items:
        profit_loss = h.get("profitLoss") or {}
        rows.append({
            "symbol": h.get("symbol"),
            "name": h.get("name"),
            "market_country": h.get("marketCountry"),
            "currency": h.get("currency"),
            # ★★★ 실제로 겪은 버그("unsupported operand types") - 토스 API
            # 문서상 이 값들은 BigDecimal 타입인데, JSON 직렬화 시 정밀도
            # 보존을 위해 문자열로 오는 경우가 있다("70000.0" 처럼). 문자열
            # 상태로 두면 이후 합계 계산(숫자 + 문자열)에서 TypeError 가
            # 난다 - 여기서 한 번에 안전하게 float 로 정규화한다.
            "quantity": _num(h.get("quantity")),
            "avg_purchase_price": _num(h.get("averagePurchasePrice")),
            "last_price": _num(h.get("lastPrice")),
            "profit_loss_amount": _num(profit_loss.get("amount")),
            "profit_loss_rate": _num(profit_loss.get("rate")),
        })

    # ★★★ "국내계좌와 해외계좌로 구분해달라" - marketCountry 가 "KR"이면
    # 국내, 그 외(US 등)면 해외로 나눈다. 시장별 수익금액 합계도 함께
    # 계산해서 준다(개별 profit_loss_amount 를 그대로 더한 값).
    domestic_rows = [r for r in rows if r["market_country"] == "KR"]
    overseas_rows = [r for r in rows if r["market_country"] != "KR"]

    def _sum_profit(rs):
        total = 0.0
        has_value = False
        for r in rs:
            if r["profit_loss_amount"] is not None:
                total += r["profit_loss_amount"]
                has_value = True
        return total if has_value else None

    try:
        bp = client.buying_power(currency="KRW")
        cash_krw = _num(bp.get("cash")) if isinstance(bp, dict) else None
    except Exception:
        cash_krw = None

    return {
        "rows": rows,  # ★ 하위 호환용 - 기존 화면이 이걸 참조해도 그대로 동작한다.
        "domestic_rows": domestic_rows,
        "overseas_rows": overseas_rows,
        "domestic_profit_total": _sum_profit(domestic_rows),
        "overseas_profit_total": _sum_profit(overseas_rows),
        "cash_krw": cash_krw,
        "total_purchase_amount": holdings.get("totalPurchaseAmount") if isinstance(holdings, dict) else None,
    }


@app.get("/api/account/crypto-holdings")
@api_guard
def get_account_crypto_holdings():
    """★★★ "빗썸 API 연결 시 암호화폐 계좌현황도 똑같이 만들어달라" -
    국내/해외 계좌 현황과 같은 형태(코인별 매수가·보유수량·현재가·수익률·
    수익금액 + 현금(KRW) 잔고)로 빗썸 실제 계좌를 보여준다. 이 엔진이
    직접 산 코인이 아니면 매매하지 않는다는 원칙은 여기서도 그대로다 -
    이 화면은 순수 조회 전용이다.
    """
    cfg = cfg_now()
    if not (cfg.bithumb_access_key and cfg.bithumb_secret_key):
        raise HTTPException(status_code=400, detail="빗썸 API 키가 등록되어 있지 않습니다. [준비·연결]에서 먼저 등록하세요.")

    from daytrader.bithumb_api import BithumbClient
    client = BithumbClient(cfg.bithumb_access_key, cfg.bithumb_secret_key)

    # ★ 빗썸/업비트 계열 표준 스펙 - accounts() 는
    # [{"currency": "BTC", "balance": "0.001", "avg_buy_price": "...", ...}, ...]
    # 리스트를 그대로 반환한다(기존 bithumb_broker.py 의 cash() 가 이미
    # 이 필드명으로 KRW 잔고를 읽고 있어, 그와 일치시켰다).
    from daytrader.bithumb_api import BithumbApiError
    try:
        accounts = client.accounts() or []
    except BithumbApiError as exc:
        # ★ 빗썸이 거절한 사유(키 오류·IP 미등록·권한 없음)를 화면에 그대로 보여준다 - 막연한 500 이 아니라.
        raise HTTPException(status_code=502, detail=_redact(f"빗썸 계좌 조회 실패: {exc}"))
    cash_krw = 0.0
    coin_accounts = []
    for a in accounts:
        currency = a.get("currency")
        balance = _num(a.get("balance")) or 0.0
        if currency == "KRW":
            cash_krw = balance
            continue
        if balance <= 0:
            continue
        coin_accounts.append(a)

    rows = []
    if coin_accounts:
        markets = [f"KRW-{a.get('currency')}" for a in coin_accounts]
        try:
            tickers = {t.get("market"): t.get("trade_price") for t in (client.ticker(markets) or [])}
        except Exception:
            tickers = {}
        for a in coin_accounts:
            currency = a.get("currency")
            market = f"KRW-{currency}"
            quantity = _num(a.get("balance")) or 0.0
            avg_price = _num(a.get("avg_buy_price")) or 0.0
            # ★ ticker() 의 trade_price 도 문자열로 올 수 있어 안전하게 변환한다.
            last_price = _num(tickers.get(market))
            profit_amount = None
            profit_rate = None
            if last_price is not None and avg_price > 0:
                profit_amount = (last_price - avg_price) * quantity
                profit_rate = (last_price - avg_price) / avg_price
            rows.append({
                "symbol": market,
                "name": currency,
                "quantity": quantity,
                "avg_purchase_price": avg_price,
                "last_price": last_price,
                "profit_loss_amount": profit_amount,
                "profit_loss_rate": profit_rate,
            })

    profit_total = None
    if rows:
        amounts = [r["profit_loss_amount"] for r in rows if r["profit_loss_amount"] is not None]
        if amounts:
            profit_total = sum(amounts)

    return {
        "rows": rows,
        "cash_krw": cash_krw,
        "profit_total": profit_total,
    }


@app.get("/api/source")
@api_guard
async def get_source():
    cfg = cfg_now()
    if cfg.mode in ("sim", "replay"):
        return {"source": "가짜 시장(시뮬레이션)"}
    client = get_client()
    out = {"source": type(client).__name__}
    if hasattr(client, "capabilities"):
        out.update(client.capabilities())
    # ★★★ 실제로 겪은 문제 - 라우터(QuoteRouter)는 어떤 API 가 무슨 이유로
    # 실패했는지 failures 에 정확히 기록해 두는데, 이 정보가 어디에도
    # 노출되지 않아 "연계 테스트는 통과하는데 시세를 못 가져온다"는
    # 상황에서 원인을 추적할 방법이 없었다. 화면이 볼 수 있게 내보낸다.
    failures = getattr(client, "failures", None)
    if failures:
        out["failures"] = dict(failures)
    source_of = getattr(client, "source_of", None)
    if source_of:
        out["source_of"] = dict(source_of)
    return out


@app.get("/api/net/diagnose")
@api_guard
def net_diagnose():
    # ★ def(동기) 로 둔다 - FastAPI 가 스레드풀에서 돌려서, 외부 접속을 기다리는 동안
    #   다른 요청(같은 화면이 동시에 부르는 API 들)을 막지 않는다.
    from daytrader import netutil
    return netutil.diagnose_cached()


@app.post("/api/net/reset")
@api_guard
async def net_reset():
    from daytrader import netutil
    netutil.reset()
    return {"ok": True}


def _write_env_var(key: str, value: str) -> None:
    """★ .env 는 토스 키와 빗썸 키가 함께 산다. 파일 전체를 새로 써버리면
    한쪽을 저장할 때 다른 쪽이 지워진다(실제로 있었던 문제) - 이 키만
    갱신하고 나머지 줄은 그대로 둔다.
    """
    lines = []
    found = False
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.rstrip("\n")
                if stripped.startswith(key + "="):
                    lines.append(f"{key}={value}")
                    found = True
                elif stripped:
                    lines.append(stripped)
    if not found:
        lines.append(f"{key}={value}")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(ENV_PATH, 0o600)
    os.environ[key] = value


@app.post("/api/credentials", dependencies=[_CONFIRM_SETTINGS])
@api_guard
async def set_credentials(body: CredentialsIn):
    # ★★★ API 키는 secrets.yaml 한 곳에 저장한다 - 예전처럼 .env 와
    # config.yaml 에 흩어 두면 공유·백업에서 빼기 어렵다.
    from daytrader import secrets
    secrets.save({"toss_client_id": body.client_id,
                  "toss_client_secret": body.client_secret})
    get_client(force_new=True)
    return {"ok": True}


class BithumbCredentialsIn(BaseModel):
    access_key: str
    secret_key: str


@app.post("/api/bithumb/credentials", dependencies=[_CONFIRM_SETTINGS])
@api_guard
async def set_bithumb_credentials(body: BithumbCredentialsIn):
    from daytrader import secrets
    secrets.save({"bithumb_access_key": body.access_key,
                  "bithumb_secret_key": body.secret_key})
    return {"ok": True}


class LlmCredentialsIn(BaseModel):
    """넘어온 항목만 바꾼다(없으면 그대로). 빈 문자열이면 그 키를 지운다."""
    groq_key: str | None = None
    groq_key2: str | None = None


def _valid_llm_key(key: str) -> bool:
    return len(key) >= 20 and key.replace("-", "").replace("_", "").isalnum()


@app.post("/api/llm/credentials", dependencies=[_CONFIRM_SETTINGS])
@api_guard
async def set_llm_credentials(body: LlmCredentialsIn):
    """Groq 키 2개 저장(암호화). 첫 키의 무료 한도가 차면 두 번째로 넘어간다. 둘 다 없어도 프로그램은 키워드 기반으로 돈다."""
    from daytrader import secrets
    updates = {}
    for name, val in (("groq_api_key", body.groq_key), ("groq_api_key2", body.groq_key2)):
        if val is None:
            continue
        val = val.strip()
        if val and not _valid_llm_key(val):
            raise HTTPException(status_code=400, detail="Groq 키 형식이 올바르지 않습니다(20자 이상의 영문·숫자 값).")
        updates[name] = val
    if updates:
        secrets.save(updates)
    return {"ok": True}


@app.post("/api/llm/test", dependencies=[_CONFIRM_SETTINGS])
@api_guard
def test_llm():
    """등록된 Groq 키를 하나씩 짧게 시험한다(어느 키가 되고 안 되는지 알려 준다)."""
    from daytrader import llm
    cfg = cfg_now()
    if not llm.available(cfg):
        raise HTTPException(status_code=400, detail="등록된 Groq 키가 없습니다.")
    return {"results": [{"label": r["label"], "ok": r["ok"], "error": _redact(r["error"])} for r in llm.test_all(cfg)]}


@app.get("/api/news/guard")
@api_guard
def news_guard_status():
    """뉴스 위험 필터 상태·최근 판정(키·헤드라인 원문 전체는 내보내지 않는다)."""
    from daytrader.news_guard import get_guard
    return get_guard(cfg_now()).status()


# ━━ 하루 복기(텔레그램) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _review_data(market: str, cfg, now_kst):
    """(거래 목록, 보유 목록, 단위, 실거래 여부, 손절폭) - 복기에 필요한 것만 모은다."""
    from datetime import timedelta
    from daytrader.timeutil import KST
    positions: list = []
    trades: list = []
    if market == "domestic":
        from daytrader.ledger import Ledger
        day = str(now_kst.date())
        for r in Ledger(cfg.state_dir).trades(modes=[cfg.mode]):
            if r.get("date") == day:
                trades.append({"name": r.get("name") or r.get("symbol"), "pnl": r.get("pnl"),
                               "technique": r.get("entry_technique") or r.get("technique"), "reason": r.get("reason"),
                               "adds": r.get("adds"), "scaled_out": r.get("scaled_out"),
                               "entry_price": r.get("entry"), "exit_price": r.get("exit")})
        eng = runner.engine
        if eng is not None:
            for sym, p in list(eng.state.positions.items()):
                last = getattr(p, "last_price", 0) or p.entry_price
                positions.append({"name": p.name or sym, "pnl": (last - p.entry_price) * p.quantity})
        return trades, positions, "won", bool(cfg.is_live), cfg.risk.stop_loss_pct
    if market == "overseas":
        from daytrader.overseas_engine import OverseasState
        st = _overseas_engine.state if _overseas_engine is not None else OverseasState(os.path.join(cfg.state_dir, "overseas_state.json"))
        from daytrader.overseas_engine import _to_ny
        ny_today = _to_ny(now_kst).date()
        for c in st.closed:
            if isinstance(c, dict) and c.get("exit_time") and _to_ny(__import__("datetime").datetime.fromtimestamp(c["exit_time"], KST)).date() == ny_today:
                trades.append({"name": c.get("symbol"), "pnl": c.get("pnl"), "technique": c.get("entry_technique"), "reason": c.get("reason"),
                               "adds": c.get("adds"), "scaled_out": c.get("scaled_out"),
                               "entry_price": c.get("entry_price"), "exit_price": c.get("exit_price")})
        for sym, p in st.book.all().items():
            last = (_overseas_engine._last_prices.get(sym) if _overseas_engine is not None else None) or p.entry_price
            positions.append({"name": sym, "pnl": (last - p.entry_price) * p.quantity})
        return trades, positions, "usd", bool(cfg.overseas.mode == "live"), cfg.overseas.stop_loss_pct
    from daytrader.crypto_engine import CryptoState
    st = _crypto_engine.state if _crypto_engine is not None else CryptoState(os.path.join(cfg.state_dir, "crypto_state.json"))
    cutoff = time.time() - 86400
    for c in st.closed:
        if isinstance(c, dict) and (c.get("exit_time") or 0) >= cutoff:
            trades.append({"name": c.get("market"), "pnl": c.get("pnl"), "technique": c.get("entry_technique"), "reason": c.get("reason"),
                           "adds": c.get("adds"), "scaled_out": c.get("scaled_out"),
                           "entry_price": c.get("entry_price"), "exit_price": c.get("exit_price")})
    for m, p in st.book.all().items():
        last = (_crypto_engine._last_prices.get(m) if _crypto_engine is not None else None) or p.entry_price
        positions.append({"name": m, "pnl": (last - p.entry_price) * p.quantity})
    return trades, positions, "won", bool(cfg.crypto.live), cfg.crypto.stop_loss_pct


def build_daily_review(market: str, day_label: str = "") -> str:
    from datetime import datetime as _dt
    from daytrader import daily_review as dr
    from daytrader.news_guard import get_guard
    from daytrader.timeutil import KST
    cfg = cfg_now()
    now = _dt.now(KST)
    trades, positions, unit, live, stop = _review_data(market, cfg, now)
    s = dr.stats(trades)
    blocked = sum(1 for r in get_guard(cfg).recent(200) if r.get("level") == "block" and r.get("market") == market and time.time() - r.get("at", 0) < 86400)
    notes = dr.observations(market, s, stop, blocked)
    return dr.compose(market, day_label or str(now.date()), s, positions, notes, unit, live)


def _send_review(market: str, day_label: str = "") -> tuple:
    from daytrader import notify
    cfg = cfg_now()
    text = build_daily_review(market, day_label)
    return notify.Telegram(cfg).send_now(text)


def _period_review_data(market: str, cfg, since_date: str, until_date: str, since_ts: float, until_ts: float):
    """(trades, unit, is_live) - since_date~until_date(포함, 'YYYY-MM-DD') 사이에 청산된 거래.
    주간·월간 복기용으로 _review_data() 를 기간 범위로 일반화한 것 - "오늘/최근 24시간"
    대신 임의의 기간을 본다. ★ 국내주식은 거래 기록에 exit_time 이 정렬용 문자열이라 날짜
    문자열(date)로 거르고, 나머지 세 시장(해외·암호화폐·스윙)은 exit_time 이 epoch 숫자라
    since_ts~until_ts 로 거른다 - 저장 형식이 시장마다 다르다."""
    trades: list = []
    if market == "domestic":
        from daytrader.ledger import Ledger
        for r in Ledger(cfg.state_dir).trades(modes=[cfg.mode]):
            d = r.get("date")
            if d and since_date <= d <= until_date:
                trades.append({"name": r.get("name") or r.get("symbol"), "pnl": r.get("pnl"),
                               "technique": r.get("entry_technique") or r.get("technique"), "reason": r.get("reason"),
                               "adds": r.get("adds"), "scaled_out": r.get("scaled_out"),
                               "entry_price": r.get("entry"), "exit_price": r.get("exit")})
        return trades, "won", bool(cfg.is_live)
    if market == "overseas":
        from daytrader.overseas_engine import OverseasState
        st = _overseas_engine.state if _overseas_engine is not None else OverseasState(os.path.join(cfg.state_dir, "overseas_state.json"))
        for c in st.closed:
            et = c.get("exit_time") if isinstance(c, dict) else None
            if et and since_ts <= et < until_ts:
                trades.append({"name": c.get("symbol"), "pnl": c.get("pnl"), "technique": c.get("entry_technique"), "reason": c.get("reason"),
                               "adds": c.get("adds"), "scaled_out": c.get("scaled_out"),
                               "entry_price": c.get("entry_price"), "exit_price": c.get("exit_price")})
        return trades, "usd", bool(cfg.overseas.mode == "live")
    if market == "crypto":
        from daytrader.crypto_engine import CryptoState
        st = _crypto_engine.state if _crypto_engine is not None else CryptoState(os.path.join(cfg.state_dir, "crypto_state.json"))
        for c in st.closed:
            et = c.get("exit_time") if isinstance(c, dict) else None
            if et and since_ts <= et < until_ts:
                trades.append({"name": c.get("market"), "pnl": c.get("pnl"), "technique": c.get("entry_technique"), "reason": c.get("reason"),
                               "adds": c.get("adds"), "scaled_out": c.get("scaled_out"),
                               "entry_price": c.get("entry_price"), "exit_price": c.get("exit_price")})
        return trades, "won", bool(cfg.crypto.live)
    from daytrader.swing_engine import SwingState
    st = _swing_engine.state if _swing_engine is not None else SwingState(os.path.join(cfg.state_dir, "swing_state.json"))
    for c in st.closed:
        et = c.get("exit_time") if isinstance(c, dict) else None
        if et and since_ts <= et < until_ts:
            trades.append({"name": c.get("name") or c.get("symbol"), "pnl": c.get("pnl"), "technique": c.get("entry_technique"), "reason": c.get("reason"),
                           "adds": c.get("adds"), "scaled_out": c.get("scaled_out"),
                           "entry_price": c.get("entry_price"), "exit_price": c.get("exit_price")})
    return trades, "won", bool(cfg.swing.mode == "live")


def build_period_review(period_label: str, since_date: str, until_date: str, since_ts: float, until_ts: float) -> str:
    from daytrader import daily_review as dr
    cfg = cfg_now()
    per_market = {}
    for market in ("domestic", "overseas", "crypto", "swing"):
        trades, unit, live = _period_review_data(market, cfg, since_date, until_date, since_ts, until_ts)
        per_market[market] = (dr.stats(trades), unit, live)
    return dr.compose_period(period_label, per_market)


def _send_period_review(period_label: str, since_date: str, until_date: str, since_ts: float, until_ts: float) -> tuple:
    from daytrader import notify
    cfg = cfg_now()
    text = build_period_review(period_label, since_date, until_date, since_ts, until_ts)
    return notify.Telegram(cfg).send_now(text)


_review_started = False
_maintenance_started = False


def _db_maintenance_loop() -> None:
    """하루 한 번 보관기간(기본 90일)을 넘긴 app_log 를 지우고, 일요일마다 VACUUM/ANALYZE 한다.
    ★ 실패해도(장중 잠김 등) 예외를 삼키고 다음 시각에 다시 시도한다 - 유지보수가 매매를 막으면 안 된다."""
    from datetime import datetime as _dt
    from daytrader import applog, db
    last_prune_date = None
    last_vacuum_date = None
    while True:
        try:
            cfg = cfg_now()
            today = time.strftime("%Y-%m-%d")
            if today != last_prune_date:
                removed = applog.prune(cfg.state_dir)
                if removed:
                    logging.getLogger(__name__).info("오래된 app_log %d건을 정리했습니다.", removed)
                last_prune_date = today
            # ★ 일요일에만 VACUUM - db 파일을 통째로 다시 쓰는 무거운 작업이라 자주 돌 필요는 없다.
            if _dt.now().weekday() == 6 and today != last_vacuum_date:
                db.vacuum_analyze(cfg.state_dir)
                last_vacuum_date = today
        except Exception:
            logging.getLogger(__name__).exception("DB 유지보수 루프 오류")
        time.sleep(3600)


def _overseas_price_client():
    """실제 지수·주가 조회용 클라이언트. 엔진이 돌고 있으면 그걸 재사용하고, 아니면 같은
    계좌(TossClient)를 새로 연다 - 시장 평가는 항상 실제 시세를 봐야 하므로 국내 get_client()
    와 달리 sim/replay 라우팅을 타지 않는다."""
    if _overseas_engine is not None and getattr(_overseas_engine, "client", None) is not None:
        return _overseas_engine.client
    cfg = cfg_now()
    if not (cfg.client_id and cfg.client_secret):
        return None
    from daytrader.tossapi import TossClient
    return TossClient(cfg.client_id, cfg.client_secret)


def build_market_review(market: str = "domestic") -> str | None:
    """오늘의 시장 평가(market_commentary.py) - 내 매매가 아니라 시장 자체에 대한 실제
    지수·주가와 외부 뉴스·증권사 코멘트를 Groq 로 요약한다. 뉴스·시세가 둘 다 없거나
    Groq 를 못 쓰면 None."""
    from daytrader import market_commentary as mc
    cfg = cfg_now()
    feed = get_news_feed()
    feed.fetch()
    headlines = mc.gather_headlines(feed, market=market)
    price_snapshot = None
    if market == "overseas":
        client = _overseas_price_client()
        if client is not None:
            try:
                price_snapshot = mc.gather_price_snapshot(client)
            except Exception:
                logging.getLogger(__name__).exception("해외 시장 평가용 시세 조회 실패")
                price_snapshot = None
    return mc.compose(cfg, headlines, market=market, price_snapshot=price_snapshot)


def _market_review_unavailable_reason(cfg) -> str:
    """build_market_review() 가 None 을 돌려줬을 때 화면·로그에 보여줄 문구.
    ★★★ 예전에는 이 사유가 항상 "Groq 키가 등록되어 있지 않습니다" 하나뿐이었다 - 키를 이미
    등록해 두고도 두 키가 모두 한도 초과·인증 오류로 막힌 날에도 똑같이 "키가 없다"고 나와
    사용자가 잘못된 곳(설정 화면에서 키를 다시 넣는 것)을 고치게 만들었다. llm.status() 의
    실제 실패 사유(예: "한도 초과(429)")를 등록 여부와 구분해서 보여준다."""
    from daytrader import llm
    if not llm.available(cfg):
        return "Groq 키가 등록되어 있지 않습니다([설정] → 속보에서 Groq 키를 등록하세요)."
    err = (llm.status().get("last_error") or "").strip()
    if err:
        return f"Groq 를 지금 쓸 수 없습니다({_redact(err)}) - 규칙 기반으로 넘어가는 뉴스 필터와 달리, 시장 평가는 Groq 없이는 만들 수 없어 건너뜁니다."
    return "오늘 참고할 뉴스·시세 자료가 없거나 Groq 응답이 비어 있습니다."


def _send_market_review(market: str = "domestic") -> tuple:
    from daytrader import notify
    from daytrader import market_commentary as mc
    cfg = cfg_now()
    text = build_market_review(market)
    if not text:
        return False, _market_review_unavailable_reason(cfg)
    ok, err = notify.Telegram(cfg).send_now(text)
    if ok:
        mc.save_review(cfg, market, text)
    return ok, err


def _review_loop() -> None:
    """1분마다 확인해 트리거 시각이 지났고 아직 안 보낸 복기를 보낸다. 서버를 다시 켜도 같은 복기를 두 번 보내지 않는다."""
    from datetime import datetime as _dt
    from daytrader import daily_review as dr
    from daytrader import market_commentary as mc
    from daytrader.overseas_engine import us_market_holidays
    from daytrader.timeutil import KST
    while True:
        try:
            cfg = cfg_now()
            if cfg.notify.telegram_token and cfg.notify.telegram_chat_id:
                sent_log = dr.SentLog(os.path.join(cfg.state_dir, "review_sent.json"))
                now = _dt.now(KST)

                def kr_day(d):
                    return d.weekday() < 5  # 휴장일에는 거래·보유가 없어 아래에서 자연히 건너뛴다

                def us_day(d):
                    return d.weekday() < 5 and d not in us_market_holidays(d.year)

                if cfg.notify.daily_review:
                    for market, key, label in dr.due_markets(now, cfg, sent_log.load(), us_trading_day=us_day, kr_trading_day=kr_day):
                        trades, positions, *_ = _review_data(market, cfg, now)
                        if trades or positions:  # 아무 일도 없던 날은 보내지 않는다
                            ok, err = _send_review(market, label)
                            if not ok:
                                logging.getLogger(__name__).warning("일일 복기 전송 실패(%s): %s", market, _redact(err))
                                continue
                        sent_log.mark(key)

                # ★ "전체 주, 월 보내라고" - daily_review 스위치와 무관하게, weekly_review_enabled/
                # monthly_review_enabled 를 각각 따로 켜고 끈다(due_periodic() 내부에서 판정).
                for period_kind, key, label, since_date, until_date, since_ts, until_ts in dr.due_periodic(now, cfg, sent_log.load()):
                    ok, err = _send_period_review(label, since_date, until_date, since_ts, until_ts)
                    if not ok:
                        logging.getLogger(__name__).warning("%s 복기 전송 실패: %s", period_kind, _redact(err))
                        continue
                    sent_log.mark(key)

                # ★ "본장이 끝나고 시장에 대한 평가를... 텔레그램으로" - 내 매매 복기와는 별개
                # 스위치. 국내(본장 마감, market_review_time)·해외(뉴욕 마감, overseas_review_time
                # 재사용 - daily_review 의 해외 복기와 같은 시각) 둘 다 이 스위치 하나로 켠다.
                if getattr(cfg.notify, "market_review_enabled", False):
                    for mkt in ("domestic", "overseas"):
                        due = mc.due(now, cfg, sent_log.load(), kr_trading_day=kr_day, us_trading_day=us_day, market=mkt)
                        if due is None:
                            continue
                        key, label = due
                        ok, err = _send_market_review(mkt)
                        if not ok:
                            # ★ 뉴스가 없거나 Groq 를 못 쓰는 날은 "실패"가 아니라 "낼 게 없음" -
                            # 매일 같은 사유로 재시도하지 않게 그래도 보낸 것으로 표시한다.
                            logging.getLogger(__name__).info("오늘의 시장 평가 건너뜀(%s): %s", label, _redact(err))
                        sent_log.mark(key)
        except Exception:
            logging.getLogger(__name__).exception("일일 복기 루프 오류")
        time.sleep(60)


class ReviewSendIn(BaseModel):
    market: str = "domestic"


@app.post("/api/review/daily/send", dependencies=[_CONFIRM_SETTINGS])
@api_guard
def send_daily_review(body: ReviewSendIn):
    """오늘 복기를 지금 텔레그램으로 보낸다(확인용)."""
    if body.market not in ("domestic", "overseas", "crypto"):
        raise HTTPException(status_code=400, detail="market 은 domestic · overseas · crypto 중 하나여야 합니다.")
    ok, err = _send_review(body.market)
    if not ok:
        raise HTTPException(status_code=502, detail=_redact(err) or "전송하지 못했습니다.")
    return {"ok": True}


@app.get("/api/review/daily/preview")
@api_guard
def preview_daily_review(market: str = "domestic"):
    if market not in ("domestic", "overseas", "crypto"):
        raise HTTPException(status_code=400, detail="market 은 domestic · overseas · crypto 중 하나여야 합니다.")
    return {"text": build_daily_review(market)}


@app.post("/api/review/market/send", dependencies=[_CONFIRM_SETTINGS])
@api_guard
def send_market_review(market: str = "domestic"):
    """오늘의 시장 평가(실제 지수·주가 + 외부 뉴스·증권사 코멘트 Groq 요약)를 지금 텔레그램으로
    보낸다(확인용). 성공하면 history 에도 남는다."""
    if market not in ("domestic", "overseas"):
        raise HTTPException(status_code=400, detail="market 은 domestic · overseas 중 하나여야 합니다.")
    ok, err = _send_market_review(market)
    if not ok:
        raise HTTPException(status_code=502, detail=_redact(err) or "전송하지 못했습니다(뉴스·시세가 없거나 Groq 를 쓸 수 없습니다).")
    return {"ok": True}


@app.get("/api/review/market/preview")
@api_guard
def preview_market_review(market: str = "domestic"):
    if market not in ("domestic", "overseas"):
        raise HTTPException(status_code=400, detail="market 은 domestic · overseas 중 하나여야 합니다.")
    text = build_market_review(market)
    if not text:
        return {"text": "", "reason": _market_review_unavailable_reason(cfg_now())}
    return {"text": text}


@app.get("/api/review/market/history")
@api_guard
def get_market_review_history(market: str = "", limit: int = 60):
    """★★★ "정리해서 보낸 내용을 프로그램에서 일자 시간별로 볼수 있게" 요청 - 그동안 보낸
    시장 평가를 최신순으로 돌려준다. market 을 비우면 국내·해외 전부."""
    from daytrader import market_commentary as mc
    cfg = cfg_now()
    rows = mc.load_reviews(cfg, market=market or None, limit=limit)
    return {"rows": rows}


def _active_notifier():
    """돌고 있는 엔진이 있으면 그 notifier(발송 횟수 등 상태 보유)를 쓴다."""
    return runner.engine.notifier if runner.engine is not None else None


@app.get("/api/notify")
@api_guard
async def get_notify():
    from daytrader import notify
    cfg = cfg_now()
    notifier = _active_notifier()
    if notifier is not None:
        return notifier.status()
    return notify.status_cfg(cfg)


@app.post("/api/notify/test")
@api_guard
async def notify_test(body: NotifyTestIn, request: Request):
    """★★ A-26. enabled() 로 판정하면 연습 모드에서는 연결 확인 자체가
    영구히 불가능해진다. 값이 채워졌는지만 본다.

    ★★★ 실제로 겪은 문제("등록했는데 테스트 전송이 안 된다") - 보안상
    입력칸은 저장 후 비워진다(저장된 토큰을 화면에 다시 뿌리지 않는다).
    그런데 여기서 빈 값이면 400 을 냈으니, 저장하고 바로 테스트를 누르면
    반드시 실패했다. 입력칸이 비어 있으면 저장된 값으로 시험한다.

    ★★★ [2-4] 실제로 겪을 수 있는 구멍 - 이 API 는 몸체로 받은 토큰·채팅ID 를 그대로 써서
    서버가 대신 외부(api.telegram.org)로 요청을 보낸다. 세션 쿠키만 있으면(재확인 없이도)
    호출할 수 있었던 예전 버전은, 탈취된 세션 쿠키 하나로 서버를 시켜 "아무 봇 토큰·채팅 ID"로나
    메시지를 보낼 수 있는 통로였다(스팸 발송대·서버의 공인 IP 확인용 SSRF 성 악용 등). 저장된
    값으로 시험할 때는 이미 그 값 자체가 재확인을 거쳐 저장됐으니 그대로 두되, 호출자가 새
    토큰·채팅ID 를 직접 넣어 시험하려는 경우에는 다른 설정 변경과 같은 수준(재확인 토큰)을 요구한다.
    """
    from daytrader import notify
    cfg = cfg_now()
    caller_token = (body.token or "").strip()
    caller_chat_id = (body.chat_id or "").strip()
    if caller_token or caller_chat_id:
        _require_confirm("settings")(request)
    token = caller_token or (cfg.notify.telegram_token or "").strip()
    chat_id = caller_chat_id or (cfg.notify.telegram_chat_id or "").strip()
    if not (token and chat_id):
        raise HTTPException(
            400,
            "토큰과 채팅 ID 가 없습니다 - 위 입력칸에 넣고 '저장'을 먼저 누르거나, "
            "입력칸에 값을 채운 뒤 다시 시도하세요.",
        )

    ok, err = notify._post_raw_sync(token, chat_id, "✅ AutoDayTrading 연결 확인 메시지입니다.")
    if not ok:
        raise HTTPException(400, f"전송에 실패했습니다: {err}")

    message = "테스트 메시지를 보냈습니다."
    if not cfg.is_live:
        message += (
            " 다만 지금은 실거래 모드가 아니므로 매매·마감 알림은 자동으로 나가지 않습니다 - "
            "시뮬레이션 매매는 기록만 남습니다."
        )
    return {"ok": True, "message": message}


class TossTestIn(BaseModel):
    symbol: str = "005930"


class BithumbTestIn(BaseModel):
    access_key: str = ""
    secret_key: str = ""
    market: str = "KRW-BTC"


@app.post("/api/bithumb/test")
@api_guard
async def bithumb_test_route(body: BithumbTestIn):
    """★★ 읽기만 한다 - 주문(place_order)은 절대 부르지 않는다.
    ★ 입력칸에 값이 있으면 저장 전 값으로 시험한다. 비어 있으면 저장된 값을 쓴다.
    """
    from daytrader import selftest
    cfg = cfg_now()
    access_key = body.access_key.strip() or cfg.bithumb_access_key
    secret_key = body.secret_key.strip() or cfg.bithumb_secret_key
    return selftest.bithumb_test(access_key, secret_key, body.market)


BITHUMB_WATCHLIST = ["KRW-BTC", "KRW-ETH", "KRW-XRP"]
BITHUMB_LABELS = {"KRW-BTC": "비트코인", "KRW-ETH": "이더리움", "KRW-XRP": "리플"}


_crypto_engine = None  # ★ 실주문 엔진은 하나뿐 - 전역 슬롯 하나만 둔다(원칙 11).
_overseas_engine = None
_swing_engine = None


class OverseasStartIn(BaseModel):
    confirm: str = ""


@app.post("/api/overseas/start", dependencies=[_CONFIRM_TRADE])
@api_guard
async def overseas_start(body: OverseasStartIn):
    global _overseas_engine
    from daytrader.overseas_engine import OverseasEngine

    if _overseas_engine is not None and _overseas_engine.is_running():
        raise HTTPException(status_code=409, detail="이미 해외주식 매매가 진행 중입니다.")

    cfg = cfg_now()
    if not cfg.overseas.enabled:
        raise HTTPException(status_code=400, detail="[설정] → 거래선택에서 해외주식 사용을 먼저 켜야 합니다.")

    if cfg.overseas.mode == "live" and (cfg.client_id and cfg.client_secret):
        # ★★ 실거래는 확인 문구가 반드시 필요하다 - 국내주식·암호화폐와 같은 원칙.
        if body.confirm != "실매매":
            raise HTTPException(status_code=400, detail="실거래를 시작하려면 확인 문구('실매매')가 필요합니다.")

    # ★★★ "계좌도 동일" - 국내주식과 같은 get_client() 를 그대로 넘긴다.
    _overseas_engine = OverseasEngine(cfg, client=get_client())
    _overseas_engine.start()
    return {"ok": True, "is_live": _overseas_engine.is_live}


class OverseasStopIn(BaseModel):
    close_positions: bool = False


@app.post("/api/overseas/stop", dependencies=[_CONFIRM_TRADE])
@api_guard
async def overseas_stop(body: OverseasStopIn = OverseasStopIn()):
    global _overseas_engine
    if _overseas_engine is None:
        return {"ok": True, "closed": 0}
    closed = 0
    if body.close_positions:
        closed = _overseas_engine.liquidate_all()
    _overseas_engine.request_stop()
    return {"ok": True, "closed": closed}


_us_theme_cache: dict = {"at": 0.0, "report": None, "last_try": 0.0}
_SYMBOL_RE = {
    "domestic": re.compile(r"^[A-Za-z0-9]{1,12}$"),
    "overseas": re.compile(r"^[A-Za-z0-9.\-]{1,10}$"),
    "crypto": re.compile(r"^[A-Z0-9]{2,10}-[A-Z0-9]{2,10}$"),
}


def _check_symbol(market: str, symbol: str) -> str:
    """★ 경로로 들어온 종목 식별자는 외부 시세 API 로 그대로 넘어가므로 형식을 먼저 검증한다."""
    if not _SYMBOL_RE[market].match(symbol or ""):
        raise HTTPException(status_code=400, detail="종목 식별자 형식이 올바르지 않습니다.")
    return symbol



@app.get("/api/overseas/selection")
@api_guard
def overseas_selection(refresh: int = 0):
    """미국 테마주 자동 산정 결과 + 직접 추가한 관심 종목. 엔진이 돌고 있으면 엔진이 뽑은 것을 그대로,
    꺼져 있으면 10분 캐시로 한 번 평가한다(주말·휴장일에는 평가하지 않는다)."""
    from datetime import datetime as _dt
    from daytrader.overseas_engine import us_session
    cfg = cfg_now()
    oc = cfg.overseas
    out = {
        "theme_select": bool(getattr(oc, "theme_select", False)), "watchlist": list(oc.watchlist or []),
        "session": us_session(_dt.now().astimezone()), "report": None, "idle": "",
    }
    if not out["theme_select"]:
        out["idle"] = "테마 자동 선정이 꺼져 있습니다 - [설정] → 해외주식에서 켤 수 있습니다."
        return out
    if _overseas_engine is not None and _overseas_engine.theme_report() is not None:
        out["report"] = _overseas_engine.theme_report()
        return out
    now = time.time()
    # ★ refresh 를 연달아 눌러도 외부 랭킹 API 를 두드리지 않게 최소 30초 간격을 둔다.
    if now - _us_theme_cache["last_try"] < 30 and _us_theme_cache["report"] is not None:
        out["report"] = _us_theme_cache["report"]
        return out
    if _us_theme_cache["report"] is not None and now - _us_theme_cache["at"] < 600 and not refresh:
        out["report"] = _us_theme_cache["report"]
        return out
    if out["session"] == "closed" and _us_theme_cache["report"] is None and not refresh:
        out["idle"] = "지금은 미국 주말·휴장일이라 테마를 평가하지 않습니다 - 거래가 열리면 다시 뽑습니다."
        return out
    from daytrader.us_themes import scan_us_themes
    _us_theme_cache["last_try"] = now
    try:
        rep = scan_us_themes(get_client(), oc, cfg)
    except Exception as exc:
        out["idle"] = f"미국 테마를 평가하지 못했습니다: {_redact(exc)}"
        return out
    if rep.get("market_size"):
        _us_theme_cache.update(at=now, report=rep)
    out["report"] = rep
    return out


@app.get("/api/overseas/status")
@api_guard
def overseas_status():
    """★★★ [8-1] 실제로 겪은 버그(py-spy 로 확인) - 이 라우트가 async def 였는데,
    엔진이 꺼져 있을 때 _usd_krw_rate() 가 (캐시가 비었으면) market.snapshot() 을
    동기(블로킹)로 호출한다 - 엔진이 켜져 있어도 _overseas_engine.snapshot() 안의
    _safe_usd_krw_rate() 가 같은 경로를 탄다. await 로 스레드에 넘기지 않고
    async def 안에서 그대로 부르면, 그 네트워크 호출이 끝날 때까지 서버 전체
    (다른 모든 요청)가 멈춘다. 이 라우트는 await 를 쓰지 않으므로 일반 def 로
    바꿔 FastAPI 가 스레드풀(run_in_threadpool)에서 돌리게 한다 - 동작은 그대로.
    """
    if _overseas_engine is not None:
        return _overseas_engine.snapshot()
    # ★★★ 실제로 겪은 버그 - 엔진을 아직 시작 안 했거나 정지한 상태에서
    # {"running": False} 만 주면, 대시보드의 "감시 중(후보)"·"보유" 카드가
    # watchlist/positions 를 아예 못 받아 텅 비어 보인다("후보 리스트
    # 누락"). 엔진이 꺼져 있어도 지금 설정된 감시목록과, 마지막으로 저장된
    # 보유·청산 기록은 있는 그대로 보여줘야 한다.
    from daytrader.overseas_engine import OverseasState, _usd_krw_rate, _FALLBACK_USD_KRW
    cfg = cfg_now()
    path = os.path.join(cfg.state_dir, "overseas_state.json")
    state = OverseasState(path)
    positions = {
        s: {
            "quantity": p.quantity, "entry_price": p.entry_price,
            "peak_price": p.peak_price, "technique": p.technique,
            "held_hours": round((time.time() - p.entry_time) / 3600.0, 2),
        }
        for s, p in state.book.all().items()
    }
    # ★ 엔진이 꺼져 있어도 환율 표시는 계속 필요하다 - 실패하면 기본값.
    try:
        usd_krw_rate = _usd_krw_rate()
    except Exception:
        usd_krw_rate = _FALLBACK_USD_KRW
    return {
        "running": False, "is_live": False, "mode": cfg.overseas.mode,
        "cash": None, "positions": positions,
        "closed_count": len(state.closed), "closed": state.closed[-10:],
        "loop_count": 0, "last_error": "",
        "usd_krw_rate": usd_krw_rate,
        # ★★★ 실제로 겪은 버그 - 이 폴백(엔진이 아직 시작 전이거나 정지한
        # 상태)이 auto_select 여부를 전혀 반영하지 않고 항상 고정
        # watchlist 만 돌려줬다. 자동 선정을 켜 놔도, 엔진을 실제로 시작
        # 하기 전까지는 오늘 무엇이 뽑힐지 아직 정해지지 않은 게 사실이니
        # (랭킹 조회는 run_once() 안에서 딱 한 번 일어난다), 여기서 새로
        # API 를 부르는 대신 "자동 선정이 켜져 있다"는 사실 자체를
        # 정확히 알려준다 - 화면이 이 필드를 보고 정확한 안내를 낼 수 있다.
        "watchlist": cfg.overseas.watchlist,
        "auto_select": getattr(cfg.overseas, "auto_select", False),
    }


def _tag_overseas_session(rows: list) -> list:
    """★★★ "매매실적 조회시 장 세션별로도 조회할수 있게해" 요청 - 해외주식 청산 기록에
    미국 세션(프리장/본장/애프터장/데이장)을 us_phase() 로 붙인다. exit_time 이 없는
    (아직 청산 안 된) 기록은 entry_time 으로 대신한다. 둘 다 time.time() 기반 epoch
    실수라, overseas_engine.halt_info() 가 세션 전환 재개 판정에 쓰는 것과 같은 방식
    (datetime.fromtimestamp(...).astimezone())으로 시간대를 붙인다.
    ★ dict(row) 로 얕은 복사를 해서 붙인다 - 원본은 엔진이 들고 있는 state.closed
    그 자체라, 그대로 건드리면 다음 save() 때 이 계산값이 상태 파일에 그대로
    저장돼 버린다."""
    from datetime import datetime as _dt
    from daytrader.overseas_engine import us_phase
    out = []
    for row in rows:
        row = dict(row)
        try:
            ts = row.get("exit_time") or row.get("entry_time")
            row["session"] = us_phase(_dt.fromtimestamp(float(ts)).astimezone()) if ts else None
        except Exception:
            row["session"] = None
        out.append(row)
    return out


@app.get("/api/overseas/journal")
@api_guard
async def overseas_journal():
    """★ 해외주식 청산 내역 - 코인과 같은 방식: 엔진이 꺼져 있어도 상태 파일에서 직접 읽는다."""
    if _overseas_engine is not None:
        return {"rows": _tag_overseas_session(list(reversed(_overseas_engine.state.closed)))}
    from daytrader.overseas_engine import OverseasState
    cfg = cfg_now()
    path = os.path.join(cfg.state_dir, "overseas_state.json")
    state = OverseasState(path)
    return {"rows": _tag_overseas_session(list(reversed(state.closed)))}


class CryptoStartIn(BaseModel):
    confirm: str = ""


@app.post("/api/crypto/start", dependencies=[_CONFIRM_TRADE])
@api_guard
async def crypto_start(body: CryptoStartIn):
    global _crypto_engine
    from daytrader.crypto_engine import CryptoEngine

    if _crypto_engine is not None and _crypto_engine.is_running():
        raise HTTPException(status_code=409, detail="이미 코인 매매가 진행 중입니다.")

    cfg = cfg_now()
    if not cfg.crypto.enabled:
        raise HTTPException(status_code=400, detail="[설정] → 코인에서 사용을 먼저 켜야 합니다.")

    if cfg.crypto.live and (cfg.bithumb_access_key and cfg.bithumb_secret_key):
        # ★★ 실거래는 확인 문구가 반드시 필요하다 - 국내주식과 같은 원칙.
        # ★ cfg.crypto.live 는 국내주식 mode 와 완전히 독립된 스위치다.
        if body.confirm != "실매매":
            raise HTTPException(status_code=400, detail="실거래를 시작하려면 확인 문구('실매매')가 필요합니다.")

    _crypto_engine = CryptoEngine(cfg)
    _crypto_engine.start()
    return {"ok": True, "is_live": _crypto_engine.is_live}


class CryptoStopIn(BaseModel):
    close_positions: bool = False


@app.post("/api/crypto/stop", dependencies=[_CONFIRM_TRADE])
@api_guard
async def crypto_stop(body: CryptoStopIn = CryptoStopIn()):
    global _crypto_engine
    if _crypto_engine is None:
        return {"ok": True, "closed": 0}
    closed = 0
    if body.close_positions:
        # ★ 정지 신호를 보내기 전에 먼저 청산한다 - 순서를 바꾸면 청산 도중에
        # 루프가 멈춰 일부만 정리될 수 있다.
        closed = _crypto_engine.liquidate_all()
    _crypto_engine.request_stop()
    return {"ok": True, "closed": closed}


@app.get("/api/crypto/status")
@api_guard
async def crypto_status():
    if _crypto_engine is not None:
        return _crypto_engine.snapshot()
    # ★★★ 해외주식과 같은 이유로 같은 방식을 쓴다 - 엔진이 꺼져 있어도
    # 감시목록·보유·청산 기록은 있는 그대로 보여줘야 한다.
    from daytrader.crypto_engine import CryptoState
    cfg = cfg_now()
    path = os.path.join(cfg.state_dir, "crypto_state.json")
    state = CryptoState(path)
    positions = {
        m: {
            "quantity": p.quantity, "entry_price": p.entry_price,
            "peak_price": p.peak_price, "technique": p.technique,
            "held_hours": round((time.time() - p.entry_time) / 3600.0, 2),
        }
        for m, p in state.book.all().items()
    }
    return {
        "running": False, "is_live": False, "mode": cfg.crypto.mode,
        "cash": None, "positions": positions,
        "closed_count": len(state.closed), "closed": state.closed[-10:],
        "loop_count": 0, "last_error": "",
        "closed_today": __import__("daytrader.crypto_engine", fromlist=["_closed_today"])._closed_today(state.closed),
        "watchlist": cfg.crypto.watchlist,
        # 엔진이 시작하면 전날 거래대금 상위 종목이 더해진다 - 화면이 그 사실을 안내한다.
        "auto_top_volume": bool(getattr(cfg.crypto, "auto_top_volume", False)),
        "top_volume_count": getattr(cfg.crypto, "top_volume_count", 10),
        # ★ 엔진이 꺼져 있어도 "무엇을 감시할 예정인지"는 보여줘야 한다.
        # Playbook 을 만들지 않고 설정값에서 라벨만 뽑는다(가벼운 조회이므로).
        "entry_techniques": _technique_labels(cfg.crypto.entry_order),
    }


@app.get("/api/crypto/journal")
@api_guard
async def crypto_journal():
    """★ 코인 청산 내역 - 매매일지 화면에서 함께 보여준다.
    국내주식 journal.py 와는 완전히 별도 파일(crypto_state.json)에서 온다.
    ★ 엔진이 지금 꺼져 있어도 상태 파일에서 직접 읽어 과거 기록을 보여준다 -
    "시작 버튼을 안 눌러서 기록이 안 보인다"는 혼란을 막는다.
    """
    if _crypto_engine is not None:
        return {"rows": list(reversed(_crypto_engine.state.closed))}
    from daytrader.crypto_engine import CryptoState
    cfg = cfg_now()
    path = os.path.join(cfg.state_dir, "crypto_state.json")
    state = CryptoState(path)
    return {"rows": list(reversed(state.closed))}


class SwingStartIn(BaseModel):
    confirm: str = ""


@app.post("/api/swing/start", dependencies=[_CONFIRM_TRADE])
@api_guard
async def swing_start(body: SwingStartIn):
    global _swing_engine
    from daytrader.swing_engine import SwingEngine

    if _swing_engine is not None and _swing_engine.is_running():
        raise HTTPException(status_code=409, detail="이미 스윙 매매가 진행 중입니다.")

    cfg = cfg_now()
    if not cfg.swing.enabled:
        raise HTTPException(status_code=400, detail="[설정] → 거래선택에서 스윙 사용을 먼저 켜야 합니다.")

    if cfg.swing.mode == "live":
        # ★ 스윙 실거래는 아직 지원하지 않는다(daytrader/swing_broker.py 의 LiveSwingBroker
        # 설명 참고 - 재시작 대조 안전장치가 아직 없다). web(관찰)·paper(모의매매)로 충분히
        # 검증한 뒤에 추가한다.
        raise HTTPException(status_code=400, detail="스윙 실거래는 아직 지원하지 않습니다. web(관찰) 또는 paper(모의매매)로 설정하세요.")

    _swing_engine = SwingEngine(cfg, client=get_client())
    _swing_engine.start()
    return {"ok": True, "is_live": _swing_engine.is_live}


class SwingStopIn(BaseModel):
    close_positions: bool = False


@app.post("/api/swing/stop", dependencies=[_CONFIRM_TRADE])
@api_guard
async def swing_stop(body: SwingStopIn = SwingStopIn()):
    global _swing_engine
    if _swing_engine is None:
        return {"ok": True, "closed": 0}
    closed = 0
    if body.close_positions:
        closed = _swing_engine.liquidate_all()
    _swing_engine.request_stop()
    return {"ok": True, "closed": closed}


@app.get("/api/swing/status")
@api_guard
async def swing_status():
    if _swing_engine is not None:
        return _swing_engine.snapshot()
    # ★ 다른 세 시장과 같은 이유 - 엔진이 꺼져 있어도 보유·청산 기록은 있는 그대로 보여준다.
    from daytrader.swing_engine import SwingState
    cfg = cfg_now()
    path = os.path.join(cfg.state_dir, "swing_state.json")
    state = SwingState(path)
    positions = {
        s: {
            "name": p.name, "theme": p.theme, "quantity": p.quantity, "entry_price": p.entry_price,
            "peak_price": p.peak_price, "technique": p.technique,
            "held_days": round((time.time() - p.entry_time) / 86400.0, 2) if p.entry_time else 0.0,
        }
        for s, p in state.book.all().items()
    }
    return {
        "running": False, "is_live": False, "mode": cfg.swing.mode,
        "cash": None, "positions": positions,
        "closed_count": len(state.closed), "closed": state.closed[-10:],
        "loop_count": 0, "last_error": "",
        "candidates": [],
        "entry_techniques": _technique_labels(cfg.swing.entry_order),
    }


def _tag_swing_session(rows: list) -> list:
    """스윙은 국내·해외 종목만 세션 개념이 있다(코인은 24시간 거래라 세션 자체가 없다,
    스윙은 며칠~몇주씩 들고 가서 국내·해외도 "세션 하나"라는 태그가 딱 들어맞진 않지만
    청산이 일어난 순간의 국면은 여전히 뜻이 있다) - record 의 market 태그로 갈라 각각
    domestic_phase()/us_phase() 를 부른다. 코인 레그는 session 자체를 안 붙인다."""
    from datetime import datetime as _dt
    from daytrader.overseas_engine import us_phase
    from daytrader.session import domestic_phase
    from daytrader.timeutil import to_kst
    out = []
    for row in rows:
        row = dict(row)
        market = row.get("market")
        if market in ("domestic", "overseas"):
            try:
                ts = row.get("exit_time") or row.get("entry_time")
                if ts:
                    aware = _dt.fromtimestamp(float(ts)).astimezone()
                    row["session"] = domestic_phase(to_kst(aware)) if market == "domestic" else us_phase(aware)
                else:
                    row["session"] = None
            except Exception:
                row["session"] = None
        out.append(row)
    return out


@app.get("/api/swing/journal")
@api_guard
async def swing_journal():
    """★ 스윙 청산 내역 - 매매일지 화면에서 함께 보여준다. 국내 단타 journal.py 와는 완전히
    별도 파일(swing_state.json)에서 온다."""
    if _swing_engine is not None:
        return {"rows": _tag_swing_session(list(reversed(_swing_engine.state.closed)))}
    from daytrader.swing_engine import SwingState
    cfg = cfg_now()
    path = os.path.join(cfg.state_dir, "swing_state.json")
    state = SwingState(path)
    return {"rows": _tag_swing_session(list(reversed(state.closed)))}


@app.get("/api/overseas/ticker")
@api_guard
def overseas_ticker():
    """★ 관심 종목의 시세만 관찰한다. 자동매매 로직은 없다.
    ★★ [8-1] get_client().stocks(...)·market_mod._fetch_yahoo_session_group(...) 모두
    실제 네트워크 호출(블로킹)이라, 원래 async def 로 돼 있으면 그동안 이벤트 루프
    전체가 멈춘다(overseas_status() 와 같은 문제). 일반 def 로 바꿔 FastAPI 가 스레드
    풀에서 돌리게 한다.
    """
    from daytrader import market as market_mod
    from daytrader import netutil
    from daytrader.timeutil import now_kst

    cfg = cfg_now()
    # ★★★ 실제로 겪은 버그 - 여기서 cfg.overseas.watchlist(고정 설정값)
    # 만 봤었다. 종목 자동 선정(auto_select)을 켜면 실제로 매매를 판단
    # 하는 종목은 매일 다시 뽑힌 목록인데, 이 시세 조회는 여전히 옛날
    # 고정 목록만 봐서 자동 선정된 종목의 실시간 시세를 종목선정 화면
    # 에서 볼 수 없었다. /api/overseas/status 가 이미 정확한 유효
    # 목록(엔진이 돌면 자동선정 결과, 아니면 고정 목록)을 계산해 주니
    # 그대로 재사용한다.
    status = overseas_status()
    watchlist = status.get("watchlist") or cfg.overseas.watchlist
    if not watchlist:
        return {"ok": True, "rows": [], "note": "관심 종목이 없습니다. [설정] → 해외주식에서 티커를 추가하세요."}

    sess = netutil.make_session()
    # ★★★ 실제로 겪은 문제 - 예전엔 items = [(t, t)] 로 티커를 라벨로
    # 그대로 써서 화면에 "AAPL (AAPL)" 처럼 종목명 없이 나왔다. 토스
    # stocks() 로 실제 종목명을 받아 라벨에 쓴다.
    # ★ 이름 조회가 실패해도 시세는 보여줘야 하니 티커로 안전하게 폴백한다.
    names = {}
    try:
        for row in (get_client().stocks(list(watchlist)) or []):
            sym = row.get("symbol")
            if sym and row.get("name"):
                names[sym] = row["name"]
    except Exception as exc:
        logging.getLogger(__name__).warning("해외주식 종목명 조회 실패(티커로 표시합니다): %s", exc)

    items = [(t, names.get(t, t)) for t in watchlist]
    rows = market_mod._fetch_yahoo_session_group(sess, items, now_kst())
    return {"ok": True, "rows": rows, "note": None}


@app.get("/api/bithumb/ticker")
@api_guard
def bithumb_ticker():
    """★ 인증이 필요 없는 공개 시세만 본다 - 계좌 조회 없이도 관찰할 수 있다.
    ★★ 아직 자동매매 로직은 없다. 이 화면은 관찰용이다.
    ★★ [8-1] BithumbClient.ticker() 는 실제 네트워크 호출(블로킹)이다 - async def 로
    두면 그동안 이벤트 루프 전체가 멈춘다. 일반 def 로 바꿔 FastAPI 가 스레드풀에서
    돌리게 한다.
    """
    from daytrader.bithumb_api import BithumbClient, BithumbApiError
    try:
        client = BithumbClient()
        rows = client.ticker(BITHUMB_WATCHLIST)
    except BithumbApiError as exc:
        return {"ok": False, "error": exc.message, "rows": []}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "rows": []}

    by_market = {r.get("market"): r for r in (rows or [])}
    out = []
    for market in BITHUMB_WATCHLIST:
        r = by_market.get(market, {})
        out.append({
            "market": market, "label": BITHUMB_LABELS.get(market, market),
            "last": r.get("trade_price"), "diff": r.get("signed_change_price"),
            "pct": r.get("signed_change_rate"), "volume": r.get("acc_trade_price_24h"),
        })
    return {"ok": True, "rows": out}


# ━━ 관심종목 검색(이름으로 찾아 추가) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★★★ "코드를 직접 타이핑해서 추가하던 걸, 이름으로 검색해서 고르게 해달라"
# (그것도 "종목명(코드)" 형태로 항상 이름을 먼저 보여 달라는 요청 두 번) - 시장별로
# 검색 가능한 자료가 다르다: 암호화폐는 빗썸이 전체 마켓 목록을 실시간으로 주지만,
# 국내주식은 이름으로 찾는 실시간 API 가 없어 themes.yaml 의 코드→이름 주석을
# 재사용하고, 해외주식은 그마저도 없어 자주 쓰이는 티커의 정적 목록을 둔다.
# 세 검색 모두 같은 모양({"code","name"})을 돌려주고, 프론트(forms.js)는 이걸
# 그대로 "이름(코드)" 로 조립해 보여준다.


def _search_code_name(items, q: str, limit: int = 20) -> list:
    """공통 검색·정렬 로직. items 는 (code, name) 쌍의 목록.
    정확 일치·시작 일치를 부분 일치보다 앞에 오게 해서, 흔한 이름/코드가
    검색어와 우연히 겹치는 다른 종목에 묻히지 않게 한다.
    """
    q = (q or "").strip().lower()
    scored = []
    for code, name in items:
        code_l = str(code).lower()
        name_l = str(name or "").lower()
        if q and q not in code_l and q not in name_l:
            continue
        if not q or code_l == q or name_l == q:
            score = 0
        elif code_l.startswith(q) or name_l.startswith(q):
            score = 1
        else:
            score = 2
        scored.append((score, code, name))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [{"code": c, "name": n} for _, c, n in scored[:limit]]


_crypto_markets_cache: dict = {"rows": None, "at": 0.0}


def _crypto_markets_cached() -> list:
    """★ 빗썸 전체 마켓 목록(/v1/market/all)은 자주 바뀌지 않는다 - 검색창에
    한 글자 칠 때마다 API 를 부르지 않도록 몇 분 캐시한다(overseas_engine.py
    _usd_krw_rate() 의 시간 기반 캐시와 같은 모양).
    """
    now = time.time()
    if _crypto_markets_cache["rows"] is not None and (now - _crypto_markets_cache["at"]) < 300:
        return _crypto_markets_cache["rows"]
    from daytrader.bithumb_api import BithumbClient
    rows = BithumbClient().markets() or []
    _crypto_markets_cache.update({"rows": rows, "at": now})
    return rows


@app.get("/api/crypto/markets/search")
@api_guard
async def search_crypto_markets(q: str = ""):
    """관심 코인 추가용 검색 - 빗썸 전체 마켓 코드/한글명/영문명 부분 일치."""
    try:
        rows = _crypto_markets_cached()
    except Exception:
        rows = []
    items = [(r.get("market", ""), r.get("korean_name") or r.get("english_name") or r.get("market", "")) for r in rows]
    return {"ok": True, "rows": _search_code_name(items, q)}


@app.get("/api/domestic/stocks/search")
@api_guard
async def search_domestic_stocks(q: str = ""):
    """관심 종목 추가용 검색 - themes.yaml 의 `# 종목명` 주석(load_theme_names)을
    코드→이름 자료로 재사용한다. 국내주식은 이름으로 찾는 실시간 API 가 없다.
    """
    from daytrader.screener import load_theme_names
    cfg = cfg_now()
    try:
        names = load_theme_names(cfg.themes_file)
    except Exception:
        names = {}
    return {"ok": True, "rows": _search_code_name(list(names.items()), q)}


# ★ 해외주식은 이름 검색 API 도, 로컬 이름 사전도 없었다 - 자주 거래되는 대형주·ETF
# 위주의 정적 목록이다(완전한 목록이라는 뜻은 아니다 - 여기 없는 티커는 이름 없이
# 코드만 보이거나 검색에 안 잡힐 수 있다. 필요하면 이 목록에 추가하면 된다).
OVERSEAS_STOCK_NAMES: dict = {
    # 기본 관심종목(OverseasCfg.watchlist 기본값)과 같은 종목들
    "AAPL": "Apple", "MSFT": "Microsoft", "GOOGL": "Alphabet(Google)", "AMZN": "Amazon",
    "NVDA": "NVIDIA", "META": "Meta Platforms", "TSLA": "Tesla", "AVGO": "Broadcom",
    "TSM": "Taiwan Semiconductor(TSMC)", "BRK.B": "Berkshire Hathaway",
    "AMD": "AMD", "MU": "Micron Technology", "ASML": "ASML Holding", "ARM": "Arm Holdings",
    "SMCI": "Super Micro Computer", "INTC": "Intel",
    "SPY": "SPDR S&P 500 ETF", "SMH": "VanEck Semiconductor ETF", "SOXX": "iShares Semiconductor ETF",
    # 그 외 널리 알려진 대형주·ETF
    "QQQ": "Invesco QQQ Trust", "DIA": "SPDR Dow Jones Industrial Average ETF",
    "IWM": "iShares Russell 2000 ETF", "VOO": "Vanguard S&P 500 ETF", "VTI": "Vanguard Total Stock Market ETF",
    "BRK.A": "Berkshire Hathaway(A)", "JPM": "JPMorgan Chase", "BAC": "Bank of America",
    "WFC": "Wells Fargo", "GS": "Goldman Sachs", "MS": "Morgan Stanley",
    "V": "Visa", "MA": "Mastercard", "PYPL": "PayPal", "NFLX": "Netflix", "DIS": "Disney",
    "CMCSA": "Comcast", "KO": "Coca-Cola", "PEP": "PepsiCo", "PG": "Procter & Gamble",
    "JNJ": "Johnson & Johnson", "PFE": "Pfizer", "MRK": "Merck", "UNH": "UnitedHealth Group",
    "LLY": "Eli Lilly", "ABBV": "AbbVie", "XOM": "ExxonMobil", "CVX": "Chevron",
    "WMT": "Walmart", "COST": "Costco", "HD": "Home Depot", "LOW": "Lowe's",
    "MCD": "McDonald's", "SBUX": "Starbucks", "NKE": "Nike",
    "ORCL": "Oracle", "CRM": "Salesforce", "ADBE": "Adobe", "IBM": "IBM", "CSCO": "Cisco",
    "QCOM": "Qualcomm", "TXN": "Texas Instruments", "NOW": "ServiceNow",
    "UBER": "Uber Technologies", "ABNB": "Airbnb", "SHOP": "Shopify", "SQ": "Block",
    "COIN": "Coinbase", "PLTR": "Palantir Technologies", "SNOW": "Snowflake",
    "PANW": "Palo Alto Networks", "CRWD": "CrowdStrike",
    "GE": "General Electric", "BA": "Boeing", "CAT": "Caterpillar", "DE": "Deere & Company",
    "F": "Ford Motor", "GM": "General Motors", "T": "AT&T", "VZ": "Verizon",
    "LIN": "Linde", "RTX": "RTX Corporation", "HON": "Honeywell",
}


@app.get("/api/overseas/stocks/search")
@api_guard
async def search_overseas_stocks(q: str = ""):
    """관심 종목 추가용 검색 - 정적 종목명 사전(OVERSEAS_STOCK_NAMES)에서 검색.
    라이브 검색 API 가 없어 최선 노력 수준의 로컬 목록이다.
    """
    return {"ok": True, "rows": _search_code_name(list(OVERSEAS_STOCK_NAMES.items()), q)}


@app.post("/api/toss/test")
@api_guard
async def toss_test_route(body: TossTestIn):
    """★★ 읽기만 한다 - 주문은 절대 내지 않는다."""
    from daytrader import selftest
    cfg = cfg_now()
    return selftest.toss_test(cfg, sample_symbol=body.symbol)


@app.post("/api/notify/credentials", dependencies=[_CONFIRM_SETTINGS])
@api_guard
async def notify_credentials(body: NotifyCredentialsIn):
    """★ 설정 전체가 아니라 이 두 값만 고친다. ★ _read_config_raw(round_trip=True)
    로 읽어야 주석이 살아남는다 (A-14). 빈 값도 받는다 - 지우는 것도 설정이다.
    """
    # ★★★ 예전에는 config.yaml 에 평문으로 넣어서, 설정 파일을 공유하면
    # 토큰이 함께 샜다(그래서 화면에 경고를 따로 붙여야 했다).
    # 이제 secrets.yaml 에만 둔다 - 그 파일 하나만 빼면 안전하다.
    from daytrader import secrets
    secrets.save({"telegram_token": body.token, "telegram_chat_id": body.chat_id})

    # ★★★ 실제로 겪은 사고 - 여기서 config.yaml 의 옛 값을 지우려고
    # 파일을 다시 썼는데, 주석을 보존하며 쓰는 과정에서 리스트 형식이
    # 깨져(watchlist: - AAPL) 설정 파일 전체를 못 읽게 됐다. 프로그램이
    # 아예 안 뜨는 상태가 된다.
    # ★ 옛 값을 지우는 것은 "있으면 좋은 정리"일 뿐인데, 그것 때문에
    #   설정 파일을 망가뜨릴 위험을 지는 건 맞지 않는다. 키는 이미
    #   secrets.yaml 에 저장됐고, config.yaml 의 값은 읽을 때 무시된다
    #   (secrets 쪽이 우선). 굳이 파일을 다시 쓰지 않는다.
    return {"ok": True}


@app.post("/api/notify/send")
@api_guard
async def notify_send(body: NotifySendIn):
    """사람이 직접 눌러 보내는 요약. ★ 어느 모드에서든 보낸다 - 대신 메시지의
    모드 표시에 '가상 성적입니다' 를 붙여 시뮬레이션 성적을 실제 수익으로
    오해할 수 없게 한다.
    """
    from daytrader import notify
    cfg = cfg_now()
    if not (cfg.notify.telegram_token and cfg.notify.telegram_chat_id):
        raise HTTPException(400, "토큰과 채팅 ID 를 먼저 [준비·연결] 에서 등록하세요.")

    tg = notify.Telegram(cfg)
    try:
        if body.kind == "now":
            if runner.engine is None:
                raise HTTPException(400, "보낼 현황이 없습니다 - 엔진이 돌고 있지 않습니다.")
            snap = runner.engine.snapshot()
            text = (
                f"📊 지금 현황 [{cfg.mode}" + ("" if cfg.is_live else " · 가상 성적입니다") + "]\n"
                f"실현손익 {snap.get('realized_pnl', 0):+,.0f}원 · 거래 {snap.get('trades', 0)}건 · "
                f"보유 {len(snap.get('positions', {}))}종목"
            )
            ok, err = tg.send_now(text)
        else:
            from daytrader.ledger import Ledger
            from daytrader.timeutil import day_str, now_kst
            ledger = Ledger(cfg.state_dir)
            today = day_str(now_kst())

            if body.kind == "daily":
                rows = ledger.daily()
                row = next((r for r in rows if r["date"] == today), None)
                if not row:
                    raise HTTPException(400, "오늘 거래 기록이 없습니다.")
                ok, err = tg.send_now(
                    f"📈 일마감 {today} [{cfg.mode}" + ("" if cfg.is_live else " · 가상 성적입니다") + "]\n"
                    f"손익 {row['pnl']:+,.0f}원 · 거래 {row['trades']}건 · 승률 {row['win_rate']*100:.0f}%"
                )
            elif body.kind == "monthly":
                rows = ledger.monthly()
                row = next((r for r in rows if r["month"] == today[:7]), None)
                if not row:
                    raise HTTPException(400, "이번 달 거래 기록이 없습니다.")
                ok, err = tg.send_now(
                    f"📈 월마감 {today[:7]} [{cfg.mode}" + ("" if cfg.is_live else " · 가상 성적입니다") + "]\n"
                    f"손익 {row['pnl']:+,.0f}원 · 거래 {row['trades']}건 · 승률 {row['win_rate']*100:.0f}%"
                )
            elif body.kind == "yearly":
                rows = ledger.yearly()
                row = next((r for r in rows if r["year"] == today[:4]), None)
                if not row:
                    raise HTTPException(400, "올해 거래 기록이 없습니다.")
                ok, err = tg.send_now(
                    f"🎯 연마감 {today[:4]} [{cfg.mode}" + ("" if cfg.is_live else " · 가상 성적입니다") + "]\n"
                    f"손익 {row['pnl']:+,.0f}원 · 거래 {row['trades']}건 · 승률 {row['win_rate']*100:.0f}%"
                )
            else:
                raise HTTPException(400, f"알 수 없는 종류입니다: {body.kind}")

        if not ok:
            raise HTTPException(400, f"전송에 실패했습니다: {err}")
        return {"ok": True}
    finally:
        tg.stop()


@app.get("/api/integrations")
@api_guard
def get_integrations():
    """토스·텔레그램·빗썸 자격증명 상태를 한 번에 준다 - 화면이 세 곳을 따로 물어보지 않는다."""
    from daytrader import netutil, notify, secrets
    cfg = cfg_now()
    diag = netutil.diagnose_cached()

    toss = {
        "configured": bool(cfg.client_id and cfg.client_secret),
        "client_id_tail": cfg.client_id[-4:] if cfg.client_id else "",
        # ★ 항목별 등록 여부 - 값 자체는 내려보내지 않는다(식별자만 끝 4자리).
        "client_id_set": bool(cfg.client_id), "client_secret_set": bool(cfg.client_secret),
        "env_path": ENV_PATH,
        "needed": cfg.needs_toss_key,
        "mode": cfg.mode, "mode_label": diag.get("mode_label", ""),
        "why": "" if cfg.client_id else "토스 API 키가 없으면 web 모드는 인터넷 시세로, paper/live 는 사용할 수 없습니다.",
    }
    bithumb = {
        "configured": bool(cfg.bithumb_access_key and cfg.bithumb_secret_key),
        "access_key_tail": cfg.bithumb_access_key[-4:] if cfg.bithumb_access_key else "",
        "access_key_set": bool(cfg.bithumb_access_key), "secret_key_set": bool(cfg.bithumb_secret_key),
    }
    llm = {"configured": bool(cfg.groq_api_key or cfg.groq_api_key2), "groq_set": bool(cfg.groq_api_key),
           "groq2_set": bool(cfg.groq_api_key2), "ai_filter": bool(cfg.news.ai_filter), "model": cfg.news.groq_model}
    telegram = notify.status_cfg(cfg)
    telegram.update({
        "token_set": bool(cfg.notify.telegram_token), "chat_id_set": bool(cfg.notify.telegram_chat_id),
        "chat_id_tail": (cfg.notify.telegram_chat_id or "")[-4:],
    })
    # ★ 실제 비밀번호 값은 절대 화면으로 내려보내지 않는다 - 기본값(123456)을
    # 그대로 쓰고 있는지만 알려준다. secrets.yaml 에 값이 저장돼 있는지가
    # 아니라 "지금 유효한 비밀번호가 기본값과 같은가"를 봐야 한다 -
    # 사용자가 굳이 123456 을 다시 입력해 저장해도 여전히 기본값이다.
    auth = {"is_default_password": _is_default_password()}
    return {"toss": toss, "telegram": telegram, "bithumb": bithumb, "llm": llm, "auth": auth}


# ━━ 월간 리뷰 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_REASON_LABELS = {
    "stop_loss": "손절", "take_profit": "익절", "trailing": "추적 손절",
    "time_stop": "시간 손절", "momentum_fade": "모멘텀 소멸", "force_close": "장 마감 강제청산",
    "기타": "기타",
}


class ReviewApplyIn(BaseModel):
    month: str
    group: str = "virtual"
    ids: list[str]
    note: str = ""


class ReviewRevertIn(BaseModel):
    id: str


def _review_technique_labels(cfg) -> dict:
    return {t["key"]: t["label"] for t in Playbook(cfg).describe()}


@app.get("/api/review/months")
@api_guard
async def review_months(group: str = "virtual"):
    from daytrader.ledger import Ledger, modes_in
    cfg = cfg_now()
    ledger = Ledger(cfg.state_dir)
    rows = ledger.monthly(modes=modes_in(group))
    return {"months": [r["month"] for r in rows], "rows": rows}


@app.get("/api/review/monthly")
@api_guard
async def review_monthly(month: str | None = None, group: str = "virtual"):
    from daytrader import review
    from daytrader.ledger import Ledger, modes_in
    cfg = cfg_now()
    ledger = Ledger(cfg.state_dir)
    modes = modes_in(group)
    trades = ledger.trades(modes=modes)

    if month is None:
        monthly_rows = ledger.monthly(modes=modes)
        if not monthly_rows:
            return {"stats": {"empty": True}, "proposals": [], "ideas": [], "applied": [],
                    "note": "거래 기록이 없습니다."}
        month = monthly_rows[-1]["month"]

    technique_labels = _review_technique_labels(cfg)
    stats = review.month_stats(trades, month, technique_labels, _REASON_LABELS)

    if stats["empty"]:
        return {"stats": stats, "proposals": [], "ideas": [], "applied": [],
                "note": "이 달은 거래 기록이 없습니다."}

    raw = _read_config_raw(round_trip=False)
    proposals = review.propose(stats, raw, technique_labels)
    ideas = review.house_ideas(stats)

    history = review.OptimizeHistory(cfg.state_dir)
    applied = [h for h in history.read() if h.get("month") == month and h.get("action") == "apply"]

    note = None
    if not stats["overall"]["enough"]:
        note = f"이번 달 거래가 {stats['overall']['trades']}건이라 아직 판단할 수 없습니다 (최소 {review.MIN_TRADES_MONTH}건 필요)."

    return {"stats": stats, "proposals": proposals, "ideas": ideas, "applied": applied, "note": note}


@app.post("/api/review/apply", dependencies=[_CONFIRM_SETTINGS])
@api_guard
async def review_apply(body: ReviewApplyIn):
    from daytrader import review
    from daytrader.ledger import Ledger, modes_in
    from daytrader.timeutil import iso, now_kst

    cfg = cfg_now()
    ledger = Ledger(cfg.state_dir)
    trades = ledger.trades(modes=modes_in(body.group))
    technique_labels = _review_technique_labels(cfg)

    stats = review.month_stats(trades, body.month, technique_labels, _REASON_LABELS)
    proposals = review.propose(stats, _read_config_raw(round_trip=False), technique_labels)
    by_id = {p["id"]: p for p in proposals}

    raw = _read_config_raw(round_trip=True)  # ★ A-14: 주석 보존을 위해 라운드트립으로 읽는다.
    changes = []
    skipped = []
    for pid in body.ids:
        p = by_id.get(pid)
        if p is None or p["kind"] != "config":
            # ★ 제안 목록에 없거나 kind!="config"(note) 인 id 는 반영 대상이 아니다.
            skipped.append(pid)
            continue
        parts = p["path"].split(".")
        node = raw
        for part in parts[:-1]:
            node = node[part]
        before = node[parts[-1]]
        node[parts[-1]] = p["proposed"]
        changes.append({
            "path": p["path"], "label": p["label"], "before": before, "after": p["proposed"],
            "reason": p["reason"], "evidence": p["evidence"], "confidence": p["confidence"],
        })

    if not changes:
        return {"ok": False, "applied": None, "skipped": skipped, "error": "반영할 항목이 없습니다."}

    _validate_raw(raw)
    _write_config_raw(raw)

    history = review.OptimizeHistory(cfg.state_dir)
    entry = {
        "id": review.new_id(), "at": iso(now_kst()), "month": body.month, "group": body.group,
        "action": "apply", "note": body.note, "changes": changes,
        "sample": {
            "trades": stats["overall"]["trades"], "pnl": stats["overall"]["pnl"],
            "win_rate": stats["overall"]["win_rate"],
        },
    }
    history.append(entry)
    return {"ok": True, "applied": entry, "skipped": skipped}


@app.get("/api/review/history")
@api_guard
async def review_history():
    from daytrader import review
    cfg = cfg_now()
    return {"entries": review.OptimizeHistory(cfg.state_dir).read()}


@app.post("/api/review/revert", dependencies=[_CONFIRM_SETTINGS])
@api_guard
async def review_revert(body: ReviewRevertIn):
    from daytrader import review
    from daytrader.timeutil import iso, now_kst

    cfg = cfg_now()
    history = review.OptimizeHistory(cfg.state_dir)
    entry = history.find(body.id)
    if entry is None:
        raise HTTPException(404, "이력을 찾을 수 없습니다.")
    if entry.get("action") != "apply":
        raise HTTPException(400, "되돌릴 수 있는 항목이 아닙니다.")

    raw = _read_config_raw(round_trip=True)
    reverted_changes = []
    for ch in entry["changes"]:
        parts = ch["path"].split(".")
        node = raw
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = ch["before"]
        reverted_changes.append({**ch, "before": ch["after"], "after": ch["before"]})

    _validate_raw(raw)
    _write_config_raw(raw)

    new_entry = {
        "id": review.new_id(), "at": iso(now_kst()), "month": entry["month"], "group": entry["group"],
        "action": "revert", "note": f"{entry['id']} 되돌림", "changes": reverted_changes,
        "sample": entry.get("sample", {}),
    }
    history.append(new_entry)
    return {"ok": True, "reverted": new_entry}


# ━━ 실험실 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LabVariantIn(BaseModel):
    name: str
    overrides: dict = {}
    note: str = ""


class LabStartIn(BaseModel):
    variants: list[LabVariantIn]
    speed: int = 600
    start: str = "09:00"
    seed: int | None = None


class LabCleanupIn(BaseModel):
    keep: int = 5


@app.get("/api/lab")
@api_guard
async def api_get_lab():
    from daytrader import lab as lab_mod
    cfg = cfg_now()
    lb = get_lab()
    status = lb.status()

    engine = runner.engine
    real = {
        "running": engine is not None, "is_live": bool(engine and cfg.is_live),
        "mode": cfg.mode, "mode_label": {"sim": "시뮬레이션", "replay": "리플레이", "web": "관찰",
                                          "paper": "모의매매", "live": "실거래"}.get(cfg.mode, cfg.mode),
    }
    can_shadow = engine is not None
    how = (
        "지금 돌고 있는 엔진 옆에서 같은 실제 시세로 다른 설정을 가상 매매시켜 비교합니다(섀도 비교)."
        if can_shadow else
        "지금 돌고 있는 엔진이 없어 섀도 비교를 할 수 없습니다. 대신 시뮬레이션 시드를 고정해 "
        "가상 참가자끼리(2명 이상) 비교하는 대조 실험은 가능합니다."
    )

    return {
        "running": status["running"], "spec": status["spec"], "participants": status["participants"],
        "presets": lab_mod.PRESETS, "max_variants": lab_mod.MAX_VARIANTS,
        "real": real, "can_shadow": can_shadow, "how": how,
    }


@app.post("/api/lab/start")
@api_guard
async def lab_start(body: LabStartIn):
    cfg = cfg_now()
    lb = get_lab()
    live_engine = runner.engine  # ★ 있으면 섀도 비교, 없으면 대조 실험(가상끼리).
    try:
        spec = lb.start(
            cfg, [v.model_dump() for v in body.variants],
            speed=body.speed, start=body.start, seed=body.seed, live_engine=live_engine,
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "spec": spec}


@app.post("/api/lab/stop")
@api_guard
async def lab_stop():
    """★ 가상 참가자만 멈춘다 - 실거래 엔진은 절대 건드리지 않는다."""
    get_lab().stop()
    return {"ok": True}


@app.get("/api/lab/result")
@api_guard
async def lab_result():
    return get_lab().result()


@app.post("/api/lab/cleanup")
@api_guard
async def lab_cleanup(body: LabCleanupIn):
    return get_lab().cleanup(keep=body.keep)


class TechniqueBacktestIn(BaseModel):
    market: str = "domestic"  # domestic | overseas | crypto | swing
    days: int = 7
    symbol_limit: int = 10
    save: bool = True


@app.post("/api/lab/technique_backtest")
@api_guard
def lab_technique_backtest(body: TechniqueBacktestIn):
    """오늘의 테마 후보 종목에, 실제 최근 시세로 기법별 손익을 재생해 순위를 매긴다(실험실 ·
    종목/테마별 기법 백테스트). save=True(기본)면 결과를 technique_prefs 에 저장해 실전 매매의
    진입 기법 채점에 가산점으로 반영한다."""
    if body.market not in ("domestic", "overseas", "crypto", "swing"):
        raise HTTPException(status_code=400, detail="market 은 domestic·overseas·crypto·swing 중 하나여야 합니다.")
    # ★ 스윙은 봉 하나가 하루(일봉)라 "최근 N일" 의 의미가 다르다 - 60일선 웜업만도
    # 30일보다 훨씬 많이 필요해서(playbook.py 의 SwingXxxEntry 참고) 상한을 크게 둔다.
    days_max = 400 if body.market == "swing" else 30
    if not (1 <= body.days <= days_max):
        raise HTTPException(status_code=400, detail=f"days 는 1~{days_max} 사이여야 합니다.")
    from daytrader import technique_backtest
    cfg = cfg_now()
    # ★ 지금 앱 모드가 sim/replay(가짜 시세)여도 이 백테스트는 항상 실제 시세를 써야 의미가
    # 있다 - get_client() 를 그대로 쓰면 가짜 시장을 "최근 1주일 실제 시세"로 착각해 재생한다.
    # 암호화폐는 애초에 국내·해외와 다른(빗썸) API 라 별도로 잡는다.
    if body.market == "crypto":
        from daytrader.bithumb_api import BithumbClient
        client = BithumbClient(cfg.bithumb_access_key, cfg.bithumb_secret_key)
    elif cfg.mode in ("sim", "replay"):
        from daytrader.router import build_router
        client = build_router(cfg)
    else:
        client = get_client()
    return technique_backtest.run(
        cfg, client, body.market, days=body.days, symbol_limit=max(1, min(30, body.symbol_limit)), save=body.save,
    )


@app.get("/api/lab/technique_prefs")
@api_guard
def lab_technique_prefs():
    from daytrader import technique_prefs
    return {"prefs": technique_prefs.summary(cfg_now())}


@app.get("/api/lab/technique_backtest/history")
@api_guard
def lab_technique_backtest_history(limit: int = 20):
    """[실험실] 화면의 "최근 실행 기록" - 수동으로 누른 것과 종목 재선정 때 자동으로 돈 것을
    모두 최신순으로 보여준다(technique_prefs.history)."""
    from daytrader import technique_prefs
    return {"history": technique_prefs.history(cfg_now(), limit=max(1, min(100, limit)))}


class TechniquePrefsClearIn(BaseModel):
    market: str | None = None


@app.post("/api/lab/technique_prefs/clear", dependencies=[_CONFIRM_SETTINGS])
@api_guard
def lab_technique_prefs_clear(body: TechniquePrefsClearIn):
    from daytrader import technique_prefs
    technique_prefs.clear(cfg_now(), body.market)
    return {"ok": True}


@app.get("/api/check")
@api_guard
async def check():
    cfg = cfg_now()
    be = breakeven_pct(cfg.costs.commission_pct, cfg.costs.tax_pct)
    warnings: list[str] = []
    if cfg.risk.take_profit_pct <= be * 2:
        warnings.append("익절폭이 왕복 비용의 2배에 못 미칩니다.")

    account = None
    if cfg.mode not in ("sim", "replay"):
        try:
            account = get_client().accounts()
        except Exception as exc:
            warnings.append(f"계좌 조회 실패: {exc}")

    return {"mode": cfg.mode, "breakeven_pct": be, "warnings": warnings, "account": account}


@app.get("/api/preflight")
@api_guard
def get_preflight():
    from daytrader.ledger import Ledger
    from daytrader.safety import preflight
    cfg = cfg_now()
    client = get_client()
    ledger = Ledger(cfg.state_dir)
    return preflight(cfg, client, ledger=ledger)


@app.post("/api/preflight/fix", dependencies=[_CONFIRM_TRADE])
@api_guard
async def fix_preflight():
    """고칠 수 있는 것만 고친다 - 지금은 우리가 낸 미체결 주문 정리만 지원한다."""
    from daytrader.safety import cleanup_orphans
    if runner.engine is None:
        return {"fixed": []}
    diffs = cleanup_orphans(runner.engine.broker, runner.engine.state)
    return {"fixed": [d.__dict__ for d in diffs]}


@app.get("/api/config")
@api_guard
async def get_config():
    # ★★★ "암호화폐도 국내주식과 동일한 기법 체계" - 이제 암호화폐 전용
    # 레지스트리(crypto_playbook.TECHNIQUES)는 안 쓴다. 국내
    # ENTRY_TECHNIQUES/EXIT_TECHNIQUES 를 그대로 공유하되, "어떤 기법이
    # 켜져 있는지"는 국내주식(cfg.strategy)과 암호화폐(cfg.crypto)가 각자
    # 다르게 정할 수 있으니, 같은 기법을 market 별로 복제해서 enabled 를
    # 따로 계산한다.
    from daytrader.playbook import ENTRY_TECHNIQUES, EXIT_TECHNIQUES
    from daytrader.technique_backtest import SWING_ENTRIES

    raw = _read_config_raw(round_trip=True)
    cfg = cfg_now()
    domestic_enabled = set(cfg.strategy.entry_order) | set(cfg.strategy.exit_enabled)
    crypto_enabled = set(cfg.crypto.entry_order) | set(cfg.crypto.exit_enabled)
    swing_enabled = set(cfg.swing.entry_order) | set(cfg.swing.exit_enabled)

    # ★★ 켜진 기법만 주면(예전엔 Playbook(cfg).describe() 를 그대로 썼다)
    # 설정 화면 체크리스트에 "아직 안 켜진" 새 기법이 아예 안 보여서 켤 방법이
    # 없어진다 - 반드시 전체 레지스트리를 준다. /api/playbook 과 같은 방식이다.
    # ★★★ 암호화폐 기법이 여기 안 들어가면 설정 화면에 체크박스 자체가
    # 안 생기고, 저장할 때 collectConfig() 가 "체크된 게 없다"고 보고
    # crypto.entry_order 를 빈 배열로 덮어써 버린다 - 실제로 겪은 저장
    # 실패 버그의 원인이었다. 스윙도 같은 이유로 반드시 넣는다.
    # ★★★ 단, 스윙의 "진입" 체크리스트에는 일봉 전용 3종(SWING_ENTRIES)만 올린다 - 분봉
    # 전용 단타 기법(시초 갭 돌파 등)을 스윙에 잘못 체크하면 일봉 위에서 의미 없이 동작한다.
    # 청산 기법은 손절·익절·추적손절처럼 퍼센트 기반이라 시장·시간축과 무관하게 재사용
    # 가능하므로 그대로 전체를 보여준다.
    all_techniques = []
    for registry, phase in ((ENTRY_TECHNIQUES, "entry"), (EXIT_TECHNIQUES, "exit")):
        for key, cls in registry.items():
            base = {
                "phase": phase, "key": cls.key, "label": cls.label,
                "description": cls.description, "origin": cls.origin, "standard": cls.standard,
            }
            all_techniques.append({**base, "market": "domestic", "enabled": key in domestic_enabled})
            all_techniques.append({**base, "market": "crypto", "enabled": key in crypto_enabled})
            if phase == "exit" or key in SWING_ENTRIES:
                all_techniques.append({**base, "market": "swing", "enabled": key in swing_enabled})

    return {
        "raw": _fill_new_defaults(_plain(raw)),
        "locked": _trading_locks(),
        "breakeven_pct": breakeven_pct(cfg.costs.commission_pct, cfg.costs.tax_pct),
        "techniques": all_techniques,
    }


def _fill_new_defaults(raw: dict) -> dict:
    """옛 config.yaml 에 없는 새 항목(투자금액·분할 매매·매매 속도 등)은 화면에 기본값으로 채워 보여준다 -
    비어 있으면 저장할 때 빈 값이 그대로 써진다."""
    from dataclasses import MISSING, fields
    from daytrader.config import CryptoCfg, EntryCfg, NewsCfg, NotifyCfg, OverseasCfg, RiskCfg, SizingCfg, SwingCfg, UiCfg

    def defaults(cls):
        # ★★★ 실제로 겪은 버그 - entry_order/exit_enabled/watchlist 류는
        # field(default_factory=...) 로 선언돼 있어 f.default 가 MISSING 이다.
        # 여기서 빠뜨리면(예전 코드) config.yaml 에 swing: 섹션 자체가 없는(예:
        # 이번에 새로 추가된 시장) 경우 화면의 진입·청산 기법 체크박스가 전부
        # 빈 배열([])로 시작해서 전부 해제된 채로 보이고, 그대로 저장하면
        # "swing.entry_order 가 비어 있습니다"로 저장이 거부된다("스윙매매
        # 선택후 저장시 매매기법이 없다고 나온다"의 원인) - 사용자는 체크박스가
        # 이미 채워져 있는 줄 알고 아무것도 안 건드렸는데 빈 배열로 저장되는
        # 것이 문제다. default_factory 도 반드시 함께 채운다.
        out = {}
        for f in fields(cls):
            if f.default is not MISSING:
                out[f.name] = f.default
            elif f.default_factory is not MISSING:  # type: ignore[misc]
                out[f.name] = f.default_factory()
        return out

    for section, cls in (("sizing", SizingCfg), ("risk", RiskCfg), ("overseas", OverseasCfg), ("crypto", CryptoCfg), ("swing", SwingCfg), ("ui", UiCfg), ("entry", EntryCfg), ("news", NewsCfg), ("notify", NotifyCfg)):
        cur = raw.get(section)
        if not isinstance(cur, dict):
            cur = raw[section] = {}
        for k, v in defaults(cls).items():
            if cur.get(k) is None and k not in ("watchlist",):
                cur[k] = v
    return raw


# ★★★ "국내주식 거래중이면 국내 설정만 잠기고, 암호화폐·해외주식 설정은
# 그대로 할 수 있어야 한다" - 최상위 설정 키가 어느 시장에 속하는지
# 매핑해 둔다. risk/strategy(위험관리·매매기법)는 국내 Playbook 기본
# 설정이지만 암호화폐·해외주식도 같은 Playbook 클래스를 재사용해 청산
# 판정에 이 값을 그대로 쓰므로, 국내주식과 함께 묶는다(따로 두면 국내가
# 거래 중일 때 암호화폐 청산 기준을 몰래 바꿔버릴 수 있어 위험하다).
_DOMESTIC_CONFIG_KEYS = {
    "mode", "capital", "risk", "screen", "entry", "exit", "strategy", "costs", "live", "account_seq",
}
_ALL_MARKET_CONFIG_KEYS = {"sizing"}  # 네 시장이 함께 쓰는 값 - 어느 시장이든 거래 중이면 바꿀 수 없다
# ★ style 은 "단타 매매 모드를 시장별로 분리해" 이후 risk/overseas/crypto 섹션 안으로 들어가서,
# 이미 각자의 _DOMESTIC_CONFIG_KEYS/_OVERSEAS_CONFIG_KEYS/_CRYPTO_CONFIG_KEYS 잠금을 그대로 따른다.
_CRYPTO_CONFIG_KEYS = {"crypto"}
_OVERSEAS_CONFIG_KEYS = {"overseas"}
_SWING_CONFIG_KEYS = {"swing"}


def _trading_locks() -> dict:
    return {
        "domestic": runner.running,
        "crypto": _crypto_engine is not None and _crypto_engine.is_running(),
        "overseas": _overseas_engine is not None and _overseas_engine.is_running(),
        "swing": _swing_engine is not None and _swing_engine.is_running(),
    }


@app.post("/api/config", dependencies=[_CONFIRM_SETTINGS])
@api_guard
async def post_config(body: dict):
    # ★ config.yaml 최상위에 올 수 있는 항목만 받는다 - load_config() 도 결국 같은 목록으로
    # 걸러내지만, 여기서 먼저 막아야 알 수 없는 키(예: 오타·다른 스키마를 노린 조작)가
    # raw.update(body) 로 병합되기 전에 분명한 400 으로 거절된다.
    unknown_keys = set(body.keys()) - KNOWN_TOP_LEVEL_KEYS
    if unknown_keys:
        raise HTTPException(
            status_code=400,
            detail=f"알 수 없는 설정 항목입니다: {', '.join(sorted(unknown_keys))}",
        )
    # ★★★ 예전엔 국내주식이 거래 중이면(runner.running) 설정 저장 전체를
    # 막았다 - 암호화폐만 거래 중이고 국내주식은 쉬고 있어도 국내 설정을
    # 못 고치는 등, 실제로 거래 중인 시장과 무관한 설정까지 막혀서
    # 불편했다. 이제 body 에 실제로 들어있는 최상위 키를 보고, 그 키가
    # 속한 시장이 지금 거래 중일 때만 그 부분을 거부한다.
    locks = _trading_locks()
    blocked_markets = []
    if locks["domestic"] and (set(body.keys()) & _DOMESTIC_CONFIG_KEYS):
        blocked_markets.append("국내주식(위험관리·매매기법 포함)")
    if any(locks.values()) and (set(body.keys()) & _ALL_MARKET_CONFIG_KEYS):
        blocked_markets.append("공통(매매 속도·투자금액 배분)")
    if locks["crypto"] and (set(body.keys()) & _CRYPTO_CONFIG_KEYS):
        blocked_markets.append("암호화폐")
    if locks["overseas"] and (set(body.keys()) & _OVERSEAS_CONFIG_KEYS):
        blocked_markets.append("해외주식")
    if locks["swing"] and (set(body.keys()) & _SWING_CONFIG_KEYS):
        blocked_markets.append("스윙")
    if blocked_markets:
        raise HTTPException(
            status_code=409,
            detail=f"{', '.join(blocked_markets)} 거래 중에는 해당 설정을 바꿀 수 없습니다. 다른 시장 설정은 그대로 저장할 수 있습니다.",
        )

    raw = _read_config_raw(round_trip=True)
    raw.update(body)
    _validate_raw(raw)  # 검증 통과한 뒤에만 실제 파일에 쓴다.
    _write_config_raw(raw)
    return {"ok": True}


@app.get("/api/themes")
@api_guard
async def get_themes():
    from daytrader.screener import load_theme_names, load_themes
    cfg = cfg_now()
    themes = load_themes(cfg.themes_file)
    # ★ 종목코드만 나오던 문제 - themes.yaml 의 인라인 주석(# 종목명)을
    # 읽어 이름 매핑을 함께 준다. 화면이 code 옆에 이 이름을 붙여 보여준다.
    names = load_theme_names(cfg.themes_file)
    return {
        "themes": themes, "names": names,
        "crypto_watchlist": cfg.crypto.watchlist,
        "overseas_watchlist": cfg.overseas.watchlist,
    }


@app.get("/api/themes/verify")
@api_guard
async def verify_themes():
    from daytrader.screener import load_themes
    cfg = cfg_now()
    themes = load_themes(cfg.themes_file)
    all_symbols = sorted({s for codes in themes.values() for s in codes})
    rows = get_client().stocks(all_symbols)
    found = {r.get("symbol"): r.get("name") for r in rows}
    missing = [s for s in all_symbols if s not in found]
    return {"ok": not missing, "missing": missing, "names": found}


@app.post("/api/themes", dependencies=[_CONFIRM_SETTINGS])
@api_guard
async def add_theme_symbol(body: ThemeAddIn):
    if not re.fullmatch(r"\d{6}", body.symbol):
        raise HTTPException(status_code=400, detail="종목코드는 6자리 숫자여야 합니다.")

    try:
        rows = get_client().stocks([body.symbol])
        name = rows[0].get("name", body.symbol) if rows else body.symbol
    except Exception:
        name = body.symbol

    path = cfg_now().themes_file
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    # 코드 옆에 회사명을 주석으로 자동 부착한다 - 여러 모듈이 이 주석을 읽어 종목명으로 쓴다.
    line = f'    - "{body.symbol}"  # {name}\n'
    marker = f"{body.theme}:\n"
    if marker not in text:
        text = text.rstrip("\n") + f"\n\n  {marker}{line}"
    else:
        text = text.replace(marker, marker + line, 1)

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, path)
    return {"ok": True, "symbol": body.symbol, "name": name}


@app.get("/api/discover")
@api_guard
async def discover(count: int = 40):
    from daytrader.screener import Screener
    cfg = cfg_now()
    screener = Screener(get_client(), cfg)
    return screener.discover(count=count)


_news_feed_cache = None


def get_news_feed():
    """요청마다 새로 만들면 NewsFeed 의 TTL 캐시가 무의미해진다. 모듈 전역에 하나 둔다."""
    global _news_feed_cache
    if _news_feed_cache is None:
        from daytrader.news import build_feed
        _news_feed_cache = build_feed(cfg_now())
    return _news_feed_cache


@app.get("/api/market")
@api_guard
def get_market(refresh: bool = False, ttl: float | None = None):
    """지수·선물·미국주·환율·코인 - 매매 판단에 개입하지 않는 관찰용 화면.
    ★ 국내 개별 종목은 토스증권 API 를 1순위로 쓴다 - 키가 등록돼 있으면 넘긴다.
    """
    from daytrader import market
    toss_client = None
    try:
        cfg = cfg_now()
        if cfg.client_id and cfg.client_secret:
            toss_client = get_client()
    except Exception:
        toss_client = None  # ★ 없어도 그만 - market.snapshot() 이 네이버로 폴백한다.
    return market.snapshot(force=refresh, ttl=ttl, toss_client=toss_client)


@app.get("/api/news")
@api_guard
def get_news(hours: float = 24.0):
    from daytrader.screener import load_themes
    from daytrader.simulator import load_theme_names
    cfg = cfg_now()
    feed = get_news_feed()
    if not feed._items:
        feed.fetch()
    items = feed.read(hours=hours, limit=200)  # 화면이 전부 그리려 들면 느려진다.
    tagged = feed.tag(items, theme_names=load_theme_names(cfg.themes_file), themes=load_themes(cfg.themes_file))
    return {"mode": cfg.news.mode, "items": tagged, "groups": feed.grouped(tagged, per_group=5)}


@app.post("/api/news/refresh")
@api_guard
def refresh_news():
    feed = get_news_feed()
    items, errors = feed.fetch(force=True)
    return {"ok": True, "items": len(items), "errors": errors}


@app.get("/api/news/test")
@api_guard
def test_news():
    return get_news_feed().self_test()


# ━━ 정적 파일 · index ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if os.path.isdir(os.path.join(WEB_DIR, "static")):
    app.mount("/static", StaticFiles(directory=os.path.join(WEB_DIR, "static")), name="static")


@app.get("/")
async def index():
    index_path = os.path.join(WEB_DIR, "index.html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()

        def _bump(m: re.Match) -> str:
            fname = m.group(1)
            fpath = os.path.join(WEB_DIR, "static", fname)
            v = int(os.path.getmtime(fpath)) if os.path.exists(fpath) else 0
            return f"/static/{fname}?v={v}"

        # ★★ index.html 을 그대로 주지 말고 정적 파일의 ?v= 를 파일 수정 시각으로
        # 바꿔서 준다. 숫자를 박아 두면 파일을 고쳐도 URL 이 그대로라 브라우저가
        # 캐시를 계속 써서, 새로고침해도 옛 코드가 돌아 "고쳤는데 왜 그대로지"를
        # 반복하게 된다. 실제로 겪은 문제다.
        html = re.sub(r"/static/([\w.-]+)\?v=\d+", _bump, html)
        return Response(html, media_type="text/html; charset=utf-8")
    except Exception:
        return FileResponse(index_path)


# ━━ 라우트: 매매 제어 · 성과 · 일지 (STAGE 14c) ━━━━━━━━━━━━━━━━━━━━━━━━

class EngineStartIn(BaseModel):
    confirm: str = ""
    scenario: str | None = None
    speed: float | None = None
    dates: list | None = None


class EngineStopIn(BaseModel):
    close_positions: bool = False


class PerformanceResetIn(BaseModel):
    group: str = "virtual"


PRINCIPLES = []  # 더 이상 쓰이지 않는다 - daytrader/principles.py 가 설정에서 동적으로 만든다.


@app.post("/api/engine/start", dependencies=[_CONFIRM_TRADE])
@api_guard
async def engine_start(body: EngineStartIn):
    if runner.running:
        raise HTTPException(status_code=409, detail="이미 매매가 진행 중입니다.")

    cfg = cfg_now()

    if cfg.is_live:
        # ★★ confirm=="실매매" 와 preflight().ok 둘 다 필요하다.
        # 하나만 걸면 나머지가 뚫린다. 실패하면 무엇이 막혔는지 이름으로 알린다.
        from daytrader.ledger import Ledger
        from daytrader.safety import preflight

        missing = []
        if body.confirm != "실매매":
            missing.append("확인 문구('실매매') 입력")

        client = None
        try:
            client = get_client(force_new=True)
            pf = preflight(cfg, client, ledger=Ledger(cfg.state_dir))
            if not pf["ok"]:
                missing.append("사전 점검(preflight)")
        except Exception as exc:
            # 클라이언트 생성 실패(예: 인증 키 없음)도 사전 점검 실패의 일종이다.
            missing.append(f"사전 점검(preflight) - {exc}")

        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"다음이 막혀 있어 실거래를 시작할 수 없습니다: {', '.join(missing)}.",
            )

        runner.start(cfg, client=client)
        return {"ok": True}

    if cfg.mode in ("sim", "replay"):
        from daytrader.clock import SimClock
        from daytrader.simulator import SimClient

        if body.scenario:
            cfg.simulation.scenario = body.scenario
        if body.speed:
            cfg.simulation.speed = body.speed
        if body.dates:
            dates = body.dates
        elif cfg.simulation.days and cfg.simulation.days > 1:
            # ★★★ 실제로 겪은 문제 - simulation.days(설정에 6일로 되어
            # 있음)가 있어도, 화면에서 dates 를 안 보내면 무조건 하루
            # ([None])만 돌았다. 기본 배속(3600배속)이면 국내 정규장
            # 6.5시간이 실제로는 6~7초 만에 끝나버려서 "시작하고 몇 초
            # 뒤 자동으로 정지된다"고 느끼게 됐다 - 실은 하루치를 다 돈
            # 것뿐이었다. 이제 설정된 일수만큼 거래일을 이어서 돈다.
            from daytrader.timeutil import now_kst, trading_days_back
            end = now_kst()
            dates = [trading_days_back(end, i) for i in range(cfg.simulation.days - 1, -1, -1)]
        else:
            dates = [None]

        def _factory(day):
            clock = SimClock(start=cfg.simulation.start_time, speed=cfg.simulation.speed, day=day)
            return SimClient(cfg, clock=clock)

        runner.start(cfg, client_factory=_factory, day_dates=dates)
        return {"ok": True}

    # ★ "배정 자금이 계좌 잔고를 초과하면 안 된다"는 원칙은 실거래에만
    # 적용한다(위 cfg.is_live 분기의 preflight() 가 막는다). 모의매매
    # (web/paper)는 실제 돈이 안 나가니 배정금액을 자유롭게 크게 잡고
    # 전략을 테스트할 수 있어야 한다 - 여기서 막지 않는다.
    runner.start(cfg, client=get_client())
    return {"ok": True}


@app.post("/api/engine/stop", dependencies=[_CONFIRM_TRADE])
@api_guard
async def engine_stop(body: EngineStopIn):
    runner.stop(close_positions=body.close_positions)
    return {"ok": True}


@app.get("/api/changelog")
@api_guard
async def get_changelog():
    """★ 릴리즈 노트(CHANGELOG.md) 원문. exe 로 묶이면 읽기 전용 자원 폴더
    (res_path)에, 소스 실행이면 프로젝트 루트에 있다 - 못 찾으면 화면이
    "없음"을 알릴 수 있게 빈 문자열로 돌려준다."""
    from daytrader import __version__
    from daytrader.paths import res_path
    text = ""
    for path in (res_path("CHANGELOG.md"), app_path("CHANGELOG.md")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            break
        except OSError:
            continue
    return {"version": __version__, "markdown": text}


@app.get("/api/status")
@api_guard
async def get_status():
    """가벼운 것 - 트레이가 3초마다 부른다."""
    # ★★★ 실제로 겪은 버그: runner.engine 은 스레드가 끝난 뒤에도 절대
    # None 으로 지워지지 않는다(_run() 이 끝나도 참조를 안 치운다) - 그래서
    # "engine is not None" 만 보고 판단하면, 정지 버튼을 눌러 스레드가 다
    # 끝난 뒤에도 이 라우트가 영원히 "running: True" 를 돌려주고, 화면은
    # "자동매매 시작" 버튼으로 절대 안 돌아간다. 진짜 실행 중 여부는
    # runner.running(스레드 생존 여부)으로만 정확히 판단할 수 있다.
    # ★ 화면 상단에 지금 켜진 버전을 늘 보여주기 위해 - 재빌드했는데
    # 실제로 반영됐는지 화면만 봐도 알 수 있게 한다.
    from daytrader import __version__
    if runner.engine is not None and runner.running:
        # ★ tick() 은 running 키가 없다 - 대시보드가 "지금 돌고 있는지"를
        # 명확히 판단할 수 있게 여기서 채워 준다.
        return {**runner.engine.tick(), "running": True, "version": __version__}
    # ★ 꺼져 있어도 "설정된 매매 모드"는 알려 준다 - 상단 밴드가 국내주식 모드를 계속 보여줘야 한다.
    try:
        mode = cfg_now().mode
    except Exception:
        mode = None
    return {"running": runner.running, "error": runner.error, "version": __version__, "mode": mode}


@app.get("/api/logs")
@api_guard
async def get_logs(after: int = 0):
    return {"logs": log_buffer.since(after), "last_id": log_buffer.last_id}


@app.get("/api/logs/history")
@api_guard
async def get_logs_history(before_id: int | None = None, level: str | None = None, limit: int = 200):
    """★ 화면의 실시간 로그(LogBuffer, 최근 2000건)보다 오래된 경고·오류를 다시 찾을 때 쓴다.
    app_log 표(WARNING 이상만, daytrader/applog.py)를 id 기준 keyset 으로 최신순 페이지네이션한다 -
    /api/logs 는 그대로 두고(화면이 안 바뀌어도 되게) 별도 엔드포인트로 추가했다."""
    from daytrader import applog
    limit = max(1, min(limit, 1000))
    cfg = cfg_now()
    rows = applog.query(cfg.state_dir, level=level, before_id=before_id, limit=limit)
    next_before_id = rows[-1]["id"] if len(rows) == limit else None
    return {"logs": rows, "next_before_id": next_before_id}


@app.get("/api/stream")
async def stream_events(request: Request):
    """SSE 로 실시간 이벤트를 흘려보낸다.
    이벤트: tick(cfg.ui.tick_ms 주기, 엔진이 돌 때만 - 불필요한 부하 방지) /
    snapshot(5초) / log / verdict / trade / alert / selection.
    log/verdict/trade/alert/selection 은 다른 모듈이 bus.publish() 로 밀어 넣은
    것을 그대로 흘린다.
    """
    q = bus.subscribe()
    cfg = cfg_now()
    tick_interval = cfg.ui.tick_ms / 1000.0
    snapshot_interval = 5.0
    last_tick = 0.0
    last_snapshot = 0.0

    async def _gen():
        nonlocal last_tick, last_snapshot
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break

                sent = False
                # BUS.subscribe() 로 받은 큐를 비우면서 event/data 를 흘린다.
                while q:
                    item = q.popleft()
                    payload = json.dumps(item["data"], ensure_ascii=False, default=str)
                    yield f"event: {item['event']}\ndata: {payload}\n\n"
                    sent = True

                now = time.monotonic()
                engine = runner.engine
                if engine is not None and now - last_tick >= tick_interval:
                    last_tick = now
                    payload = json.dumps(engine.tick(), ensure_ascii=False, default=str)
                    yield f"event: tick\ndata: {payload}\n\n"
                    sent = True

                if engine is not None and now - last_snapshot >= snapshot_interval:
                    last_snapshot = now
                    payload = json.dumps(engine.snapshot(), ensure_ascii=False, default=str)
                    yield f"event: snapshot\ndata: {payload}\n\n"
                    sent = True

                if not sent:
                    yield ": ping\n\n"  # 아무것도 보낼 게 없으면 하트비트.

                await asyncio.sleep(min(tick_interval, 1.0) if engine is not None else 1.0)
        except asyncio.CancelledError:
            pass  # 연결이 끊기면 조용히 정리한다.
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/selection")
@api_guard
async def get_selection(refresh: bool = False):
    """★ 매매 중이면 엔진의 report 를 그대로 준다. 화면과 실제가 다르면
    화면을 믿을 수 없다. 매매 중이 아니면 Screener 로 새로 뽑아 보여준다.
    """
    idle = _screening_idle()
    if runner.engine is not None:
        eng = runner.engine
        if refresh and not idle:
            eng.rescreen(force=True)
        # ★★★ 실제로 겪은 문제 - 엔진이 살아 있어도 아직 한 번도 스크리닝을
        # 안 했으면(막 시작했거나, 지금이 매매 시간대가 아니어서 rescreen()
        # 이 호출된 적 없으면) report 가 None 이라, 화면에는 "아직 스크리닝
        # 결과가 없습니다"만 뜨고 왜 없는지는 알 수 없었다. 사용자가 이
        # 화면을 열었다는 건 결과를 보고 싶다는 뜻이니, 그 자리에서 한 번
        # 돌려서 실제 결과(또는 실패 원인)를 보여준다.
        if eng.report is None and not idle:
            try:
                eng.rescreen(force=True)
            except Exception as exc:
                logging.getLogger(__name__).warning("종목선정 화면에서 즉시 스크리닝 실패: %s", exc)
        return {
            "selection": eng.report.to_dict() if eng.report else None,
            "candidates": [
                {
                    "symbol": c.symbol, "name": c.name, "theme": c.theme,
                    "last_price": c.last_price, "change_rate": c.change_rate, "why": c.why,
                    "status": eng.candidate_status.get(c.symbol, ""),
                    "verdicts": [v.to_dict() for v in eng.candidate_verdicts.get(c.symbol, [])],
                }
                for c in eng.candidates
            ],
            "session": _session_info(),
            # ★★★ "종목 선정이 언제 다시되는지도 메시지로 알려줘" -
            # 엔진이 30분마다 다시 고르는데 화면에 그 사실이 안 나왔다.
            "rescreen": eng._rescreen_info() if hasattr(eng, "_rescreen_info") else {},
        }

    # ★★★ 엔진이 꺼져 있을 때는 화면이 갱신될 때마다(15초 주기) 스크리닝을
    # 새로 돌렸다 - 한 번에 랭킹 API 를 2회 부르므로, 다른 화면이나 진단이
    # 겹치면 초당 3회 한도를 넘겨 빈 응답을 받는다. 실제로 "시세를 하나도
    # 받아오지 못했다"가 이렇게 나올 수 있었다.
    # ★ 짧게 캐시해 같은 결과를 재사용한다 - 랭킹은 몇 초 단위로 크게
    #   달라지지 않으므로 화면 정확도에는 영향이 없다.
    if idle:
        return {"selection": None, "candidates": [], "session": _session_info()}

    global _selection_cache
    now = time.time()
    if not refresh and _selection_cache and (now - _selection_cache["at"]) < 20:
        return _selection_cache["data"]

    from daytrader.screener import Screener
    cfg = cfg_now()
    report = Screener(get_client(), cfg).build_report()
    data = {
        "selection": report.to_dict(),
        "candidates": [asdict(c) for c in report.candidates],
        "session": _session_info(),
    }
    # ★★★ 실패한 결과는 캐시하지 않는다 - 일시적인 오류 한 번이 캐시에
    # 박히면 그 시간 동안 "시세를 못 받았다"가 고정돼 버린다. 다음 호출에
    # 다시 시도할 수 있어야 한다.
    if getattr(report, "market_size", 0) > 0:
        _selection_cache = {"at": now, "data": data}
    return data


@app.get("/api/screen")
@api_guard
async def get_screen(refresh: bool = False):
    """후보만 간단히."""
    data = await get_selection(refresh=refresh)
    return data["candidates"]


@app.get("/api/performance")
@api_guard
async def get_performance(group: str = "virtual"):
    """★ 연습(virtual)과 실거래(real) 원장을 절대 섞지 않는다."""
    from daytrader.ledger import Ledger, modes_in
    cfg = cfg_now()
    ledger = Ledger(cfg.state_dir)
    modes = modes_in(group)
    daily = ledger.daily(modes=modes)
    return {
        "daily": daily,
        "monthly": ledger.monthly(modes=modes),
        "yearly": ledger.yearly(modes=modes),
        "totals": ledger.totals(modes=modes, daily_rows=daily),
        "trades": ledger.trades(modes=modes)[-5000:],
        "by_mode": ledger.by_mode(),
        "by_technique": ledger.by_technique(modes=modes),
        "live": group == "real",
    }


@app.post("/api/performance/reset", dependencies=[_CONFIRM_SETTINGS])
@api_guard
async def reset_performance(body: PerformanceResetIn):
    # ★ group=="real" 이면 400 - 실거래 기록은 못 지운다.
    if body.group == "real":
        raise HTTPException(status_code=400, detail="실거래 기록은 지울 수 없습니다.")
    from daytrader.ledger import Ledger, modes_in
    cfg = cfg_now()
    Ledger(cfg.state_dir).reset(modes=modes_in(body.group))
    return {"ok": True}


def _tag_domestic_session(rows: list) -> None:
    """★★★ "매매실적 조회시 장 세션별로도 조회할수 있게해" 요청 - 매수·매도 각 행에
    그 사건이 일어난 시각의 국내 세션(프리장/본장/NXT장)을 domestic_phase() 로 붙인다.
    매도 행은 detail.exit_time(청산 시각)을, 없으면 detail.entry_time 을, 그것도
    없으면(매수 행은 애초에 detail 에 시각이 없다) 이 행 자체가 기록된 시각(at)을 쓴다.
    ★ 시각 하나가 깨져 있다고 매매일지 전체가 못 열리면 안 되므로 행 단위로 감싼다 -
    실패하면 그 행만 session=None."""
    from daytrader.session import domestic_phase
    from daytrader.timeutil import parse_dt
    for row in rows:
        try:
            detail = row.get("detail") or {}
            ts = detail.get("exit_time") or detail.get("entry_time") or row.get("at")
            dt = parse_dt(ts)
            row["session"] = domestic_phase(dt) if dt else None
        except Exception:
            row["session"] = None


@app.get("/api/journal")
@api_guard
async def get_journal(group: str = "virtual", date: str | None = None, all_kinds: bool = False):
    """★★★ "매매일지에 매매한 이력 외에 종목선정 같은 항목은 보이지 않도록" -
    journal 파일에는 매수·매도 말고도 종목선정 거절(reject), 세션 전환·
    사전점검(session), 중단 사유(halt), 계좌 대조(reconcile) 가 함께
    쌓인다. 이것들은 진단·감사에는 필요하지만 "매매일지"라는 화면에서는
    실제 매매를 가려 버린다. 기본은 매수·매도만 보여주고, 필요하면
    all_kinds=true 로 전부 볼 수 있게 남겨 둔다(기록 자체는 안 지운다).
    """
    from daytrader.journal import Journal
    from daytrader.ledger import modes_in
    cfg = cfg_now()
    journal = Journal(cfg.state_dir, mode=cfg.mode)
    modes = modes_in(group)
    kinds = None if all_kinds else TRADE_JOURNAL_KINDS
    # ★ 날짜 목록도 같은 종류(매수·매도)만 센다 - 아니면 시뮬레이션 로그만 있는 날이 "가장 최근
    #   날짜"로 잡혀 화면이 빈 채로 열린다. 날짜를 안 주면 가장 최근 날짜 하루치만 준다
    #   (예전엔 전 기간 기록을 한꺼번에 내려보냈다).
    dates = journal.dates(modes=modes, kinds=kinds)
    if date is None and dates:
        date = dates[-1]
    rows = journal.read(date=date, modes=modes, kinds=kinds)
    _tag_domestic_session(rows)
    return {
        "rows": rows,
        "dates": dates,
    }


@app.get("/api/playbook")
@api_guard
async def get_playbook():
    """켜진 기법 + 전체 기법(원전 포함). 국내주식·암호화폐가 같은 레지스트리를
    공유하되, market별로 enabled 여부만 따로 계산한다(/api/config 와 동일한 방식)."""
    from daytrader.playbook import ENTRY_TECHNIQUES, EXIT_TECHNIQUES
    from daytrader.technique_backtest import SWING_ENTRIES
    cfg = cfg_now()
    pb = Playbook(cfg)
    crypto_pb = Playbook(cfg, entry_order=cfg.crypto.entry_order, exit_enabled=cfg.crypto.exit_enabled)
    swing_pb = Playbook(cfg, entry_order=cfg.swing.entry_order, exit_enabled=cfg.swing.exit_enabled, market="swing")
    domestic_enabled = set(cfg.strategy.entry_order) | set(cfg.strategy.exit_enabled)
    crypto_enabled = set(cfg.crypto.entry_order) | set(cfg.crypto.exit_enabled)
    swing_enabled = set(cfg.swing.entry_order) | set(cfg.swing.exit_enabled)

    all_techniques = []
    for registry, phase in ((ENTRY_TECHNIQUES, "entry"), (EXIT_TECHNIQUES, "exit")):
        for key, cls in registry.items():
            base = {
                "phase": phase, "key": cls.key, "label": cls.label,
                "description": cls.description, "origin": cls.origin, "standard": cls.standard,
            }
            all_techniques.append({**base, "market": "domestic", "enabled": key in domestic_enabled})
            all_techniques.append({**base, "market": "crypto", "enabled": key in crypto_enabled})
            # ★ 스윙의 진입 체크리스트는 일봉 전용 3종만(get_config() 와 같은 이유).
            if phase == "exit" or key in SWING_ENTRIES:
                all_techniques.append({**base, "market": "swing", "enabled": key in swing_enabled})

    return {
        "enabled": [{**t, "market": "domestic"} for t in pb.describe()]
        + [{**t, "market": "crypto"} for t in crypto_pb.describe()]
        + [{**t, "market": "swing"} for t in swing_pb.describe()],
        "all": all_techniques,
    }


@app.get("/api/techniques")
@api_guard
async def get_techniques():
    """진입/청산으로 묶은 기법 전체 목록. forms.js 의 techlist·기법 배지 팝업이 쓴다."""
    from daytrader.playbook import ENTRY_TECHNIQUES, EXIT_TECHNIQUES
    cfg = cfg_now()
    enabled_keys = set(cfg.strategy.entry_order) | set(cfg.strategy.exit_enabled)

    def _describe(registry, phase):
        return [
            {
                "phase": phase, "key": cls.key, "label": cls.label,
                "description": cls.description, "origin": cls.origin, "standard": cls.standard,
                "enabled": key in enabled_keys,
            }
            for key, cls in registry.items()
        ]

    return {"entry": _describe(ENTRY_TECHNIQUES, "entry"), "exit": _describe(EXIT_TECHNIQUES, "exit")}


@app.get("/api/techniques/bonus")
@api_guard
async def get_technique_bonus():
    """["각 매매기법별로 가산점이 어떻게 되어 있는지 거래시장별로 볼수 있게해"] 요청의 구현.
    시장별로 지금 이 순간 실적 기준 가산점(Playbook.bonus_table())이 몇 배로 걸려 있는지 보여준다.

    ★★★ "실행 중인 엔진이 있으면 그 살아있는 Playbook 을, 없으면 저장된 거래 기록에서 그
    자리에서 다시 계산한 Playbook 을 쓴다"는 원칙 - bonus_table() 은 순수 계산(부작용 없음)이라
    둘 중 어느 쪽이든 항상 최신 결과를 낸다. 이렇게 하는 이유는 이 앱이 상시 실행형이 아니라
    사용자가 필요할 때만 켜는 데스크톱 앱이라, "엔진이 꺼져 있어서 값을 모른다"고 하면 화면이
    거의 항상 비어 보이기 때문이다(crypto_status()/swing_status() 의 디스크 폴백과 같은 이유).

    ★★★ 국내주식은 원래부터 시장 전체가 공유하는 daytrader.db 의 trades 표(daytrader/ledger.py)에 기록되지만,
    해외주식·암호화폐·스윙은 각자 자기 상태 파일(overseas_state.json 등)의 state.closed 리스트에
    "entry_technique" 필드로 기록한다(2026-09-24 조사에서 발견 - engine.py 만 Playbook.set_performance()
    를 호출해서, 이 세 시장은 학습 모드를 뭘로 두든 실적 가산점이 항상 중립(1.0)이었다). 여기서는
    daytrader.perf_stats.summarize_closed_trades() 로 그 세 시장의 state.closed 를
    Ledger.by_technique() 와 같은 모양(dict[key] = {trades, win_rate, ...})으로 맞춰 준다."""
    from daytrader.ledger import Ledger
    from daytrader.perf_stats import summarize_closed_trades

    cfg = cfg_now()

    def _domestic() -> dict:
        if runner.engine is not None:
            pb = runner.engine.playbook
        else:
            pb = Playbook(cfg, market="domestic", learning_mode=cfg.risk.technique_learning_mode)
            pb.set_performance(Ledger(cfg.state_dir).by_technique(modes=[cfg.mode]))
        return {"learning_mode": pb.learning_mode, "bonus": pb.bonus_table()}

    def _crypto() -> dict:
        if _crypto_engine is not None:
            pb = _crypto_engine.playbook
        else:
            from daytrader.crypto_engine import CryptoState
            state = CryptoState(os.path.join(cfg.state_dir, "crypto_state.json"))
            pb = Playbook(cfg, entry_order=cfg.crypto.entry_order, exit_enabled=cfg.crypto.exit_enabled,
                          market="crypto", learning_mode=cfg.crypto.technique_learning_mode)
            pb.set_performance(summarize_closed_trades(state.closed, technique_field="entry_technique")["by_technique"])
        return {"learning_mode": pb.learning_mode, "bonus": pb.bonus_table()}

    def _overseas() -> dict:
        if _overseas_engine is not None:
            pb = _overseas_engine.playbook
        else:
            from types import SimpleNamespace
            from daytrader.overseas_engine import OverseasState
            state = OverseasState(os.path.join(cfg.state_dir, "overseas_state.json"))
            # ★ 해외주식은 crypto/swing 과 달리 자기만의 entry_order/exit_enabled 가 없다 -
            # overseas_engine.py 의 실제 생성 방식과 똑같이 국내주식 설정(cfg.strategy.*)을
            # Playbook 기본값 그대로 쓰고, risk/max_hold_minutes/market/learning_mode 만 덮어쓴다.
            overseas_risk = SimpleNamespace(
                stop_loss_pct=cfg.overseas.stop_loss_pct, take_profit_pct=cfg.overseas.take_profit_pct,
                trailing_stop_pct=cfg.overseas.trailing_pct, trailing_arm_pct=cfg.overseas.trailing_arm_pct,
            )
            pb = Playbook(cfg, risk=overseas_risk, max_hold_minutes=cfg.overseas.max_hold_minutes,
                          market="overseas", learning_mode=cfg.overseas.technique_learning_mode)
            pb.set_performance(summarize_closed_trades(state.closed, technique_field="entry_technique")["by_technique"])
        return {"learning_mode": pb.learning_mode, "bonus": pb.bonus_table()}

    def _swing() -> dict:
        if _swing_engine is not None:
            pb = _swing_engine.playbook
        else:
            from daytrader.swing_engine import SwingState
            state = SwingState(os.path.join(cfg.state_dir, "swing_state.json"))
            pb = Playbook(cfg, entry_order=cfg.swing.entry_order, exit_enabled=cfg.swing.exit_enabled,
                          market="swing", learning_mode=cfg.swing.technique_learning_mode)
            pb.set_performance(summarize_closed_trades(state.closed, technique_field="entry_technique")["by_technique"])
        return {"learning_mode": pb.learning_mode, "bonus": pb.bonus_table()}

    return {"domestic": _domestic(), "overseas": _overseas(), "crypto": _crypto(), "swing": _swing()}


@app.get("/api/playbook/stats")
@api_guard
async def get_playbook_stats(group: str = "virtual"):
    """기법별 성적."""
    from daytrader.ledger import Ledger, modes_in
    cfg = cfg_now()
    return Ledger(cfg.state_dir).by_technique(modes=modes_in(group))


@app.get("/api/playbook/histogram")
@api_guard
async def get_playbook_histogram():
    """오늘 어디서 막혔나 - blocked_by 항목별 발생 횟수."""
    from daytrader.journal import Journal
    from daytrader.timeutil import day_str, now_kst
    cfg = cfg_now()
    journal = Journal(cfg.state_dir, mode=cfg.mode)
    today = day_str(now_kst())
    counts: dict = {}
    for r in journal.read(date=today, kinds=["evaluate"]):
        for key in r.get("detail", {}).get("blocked_by", []):
            counts[key] = counts.get(key, 0) + 1
    return {"date": today, "histogram": counts}


@app.get("/api/verdicts")
@api_guard
async def get_verdicts(limit: int = 50):
    """최근 판정."""
    from daytrader.journal import Journal
    cfg = cfg_now()
    journal = Journal(cfg.state_dir, mode=cfg.mode)
    return {"verdicts": journal.read(kinds=["evaluate"], limit=limit)}


@app.get("/api/principles")
@api_guard
async def get_principles():
    from daytrader import principles
    cfg = cfg_now()
    return principles.build(cfg)


@app.get("/api/chart/{symbol}")
@api_guard
async def get_chart(symbol: str, count: int = 180, date: str | None = None):
    """분봉 + 매수·매도 마커. marks 는 엔진 스냅샷(진행 중·오늘 청산)과
    원장(지난 기록)에서 모으되, 같은 entry_time 이면 중복 제외한다.
    """
    from daytrader.ledger import Ledger
    from daytrader.timeutil import day_str, iso, now_kst

    cfg = cfg_now()
    _check_symbol("domestic", symbol)
    count = max(20, min(int(count), 400))
    date = date or day_str(now_kst())

    try:
        bars = get_client().candles(symbol, "1m", count)
    except Exception:
        bars = []

    name = symbol
    marks: list[dict] = []
    seen_entry_times: set = set()

    def _buy_mark(entry_time, price, qty, technique, why):
        seen_entry_times.add(entry_time)
        marks.append({
            "kind": "buy", "at": entry_time, "price": price, "qty": qty,
            "label": "매수", "technique": technique, "why": why, "pnl": None, "reason": "",
        })

    def _sell_mark(exit_time, price, qty, technique, reason, pnl):
        marks.append({
            "kind": "sell", "at": exit_time, "price": price, "qty": qty,
            "label": "매도", "technique": technique, "why": "", "pnl": pnl, "reason": reason,
        })

    # 1) 엔진 스냅샷의 positions(진행 중) + closed(오늘 청산)
    if runner.engine is not None:
        eng = runner.engine
        pos = eng.state.positions.get(symbol)
        if pos is not None:
            name = pos.name or name
            entry_time = iso(pos.entry_time)
            _buy_mark(entry_time, pos.entry_price, pos.quantity, pos.technique, pos.why)

        for t in eng.state.closed:
            if t["symbol"] != symbol:
                continue
            name = t.get("name") or name
            if t["entry_time"] not in seen_entry_times:
                _buy_mark(t["entry_time"], t["entry"], t["qty"], t["technique"], "")
            _sell_mark(t["exit_time"], t["exit"], t["qty"], t["technique"], t["reason"], t["pnl"])

    # 2) 원장 trades(지난 기록) - 같은 entry_time 이면 중복 제외
    for t in Ledger(cfg.state_dir).trades():
        if t["symbol"] != symbol or t["date"] != date:
            continue
        name = t.get("name") or name
        if t["entry_time"] not in seen_entry_times:
            _buy_mark(t["entry_time"], t["entry"], t["qty"], t["technique"], "")
        _sell_mark(t["exit_time"], t["exit"], t["qty"], t["technique"], t["reason"], t["pnl"])

    marks.sort(key=lambda m: m["at"])

    return {
        "symbol": symbol, "name": name, "bars": bars, "marks": marks,
        "stop_pct": cfg.risk.stop_loss_pct, "target_pct": cfg.risk.take_profit_pct,
    }


@app.get("/api/market-chart/{market}/{symbol}")
@api_guard
def get_market_chart(market: str, symbol: str, count: int = 180):
    """해외주식·암호화폐(국내는 /api/chart) 종목의 분봉 시세 + 매수·매도 시점.
    분봉은 시장별 시세원에서 받고, 매수·매도 시점은 엔진(또는 꺼져 있으면 상태 파일)의 보유·청산 기록에서 모은다."""
    from datetime import datetime as _dt, timedelta, timezone
    if market not in ("overseas", "crypto"):
        raise HTTPException(status_code=400, detail="market 은 overseas 또는 crypto 여야 합니다.")
    _check_symbol(market, symbol)
    count = max(20, min(int(count), 200))
    cfg = cfg_now()
    kst = timezone(timedelta(hours=9))

    if market == "crypto":
        from daytrader.bithumb_api import BithumbClient
        from daytrader.crypto_engine import CryptoState
        try:
            rows = BithumbClient().candles(symbol, unit=1, count=count)
        except Exception:
            rows = []
        bars = [{
            "timestamp": str(r.get("candle_date_time_kst") or ""),
            "openPrice": float(r.get("opening_price") or 0), "highPrice": float(r.get("high_price") or 0),
            "lowPrice": float(r.get("low_price") or 0), "closePrice": float(r.get("trade_price") or 0),
            "volume": float(r.get("candle_acc_trade_volume") or 0),
        } for r in rows if isinstance(r, dict)]
        state = _crypto_engine.state if _crypto_engine is not None else CryptoState(os.path.join(cfg.state_dir, "crypto_state.json"))
        key, unit = "market", "won"
        # 빗썸 분봉 시각은 KST 벽시계(오프셋 없음) - 마크도 같은 형식으로 맞춰 분 단위로 정확히 겹치게 한다.
        stamp = lambda ts: _dt.fromtimestamp(ts, kst).strftime("%Y-%m-%dT%H:%M:%S")
    else:
        from daytrader.overseas_engine import OverseasState
        try:
            bars = get_client().candles(symbol, "1m", count)
        except Exception:
            bars = []
        state = _overseas_engine.state if _overseas_engine is not None else OverseasState(os.path.join(cfg.state_dir, "overseas_state.json"))
        key, unit = "symbol", "usd"
        stamp = lambda ts: _dt.fromtimestamp(ts, kst).isoformat()

    marks: list = []
    name = symbol
    pos = state.book.all().get(symbol)
    if pos is not None:
        marks.append({
            "kind": "buy", "at": stamp(pos.entry_time), "price": pos.entry_price, "qty": pos.quantity,
            "label": "매수", "technique": pos.technique, "why": "", "pnl": None, "reason": "",
        })
    for t in state.closed:
        if not isinstance(t, dict) or t.get(key) != symbol:
            continue
        marks.append({
            "kind": "buy", "at": stamp(t["entry_time"]), "price": t.get("entry_price"), "qty": t.get("quantity"),
            "label": "매수", "technique": t.get("entry_technique", ""), "why": "", "pnl": None, "reason": "",
        })
        marks.append({
            "kind": "sell", "at": stamp(t["exit_time"]), "price": t.get("exit_price"), "qty": t.get("quantity"),
            "label": "매도", "technique": t.get("entry_technique", ""), "why": "",
            "pnl": t.get("pnl"), "reason": t.get("reason", ""),
        })
    marks = [m for m in marks if m["price"] is not None]
    marks.sort(key=lambda m: m["at"])
    return {
        "symbol": symbol, "name": name, "unit": unit, "bars": bars, "marks": marks,
        "stop_pct": None, "target_pct": None,
    }


# ━━ 프로세스 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _pid_alive(pid: int) -> bool:
    """★★ os.kill(pid, 0) 은 POSIX 에서만 "그냥 확인"이다. Windows 에서는
    시그널 번호 0 이 CTRL_C_EVENT 와 같은 값이라, os.kill(pid, 0) 을 부르면
    실제로 Ctrl+C 를 그 프로세스가 속한 콘솔 그룹 전체에 방송한다 - 같은
    그룹을 공유하는 호출자 자신(이 서버)도 그 신호를 맞아 멀쩡히 떠 있다가
    "정상 종료(코드 0)"로 꺼져 버린다(실제로 겪은 문제). Windows 에서는
    ctypes 로 핸들을 열어 종료 여부만 확인한다.

    ★ 애매하면(권한 거부 등으로 확인 자체가 안 되면) "살아있다"로 본다.
    이 감시는 부모가 죽었을 때 포트를 물고 남지 않으려는 정리용일 뿐이다.
    확인이 애매한데 죽었다고 단정해 서버를 내려버리면, 그 대가(장중에
    갑자기 멈춘 서버)가 정리 안 된 프로세스 하나 남는 것보다 훨씬 크다.
    """
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_INVALID_PARAMETER = 87  # 그 PID 를 가진 프로세스가 아예 없다.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            if err == ERROR_INVALID_PARAMETER:
                return False  # 확실히 없다.
            return True  # 권한 등 다른 이유 - 모른다. 죽었다고 단정하지 않는다.
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # 조회 자체가 실패 - 역시 단정하지 않는다.
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False  # 확실히 없다.
    except OSError:
        return True  # 권한 등 다른 이유 - 모른다.


def _watch_parent() -> None:
    """DAYTRADER_PARENT_PID 가 죽으면 os._exit(0). 5초 주기.
    ★ 트레이를 끄면 서버도 따라 내려가야 한다. 안 그러면 포트를 물고 남는다.
    """
    parent_pid = os.environ.get("DAYTRADER_PARENT_PID")
    if not parent_pid:
        return
    pid = int(parent_pid)

    def _loop() -> None:
        while True:
            time.sleep(5)
            if not _pid_alive(pid):
                # ★ 왜 죽는지 서버 자신의 로그에 남긴다 - tray.py 가 이 로그의
                # 마지막 부분을 그대로 알림창에 보여주므로, 다음에 이 문제가
                # 또 나면 원인이 이것인지 아닌지 바로 구분할 수 있다.
                print(f"[watch_parent] 부모 프로세스(pid={pid})가 사라져 서버를 종료합니다.", flush=True)
                os._exit(0)

    threading.Thread(target=_loop, daemon=True).start()


def main() -> None:
    _watch_parent()
    cfg = cfg_now()
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(os.path.join(cfg.state_dir, "server.pid"), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    import uvicorn
    host = os.environ.get("DAYTRADER_HOST", "127.0.0.1")
    port = int(os.environ.get("DAYTRADER_PORT", "8000"))

    # ★ host 가 127.0.0.1/localhost 가 아니면 경고한다 - 인증이 없고 계좌를 움직일 수 있다.
    if host not in ("127.0.0.1", "localhost"):
        logging.getLogger(__name__).warning(
            "서버가 %s 에서 열립니다. 이 서버에는 인증이 없고 계좌를 움직일 수 있습니다. "
            "신뢰할 수 없는 네트워크에 노출하지 마세요.", host,
        )

    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
