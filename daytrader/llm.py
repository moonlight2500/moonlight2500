"""AI(LLM) 호출 한 곳 - 뉴스 위험 필터에만 쓴다.

제공자 순서: Groq 키 1 -> Groq 키 2(백업). 앞의 것이 한도 초과·인증 오류·네트워크 오류면 다음 것으로
넘어가고, 전부 안 되면 None 을 돌려준다 - 호출하는 쪽이 규칙(키워드) 기반으로 대신한다(AI 는 어디까지나 보조다).

한도 초과(429)·인증 오류(401/403)가 난 키는 잠시 쉬게 한다(그동안 다른 키를 쓴다). 키는 헤더로만 보내고 로그·오류에 남기지 않는다.
Groq 는 OpenAI 호환 chat completions 형식을 쓴다(무료 플랜 - LPU 기반이라 응답이 빠르다).
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
COOLDOWN_QUOTA = 30 * 60   # 한도 초과(429) - 30분 쉼
COOLDOWN_AUTH = 6 * 3600   # 키 오류(401/403) - 6시간 쉼

_lock = threading.Lock()
_cool: dict = {}        # 라벨 -> 다시 써도 되는 시각
_state = {"last_error": "", "last_provider": "", "calls": {}}


def providers(cfg) -> list:
    """(라벨, 종류, 키) 목록 - 등록된 키만."""
    out = []
    for label, attr, kind in (("Groq 키 1", "groq_api_key", "groq"), ("Groq 키 2", "groq_api_key2", "groq")):
        key = (getattr(cfg, attr, "") or "").strip()
        if key:
            out.append((label, kind, key))
    return out


def available(cfg) -> bool:
    return bool(providers(cfg))


def status() -> dict:
    with _lock:
        return {"last_error": _state["last_error"], "last_provider": _state["last_provider"], "calls": dict(_state["calls"]),
                "cooling": {k: int(v - time.time()) for k, v in _cool.items() if v > time.time()}}


def _default_post(url: str, headers: dict, body: dict, timeout: float):
    import requests
    return requests.post(url, headers=headers, json=body, timeout=timeout)


def _request(kind: str, key: str, cfg, system: str, user: str, max_tokens: int, temperature: float, post):
    """Groq 요청 한 번 -> 응답 객체(OpenAI 호환 chat completions). 예외는 호출부가 처리한다.
    키는 Authorization 헤더로만 보낸다(URL 에 넣지 않는다).
    ★★★ 실제로 겪은 문제 - 지금 Groq 가 주는 모델(openai/gpt-oss-*)은 "추론형"이라, 답을 내놓기
    전에 속으로 생각하는 과정(reasoning)에 먼저 토큰을 쓴다. reasoning_effort 를 낮추지 않으면
    짧은 max_tokens 안에서 생각만 하다 끝나 content 가 빈 문자열로 오고(응답 형식은 정상, 그래서
    HTTP 오류가 아니라 "빈 응답"으로 실패), 헤드라인 몇 줄 거르는 이 정도 일에는 낮은 reasoning
    으로 충분하다."""
    model = getattr(cfg.news, "groq_model", "openai/gpt-oss-20b")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
            "reasoning_effort": "low"}
    return post(GROQ_URL, {"Authorization": f"Bearer {key}", "content-type": "application/json"}, body, 20)


def _extract(kind: str, data: dict) -> str:
    choices = data.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    return (msg.get("content") or "").strip()


def _error_detail(resp) -> str:
    """오류 응답 본문에서 사람이 읽을 메시지를 뽑는다(모델 이름 오류인지 등 원인을 화면에서 바로 보이게).
    OpenAI 호환 형식은 보통 {"error": {"message": "..."}}. 실패하면 본문 앞부분을 그대로 잘라 쓴다."""
    try:
        data = resp.json()
        msg = ((data.get("error") or {}) if isinstance(data, dict) else {}).get("message")
        if msg:
            return str(msg)[:150]
    except Exception:
        pass
    try:
        return (resp.text or "")[:150]
    except Exception:
        return ""


def _try(label: str, kind: str, key: str, cfg, system, user, max_tokens, temperature, post):
    """한 제공자를 시도한다. (텍스트, 오류메시지). 텍스트가 있으면 성공."""
    try:
        resp = _request(kind, key, cfg, system, user, max_tokens, temperature, post)
    except Exception as exc:
        return None, f"{label}: 연결 실패({type(exc).__name__})", 0
    code = getattr(resp, "status_code", 0)
    if code == 429:
        return None, f"{label}: 한도 초과(429)", COOLDOWN_QUOTA
    if code in (401, 403):
        return None, f"{label}: 키가 거절됨({code}) - {_error_detail(resp)}", COOLDOWN_AUTH
    if code >= 400:
        return None, f"{label}: 서버 오류({code}) - {_error_detail(resp)}", 0
    try:
        text = _extract(kind, resp.json()).strip()
    except Exception:
        return None, f"{label}: 응답 형식 오류", 0
    if not text:
        return None, f"{label}: 빈 응답", 0
    return text, "", 0


def complete(cfg, system: str, user: str, max_tokens: int = 300, temperature: float = 0.2, *, post=None):
    """텍스트 한 덩이를 돌려준다. 어떤 제공자도 못 쓰면 None."""
    post = post or _default_post
    errors = []
    for label, kind, key in providers(cfg):
        with _lock:
            if _cool.get(label, 0) > time.time():
                continue
        text, err, cool = _try(label, kind, key, cfg, system, user, max_tokens, temperature, post)
        if text:
            with _lock:
                _state["last_provider"] = label
                _state["calls"][label] = _state["calls"].get(label, 0) + 1
                _state["last_error"] = "; ".join(errors)  # 앞선 키가 실패해 넘어왔다면 그 사유를 남긴다
            return text
        errors.append(err)
        if cool:
            with _lock:
                _cool[label] = time.time() + cool
    with _lock:
        _state["last_error"] = "; ".join(errors) if errors else "등록된(또는 사용 가능한) AI 키가 없습니다."
    log.warning("AI 호출 실패 - 규칙 기반으로 대신합니다: %s", _state["last_error"])
    return None


def test_all(cfg, *, post=None) -> list:
    """등록된 키를 하나씩 시험한다(쉬는 중인 키도 무시하고 시도). [{label, ok, error}]"""
    post = post or _default_post
    out = []
    for label, kind, key in providers(cfg):
        # ★★★ 실제로 겪은 문제 - 토큰 한도가 너무 빠듯하면(예전엔 20) 추론형 모델이 답을 내놓기
        # 전에 속으로 생각하는 데(reasoning)만 다 써버려 "빈 응답"으로 잘못 실패 처리됐다(연결은
        # 멀쩡한데 연결 테스트만 거짓으로 실패). 실제 필터 호출(150)보다야 훨씬 짧지만 여유를 둔다.
        text, err, _cool_s = _try(label, kind, key, cfg, "", "한 단어로만 답하세요: 확인", 60, 0.0, post)
        if text:
            with _lock:
                _cool.pop(label, None)
        out.append({"label": label, "ok": bool(text), "error": err})
    return out


def reset_for_tests() -> None:
    with _lock:
        _cool.clear()
        _state.update(last_error="", last_provider="", calls={})
