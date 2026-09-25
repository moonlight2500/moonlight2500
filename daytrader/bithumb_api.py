"""빗썸 Open API 2.0 클라이언트.

★ 이 모듈의 엔드포인트·요청 형식은 빗썸 공식 개발자 문서
(https://apidocs.bithumb.com) 를 직접 확인해 맞췄다:
  - 시세: GET /v1/ticker?markets=KRW-BTC,KRW-ETH (공개, 인증 불필요)
  - 잔고: GET /v1/accounts (JWT 인증)
  - 주문가능정보: GET /v1/orders/chance?market=... (JWT 인증)
  - 주문: POST /v2/orders (JWT 인증)
  - 주문취소: DELETE /v2/order?order_id=...|client_order_id=... (JWT 인증)

★ 시장 심볼은 "KRW-BTC" 형식이다 (업비트와 동일한 표기).

★★ POST 요청의 query_hash 계산 방식(본문을 쿼리스트링처럼 URL인코딩한 뒤
SHA512)은 공식 예제가 GET 요청 기준으로만 보여준 것을 업비트 계열 API의
통용 관례에 따라 그대로 적용한 것이다 - 실제 주문을 넣기 전에 반드시
연계 테스트(읽기 전용 항목들)로 인증 자체가 되는지 먼저 확인해야 한다.
"""

from __future__ import annotations

import time
import uuid

import jwt as pyjwt

from daytrader import netutil

BASE = "https://api.bithumb.com"


def _build_query_string(params: dict) -> str:
    """★★★ 빗썸 query_hash 계산용 쿼리 문자열을 공식 문서 규칙대로 만든다.
    두 가지가 핵심이고, 둘 다 실제로 틀려서 인증이 실패했던 부분이다:
      1. 정렬하지 않는다 - 딕셔너리에 넣은 순서를 그대로 유지한다.
         (문서의 모든 예제가 urlencode(param) 을 정렬 없이 쓴다.)
      2. 배열 값은 "key[]=v1&key[]=v2" 로 전개한다.
         (문서: "쉼표 구분(key=v1,v2)은 올바른 해시를 생성하지 않습니다.")
    ★ 서명에 쓰는 이 문자열은 실제 요청 URL 의 쿼리와 정확히 같아야 한다 -
    다르면 서버가 계산한 해시와 안 맞아 401 invalid_query_payload 가 난다.
    """
    parts = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for v in value:
                parts.append(f"{key}[]={v}")
        else:
            parts.append(f"{key}={value}")
    return "&".join(parts)


class BithumbApiError(RuntimeError):
    def __init__(self, status, code, message, data=None):
        super().__init__(f"[{status}] {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.data = data


class BithumbClient:
    """★ 실제 주문 메서드(place_order/cancel_order)는 이 클래스 밖에서
    호출되기 전까지는 어떤 자동 루프에도 연결되어 있지 않다. 읽기 전용
    메서드(ticker/accounts/order_chance)만으로 연계 테스트가 가능하다.
    """

    def __init__(self, access_key: str = "", secret_key: str = "", timeout: float = 10.0):
        # ★ Public API(시세)는 인증이 필요 없다 - 키가 없어도 만들 수 있게
        # 하고, 실제로 인증이 필요한 호출(_request(..., private=True))에서만
        # 키가 없다고 거부한다. ticker() 처럼 관찰만 하려는 화면이 키 없이도
        # 동작해야 한다.
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout
        self._session = netutil.make_session()

    # ━━ 인증 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _jwt(self, params: dict | None = None) -> str:
        """★ payload 구조는 공식 문서의 '인증 헤더 만들기' 예제 그대로다:
        access_key/nonce/timestamp, 파라미터가 있으면 query_hash(SHA512)와
        query_hash_alg 를 더한다.

        ★★★ 실제로 겪은 버그(빗썸 인증 실패) - query_hash 계산이 두 군데
        틀려 있었다. 공식 문서를 다시 대조해 고쳤다:
          1. 정렬하면 안 된다. 문서의 모든 예제가 urlencode(param) 을
             "딕셔너리에 넣은 순서 그대로" 쓰는데, 우리는 sorted() 로
             정렬해서 넘겼다 - 파라미터가 2개 이상이면 서버가 계산한
             해시와 달라져 401 invalid_query_payload 가 난다.
          2. 배열 파라미터는 "key[]=v1&key[]=v2" 형태여야 한다. 문서가
             명시적으로 "쉼표 구분(key=v1,v2)이나 key=v1&key=v2 는 올바른
             해시를 만들지 못한다"고 경고하는데, doseq=True 는 정확히
             그 잘못된 형태(key=v1&key=v2)를 만든다.
        """
        import hashlib

        payload = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
            "timestamp": round(time.time() * 1000),
        }
        if params:
            query = _build_query_string(params)
            payload["query_hash"] = hashlib.sha512(query.encode("utf-8")).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        return pyjwt.encode(payload, self.secret_key)

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None, private: bool = True):
        if private and not (self.access_key and self.secret_key):
            raise ValueError(
                "빗썸 API 키가 없습니다. 빗썸 로그인 → 마이페이지 → Open API 관리에서 "
                "Access Key/Secret Key 를 발급받은 뒤 넣어주세요."
            )
        url = BASE + path
        headers = {}
        request_params = params
        if private:
            sign_source = params if params else body
            headers["Authorization"] = "Bearer " + self._jwt(sign_source)
            # ★★★ 서명에 쓴 쿼리 문자열과 실제로 보내는 URL 의 쿼리가
            # 한 글자라도 다르면 401 invalid_query_payload 가 난다.
            # requests 에 params(dict) 를 넘기면 자체 규칙으로 인코딩해서
            # (특수문자·배열에서 특히) 서명 대상과 어긋날 수 있으므로,
            # GET 파라미터가 있으면 서명한 그 문자열을 URL 에 그대로 붙이고
            # params 는 넘기지 않는다 - 둘이 같은 문자열임을 보장한다.
            if params:
                url = url + "?" + _build_query_string(params)
                request_params = None
        try:
            resp = self._session.request(
                method, url, params=request_params, json=body, headers=headers, timeout=self.timeout,
            )
        except Exception as exc:
            raise BithumbApiError(None, "network_error", str(exc)) from exc

        if resp.status_code >= 400:
            try:
                data = resp.json()
                err = data.get("error", data)
                code = err.get("name") or err.get("code") or "error"
                message = err.get("message") or str(data)
            except Exception:
                code, message, data = "error", resp.text[:300], None
            # ★★★ 빗썸 공식 문서가 401 에러 코드별 원인을 명확히 정리해
            # 두었다 - 막연히 "이것저것 확인하세요"가 아니라, 서버가 준
            # 에러 코드에 맞는 정확한 원인과 해결법을 짚어 준다.
            _HINTS = {
                "invalid_query_payload": "요청 파라미터와 서명(query_hash)이 안 맞습니다. 프로그램 버그일 수 있으니 개발자에게 알려주세요.",
                "jwt_verification": "Secret Key 가 틀렸을 가능성이 큽니다 - 빗썸 마이페이지에서 발급한 값을 다시 복사해 넣어보세요.",
                "expired_jwt": "요청 시각이 만료됐습니다. PC 시계가 실제 시각과 많이 어긋나 있으면 발생합니다 - 시간 동기화를 확인하세요.",
                "NotAllowIP": "허용되지 않은 IP 에서 접근했습니다 - 빗썸 마이페이지 → Open API 관리에서 지금 이 PC 의 IP 를 등록하세요.",
                "out_of_scope": "API Key 에 이 기능 권한이 없습니다 - 빗썸 마이페이지 → Open API 관리에서 '자산조회' 등 필요한 항목을 켜세요.",
            }
            if private and resp.status_code in (401, 403):
                hint = _HINTS.get(code)
                if hint:
                    message += f" — {hint}"
                else:
                    message += (
                        " (흔한 원인: ①API 키 발급 시 '자산조회' 권한을 안 켰거나 ②등록한 IP가 아닌 곳에서 "
                        "요청했거나 ③Access/Secret Key 를 잘못 붙여넣은 경우입니다 - 빗썸 마이페이지 → "
                        "Open API 관리에서 확인하세요.)"
                    )
            raise BithumbApiError(resp.status_code, code, message, data)

        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ━━ 공개(시세) - 인증 불필요 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def ticker(self, markets: list) -> list:
        """★ 콤마로 여러 마켓을 한 번에 받는다."""
        return self._request("GET", "/v1/ticker", params={"markets": ",".join(markets)}, private=False)

    def markets(self) -> list:
        """거래 가능한 전체 마켓 목록(KRW-·BTC- 등이 섞여 있다)."""
        return self._request("GET", "/v1/market/all", params={"isDetails": "false"}, private=False) or []

    def day_candles(self, market: str, count: int = 2) -> list:
        """일봉(KST 자정 기준). 최신→과거 순 그대로 돌려준다. candle_acc_trade_price 가 그날의 거래대금이다."""
        return self._request("GET", "/v1/candles/days", params={"market": market, "count": count}, private=False) or []

    def candles(self, market: str, unit: int = 1, count: int = 200, to: str | None = None) -> list:
        """분봉 조회. unit: 1|3|5|10|15|30|60|240(분). count 최대 200.
        ★ 공식 응답은 최신→과거 순으로 온다 - 지표 계산은 과거→현재 순서가
        자연스러우므로 뒤집어서 돌려준다.
        """
        if unit not in (1, 3, 5, 10, 15, 30, 60, 240):
            raise ValueError(f"지원하지 않는 분봉 단위입니다: {unit}")
        params = {"market": market, "count": min(count, 200)}
        if to:
            params["to"] = to
        rows = self._request("GET", f"/v1/candles/minutes/{unit}", params=params, private=False)
        return list(reversed(rows or []))

    # ━━ 비공개(계좌·주문) - JWT 인증 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def accounts(self) -> list:
        """보유 중인 자산 정보를 조회한다."""
        return self._request("GET", "/v1/accounts")

    def order_chance(self, market: str) -> dict:
        """거래 대상 페어의 수수료·최소 주문 금액 등 주문 가능 정보를 조회한다."""
        return self._request("GET", "/v1/orders/chance", params={"market": market})

    def place_order(self, market: str, side: str, order_type: str, *,
                     price: str | None = None, volume: str | None = None,
                     client_order_id: str | None = None, time_in_force: str | None = None) -> dict:
        """★★ 실제 주문을 낸다. side: "bid"(매수)|"ask"(매도).
        order_type: "limit"|"price"(시장가 매수)|"market"(시장가 매도)|"best".
        지정가는 price·volume 모두, 시장가 매수는 price(총액), 시장가 매도는
        volume 이 필요하다.
        """
        if side not in ("bid", "ask"):
            raise ValueError(f"side 는 bid 또는 ask 여야 합니다: {side}")
        if order_type not in ("limit", "price", "market", "best"):
            raise ValueError(f"알 수 없는 order_type 입니다: {order_type}")
        body = {"market": market, "side": side, "order_type": order_type}
        if price is not None:
            body["price"] = str(price)
        if volume is not None:
            body["volume"] = str(volume)
        if client_order_id:
            body["client_order_id"] = client_order_id
        if time_in_force:
            body["time_in_force"] = time_in_force
        return self._request("POST", "/v2/orders", body=body)

    def cancel_order(self, order_id: str | None = None, client_order_id: str | None = None) -> dict:
        """★ order_id 또는 client_order_id 중 하나 이상 필요. 둘 다 주면 order_id 우선."""
        if not order_id and not client_order_id:
            raise ValueError("order_id 또는 client_order_id 중 하나는 있어야 합니다.")
        params = {}
        if order_id:
            params["order_id"] = order_id
        if client_order_id:
            params["client_order_id"] = client_order_id
        return self._request("DELETE", "/v2/order", params=params)

    def get_order(self, order_id: str | None = None, client_order_id: str | None = None) -> dict | None:
        """★★ 개별 주문 조회 - 타임아웃 뒤 재전송 대신 접수 여부를 확인하는
        용도다(place_order 가 network_error 로 실패했을 때). order_id 또는
        client_order_id 중 하나 이상 필요(cancel_order 와 같은 규칙).
        ★★★ place_order/cancel_order 와 달리 이 엔드포인트는 아직 실제 응답으로
        연계 테스트를 해보지 못했다 - 실전 투입 전 반드시 먼저 확인해야 한다.
        조회 자체가 실패해도 예외를 던지지 않고 None 을 돌려준다 - 호출부(브로커)가
        "확인 못 함"으로 보고 안전한 쪽(매매 중단)으로 넘어갈 수 있어야 한다.
        """
        if not order_id and not client_order_id:
            raise ValueError("order_id 또는 client_order_id 중 하나는 있어야 합니다.")
        params = {}
        if order_id:
            params["uuid"] = order_id
        if client_order_id:
            params["client_order_id"] = client_order_id
        try:
            return self._request("GET", "/v1/order", params=params)
        except BithumbApiError:
            return None
