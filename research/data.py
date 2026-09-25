"""1분봉 시세 로더 4종.

여기서 만드는 봉은 전부 daytrader.playbook.Bar 다 - 새로 자료구조를 만들지 않는다.
그래야 backtest.py 가 엔진과 똑같은 Bar 를 playbook.py 에 그대로 넘길 수 있다.

  (a) load_csv        - 미리 받아 둔 CSV(symbol, ts, open, high, low, close, volume)
  (b) fetch_yahoo      - 야후 파이낸스 차트 API(.KS/.KQ), 최근 30일치를 7일 단위로 나눠 받는다
  (c) fetch_toss       - 이 프로그램이 실전에서 쓰는 TossClient.candles() 그대로
  (d) generate_synthetic - daytrader/simulator.py 의 SimClient(테마 기반 시나리오) 재사용

★ 모든 타임스탬프는 KST(+09:00) 이고, 정규장 시간(09:00~15:30) 밖의 봉은 버린다.
"""

from __future__ import annotations

import csv
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from daytrader.playbook import Bar
from daytrader.timeutil import KST, iso, now_kst

RESEARCH_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(RESEARCH_DIR, "cache")

MARKET_OPEN = "09:00"
MARKET_CLOSE = "15:30"

# ★ 사용자 요청("themes.yaml + 대형주 몇 개") - 코디네이터가 지정한 5종목.
#   005930·000660·035420·035720 은 이미 themes.yaml 에 있지만 중복으로 넣어도
#   default_universe() 가 set 으로 합쳐 문제 없다(있으면 그냥 무시된다).
EXTRA_LARGE_CAPS: Dict[str, str] = {
    "005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER",
    "005380": "현대차", "035720": "카카오",
}

CSV_COLUMNS = ("symbol", "ts", "open", "high", "low", "close", "volume")


def _within_session(hhmm: str) -> bool:
    return MARKET_OPEN <= hhmm <= MARKET_CLOSE


def _sort_all(bars_by_symbol: Dict[str, List[Bar]]) -> Dict[str, List[Bar]]:
    for bars in bars_by_symbol.values():
        bars.sort(key=lambda b: b.ts)
    return bars_by_symbol


def default_universe(cfg) -> Dict[str, str]:
    """themes.yaml 의 전 종목 + EXTRA_LARGE_CAPS. {종목코드: 종목명}."""
    from daytrader.simulator import load_theme_names

    names = dict(load_theme_names(cfg.themes_file))
    for code, name in EXTRA_LARGE_CAPS.items():
        names.setdefault(code, name)
    return names


def symbol_theme_map(cfg) -> Dict[str, str]:
    """{종목코드: 테마명}. themes.yaml 에 없는 종목(대형주 추가분 등)은 "관심종목"."""
    import yaml

    with open(cfg.themes_file, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out: Dict[str, str] = {}
    for theme, codes in (raw.get("themes") or {}).items():
        for code in codes:
            out[str(code)] = theme
    for code in EXTRA_LARGE_CAPS:
        out.setdefault(code, "관심종목")
    return out


# ── (a) CSV ──────────────────────────────────────────────────────────────

def load_csv(path: str) -> Dict[str, List[Bar]]:
    """컬럼: symbol, ts(ISO, KST 오프셋 포함), open, high, low, close, volume.
    예) 005930,2026-08-01T09:01:00+09:00,71000,71100,70900,71000,12345
    정규장(09:00~15:30) 밖의 행은 버린다. ts 에 타임존이 없으면 KST 로 간주한다.
    """
    out: Dict[str, List[Bar]] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = set(CSV_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"CSV 에 필요한 컬럼이 없습니다: {sorted(missing)} (있는 컬럼: {reader.fieldnames})"
            )
        for row in reader:
            ts = (row.get("ts") or "").strip()
            if len(ts) < 16:
                continue
            if "+" not in ts[10:] and "Z" not in ts:
                ts = ts + "+09:00"
            hhmm = ts[11:16]
            if not _within_session(hhmm):
                continue
            sym = (row.get("symbol") or "").strip()
            if sym.isdigit():
                sym = sym.zfill(6)
            try:
                bar = Bar(
                    ts=ts, open=float(row["open"]), high=float(row["high"]),
                    low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"]),
                )
            except (TypeError, ValueError):
                continue
            out.setdefault(sym, []).append(bar)
    return _sort_all(out)


def save_csv(path: str, bars_by_symbol: Dict[str, List[Bar]]) -> None:
    """load_csv 와 같은 스키마로 저장한다(야후·토스 캐시가 이 함수를 쓴다)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for sym, bars in bars_by_symbol.items():
            for b in bars:
                w.writerow([sym, b.ts, b.open, b.high, b.low, b.close, b.volume])


# ── (b) 야후 파이낸스 ────────────────────────────────────────────────────

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (research harness; +https://claude.ai/code)"}


def _yahoo_ticker_candidates(symbol: str) -> List[str]:
    """코스피(.KS)를 먼저 시도하고, 응답이 비면 코스닥(.KQ)으로 넘어간다.
    실제로 어느 시장인지는 이 파일이 몰라도 된다 - 결과가 비었다는 사실 자체가 신호다."""
    return [f"{symbol}.KS", f"{symbol}.KQ"]


def _yahoo_chunk(ticker: str, period1: int, period2: int, timeout: float = 12.0) -> dict:
    import requests

    params = {"interval": "1m", "period1": period1, "period2": period2, "includePrePost": "false"}
    r = requests.get(
        YAHOO_CHART_URL.format(ticker=ticker), params=params, timeout=timeout, headers=_YAHOO_HEADERS,
    )
    r.raise_for_status()
    return r.json()


def _yahoo_rows_from_chunk(data: dict) -> List[Bar]:
    chart = data.get("chart") or {}
    result = chart.get("result")
    if not result:
        err = (chart.get("error") or {}).get("description") or chart.get("error")
        raise RuntimeError(f"야후 응답에 result 가 없습니다: {err}")
    res = result[0]
    ts_list = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    vols = quote.get("volume") or []

    out: List[Bar] = []
    for i, t in enumerate(ts_list):
        try:
            o, h, l, c, v = opens[i], highs[i], lows[i], closes[i], vols[i]
        except IndexError:
            continue
        # ★★★ 실제로 겪을 수 있는 문제 - 야후는 거래가 없던 분(호가만 있던 분)의
        # OHLCV 필드를 통째로 null 로 준다. Bar.from_api() 와 같은 원칙으로,
        # 값이 없는 봉은 통째로 버린다(0으로 채우면 "거래량 0에 가격 0"이라는
        # 거짓 사실이 되어 거래량 급증·VWAP 계산을 오염시킨다).
        if o is None or h is None or l is None or c is None or v is None:
            continue
        try:
            dt = datetime.fromtimestamp(int(t), tz=KST)
        except (TypeError, ValueError, OSError):
            continue
        ts_iso = iso(dt)
        hhmm = ts_iso[11:16]
        if not _within_session(hhmm):
            continue
        out.append(Bar(ts=ts_iso, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v)))
    return out


def _fetch_yahoo_symbol(symbol: str, days: int) -> Tuple[List[Bar], str]:
    """★★★ 실제로 겪은 문제(실측) - 야후 차트 API 는 period1/period2 를 엄격히
    지키지 않는다(7일 요청에 그보다 훨씬 넓은 범위가 섞여 온 적이 있다). 그래서
    겹치게(1일씩 겹쳐서) 7일 단위로 여러 번 불러 timestamp 기준으로 중복 제거한다.
    분봉은 실측상 하루 09:00~15:00 까지만 오고 15:00~15:30(동시호가 포함) 구간은
    빠질 수 있다 - 그 경우 이 종목은 장 막판 강제청산 타이밍을 정확히 재현하지
    못한다(README·report.md 의 한계로 남긴다).

    ★★★ 실제로 겪은 버그 - 1분봉은 대략 한 달을 넘어가는 과거 구간을 요청하면
    그 청크만 422 로 거부된다(야후의 1분봉 보존 기간 한계로 보인다). 처음엔 청크
    하나가 실패하면 그 종목 전체(이미 받아 둔 최근 구간까지)를 통째로 버렸다 -
    "최근 N일"을 달라고 했는데 N이 보존 기간을 넘으면 아무것도 못 받는 꼴이었다.
    이제 청크 하나가 실패해도 그 구간만 건너뛰고(가장 오래된 구간부터 빠지는 게
    자연스럽다) 나머지 구간은 계속 받는다 - 요청한 기간의 일부만 못 받는 것으로
    끝난다.
    """
    now = int(time.time())
    start = now - days * 24 * 3600
    chunk = 7 * 24 * 3600
    step = 6 * 24 * 3600  # 1일씩 겹쳐서 요청 - 야후가 경계를 흘려도 빠지는 날이 없게

    last_exc: Exception | None = None
    for ticker in _yahoo_ticker_candidates(symbol):
        by_ts: Dict[str, Bar] = {}
        cursor = start
        got_any = False
        ticker_exc: Exception | None = None
        while cursor < now:
            p2 = min(cursor + chunk, now)
            try:
                data = _yahoo_chunk(ticker, cursor, p2)
                rows = _yahoo_rows_from_chunk(data)
            except Exception as exc:  # noqa: BLE001 - 이 구간만 건너뛰고 계속한다
                ticker_exc = exc
                cursor += step
                continue
            if rows:
                got_any = True
            for b in rows:
                by_ts[b.ts] = b
            cursor += step
        if got_any:
            bars = sorted(by_ts.values(), key=lambda b: b.ts)
            return bars, ticker
        last_exc = ticker_exc or last_exc
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("야후에서 봉을 하나도 받지 못했습니다(빈 응답).")


def fetch_yahoo(
    symbols: List[str], days: int = 30, cache_dir: str = CACHE_DIR,
) -> Tuple[Dict[str, List[Bar]], List[str]]:
    """야후 파이낸스에서 최근 days 일치 1분봉을 받는다. 실패한 종목은 캐시(research/cache/*.csv)로
    대신하고, 그마저 없으면 건너뛴다 - 네트워크가 막힌 환경(이 샌드박스의 기본 상태)에서도
    명확한 이유와 함께 조용히 죽지 않게 하기 위해서다."""
    os.makedirs(cache_dir, exist_ok=True)
    out: Dict[str, List[Bar]] = {}
    errors: List[str] = []
    for symbol in symbols:
        cache_path = os.path.join(cache_dir, f"{symbol}.csv")
        try:
            bars, ticker = _fetch_yahoo_symbol(symbol, days)
            out[symbol] = bars
            save_csv(cache_path, {symbol: bars})
        except Exception as exc:  # noqa: BLE001
            cached = load_csv(cache_path).get(symbol) if os.path.exists(cache_path) else None
            if cached:
                out[symbol] = cached
                errors.append(f"{symbol}: 야후 조회 실패({exc}) - 캐시된 {len(cached)}봉을 대신 씁니다.")
            else:
                errors.append(
                    f"{symbol}: 야후 조회 실패({exc}) - 이 환경에서 Yahoo Finance(query1/2.finance.yahoo.com) "
                    "접근이 막혀 있을 수 있습니다. 네트워크가 열린 세션에서 다시 시도하거나 --source csv 로 "
                    "미리 받아 둔 시세를 쓰세요."
                )
    return _sort_all(out), errors


# ── (c) Toss Open API ───────────────────────────────────────────────────

_TOSS_BATCH = 200  # daytrader/technique_backtest.py 의 _ROUTER_BATCH 와 같은 한도(3-4 주석 참고).


def fetch_toss(cfg, symbols: List[str], days: int = 30) -> Tuple[Dict[str, List[Bar]], List[str]]:
    """daytrader.tossapi.TossClient.candles(symbol, interval, count, before=) 를 그대로 쓴다.
    한 번에 최대 200개까지만 주므로 before 로 과거 방향으로 페이지를 넘긴다
    (daytrader/technique_backtest.py 의 _fetch_router_bars 와 같은 방식).
    클라이언트 ID·시크릿은 config.yaml 의 client_id/client_secret 또는 환경변수
    TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 에서 읽는다 - 이 샌드박스에는 없으므로(Toss API 는
    네트워크 정책상 계속 막혀 있다) 여기서는 "키 없음"으로 실패하는 것이 정상이다."""
    client_id = getattr(cfg, "client_id", "") or os.environ.get("TOSS_CLIENT_ID", "")
    client_secret = getattr(cfg, "client_secret", "") or os.environ.get("TOSS_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Toss Open API 키가 없습니다. config.yaml 의 client_id/client_secret 을 채우거나 "
            "환경변수 TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 을 설정한 뒤(로컬 세션에서) 다시 실행하세요. "
            "이 샌드박스는 네트워크 정책상 Toss API 자체가 막혀 있어 --source toss 는 여기서 항상 실패합니다."
        )
    from daytrader.tossapi import TossClient

    client = TossClient(client_id, client_secret, account_seq=getattr(cfg, "account_seq", None))
    target = min(2000, days * 390 + 60)
    out: Dict[str, List[Bar]] = {}
    errors: List[str] = []
    for symbol in symbols:
        rows: List[dict] = []
        before = None
        for _ in range(target // _TOSS_BATCH + 2):
            try:
                page = (
                    client.candles(symbol, "1m", _TOSS_BATCH, before=before)
                    if before else client.candles(symbol, "1m", _TOSS_BATCH)
                )
            except TypeError:
                break  # before 를 못 받는 클라이언트 - 첫 페이지로 만족한다.
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{symbol}: {exc}")
                break
            page = [r for r in (page or []) if isinstance(r, dict)]
            if not page:
                break
            rows = page + rows
            oldest = page[0].get("timestamp")
            if not oldest or len(rows) >= target or len(page) < _TOSS_BATCH:
                break
            before = oldest
        bars = [Bar.from_api(r) for r in rows]
        bars = [b for b in bars if _within_session((b.ts or "")[11:16])]
        bars.sort(key=lambda b: b.ts or "")
        if bars:
            out[symbol] = bars[-target:]
    return out, errors


# ── (d) 합성 시세(오프라인) ──────────────────────────────────────────────

def generate_synthetic(
    cfg, symbols: List[str] | None = None, days: int = 30, scenario_by_day: Dict[str, str] | None = None,
) -> Dict[str, List[Bar]]:
    """daytrader/simulator.py 의 SimClient 를 그대로 재사용해 영업일 days 일치 1분봉을 만든다.
    scenario_by_day 로 날짜별 시나리오(normal/strong_theme/choppy/crash)를 섞을 수 있다(없으면
    cfg.simulation.scenario 하나로 전체를 돈다) - 세션·기법 조합이 장세에 따라 어떻게 달라지는지
    보려면 여러 장세가 섞인 한 달이 있는 게 자연스럽기 때문이다.

    ★ SimClient._full_day_bars() 는 "그 날 09:00~15:30 전체" 봉을 만드는 내부 메서드다(공개
    메서드인 candles()/prices() 는 "지금 시각까지만" 잘라 주는데, 여기서는 과거 하루 전체가
    필요하므로 일부러 내부 메서드를 직접 부른다 - 시뮬레이터를 새로 베끼지 않기 위해서다."""
    import copy

    from daytrader.simulator import SESSION_END, SESSION_START, SimClient

    if symbols is None:
        symbols = list(default_universe(cfg).keys())

    end = now_kst().date() - timedelta(days=1)
    business_days: List[str] = []
    d = end
    while len(business_days) < days:
        if d.weekday() < 5:
            business_days.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    business_days.reverse()

    out: Dict[str, List[Bar]] = {sym: [] for sym in symbols}
    scenario_by_day = scenario_by_day or {}
    default_scn = cfg.simulation.scenario
    _client_cache: Dict[str, SimClient] = {}
    for day in business_days:
        scn = scenario_by_day.get(day, default_scn)
        client = _client_cache.get(scn)
        if client is None:
            day_cfg = cfg
            if scn != cfg.simulation.scenario:
                day_cfg = copy.copy(cfg)
                day_cfg.simulation = copy.copy(cfg.simulation)
                day_cfg.simulation.scenario = scn
            client = SimClient(day_cfg, themes_path=cfg.themes_file)
            _client_cache[scn] = client
        for sym in symbols:
            rows = client._full_day_bars(sym, day)  # noqa: SLF001 - 의도적(위 설명 참고)
            out[sym].extend(Bar.from_api(r) for r in rows)
    _ = (SESSION_START, SESSION_END)  # 문서화 목적으로만 참조
    return _sort_all(out)
