"""토스가 없을 때 쓰는 인터넷 시세 소스(네이버). TossClient 와 같은 인터페이스를 흉내낸다.

공식 API 가 아니다. 소스 형식이 바뀌면 예고 없이 멈춘다. 깨지면 그건 이
프로그램의 버그가 아니라 소스 변경이다. 시세에 지연이 있다.
거래대금 랭킹과 투자경고 정보를 받아올 수 없어서 themes.yaml 에 적힌 종목만
유니버스로 삼고 랭킹은 그 안에서 계산한다.
과도한 호출은 상대 서버에 부담이고 차단 사유다.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET

from daytrader import netutil

log = logging.getLogger(__name__)

NAVER_POLL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{codes}"
NAVER_CHART = "https://fchart.stock.naver.com/sise.nhn"

PRICE_TTL = 20.0
CANDLE_TTL = 55.0  # 1분봉이라 1분 안에 두 번 받을 이유가 없다.
MIN_INTERVAL = 0.25  # 과도한 호출은 상대 서버에 부담이고 차단 사유다.


def _num(v) -> float:
    """"7,305억" "1조 2,817억" "252,500" 같은 표기를 숫자로 푼다.
    ★ 거래대금이 이런 형태로 오는데 float() 하면 즉시 깨진다.
    """
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return 0.0

    neg = s.startswith("-")
    s = s.lstrip("+-")
    total = 0.0

    m = re.search(r"([\d,\.]+)\s*조", s)
    if m:
        total += float(m.group(1).replace(",", "")) * 1e12
        s = s[m.end():]
    m = re.search(r"([\d,\.]+)\s*억", s)
    if m:
        total += float(m.group(1).replace(",", "")) * 1e8
        s = s[m.end():]
    m = re.search(r"([\d,\.]+)\s*만", s)
    if m:
        total += float(m.group(1).replace(",", "")) * 1e4
        s = s[m.end():]

    remaining = re.sub(r"[^\d.,]", "", s).replace(",", "")
    if remaining:
        try:
            total += float(remaining)
        except ValueError:
            pass

    return -total if neg else total


class WebQuoteClient:
    """TossClient 와 이름이 같은 메서드로 인터넷 공개 시세를 준다."""

    account_seq = None
    clock = None

    def __init__(self, universe: list | None = None):
        self._session = netutil.make_session()
        self.universe = universe or []
        self.names: dict = {}
        self._price_cache: dict = {}   # symbol -> (fetched_at, row)
        self._candle_cache: dict = {}  # (symbol, timeframe, count) -> (fetched_at, bars)
        self._last_call = 0.0
        self._warned_synthetic = False

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        self._last_call = time.monotonic()

    # ━━ 현재가 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def prices(self, symbols):
        symbols = list(symbols)
        now = time.monotonic()
        need = [s for s in symbols if s not in self._price_cache or now - self._price_cache[s][0] > PRICE_TTL]
        if need:
            for row in self._naver_prices(need):
                self._price_cache[row["symbol"]] = (now, row)
        return [self._price_cache[s][1] for s in symbols if s in self._price_cache]

    def _naver_prices(self, symbols: list) -> list:
        """★★ 실제 필드: itemCode/stockName/closePrice/compareToPreviousClosePrice/
        compareToPreviousPrice.name/integratedPriceInfo.*Raw. 짧은 키(cd/nv/pcv 등)는 오지 않는다.
        """
        self._throttle()
        resp = self._session.get(NAVER_POLL.format(codes=",".join(symbols)), timeout=5)
        resp.raise_for_status()
        data = resp.json()

        rows = []
        for area in (data.get("result", {}) or {}).get("areas", []):
            for d in area.get("datas", []):
                code = d.get("itemCode")
                if not code:
                    continue
                self.names[code] = d.get("stockName", code)

                close = _num(d.get("closePrice"))
                # 전일 종가가 직접 오지 않는다. closePrice - compareToPreviousClosePrice 로
                # 만들고, 부호는 compareToPreviousPrice.name 으로 정한다.
                diff = abs(_num(d.get("compareToPreviousClosePrice")))
                direction = (d.get("compareToPreviousPrice") or {}).get("name", "")
                if direction == "FALLING":
                    diff = -diff
                prev_close = close - diff
                change_rate = (diff / prev_close) if prev_close else 0.0

                integrated = d.get("integratedPriceInfo") or {}
                # *Raw 가 있으면 그걸 우선 쓴다 (사람이 읽는 표기보다 정확하다).
                volume = integrated.get("accumulatedTradingVolumeRaw")
                amount = integrated.get("accumulatedTradingValueRaw")
                if volume is None:
                    volume = d.get("accumulatedTradingVolume")
                if amount is None:
                    amount = d.get("accumulatedTradingValue")

                rows.append({
                    "symbol": code, "name": self.names.get(code, code),
                    "price": close, "changeRate": change_rate,
                    "tradingAmount": _num(amount), "volume": _num(volume),
                })
        return rows

    # ━━ 분봉/일봉 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def candles(self, symbol, timeframe, count, adjusted=True):
        key = (symbol, timeframe, count)
        now = time.monotonic()
        cached = self._candle_cache.get(key)
        if cached and now - cached[0] < CANDLE_TTL:
            return cached[1]

        bars = self._naver_candles(symbol, timeframe, count)
        if not bars:
            bars = self._yf_candles(symbol, timeframe, count)
        self._candle_cache[key] = (now, bars)
        return bars

    def _naver_candles(self, symbol: str, timeframe: str, count: int) -> list:
        """응답 예: <item data="202608260900|null|null|null|257000|548187"/>"""
        self._throttle()
        params = {
            "symbol": symbol, "timeframe": "day" if timeframe == "1d" else "minute",
            "count": count, "requestType": 0,
        }
        try:
            resp = self._session.get(NAVER_CHART, params=params, timeout=5)
            resp.raise_for_status()
            text = resp.text
        except Exception as exc:
            log.warning("네이버 분봉 조회 실패: %s", exc)
            return []

        bars = []
        for raw in self._parse_naver_xml(text):
            parts = raw.split("|")
            if len(parts) < 6:
                continue
            ts_raw, o, h, low, c, v = parts[:6]
            close = _num(c)
            if close == 0:
                continue
            # 1) O/H/L 이 null 로 온다. 종가와 거래량만 있다.
            synthetic = any(x in (None, "", "null") for x in (o, h, low))
            bars.append({
                "timestamp": self._parse_naver_ts(ts_raw),
                "openPrice": None if synthetic else _num(o),
                "highPrice": None if synthetic else _num(h),
                "lowPrice": None if synthetic else _num(low),
                "closePrice": close, "volume": _num(v),
                "synthetic_ohl": synthetic,
            })

        bars.sort(key=lambda b: b["timestamp"])
        self._fill_synthetic_ohl(bars)
        # ★ 정렬 직후 차분을 낸다 - 정렬 전에 하면 순서가 뒤섞여 차분이 무의미해진다.
        self._decumulate(bars)
        return bars

    def _parse_naver_xml(self, text: str) -> list:
        try:
            root = ET.fromstring(text)
            return [item.attrib.get("data", "") for item in root.findall(".//item")]
        except ET.ParseError:
            # XML 파싱 실패 시 정규식 폴백.
            return re.findall(r'data="([^"]+)"', text)

    def _parse_naver_ts(self, raw: str) -> str:
        """timestamp 는 YYYYMMDDHHMM / YYYYMMDDHHMMSS / YYYYMMDD 세 형태로 온다."""
        raw = raw.strip()
        if len(raw) == 12:
            y, mo, d, h, mi = raw[:4], raw[4:6], raw[6:8], raw[8:10], raw[10:12]
            return f"{y}-{mo}-{d}T{h}:{mi}:00+09:00"
        if len(raw) == 14:
            y, mo, d, h, mi, s = raw[:4], raw[4:6], raw[6:8], raw[8:10], raw[10:12], raw[12:14]
            return f"{y}-{mo}-{d}T{h}:{mi}:{s}+09:00"
        if len(raw) == 8:
            y, mo, d = raw[:4], raw[4:6], raw[6:8]
            return f"{y}-{mo}-{d}T00:00:00+09:00"
        return raw

    def _fill_synthetic_ohl(self, bars: list) -> None:
        """직전 종가를 시가로, 고가·저가를 시가~종가 범위로 근사한다.
        ★ 봉 안의 진폭을 알 수 없어 돌파 판정이 실제보다 보수적이다. 딱 한 번만 경고한다.
        """
        prev_close = None
        any_synthetic = False
        for b in bars:
            if b["synthetic_ohl"]:
                any_synthetic = True
                open_ = prev_close if prev_close is not None else b["closePrice"]
                b["openPrice"] = open_
                b["highPrice"] = max(open_, b["closePrice"])
                b["lowPrice"] = min(open_, b["closePrice"])
            prev_close = b["closePrice"]

        if any_synthetic and not self._warned_synthetic:
            log.warning(
                "네이버 분봉에 시가·고가·저가가 없어 근사값을 씁니다 - "
                "봉 안의 진폭을 알 수 없어 돌파 판정이 실제보다 보수적입니다."
            )
            self._warned_synthetic = True

    def _decumulate(self, bars: list) -> None:
        """★ 거래량이 누적값이다(단조 증가). 그대로 쓰면 volume_ratio 가 늘 1
        근처가 되어 거래량 급증 조건이 꺼진 채로 매매한다 - 화면에는 이상이
        없어 보이지만 진입 3조건 중 하나가 조용히 무력화된다.
        """
        if len(bars) < 2:
            return
        volumes = [b["volume"] for b in bars]
        rising = sum(1 for i in range(1, len(volumes)) if volumes[i] >= volumes[i - 1])
        if rising < len(volumes) - 1:
            return  # 이미 봉별 거래량이면(단조 증가가 아니면) 건드리지 않는다.

        prev = 0.0
        for b in bars:
            cur = b["volume"]
            b["volume"] = max(cur - prev, 0.0)
            prev = cur

    def _yf_candles(self, symbol: str, timeframe: str, count: int) -> list:
        """yfinance 가 설치돼 있을 때만 쓰는 마지막 폴백."""
        try:
            import yfinance as yf
        except ImportError:
            return []

        interval = "1d" if timeframe == "1d" else "1m"
        period = f"{max(count, 5)}d" if interval == "1d" else "5d"
        for suffix in (".KS", ".KQ"):
            try:
                hist = yf.Ticker(symbol + suffix).history(period=period, interval=interval)
                if hist.empty:
                    continue
                bars = []
                for idx, row in hist.tail(count).iterrows():
                    bars.append({
                        "timestamp": idx.isoformat(),
                        "openPrice": float(row["Open"]), "highPrice": float(row["High"]),
                        "lowPrice": float(row["Low"]), "closePrice": float(row["Close"]),
                        "volume": float(row["Volume"]),
                    })
                return bars
            except Exception:
                continue
        return []

    # ━━ 그 밖 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def price_limits(self, symbol):
        rows = self.prices([symbol])
        if not rows:
            raise RuntimeError(f"{symbol} 현재가를 받지 못해 가격 제한폭을 추정할 수 없습니다.")
        price, change_rate = rows[0]["price"], rows[0]["changeRate"]
        prev_close = price / (1 + change_rate) if (1 + change_rate) else price
        return {"symbol": symbol, "upperLimit": round(prev_close * 1.30), "lowerLimit": round(prev_close * 0.70)}

    def orderbook(self, symbol):
        raise RuntimeError("인터넷 시세로는 호가 잔량을 받을 수 없습니다.")

    def stocks(self, symbols):
        rows = self.prices(symbols)
        found = {r["symbol"] for r in rows}
        return [{"symbol": s, "name": self.names.get(s, s)} for s in symbols if s in found or True]

    def warnings(self, symbol):
        """받아올 방법이 없다. 이 한계는 QuoteRouter.capabilities() 가 화면에 알린다."""
        return []

    def market_calendar_kr(self):
        from daytrader.timeutil import now_kst
        n = now_kst()
        return {
            "open": n.weekday() < 5, "date": n.strftime("%Y-%m-%d"),
            "note": "공휴일 정보가 없어 주말만 판단합니다.",
        }

    def accounts(self):
        raise RuntimeError("인터넷 시세 소스는 계좌 정보를 제공하지 않습니다.")

    def resolve_account(self, prefer=None):
        raise RuntimeError("인터넷 시세 소스는 계좌 정보를 제공하지 않습니다.")

    def holdings(self):
        raise RuntimeError("인터넷 시세 소스는 계좌 정보를 제공하지 않습니다.")

    def buying_power(self):
        raise RuntimeError("인터넷 시세 소스는 계좌 정보를 제공하지 않습니다.")

    def sellable_quantity(self, symbol):
        raise RuntimeError("인터넷 시세 소스는 계좌 정보를 제공하지 않습니다.")

    def rankings(self, type, marketCountry="KR", duration=None, count=100, excludeInvestmentCaution=None):
        """themes.yaml 유니버스 안에서 계산한다 - 시장 전체 랭킹을 받을 방법이 없다."""
        universe = self.universe or list(self.names.keys())
        if not universe:
            return []
        rows = self.prices(universe)
        rows.sort(key=lambda r: r.get("changeRate", 0.0), reverse=True)
        return rows[:count]

    # ━━ 주문 API 는 전부 막는다 - 실주문 경로가 물리적으로 없어야 한다 ━━━━━

    def _no(self, *args, **kwargs):
        raise RuntimeError("인터넷 시세 클라이언트는 주문을 낼 수 없습니다.")

    create_order = modify_order = cancel_order = create_oco = \
        cancel_conditional_order = get_order = get_orders = conditional_orders = _no

    # ━━ 자가 진단 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def self_test(self, sample: int = 3) -> dict:
        """UI 의 "시세 연결 확인" 버튼용."""
        candidates = (self.universe or list(self.names.keys())) or ["005930", "000660", "035420"]
        targets = candidates[:sample]

        results = []
        ok_count = 0
        for s in targets:
            try:
                rows = self.prices([s])
                ok = bool(rows)
                ok_count += int(ok)
                results.append({"symbol": s, "ok": ok, "price": rows[0]["price"] if rows else None})
            except Exception as exc:
                results.append({"symbol": s, "ok": False, "error": str(exc)})

        return {"ok": ok_count > 0, "tested": len(targets), "succeeded": ok_count, "results": results}


def build_web_client(cfg, clock=None) -> WebQuoteClient:
    from daytrader.screener import load_themes
    themes = load_themes(cfg.themes_file)
    universe = sorted({s for codes in themes.values() for s in codes})
    client = WebQuoteClient(universe=universe)
    client.clock = clock
    return client
