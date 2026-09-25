"""새 기기 승인(daytrader/devices.py) 테스트.
`python tests/test_devices.py` 로 실행한다.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader import devices  # noqa: E402

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


def new_store():
    return devices.DeviceStore(os.path.join(tempfile.mkdtemp(), "devices.json"))


def test_key() -> None:
    print("== 기기 키(쿠키 토큰 기반 - IP·User-Agent 는 더 이상 식별에 안 쓴다) ==")
    t1, t2 = devices.new_device_token(), devices.new_device_token()
    check("토큰 두 개를 새로 만들면 서로 다름", t1 != t2)
    k1a = devices.key_for_token(t1)
    k1b = devices.key_for_token(t1)
    k2 = devices.key_for_token(t2)
    check("같은 토큰은 같은 키", k1a == k1b)
    check("다른 토큰은 다른 키", k1a != k2)
    check("★ 원본 토큰 값 자체는 키에 그대로 남지 않음(해시)", t1 not in k1a)
    check("SHA-256 전체(64자)를 그대로 쓴다(충돌 확률을 최대한 낮춤)", len(k1a) == 64)


def test_pending_and_approve() -> None:
    print("== 대기 -> 승인 ==")
    store = new_store()
    key = devices.key_for_token("1.2.3.4|Chrome")
    check("처음엔 신뢰 안 됨", not store.is_trusted(key))
    check("대기 중인 요청 없음", store.find_pending_for(key) is None)

    token = store.create_pending(key, "1.2.3.4", "Chrome")
    check("재요청하면 같은 토큰을 재사용(중복 방지)", store.find_pending_for(key) == token)
    rows = store.list_pending()
    check("관리자 목록에 1건", len(rows) == 1 and rows[0]["token"] == token and rows[0]["status"] == "pending")

    check("승인 처리됨", store.decide(token, approve=True))
    check("승인 뒤 신뢰됨", store.is_trusted(key))
    check("get_pending 은 approved 상태", store.get_pending(token)["status"] == "approved")
    store.pop(token)
    check("확인 후 대기 목록에서 지워짐", store.get_pending(token) is None)

    store.touch(key)
    trusted = store.list_trusted()
    check("신뢰 목록에 1건, 키 포함", len(trusted) == 1 and trusted[0]["key"] == key)


def test_deny_and_expire() -> None:
    print("== 거부 · 만료 ==")
    store = new_store()
    key = devices.key_for_token("5.6.7.8|Safari")
    token = store.create_pending(key, "5.6.7.8", "Safari")
    check("거부 처리됨", store.decide(token, approve=False))
    check("거부하면 신뢰 안 됨", not store.is_trusted(key))
    check("get_pending 은 denied 상태", store.get_pending(token)["status"] == "denied")

    token2 = store.create_pending("k2", "0.0.0.0", "X")
    store._data["pending"][token2]["expires_at"] = time.time() - 1
    store._save()
    check("기한 지나면 expired 로 보임", store.get_pending(token2)["status"] == "expired")
    check("만료된 건 승인할 수 없음", not store.decide(token2, approve=True))
    check("만료된 건 거부도 안 됨(이미 끝난 요청)", not store.decide(token2, approve=False))


def test_revoke_and_persist() -> None:
    print("== 해제 · 파일 영속성 ==")
    path = os.path.join(tempfile.mkdtemp(), "devices.json")
    store = devices.DeviceStore(path)
    key = devices.key_for_token("1.1.1.1|A")
    token = store.create_pending(key, "1.1.1.1", "A")
    store.decide(token, approve=True)
    check("해제 전엔 신뢰됨", store.is_trusted(key))
    check("해제 성공", store.revoke(key))
    check("해제 뒤 신뢰 안 됨", not store.is_trusted(key))
    check("이미 없는 걸 또 해제하면 False", not store.revoke(key))

    # 새 인스턴스로 다시 열어도 상태가 파일에 남아 있어야 한다.
    key2 = devices.key_for_token("2.2.2.2|B")
    token2 = store.create_pending(key2, "2.2.2.2", "B")
    store.decide(token2, approve=True)
    store2 = devices.DeviceStore(path)
    check("재시작해도 신뢰 목록이 파일에서 복원됨", store2.is_trusted(key2))
    check("깨진 파일도 예외 없이 빈 상태로 시작",
          devices.DeviceStore(os.path.join(tempfile.mkdtemp(), "missing.json")).list_trusted() == [])


def test_denied_cooldown() -> None:
    print("== 거부된 기기는 쉬는 시간 동안 재요청 차단 ==")
    store = new_store()
    key = devices.key_for_token("6.6.6.6|Persistent")
    check("거부 전엔 쉬는 시간 없음", store.denied_cooldown_remaining(key) == 0)
    token = store.create_pending(key, "6.6.6.6", "Persistent")
    store.decide(token, approve=False)
    remaining = store.denied_cooldown_remaining(key)
    check("거부 직후엔 쉬는 시간이 거의 그대로 남음", 0 < remaining <= devices.DENIED_COOLDOWN)

    # 나중에 마음이 바뀌어 승인하면 쉬는 시간이 풀린다.
    token2 = store.create_pending(key, "6.6.6.6", "Persistent")
    store.decide(token2, approve=True)
    check("승인하면 쉬는 시간 해제", store.denied_cooldown_remaining(key) == 0)
    check("승인됐으니 신뢰됨", store.is_trusted(key))

    # 기한이 지나면 다시 0.
    key2 = devices.key_for_token("7.7.7.7|Old")
    store._data["denied"][key2] = time.time() - devices.DENIED_COOLDOWN - 1
    store._save()
    check("쉬는 시간이 지나면 0으로 돌아옴", store.denied_cooldown_remaining(key2) == 0)


def test_tamper_protection() -> None:
    print("== ★ devices.json 을 프로그램 밖에서 직접 손대면 통째로 버려짐(무결성 서명) ==")
    path = os.path.join(tempfile.mkdtemp(), "devices.json")
    store = devices.DeviceStore(path)
    key = devices.key_for_token("1.1.1.1|Real")
    store.trust_directly(key, "1.1.1.1", "Real")
    check("정상 저장 후 다시 열어도 신뢰 유지(서명 정상)", devices.DeviceStore(path).is_trusted(key))

    # 서명을 다시 계산하지 않고 trusted 에 가짜 항목을 직접 끼워 넣는다(메모장으로 편집한 것과 같음).
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    fake_key = devices.key_for_token("6.6.6.6|Attacker")
    raw["trusted"][fake_key] = {"ip": "6.6.6.6", "ua": "Attacker", "approved_at": 0, "last_seen": 0}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f)  # _sig 는 그대로(안 맞음)

    reopened = devices.DeviceStore(path)
    check("서명이 안 맞으면 끼워 넣은 가짜 기기는 신뢰 안 됨", not reopened.is_trusted(fake_key))
    check("★ 원래 있던 정상 기기도 함께 사라짐(안전한 쪽으로 - 통째로 버림, 부분 신뢰 금지)",
          not reopened.is_trusted(key))
    check("조작된 원본 파일은 .tampered-* 로 남아 조사할 수 있음",
          any(fn.startswith(os.path.basename(path) + ".tampered-") for fn in os.listdir(os.path.dirname(path))))

    # 서명 필드 자체가 아예 없는 파일(예전 형식·수작업 생성)도 마찬가지로 버려진다.
    path2 = os.path.join(tempfile.mkdtemp(), "devices.json")
    with open(path2, "w", encoding="utf-8") as f:
        json.dump({"trusted": {fake_key: {"ip": "6.6.6.6", "ua": "x"}}, "pending": {}, "denied": {}}, f)
    check("서명 필드가 아예 없는 파일도 버려짐", not devices.DeviceStore(path2).is_trusted(fake_key))


def test_trust_directly() -> None:
    print("== 같은 PC 접속은 대기 없이 곧바로 신뢰 ==")
    store = new_store()
    key = devices.key_for_token("127.0.0.1|LocalBrowser")
    check("아직 신뢰 안 됨", not store.is_trusted(key))
    store.trust_directly(key, "127.0.0.1", "LocalBrowser")
    check("곧바로 신뢰됨(승인 대기 없음)", store.is_trusted(key))
    check("대기 목록에는 아무것도 안 남음", store.list_pending() == [])
    trusted = store.list_trusted()
    check("신뢰 목록에 등록됨", len(trusted) == 1 and trusted[0]["key"] == key)


def test_no_duplicate_spam() -> None:
    print("== 반복 로그인 시도해도 대기 목록이 늘어나지 않음 ==")
    store = new_store()
    key = devices.key_for_token("3.3.3.3|Retry")
    tokens = {store.find_pending_for(key) or store.create_pending(key, "3.3.3.3", "Retry") for _ in range(5)}
    check("5번 시도해도 토큰 1개", len(tokens) == 1)
    check("대기 목록도 1건", len(store.list_pending()) == 1)


def main() -> None:
    for t in (test_key, test_pending_and_approve, test_deny_and_expire, test_revoke_and_persist,
              test_tamper_protection, test_denied_cooldown, test_trust_directly, test_no_duplicate_spam):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
