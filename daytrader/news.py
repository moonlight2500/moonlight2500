"""헤드라인·출처·링크·시각만 다룬다. 기사 본문을 가져오지도, 저장하지도,
요약하지도 않는다. 기사는 저작물이고, 전문을 긁어 보관하는 것은 사용자를
곤란하게 만들 수 있다. 본문이 필요하면 링크를 눌러 원문에서 읽는 구조다.

매매 개입 - 기본은 개입하지 않는다.
news.mode = off | view(기본) | avoid | boost
boost 를 권하지 않는 이유: 이 모듈은 기사 내용을 이해하지 못한다. 제목의
문자열만 본다. '삼성전자 주가 급락'과 '급등'을 구분하지 못한다. 기사가 많다는
것은 관심이 많다는 뜻이지 오른다는 뜻이 아니다.
avoid 가 그나마 쓸 만한 이유: 같은 문자열 매칭이라도 사지 않는 쪽으로 쓰면
틀렸을 때의 대가가 작다. 놓치는 기회의 비용은 손실의 비용보다 작다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote

# ★★★ [2-8] xml.etree 는 외부에서 받은 XML 을 곧이곧대로 파싱한다 - 악의적인(또는 흉내 낸) RSS
# 서버가 "billion laughs"(엔티티 재귀 확장으로 메모리 소진) 같은 조작된 응답을 주면 이 프로그램이
# 죽거나 매우 느려질 수 있다. defusedxml 은 그런 확장·외부 엔티티 참조를 미리 차단한 뒤 표준
# ElementTree 로 파싱한다 - fromstring() 호출만 바꾸면 되고 나머지 API(있으니 find/findall 등)는
# 그대로다.
from defusedxml import ElementTree as _DefusedET
from defusedxml.common import DefusedXmlException as _DefusedXmlException

from daytrader import netutil
from daytrader.timeutil import iso, now_kst, parse_dt

STORED_FIELDS = ("id", "title", "link", "source_id", "source", "publisher", "group", "published")

GROUP_ORDER = ["국내 증시", "증권사·수급", "테마", "해외 증시", "해외 기술주"]
GROUP_ALIAS = {"국내": "국내 증시", "국내 투자사": "증권사·수급", "미국": "해외 증시"}

RISK_WORDS = [
    "유상증자", "무상감자", "감자", "블록딜", "대량매도", "지분 매각", "상장폐지",
    "거래정지", "관리종목", "횡령", "배임", "분식", "불성실공시", "실적 쇼크", "어닝쇼크",
    "적자전환", "영업정지", "소송", "압수수색", "검찰", "리콜", "임상 실패", "품목허가 취소",
]

_GOOGLE_NEWS_URL = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
# ★★★ "미장은 신뢰성 있는 해외 소식 위주로 취합" 요청 - 구글뉴스 한국어판(hl=ko&gl=KR)은
# 한국 매체가 재가공한 기사 위주로 나온다. 영어권 판(hl=en-US&gl=US)으로 원문 기사 자체를
# 검색해 온다.
_GOOGLE_NEWS_URL_EN = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def _google_news_url(query: str) -> str:
    return _GOOGLE_NEWS_URL.format(q=quote(query))


def _google_news_url_en(query: str) -> str:
    return _GOOGLE_NEWS_URL_EN.format(q=quote(query))


DEFAULT_SOURCES = [
    {"id": "kr_market", "group": "국내 증시", "publisher": "구글뉴스", "url": _google_news_url("국내 증시 속보")},
    {"id": "kr_disclosure", "group": "국내 증시", "publisher": "구글뉴스", "url": _google_news_url("공시 유상증자 실적")},
    {"id": "kr_broker", "group": "증권사·수급", "publisher": "구글뉴스", "url": _google_news_url("증권사 리포트 목표주가")},
    {"id": "kr_flow", "group": "증권사·수급", "publisher": "구글뉴스", "url": _google_news_url("외국인 기관 수급")},
    {"id": "us_wallst", "group": "해외 증시", "publisher": "구글뉴스", "url": _google_news_url("월가 뉴욕증시")},
    {"id": "us_fed", "group": "해외 증시", "publisher": "구글뉴스", "url": _google_news_url("연준 금리")},
    {"id": "us_bigtech", "group": "해외 증시", "publisher": "구글뉴스", "url": _google_news_url("빅테크 반도체")},
    {"id": "kr_hankyung", "group": "국내 증시", "publisher": "한국경제", "url": "https://www.hankyung.com/feed/finance"},
    {"id": "kr_mk", "group": "국내 증시", "publisher": "매일경제", "url": "https://www.mk.co.kr/rss/50200011/"},
    # ★★★ "속보의 소스는 좀더 넓혀" 요청(2026-09-24) - 아래 4개를 추가했다. URL 은 전부
    # curl 로 실제 응답(200/유효 XML)을 확인한 뒤에만 넣었다(예: sedaily.com, edaily.co.kr,
    # it.chosun.com 의 후보 URL은 404/연결 실패로 확인돼 뺐다 - 정확한 경로를 모르면서
    # 넣으면 그 소스가 계속 조용히 실패만 한다). 이 중 kr_yna·kr_mk_stock·crypto_ct 는
    # "국내 증시 속보" 같은 검색어로 좁혀진 게 아니라 언론사의 경제/코인 전체 RSS라서
    # 증권·주식과 무관한 기사도 섞여 들어온다 - "속보는 증권·주식시장 관련만" 요청에 따라
    # require_market_keyword: True 로 표시해 두면 fetch() 가 MARKET_KEYWORDS 에 안 걸리는
    # 제목은 걸러낸다(구글뉴스 검색 소스들은 검색어 자체가 이미 증시로 좁혀져 있어 그대로 둔다).
    {"id": "kr_yna", "group": "국내 증시", "publisher": "연합뉴스", "url": "https://www.yna.co.kr/rss/economy.xml",
     "require_market_keyword": True},
    {"id": "kr_hankyung_eco", "group": "국내 증시", "publisher": "한국경제", "url": "https://www.hankyung.com/feed/economy",
     "require_market_keyword": True},
    {"id": "kr_mk_stock", "group": "국내 증시", "publisher": "매일경제", "url": "https://www.mk.co.kr/rss/30100041/",
     "require_market_keyword": True},
    # ★ 암호화폐 거래(crypto_engine.py) 추가 이후에도 속보 소스에 코인 전용 출처가 하나도
    # 없었다 - 유일한 암호화폐 시장 뉴스 소스로 코인텔레그래프를 추가한다(영어 매체라
    # MARKET_KEYWORDS 에 영어 키워드도 같이 둔다 - 아래 참고).
    {"id": "crypto_ct", "group": "테마", "publisher": "코인텔레그래프", "url": "https://cointelegraph.com/rss",
     "require_market_keyword": True},
    # ★ "속보에 마이클버리·ARK 등 주요 투자자나 투자사들의 동향도 추가해" 요청(2026-09-24) -
    # 유명 투자자·투자사의 매매·포지션 동향은 구글뉴스 검색으로 충분히 좁혀지므로(검색어 자체가
    # 이미 그 인물/회사에 한정) require_market_keyword 없이 그대로 둔다.
    {"id": "us_investor_burry", "group": "해외 증시", "publisher": "구글뉴스", "url": _google_news_url("마이클 버리 Michael Burry 포지션")},
    {"id": "us_investor_ark", "group": "해외 증시", "publisher": "구글뉴스", "url": _google_news_url("ARK 인베스트 캐시우드 매수 매도")},
    {"id": "us_investor_buffett", "group": "해외 증시", "publisher": "구글뉴스", "url": _google_news_url("워런 버핏 버크셔 해서웨이 투자")},
    {"id": "us_investor_13f", "group": "해외 증시", "publisher": "구글뉴스", "url": _google_news_url("월가 헤지펀드 13F 포지션 공개")},
    # ★★★ "미장은 신뢰성 있는 해외 소식 위주로 취합하고 특히 기술주와 AI 반도체 관련 주식의
    # 동향과 뉴스에 집중" 요청(2026-09-25) - 한국어 재가공 기사가 아니라 영어권 1차 소스를
    # 쓴다. 전부 curl·앱의 실제 요청 세션(netutil.make_session()) 양쪽으로 200/유효 XML
    # 응답을 직접 확인한 뒤에만 넣었다(사용자가 블룸버그를 제안해 확인 후 추가).
    {"id": "us_bloomberg_markets", "group": "해외 기술주", "publisher": "Bloomberg Markets",
     "url": "https://www.bloomberg.com/feeds/markets/news.rss", "require_market_keyword": True},
    {"id": "us_bloomberg_tech", "group": "해외 기술주", "publisher": "Bloomberg Technology",
     "url": "https://www.bloomberg.com/feeds/technology/news.rss", "require_market_keyword": True},
    {"id": "us_marketwatch_top", "group": "해외 기술주", "publisher": "MarketWatch",
     "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories", "require_market_keyword": True},
    {"id": "us_marketwatch_pulse", "group": "해외 기술주", "publisher": "MarketWatch",
     "url": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse", "require_market_keyword": True},
    {"id": "us_ai_semi", "group": "해외 기술주", "publisher": "Google News(EN)",
     "url": _google_news_url_en("AI semiconductor chip stocks Nvidia AMD")},
    {"id": "us_bigtech_en", "group": "해외 기술주", "publisher": "Google News(EN)",
     "url": _google_news_url_en("Nasdaq big tech stocks close")},
]

# ★ require_market_keyword=True 인 소스(언론사 전체 RSS - 증권과 무관한 기사도 섞인다)에서,
# 제목에 이 중 하나라도 있어야 살아남는다. 구글뉴스 검색 소스는 검색어 자체가 이미 증시로
# 좁혀져 있어 이 필터를 안 거친다(대소문자 구분 없이 부분 문자열로 비교한다).
MARKET_KEYWORDS = [
    "증시", "코스피", "코스닥", "주가", "주식", "종목", "상장", "공모", "매수", "매도",
    "급등", "급락", "실적", "어닝", "배당", "시가총액", "코인", "비트코인", "암호화폐", "가상자산",
    "ETF", "환율", "금리", "나스닥", "다우", "S&P", "stock", "shares", "market", "trading",
    "crypto", "bitcoin", "ETH", "BTC", "price", "exchange", "SEC", "ETF",
    # ★ "기술주와 AI 반도체 관련 주식의 동향에 집중" 요청 - 블룸버그 테크·마켓워치 같은
    # 일반 경제 RSS 는 이 단어들이 없으면 반도체·AI 기사도 다른 일반 기사와 섞여 걸러진다.
    "nasdaq", "chip", "semiconductor", "AI", "nvidia", "amd", "tsmc", "broadcom", "micron",
    "earnings", "rate", "fed", "tech stocks", "wall street",
]

# 테마명을 검색어로 쓰기 좋게 다듬는다 (밑줄 제거 등).
THEME_QUERY_FIX = {
    "원자력_전력기기": "원자력 전력기기",
    "AI반도체_HBM": "AI 반도체 HBM",
    "바이오_제약": "바이오 제약",
    "엔터_미디어": "엔터 미디어",
    "우주_위성": "우주 위성",
    "AI_플랫폼": "AI 플랫폼",
}


def _is_market_relevant(title: str) -> bool:
    """MARKET_KEYWORDS 중 하나라도 제목에 있으면(대소문자 무시) 증권·주식시장 관련으로 본다."""
    t = title.lower()
    return any(kw.lower() in t for kw in MARKET_KEYWORDS)


def norm_group(g) -> str:
    """소스마다 group 이름이 조금씩 다르다. 정규화하지 않으면 같은 카테고리가
    두 줄로 갈라진다.
    """
    return GROUP_ALIAS.get(g, g or "기타")


def parse_feed(xml_text: str, source: dict) -> list:
    """RSS 는 defusedxml(내부적으로 xml.etree 를 쓰되 위험한 확장을 차단)로 파싱한다.
    <item> 의 title/link/pubDate/source 만 읽는다.
    """
    try:
        root = _DefusedET.fromstring(xml_text)
    except (ET.ParseError, _DefusedXmlException):
        return []

    items = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pubdate_el = item.find("pubDate")
        source_el = item.find("source")

        title = (title_el.text or "").strip() if title_el is not None else ""
        link = (link_el.text or "").strip() if link_el is not None else ""
        if not title or not link:
            continue
        # ★★★ "속보는 증권이나 주식시장과 관련 있는것으로 필터링해" - 언론사 전체 RSS
        # (require_market_keyword=True 로 표시된 소스, 위 DEFAULT_SOURCES 참고)는 증시와
        # 무관한 기사도 섞여 온다. 구글뉴스 검색 소스는 검색어 자체가 이미 증시로 좁혀져
        # 있어 이 필터를 안 거친다.
        if source.get("require_market_keyword") and not _is_market_relevant(title):
            continue

        published = ""
        if pubdate_el is not None and pubdate_el.text:
            try:
                published = iso(parsedate_to_datetime(pubdate_el.text.strip()))
            except Exception:
                published = ""

        publisher = source.get("publisher", "")
        if source_el is not None and source_el.text:
            publisher = source_el.text.strip()

        uid = hashlib.sha256(f"{source['id']}|{link}".encode("utf-8")).hexdigest()[:16]
        items.append({
            "id": uid, "title": title, "link": link,
            "source_id": source["id"], "source": source["id"],
            "publisher": publisher, "group": norm_group(source.get("group")),
            "published": published,
        })
    return items


class NewsFeed:
    TTL = 240.0  # 같은 피드를 4분 안에 두 번 받지 않는다.
    MIN_INTERVAL = 0.4

    def __init__(self, sources: list, cfg=None):
        self._sources = sources
        self.cfg = cfg
        self._session = netutil.make_session()
        self._cache: dict = {}  # source_id -> (fetched_at, items)
        self._last_call = 0.0
        self._items: list = []
        self._errors: list = []

    def sources(self) -> list:
        return list(self._sources)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.MIN_INTERVAL:
            time.sleep(self.MIN_INTERVAL - elapsed)
        self._last_call = time.monotonic()

    def fetch(self, force: bool = False):
        items: list = []
        errors: list = []
        now = time.monotonic()

        for src in self._sources:
            cached = self._cache.get(src["id"])
            if not force and cached and now - cached[0] < self.TTL:
                items.extend(cached[1])
                continue
            try:
                self._throttle()
                resp = self._session.get(src["url"], timeout=8)
                resp.raise_for_status()
                parsed = parse_feed(resp.text, src)
                self._cache[src["id"]] = (now, parsed)
                items.extend(parsed)
            except Exception as exc:
                errors.append({"source": src["id"], "error": str(exc)})
                if cached:
                    items.extend(cached[1])  # 실패해도 직전에 받은 것은 유지한다.

        items = self._dedup(items)
        self._items = items
        self._errors = errors
        self._persist(items)
        return items, errors

    def _dedup(self, items: list) -> list:
        """같은 제목이 여러 매체에서 오면 하나만 남긴다.
        한글/영숫자만 남긴 앞 40자로 판단한다.
        """
        seen = set()
        out = []
        for it in items:
            key = re.sub(r"[^0-9A-Za-z가-힣]", "", it["title"])[:40]
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
        return out

    def _persist(self, items: list) -> None:
        """★ 저장 필드는 정확히 8개 (STORED_FIELDS)."""
        if self.cfg is None:
            return
        path = os.path.join(self.cfg.state_dir, "news.jsonl")
        os.makedirs(self.cfg.state_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for it in items:
                row = {k: it.get(k, "") for k in STORED_FIELDS}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def read(self, hours: float = 24.0, limit: int | None = None) -> list:
        def _ts(it):
            # ★ 발행 시각이 없는 항목은 정렬에서 맨 뒤로 - 빈 문자열이면
            # 가장 오래된 것처럼 취급돼 위치가 흔들리므로 -1 을 명시적으로 쓴다.
            if not it.get("published"):
                return -1.0
            dt = parse_dt(it["published"])
            return dt.timestamp() if dt else -1.0

        cutoff = now_kst().timestamp() - hours * 3600
        rows = [it for it in self._items if _ts(it) < 0 or _ts(it) >= cutoff]
        rows.sort(key=_ts, reverse=True)
        if limit:
            rows = rows[:limit]
        return rows

    def tag(self, items: list, theme_names: dict | None = None, themes: dict | None = None) -> list:
        """제목에 종목명·테마 키워드가 있으면 붙인다. it["risk"] 도 채운다.
        ★ 문자열 매칭일 뿐이다. 동명이의로 잘못 붙을 수 있다.
        """
        theme_names = theme_names or {}
        themes = themes or {}
        for it in items:
            title = it.get("title", "")
            it["symbols"] = [s for s, name in theme_names.items() if name and name in title]
            it["themes"] = [
                t for t, codes in themes.items()
                if any(theme_names.get(c) and theme_names[c] in title for c in codes)
            ]
            it["risk"] = [w for w in RISK_WORDS if w in title]
        return items

    def risk_map(self, items: list, hours: float = 12.0) -> dict:
        """종목별 위험 신호. mode=="avoid" 일 때 스크리너가 본다."""
        rows = items if items else self.read(hours=hours)
        out: dict = {}
        for it in rows:
            if not it.get("risk"):
                continue
            for s in it.get("symbols", []):
                out.setdefault(s, set()).update(it["risk"])
        return {s: sorted(words) for s, words in out.items()}

    def reactions(self, client, items: list, window_min: int = 30, max_lookups: int = 12) -> list:
        """★ 상관일 뿐 인과가 아니다. 이미 오르던 종목에 기사가 따라붙는 경우가 흔하다."""
        out = []
        checked = 0
        for it in items:
            if checked >= max_lookups:
                break
            symbols = it.get("symbols") or []
            if not symbols or not it.get("published"):
                continue
            for symbol in symbols:
                if checked >= max_lookups:
                    break
                checked += 1
                try:
                    rows = client.prices([symbol])
                    price = rows[0]["price"] if rows else None
                    change_rate = rows[0]["changeRate"] if rows else None
                except Exception:
                    price, change_rate = None, None
                out.append({
                    "id": it["id"], "title": it["title"], "symbol": symbol,
                    "published": it["published"], "window_min": window_min,
                    "price_after": price, "change_rate_after": change_rate,
                })
        return out

    def flow(self, items: list | None = None, hours: float = 12.0) -> dict:
        """어느 테마에 기사가 몰렸나."""
        rows = items if items is not None else self.read(hours=hours)
        counts: dict = {}
        for it in rows:
            for t in it.get("themes", []):
                counts[t] = counts.get(t, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))

    def grouped(self, items: list, per_group: int = 5) -> list:
        """★ 이것이 화면의 기본 표시 방식이다. 빼먹지 마라.
        헤드라인 수백 건을 한 줄로 늘어놓으면 아무것도 읽히지 않는다.
        장중에 실제로 보는 것은 카테고리마다 맨 위 몇 건뿐이다.
        나머지는 접어 두고 필요할 때 펼친다.
        """
        by_group: dict = {}
        for it in items:
            by_group.setdefault(it.get("group", "기타"), []).append(it)

        ordered = [g for g in GROUP_ORDER if g in by_group]
        ordered += [g for g in by_group if g not in GROUP_ORDER]

        out = []
        for g in ordered:
            rows = by_group[g]
            out.append({
                "group": g, "total": len(rows),
                "top": rows[:per_group], "rest": rows[per_group:per_group + 25],
                "risk": sum(1 for r in rows if r.get("risk")),
            })
        return out

    def self_test(self) -> dict:
        items, errors = self.fetch(force=True)
        return {
            "ok": len(errors) < len(self._sources), "sources": len(self._sources),
            "items": len(items), "errors": errors,
        }


def build_feed(cfg) -> NewsFeed:
    """설정에서 소스 목록을 만들어 조립한다."""
    sources = list(DEFAULT_SOURCES)
    if getattr(cfg.news, "theme_queries", False):
        try:
            from daytrader.screener import load_themes
            themes = load_themes(cfg.themes_file)
            for name in themes:
                query = THEME_QUERY_FIX.get(name, name.replace("_", " "))
                sources.append({
                    "id": f"theme_{name}", "group": "테마", "publisher": "구글뉴스",
                    "url": _google_news_url(query),
                })
        except Exception:
            pass
    return NewsFeed(sources, cfg=cfg)
