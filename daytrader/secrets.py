"""API 키 전용 저장소 - secrets.yaml 한 파일로 모은다.

★★★ 왜 분리하나
예전에는 키가 두 곳에 흩어져 있었다:
  - 토스·빗썸 → .env (환경변수 형식)
  - 텔레그램   → config.yaml (평문, 다른 설정과 뒤섞임)
이래서 생긴 실제 문제들:
  ① config.yaml 을 남에게 주거나 백업에 올리면 텔레그램 토큰이 함께 샌다.
     (그래서 화면에 "평문 저장됩니다" 경고를 따로 붙여야 했다.)
  ② 키를 옮기거나 지우려면 두 파일을 다 봐야 한다.
  ③ 설정을 초기화하면 텔레그램 키까지 날아간다.

이제 키는 전부 secrets.yaml 한 곳에 둔다:
  - 이 파일만 백업·공유에서 빼면 된다(.gitignore 에도 이것만 넣으면 된다).
  - config.yaml 은 순수 설정만 담아 안심하고 공유할 수 있다.
  - 파일 권한을 0600(소유자만 읽기)으로 잠근다.

★ 환경변수가 있으면 그쪽이 이긴다 - 서버·CI 에서 파일 없이 주입하는
  방식을 계속 쓸 수 있어야 한다.
"""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import sys

import yaml

from daytrader.paths import app_path

log = logging.getLogger(__name__)

SECRETS_PATH = app_path("secrets.yaml")

# ★★★ secrets.yaml 은 파일 자체로는 평문이라, 실수로 백업/공유/클라우드
# 동기화 폴더에 들어가거나 다른 사람이 이 PC 를 만지면 API 키와 로그인
# 비밀번호가 그대로 읽힌다. 그래서 저장할 때 Windows DPAPI(현재 로그인한
# 윈도우 계정에 묶인 암호화)로 감싼다 - 파일이 그대로 유출돼도 같은
# 컴퓨터의 같은 윈도우 계정이 아니면 복호화할 수 없다. 마스터 비밀번호를
# 따로 만들지 않는 이유: 그러면 프로그램이 자동으로 못 켜지고 매번 그
# 비밀번호를 물어야 한다 - 이 앱은 무인 자동매매가 목적이라 맞지 않는다.
_ENC_PREFIX = "enc:"


def _dpapi_available() -> bool:
    return sys.platform == "win32"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    desc = ctypes.c_wchar_p("AutoDayTrading")
    ok = ctypes.windll.crypt32.CryptProtectData(  # type: ignore[attr-defined]
        ctypes.byref(blob_in), desc, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[attr-defined]


def _dpapi_unprotect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(  # type: ignore[attr-defined]
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[attr-defined]


def _encrypt_value(plain: str) -> str:
    """저장용 문자열로 변환한다. 실패하면(윈도우가 아니거나 DPAPI 오류)
    평문으로라도 저장한다 - 암호화 실패로 키 저장 자체가 안 되면 더
    큰 문제다."""
    if not plain:
        return ""
    if not _dpapi_available():
        return plain
    try:
        blob = _dpapi_protect(plain.encode("utf-8"))
        return _ENC_PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception as exc:
        log.warning("비밀 값을 암호화하지 못했습니다(%s) - 평문으로 저장합니다.", exc)
        return plain


def _decrypt_value(stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        # ★ 예전 버전(암호화 도입 전)에 평문으로 저장된 값. 그대로 읽되,
        #   다음 migrate_plaintext_to_encrypted() 호출 때 암호화된다.
        return stored
    if not _dpapi_available():
        return ""
    try:
        blob = base64.b64decode(stored[len(_ENC_PREFIX):])
        return _dpapi_unprotect(blob).decode("utf-8")
    except Exception as exc:
        log.warning("비밀 값을 복호화하지 못했습니다(%s) - 값이 없는 것으로 처리합니다.", exc)
        return ""

# ★ 키 이름 → (secrets.yaml 안의 위치, 대응하는 환경변수)
#   환경변수 이름은 예전 것을 그대로 쓴다 - 이미 그렇게 쓰던 사용자가
#   설정을 바꾸지 않아도 계속 동작해야 한다.
FIELDS = {
    "toss_client_id": ("toss", "client_id", "TOSS_CLIENT_ID"),
    "toss_client_secret": ("toss", "client_secret", "TOSS_CLIENT_SECRET"),
    "bithumb_access_key": ("bithumb", "access_key", "BITHUMB_ACCESS_KEY"),
    "bithumb_secret_key": ("bithumb", "secret_key", "BITHUMB_SECRET_KEY"),
    "telegram_token": ("telegram", "token", "TELEGRAM_BOT_TOKEN"),
    "telegram_chat_id": ("telegram", "chat_id", "TELEGRAM_CHAT_ID"),
    # AI 뉴스·공시 위험 필터에 쓰는 Groq API 키 2개(무료 플랜 한도가 차면 두 번째로 넘어간다). 없으면 규칙(키워드) 기반으로 돈다.
    "groq_api_key": ("llm", "groq_key", "GROQ_API_KEY"),
    "groq_api_key2": ("llm", "groq_key2", "GROQ_API_KEY2"),
    # ★ 화면 로그인 비밀번호(숫자 6자리)와 그 세션 쿠키 서명에 쓰는 비밀키.
    # 사용자가 직접 넣는 값이 아니라 프로그램이 자동으로 만들어 두는
    # 값이라 환경변수 이름은 거의 안 쓰이지만, 다른 키와 같은 방식
    # (get/save)으로 다루기 위해 형식만 맞춘다.
    "auth_password": ("auth", "password", "DAYTRADER_AUTH_PASSWORD"),
    "auth_session_secret": ("auth", "session_secret", "DAYTRADER_AUTH_SESSION_SECRET"),
    # ★ 최초 실행 때 서버가 스스로 지어 저장한 임시 비밀번호인지 표시한다("1"이면 그렇다).
    # 사용자가 /api/auth/password 로 직접 바꾸면 지운다 - 그 뒤로는 원격에서도 로그인을 허용해도 된다.
    "auth_password_is_default": ("auth", "password_is_default", "DAYTRADER_AUTH_PASSWORD_IS_DEFAULT"),
    # ★ 새 기기 승인 목록(devices.json) 서명 키 - 위와 같은 이유로 자동 생성된다. 이 키가 없으면
    # devices.json 을 이 프로그램 밖에서 손댔는지 확인할 수 없다.
    "devices_integrity_key": ("auth", "devices_integrity_key", "DAYTRADER_DEVICES_INTEGRITY_KEY"),
}

_TEMPLATE = """# ━━ API 키 (이 파일은 절대 공유하지 마세요) ━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# ★ 이 파일 하나만 백업·공유·버전관리에서 빼면 됩니다.
#   config.yaml 에는 키가 들어가지 않으니 마음 놓고 공유할 수 있습니다.
#
# ★ 값을 비워 두면 그 기능만 꺼집니다 - 전부 비어 있어도 연습 모드는
#   인터넷 공개 시세로 정상 동작합니다.
#
# ★ 환경변수(TOSS_CLIENT_ID 등)가 설정돼 있으면 그쪽이 우선합니다.

toss:
  client_id: ""
  client_secret: ""

bithumb:
  access_key: ""
  secret_key: ""

telegram:
  token: ""
  chat_id: ""
"""


def _read_file() -> dict:
    """★ 파일이 없거나 깨져도 예외를 내지 않는다 - 키가 없는 것은
    정상 상태이고(연습 모드), 그것 때문에 프로그램이 못 뜨면 안 된다."""
    if not os.path.exists(SECRETS_PATH):
        return {}
    try:
        with open(SECRETS_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("secrets.yaml 을 읽지 못했습니다(%s) - 키가 없는 것으로 봅니다.", exc)
        return {}


def _write_file(data: dict) -> None:
    """★ 저장 후 권한을 0600 으로 잠근다 - 같은 PC 의 다른 계정이
    읽지 못하게. 윈도우에서는 chmod 가 무시되지만 예외는 안 난다."""
    with open(SECRETS_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        pass


def get(name: str) -> str:
    """키 하나를 읽는다. 환경변수 > secrets.yaml 순서."""
    section, key, env = FIELDS[name]
    env_value = os.environ.get(env, "").strip()
    if env_value:
        return env_value
    data = _read_file()
    value = (data.get(section) or {}).get(key, "")
    return _decrypt_value(str(value).strip()) if value else ""


def all_values() -> dict:
    return {name: get(name) for name in FIELDS}


def save(values: dict) -> None:
    """넘어온 키만 고친다 - 토스를 저장할 때 빗썸이 지워지면 안 된다.

    ★ 값이 None 이면 건드리지 않고, 빈 문자열이면 지우는 것으로 본다
      (키를 빼는 것도 사용자의 선택이다).
    """
    data = _read_file()
    for name, value in values.items():
        if name not in FIELDS or value is None:
            continue
        section, key, _env = FIELDS[name]
        data.setdefault(section, {})
        plain = str(value).strip()
        data[section][key] = _encrypt_value(plain)
        # ★ 같은 프로세스 안에서 바로 반영되게 환경변수도 맞춰 준다 -
        #   저장 직후 "등록 안 됨"으로 보이던 문제를 막는다.
        env = FIELDS[name][2]
        if plain:
            os.environ[env] = plain
        else:
            os.environ.pop(env, None)
    _write_file(data)


def ensure_file() -> None:
    """★ 파일이 없으면 주석이 달린 빈 템플릿을 만들어 둔다 - 사용자가
    어디에 무엇을 넣어야 하는지 파일만 열어도 알 수 있게."""
    if os.path.exists(SECRETS_PATH):
        return
    try:
        with open(SECRETS_PATH, "w", encoding="utf-8") as f:
            f.write(_TEMPLATE)
        try:
            os.chmod(SECRETS_PATH, 0o600)
        except OSError:
            pass
    except OSError as exc:
        log.warning("secrets.yaml 을 만들지 못했습니다: %s", exc)


def migrate_plaintext_to_encrypted() -> int:
    """★ 암호화 도입 이전 버전에서 평문으로 저장된 값을 전부 암호화해서
    다시 쓴다. 새 버전으로 올리기만 해도 자동으로 적용되게 하기 위해
    프로세스 시작 시 한 번 호출한다(ensure_file() 바로 뒤)."""
    if not _dpapi_available():
        return 0
    data = _read_file()
    changed = 0
    for section, key, _env in FIELDS.values():
        raw = (data.get(section) or {}).get(key, "")
        raw = str(raw).strip() if raw else ""
        if raw and not raw.startswith(_ENC_PREFIX):
            data.setdefault(section, {})[key] = _encrypt_value(raw)
            changed += 1
    if changed:
        _write_file(data)
        log.info("secrets.yaml 의 평문 값 %d개를 암호화했습니다.", changed)
    return changed


def migrate_from_legacy(env_path: str, config_raw: dict | None = None) -> list:
    """★★★ 예전 위치(.env, config.yaml)에 있던 키를 secrets.yaml 로 옮긴다.

    이미 쓰고 있던 사람이 키를 다시 입력하게 만들면 안 된다 - 프로그램을
    새 버전으로 바꿨을 뿐인데 갑자기 "키가 없습니다"가 뜨면 그건 고장이다.
    ★ 옮긴 뒤 원본은 지우지 않는다 - 되돌릴 여지를 남긴다(특히 .env 는
      다른 도구가 함께 쓸 수 있다).
    ★ 이미 secrets.yaml 에 값이 있으면 덮어쓰지 않는다.
    """
    moved = []
    current = _read_file()

    def _already(name):
        section, key, _ = FIELDS[name]
        return bool((current.get(section) or {}).get(key))

    picked = {}

    # ① .env 에서 (토스·빗썸)
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    for name, (_s, _k, env) in FIELDS.items():
                        if k.strip() == env and v.strip() and not _already(name):
                            picked[name] = v.strip()
                            moved.append(name)
        except Exception as exc:
            log.warning(".env 를 읽지 못했습니다: %s", exc)

    # ② config.yaml 에서 (텔레그램)
    if isinstance(config_raw, dict):
        notify = config_raw.get("notify") or {}
        for name, key in (("telegram_token", "telegram_token"),
                          ("telegram_chat_id", "telegram_chat_id")):
            value = notify.get(key)
            if value and not _already(name):
                picked[name] = str(value).strip()
                moved.append(name)

    if picked:
        save(picked)
        log.info("API 키 %d개를 secrets.yaml 로 옮겼습니다: %s", len(moved), ", ".join(moved))
    return moved
