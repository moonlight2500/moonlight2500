"""미국 주식 테마 분류와 자동 선정.

국내주식(screener.py)과 같은 방식이다 - 종목을 테마로 묶어 두고, 오늘 시장에서 한 테마의 종목들이
함께 오르면 그 테마를 "오늘의 테마"로 인정해 그 안의 강한 종목을 거래 후보로 뽑는다.
  테마 점수 = 동반상승 종목수 × 상승률 중앙값 × log10(거래대금)
토스 랭킹(거래대금·급등, marketCountry="US")과 종목별 시세를 재료로 쓴다.

테마 사전은 기본값을 여기에 내장하고, 실행 파일 옆에 us_themes.yaml 이 있으면 그것을 대신 쓴다
(형식은 themes.yaml 과 같고, 종목코드 자리에 미국 티커를 적는다).
"""

from __future__ import annotations

import logging
import math
import os
import statistics

from daytrader.timeutil import iso, now_kst

log = logging.getLogger(__name__)

# 테마명 -> [(티커, 종목명), ...]  ★ 테마는 계속 바뀌므로 출발점일 뿐이다.
DEFAULT_US_THEMES: dict = {
    "AI반도체": [("NVDA", "엔비디아"), ("AMD", "AMD"), ("AVGO", "브로드컴"), ("TSM", "TSMC"), ("MU", "마이크론"),
              ("ARM", "ARM"), ("SMCI", "슈퍼마이크로"), ("MRVL", "마벨"), ("INTC", "인텔")],
    "반도체장비": [("ASML", "ASML"), ("AMAT", "어플라이드머티리얼즈"), ("LRCX", "램리서치"), ("KLAC", "KLA"),
               ("TER", "테러다인"), ("ONTO", "온투이노베이션")],
    "AI소프트웨어_클라우드": [("MSFT", "마이크로소프트"), ("GOOGL", "알파벳"), ("ORCL", "오라클"), ("PLTR", "팔란티어"),
                     ("SNOW", "스노우플레이크"), ("NOW", "서비스나우"), ("CRM", "세일즈포스"), ("DDOG", "데이터독")],
    "빅테크": [("AAPL", "애플"), ("AMZN", "아마존"), ("META", "메타"), ("NFLX", "넷플릭스"), ("TSLA", "테슬라")],
    "전기차_배터리": [("TSLA", "테슬라"), ("RIVN", "리비안"), ("LCID", "루시드"), ("F", "포드"), ("GM", "GM"),
                 ("ALB", "앨버말")],
    "원전_우라늄": [("CEG", "컨스텔레이션에너지"), ("VST", "비스트라"), ("OKLO", "오클로"), ("SMR", "뉴스케일파워"),
               ("CCJ", "카메코"), ("LEU", "센트러스에너지")],
    "방산_우주": [("LMT", "록히드마틴"), ("RTX", "RTX"), ("NOC", "노스롭그루먼"), ("GD", "제너럴다이내믹스"),
              ("RKLB", "로켓랩"), ("ASTS", "AST스페이스모바일")],
    "바이오_제약": [("LLY", "일라이릴리"), ("NVO", "노보노디스크"), ("MRNA", "모더나"), ("VRTX", "버텍스"),
               ("REGN", "리제네론"), ("AMGN", "암젠")],
    "핀테크_가상자산": [("COIN", "코인베이스"), ("HOOD", "로빈후드"), ("MSTR", "스트래티지"), ("SQ", "블록"),
                  ("PYPL", "페이팔"), ("MARA", "마라홀딩스")],
    "사이버보안": [("CRWD", "크라우드스트라이크"), ("PANW", "팔로알토"), ("ZS", "지스케일러"), ("FTNT", "포티넷"),
               ("NET", "클라우드플레어")],
    "로봇_자동화": [("ISRG", "인튜이티브서지컬"), ("SYM", "심보틱"), ("TER", "테러다인"), ("ROK", "록웰오토메이션"),
               ("PATH", "유아이패스")],
    "클린에너지": [("ENPH", "엔페이즈"), ("FSLR", "퍼스트솔라"), ("NEE", "넥스트에라"), ("RUN", "선런"), ("PLUG", "플러그파워")],
    "에너지_석유": [("XOM", "엑슨모빌"), ("CVX", "셰브론"), ("OXY", "옥시덴탈"), ("COP", "코노코필립스"), ("SLB", "슐럼버거")],
    "금융": [("JPM", "JP모건"), ("GS", "골드만삭스"), ("MS", "모건스탠리"), ("BAC", "뱅크오브아메리카"), ("V", "비자")],
}


def load_us_themes(cfg) -> tuple:
    """(테마->[티커], 티커->이름). us_themes.yaml 이 있으면 그것을, 없으면 내장 기본값을 쓴다."""
    names: dict = {}
    themes: dict = {}
    path = os.path.join(os.path.dirname(getattr(cfg, "themes_file", "") or ""), "us_themes.yaml")
    if path and os.path.exists(path):
        try:
            import yaml
            from daytrader.screener import load_theme_names
            with open(path, "r", encoding="utf-8") as f:
                raw = (yaml.safe_load(f) or {}).get("themes", {}) or {}
            themes = {str(k): [str(c).strip().upper() for c in (v or [])] for k, v in raw.items()}
            names = {k.upper(): v for k, v in load_theme_names(path).items()}
        except Exception as exc:
            log.warning("us_themes.yaml 을 읽지 못해 내장 미국 테마를 씁니다: %s", exc)
            themes = {}
    if not themes:
        for theme, members in DEFAULT_US_THEMES.items():
            themes[theme] = [t for t, _n in members]
            for t, n in members:
                names.setdefault(t, n)
    return themes, names


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _row_from_ranking(r: dict) -> dict | None:
    symbol = str(r.get("symbol", "")).strip().upper()
    if not symbol:
        return None
    price_obj = r.get("price")
    if isinstance(price_obj, dict):
        price, change = _f(price_obj.get("lastPrice")), _f(price_obj.get("changeRate"))
    else:
        price, change = _f(price_obj or r.get("lastPrice")), _f(r.get("changeRate"))
    amount = _f(r.get("tradingAmount") or r.get("tradingValue"))
    return {"symbol": symbol, "price": price, "change": change, "amount": amount, "name": r.get("name") or ""}


def market_snapshot(client, count: int, errors: list) -> dict:
    """미국 거래대금 상위 + 급등 상위 랭킹을 합친다(겹치면 거래대금이 큰 쪽)."""
    rows: dict = {}
    for kind, duration in (("MARKET_TRADING_AMOUNT", "realtime"), ("TOP_GAINERS", "1d")):
        try:
            data = client.rankings(type=kind, marketCountry="US", duration=duration, count=count)
        except Exception as exc:
            errors.append(f"{kind}({duration}) 조회 실패: {exc}")
            continue
        for r in data or []:
            if not isinstance(r, dict):
                continue
            row = _row_from_ranking(r)
            if row is None:
                continue
            cur = rows.get(row["symbol"])
            if cur is None or row["amount"] > cur["amount"]:
                rows[row["symbol"]] = row
    return rows


def fill_from_prices(client, symbols: list, market: dict, errors: list) -> None:
    """랭킹에 안 잡힌 테마 종목은 시세를 따로 받아 채운다(등락률이 있을 때만)."""
    need = [s for s in symbols if s not in market]
    for i in range(0, len(need), 50):
        chunk = need[i:i + 50]
        try:
            rows = client.prices(chunk)
        except Exception as exc:
            errors.append(f"시세 조회 실패: {exc}")
            return
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            sym = str(r.get("symbol", "")).strip().upper()
            price = _f(r.get("price") or r.get("lastPrice"))
            change = r.get("changeRate")
            if not sym or not price or change is None:
                continue
            market[sym] = {"symbol": sym, "price": price, "change": _f(change),
                           "amount": _f(r.get("tradingAmount") or r.get("tradingValue")), "name": r.get("name") or ""}


def scan_us_themes(client, ocfg, cfg=None) -> dict:
    """미국 테마를 평가하고 거래 후보를 뽑는다. 국내 선정 화면과 같은 모양의 dict 를 돌려준다."""
    themes, names = load_us_themes(cfg) if cfg is not None else ({k: [t for t, _ in v] for k, v in DEFAULT_US_THEMES.items()}, {})
    errors: list = []
    min_up = max(1, int(getattr(ocfg, "min_theme_members_up", 2)))
    min_change = float(getattr(ocfg, "theme_min_change_rate", 0.01))
    max_change = float(getattr(ocfg, "theme_max_change_rate", 0.12))
    min_amount = float(getattr(ocfg, "theme_min_trading_amount", 20_000_000))
    min_price = float(getattr(ocfg, "min_price", 0) or 0)
    top_n = max(1, int(getattr(ocfg, "top_themes", 3)))
    per_theme = max(1, int(getattr(ocfg, "candidates_per_theme", 3)))

    market = market_snapshot(client, 100, errors)
    all_symbols = sorted({s for members in themes.values() for s in members})
    fill_from_prices(client, all_symbols, market, errors)

    views: list = []
    for theme, symbols in themes.items():
        members = []
        ups, up_amounts = [], []
        for s in symbols:
            row = market.get(s)
            in_market = row is not None
            change = row["change"] if row else 0.0
            is_up = in_market and change >= min_change
            if is_up:
                reason = ""
            elif not in_market:
                reason = "시장 스냅샷에 없음"
            else:
                reason = f"상승률 {change*100:+.2f}%로 기준 {min_change*100:.1f}% 미달"
            members.append({
                "symbol": s, "name": names.get(s) or (row or {}).get("name") or s,
                "last_price": row["price"] if row else 0.0, "change_rate": change,
                "trading_amount": row["amount"] if row else 0.0, "in_market": in_market,
                "is_up": bool(is_up), "reason": reason, "selected": False, "reject": "", "rank_in_theme": 0,
            })
            if is_up:
                ups.append(change)
                up_amounts.append(row["amount"])
        breadth = len(ups)
        if breadth < min_up:
            any_in = any(m["in_market"] for m in members)
            views.append({
                "name": theme, "score": 0.0, "breadth": breadth, "intensity": 0.0, "amount": sum(up_amounts),
                "liquidity": 0.0, "news_boost": 0.0, "qualified": False, "rank": 0, "picked": False,
                "reason": ("이 테마 종목의 시세를 받아오지 못했습니다 - 시장 판단이 아니라 데이터 부재입니다" if not any_in
                           else f"동반 상승 {breadth}종목 < 기준 {min_up}종목 - 한 종목만 튀는 건 테마가 아니라 개별 이슈로 봅니다"),
                "members": members, "formula": "",
            })
            continue
        intensity = statistics.median(ups)
        amount = sum(up_amounts)
        # 거래대금(달러)의 로그 - 1백만 달러 기준. 시세만 있고 대금이 없으면(0) 최소 가중치.
        liquidity = max(math.log10(max(amount, 1e6) / 1e6), 0.1)
        score = breadth * intensity * liquidity
        ranked = sorted([m for m in members if m["is_up"]], key=lambda m: m["trading_amount"], reverse=True)
        for i, m in enumerate(ranked, start=1):
            m["rank_in_theme"] = i
        views.append({
            "name": theme, "score": score, "breadth": breadth, "intensity": intensity, "amount": amount,
            "liquidity": liquidity, "news_boost": 0.0, "qualified": True, "rank": 0, "picked": False,
            "reason": "", "members": members,
            "formula": (f"{breadth}종목 × 중앙 {intensity*100:+.2f}% × log(대금 ${amount/1e6:,.0f}M)"
                        f"={liquidity:.2f} = {score:.3f}"),
        })
    qualified = sorted([v for v in views if v["qualified"]], key=lambda v: v["score"], reverse=True)
    unqualified = sorted([v for v in views if not v["qualified"]], key=lambda v: v["breadth"], reverse=True)
    for i, v in enumerate(qualified, start=1):
        v["rank"] = i
        v["picked"] = i <= top_n

    candidates: list = []
    chosen: set = set()
    for v in qualified:
        if not v["picked"]:
            continue
        taken = 0
        for m in sorted([m for m in v["members"] if m["is_up"]], key=lambda m: m["trading_amount"], reverse=True):
            if taken >= per_theme:
                break
            row = market.get(m["symbol"]) or {}
            if min_price and m["last_price"] < min_price:
                m["reject"] = f"가격 ${m['last_price']:.2f} 가 최소 ${min_price:.2f} 미만입니다."
                continue
            if m["change_rate"] > max_change:
                m["reject"] = f"상승률 {m['change_rate']*100:+.2f}%로 기준 {max_change*100:.0f}%를 넘어 추격매수 구간입니다."
                continue
            if m["trading_amount"] and m["trading_amount"] < min_amount:
                m["reject"] = f"거래대금 ${m['trading_amount']/1e6:,.1f}M 로 기준 ${min_amount/1e6:,.0f}M 미달입니다."
                continue
            if m["symbol"] in chosen:
                m["selected"] = True  # 다른 테마에서 이미 뽑힌 종목 - 표시만 하고 중복으로 담지 않는다.
                taken += 1
                continue
            m["selected"] = True
            chosen.add(m["symbol"])
            taken += 1
            candidates.append({
                "symbol": m["symbol"], "name": m["name"], "theme": v["name"], "last_price": m["last_price"],
                "change_rate": m["change_rate"], "trading_amount": m["trading_amount"], "theme_score": v["score"],
                "theme_rank": v["rank"], "rank_in_theme": m["rank_in_theme"], "theme_breadth": v["breadth"],
                "theme_intensity": v["intensity"],
                "why": (f"'{v['name']}' 테마가 오늘 {v['rank']}위입니다({v['breadth']}종목 동반 상승, 중앙값 "
                        f"{v['intensity']*100:+.2f}%). 그 안에서 {'대장주' if m['rank_in_theme'] == 1 else str(m['rank_in_theme']) + '위'}"
                        f"이고 현재 ${m['last_price']:,.2f}({m['change_rate']*100:+.2f}%)입니다."),
            })

    if candidates:
        summary = f"{len(candidates)}개 후보를 찾았습니다 ({', '.join(sorted({c['theme'] for c in candidates}))})."
    elif not market:
        summary = "시세를 하나도 받아오지 못해 미국 테마를 평가하지 못했습니다" + (f" (조회 오류: {errors[0]})" if errors else "") + "."
    elif not qualified:
        summary = "오늘은 테마로 인정될 만큼 동반 상승한 미국 종목 묶음이 없습니다 - 관심 종목만 평가합니다."
    else:
        summary = "테마는 있었지만 가격·상승률·거래대금 조건을 통과한 종목이 없습니다."

    return {
        "at": iso(now_kst()), "market_size": len(market),
        "criteria": {"text": [
            f"테마로 인정: 구성 종목 {min_up}개 이상이 +{min_change*100:.1f}% 이상 동반 상승",
            "테마 점수 = 동반상승 종목수 × 상승률 중앙값 × log10(거래대금)",
            f"상위 {top_n}개 테마에서 테마당 {per_theme}종목까지",
            f"등락률 +{min_change*100:.1f}%~+{max_change*100:.0f}% (더 오른 종목은 추격매수라 제외)",
            f"거래대금 ${min_amount/1e6:,.0f}M 이상, 최소 가격 ${min_price:,.0f}",
            "관심 종목은 테마와 별개로 항상 거래 대상에 함께 들어갑니다",
        ]},
        "themes": qualified + unqualified, "candidates": candidates, "summary": summary, "errors": errors,
    }
