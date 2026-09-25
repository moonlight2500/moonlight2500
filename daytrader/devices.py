"""접속 기기 승인.

★★★ 실제로 겪은 문제 - 처음엔 "IP+User-Agent"로 기기를 식별했는데, 휴대폰은 와이파이↔데이터
전환이나 통신사 쪽 사정으로 접속할 때마다 IP 가 자주 바뀐다. 그래서 같은 휴대폰·같은 브라우저인데도
IP 가 바뀔 때마다 "처음 보는 기기"로 취급돼 매번 다시 승인해야 했다(승인해도 승인해도 계속 뜸).
그래서 지금은 브라우저에 심어 두는 오래가는 쿠키(daytrader_device, 약 400일)의 무작위 토큰으로
기기를 식별한다 - 이 토큰은 IP 가 바뀌어도 그대로라 한 번 승인하면 계속 유지된다. IP·User-Agent 는
이제 식별에는 안 쓰고, 관리자 화면에 "어디서 왔는지" 보여주는 참고 정보로만 남긴다(그 브라우저가
쿠키를 지우면 - 사생활 보호 모드, 앱 데이터 삭제 등 - 새 토큰이 발급되어 다시 승인이 필요하다).

처음 보는 기기가 비밀번호까지 맞게 입력하면 곧바로 로그인시키지 않고 "승인 대기" 상태로 잡아
둔다. 서버가 도는 PC에서만 열리는 관리자 페이지(/admin, server.py 의 _is_admin_local)에서
그 목록을 보고 승인·거부한다. 한 번 승인된 기기는 devices.json 의 trusted 에 남아, 이후로는
비밀번호만으로 다시 접속할 수 있다(재승인 불필요).

파일 하나(devices.json)에 pending·trusted 를 같이 둔다. 요청이 드물어(로그인 시점뿐) 매번
새로 읽고 쓰는 것으로 충분하다 - 이 프로세스는 하나뿐이라 락으로 동시쓰기만 막으면 된다.

★★★ 무결성 서명 - devices.json 은 평범한 텍스트 파일이라, 이 프로그램을 거치지 않고(예: 메모장으로
직접 열어) 신뢰 목록에 항목을 끼워 넣으면 승인 절차 전체가 의미 없어진다. 그래서 저장할 때마다
이 PC에만 있는 비밀 키(secrets.yaml, DPAPI 로 암호화)로 내용 전체에 서명(HMAC-SHA256)을 남기고,
불러올 때 그 서명을 다시 확인한다. 서명이 없거나 안 맞으면(=이 프로그램 밖에서 손댄 흔적) 그 파일은
통째로 버리고 빈 상태로 다시 시작한다 - 조작된 신뢰 목록을 안전한 것으로 착각하느니, 전부 다시
승인받는 쪽(안전한 쪽)을 택한다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets as _secrets_mod
import threading
import time

PENDING_TTL = 30 * 60  # 30분 안에 관리자 페이지에서 승인·거부하지 않으면 자동 만료
DENIED_COOLDOWN = 60 * 60  # ★ 거부된 기기는 1시간 동안 다시 승인 대기를 만들 수 없다 - 비밀번호를
# 맞힌(=이미 위험한) 원격 상대가 거부당한 직후 곧바로 다시 시도해 관리자 페이지에 계속 새 요청을
# 띄우는 것(성가심을 넘어 "혹시 실수로 눌러주지 않을까" 노리는 사회공학)을 막는다.

_lock = threading.Lock()


def _integrity_key() -> str:
    """devices.json 서명에 쓰는 비밀 키 - 처음 쓸 때 한 번만 만들어 secrets.yaml(암호화)에 저장한다.
    daytrader.secrets 를 지연 임포트한다(devices.py 는 순환 임포트를 피해 가볍게 유지한다)."""
    from daytrader import secrets as _secrets_store
    value = _secrets_store.get("devices_integrity_key")
    if not value:
        value = os.urandom(32).hex()
        _secrets_store.save({"devices_integrity_key": value})
    return value


def _sign(payload: dict) -> str:
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hmac.new(_integrity_key().encode("utf-8"), canon.encode("utf-8"), hashlib.sha256).hexdigest()


def new_device_token() -> str:
    """새 기기 쿠키(daytrader_device)에 심을 무작위 토큰 - server.py 가 쿠키에 없을 때 한 번만 만든다."""
    return _secrets_mod.token_urlsafe(32)


def key_for_token(token: str) -> str:
    """기기 쿠키 토큰을 그대로 저장하지 않고 해시해 키로 쓴다(devices.json 이 유출돼도 쿠키 값
    자체는 새지 않게). IP·User-Agent 는 더 이상 키에 안 들어간다(휴대폰은 IP 가 자주 바뀐다)."""
    return hashlib.sha256(f"device:{token}".encode("utf-8")).hexdigest()


class DeviceStore:
    def __init__(self, path: str):
        self.path = path
        self._data = {"trusted": {}, "pending": {}, "denied": {}}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            loaded = {
                "trusted": dict(data.get("trusted") or {}), "pending": dict(data.get("pending") or {}),
                "denied": dict(data.get("denied") or {}),
            }
            sig = data.get("_sig", "")
            # ★★★ 서명이 없거나 안 맞으면 이 프로그램 밖에서 손댄 파일이다 - 통째로 버린다(안전한
            # 쪽으로. 조작된 신뢰 목록을 그대로 믿으면 승인 절차 전체가 뚫린다).
            if not sig or not hmac.compare_digest(sig, _sign(loaded)):
                try:
                    os.replace(self.path, f"{self.path}.tampered-{int(time.time())}")
                except Exception:
                    pass
                self._data = {"trusted": {}, "pending": {}, "denied": {}}
                return
            self._data = loaded
        except Exception:
            self._data = {"trusted": {}, "pending": {}, "denied": {}}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        payload = dict(self._data)
        payload["_sig"] = _sign(self._data)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ── 신뢰된 기기 ──

    def is_trusted(self, key: str) -> bool:
        with _lock:
            return key in self._data["trusted"]

    def touch(self, key: str) -> None:
        """신뢰된 기기가 다시 로그인하면 마지막 접속 시각만 갱신한다."""
        with _lock:
            if key in self._data["trusted"]:
                self._data["trusted"][key]["last_seen"] = time.time()
                self._save()

    def trust_directly(self, key: str, ip: str, ua: str) -> None:
        """대기·승인 절차 없이 곧바로 신뢰 목록에 넣는다 - 서버가 도는 이 PC에서 직접 온 접속처럼,
        이미 물리적으로 그 PC를 쓰고 있다는 사실 자체가 승인과 같은 수준의 신뢰이기 때문이다."""
        with _lock:
            self._data["trusted"][key] = {
                "ip": ip, "ua": (ua or "")[:200], "approved_at": time.time(), "last_seen": time.time(),
            }
            self._save()

    def list_trusted(self) -> list:
        with _lock:
            return [dict(v, key=k) for k, v in self._data["trusted"].items()]

    def revoke(self, key: str) -> bool:
        with _lock:
            existed = self._data["trusted"].pop(key, None) is not None
            if existed:
                self._save()
            return existed

    # ── 승인 대기 ──

    def find_pending_for(self, key: str):
        """이미 대기 중인(안 만료된) 요청이 있으면 그 토큰을 그대로 쓴다 - 다시 시도할 때마다
        목록에 새 줄이 늘어나지 않게 한다."""
        with _lock:
            now = time.time()
            for token, p in self._data["pending"].items():
                if p["key"] == key and p["status"] == "pending" and p["expires_at"] > now:
                    return token
            return None

    def denied_cooldown_remaining(self, key: str) -> float:
        """이 기기가 최근에 거부됐고 아직 쉬는 시간(DENIED_COOLDOWN)이 안 지났으면 남은 초, 아니면 0."""
        with _lock:
            at = self._data["denied"].get(key)
            if not at:
                return 0.0
            remaining = (at + DENIED_COOLDOWN) - time.time()
            return max(0.0, remaining)

    def create_pending(self, key: str, ip: str, ua: str) -> str:
        token = _secrets_mod.token_urlsafe(24)
        with _lock:
            self._data["pending"][token] = {
                "key": key, "ip": ip, "ua": (ua or "")[:200],
                "requested_at": time.time(), "expires_at": time.time() + PENDING_TTL,
                "status": "pending",
            }
            self._save()
        return token

    def list_pending(self) -> list:
        """관리자 페이지용 - 만료된 건 status 를 expired 로 바꿔서 같이 보여준다(치웠는지 헷갈리지 않게)."""
        with _lock:
            now = time.time()
            out = []
            for token, p in self._data["pending"].items():
                row = dict(p, token=token)
                if row["status"] == "pending" and row["expires_at"] < now:
                    row["status"] = "expired"
                out.append(row)
            out.sort(key=lambda r: r["requested_at"], reverse=True)
            return out

    def get_pending(self, token: str):
        with _lock:
            p = self._data["pending"].get(token)
            if not p:
                return None
            row = dict(p)
            if row["status"] == "pending" and row["expires_at"] < time.time():
                row["status"] = "expired"
            return row

    def decide(self, token: str, approve: bool) -> bool:
        """관리자 페이지에서 승인/거부. 이미 처리됐거나 만료됐으면 False."""
        with _lock:
            p = self._data["pending"].get(token)
            if not p or p["status"] != "pending" or p["expires_at"] < time.time():
                return False
            p["status"] = "approved" if approve else "denied"
            if approve:
                self._data["trusted"][p["key"]] = {
                    "ip": p["ip"], "ua": p["ua"], "approved_at": time.time(), "last_seen": time.time(),
                }
                self._data["denied"].pop(p["key"], None)  # 예전에 거부했다가 이번엔 승인 - 쉬는 시간 해제
            else:
                self._data["denied"][p["key"]] = time.time()
            self._save()
            return True

    def pop(self, token: str) -> None:
        """대기 중이던 클라이언트가 결과(승인/거부/만료)를 확인해 갔으면 목록에서 지운다."""
        with _lock:
            if token in self._data["pending"]:
                self._data["pending"].pop(token, None)
                self._save()
