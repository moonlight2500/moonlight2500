"""사내 프록시 인증서 문제 해결.

사내망·VPN·보안 프로그램이 깔린 PC 에서는 평범한 HTTPS 요청이 실패한다.
실패 메시지가 'certificate verify failed' 라서 사용자는 프로그램이 고장난 줄
안다. 실제로는 회사 프록시가 TLS 를 중간에서 다시 서명하는데 그 CA 인증서를
파이썬이 신뢰하지 않는 것뿐이다.

그래서 이렇게 한다:
  1) 먼저 아무것도 하지 않고 평범하게 접속한다. 대부분의 환경은 이걸로 끝난다.
  2) 인증서 오류가 나면 그때만 운영체제 신뢰 저장소를 쓴다(truststore).
     브라우저가 되는 환경이면 이걸로 된다.
  3) 그래도 안 되면 무엇이 왜 막혔는지 사람이 읽을 수 있게 알려준다.
한 번 성공한 방식은 기억해 두고 다음부터 바로 쓴다.
"""

from __future__ import annotations

import os
import ssl
import time as _time

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import ConnectTimeout, ProxyError, SSLError, Timeout

MODE_DIRECT = "direct"
MODE_SYSTEM = "system-trust"
MODE_INSECURE = "insecure"

_MODE_LABELS = {
    MODE_DIRECT: "직접 연결",
    MODE_SYSTEM: "운영체제 신뢰 저장소 사용",
    MODE_INSECURE: "인증서 검증 끔 (위험)",
}

# ★ HTTPS_PROXY 가 잡혀 있으면 트레이가 자기 서버를 부를 때도 프록시로 나가서
# "서버는 살아있는데 응답을 못 받는" 상태가 된다. 로컬 주소는 프록시에서 뺀다.
_LOCAL = "127.0.0.1,localhost,::1,0.0.0.0"


def _ensure_no_proxy_locals() -> None:
    for key in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(key, "")
        have = {h.strip() for h in current.split(",") if h.strip()}
        missing = [h for h in _LOCAL.split(",") if h not in have]
        if missing:
            merged = ",".join([current, *missing]) if current else ",".join(missing)
            os.environ[key] = merged.strip(",")


_ensure_no_proxy_locals()

_cached_mode: str | None = None


def proxies_in_use() -> dict:
    """진단 화면에 그대로 보여줄 현재 프록시 관련 환경변수."""
    keys = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")
    return {k: v for k, v in os.environ.items() if k in keys}


def _system_trust_adapter() -> HTTPAdapter:
    """truststore 의 SSLContext(운영체제 신뢰 저장소)를 쓰는 어댑터.
    init_poolmanager 와 proxy_manager_for 둘 다 오버라이드해야
    프록시를 거치는 경우에도 같은 신뢰 저장소가 적용된다.
    """
    import truststore

    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    class _SystemTrustAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["ssl_context"] = ctx
            return super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, *args, **kwargs):
            kwargs["ssl_context"] = ctx
            return super().proxy_manager_for(*args, **kwargs)

    return _SystemTrustAdapter()


def _insecure_adapter() -> HTTPAdapter:
    """인증서 검증을 끈다. 사용자가 명시적으로 켤 때만 쓴다."""
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return HTTPAdapter()


_ipv4_forced = False


def force_ipv4() -> None:
    """★ 증권사 Open API 의 '허용 IP 관리'는 거의 항상 IPv4 만 받는다. PC 가
    듀얼스택(IPv4+IPv6) 이면 파이썬이 IPv6 로 먼저 나갈 수 있는데, 그러면
    등록해 둔 IPv4 주소와 실제 접속 경로가 달라 키가 맞아도 403 이 나고
    사용자는 원인을 알 방법이 없다(실제로 겪은 문제).

    ★ urllib3 의 주소 조회 함수를 바꿔치기하는 방식이라 이 프로세스의 모든
    HTTP 요청에 적용된다(세션 하나만이 아니다). 증권사 API 뿐 아니라 다른
    호출도 전부 IPv4 로 나가지만, IPv6 전용이어야만 접속되는 서버는 사실상
    없으므로 부작용은 없다 - 오히려 "어디를 등록해야 하는지"가 하나로
    고정되어 사용자에게 더 명확하다. 한 프로세스에서 한 번만 적용하면
    된다.
    """
    global _ipv4_forced
    if _ipv4_forced:
        return
    import socket
    import urllib3.util.connection as urllib3_conn

    urllib3_conn.allowed_gai_family = lambda: socket.AF_INET
    _ipv4_forced = True


def _session_for_mode(mode: str) -> requests.Session:
    s = requests.Session()
    # ★★ requests 기본 User-Agent("python-requests/2.x")는 네이버·야후 등
    # 많은 사이트에서 봇 트래픽으로 간주되어 403으로 차단된다(실제로 겪은
    # 문제 - 시장 탭 전 그룹 조회실패). 평범한 브라우저처럼 보이게 한다.
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    if mode == MODE_SYSTEM:
        adapter = _system_trust_adapter()
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    elif mode == MODE_INSECURE:
        s.verify = False
        adapter = _insecure_adapter()
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    return s


def _probe(url: str, *, allow_insecure: bool, timeout: float) -> str:
    """어떤 방식이어야 접속이 되는지 한 번만 확인한다.
    결과는 모듈 전역에 기억해 두고 다음부터 바로 재사용한다.
    """
    global _cached_mode
    if _cached_mode is not None:
        return _cached_mode

    # 1) 아무것도 하지 않고 평범하게 접속한다.
    try:
        requests.get(url, timeout=timeout)
        _cached_mode = MODE_DIRECT
        return _cached_mode
    except SSLError:
        pass
    except Exception:
        # 인증서 문제가 아니라 네트워크 자체가 막힌 경우다.
        # 여기서 방식을 바꿔도 해결되지 않으므로 direct 를 유지해
        # 실제 호출에서 나는 오류가 진짜 원인을 가리키게 둔다.
        _cached_mode = MODE_DIRECT
        return _cached_mode

    # 2) 인증서 오류일 때만 운영체제 신뢰 저장소를 시도한다.
    try:
        s = _session_for_mode(MODE_SYSTEM)
        s.get(url, timeout=timeout)
        _cached_mode = MODE_SYSTEM
        return _cached_mode
    except Exception:
        pass

    # 3) 그래도 안 되면, 사용자가 명시적으로 허용한 경우에만 검증을 끈다.
    if allow_insecure:
        _cached_mode = MODE_INSECURE
        return _cached_mode

    _cached_mode = MODE_DIRECT  # 실패 사유는 explain_failure 가 설명한다.
    return _cached_mode


def make_session(*, allow_insecure: bool = False, probe_url: str | None = None, timeout: float = 10.0) -> requests.Session:
    """탐색된(혹은 이전에 기억해 둔) 접속 방식으로 세션을 만든다."""
    target = probe_url or "https://www.google.com"
    mode = _probe(target, allow_insecure=allow_insecure, timeout=timeout)
    return _session_for_mode(mode)


def reset() -> None:
    """기억한 접속 방식을 지운다. 네트워크 환경이 바뀌었을 때 쓴다."""
    global _cached_mode
    _cached_mode = None
    _diag_cache["data"] = None  # 환경이 바뀌었다면 이전 진단 결과도 믿을 수 없다.


_diag_cache: dict = {"at": 0.0, "key": None, "data": None}


def diagnose_cached(ttl: float = 30.0, allow_insecure: bool = False) -> dict:
    """★ 진단은 외부 사이트 4곳에 실제 접속해서 0.5초쯤 걸린다. 준비·연결 화면을 한 번
    열 때 /api/integrations 와 /api/net/diagnose 가 각각 부르고 있어 같은 진단이
    두세 번씩 돌았다 - 몇 초 안에 다시 부르면 방금 결과를 그대로 준다.
    (reset() 은 이 캐시도 비운다.)"""
    now = _time.monotonic()
    c = _diag_cache
    if c["data"] is not None and c["key"] == allow_insecure and now - c["at"] < ttl:
        return c["data"]
    data = diagnose(allow_insecure=allow_insecure)
    c["at"], c["key"], c["data"] = now, allow_insecure, data
    return data


def explain_failure(exc: Exception) -> str:
    """예외를 그대로 보여주지 않고, 무엇이 막혔고 무엇을 하면 되는지 알려준다."""
    if isinstance(exc, SSLError):
        msg = "회사·학교 네트워크나 보안 프로그램이 통신을 중간에서 다시 서명하는 환경일 가능성이 큽니다."
        if "Basic Constraints" in str(exc):
            msg += " (인증서에 Basic Constraints 오류가 있어 자체 서명된 중간 인증서로 보입니다.)"
        msg += " 해결: pip install truststore 후 다시 시도해 보세요."
        return msg
    if isinstance(exc, ProxyError):
        return "프록시 서버에 연결하지 못했습니다. 사내 프록시 주소·인증 정보를 확인하세요."
    if isinstance(exc, ConnectTimeout):
        return "연결 시간이 초과되었습니다. 방화벽이나 VPN 이 해당 주소를 막고 있는지 확인하세요."
    if isinstance(exc, (ReqConnectionError, Timeout)):
        return "서버에 연결할 수 없습니다. 인터넷 연결 또는 대상 서버 상태를 확인하세요."
    return f"알 수 없는 통신 오류입니다: {exc}"


_DIAG_TARGETS = [
    {"key": "toss_oauth", "label": "토스 OAuth", "method": "POST",
     "url": "https://openapi.tossinvest.com/oauth2/token-p"},
    {"key": "naver_price", "label": "네이버 실시간시세", "method": "GET",
     "url": "https://polling.finance.naver.com/api/realtime/domestic/stock/005930"},
    {"key": "naver_bar", "label": "네이버 분봉", "method": "GET",
     "url": "https://api.finance.naver.com/siseJson.naver?symbol=005930&timeframe=day&count=1&requestType=0"},
    {"key": "google_news", "label": "구글뉴스 RSS", "method": "GET",
     "url": "https://news.google.com/rss/search?q=%EC%A6%9D%EC%8B%9C&hl=ko"},
]


def diagnose(allow_insecure: bool = False) -> dict:
    """4개 외부 대상에 실제로 접속해보고 통신 가능 여부를 진단한다.
    ★ 401·400 은 "서버에 닿았고 자격증명만 없다"는 뜻이다. status<500 을 성공으로 본다.
    401 → 자격증명만 없음(정상). 403 → 서버 정책 문제(통신 문제는 아님).
    """
    probe_target = "https://www.google.com"
    session = make_session(allow_insecure=allow_insecure, probe_url=probe_target)

    results = []
    all_ok = True
    for t in _DIAG_TARGETS:
        started = _time.monotonic()
        entry = {"key": t["key"], "label": t["label"], "ok": False, "status": None, "ms": None, "detail": ""}
        try:
            if t["method"] == "POST":
                resp = session.post(t["url"], timeout=10.0)
            else:
                resp = session.get(t["url"], timeout=10.0)
            entry["ms"] = round((_time.monotonic() - started) * 1000)
            entry["status"] = resp.status_code
            if resp.status_code < 500:
                if resp.status_code == 401:
                    # ★ 401 은 진짜 정상이다 - 이 진단은 인증 없이 부르므로
                    # "인증만 없고 서버에는 닿았다"는 뜻이다.
                    entry["ok"] = True
                    entry["detail"] = "서버에 닿았습니다. 인증 정보만 없습니다(정상)."
                elif resp.status_code == 403:
                    # ★★★ 실제로 겪은 문제 - 403 을 "통신 문제는 아니다"라며
                    # ok=True 로 처리했다. 하지만 이 진단의 목적은 "시세를
                    # 받아올 수 있는가"이지 "TCP 가 연결되는가"가 아니다.
                    # 403 이면 데이터를 한 줄도 못 받으므로 시세 조회는
                    # 실패한다 - 그런데 진단은 전부 초록불이라, 종목 선정이
                    # 안 되는 원인을 찾을 수가 없었다. 실패로 정확히 잡는다.
                    entry["ok"] = False
                    entry["detail"] = (
                        "서버가 접근을 거부했습니다(403). 통신은 됐지만 데이터를 받지 못하므로 "
                        "이 소스로는 시세를 조회할 수 없습니다 - 회사·학교 네트워크나 백신·방화벽이 "
                        "막고 있거나, 해당 사이트가 자동 조회를 차단한 경우입니다."
                    )
                elif resp.status_code == 429:
                    entry["ok"] = False
                    entry["detail"] = "요청이 너무 잦아 일시적으로 차단됐습니다(429). 잠시 후 다시 시도하세요."
                else:
                    entry["ok"] = True
                    entry["detail"] = "정상 응답."
            else:
                entry["detail"] = f"서버 오류 응답 ({resp.status_code})."
        except Exception as exc:
            entry["ms"] = round((_time.monotonic() - started) * 1000)
            entry["detail"] = explain_failure(exc)
        results.append(entry)
        all_ok = all_ok and entry["ok"]

    return {
        "mode": _cached_mode,
        "mode_label": _MODE_LABELS.get(_cached_mode, _cached_mode),
        "proxies": proxies_in_use(),
        "targets": results,
        "ok": all_ok,
        "probe": probe_target,
    }
