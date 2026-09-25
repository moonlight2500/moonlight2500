"""TossClient 와 같은 인터페이스라 엔진·스크리너가 자기가 가짜 데이터를
보고 있다는 걸 모른 채 그대로 돈다.
⚠️ 연습용이다. 호가 잔량, VI, 체결 우선순위, 뉴스 반응, 군집 행동은 재현하지
않는다. 여기서 잘 나온 성적은 아무것도 보장하지 않는다.
"""

from __future__ import annotations

import bisect
import hashlib
import random
import re
from datetime import date, timedelta

import yaml

from daytrader.clock import RealClock
from daytrader.timeutil import combine, day_str, iso, parse_hhmm

SESSION_START = (9, 0)
SESSION_END = (15, 30)
BARS_PER_DAY = 390

SCENARIOS = {
    "normal": "평범한 날 — 테마 하나가 조용히 움직인다",
    "strong_theme": "강한 테마 — 두 테마가 뚜렷하게 오른다",
    "choppy": "방향 없는 날 — 신호가 잘 안 나오는 게 정상이다",
    "crash": "급락장 — 속임수 반등 뒤 무너진다. 손절이 제대로 도는지 본다",
}

_THEME_NAME_RE = re.compile(r'\s*-\s*"?(\d{6})"?\s*#\s*(.+?)\s*$')


def load_theme_names(path: str) -> dict:
    """themes.yaml 을 정규식으로 훑어 {종목코드: 종목명} 을 만든다."""
    names: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = _THEME_NAME_RE.match(line)
            if m:
                names[m.group(1)] = m.group(2)
    return names


def _load_theme_groups(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("themes", {})


def _seed_of(*parts) -> int:
    """재현 가능한 시드. sha256 앞 12자리를 정수로 쓴다."""
    s = "|".join(str(p) for p in parts)
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()
    return int(h[:12], 16)


def _parse_day(s: str) -> date:
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def _interpolate(key_points, n):
    """키프레임 (인덱스, 배율) 사이를 선형보간한다."""
    out = [0.0] * n
    for (i0, v0), (i1, v1) in zip(key_points, key_points[1:]):
        span = i1 - i0
        for i in range(i0, i1 + 1):
            t = (i - i0) / span if span else 0.0
            out[i] = v0 + (v1 - v0) * t
    return out


class SimClient:
    """TossClient 와 같은 이름의 메서드로 가짜 시세를 만들어 낸다."""

    account_seq = 0

    def __init__(self, cfg, clock=None, themes_path=None):
        self.cfg = cfg
        self.clock = clock or RealClock()
        self.scenario = cfg.simulation.scenario
        self.seed = cfg.simulation.seed
        self.day = day_str(self.clock.now())

        themes_path = themes_path or cfg.themes_file
        self.theme_names = load_theme_names(themes_path)
        self.themes = _load_theme_groups(themes_path)

        self._plan_cache: dict[str, dict] = {}
        self._bars_cache: dict[tuple, list] = {}
        self._anchor_cache: dict[tuple, float] = {}

        self._cash = float(cfg.capital.allocation)
        self._holdings: dict[str, dict] = {}

    # ━━ 하루 설계 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _plan_for_day(self, day: str) -> dict:
        if day in self._plan_cache:
            return self._plan_cache[day]

        rng = random.Random(_seed_of(self.seed, day, self.scenario))
        theme_list = list(self.themes.keys())

        # 하루 설계: hot 테마 수 normal=1 strong_theme=2 choppy=0 crash=0
        hot_count = {"normal": 1, "strong_theme": 2, "choppy": 0, "crash": 0}[self.scenario]
        hot_themes = rng.sample(theme_list, k=min(hot_count, len(theme_list))) if hot_count else []

        all_symbols = [code for codes in self.themes.values() for code in codes]
        # 유의종목 하나를 심어 필터가 작동하는지 보이게 한다.
        warned_symbol = rng.choice(all_symbols) if all_symbols else None

        profiles = {}
        for theme, codes in self.themes.items():
            for idx, code in enumerate(codes):
                profiles[code] = self._build_profile(theme, idx, theme in hot_themes, rng)

        plan = {"hot_themes": hot_themes, "warned_symbol": warned_symbol, "profiles": profiles}
        self._plan_cache[day] = plan
        return plan

    def _build_profile(self, theme, idx, is_hot, rng) -> dict:
        breakout = False
        fake_breakout = False

        if self.scenario == "crash":
            # crash 오늘: drift -13%~-6%, breakout 없음
            drift = rng.uniform(-0.13, -0.06)
        elif is_hot:
            # hot: 테마 내 index<2 는 대장주(+4%~+11%), 나머지 후발주(+1.5%~+5%)
            drift = rng.uniform(0.04, 0.11) if idx < 2 else rng.uniform(0.015, 0.05)
            breakout = rng.random() < 0.15
        elif self.scenario == "choppy":
            # choppy: ±2%, breakout 없음
            drift = rng.uniform(-0.02, 0.02)
        else:
            # 그 외: -2.5%~+3%, 15% 확률 breakout
            drift = rng.uniform(-0.025, 0.03)
            breakout = rng.random() < 0.15

        if breakout and rng.random() < 0.45:
            # ★★ 이걸 빼면 돌파 전략이 100% 이기는 것처럼 보인다.
            # 실전에서 손실의 주된 원인이 바로 속임수 돌파다.
            fake_breakout = True
            drift = rng.uniform(-0.06, -0.005)

        return {
            "theme": theme, "drift": drift, "breakout": breakout,
            "fake_breakout": fake_breakout, "is_hot": is_hot,
        }

    def _base_price(self, symbol: str) -> float:
        return 5000 + (_seed_of(symbol) % 95000)

    # ━━ 성능: 스칼라 앵커 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _anchor(self, symbol: str, day: str) -> float:
        """봉을 만들지 않고 일간 수익률만 스칼라로 이어 계산한다.
        하루치 봉 생성 비용의 1/1000. 필요한 날만 실제로 봉을 만들 수 있게 된다.
        """
        key = (symbol, day)
        if key in self._anchor_cache:
            return self._anchor_cache[key]
        plan = self._plan_for_day(day)
        profile = plan["profiles"].get(symbol, {"drift": 0.0})
        ret = profile["drift"]
        self._anchor_cache[key] = ret
        return ret

    # ━━ 봉 생성 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _full_day_bars(self, symbol: str, day: str) -> list:
        key = (symbol, day)
        if key in self._bars_cache:
            return self._bars_cache[key]

        plan = self._plan_for_day(day)
        profile = plan["profiles"].get(
            symbol, {"drift": 0.0, "breakout": False, "fake_breakout": False, "is_hot": False},
        )

        rng = random.Random(_seed_of(self.seed, day, symbol))
        base_price = self._base_price(symbol)
        vol = base_price * 0.006
        n = BARS_PER_DAY
        drift = profile["drift"]
        breakout_at = None

        # ★ 먼저 키프레임으로 하루의 이야기를 정하고 사이를 선형보간한 뒤 노이즈를 얹는다.
        # 봉마다 증분을 더하면 노이즈가 누적돼 시나리오가 뭉개진다.
        if profile["fake_breakout"]:
            peak_at = int(n * rng.uniform(0.15, 0.35))
            peak_gain = rng.uniform(0.02, 0.05)
            breakout_at = peak_at
            key_points = [(0, 1.0), (peak_at, 1.0 + peak_gain), (n - 1, 1.0 + drift)]
        elif profile["breakout"]:
            breakout_at = int(n * rng.uniform(0.3, 0.7))
            key_points = [(0, 1.0), (breakout_at, 1.0 + drift * 0.3), (n - 1, 1.0 + drift)]
        else:
            mid = n // 2
            key_points = [(0, 1.0), (mid, 1.0 + drift * 0.5), (n - 1, 1.0 + drift)]

        mults = _interpolate(key_points, n)
        start_dt = combine(_parse_day(day), parse_hhmm(f"{SESSION_START[0]:02d}:{SESSION_START[1]:02d}"))

        bars = []
        noise = 0.0
        prev_close = base_price
        for i in range(n):
            # 노이즈는 평균회귀: noise = noise*0.85 + gauss(0, vol/3)
            noise = noise * 0.85 + rng.gauss(0, vol / 3)
            close = base_price * mults[i] + noise
            open_ = prev_close
            hi = max(open_, close) + abs(rng.gauss(0, vol * 0.3))
            lo = min(open_, close) - abs(rng.gauss(0, vol * 0.3))

            # 거래량: 돌파 구간 3~8배, 장 초반 30봉 1.8배 · 마감 30봉 1.4배 (U자)
            vmult = 1.0
            if i < 30:
                vmult = 1.8
            elif i >= n - 30:
                vmult = 1.4
            if breakout_at is not None and abs(i - breakout_at) <= 3:
                vmult *= rng.uniform(3.0, 8.0)
            volume = max(1, int(rng.gauss(1000, 200) * vmult))

            ts = iso(start_dt + timedelta(minutes=i))
            bars.append({
                "timestamp": ts, "openPrice": round(open_), "highPrice": round(hi),
                "lowPrice": round(lo), "closePrice": round(close), "volume": volume,
            })
            prev_close = close

        self._bars_cache[key] = bars
        return bars

    def _today_bars(self, symbol: str, day: str | None = None) -> list:
        """★ 시계 시각까지만 이분탐색으로 잘라 준다. 미래 봉 절대 금지."""
        day = day or day_str(self.clock.now())
        bars = self._full_day_bars(symbol, day)
        now_iso = iso(self.clock.now())
        timestamps = [b["timestamp"] for b in bars]
        idx = bisect.bisect_right(timestamps, now_iso)
        return bars[:idx]

    # ━━ TossClient 인터페이스 전부 (읽기) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def prices(self, symbols):
        out = []
        for s in symbols:
            bars = self._today_bars(s)
            base = self._base_price(s)
            close = bars[-1]["closePrice"] if bars else base
            out.append({
                "symbol": s, "price": close,
                "changeRate": (close - base) / base if base else 0.0,
                "volume": sum(b["volume"] for b in bars),
            })
        return out

    def candles(self, symbol, timeframe, count, adjusted=True):
        if timeframe in ("1m", "1min", "minute"):
            return self._today_bars(symbol)[-count:]

        # 1d(일봉): 여러 날의 종가를 _anchor() 로 싸게 이어 계산한다.
        base = self._base_price(symbol)
        cur_day = _parse_day(day_str(self.clock.now()))
        history = []
        d = cur_day
        while len(history) < count:
            if d.weekday() < 5:
                ds = d.strftime("%Y-%m-%d")
                history.append((ds, self._anchor(symbol, ds)))
            d -= timedelta(days=1)
        history.reverse()

        rows = []
        cum = 1.0
        for ds, ret in history:
            cum *= (1 + ret)
            close = base * cum
            rows.append({
                "timestamp": ds, "openPrice": round(close * 0.99), "highPrice": round(close * 1.01),
                "lowPrice": round(close * 0.98), "closePrice": round(close), "volume": 100000,
            })
        return rows

    def orderbook(self, symbol):
        bars = self._today_bars(symbol)
        last = bars[-1]["closePrice"] if bars else self._base_price(symbol)
        return {"symbol": symbol, "bidPrice": last - 10, "askPrice": last + 10}

    def price_limits(self, symbol):
        base = self._base_price(symbol)
        return {"symbol": symbol, "upperLimit": round(base * 1.30), "lowerLimit": round(base * 0.70)}

    def rankings(self, type, marketCountry="KR", duration=None, count=100, excludeInvestmentCaution=None):
        plan = self._plan_for_day(self.day)
        rows = []
        for code in plan["profiles"]:
            bars = self._today_bars(code)
            if not bars:
                continue
            base = self._base_price(code)
            close = bars[-1]["closePrice"]
            amount = sum(b["closePrice"] * b["volume"] for b in bars)
            rows.append({
                "symbol": code, "name": self.theme_names.get(code, code),
                "price": close, "tradingAmount": amount,
                "changeRate": (close - base) / base if base else 0.0,
                "isWarned": code == plan["warned_symbol"],
            })
        rows.sort(key=lambda r: r["changeRate"], reverse=True)
        if excludeInvestmentCaution:
            rows = [r for r in rows if not r["isWarned"]]
        return rows[:count]

    def stocks(self, symbols):
        plan = self._plan_for_day(self.day)
        out = []
        for s in symbols:
            out.append({
                "symbol": s, "name": self.theme_names.get(s, s),
                "delisted": False, "tradingHalt": False,
                "isPreferred": False, "isETF": False, "isETN": False,
                "isLeveraged": False, "isInverse": False,
            })
        return out

    def warnings(self, symbol):
        plan = self._plan_for_day(self.day)
        if symbol == plan["warned_symbol"]:
            return [{"symbol": symbol, "type": "INVESTMENT_WARNING"}]
        return []

    def market_calendar_kr(self):
        return {"open": True, "date": self.day}

    def accounts(self):
        return [{"accountSeq": self.account_seq, "accountType": "BROKERAGE", "accountNumber": "SIM-0000"}]

    def resolve_account(self, prefer=None):
        return self.account_seq

    def buying_power(self):
        return {"cash": self._cash, "buyingPower": self._cash}

    def holdings(self):
        return list(self._holdings.values())

    def sellable_quantity(self, symbol):
        h = self._holdings.get(symbol)
        return {"sellableQuantity": h["quantity"] if h else 0}

    # ━━ 주문 API 는 전부 막는다 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _no(self, *a, **kw):
        raise RuntimeError("시뮬레이션 모드에서는 실주문 API를 호출할 수 없습니다.")

    create_order = modify_order = cancel_order = create_oco = \
        cancel_conditional_order = get_order = get_orders = conditional_orders = _no


def build_sim_client(cfg, clock=None):
    return SimClient(cfg, clock=clock)
