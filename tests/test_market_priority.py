"""market.py 의 우선순위·폴백 로직 오프라인 테스트. `python tests/test_market_priority.py` 로 실행한다."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader import market  # noqa: E402
import daytrader.bithumb_api as bapi  # noqa: E402

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    mark = "OK  " if cond else "FAIL"
    line = f"[{mark}] {name}"
    if extra:
        line += f" - {extra}"
    print(line)
    if not cond:
        _failures.append(name)


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, upbit_payload=None):
        self.upbit_payload = upbit_payload if upbit_payload is not None else []

    def get(self, url, timeout=None, params=None):
        if "upbit" in url:
            return FakeResp(self.upbit_payload)
        return FakeResp([])


def test_coin_bithumb_first() -> None:
    print("\n== 코인 시세: 빗썸 1순위 ==")
    orig = bapi.BithumbClient

    class FakeBithumbOk:
        def __init__(self, *a, **kw):
            pass

        def ticker(self, markets):
            return [{"market": m, "trade_price": 100_000_000, "signed_change_price": 1000} for m in markets]

    bapi.BithumbClient = FakeBithumbOk
    out = market._fetch_coins(FakeSession(), [("BTC", "비트코인"), ("ETH", "이더리움")])
    check("빗썸이 정상 응답하면 그대로 씀", out["BTC"]["src"] == "빗썸" and out["ETH"]["src"] == "빗썸")
    bapi.BithumbClient = orig


def test_coin_fallback_to_upbit() -> None:
    print("\n== 코인 시세: 빗썸 실패 시 업비트 2순위 폴백 ==")
    orig = bapi.BithumbClient

    class FakeBithumbFail:
        def __init__(self, *a, **kw):
            pass

        def ticker(self, markets):
            raise RuntimeError("네트워크 오류")

    bapi.BithumbClient = FakeBithumbFail
    sess = FakeSession(upbit_payload=[{"market": "KRW-BTC", "trade_price": 99_000_000, "signed_change_price": 500}])
    out = market._fetch_coins(sess, [("BTC", "비트코인")])
    check("빗썸 실패 시 업비트로 폴백", out["BTC"]["src"] == "업비트")
    bapi.BithumbClient = orig


def test_toss_skipped_when_no_client() -> None:
    print("\n== 국내 종목: 토스 클라이언트가 없으면(None) 아예 시도 안 함 ==")

    payload = {"result": {"areas": [{"datas": [
        {"itemCode": "005930", "closePrice": "72000", "compareToPreviousClosePrice": "500",
         "compareToPreviousPrice": {"code": "2"}, "stockName": "삼성전자"},
    ]}]}}

    class FakeNaverSession:
        def get(self, url, timeout=None):
            return FakeResp(payload)

    out = market._domestic_stocks_batch(None, FakeNaverSession(), [("005930", "삼성전자")])
    check("toss_client=None 이면 네이버로 바로 감(예외 없이 정상 완료)", "error" not in out["005930"])


def test_toss_actually_not_called_via_server_route() -> None:
    print("\n== /api/market 라우트: 토스 키가 없으면 get_client() 자체를 호출하지 않음 ==")
    import tempfile
    import shutil
    import daytrader.server as server
    from fastapi.testclient import TestClient

    tmpdir = tempfile.mkdtemp()
    server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
    shutil.copy(os.path.join(ROOT, "config.yaml"), server.CONFIG_PATH)

    call_count = {"n": 0}

    def fake_get_client(force_new=False):
        call_count["n"] += 1
        raise RuntimeError("키가 없는데 토스 클라이언트를 만들려 함 - 호출되면 안 된다")

    # ★★ [5-2] 이 서버는 Host 검증(DNS 리바인딩 방어)과 로그인 벽을 둘 다 앞단에
    # 두고 있다. TestClient 기본값(Host: testserver, client IP: testclient)은 둘
    # 다 거절 대상이라 그대로 부르면 라우트에 닿기도 전에 400/401 이 난다 - 로컬
    # 제어 통로(DAYTRADER_LOCAL_TOKEN + X-Local-Control, _is_local_control 참고)를
    # loopback IP 로 흉내 내 실제 트레이가 서버를 부르는 경로와 같은 방식으로 통과한다.
    local_token = "test-local-control-token"
    orig_local_token = os.environ.get("DAYTRADER_LOCAL_TOKEN")
    os.environ["DAYTRADER_LOCAL_TOKEN"] = local_token
    orig_get_client = server.get_client
    server.get_client = fake_get_client
    try:
        client = TestClient(server.app, base_url="http://127.0.0.1", client=("127.0.0.1", 51000))
        r = client.get("/api/market?ttl=0", headers={"x-local-control": local_token})
        check("응답 정상(200)", r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
        check("★★★ 키 없으면 get_client() 호출 자체가 안 일어남", call_count["n"] == 0)
    finally:
        server.get_client = orig_get_client
        if orig_local_token is None:
            os.environ.pop("DAYTRADER_LOCAL_TOKEN", None)
        else:
            os.environ["DAYTRADER_LOCAL_TOKEN"] = orig_local_token


def main() -> None:
    tests = [
        test_coin_bithumb_first, test_coin_fallback_to_upbit,
        test_toss_skipped_when_no_client, test_toss_actually_not_called_via_server_route,
    ]
    for t in tests:
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
