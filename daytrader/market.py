"""24시간 켜두는 것을 전제로 한다. 국내장이 닫혀도 미국 지수선물은 계속
돌고, 밤사이 그것이 움직이면 다음 날 아침 갭이 된다.

★ 이 값들은 매매 판단에 개입하지 않는다. 보기만 하는 화면이다.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from daytrader import netutil
from daytrader.timeutil import now_kst

_log = logging.getLogger(__name__)


def _short_error(exc: Exception, context: str) -> str:
    """★★ [2-8] 실제로 겪을 수 있는 문제 - requests 예외 메시지는 흔히 사내 프록시 주소나
    이 PC가 실제로 접속한 호스트명 등 내부 네트워크 정보를 그대로 담고 있다(예:
    "HTTPSConnectionPool(host='proxy.internal', port=8080): ...").  이 값들은 매매 판단과
    무관한 "보기만 하는" 시세 카드일 뿐인데, str(exc) 를 그대로 화면(/api/market)에 보내면
    이 PC가 어떤 사내망·프록시 뒤에 있는지가 드러난다. 그래서 화면에는 짧고 안전한 한국어
    메시지만 보내고, 실제 원인은 서버 로그에만(개발자가 진단할 때만 보이게) 남긴다."""
    _log.warning("시세 조회 실패(%s): %s", context, exc)
    return "일시적으로 시세를 가져오지 못했습니다."

REFRESH_CHOICES = [5, 10, 20, 30, 60, 120, 300, 0]  # 0 = 자동 갱신 끔
DEFAULT_REFRESH = 20
_MIN_TTL = 5.0

NAVER_DOMESTIC_INDEX = "https://polling.finance.naver.com/api/realtime/domestic/index/{codes}"
NAVER_DOMESTIC_STOCK = "https://polling.finance.naver.com/api/realtime/domestic/stock/{codes}"
NAVER_WORLD_INDEX = "https://polling.finance.naver.com/api/realtime/worldstock/index/{codes}"
NAVER_WORLD_STOCK = "https://polling.finance.naver.com/api/realtime/worldstock/stock/{codes}"
NAVER_FX = "https://api.stock.naver.com/marketindex/exchange/{code}"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{code}"
UPBIT_TICKER = "https://api.upbit.com/v1/ticker"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/24hr"

# ━━ 볼 항목 (그룹 순서대로) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DOMESTIC_INDEX_ITEMS = [("KOSPI", "코스피"), ("KOSDAQ", "코스닥"), ("KPI200", "코스피200")]
FUTURES_ITEMS = [("NQ=F", "나스닥 선물"), ("ES=F", "S&P500 선물"), ("YM=F", "다우 선물")]
WORLD_INDEX_ITEMS = [
    (".IXIC", "나스닥"), (".INX", "S&P500"), (".DJI", "다우"),
    (".N225", "니케이225"), (".HSI", "항셍"), (".SSEC", "상해종합"),
]
US_LARGECAP_ITEMS = [
    ("NVDA", "엔비디아"), ("AAPL", "애플"), ("MSFT", "마이크로소프트"), ("GOOGL", "알파벳"),
    ("AMZN", "아마존"), ("META", "메타"), ("AVGO", "브로드컴"), ("TSLA", "테슬라"),
    ("BRK-B", "버크셔B"), ("LLY", "일라이릴리"),
]
DOMESTIC_STOCK_EXTRA_ITEMS = [("000660", "SK하이닉스"), ("005930", "삼성전자")]  # ★ "국내" 그룹으로 옮김(원래 AI반도체에 있었음)
# ★★ 시가총액 상위 종목은 실시간 랭킹 API(코스피/코스닥 구분 필드가 실측
# 검증되지 않았다)에 기대는 대신, 확실히 맞는 종목 코드를 정적으로 확정해
# 시세만 실시간으로 받는다 - 틀릴 여지가 있는 부분을 줄이는 게 목적이다.
# 코스피 상위 5개(삼성전자·SK하이닉스 포함 - AI반도체에서 옮긴 것과 자연히
# 합쳐진다), 코스닥 상위 3개.
KOSPI_TOP5_ITEMS = [
    ("005930", "삼성전자"), ("000660", "SK하이닉스"), ("373220", "LG에너지솔루션"),
    ("207940", "삼성바이오로직스"), ("005380", "현대차"),
]
KOSDAQ_TOP3_ITEMS = [("247540", "에코프로비엠"), ("196170", "알테오젠"), ("086520", "에코프로")]
# ★★ "AI반도체" -> "미국-HBM" 으로 개편. DRAM(Roundhill Memory ETF, 2026-04 상장 -
# HBM·DRAM·NAND 등 메모리 반도체 전문 ETF)과 SOXX(iShares Semiconductor ETF)를
# 추가했다 - 둘 다 실제 상장된 티커다(뉴스 검색으로 확인).
US_HBM_ITEMS = [
    ("MU", "마이크론"), ("AMD", "AMD"), ("ASML", "ASML"), ("ARM", "ARM"), ("TSM", "TSMC"),
    ("DRAM", "DRAM ETF"), ("SOXX", "반도체 ETF"),
]
FX_NAVER_ITEMS = [("FX_USDKRW", "달러/원"), ("FX_JPYKRW", "엔/원"), ("FX_EURKRW", "유로/원")]
FX_YAHOO_ITEMS = [("DX-Y.NYB", "달러인덱스")]
COIN_ITEMS = [("BTC", "비트코인"), ("ETH", "이더리움"), ("XRP", "리플")]  # 시가총액 순


def _sign(direction) -> int:
    """★ 네이버 등락폭은 부호가 없다. 방향 필드로 직접 부호를 붙인다.
    ★ 실측 문서상 코드값은 정수 2/5 로 올 수도, 문자열 "2"/"5" 로 올 수도
    있다 - 둘 다 안전하게 비교하려고 문자열로 통일해서 비교한다.
    """
    d = str(direction) if direction is not None else ""
    if d in ("RISING", "UP", "2"):
        return 1
    if d in ("FALLING", "DOWN", "5"):
        return -1
    return 0


def _dp_for(kind: str) -> int:
    if kind == "coin":
        return 0
    return 2


def _fmt_time(dt) -> str:
    today = now_kst().date()
    if dt.date() == today:
        return dt.strftime("%H:%M")
    return dt.strftime("%m/%d %H:%M")


def _to_float(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


# ━━ 네이버 배치 조회 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_QUOTE_LIKE_KEYS = {"closePrice", "nv", "tradePrice", "trade_price", "stck_prpr"}


def _find_quote_rows(data) -> list:
    """★★ result.areas[].datas[] 라는 최상위 껍데기 구조는 실측 문서 없이
    추측으로 짰다 - 이게 틀리면 rows 가 아예 비어서 위치 폴백(len 비교)도
    작동하지 않고 전부 "응답에 값이 없습니다"가 된다(실제로 겪은 문제).
    JSON 트리 어디에 있든 시세 필드(closePrice 등)를 가진 딕셔너리 리스트를
    찾아낸다 - 껍데기 구조를 몰라도 데이터를 찾을 수 있게 하는 게 목적이다.
    """
    def _walk(node):
        if isinstance(node, list):
            if node and isinstance(node[0], dict) and _QUOTE_LIKE_KEYS & set(node[0].keys()):
                return node
            for item in node:
                found = _walk(item)
                if found:
                    return found
        elif isinstance(node, dict):
            for v in node.values():
                found = _walk(v)
                if found:
                    return found
        return None

    return _walk(data) or []


_prev_close_cache: dict = {"day": "", "by_code": {}}


def _prev_closes(toss_client, codes: list) -> dict:
    """★★★ 토스 prices 응답에는 등락률·전일종가가 없다(공식 PriceResponse
    모델 확인). 등락률을 보여주려면 전일 종가가 반드시 필요하므로 일봉
    (candles)에서 받아 온다.

    ★ 전일 종가는 하루 동안 변하지 않으니 날짜 단위로 캐시한다 - 5초마다
    갱신되는 화면에서 종목마다 캔들을 매번 부르면 API 호출이 폭증한다.
    ★ 실패는 조용히 넘긴다 - 등락률을 못 보여줄 뿐, 현재가 자체는 살아야 한다.
    """
    today = now_kst().strftime("%Y-%m-%d")
    if _prev_close_cache["day"] != today:
        _prev_close_cache["day"] = today
        _prev_close_cache["by_code"] = {}
    cache = _prev_close_cache["by_code"]

    for code in codes:
        if code in cache:
            continue
        try:
            rows = toss_client.candles(code, "1d", 2)
            # ★ 마지막 봉은 오늘(진행 중)이므로, 그 앞 봉이 전일 종가다.
            if rows and len(rows) >= 2:
                prev = rows[-2].get("closePrice") or rows[-2].get("close")
                cache[code] = _to_float(prev)
            elif rows:
                cache[code] = _to_float(rows[-1].get("closePrice") or rows[-1].get("close"))
        except Exception:
            cache[code] = None
    return cache


def _parse_ts(value):
    """★ 토스 timestamp(ISO 문자열)를 datetime 으로. 실패하면 None -
    시각 하나 때문에 시세 전체가 죽으면 안 된다."""
    if not value:
        return None
    try:
        from datetime import datetime
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(now_kst().tzinfo)
    except Exception:
        return None


def _domestic_stocks_batch(toss_client, sess, items: list) -> dict:
    """★★ 국내 개별 종목 시세는 토스증권 API 를 1순위로, 실패하면(키 없음·
    오류·데이터 없음) 네이버 배치로 2순위 폴백한다. 지수는 토스 API 가
    지원하는지 확인되지 않아 그대로 네이버를 쓴다.

    ★★★ 실제로 겪은 버그와 그 정정 - 처음엔 토스 prices 응답에
    changeRate(등락률)가 온다고 보고 그걸 읽게 고쳤는데, 공식 문서
    (PriceResponse 모델)를 다시 확인하니 이 응답의 필드는
    symbol / timestamp / lastPrice / currency 넷뿐이고 등락률·전일종가는
    아예 없었다. changeRate 는 rankings(랭킹) 응답에만 있는 필드다.
    없는 필드를 읽으니 상승률이 계속 비어 있었고, 전일종가를 못 구해
    "시세는 바뀌는데 상승률이 안 맞는" 상태가 이어졌다.
    → 전일 종가는 일봉(candles)에서 받아와 직접 계산한다.
    """
    codes = [code for code, _label in items]
    if toss_client is not None:
        try:
            rows = toss_client.prices(codes)
            by_code = {}
            for r in rows or []:
                # ★★★ "'str' object has no attribute 'get'" 방지 - 시세
                # 응답에 딕셔너리가 아닌 항목이 섞여도 화면이 죽으면 안 된다.
                if not isinstance(r, dict):
                    continue
                symbol = str(r.get("symbol") or r.get("code") or "").zfill(6)
                if symbol.strip("0"):
                    by_code[symbol] = r
            if by_code:
                # ★ 전일 종가를 일봉에서 받아 둔다(종목당 1회, 실패해도 무시).
                #   등락률을 화면에 보여주려면 반드시 필요한데 prices 가
                #   안 주기 때문이다. 캐시가 있으면 재조회하지 않는다.
                prev_closes = _prev_closes(toss_client, list(by_code.keys()))
                out = {}
                for code, _label in items:
                    r = by_code.get(code)
                    if not r:
                        continue
                    # ★ 공식 필드명은 lastPrice 다 - 나머지는 방어적 폴백.
                    price = r.get("lastPrice") or r.get("price") or r.get("currentPrice")
                    if price is None:
                        continue
                    last = _to_float(price)
                    prev = prev_closes.get(code)
                    diff = (last - prev) if (last is not None and prev) else None
                    # ★ timestamp(데이터 시각)가 오면 그걸 쓴다 - 체결이 없어
                    #   null 일 수 있으므로 그때는 조회 시각으로 대신한다.
                    quoted = _parse_ts(r.get("timestamp")) or now_kst()
                    out[code] = {
                        "last": last,
                        "diff": diff if diff is not None else 0.0,
                        "prev_close": prev,
                        "name": r.get("name"), "symbol": code, "src": "토스증권",
                        "quoted_at": quoted,
                    }
                # ★ 요청한 종목 전부를 못 채우면(일부 심볼만 응답) 나머지는
                # 네이버로 채운다 - 부분 성공을 버리지 않는다.
                missing = [(c, lbl) for c, lbl in items if c not in out]
                if missing:
                    fallback = _naver_batch(sess, NAVER_DOMESTIC_STOCK, missing)
                    out.update(fallback)
                if out:
                    return out
        except Exception:
            pass  # ★ 조용히 네이버로 폴백한다 - 토스 실패가 화면을 막으면 안 된다.

    return _naver_batch(sess, NAVER_DOMESTIC_STOCK, items)


def _naver_batch(sess, url_template: str, items: list) -> dict:
    """★★ 콤마로 여러 개를 한 번에 받는다 - 종목마다 부르지 않는다.

    ★ 응답에서 "이 값이 어느 코드 것인지"를 알려주는 필드명은 정확한 실측
    문서가 없어 추측(itemCode/cd/code)으로 짰다. 그 필드명이 하나라도
    틀리면 매칭이 통째로 실패해 전부 "응답에 값이 없습니다"로 보인다
    (실제로 겪은 문제 - "지수가 안 나와"). 필드명 매칭이 실패하면, 요청한
    순서와 응답 순서가 같다고 보고 위치로 다시 맞춰본다 - 필드명을 몰라도
    데이터가 나오게 하는 게 목적이다.
    """
    codes = [code for code, _label in items]
    url = url_template.format(codes=",".join(codes))
    out = {}
    try:
        resp = sess.get(url, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        rows = []
        for area in (data.get("result", {}) or {}).get("areas", []):
            rows.extend(area.get("datas", []))
        if not rows:
            # ★ 가정한 껍데기 구조(result.areas[].datas[])가 안 맞았다 -
            # 트리 전체에서 시세처럼 보이는 리스트를 다시 찾는다.
            rows = _find_quote_rows(data)

        by_code = {}
        for r in rows:
            key = r.get("itemCode") or r.get("cd") or r.get("code") or r.get("reutersCode")
            if key:
                by_code[key] = r

        matched_by_key = sum(1 for code in codes if code in by_code)
        # ★ 필드명 매칭이 거의 다 실패했는데 응답 개수는 요청 개수와 같다면,
        # 순서가 같다고 보고 위치로 다시 짝짓는다.
        use_positional = matched_by_key == 0 and len(rows) == len(codes)

        for i, (code, _label) in enumerate(items):
            r = by_code.get(code) or (rows[i] if use_positional and i < len(rows) else None)
            if not r:
                out[code] = {"error": "응답에 값이 없습니다."}
                continue
            last = r.get("closePrice") or r.get("nv")
            diff = abs(_to_float(r.get("compareToPreviousClosePrice") or r.get("cv") or 0))
            cmp_prev = r.get("compareToPreviousPrice") or {}
            fluct = r.get("fluctuationsType") or {}
            # ★ 실측 문서상 방향은 compareToPreviousPrice.code 에 "2"(상승)/
            # "5"(하락)로 들어간다. .name 필드도 혹시 몰라 같이 본다.
            direction = cmp_prev.get("code") or cmp_prev.get("name") or fluct.get("code") or fluct.get("name")
            sign = _sign(direction)
            out[code] = {
                "last": _to_float(last), "diff": sign * diff,
                "name": r.get("stockName") or r.get("nm"), "symbol": code,
            }
    except Exception as exc:
        msg = _short_error(exc, f"네이버 일괄조회({url_template})")
        for code, _label in items:
            out[code] = {"error": msg}
    return out


def _naver_fx(sess, code: str) -> dict:
    """환율 응답은 {"exchangeInfo": {...}} 로 한 겹 감싸여 있고, 등락폭 필드명이
    compareToPreviousClosePrice 가 아니라 fluctuations 다.
    """
    try:
        resp = sess.get(NAVER_FX.format(code=code), timeout=8)
        resp.raise_for_status()
        info = resp.json().get("exchangeInfo", {})
        last = _to_float(info.get("closePrice"))
        diff_abs = abs(_to_float(info.get("fluctuations")))
        direction = (info.get("fluctuationsType") or {}).get("name")
        sign = _sign(direction)
        return {"last": last, "diff": sign * diff_abs}
    except Exception as exc:
        return {"error": _short_error(exc, f"네이버 환율({code})")}


# ━━ 야후 파이낸스 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _yahoo_simple(sess, code: str) -> dict:
    """선물·해외지수·달러인덱스 - 구간 구분 없이 지금 값 하나만 본다."""
    try:
        resp = sess.get(YAHOO_CHART.format(code=code), params={"range": "1d", "interval": "1d"}, timeout=8)
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        meta = result["meta"]
        last = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        market_state = meta.get("marketState", "")
        diff = (last - prev) if (last is not None and prev) else None
        return {"last": last, "diff": diff, "prev_close": prev, "market_state": market_state}
    except Exception as exc:
        return {"error": _short_error(exc, f"야후 단순조회({code})")}


def _session_from_ny_time() -> str:
    """★ market_state 가 애매할 때의 안전한 폴백 - 실제 뉴욕 시각을 계산해
    지금이 프리·정규·애프터 중 어디인지 사실 기반으로 판정한다. 정규장
    시간 외(자정~04:00, 20:00~24:00, 주말)에는 "post"(가장 최근에 확정된
    값)로 본다 - active_val 계산이 pre/regular/post 세 값만 다루므로,
    새 카테고리를 만드는 대신 가장 자연스러운 기존 값에 맞춘다.
    """
    from datetime import datetime, time as dtime, timedelta, timezone
    try:
        from zoneinfo import ZoneInfo
        now_ny = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # ★ overseas_engine.py 에서 겪은 것과 같은 문제(tzdata 없음) 대비 -
        # 서머타임을 직접 계산해 UTC 오프셋을 적용한다.
        now_utc = datetime.now(timezone.utc)
        year = now_utc.year

        def _nth_sunday(y, month, n):
            d = datetime(y, month, 1, tzinfo=timezone.utc)
            offset = (6 - d.weekday()) % 7
            return d + timedelta(days=offset + 7 * (n - 1))

        dst_start = _nth_sunday(year, 3, 2) + timedelta(hours=7)
        dst_end = _nth_sunday(year, 11, 1) + timedelta(hours=6)
        offset = -4 if dst_start <= now_utc < dst_end else -5
        now_ny = now_utc + timedelta(hours=offset)

    if now_ny.weekday() >= 5:  # 주말
        return "post"
    t = now_ny.time()
    if dtime(4, 0) <= t < dtime(9, 30):
        return "pre"
    if dtime(9, 30) <= t < dtime(16, 0):
        return "regular"
    return "post"


def _yahoo_session_quote(sess, code: str) -> dict:
    """미국 종목은 하루가 세 구간(프리·정규·애프터)이다.
    ★★ 큰 숫자 하나만 보여주면 그게 어느 구간 값인지 알 수 없다.
    """
    try:
        resp = sess.get(
            YAHOO_CHART.format(code=code),
            params={"range": "1d", "interval": "5m", "includePrePost": "true"},
            timeout=8,
        )
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        meta = result["meta"]
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")

        timestamps = result.get("timestamp") or []
        quote0 = (result.get("indicators", {}).get("quote") or [{}])[0]
        closes = quote0.get("close") or []

        periods = meta.get("tradingPeriods") or {}

        def _range_of(name):
            try:
                block = periods.get(name)
                first = block[0][0]
                return first["start"], first["end"]
            except Exception:
                return None

        def _last_close_in(rng):
            if not rng:
                return None
            start, end = rng
            last = None
            for ts, c in zip(timestamps, closes):
                if c is not None and start <= ts <= end:
                    last = c
            return last

        pre_val = _last_close_in(_range_of("pre"))
        post_val = _last_close_in(_range_of("post"))
        # 정규장 값은 meta.regularMarketPrice 를 우선한다 - 캔들보다 확정값이다.
        reg_val = meta.get("regularMarketPrice")
        if reg_val is None:
            reg_val = _last_close_in(_range_of("regular"))

        market_state = meta.get("marketState", "")
        # ★★★ 실제로 겪은 버그 - market_state 가 "PRE"·"REGULAR" 중 하나로
        # 명확히 안 오는 경우(빈 문자열 등, 프리마켓 초반처럼 야후 응답이
        # 아직 안정되지 않았을 때 실제로 벌어진다), 예전엔 무조건
        # "regular"(정규장)로 잘못 폴백했다 - 그래서 실제로는 프리마켓인데
        # "정규장"으로 표시됐다. POST/POSTPOST 도 명시적으로 처리하고,
        # 그래도 애매하면 실제 뉴욕 시각을 계산해서 판정한다(추정이 아니라
        # 사실 기반 폴백).
        if market_state in ("PRE", "PREPRE"):
            active = "pre"
        elif market_state == "REGULAR":
            active = "regular"
        elif market_state in ("POST", "POSTPOST"):
            active = "post"
        elif market_state == "CLOSED":
            active = _session_from_ny_time()
        else:
            active = _session_from_ny_time()

        active_val = {"pre": pre_val, "regular": reg_val, "post": post_val}.get(active)
        diff = (active_val - prev_close) if (active_val is not None and prev_close) else None

        return {
            "last": active_val, "diff": diff, "prev_close": prev_close,
            "sessions": {"prev": prev_close, "pre": pre_val, "regular": reg_val, "post": post_val, "active": active},
            "market_state": market_state,
        }
    except Exception as exc:
        return {"error": _short_error(exc, f"야후 세션조회({code})")}


def _fetch_yahoo_session_group(sess, items: list, now) -> list:
    """★ ThreadPoolExecutor(max_workers=8) 로 동시에 던져 순차 15초를 2~3초로 줄인다."""
    results: dict = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_yahoo_session_quote, sess, code): code for code, _label in items}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                results[code] = fut.result()
            except Exception as exc:
                results[code] = {"error": _short_error(exc, f"야후 세션조회({code})")}

    rows = []
    for code, label in items:  # 완료 순서가 아니라 정의 순서대로 카드가 나오게 정렬한다.
        data = results.get(code, {"error": "no data"})
        data.setdefault("symbol", code)  # ★ 개별 종목은 티커를 화면에 함께 보여준다.
        rows.append(_row(label, "yahoo_session", "야후", data, now, True))
    return rows


# ━━ 코인 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_COIN_SYMBOL_USDT = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "XRP": "XRPUSDT"}


def _fetch_coins(sess, items: list) -> dict:
    """★★ 빗썸을 1순위로 쓴다(사용자가 실제 거래하는 거래소와 화면 시세가
    같아야 헷갈리지 않는다). 빗썸 시세는 공개 API라 키가 없어도 조회된다.
    실패하면 업비트, 그마저 실패하면 바이낸스(원화환산)로 내려간다.
    """
    codes = [c for c, _ in items]

    # ── 1순위: 빗썸 ──
    try:
        from daytrader.bithumb_api import BithumbClient
        client = BithumbClient()
        markets = [f"KRW-{c}" for c in codes]
        rows = client.ticker(markets)
        by_code = {}
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            market = r.get("market", "")
            code = market.replace("KRW-", "")
            last = r.get("trade_price")
            if last is not None:
                by_code[code] = {"last": last, "diff": r.get("signed_change_price"), "src": "빗썸"}
        if by_code and len(by_code) == len(codes):
            return by_code
    except Exception:
        by_code = {}

    # ── 2순위: 업비트 ──
    try:
        markets = ",".join(f"KRW-{c}" for c in codes)
        resp = sess.get(UPBIT_TICKER, params={"markets": markets}, timeout=8)
        resp.raise_for_status()
        rows = resp.json()
        for r in rows:
            market = r.get("market", "")
            code = market.replace("KRW-", "")
            if code not in by_code:
                by_code[code] = {"last": r.get("trade_price"), "diff": r.get("signed_change_price"), "src": "업비트"}
        if len(by_code) == len(codes):
            return by_code
    except Exception:
        pass

    # ── 3순위: 바이낸스(USDT, 원화환산) - 빗썸·업비트 둘 다 막혔을 때만 ──
    missing = [c for c in codes if c not in by_code]
    if missing:
        usdkrw = _naver_fx(sess, "FX_USDKRW").get("last")
        for c in missing:
            symbol = _COIN_SYMBOL_USDT.get(c)
            if not symbol:
                by_code[c] = {"error": "심볼을 알 수 없습니다.", "src": "바이낸스·원화환산"}
                continue
            try:
                r = sess.get(BINANCE_TICKER, params={"symbol": symbol}, timeout=8)
                r.raise_for_status()
                data = r.json()
                last_usdt = _to_float(data.get("lastPrice"))
                change_usdt = _to_float(data.get("priceChange"))
                if usdkrw:
                    by_code[c] = {"last": last_usdt * usdkrw, "diff": change_usdt * usdkrw, "src": "바이낸스·원화환산"}
                else:
                    by_code[c] = {"last": last_usdt, "diff": change_usdt, "src": "바이낸스(USDT)"}
            except Exception as exc:
                by_code[c] = {"error": _short_error(exc, f"바이낸스 원화환산({c})"), "src": "바이낸스·원화환산"}

    for c in codes:
        by_code.setdefault(c, {"error": "응답에 값이 없습니다.", "src": "빗썸"})
    return by_code


# ━━ 카드 조립 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _status_of(market_state: str) -> str:
    """marketStatus 가 OPEN·REGULAR·PRE·POST·24H 면 '현재가', 아니면 '종료가'.
    ★ 야후가 marketState 를 안 주면 '현재가'로 본다 - CME 선물은 거의 24시간 돈다.
    """
    if not market_state:
        return "현재가"
    if market_state.upper() in ("OPEN", "REGULAR", "PRE", "POST", "24H", "PREPRE"):
        return "현재가"
    return "종료가"


def _row(label: str, kind: str, src: str, data: dict, now, stale_source: bool) -> dict:
    if "error" in data:
        return {"label": label, "last": None, "diff": None, "pct": None, "dp": _dp_for(kind),
                "status": "-", "kind": kind, "src": src, "error": data["error"],
                "symbol": data.get("symbol")}

    last = data.get("last")
    diff = data.get("diff")
    prev = data.get("prev_close")
    if prev is None and last is not None and diff is not None:
        prev = last - diff
    pct = (diff / prev) if (diff is not None and prev) else None

    row = {
        "label": label, "last": last, "diff": diff, "pct": pct, "dp": _dp_for(kind),
        "status": _status_of(data.get("market_state", "")), "kind": kind,
        "src": data.get("src", src), "error": None,
        # ★ 주식(개별 종목) 그룹은 코드가 있으면 화면에서 라벨과 함께 보여준다.
        # 지수·환율·코인은 symbol 이 없어(None) 화면에서 자연히 생략된다.
        "symbol": data.get("symbol"),
    }
    if "sessions" in data:
        row["sessions"] = data["sessions"]

    # ★★★ 실제로 겪은 문제 - 시각을 "3분 넘게 묵었을 때만" 적었더니,
    # 실시간 소스(토스)는 시각이 영영 안 나와서 화면이 멈춘 것처럼
    # 보였다("가격은 바뀌는데 시간이 안 바뀐다"). 소스가 알려준 조회
    # 시각이 있으면 그걸 쓰고, 없으면 기존 규칙(묵었을 때만)을 따른다.
    quoted_at = data.get("quoted_at")
    if quoted_at is not None:
        # ★ 실시간 조회 시각은 초까지 보여준다 - 분 단위만 쓰면 몇 초마다
        # 갱신해도 표시가 그대로라 "시간이 안 바뀐다"고 느끼게 된다.
        row["at"] = quoted_at.strftime("%H:%M:%S")
    elif stale_source and "sessions" not in data:
        row["at"] = _fmt_time(now)
    return row


# ━━ 스냅샷 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_cache: dict = {"at": 0.0, "data": None}


def snapshot(force: bool = False, ttl: float | None = None, toss_client=None) -> dict:
    """★ 화면이 5초마다 물어보는데 서버가 20초 캐시를 물고 있으면 값이 안
    바뀌어 고장으로 보인다 - 캐시 수명을 사용자 선택(ttl)에 맞춘다.
    """
    ttl = max(_MIN_TTL, ttl if ttl is not None else _MIN_TTL)
    now_mono = time.monotonic()
    if not force and _cache["data"] is not None and (now_mono - _cache["at"]) < ttl:
        return _cache["data"]

    sess = netutil.make_session()
    now = now_kst()
    errors: list = []
    groups_out: list = []

    def _add_group(name: str, rows: list) -> None:
        # ★ 소스 함수들은 저마다 실패를 행 단위 error 로 삼킨다. 그래서 밖의
        # try/except 로는 "소스가 통째로 실패했다"를 못 잡는다 - 여기서
        # 직접 세어 errors[] 에 한 줄 남긴다.
        if rows and all(r.get("error") for r in rows):
            errors.append(f"{name}: 전부 조회 실패 ({rows[0]['error']})")
        groups_out.append({"group": name, "rows": rows})

    # ── 국내 지수 (네이버 배치 1회 - 토스가 지수를 지원하는지 불확실해 그대로 둔다) ──
    try:
        idx_data = _naver_batch(sess, NAVER_DOMESTIC_INDEX, DOMESTIC_INDEX_ITEMS)
        rows = [_row(label, "domestic_index", "네이버", idx_data.get(code, {"error": "no data"}), now, False)
                for code, label in DOMESTIC_INDEX_ITEMS]
    except Exception as exc:
        msg = _short_error(exc, "국내 지수 조회")
        errors.append(f"국내 지수 조회 실패: {msg}")
        rows = [_row(label, "domestic_index", "네이버", {"error": msg}, now, False)
                for code, label in DOMESTIC_INDEX_ITEMS]

    # ── 국내 개별 종목(코스피 상위5·코스닥 상위3) - 토스증권 1순위, 네이버 2순위 ──
    # ★ 삼성전자·SK하이닉스는 원래 "AI반도체" 그룹에 있었는데 여기로 옮겼다 -
    # 코스피 상위 5개 안에 자연히 포함된다.
    stock_items = KOSPI_TOP5_ITEMS + KOSDAQ_TOP3_ITEMS
    try:
        stock_data = _domestic_stocks_batch(toss_client, sess, stock_items)
        rows += [_row(label, "domestic_stock", stock_data.get(code, {}).get("src", "네이버"),
                       stock_data.get(code, {"error": "no data"}), now, False)
                 for code, label in stock_items]
    except Exception as exc:
        msg = _short_error(exc, "국내 종목 조회")
        errors.append(f"국내 종목 조회 실패: {msg}")
        rows += [_row(label, "domestic_stock", "네이버", {"error": msg}, now, False)
                  for code, label in stock_items]
    _add_group("국내", rows)

    # ── 선물 (야후 단순조회) ──
    rows = [_row(label, "yahoo", "야후", _yahoo_simple(sess, code), now, True) for code, label in FUTURES_ITEMS]
    _add_group("선물", rows)

    # ── 해외 지수 (네이버 배치 1회) ──
    try:
        world_data = _naver_batch(sess, NAVER_WORLD_INDEX, WORLD_INDEX_ITEMS)
        rows = [_row(label, "world_index", "네이버", world_data.get(code, {"error": "no data"}), now, True)
                for code, label in WORLD_INDEX_ITEMS]
    except Exception as exc:
        msg = _short_error(exc, "해외 지수 조회")
        errors.append(f"해외 지수 조회 실패: {msg}")
        rows = [_row(label, "world_index", "네이버", {"error": msg}, now, True) for code, label in WORLD_INDEX_ITEMS]
    _add_group("해외", rows)

    # ── 미국 대형주 (야후 세션조회, 동시 호출) ──
    rows = _fetch_yahoo_session_group(sess, US_LARGECAP_ITEMS, now)
    _add_group("미국 대형주", rows)

    # ── 미국-HBM (메모리 반도체 관련 - 예전 "AI반도체"에서 국내 종목을 빼고 개편) ──
    rows = _fetch_yahoo_session_group(sess, US_HBM_ITEMS, now)
    _add_group("미국-HBM", rows)

    # ── 환율 (네이버 3개 + 달러인덱스 1개) ──
    rows = [_row(label, "fx", "네이버", _naver_fx(sess, code), now, False) for code, label in FX_NAVER_ITEMS]
    rows += [_row(label, "yahoo", "야후", _yahoo_simple(sess, code), now, True) for code, label in FX_YAHOO_ITEMS]
    _add_group("환율", rows)

    # ── 코인 ──
    try:
        coin_data = _fetch_coins(sess, COIN_ITEMS)
        rows = [_row(label, "coin", coin_data.get(code, {}).get("src", "업비트"),
                      coin_data.get(code, {"error": "no data"}), now, False)
                for code, label in COIN_ITEMS]
    except Exception as exc:
        msg = _short_error(exc, "코인 조회")
        errors.append(f"코인 조회 실패: {msg}")
        rows = [_row(label, "coin", "업비트", {"error": msg}, now, False) for code, label in COIN_ITEMS]
    _add_group("암호화폐", rows)

    # ★★★ "야간시장(코스피200 야간선물 등) 시세를 받아올 수 있는지
    # 검토하고 못 가져오면 항목을 삭제해" - HLKR 이라는 참고용 사이트로
    # 한 번 시도했었지만, 그 사이트 자체가 "한국거래소(KRX) 시장데이터를
    # 수신·이용하지 않으며, 표시되는 가격은 Hyperliquid 등 해외 파생상품
    # 거래소가 만든 합성 perp 체결가"라고 명시하고 있어 애초에 실제
    # 야간선물 시세가 아니었고, 공식 API도 없어 안정적으로 받아올 방법이
    # 없었다("last"가 항상 None인 링크 안내 카드 하나뿐이었다). 실제로
    # 못 가져오는 항목이므로 삭제한다 - 미국 지수선물(위 "선물" 그룹,
    # NQ=F/ES=F/YM=F, 야후 실시간)은 실제로 밤사이 데이터를 주므로 그대로 둔다.

    total = sum(len(g["rows"]) for g in groups_out)
    ok = total > 0 and any(r.get("error") is None for g in groups_out for r in g["rows"])

    data = {
        "at": now.isoformat(), "ok": ok, "total": total, "ttl": ttl,
        "refresh_choices": REFRESH_CHOICES, "default_refresh": DEFAULT_REFRESH,
        "groups": groups_out, "errors": errors,
        "note": "이 값들은 매매 판단에 개입하지 않습니다. 보기만 하는 화면입니다.",
    }
    _cache["at"] = now_mono
    _cache["data"] = data
    return data
