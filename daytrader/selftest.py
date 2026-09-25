"""연계 테스트 - "연결이 된다"와 "내가 쓰려는 기능이 된다"는 다르다.
토큰은 받아지는데 계좌 조회 권한이 없을 수 있고, 시세는 되는데 주문 조회가
막혀 있을 수 있다. 실거래를 시작한 뒤에 알면 늦다.

★★ 읽기만 한다. 주문은 절대 내지 않는다. create_order·cancel_order·create_oco
는 이 모듈에서 부르지 않는다. "테스트 주문으로 1주 사보기" 기능은 만들지
않는다 - 그건 테스트가 아니라 실제 매매다.
"""

from __future__ import annotations

import time

HTTP_HINT = {
    400: "요청이 잘못되었습니다 - 파라미터를 확인하세요.",
    401: "자격증명이 틀렸거나 만료됐습니다 - 키를 다시 넣어보세요.",
    403: "이 계정에 권한이 없습니다 - 토스증권에 신청이 필요합니다.",
    404: "찾을 수 없습니다.",
    429: "호출 제한에 걸렸습니다 - 잠시 뒤 다시 시도하세요.",
    500: "토스증권 서버 오류입니다.",
    503: "점검 시간일 수 있습니다.",
}


def _hint(status, message: str = "") -> str:
    if status and status in HTTP_HINT:
        detail = f" ({message})" if message else ""
        return HTTP_HINT[status] + detail
    return message or "알 수 없는 오류입니다."


class Step:
    """★ why 는 "이걸 왜 확인하나" 다. 화면에서 행을 펼치면 이게 보인다."""

    def __init__(self, key: str, label: str, why: str, required: bool = True):
        self.key = key
        self.label = label
        self.why = why
        self.required = required
        self.ok = None  # True|False|None(건너뜀)
        self.detail = None
        self.error = None
        self.ms = None

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "why": self.why, "required": self.required,
            "ok": self.ok, "detail": self.detail, "error": self.error, "ms": self.ms,
        }


def toss_test(cfg, sample_symbol: str = "005930") -> dict:
    """검사 순서대로 실행한다. 앞의 필수 단계가 실패하면 뒤는 ok=None 으로 건너뛴다."""
    steps: list = []

    def run(key, label, why, required, fn):
        s = Step(key, label, why, required)
        steps.append(s)
        # ★ 이전에 실패한 '필수' 단계가 하나라도 있으면 이후는(선택이라도) 건너뛴다 -
        # 토큰이 없는데 계좌를 물어봐야 의미가 없다.
        if any(x.required and x.ok is False for x in steps[:-1]):
            return s
        t0 = time.monotonic()
        try:
            s.detail = fn()
            s.ok = True
        except Exception as exc:
            s.ok = False
            status = getattr(exc, "status", None)
            message = getattr(exc, "message", None) or str(exc)
            s.error = _hint(status, message)
        s.ms = round((time.monotonic() - t0) * 1000, 1)
        return s

    # 0. API 키 등록 - 필수. 없으면 즉시 반환한다.
    s0 = Step("keys", "API 키 등록", "토스증권 API 를 쓰려면 client_id/client_secret 이 있어야 합니다.", required=True)
    steps.append(s0)
    if not (cfg.client_id and cfg.client_secret):
        s0.ok = False
        s0.error = "API 키가 등록되어 있지 않습니다."
        s0.detail = "키 없이도 연습(web) 모드는 인터넷 공개 시세로 동작합니다."
        return _finish(steps, cfg, sample_symbol)
    s0.ok = True
    s0.detail = f"등록됨 (…{cfg.client_id[-4:]})"

    from daytrader.tossapi import TossClient
    client = TossClient(cfg.client_id, cfg.client_secret)

    run("token", "접근 토큰 발급", "이후 모든 조회가 이 토큰으로 인증됩니다.", True,
        lambda: _fetch_token(client))

    run("accounts", "계좌 목록", "실거래·모의매매가 어느 계좌로 나갈지 확인합니다.", True,
        lambda: f"{len(client.accounts())}개 계좌")

    def _account():
        seq = client.resolve_account(getattr(cfg, "account_seq", None))
        client.account_seq = seq
        return f"계좌 {seq} 번 사용"

    run("account", "사용할 계좌 확정", "여러 계좌가 있을 때 어느 계좌를 쓸지 정합니다.", True, _account)

    run("buying_power", "매수가능금액", "1회 투입금과 살 수 있는 수량이 여기서 나옵니다.", True,
        lambda: client.buying_power())

    run("holdings", "보유 종목", "계좌 대조(reconcile)가 이 조회에 의존합니다.", True,
        lambda: f"{len((client.holdings() or {}).get('items', []))}종목 보유")

    run("orders", "주문 내역", "미체결·최근 주문을 조회만 합니다 - 새 주문을 내지 않습니다.", True,
        lambda: f"{len(client.get_orders(status='OPEN'))}건 미체결")

    # ★★★ 실제로 겪은 버그 - 바로 아래(주문/현재가 등)처럼 실제 매매
    # 로직(screener.py, engine.py)에서는 이 두 API 를 부를 때 항상 필수
    # 파라미터를 명시적으로 넘기는데, 이 연계 테스트만 인자 없이 불러서
    # "실패"로 나오고 있었다 - 실제 서비스는 문제없이 됐을 것이다.
    run("conditional", "조건부 주문(OCO)",
        "서버에 손절·익절을 걸어둘 수 있는지 확인합니다. 권한이 없으면 프로그램이 직접 감시합니다.",
        False, lambda: f"{len(client.conditional_orders(status='OPEN'))}건")

    run("price", "현재가", "화면과 진입 판단이 이 값을 씁니다.", True,
        lambda: client.prices([sample_symbol]))

    run("candles", "분봉", "돌파·거래량 급증 판정이 분봉을 봅니다.", True,
        lambda: _check_candles(client, sample_symbol))

    run("orderbook", "호가", "체결 강도를 참고할 때 씁니다.", False,
        lambda: client.orderbook(sample_symbol))

    run("limits", "상·하한가", "상한가 근접 회피에 씁니다.", False,
        lambda: client.price_limits(sample_symbol))

    # ★★★ 실제로 겪은 버그 - 여기서만 존재하지 않는 랭킹 타입
    # 'TRADING_VALUE' 를 써서, 실제 매매는 멀쩡한데 연계 테스트의
    # '거래대금 랭킹'만 계속 실패했다. 실제 스크리너(screener.py)가 쓰는
    # 값은 'MARKET_TRADING_AMOUNT' 다 - 테스트가 실제 코드와 다른 값을
    # 쓰면 테스트의 의미 자체가 없다. marketCountry 도 필수라 함께 넘긴다.
    run("rankings", "거래대금 랭킹", "테마 스코어링의 재료입니다.", False,
        lambda: f"{len(client.rankings('MARKET_TRADING_AMOUNT', marketCountry='KR', duration='realtime', count=10))}건")

    run("warnings", "유의종목", "정리매매·투자경고 종목을 거르는 데 씁니다.", False,
        lambda: client.warnings(sample_symbol))

    run("calendar", "국내 영업일", "휴장일 판정이 이 조회에 의존합니다.", False,
        lambda: client.market_calendar_kr())

    return _finish(steps, cfg, sample_symbol)


def _fetch_token(client) -> str:
    client._fetch_token()
    return "발급됨"


def _check_candles(client, symbol: str) -> dict:
    rows = client.candles(symbol, "1m", 30)
    detail: dict = {"count": len(rows)}
    volumes = [r.get("volume") for r in rows if isinstance(r.get("volume"), (int, float))]
    # ★ 거래량이 단조 증가면 봉별 값이 아니라 누적값으로 보인다는 뜻이다.
    if len(volumes) >= 3 and all(volumes[i] <= volumes[i + 1] for i in range(len(volumes) - 1)):
        detail["warning"] = "거래량이 단조 증가로 보입니다 - 누적값일 수 있습니다."
    return detail


def _finish(steps: list, cfg, sample_symbol: str) -> dict:
    required_failed = [s.key for s in steps if s.required and s.ok is False]
    optional_failed = [s.key for s in steps if not s.required and s.ok is False]

    if required_failed:
        summary = (
            f"필수 항목 {len(required_failed)}개가 실패했습니다: {', '.join(required_failed)}. "
            "이 상태로 실거래를 시작하면 안 됩니다."
        )
    elif optional_failed:
        summary = (
            f"필수는 통과. 선택 {len(optional_failed)}개가 안 됩니다: {', '.join(optional_failed)}. "
            "그 기능만 빠진 채로 동작합니다."
        )
    else:
        summary = "모든 항목을 통과했습니다."

    return {
        "steps": [s.to_dict() for s in steps],
        "ok": len(required_failed) == 0,
        "required_failed": required_failed, "optional_failed": optional_failed,
        "summary": summary,
        "note": "조회만 하므로 계좌에 아무 변화도 없습니다.",
        "mode": cfg.mode, "symbol": sample_symbol,
    }


def bithumb_test(access_key: str, secret_key: str, market: str = "KRW-BTC") -> dict:
    """★★ 읽기만 한다 - 주문(place_order)은 이 함수에서 절대 부르지 않는다."""
    steps: list = []

    def run(key, label, why, required, fn):
        s = Step(key, label, why, required)
        steps.append(s)
        if any(x.required and x.ok is False for x in steps[:-1]):
            return s
        t0 = time.monotonic()
        try:
            s.detail = fn()
            s.ok = True
        except Exception as exc:
            s.ok = False
            status = getattr(exc, "status", None)
            message = getattr(exc, "message", None) or str(exc)
            s.error = _hint(status, message)
        s.ms = round((time.monotonic() - t0) * 1000, 1)
        return s

    s0 = Step("keys", "API 키 등록", "빗썸 Open API 를 쓰려면 Access Key/Secret Key 가 있어야 합니다.", required=True)
    steps.append(s0)
    if not (access_key and secret_key):
        s0.ok = False
        s0.error = "API 키가 등록되어 있지 않습니다."
        s0.detail = "키 없이도 시세 조회(공개 API)는 가능합니다 - 주문·잔고 조회만 막힙니다."
        return _bithumb_finish(steps, market)
    s0.ok = True
    s0.detail = f"등록됨 (…{access_key[-4:]})"

    from daytrader.bithumb_api import BithumbClient
    client = BithumbClient(access_key, secret_key)

    run("ticker", "현재가 조회", "화면과 매매 판단이 이 값을 씁니다. 인증 없이도 되는 공개 API입니다.", True,
        lambda: client.ticker([market]))

    def _accounts_detail():
        # ★★★ 실제로 겪은 문제 - accounts() 가 None 을 돌려주는 경우
        # (보유 자산이 하나도 없거나 204/빈 응답)에 len(None) 으로 죽어서
        # "보유 자산 조회 실패"로만 보였다. 인증은 멀쩡한데 실패로 뜨니
        # 원인을 찾을 수 없었다. 자산 0개는 실패가 아니라 정상 상태다.
        rows = client.accounts()
        if not rows:
            return "0개 자산 (조회 성공 - 보유 중인 자산이 없습니다)"
        krw = next((a for a in rows if a.get("currency") == "KRW"), None)
        coins = [a for a in rows if a.get("currency") != "KRW"]
        parts = [f"{len(rows)}개 자산"]
        if krw:
            parts.append(f"원화 {float(krw.get('balance') or 0):,.0f}원")
        if coins:
            parts.append(f"코인 {len(coins)}종")
        return " · ".join(parts)

    run("accounts", "보유 자산 조회", "잔고·평가금액 계산에 이 조회가 쓰입니다.", True,
        _accounts_detail)

    run("order_chance", "주문 가능 정보", "수수료·최소 주문 금액을 여기서 확인합니다.", True,
        lambda: client.order_chance(market))

    return _bithumb_finish(steps, market)


def _bithumb_finish(steps: list, market: str) -> dict:
    required_failed = [s.key for s in steps if s.required and s.ok is False]
    optional_failed = [s.key for s in steps if not s.required and s.ok is False]

    if required_failed:
        summary = (
            f"필수 항목 {len(required_failed)}개가 실패했습니다: {', '.join(required_failed)}. "
            "이 상태로 실거래를 시작하면 안 됩니다."
        )
    elif optional_failed:
        summary = f"필수는 통과. 선택 {len(optional_failed)}개가 안 됩니다: {', '.join(optional_failed)}."
    else:
        summary = "모든 항목을 통과했습니다."

    return {
        "steps": [s.to_dict() for s in steps],
        "ok": len(required_failed) == 0,
        "required_failed": required_failed, "optional_failed": optional_failed,
        "summary": summary,
        "note": "조회만 하므로 계좌에 아무 변화도 없습니다.",
        "market": market,
    }
