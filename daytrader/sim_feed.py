"""해외주식·암호화폐 시뮬레이션(sim) 모드용 합성 시세 생성기.

★★★ 실제로 겪은 문제 - 해외주식·암호화폐의 sim 모드가 "시뮬레이션"이라는
이름과 달리 실제 토스/빗썸 API 를 그대로 호출하고 있었다. 그래서 API 키가
없거나 네트워크가 막히면 아무것도 못 하고 조용히 실패했다("전혀 동작 안
한다"는 문의의 정체). 국내주식의 sim 이 SimClient 로 합성 시세를 만들어
실제 API 없이 도는 것과 대조적이었다.

이 모듈은 국내 SimClient 와 같은 취지로, 실제 API 를 전혀 부르지 않고
씨앗(seed)만으로 재현 가능한 가짜 시세를 만든다. 국내 SimClient 를 그대로
못 쓰는 이유는 그쪽이 "테마 스크리닝"을 전제로 짜여 있어서다(해외주식·
암호화폐에는 테마 개념이 없다).

★ 재현 가능성 - 같은 seed·같은 종목이면 항상 같은 시세가 나온다. 그래야
"어제 이 설정으로 돌렸을 때와 결과가 다르다"를 판단할 수 있다.
"""

from __future__ import annotations

import hashlib
import math
import random
import time


def _seed_for(symbol: str, base_seed: int) -> int:
    """★ 종목마다 다르지만 실행할 때마다 같은 씨앗 - 종목명을 해시해 섞는다."""
    h = hashlib.sha256(f"{symbol}:{base_seed}".encode("utf-8")).hexdigest()
    return int(h[:8], 16)


def _base_price_for(symbol: str) -> float:
    """★ 종목마다 그럴듯한 기준가를 정한다 - BTC 가 10만원이거나 AAPL 이
    100만달러이면 화면이 어색하다. 실제 값과 같을 필요는 없고, 자릿수만
    자연스러우면 시뮬레이션 목적에는 충분하다."""
    known = {
        "KRW-BTC": 95_000_000.0, "KRW-ETH": 4_500_000.0, "KRW-XRP": 3_000.0,
        "KRW-SOL": 250_000.0, "KRW-DOGE": 300.0,
        "AAPL": 230.0, "NVDA": 180.0, "MSFT": 430.0, "GOOGL": 200.0,
        "TSLA": 350.0, "AMZN": 220.0, "META": 600.0,
    }
    if symbol in known:
        return known[symbol]
    # ★ 모르는 종목은 이름 해시로 자릿수를 정한다 - 원화 마켓(KRW-)이면
    # 큰 값, 미국 티커면 두세 자리 달러가 자연스럽다.
    h = _seed_for(symbol, 0)
    if symbol.startswith("KRW-"):
        return 1_000.0 * (1 + h % 50_000)
    return 20.0 + (h % 500)


class SimFeedClient:
    """★ 해외주식(TossClient)·암호화폐(BithumbClient) 양쪽의 메서드 이름을
    모두 흉내낸다 - 두 엔진이 서로 다른 이름을 쓰기 때문이다. 실제 주문
    메서드(create_order/place_order)도 받아주되 아무 데도 보내지 않는다.

    ★★ 이 클래스는 sim 모드에서만 쓰인다. live/paper/web 모드는 예전처럼
    진짜 클라이언트를 쓴다 - 시뮬레이터가 실거래 경로에 끼어들 일은 없다.
    """

    account_seq = 0

    def __init__(self, seed: int = 42, volatility: float = 0.02, drift: float = 0.0005):
        self.seed = seed
        self.volatility = volatility
        self.drift = drift
        self._t0 = time.time()

    # ━━ 내부: 가격 곡선 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _series(self, symbol: str, count: int) -> list:
        """★ 랜덤워크 + 완만한 추세로 count 개의 종가를 만든다.
        ★★ 마지막 구간에 가끔 돌파(급등)를 넣는다 - 그게 없으면 진입
        기법이 영원히 발동하지 않아 "시뮬레이션인데 아무것도 안 산다"가
        된다(실제로 겪은 문제의 재발 방지).
        """
        rng = random.Random(_seed_for(symbol, self.seed))
        base = _base_price_for(symbol)
        prices = []
        p = base
        for i in range(count):
            shock = rng.gauss(self.drift, self.volatility)
            p = max(base * 0.3, p * (1 + shock))
            prices.append(p)

        # ★ 이 종목이 "오늘 돌파하는 종목"인지 씨앗으로 결정한다 - 매번
        # 전부 급등하면 그것도 비현실적이라, 대략 3종목 중 1종목만.
        if rng.random() < 0.35 and count >= 5:
            for j in range(1, 4):
                prices[-j] = prices[-4] * (1 + 0.03 * (4 - j))
        return prices

    def _last_price(self, symbol: str) -> float:
        # ★★★ 실제로 겪은 문제 - candles() 는 보통 80개를 요청받는데(overseas_engine.py
        # _fetch_bars 기본값) 여기는 60개로 시리즈를 만들었다. _series() 뒷부분(마지막 3개)에
        # "오늘 돌파하는 종목" 급등을 얹는 로직이 있어서, 60개짜리와 80개짜리는 부스트가 서로
        # 다른 지점(56~59번째 vs 76~79번째)에 걸려 마지막 값이 완전히 달라진다. 그 결과
        # prices()(여기)와 candles()(진짜 체결가 검증에 쓰는 실시간가 대역)가 서로 어긋나서,
        # 진입 직전 교차 검증(overseas_engine.py, "해외주식 진입가가 이상해" 버그 수정)이
        # sim 모드에서도 잘못 걸렸다. candles() 의 흔한 기본 개수(80)로 맞춘다.
        return self._series(symbol, 80)[-1]

    def _candles(self, symbol: str, count: int) -> list:
        prices = self._series(symbol, count)
        rng = random.Random(_seed_for(symbol, self.seed) + 1)
        out = []
        for i, close in enumerate(prices):
            spread = close * 0.004
            high = close + abs(rng.gauss(0, spread))
            low = close - abs(rng.gauss(0, spread))
            open_ = prices[i - 1] if i > 0 else close
            # ★ 마지막 봉의 거래량을 키운다 - 거래량 급증 조건을 쓰는
            # 기법들이 시뮬레이션에서도 판단할 거리가 있어야 한다.
            volume = 500_000 * (1 + rng.random())
            if i >= count - 3:
                volume *= 3.5
            out.append({"open": open_, "high": max(high, close, open_),
                        "low": min(low, close, open_), "close": close,
                        "volume": volume, "timestamp": str(i)})
        return out

    # ━━ 해외주식(TossClient) 인터페이스 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def candles(self, symbol: str, interval=None, count: int = 80, before=None, unit=None, to=None):
        """★ 해외주식은 candles(symbol, "1d", count), 암호화폐는
        candles(market, unit=1, count=200) 로 부른다 - 둘 다 받아준다.
        반환 형식도 각 엔진이 기대하는 키를 모두 넣어 준다."""
        rows = self._candles(symbol, count)
        if unit is not None:
            # ★ 암호화폐(빗썸) 형식 - 키 이름이 다르다.
            return [
                {"opening_price": r["open"], "high_price": r["high"],
                 "low_price": r["low"], "trade_price": r["close"],
                 "candle_acc_trade_volume": r["volume"]}
                for r in rows
            ]
        return rows

    def prices(self, symbols):
        return [{"symbol": s, "price": self._last_price(s)} for s in symbols]

    def buying_power(self, currency: str = "KRW"):
        return {"cash": 100_000_000.0 if currency == "KRW" else 100_000.0}

    def create_order(self, symbol, side, orderType, quantity, price=None, timeInForce="DAY", clientOrderId=None):
        # ★★ 시뮬레이션이므로 어디에도 보내지 않는다 - 체결된 척만 한다.
        return {"orderId": f"sim-{symbol}-{side}-{int(time.time()*1000)}"}

    def rankings(self, type=None, marketCountry="US", duration=None, count=100, excludeInvestmentCaution=None):
        """★ 해외주식 자동 선정(auto_select)이 이걸 부른다 - 시뮬레이션
        에서도 종목이 뽑혀야 매매가 일어난다."""
        pool = ["AAPL", "NVDA", "MSFT", "GOOGL", "TSLA", "AMZN", "META"]
        rng = random.Random(_seed_for(str(type), self.seed))
        picked = pool[:]
        rng.shuffle(picked)
        return [{"symbol": s} for s in picked[:count]]

    def resolve_account(self, prefer=None):
        return 0

    def stocks(self, symbols):
        return [{"symbol": s, "name": s} for s in symbols]

    # ━━ 암호화폐(BithumbClient) 인터페이스 ━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def ticker(self, markets):
        return [{"market": m, "trade_price": self._last_price(m)} for m in markets]

    def accounts(self):
        return [{"currency": "KRW", "balance": "100000000"}]

    def place_order(self, market, side, order_type, price=None, volume=None,
                    client_order_id=None, time_in_force=None):
        return {"uuid": f"sim-{market}-{side}-{int(time.time()*1000)}"}

    def order_chance(self, market):
        return {"market": {"id": market}, "bid_fee": "0.0004", "ask_fee": "0.0004"}
