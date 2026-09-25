from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time as _time
from dataclasses import dataclass

from daytrader import netutil

log = logging.getLogger(__name__)

BASE = "https://openapi.tossinvest.com"


class TossApiError(RuntimeError):
    def __init__(self, status, code, message, data=None):
        super().__init__(f"[{status}] {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.data = data


class RateLimiter:
    """그룹별 토큰 버킷. 문서 한도에서 안전 마진을 둔 값이다."""

    LIMITS = {
        "AUTH": 3, "ACCOUNT": 1, "ASSET": 3, "STOCK": 3, "STOCK_ALL": 1,
        "MARKET_INFO": 2, "MARKET_DATA": 10, "MARKET_DATA_CHART": 12,
        "RANKING": 3, "ORDER": 5, "ORDER_HISTORY": 3, "ORDER_INFO": 3,
        "CONDITIONAL_ORDER": 3, "CONDITIONAL_ORDER_HISTORY": 5,
    }

    def __init__(self):
        self._lock = threading.Lock()
        self._calls = {g: collections.deque() for g in self.LIMITS}

    def acquire(self, group: str) -> None:
        limit = self.LIMITS.get(group)
        if not limit:
            return
        while True:
            with self._lock:
                q = self._calls[group]
                now = _time.monotonic()
                while q and now - q[0] >= 1.0:
                    q.popleft()
                if len(q) < limit:
                    q.append(now)
                    return
                wait = 1.0 - (now - q[0])
            _time.sleep(max(wait, 0.01))


@dataclass
class Token:
    access_token: str
    expires_at: float  # time.time() 기준 초

    @property
    def valid(self) -> bool:
        # 만료 60초 전을 기준으로 미리 갱신한다 - 요청 도중 만료되는 것을 막기 위해서다.
        return _time.time() < self.expires_at - 60


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class TossClient:
    def __init__(self, client_id, client_secret, account_seq=None, timeout=10.0, token_cache=None):
        if not client_id or not client_secret:
            raise ValueError(
                "토스 API 키가 없습니다. https://openapi.tossinvest.com 에서 앱을 등록해 "
                "client_id/client_secret 을 발급받은 뒤 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET "
                "환경변수로 넣어주세요."
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_seq = account_seq
        self.timeout = timeout
        self.token_cache = token_cache
        # ★ 사내망 대응 - 프록시/인증서 문제를 자동으로 우회하는 세션을 쓴다.
        netutil.force_ipv4()  # 허용 IP 등록은 IPv4 기준이다 - 접속 경로도 IPv4 로 고정한다.
        self._session = netutil.make_session()
        self._limiter = RateLimiter()
        self._token: Token | None = None
        self._token_lock = threading.Lock()

        if token_cache and os.path.exists(token_cache):
            try:
                with open(token_cache, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self._token = Token(d["access_token"], d["expires_at"])
            except Exception:
                self._token = None

    # ━━ 인증 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _save_token_cache(self) -> None:
        if not self.token_cache or not self._token:
            return
        with open(self.token_cache, "w", encoding="utf-8") as f:
            json.dump({"access_token": self._token.access_token, "expires_at": self._token.expires_at}, f)
        os.chmod(self.token_cache, 0o600)  # 토큰 파일은 소유자만 읽게 한다.

    def _fetch_token(self) -> None:
        self._limiter.acquire("AUTH")
        resp = self._session.post(
            f"{BASE}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout,
        )
        try:
            payload = resp.json()
        except Exception:
            payload = {}

        if resp.status_code >= 400 or "error" in payload:
            err = payload.get("error", {})
            raise TossApiError(
                resp.status_code, err.get("code", "auth-failed"),
                err.get("message", "인증에 실패했습니다."), err.get("data"),
            )

        result = payload.get("result", payload)
        access_token = result.get("access_token")
        expires_in = result.get("expires_in", 3600)
        self._token = Token(access_token, _time.time() + expires_in)
        self._save_token_cache()

    def _ensure_token(self) -> str:
        with self._token_lock:
            if self._token is None or not self._token.valid:
                self._fetch_token()
        return self._token.access_token

    def _discard_token(self) -> None:
        """★★★ 메모리뿐 아니라 디스크 캐시도 지운다 - 파일에 죽은 토큰이
        남아 있으면 프로그램을 다시 켤 때 그걸 또 읽어서 같은 실패가
        반복된다(token-revoked 가 계속 나오던 이유 중 하나).
        """
        self._token = None
        if self.token_cache and os.path.exists(self.token_cache):
            try:
                os.remove(self.token_cache)
            except OSError:
                pass  # ★ 캐시를 못 지워도 메모리 토큰은 버렸으니 재발급은 된다.

    # ━━ 공통 요청 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _request(self, method, path, *, group, params=None, body=None, account=False, retries=3, idempotent=True):
        url = f"{BASE}{path}"
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        attempt = 0
        while True:
            self._limiter.acquire(group)
            token = self._ensure_token()
            headers = {"Authorization": f"Bearer {token}"}
            if account:
                if self.account_seq is None:
                    raise TossApiError(0, "no-account", "계좌가 지정되지 않았습니다. resolve_account() 를 먼저 호출하세요.")
                headers["X-Tossinvest-Account"] = str(self.account_seq)

            try:
                resp = self._session.request(
                    method, url, params=params, json=body, headers=headers, timeout=self.timeout,
                )
            except Exception as exc:
                # ★★ idempotent=False 면 네트워크 예외는 즉시 던진다.
                # POST /orders 를 재시도하면 이중 주문이 난다. 읽기는 재시도해도
                # 안전하지만 쓰기는 그렇지 않다.
                if not idempotent:
                    raise TossApiError(0, "network-uncertain", f"요청 결과를 알 수 없습니다: {exc}") from exc
                attempt += 1
                if attempt > retries:
                    raise TossApiError(0, "network-error", str(exc)) from exc
                _time.sleep(min(8, 2 ** attempt))
                continue

            if resp.status_code == 200:
                payload = resp.json() if resp.content else {}
                # ★★★ 실제로 겪은 버그 - 응답이 리스트로 바로 오는 API 가
                # 있는데 무조건 .get("result") 를 불러 AttributeError 로
                # 죽었다("'list' object has no attribute 'get'").
                # 딕셔너리일 때만 result 를 벗긴다.
                if isinstance(payload, dict):
                    return payload.get("result", payload)
                return payload

            try:
                payload = resp.json()
            except Exception:
                payload = {}
            err = payload.get("error", {})
            code = err.get("code", "")
            message = err.get("message") or resp.text[:200]
            data = err.get("data")

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else min(8, 2 ** attempt)
                attempt += 1
                if attempt > retries:
                    raise TossApiError(429, code or "rate-limited", message, data)
                _time.sleep(wait)
                continue

            if resp.status_code == 401:
                # ★★★ 실제로 겪은 버그 - 재발급 대상에 expired-token/
                # invalid-token 만 있고 token-revoked 가 빠져 있었다.
                # 토스는 새 토큰이 발급되면 이전 토큰을 무효화하는데
                # ("새로 발급된 토큰으로 대체되어 더 이상 유효하지 않은
                # 토큰입니다"), 그 코드를 재발급 대상으로 안 봐서 캐시된
                # 죽은 토큰을 계속 쓰며 실패했다. 프로그램을 두 번 켜거나
                # 다른 곳에서 같은 키를 쓰면 바로 이 상태가 된다.
                # ★ 401 은 "이 토큰으로는 안 된다"는 뜻이므로, 코드가
                #   무엇이든 일단 버리고 다시 받아 보는 게 맞다. 자격
                #   증명 자체가 틀렸다면 재발급에서 막히니 안전하다.
                self._discard_token()
                attempt += 1
                if attempt > retries:
                    raise TossApiError(401, code, message, data)
                continue

            if resp.status_code >= 500:
                # ★★ idempotent=False 면 5xx 도 즉시 던진다. 같은 이유(이중 주문 방지)다.
                if not idempotent:
                    raise TossApiError(resp.status_code, code or "server-error", message, data)
                attempt += 1
                if attempt > retries:
                    raise TossApiError(resp.status_code, code or "server-error", message, data)
                _time.sleep(min(8, 2 ** attempt))
                continue

            raise TossApiError(resp.status_code, code, message, data)

    # ━━ 계좌·자산 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def accounts(self):
        return self._request("GET", "/api/v1/accounts", group="ACCOUNT")

    def resolve_account(self, prefer=None):
        """지정한 계좌 → BROKERAGE(위탁) 첫 계좌 → 첫 계좌 순으로 고른다."""
        accs = self.accounts()
        if prefer is not None:
            for a in accs:
                if a.get("accountSeq") == prefer or a.get("accountNumber") == prefer:
                    self.account_seq = a.get("accountSeq")
                    return self.account_seq
        for a in accs:
            if a.get("accountType") == "BROKERAGE":
                self.account_seq = a.get("accountSeq")
                return self.account_seq
        if accs:
            self.account_seq = accs[0].get("accountSeq")
            return self.account_seq
        raise TossApiError(0, "no-account", "사용 가능한 계좌가 없습니다.")

    def holdings(self):
        return self._request("GET", "/api/v1/holdings", group="ASSET", account=True)

    def buying_power(self, currency: str = "KRW"):
        """★ currency 는 필수 파라미터다(공식 SDK/CLI 예시 확인: getBuyingPower({account, currency})).
        이게 빠지면 "요청 필드가 올바르지 않습니다" 오류가 난다 - 실제로 겪은 문제.
        """
        return self._request(
            "GET", "/api/v1/buying-power", group="ORDER_INFO", account=True,
            params={"currency": currency},
        )

    def sellable_quantity(self, symbol):
        return self._request(
            "GET", "/api/v1/sellable-quantity", group="ORDER_INFO", account=True,
            params={"symbol": symbol},
        )

    # ━━ 시세·종목 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def rankings(self, type, marketCountry="KR", duration=None, count=100, excludeInvestmentCaution=None):
        params = {
            "type": type, "marketCountry": marketCountry, "duration": duration,
            "count": count, "excludeInvestmentCaution": excludeInvestmentCaution,
        }
        r = self._request("GET", "/api/v1/rankings", group="RANKING", params=params)
        # ★★★ 실제로 겪은 버그의 최종 원인 - 응답이 리스트로 올 때도 있고
        # {"rankings": [...]} 같은 딕셔너리로 올 때도 있다. 예전엔 받은
        # 그대로 돌려줘서, 딕셔너리로 오면 스크리너가 키 문자열을 순회하며
        # 종목을 하나도 못 찾았다("시세를 하나도 받아오지 못했다"의 정체).
        # 진단 화면에서는 딕셔너리에 [:5] 를 써서
        # "KeyError: slice(None, 5, None)" 로 드러났다.
        # ★ prices() 는 이미 같은 처리를 하고 있었다 - rankings 만 빠져 있었다.
        if isinstance(r, list):
            return r
        if isinstance(r, dict):
            for key in ("rankings", "items", "list", "data", "result", "content"):
                v = r.get(key)
                if isinstance(v, list):
                    return v
            # ★ 키 이름을 모르면, 값 중 리스트를 하나 골라 쓴다 - 필드명이
            #   바뀌어도 동작하도록(모르면 빈 목록이 낫다).
            for v in r.values():
                if isinstance(v, list):
                    return v
        # ★★★ 실제로 겪은 문제 - 여기까지 왔다는 건 HTTP 200 인데 리스트를
        # 어디서도 못 찾았다는 뜻이다. _request() 는 상태코드로만 오류를
        # 구분하므로, 요청 한도 초과·권한 문제 등이 200 OK 에 담겨 오면
        # (예: {"result": {"code": "..."}}) 지금까지 조용히 빈 목록으로
        # 둔갑했다 - 화면에는 "오늘 시세가 없다"로만 보이고 로그에도 진짜
        # 원인이 전혀 안 남아서, 스크리너가 왜 후보를 못 찾는지 끝까지
        # 알 수 없었다("연계 테스트는 통과하는데 종목선정만 시세를 못
        # 받는다"는 혼란의 또 다른 경로). 반환값 계약(빈 리스트)은 그대로
        # 지키되, 로그에는 실제 받은 응답을 남겨 다음엔 바로 원인을 알 수
        # 있게 한다.
        log.warning(
            "rankings(type=%s, duration=%s) 응답에서 목록을 찾지 못해 빈 결과로 처리합니다 - "
            "요청 한도 초과·권한 문제일 수 있습니다. 받은 응답: %s",
            type, duration, str(r)[:300],
        )
        return []

    def prices(self, symbols):
        """200개씩 청크로 나눠 조회한다."""
        out = []
        for chunk in _chunks(list(symbols), 200):
            r = self._request("GET", "/api/v1/prices", group="MARKET_DATA", params={"symbols": ",".join(chunk)})
            out.extend(r if isinstance(r, list) else r.get("prices", []))
        return out

    def candles(self, symbol, interval, count, before=None):
        """★ 파라미터명이 timeframe 이 아니라 interval 이다(공식 CLI 예시 확인:
        `candles --symbol 005930 --interval 1d --count 30`). adjusted 필드는
        검증 안 된 추측이라 뺐다 - 없는 필드를 보내면 "요청 필드가 올바르지
        않습니다" 오류가 날 수 있다.
        """
        params = {"symbol": symbol, "interval": interval, "count": count}
        if before:
            params["before"] = before
        r = self._request("GET", "/api/v1/candles", group="MARKET_DATA_CHART", params=params)
        rows = r if isinstance(r, list) else r.get("candles", [])
        # ★ 최신→과거 순으로 올 수 있으니 항상 timestamp 오름차순으로 정규화한다.
        return sorted(rows, key=lambda c: c.get("timestamp", ""))

    def orderbook(self, symbol):
        return self._request("GET", "/api/v1/orderbook", group="MARKET_DATA", params={"symbol": symbol})

    def price_limits(self, symbol):
        return self._request("GET", "/api/v1/price-limits", group="MARKET_DATA", params={"symbol": symbol})

    def stocks(self, symbols):
        """100개씩 청크로 나눠 조회한다."""
        out = []
        for chunk in _chunks(list(symbols), 100):
            r = self._request("GET", "/api/v1/stocks", group="STOCK", params={"symbols": ",".join(chunk)})
            out.extend(r if isinstance(r, list) else r.get("stocks", []))
        return out

    def warnings(self, symbol):
        return self._request("GET", f"/api/v1/stocks/{symbol}/warnings", group="STOCK")

    def market_calendar_kr(self):
        return self._request("GET", "/api/v1/market-calendar/KR", group="MARKET_INFO")

    # ━━ 주문 (전부 idempotent=False - 재시도하면 이중 주문이 난다) ━━━━━━━━

    def create_order(self, symbol, side, orderType, quantity, price=None, timeInForce="DAY", clientOrderId=None):
        body = {
            "symbol": symbol, "side": side, "orderType": orderType,
            "quantity": quantity, "timeInForce": timeInForce,
        }
        if orderType == "LIMIT" and price is not None:
            body["price"] = int(price)
        if clientOrderId:
            body["clientOrderId"] = clientOrderId[:36]
        return self._request("POST", "/api/v1/orders", group="ORDER", account=True, body=body, idempotent=False)

    def cancel_order(self, order_id):
        return self._request(
            "POST", f"/api/v1/orders/{order_id}/cancel", group="ORDER", account=True, idempotent=False,
        )

    def modify_order(self, order_id, **changes):
        return self._request(
            "POST", f"/api/v1/orders/{order_id}/modify", group="ORDER", account=True,
            body=changes, idempotent=False,
        )

    def get_order(self, order_id):
        return self._request("GET", f"/api/v1/orders/{order_id}", group="ORDER_HISTORY", account=True)

    def get_orders(self, **params):
        return self._request("GET", "/api/v1/orders", group="ORDER_HISTORY", account=True, params=params)

    def create_oco(self, symbol, quantity, expire_date, *,
                    take_profit_trigger, take_profit_price,
                    stop_loss_trigger, stop_loss_price):
        """익절/손절을 한 번에 거는 조건부(OCO) 주문.
        인자 이름은 daytrader.playbook.oco_levels() 가 돌려주는 dict 키와 그대로 맞췄다.

        ★★ 공식 문서(modifyConditionalOrder 설명)에서 "수량(quantity)은 각
        감시 조건(first/second) 안에 입력합니다"라고 명시한다 - 이걸 몰라서
        quantity 를 최상위(body 레벨)에 뒀던 게 "요청 필드가 올바르지
        않습니다" 오류의 원인이었을 가능성이 크다. first/second 는 둘 다
        매도(SELL)이며 first 감시가 > 현재가 > second 감시가여야 한다(공식
        문서 확인) - 익절(더 높은 가격)을 first, 손절(더 낮은 가격)을
        second 로 넣는 이유다.
        """
        body = {
            "symbol": symbol,
            "type": "OCO",
            "orderType": "LIMIT",
            "expireDate": expire_date,
            "first": {"orderSide": "SELL", "quantity": quantity,
                       "triggerPrice": take_profit_trigger, "orderPrice": take_profit_price},
            "second": {"orderSide": "SELL", "quantity": quantity,
                        "triggerPrice": stop_loss_trigger, "orderPrice": stop_loss_price},
        }
        return self._request(
            "POST", "/api/v1/conditional-orders", group="CONDITIONAL_ORDER", account=True,
            body=body, idempotent=False,
        )

    def cancel_conditional_order(self, conditional_order_id):
        return self._request(
            "DELETE", f"/api/v1/conditional-orders/{conditional_order_id}",
            group="CONDITIONAL_ORDER", account=True, idempotent=False,
        )

    def conditional_orders(self, **params):
        return self._request(
            "GET", "/api/v1/conditional-orders", group="CONDITIONAL_ORDER_HISTORY",
            account=True, params=params,
        )
