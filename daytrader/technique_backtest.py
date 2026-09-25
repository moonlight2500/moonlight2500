"""종목별·테마별로 어떤 매매 기법이 더 잘 맞는지 확인하는 백테스트.

오늘 선정된 테마 후보 종목(국내·해외·암호화폐 중 고른 시장)에 대해, 최근 실제 시세(1분봉)를
기법별로 독립적으로 재생해 "그 기법이 이 종목에서 최근 며칠간 얼마나 벌었을지"를 계산하고
순위를 매긴다. 결과는 technique_prefs.py 에 저장되어, 실전 매매(모의·실거래)의 진입 기법
채점(daytrader.playbook.Playbook._performance_multiplier 옆)에서 "이 종목/테마는 이 기법이
그동안 더 잘 맞았다"는 가산점으로 쓰인다.

★★★ 단순화(정직하게 밝힌다) - 이건 실거래 엔진의 축소판이 아니다:
  - 진입 판정은 각 기법의 실제 로직(daytrader.playbook.ENTRY_TECHNIQUES)을 그대로 쓴다.
  - 청산은 모든 기법에 공통으로 실거래와 같은 손절·익절·추적손절(cfg.risk)만 쓴다 - 기법마다
    청산 방식을 다르게 섞으면 "진입 신호 자체가 좋았는가"를 비교하려는 목적이 흐려진다.
    ATR 손절·모멘텀 소진·시간 청산 같은 고급 청산 기법, 분할 매수·분할 매도는 재현하지 않는다.
  - 테마 대장주(theme_leader)처럼 같은 시각 다른 종목 시세가 함께 필요한 기법은, 이 백테스트가
    종목을 하나씩 따로 재생하는 구조라 그 정보가 없어 신호가 거의(또는 전혀) 안 나온다 - 결함이
    아니라 이 방식의 한계다.
  - 데이터가 아주 많은 시장(암호화폐는 24시간)은 봉 개수를 상한(MAX_BARS)으로 자른다 - "최근
    1주일"을 정확히 다 못 채울 수 있다.
"""

from __future__ import annotations

from types import SimpleNamespace

from daytrader.playbook import ENTRY_TECHNIQUES, NAN, Bar

MAX_BARS = 2000  # 종목 하나당 재생할 봉 개수 상한(API 호출·연산 시간 보호)
DOMESTIC_ONLY = ("open_gap", "close_squeeze")  # config.DOMESTIC_ONLY_ENTRIES 와 같은 목록(순환 임포트 회피)
MIN_WARMUP_BARS = 30  # 지표 계산에 필요한 최소 선행 봉 수
# ★★★ 스윙 기법은 일봉(하루=봉 하나) 전용이다 - vwap_pullback·orb 같은 분봉 기법을 일봉에
# 그대로 돌리면 "오늘 하루 안의 vwap" 같은 개념 자체가 성립하지 않아 의미 없는 결과가 나온다.
# 그래서 스윙은 playbook.py 의 SwingXxxEntry 3종만 평가한다(다른 시장처럼 레지스트리 전체가 아니라).
SWING_ENTRIES = ("swing_ma_pullback", "swing_breakout", "swing_golden_cross")


def applicable_techniques(market: str) -> list:
    """이 시장에서 의미 있게 평가할 수 있는 진입 기법 키 목록."""
    if market == "swing":
        return list(SWING_ENTRIES)
    if market == "domestic":
        return [k for k in ENTRY_TECHNIQUES if k not in SWING_ENTRIES]
    return [k for k in ENTRY_TECHNIQUES if k not in DOMESTIC_ONLY and k not in SWING_ENTRIES]


# ── 후보 종목(오늘의 테마 선정) ──────────────────────────────────────────

def merge_with_watchlist(candidates: list, watchlist) -> list:
    """★★★ 실제로 겪은 문제 - 지금 이 순간 테마로 인정될 만큼 동반 상승한 종목 묶음이 없으면
    (예: 애프터마켓·야간 조용한 시간) 테마 후보가 텅 비어서 백테스트가 "후보 0종목"으로 끝났다.
    실제 엔진은 이럴 때도 관심 종목은 테마와 별개로 항상 거래 대상에 넣는다(사용자 요청 - "관심
    종목은 테마와 별개로 항상 거래 대상") - 여기서도 같은 원칙을 따른다. 이미 테마로 뽑힌 종목은
    중복으로 다시 안 붙인다(테마 이름을 그대로 유지)."""
    out = list(candidates or [])
    have = {c["symbol"] for c in out}
    for sym in watchlist or []:
        if sym not in have:
            out.append({"symbol": sym, "name": sym, "theme": "관심 종목"})
            have.add(sym)
    return out


def get_candidates(market: str, client, cfg, limit: int = 10) -> list:
    """오늘의 테마 후보 종목을 시장별로 뽑는다. [{symbol, name, theme}, ...]."""
    if market == "domestic":
        from daytrader.screener import Screener
        report = Screener(client, cfg).build_report()
        return [{"symbol": c.symbol, "name": c.name, "theme": c.theme} for c in report.candidates[:limit]]

    if market == "overseas":
        from daytrader.us_themes import scan_us_themes
        report = scan_us_themes(client, cfg.overseas, cfg)
        theme_candidates = [{"symbol": c["symbol"], "name": c.get("name") or c["symbol"], "theme": c.get("theme", "")}
                             for c in (report.get("candidates") or [])]
        out = merge_with_watchlist(theme_candidates, getattr(cfg.overseas, "watchlist", None))
        return out[:limit]

    if market == "crypto":
        from daytrader.crypto_engine import fetch_top_volume_markets
        from daytrader.timeutil import day_str, now_kst
        markets = fetch_top_volume_markets(client, day_str(now_kst()), int(getattr(cfg.crypto, "top_volume_count", 10) or 10))
        return [{"symbol": m, "name": m.replace("KRW-", ""), "theme": "거래대금 상위"} for m in markets[:limit]]

    if market == "swing":
        # ★★★ "스윙매매 대상은 국내주식·해외주식·암호화폐 모두 해당되도록" - 세 시장의 후보를
        # 한 번에 모은다. client 는 국내·해외용(TossClient/QuoteRouter)만 받는다 - 암호화폐
        # 후보는 여기서 자체적으로 빗썸 클라이언트를 만들어 받는다(키가 없으면 그냥 건너뛴다 -
        # 국내·해외 스윙 백테스트는 빗썸 키 없이도 동작해야 한다).
        try:
            from daytrader.screener import Screener
            report = Screener(client, cfg).build_report()
            dom = [{"symbol": c.symbol, "name": c.name, "theme": c.theme} for c in report.candidates]
        except Exception:
            dom = []
        dom = [{**c, "asset_market": "domestic"} for c in merge_with_watchlist(dom, getattr(cfg.swing, "watchlist", None))]

        ov_pool = list(getattr(cfg.overseas, "watchlist", None) or []) + list(getattr(cfg.swing, "overseas_watchlist", None) or [])
        ov = [{**c, "asset_market": "overseas"} for c in merge_with_watchlist([], ov_pool)]

        cr = []
        if getattr(cfg, "bithumb_access_key", "") and getattr(cfg, "bithumb_secret_key", ""):
            try:
                from daytrader.bithumb_api import BithumbClient
                BithumbClient(cfg.bithumb_access_key, cfg.bithumb_secret_key)  # 키 형식만 확인(연결은 지연)
                cr_pool = list(getattr(cfg.crypto, "watchlist", None) or []) + list(getattr(cfg.swing, "crypto_watchlist", None) or [])
                have = set()
                for sym in cr_pool:
                    if sym not in have:
                        cr.append({"symbol": sym, "name": sym.replace("KRW-", ""), "theme": "관심 종목", "asset_market": "crypto"})
                        have.add(sym)
            except Exception:
                cr = []

        # ★ 한 시장이 후보가 많다고 나머지 시장을 밀어내지 않도록 번갈아 담는다(round-robin).
        pools = [dom, ov, cr]
        out = []
        i = 0
        while len(out) < limit and any(pools):
            p = pools[i % 3]
            if p:
                out.append(p.pop(0))
            i += 1
        return out

    raise ValueError(f"알 수 없는 시장입니다: {market}")


# ── 실제 시세(1분봉) 가져오기 ─────────────────────────────────────────────

def fetch_bars(market: str, client, symbol: str, days: int = 7) -> list:
    """최근 며칠치 시세를 최대한 모은다(API 가 한 번에 다 안 주면 이어 받는다).
    ★ 스윙만 1분봉이 아니라 일봉이다 - days 의 의미도 다르다(1분봉 시장은 "최근 N일치
    1분봉", 스윙은 "최근 N개 거래일치 일봉" - 봉 하나가 이미 하루라 그대로 개수가 된다)."""
    if market == "crypto":
        return _fetch_crypto_bars(client, symbol, days)
    if market == "swing":
        return _fetch_daily_bars(client, symbol, days)
    return _fetch_router_bars(client, symbol, days)


_ROUTER_BATCH = 200  # ★★★ 실제로 겪은 문제 - QuoteRouter/TossClient 는 한 번에 200개 넘게
# 요청하면 오류 없이 조용히 빈 배열을 돌려준다(해외 종목에서 count=400 을 줬더니 매번 0개가
# 와서 백테스트가 "봉이 아예 없음"으로 실패했다 - 국내 종목은 우연히 네이버 폴백이 count 를
# 무시하고 훨씬 많이 돌려줘서 이 한도에 안 걸렸을 뿐이다). 그래서 200개씩 나눠 받는다.


def _fetch_router_bars(client, symbol: str, days: int) -> list:
    """국내·해외 - QuoteRouter/TossClient 스타일(candles(symbol, interval, count, before=)).
    ★ QuoteRouter(엔진이 실제로 쓰는 래퍼)는 candles() 에 before 를 안 받는다(daytrader/router.py) -
    페이지를 더 넘기려면 그 안의 실제 TossClient(.primary)를 직접 불러야 한다. 없으면(테스트용
    가짜 클라이언트 등) client 자체로 되돌아간다 - 그때는 첫 페이지(최대 200개)만 받는다."""
    target = min(MAX_BARS, days * 390 + 60)  # 국내 정규장 기준 하루 390분 - 대략치, 넘치면 어차피 자른다
    raw = getattr(client, "primary", None) or client
    rows: list = []
    before = None
    for _ in range(target // _ROUTER_BATCH + 2):
        try:
            batch = raw.candles(symbol, "1m", _ROUTER_BATCH, before=before) if before else raw.candles(symbol, "1m", _ROUTER_BATCH)
        except TypeError:
            # ★★★ 실제로 겪은 버그 - before 를 못 받는 클라이언트라, 여기서 다시 불러 봐야 방금
            # 받은 것과 똑같은(최신) 페이지가 또 온다. 그걸 그대로 rows 에 얹으면 같은 봉이
            # 중복으로 두 번 들어간다. before 가 안 먹힌다는 걸 안 순간 더 볼 것도 없이 멈춘다.
            break
        except Exception:
            break
        batch = [r for r in (batch or []) if isinstance(r, dict)]
        if not batch:
            break
        rows = batch + rows
        oldest = batch[0].get("timestamp")
        if not oldest or len(rows) >= target or len(batch) < _ROUTER_BATCH:
            break
        before = oldest
    bars = [Bar.from_api(r) for r in rows]
    bars.sort(key=lambda b: b.ts or "")
    return bars[-MAX_BARS:]


def _fetch_daily_bars(client, symbol: str, days: int) -> list:
    """스윙 - 국내주식 일봉(interval="1d"). 봉 하나가 하루라 페이지당 최대 개수만
    조심하면 된다(위 _ROUTER_BATCH 와 같은 200개 제한 - 일봉은 하루 하나뿐이라
    분봉보다 훨씬 적게 받아도 되지만 안전하게 같은 한도를 쓴다)."""
    target = min(MAX_BARS, max(days, MIN_WARMUP_BARS + 10))
    raw = getattr(client, "primary", None) or client
    rows: list = []
    before = None
    for _ in range(target // _ROUTER_BATCH + 2):
        try:
            batch = raw.candles(symbol, "1d", _ROUTER_BATCH, before=before) if before else raw.candles(symbol, "1d", _ROUTER_BATCH)
        except TypeError:
            break
        except Exception:
            break
        batch = [r for r in (batch or []) if isinstance(r, dict)]
        if not batch:
            break
        rows = batch + rows
        oldest = batch[0].get("timestamp")
        if not oldest or len(rows) >= target or len(batch) < _ROUTER_BATCH:
            break
        before = oldest
    bars = [Bar.from_api(r) for r in rows]
    bars.sort(key=lambda b: b.ts or "")
    return bars[-MAX_BARS:]


def _fetch_crypto_bars(client, market: str, days: int) -> list:
    """암호화폐 - Bithumb(Upbit 호환) candles(market, unit=, count<=200, to=)."""
    target = min(MAX_BARS, days * 24 * 60)
    rows: list = []
    to = None
    for _ in range(max(1, target // 200 + 2)):
        try:
            batch = client.candles(market, unit=1, count=200, to=to) if to else client.candles(market, unit=1, count=200)
        except Exception:
            break
        batch = [r for r in (batch or []) if isinstance(r, dict)]
        if not batch:
            break
        # bithumb_api.candles() 는 이미 과거→최신으로 뒤집어 준다.
        rows = batch + rows
        oldest = batch[0].get("candle_date_time_kst") or batch[0].get("timestamp")
        if not oldest or len(rows) >= target or len(batch) < 200:
            break
        to = oldest
    bars = [
        Bar(
            ts=str(r.get("candle_date_time_kst") or r.get("timestamp") or ""),
            open=float(r.get("opening_price") or r.get("open") or 0),
            high=float(r.get("high_price") or r.get("high") or 0),
            low=float(r.get("low_price") or r.get("low") or 0),
            close=float(r.get("trade_price") or r.get("close") or 0),
            volume=float(r.get("candle_acc_trade_volume") or r.get("volume") or 0),
        )
        for r in rows
    ]
    bars.sort(key=lambda b: b.ts or "")
    return bars[-MAX_BARS:]


def _fetch_crypto_daily_bars(client, market: str, days: int) -> list:
    """스윙의 암호화폐 레그 - 분봉이 아니라 빗썸 일봉(day_candles, KST 자정 기준)을 받는다.
    ★ day_candles 는 count 만 받고 페이지(before/to) 파라미터가 없다 - 스윙에 필요한 범위
    (60일선 웜업 + 최근 1주 모멘텀 확인, 최대 240일)가 한 번에 받을 수 있는 한도(200) 안에
    들어오므로 페이지네이션 없이 한 번만 부른다."""
    count = min(200, max(days, MIN_WARMUP_BARS + 10))
    try:
        rows = client.day_candles(market, count)
    except Exception:
        rows = []
    bars = [
        Bar(
            ts=str(r.get("candle_date_time_kst") or r.get("timestamp") or ""),
            open=float(r.get("opening_price") or r.get("open") or 0),
            high=float(r.get("high_price") or r.get("high") or 0),
            low=float(r.get("low_price") or r.get("low") or 0),
            close=float(r.get("trade_price") or r.get("close") or 0),
            volume=float(r.get("candle_acc_trade_volume") or r.get("volume") or 0),
        )
        for r in (rows or []) if isinstance(r, dict)
    ]
    bars.sort(key=lambda b: b.ts or "")
    return bars


# ── 기법 하나를 종목 하나에 재생 ─────────────────────────────────────────

def _pnl_pct(entry: float, exit_price: float, commission_pct: float, tax_pct: float) -> float:
    cost = entry * (1 + commission_pct)
    proceeds = exit_price * (1 - commission_pct - tax_pct)
    return (proceeds - cost) / cost if cost else 0.0


def _window_for(cfg, now_dt, market: str) -> str:
    """국내만 장 초반·막판 시간대 구분이 있다(open_gap/close_squeeze 용) - 그 밖은 항상 main."""
    if market != "domestic":
        return "main"
    try:
        from daytrader import session
        return session.phase(cfg, now=now_dt, client=None).get("window") or "main"
    except Exception:
        return "main"


def simulate_technique(cfg, market: str, technique_key: str, bars: list, symbol: str, name: str, theme: str, risk=None) -> dict:
    """기법 하나를 이 종목의 실제 과거 봉에 그대로 재생한다. 진입은 실제 기법 로직, 청산은
    공통 규칙(손절·익절·추적손절)만 쓴다(모듈 설명 참고).

    ★★★ risk 오버라이드 - 시장마다 실제로 쓰는 손절·익절·추적폭이 다르다(스윙은 일봉 기준
    며칠~몇 주 변동을 견뎌야 하는 8~20%대, 암호화폐·해외주식도 각자 cfg.crypto/cfg.overseas 값).
    안 넘기면 cfg.risk(국내)로 계산해, 다른 시장은 실제 매매와 다른 기준으로 채점된다(그 결과가
    technique_prefs.json 으로 실전 가산점에 반영되므로 조용히 틀리면 위험하다) - run() 이 시장별로
    만든 네임스페이스를 넘긴다(크립토 엔진이 Playbook 에 crypto 전용 risk 를 넘기는 것과 같은 원칙)."""
    from daytrader.timeutil import now_kst, parse_dt

    tech = ENTRY_TECHNIQUES[technique_key](cfg, cfg.strategy.p(technique_key))
    windows = getattr(tech, "windows", ("main",))
    kr_session = market == "domestic"

    r = risk or cfg.risk
    commission, tax = cfg.costs.commission_pct, cfg.costs.tax_pct

    trades: list = []
    position = None  # {"entry_price","entry_ts","peak"}
    day_open: dict = {}

    if len(bars) <= MIN_WARMUP_BARS:
        return _empty_result(technique_key, tech.label)

    for i in range(MIN_WARMUP_BARS, len(bars)):
        bar = bars[i]
        date = (bar.ts or "")[:10]
        o = day_open.setdefault(date, bar.open if bar.open == bar.open else bar.close)
        change_rate = (bar.close - o) / o if o else 0.0
        now_dt = parse_dt(bar.ts) or now_kst()
        window = _window_for(cfg, now_dt, market)

        if position is None:
            if window not in windows:
                continue
            ctx = SimpleNamespace(
                symbol=symbol, name=name, theme=theme, now=now_dt, change_rate=change_rate,
                kr_session=kr_session, window=window, upper_limit=None,
                prev_verdict=None, prev_verdicts=[], theme_bars=None, theme_breadth=NAN,
                theme_rank=NAN, theme_intensity=NAN, orb_done=False, minutes_to_close=999.0,
                force_close=False, scale_out=False,
            )
            try:
                v = tech.evaluate(bars[: i + 1], ctx)
            except Exception:
                continue
            if v and v.ok:
                position = {"entry_price": bar.close, "entry_ts": bar.ts, "peak": bar.close}
            continue

        hi = bar.high if bar.high == bar.high else bar.close
        lo = bar.low if bar.low == bar.low else bar.close
        position["peak"] = max(position["peak"], hi)
        entry = position["entry_price"]
        stop_price = entry * (1 - r.stop_loss_pct)
        take_price = entry * (1 + r.take_profit_pct)
        armed = position["peak"] >= entry * (1 + r.trailing_arm_pct)
        trail_price = position["peak"] * (1 - r.trailing_stop_pct) if armed else None

        exit_price, reason = None, ""
        if lo <= stop_price:
            exit_price, reason = stop_price, "손절"
        elif trail_price is not None and lo <= trail_price:
            exit_price, reason = trail_price, "추적손절"
        elif hi >= take_price:
            exit_price, reason = take_price, "익절"
        elif i == len(bars) - 1:
            exit_price, reason = bar.close, "기간종료(미청산)"

        if exit_price is not None:
            pnl_pct = _pnl_pct(entry, exit_price, commission, tax)
            trades.append({"entry_ts": position["entry_ts"], "exit_ts": bar.ts, "pnl_pct": pnl_pct, "reason": reason})
            position = None

    return _aggregate(technique_key, tech.label, trades)


def _empty_result(key: str, label: str) -> dict:
    return {
        "technique": key, "label": label, "trades": 0, "wins": 0, "losses": 0,
        "win_rate": 0.0, "total_pnl_pct": 0.0, "avg_pnl_pct": 0.0, "profit_factor": 0.0, "sample": [],
    }


def _aggregate(key: str, label: str, trades: list) -> dict:
    if not trades:
        return _empty_result(key, label)
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    total = sum(t["pnl_pct"] for t in trades)
    gains = sum(t["pnl_pct"] for t in wins)
    loss_sum = -sum(t["pnl_pct"] for t in losses)
    if loss_sum > 0:
        profit_factor = gains / loss_sum
    else:
        profit_factor = float("inf") if gains > 0 else 0.0
    return {
        "technique": key, "label": label, "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades), "total_pnl_pct": total, "avg_pnl_pct": total / len(trades),
        "profit_factor": profit_factor, "sample": trades[-5:],
    }


# ── 종목·시장 전체를 돌려 순위까지 ────────────────────────────────────────

def run(cfg, client, market: str, days: int = 7, symbol_limit: int = 10, save: bool = True,
        candidates: list | None = None, trigger: str = "manual") -> dict:
    """오늘의 테마 후보를 뽑고, 종목마다 이 시장에서 의미 있는 기법을 전부 재생해 순위를 매긴다.
    save=True 면 종목별·테마별 1등 기법을 technique_prefs.py 에 저장해 실전 매매에 반영한다.

    candidates 를 넘기면 새로 안 뽑고 그대로 쓴다 - auto_backtest.py 가 재선정 직후 이미 뽑아 둔
    후보를 그대로 넘겨 API 를 두 번 안 부르게 한다. trigger 는 기록(technique_prefs.history)에
    "manual"(사용자가 [실험실]에서 직접 실행)인지 "auto"(종목 재선정 때 자동 실행)인지 남긴다."""
    if market == "swing" and days == 7:
        # ★ 호출부가 명시적으로 안 정했으면 스윙 기본값(일봉 약 8개월치)을 쓴다 - 위 fetch_bars
        # 설명대로 스윙은 days 가 "거래일 수"라 분봉 시장의 기본값 7 은 웜업(60일선)도 못 채운다.
        days = 240

    if candidates is None:
        candidates = get_candidates(market, client, cfg, limit=symbol_limit)
    else:
        candidates = candidates[:symbol_limit]
    techniques = applicable_techniques(market)
    # ★★★ 실제로 겪은 버그(crypto_engine.py 의 "코인 청산이 전부 90분 시간손절로만 찍힌다"와
    # 같은 종류) - simulate_technique() 은 risk 를 안 넘기면 cfg.risk(국내)로 손절·익절·추적폭을
    # 계산한다. 시장별로 안 넘기면 백테스트(→ technique_prefs.json → 실전 채점 가산점)가 국내
    # 기준으로 채점돼, 크립토·해외주식은 자기 실제 손절·익절 폭과 다른 기준으로 "이 기법이 더
    # 잘 맞았다"를 판단하게 된다. 시장마다 실제로 쓰는 값을 명시적으로 넘긴다.
    market_risk = None
    if market == "swing":
        from types import SimpleNamespace as _NS
        market_risk = _NS(
            stop_loss_pct=cfg.swing.stop_loss_pct, take_profit_pct=cfg.swing.take_profit_pct,
            trailing_stop_pct=cfg.swing.trailing_pct, trailing_arm_pct=cfg.swing.trailing_pct,
        )
    elif market == "crypto":
        from types import SimpleNamespace as _NS
        market_risk = _NS(
            stop_loss_pct=cfg.crypto.stop_loss_pct, take_profit_pct=cfg.crypto.take_profit_pct,
            trailing_stop_pct=cfg.crypto.trailing_pct, trailing_arm_pct=cfg.crypto.trailing_pct,
        )
    elif market == "overseas":
        from types import SimpleNamespace as _NS
        market_risk = _NS(
            stop_loss_pct=cfg.overseas.stop_loss_pct, take_profit_pct=cfg.overseas.take_profit_pct,
            trailing_stop_pct=cfg.overseas.trailing_pct, trailing_arm_pct=cfg.overseas.trailing_arm_pct,
        )

    by_symbol = []
    theme_scores: dict = {}  # theme -> {technique: [pnl_pct, ...]}
    errors = []
    _bithumb_cache = []  # ★ 스윙의 암호화폐 후보만 필요할 때 한 번만 만든다(지연 생성 캐시).

    def _bithumb_client():
        if not _bithumb_cache:
            from daytrader.bithumb_api import BithumbClient
            _bithumb_cache.append(BithumbClient(cfg.bithumb_access_key, cfg.bithumb_secret_key))
        return _bithumb_cache[0]

    for c in candidates:
        symbol, name, theme = c["symbol"], c["name"], c.get("theme", "")
        try:
            if market == "swing":
                # ★★★ "스윙매매 대상은 국내주식·해외주식·암호화폐 모두" - 후보마다 실제로
                # 어느 시장 것인지(asset_market)를 보고 알맞은 클라이언트·간격으로 일봉을 받는다.
                asset_market = c.get("asset_market", "domestic")
                if asset_market == "crypto":
                    bars = _fetch_crypto_daily_bars(_bithumb_client(), symbol, days)
                else:
                    bars = _fetch_daily_bars(client, symbol, days)
            else:
                bars = fetch_bars(market, client, symbol, days=days)
        except Exception as exc:
            errors.append(f"{symbol}: 시세 조회 실패 - {exc}")
            continue
        if len(bars) <= MIN_WARMUP_BARS:
            errors.append(f"{symbol}: 받은 봉이 너무 적어({len(bars)}개) 건너뜁니다.")
            continue

        results = [simulate_technique(cfg, market, key, bars, symbol, name, theme, risk=market_risk) for key in techniques]
        results.sort(key=lambda r: r["total_pnl_pct"], reverse=True)
        by_symbol.append({
            "symbol": symbol, "name": name, "theme": theme, "bars": len(bars),
            "best_technique": results[0]["technique"] if results and results[0]["trades"] else None,
            "results": results,
        })
        for r in results:
            if r["trades"]:
                theme_scores.setdefault(theme, {}).setdefault(r["technique"], []).append(r["total_pnl_pct"])

    by_theme = []
    for theme, per_tech in theme_scores.items():
        rows = sorted(
            ({"technique": k, "avg_pnl_pct": sum(v) / len(v), "symbols": len(v)} for k, v in per_tech.items()),
            key=lambda r: r["avg_pnl_pct"], reverse=True,
        )
        by_theme.append({"theme": theme, "best_technique": rows[0]["technique"] if rows else None, "results": rows})

    out = {
        "market": market, "days": days, "at": _now_iso(),
        "candidates": len(candidates), "by_symbol": by_symbol, "by_theme": by_theme, "errors": errors,
    }
    if save:
        from daytrader import technique_prefs
        technique_prefs.save_from_backtest(cfg, market, out, trigger=trigger)
    return _json_safe(out)


def _json_safe(v):
    """★★★ 실제로 겪은 문제 - 손실이 하나도 없는 기법의 손익비를 float("inf") 로 돌려줬는데,
    Starlette 의 JSONResponse 는 기본으로 무한대·NaN 을 거부해서(allow_nan=False) 응답 전체가
    500 으로 죽었다(playbook.py 의 Verdict.inputs 에서 이미 한 번 겪은 것과 같은 종류의 문제).
    무한대는 화면에서 "∞"로 표시할 수 있게 None 으로 바꿔 내보낸다(계산 자체는 그대로 두고,
    API 로 나가기 직전에만 정리한다)."""
    if isinstance(v, float) and (v == float("inf") or v == float("-inf") or v != v):
        return None
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


def _now_iso() -> str:
    from daytrader.timeutil import iso, now_kst
    return iso(now_kst())
