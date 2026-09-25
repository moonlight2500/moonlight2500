"""시세 라우터: 토스증권을 먼저 쓰고, 토스가 주지 못하는 것만 밖에서 가져온다.
같은 정보를 두 곳에서 받을 수 있으면 공식 API 가 낫고, 토스만 줄 수 있는 것이 있다.

  기능              토스   인터넷(네이버)
  현재가             O      O
  분봉 OHLCV         O      완전 △ 시가·고가·저가 없음, 거래량 누적
  시장 전체 랭킹      O      X  themes.yaml 종목만
  매수 유의사항       O      X  투자경고·단기과열을 알 수 없다
  상한가·하한가       O      △ 전일 종가 ±30% 추정
  호가 잔량           O      X
  거래일 캘린더       O      X  주말만 판단

분봉에 고가가 없으면 '최근 N봉 고가 돌파'가 종가 기준으로 무너지고,
유의사항을 못 받으면 정리매매·투자경고 종목을 걸러낼 수 없고,
시장 전체 랭킹이 없으면 '오늘 가장 강한 테마'가 아니라
'내가 적어둔 종목 중 가장 강한 테마'가 된다.
"""

from __future__ import annotations

import time

TOSS_ONLY = ("rankings", "warnings", "orderbook", "market_calendar_kr")
SOURCE_LABELS = {"toss": "토스증권 Open API", "web": "인터넷 공개 시세", "none": "받지 못함"}

_SKIP_SECONDS = 30.0


class QuoteRouter:
    def __init__(self, primary=None, backup=None, *, cfg=None):
        self.primary = primary
        self.backup = backup
        self.cfg = cfg
        self._skip_until: dict = {}
        self.source_of: dict = {}
        self.failures: dict = {}

    @property
    def account_seq(self):
        if self.primary is not None:
            return self.primary.account_seq
        if self.backup is not None:
            return getattr(self.backup, "account_seq", None)
        return None

    @property
    def clock(self):
        if self.primary is not None and hasattr(self.primary, "clock"):
            return self.primary.clock
        if self.backup is not None and hasattr(self.backup, "clock"):
            return self.backup.clock
        return None

    def _skip_primary(self, name: str) -> bool:
        """★ 방금 실패한 기능은 30초 동안 토스를 건너뛴다.
        매 호출마다 죽은 API 를 다시 때리면 느려지고 한도만 태운다.
        """
        until = self._skip_until.get(name)
        return until is not None and time.monotonic() < until

    def _mark_failed(self, name: str, exc) -> None:
        """★★★ 실제로 겪은 문제 - 스킵은 '폴백이 있을 때' 의미가 있다.
        인터넷으로 대체 가능한 기능(prices 등)은 토스가 아플 때 건너뛰고
        웹 시세로 넘어가면 되지만, TOSS_ONLY(rankings 등)는 대체가
        없어서 스킵하는 순간 30초 동안 무조건 실패한다. 일시적인 한도
        초과 한 번 때문에 종목 선정이 30초씩 통째로 멈췄고, 화면에는
        "시세를 하나도 받아오지 못했다"만 떴다.
        ★ TOSS_ONLY 는 스킵하지 않는다 - 어차피 대안이 없으니 매번
        다시 시도하는 편이 낫다(실패하면 그 오류가 그대로 보인다).
        """
        if name not in TOSS_ONLY:
            self._skip_until[name] = time.monotonic() + _SKIP_SECONDS
        self.failures[name] = str(exc)

    @staticmethod
    def _supported(fn, optional: dict) -> dict:
        """★★★ 클라이언트마다 메서드 시그니처가 조금씩 다르다(토스는
        candles 에 adjusted 가 없고, 웹 시세에는 있다). 받는 쪽이 실제로
        지원하는 인자만 골라 넘겨서 TypeError 를 원천 차단한다.
        ★ 시그니처를 못 읽으면(내장 함수·목업 등) 안전하게 아무것도 안 넘긴다.
        """
        if not optional:
            return {}
        try:
            import inspect
            params = inspect.signature(fn).parameters
            if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                return dict(optional)  # ★ **kwargs 를 받으면 다 넘겨도 된다.
            return {k: v for k, v in optional.items() if k in params}
        except (TypeError, ValueError):
            return {}

    def _call(self, name: str, *args, _optional: dict | None = None, **kwargs):
        """토스 → 인터넷 순서로 시도한다. 실패하면 _skip_until 에 기록한다.
        TOSS_ONLY 는 primary 가 있으면 폴백하지 않는다 - 인터넷에는 그 기능
        자체가 없기 때문이다.

        ★ _optional 은 "받는 쪽이 지원하면 넘기고, 아니면 생략할" 인자다.
        """
        if self.primary is not None and not self._skip_primary(name):
            try:
                fn = getattr(self.primary, name)
                result = fn(*args, **kwargs, **self._supported(fn, _optional))
                self.source_of[name] = "toss"
                return result
            except Exception as exc:
                self._mark_failed(name, exc)

        if name in TOSS_ONLY and self.primary is not None:
            # ★★★ 실제로 겪은 문제 - 토스가 준 진짜 원인(400 파라미터 오류,
            # 403 권한 없음 등)이 여기서 통째로 덮여 사라지고, "인터넷으로
            # 대체할 수 없습니다"라는 무의미한 메시지만 화면에 올라갔다.
            # 게다가 30초 스킵(_skip_primary) 중이면 호출조차 안 하고 바로
            # 이 줄로 오기 때문에, 그 사이 화면에는 원인이 전혀 안 보인다.
            # self.failures 에 남겨 둔 마지막 실제 오류를 반드시 함께 알린다.
            last = self.failures.get(name)
            detail = f" 마지막 오류: {last}" if last else ""
            raise RuntimeError(
                f"{name}: 토스 API 호출에 실패했고, 이 기능은 인터넷으로 대체할 수 없습니다.{detail}"
            )

        if self.backup is not None:
            try:
                fn = getattr(self.backup, name)
                result = fn(*args, **kwargs, **self._supported(fn, _optional))
                self.source_of[name] = "web"
                return result
            except Exception as exc:
                self._mark_failed(name, exc)

        # ★ 여기까지 왔다는 건 토스도 인터넷도 실패했다는 뜻이다 - 마지막
        # 실제 오류를 함께 알려야 원인을 찾을 수 있다(위와 같은 이유).
        last = self.failures.get(name)
        detail = f" 마지막 오류: {last}" if last else ""
        raise RuntimeError(f"{name}: 시세를 받아올 곳이 없습니다.{detail}")

    def prices(self, symbols):
        """★ 토스가 일부만 줬으면 나머지 심볼만 인터넷으로 메운다."""
        symbols = list(symbols)

        if self.primary is not None and not self._skip_primary("prices"):
            try:
                rows = self.primary.prices(symbols)
                got = {str(r.get("symbol", "")).zfill(6) for r in rows}
                missing = [s for s in symbols if s not in got]
                if not missing:
                    self.source_of["prices"] = "toss"
                    return rows
                if self.backup is not None:
                    try:
                        extra = self.backup.prices(missing)
                        self.source_of["prices"] = "toss+web"
                        return rows + extra
                    except Exception as exc:
                        self._mark_failed("prices", exc)
                self.source_of["prices"] = "toss"
                return rows
            except Exception as exc:
                self._mark_failed("prices", exc)

        if self.backup is not None:
            rows = self.backup.prices(symbols)
            self.source_of["prices"] = "web"
            return rows

        raise RuntimeError("prices: 시세를 받아올 곳이 없습니다.")

    def candles(self, symbol, timeframe, count, adjusted=True):
        """★★★ 실제로 겪은 버그("candles: TossClient.candles() got an
        unexpected keyword argument 'adjusted'") - 여기서 adjusted 를 그대로
        넘겼는데, TossClient.candles() 는 이 인자를 받지 않는다(tossapi.py
        주석대로 "검증 안 된 추측이라 뺐다"). 그래서 토스로 캔들을 받을
        때마다 TypeError 가 나고 종목선정이 통째로 실패했다.
        ★ 폴백(웹 시세)은 adjusted 를 받으므로, 받는 쪽이 지원할 때만
        넘기도록 시그니처를 직접 확인해서 전달한다.
        """
        return self._call("candles", symbol, timeframe, count, _optional={"adjusted": adjusted})

    def rankings(self, type, marketCountry="KR", duration=None, count=100, excludeInvestmentCaution=None):
        return self._call(
            "rankings", type, marketCountry=marketCountry, duration=duration,
            count=count, excludeInvestmentCaution=excludeInvestmentCaution,
        )

    def price_limits(self, symbol):
        return self._call("price_limits", symbol)

    def orderbook(self, symbol):
        return self._call("orderbook", symbol)

    def stocks(self, symbols):
        return self._call("stocks", symbols)

    def warnings(self, symbol):
        """인터넷 소스는 유의사항을 알 수 없다. 실패하면 안전하게 빈 배열로 감싼다."""
        try:
            return self._call("warnings", symbol)
        except Exception:
            return []

    def market_calendar_kr(self):
        return self._call("market_calendar_kr")

    def accounts(self):
        return self._call("accounts")

    def resolve_account(self, prefer=None):
        return self._call("resolve_account", prefer)

    def holdings(self):
        return self._call("holdings")

    def buying_power(self, currency: str = "KRW"):
        # ★★★ 실제로 겪을 뻔한 버그 - TossClient.buying_power() 는 currency
        # 가 필수 파라미터다(빠지면 "요청 필드가 올바르지 않습니다" 에러).
        # 이 라우터가 인자를 안 받고 그냥 _call("buying_power") 만 하면,
        # 결국 토스 클라이언트를 인자 없이 부르게 돼서 그대로 실패한다.
        return self._call("buying_power", currency)

    def sellable_quantity(self, symbol):
        return self._call("sellable_quantity", symbol)

    # ━━ 주문은 라우팅하지 않는다 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 시세를 대체하는 것과 주문을 대체하는 것은 전혀 다른 이야기다.

    def _order(self, name: str, *args, **kwargs):
        if self.primary is None:
            raise RuntimeError("주문은 토스증권 API 로만 나갑니다. 준비 탭에서 API 키를 등록하세요.")
        return getattr(self.primary, name)(*args, **kwargs)

    def create_order(self, *args, **kwargs):
        return self._order("create_order", *args, **kwargs)

    def cancel_order(self, *args, **kwargs):
        return self._order("cancel_order", *args, **kwargs)

    def modify_order(self, *args, **kwargs):
        return self._order("modify_order", *args, **kwargs)

    def get_order(self, *args, **kwargs):
        return self._order("get_order", *args, **kwargs)

    def get_orders(self, *args, **kwargs):
        return self._order("get_orders", *args, **kwargs)

    def create_oco(self, *args, **kwargs):
        return self._order("create_oco", *args, **kwargs)

    def cancel_conditional_order(self, *args, **kwargs):
        return self._order("cancel_conditional_order", *args, **kwargs)

    def conditional_orders(self, *args, **kwargs):
        return self._order("conditional_orders", *args, **kwargs)

    def capabilities(self) -> dict:
        """★ 사용자가 자기가 보고 있는 데이터의 품질을 모르는 채로 매매하면 안 된다."""
        has_toss = self.primary is not None
        has_backup = self.backup is not None

        limits = []
        if not has_toss:
            limits = [
                "분봉에 시가·고가·저가가 없어 '최근 N봉 고가 돌파' 판정이 종가 기준으로만 동작합니다.",
                "시장 전체 랭킹을 받을 수 없어 themes.yaml 에 적어둔 종목 안에서만 테마를 찾습니다.",
                "매수 유의사항(투자경고·단기과열 등)을 받을 수 없어 위험 종목을 걸러내지 못할 수 있습니다.",
                "상한가·하한가가 전일 종가 ±30% 추정치입니다.",
                "호가 잔량과 정확한 거래일 캘린더를 받을 수 없습니다(주말만 판단합니다).",
            ]

        quality = "full" if has_toss else ("limited" if has_backup else "degraded")

        return {
            "primary": "toss" if has_toss else None,
            "primary_label": SOURCE_LABELS["toss"] if has_toss else SOURCE_LABELS["none"],
            "has_toss": has_toss,
            "has_backup": has_backup,
            "source_of": dict(self.source_of),
            "failures": dict(self.failures),
            "limits": limits,
            "quality": quality,
        }


def build_router(cfg, *, clock=None, allow_web_fallback: bool = True) -> QuoteRouter:
    primary = None
    if cfg.client_id and cfg.client_secret:
        try:
            from daytrader.tossapi import TossClient
            from daytrader.paths import app_path
            primary = TossClient(cfg.client_id, cfg.client_secret, token_cache=app_path(".token_cache.json"))
        except Exception:
            primary = None

    backup = None
    if allow_web_fallback:
        try:
            from daytrader.webquote import build_web_client
            backup = build_web_client(cfg, clock=clock)
        except Exception:
            backup = None

    if primary is None and backup is None:
        raise RuntimeError("시세를 받아올 곳이 없습니다. 토스 API 키를 등록하거나 인터넷 연결을 확인하세요.")

    return QuoteRouter(primary, backup, cfg=cfg)
