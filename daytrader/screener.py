"""★ 이 파일에서 가장 중요한 것: 선정 과정을 숨기지 않는 것.
후보 목록만 보여주면 사용자는 "왜 이 종목이지?"를 알 수 없고, 더 나쁘게는
"왜 저 종목은 빠졌지?"를 영영 알 수 없다.
그래서 pick() 은 후보만 돌려주지 않고 SelectionReport 를 만든다.
화면은 이걸 그대로 표로 그리기만 하면 된다.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import asdict, dataclass, field

import yaml

from daytrader.timeutil import iso, now_kst


def load_themes(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("themes", {})
    return {name: [str(code).zfill(6) for code in codes] for name, codes in raw.items()}


def load_theme_names(path: str) -> dict:
    """★★ themes.yaml 은 각 종목코드 옆에 `# 종목명` 주석을 달아 두는 걸
    약속으로 삼고 있다(파일 머리말 참고: "여러 모듈이 이 주석을 읽어
    종목명으로 씁니다") - 그런데 load_themes() 는 표준 yaml.safe_load 를
    써서 주석을 그냥 버리고 있었다(실제로 발견한 버그). 화면에 종목코드만
    나오고 이름이 안 보이던 원인이 이것이다. ruamel.yaml 로 다시 읽어
    인라인 주석에서 이름을 뽑아낸다. load_themes() 자체의 반환 타입은
    다른 코드(스크리닝 등)가 이미 의존하고 있어 그대로 두고, 이름 조회는
    이 별도 함수로 분리한다.
    """
    from ruamel.yaml import YAML

    yaml_loader = YAML()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml_loader.load(f) or {}
    raw = data.get("themes", {})

    names: dict = {}
    for _theme_name, codes in raw.items():
        if codes is None:
            continue
        for i, code in enumerate(codes):
            code_str = str(code).zfill(6) if str(code).isdigit() else str(code)
            comment_name = None
            ca_items = getattr(codes, "ca", None)
            if ca_items is not None:
                entry = ca_items.items.get(i)
                if entry:
                    for token in entry:
                        if token is not None and getattr(token, "value", None):
                            # ★ 토큰 값은 "  # 두산에너빌리티\n" 형태 - 기호와 개행을 벗겨낸다.
                            comment_name = token.value.lstrip("#").strip()
                            break
            if comment_name:
                names[code_str] = comment_name
    return names


@dataclass
class MarketRow:
    symbol: str
    last_price: float
    change_rate: float
    trading_amount: float


@dataclass
class MemberView:
    """테마 구성 종목 하나 = 화면의 한 줄."""

    symbol: str
    name: str
    last_price: float = 0.0
    change_rate: float = 0.0
    trading_amount: float = 0.0
    in_market: bool = False  # 오늘 시장 스냅샷에 잡혔는지
    is_up: bool = False  # 동반 상승으로 인정됐는지
    reason: str = ""  # 인정 안 됐으면 왜
    selected: bool = False  # 최종 매매 대상인지
    reject: str = ""  # 후보에서 탈락했으면 사유
    rank_in_theme: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ThemeView:
    name: str
    score: float
    breadth: int
    intensity: float
    amount: float
    liquidity: float
    news_boost: float
    qualified: bool
    reason: str
    rank: int
    picked: bool
    members: list  # list[MemberView]

    @property
    def formula(self) -> str:
        """점수가 어떤 숫자에서 나왔는지 한 줄로. 화면에 그대로 뜬다."""
        amount_eok = self.amount / 1e8
        return (
            f"{self.breadth}종목 × 중앙 {self.intensity*100:+.2f}% × "
            f"log(대금 {amount_eok:,.0f}억)={self.liquidity:.2f} = {self.score:.3f}"
        )

    def to_dict(self) -> dict:
        d = asdict(self)  # dataclasses.asdict 는 members(list[MemberView])도 재귀적으로 dict 화한다.
        d["formula"] = self.formula
        return d


@dataclass
class Candidate:
    symbol: str
    name: str
    theme: str
    last_price: float
    change_rate: float
    trading_amount: float
    theme_score: float
    theme_rank: int
    rank_in_theme: int
    theme_breadth: int
    theme_intensity: float
    why: str  # ★ 이 종목이 뽑힌 이유 (한 문장)


@dataclass
class SelectionReport:
    at: str
    market_size: int
    criteria: dict
    themes: list  # list[ThemeView]
    candidates: list  # list[Candidate]
    summary: str
    errors: list

    def to_dict(self) -> dict:
        return {
            "at": self.at,
            "market_size": self.market_size,
            "criteria": self.criteria,
            "themes": [t.to_dict() for t in self.themes],
            "candidates": [asdict(c) for c in self.candidates],
            "summary": self.summary,
            "errors": self.errors,
        }


class Screener:
    def __init__(self, client, cfg, journal=None, news_flow=None, names=None, news_risk=None):
        self.client = client
        self.cfg = cfg
        self.journal = journal
        self.news_flow = news_flow
        self.news_risk = news_risk or {}  # symbol -> [위험어, ...] (mode=="avoid" 일 때만 쓴다)
        self.themes = load_themes(cfg.themes_file)
        self.names = names or {}
        self._meta_cache: dict[str, tuple] = {}  # symbol -> (data, fetched_at_monotonic)

    def name_of(self, symbol: str) -> str:
        return self.names.get(symbol, symbol)

    def _ensure_names(self, symbols: list) -> None:
        """★ self.names 에 없는 종목명을 한 번에 배치로 채운다.
        이걸 안 하면 name_of() 는 캐시에 없는 종목의 코드를 그대로
        돌려준다 - 화면에 종목명 대신 6자리 코드가 그대로 나오는 원인이었다.
        """
        missing = [s for s in symbols if s not in self.names]
        if not missing:
            return
        meta = self._meta(missing)
        for sym, m in meta.items():
            name = m.get("name")
            if name:
                self.names[sym] = name

    def _meta(self, symbols) -> dict:
        """종목 메타(상장상태·우선주·ETF 등)를 TTL 캐시로 가져온다.
        ★ 캐시에 TTL(meta_cache_minutes). 없으면 상장폐지나 종목명 변경이
        영원히 반영되지 않는다.
        """
        ttl = self.cfg.screen.meta_cache_minutes * 60
        now = time.monotonic()
        need = [s for s in symbols if s not in self._meta_cache or now - self._meta_cache[s][1] > ttl]
        if need:
            try:
                rows = self.client.stocks(need)
            except Exception:
                rows = []
            # ★ 위와 같은 이유 - 딕셔너리가 아닌 항목은 건너뛴다.
            by_symbol = {r.get("symbol"): r for r in (rows or []) if isinstance(r, dict)}
            for s in need:
                self._meta_cache[s] = (by_symbol.get(s, {}), now)
        return {s: self._meta_cache[s][0] for s in symbols if s in self._meta_cache}

    def market_snapshot(self, errors: list | None = None) -> dict:
        """MARKET_TRADING_AMOUNT(realtime) + TOP_GAINERS(1d) 를 합친다.
        겹치면 거래대금이 큰 쪽을 채택한다. 조회 실패는 warning 후 건너뛴다.
        """
        rows: dict[str, MarketRow] = {}

        def _ingest(kind: str, duration: str) -> None:
            try:
                data = self.client.rankings(
                    type=kind, marketCountry="KR", duration=duration,
                    count=self.cfg.screen.ranking_count,
                )
            except Exception as exc:
                if errors is not None:
                    errors.append(f"{kind}({duration}) 조회 실패: {exc}")
                return
            for r in data or []:
                # ★ 랭킹 응답에 딕셔너리가 아닌 항목이 섞여도 죽지 않게.
                if not isinstance(r, dict):
                    continue
                symbol = str(r.get("symbol", "")).zfill(6)
                if not symbol.strip("0"):
                    continue
                # ★★★ 실제로 겪은 버그 - 토스 랭킹 API 응답은 가격 정보를
                # 최상위가 아니라 "price": {"lastPrice", "basePrice",
                # "changeRate"} 로 한 단계 감싸서 준다. 예전 코드는
                # r.get("price")를 그대로 last_price 에 넣어서(그 값이
                # dict라 늘 참으로 평가됨) MarketRow.last_price 가 숫자가
                # 아니라 dict가 됐고, r.get("changeRate")는 최상위에 없어
                # 항상 None → 0.0 으로 떨어졌다. 그 결과 모든 종목의
                # 등락률이 0%로 보여 "+3% 이상 동반 상승" 테마 조건을
                # 아무 종목도 통과하지 못했다 - 시장이 조용한 게 아니라
                # 이 파싱 버그 때문에 매번 종목 선정이 실패한 것이었다.
                # 혹시 다른 랭킹 타입이 예전처럼 평평한 구조로 줄 수도
                # 있으니, 중첩 dict가 아니면 기존 방식으로도 읽는다.
                price_obj = r.get("price")
                if isinstance(price_obj, dict):
                    price = price_obj.get("lastPrice") or 0.0
                    change = price_obj.get("changeRate") or 0.0
                else:
                    price = price_obj or r.get("lastPrice") or 0.0
                    change = r.get("changeRate") or 0.0
                amount = r.get("tradingAmount") or r.get("tradingValue") or 0.0
                # ★ 위 값들이 전부 문자열로 올 수 있다(실제로 그렇다) - 숫자
                # 비교(아래 amount > cur.trading_amount, 화면의 가격 필터)가
                # 문자열끼리의 사전식 비교로 조용히 틀어지지 않게 변환한다.
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    price = 0.0
                try:
                    change = float(change)
                except (TypeError, ValueError):
                    change = 0.0
                try:
                    amount = float(amount)
                except (TypeError, ValueError):
                    amount = 0.0
                cur = rows.get(symbol)
                if cur is None or amount > cur.trading_amount:
                    rows[symbol] = MarketRow(symbol=symbol, last_price=price, change_rate=change, trading_amount=amount)

        _ingest("MARKET_TRADING_AMOUNT", "realtime")
        _ingest("TOP_GAINERS", "1d")
        return rows

    def criteria(self) -> dict:
        s = self.cfg.screen
        text = [
            f"테마로 인정: 구성 종목 {s.min_theme_members_up}개 이상이 +{s.min_change_rate*100:.0f}% 이상 동반 상승",
            "테마 점수 = 동반상승 종목수 × 상승률 중앙값 × log10(거래대금)",
            f"상위 {s.top_themes}개 테마에서 테마당 {s.candidates_per_theme}종목까지",
            f"가격 {s.min_price:,}~{s.max_price:,}원",
            f"등락률 +{s.min_change_rate*100:.0f}%~+{s.max_change_rate*100:.0f}% (더 오른 종목은 추격매수라 제외)",
            f"거래대금 {s.min_trading_amount/1e8:,.0f}억 이상",
        ]
        if s.exclude_preferred or s.exclude_etf_etn:
            text.append("우선주·ETF·ETN·레버리지 제외")
        text.append("정리매매·투자경고·투자위험·단기과열 종목 제외")

        news_labels = {
            "off": "꺼짐(속보를 보지 않습니다)",
            "view": "참고용으로만 표시(매매 판단에 개입하지 않습니다)",
            "avoid": "위험 신호가 있는 종목은 후보에서 제외",
            "boost": "테마 점수에 가점 반영",
        }
        news_mode = self.cfg.news.mode
        text.append(f"속보 매매 개입: {news_labels.get(news_mode, news_mode)}")

        return {
            "min_price": s.min_price, "max_price": s.max_price,
            "min_change_rate": s.min_change_rate, "max_change_rate": s.max_change_rate,
            "min_trading_amount": s.min_trading_amount,
            "min_theme_members_up": s.min_theme_members_up,
            "top_themes": s.top_themes, "candidates_per_theme": s.candidates_per_theme,
            "text": text,
        }

    def score_themes(self, market: dict) -> list:
        """★ 모든 테마를 평가한다. 탈락한 테마도 왜 탈락했는지와 함께 남긴다."""
        s = self.cfg.screen
        views: list[ThemeView] = []

        # ★★ name_of() 는 예전엔 생성자에 넘긴 names 딕셔너리만 봤는데,
        # 실제로는 아무도 그 파라미터를 넘기지 않아 화면에 종목명 대신
        # 코드가 그대로 나왔다(실제로 겪은 버그). 여기서 테마에 속한 전체
        # 종목명을 한 번에 배치로 채워 둔다 - 종목마다 따로 조회하면
        # "시세는 배치로 받는다"는 원칙에 어긋난다.
        all_symbols = [sym for symbols in self.themes.values() for sym in symbols]
        self._ensure_names(all_symbols)

        for theme_name, symbols in self.themes.items():
            members: list[MemberView] = []
            up_rates: list[float] = []
            up_amounts: list[float] = []

            for symbol in symbols:
                row = market.get(symbol)
                in_market = row is not None
                change_rate = row.change_rate if row else 0.0
                last_price = row.last_price if row else 0.0
                trading_amount = row.trading_amount if row else 0.0
                is_up = in_market and change_rate >= s.min_change_rate

                if is_up:
                    reason = ""
                elif not in_market:
                    reason = "시장 스냅샷에 없음"
                else:
                    reason = f"상승률 {change_rate*100:+.2f}%로 기준 {s.min_change_rate*100:.0f}% 미달"

                members.append(MemberView(
                    symbol=symbol, name=self.name_of(symbol), last_price=last_price,
                    change_rate=change_rate, trading_amount=trading_amount,
                    in_market=in_market, is_up=is_up, reason=reason,
                ))
                if is_up:
                    up_rates.append(change_rate)
                    up_amounts.append(trading_amount)

            breadth = len(up_rates)
            if breadth < s.min_theme_members_up:
                # ★★★ 시세를 하나도 못 받았을 때(market 이 비어 이 테마의
                # 어떤 종목도 스냅샷에 없음) "한 종목만 튀는 건 테마가
                # 아니다"라고 안내하면, 실제로는 데이터가 없는 건데 시장을
                # 평가한 결과인 줄 오해하게 된다 - 두 경우를 구분한다.
                any_in_market = any(m.in_market for m in members)
                if not any_in_market:
                    reason = "이 테마 종목의 시세를 하나도 받아오지 못했습니다 - 시장 판단이 아니라 데이터 부재입니다"
                else:
                    reason = (
                        f"동반 상승 {breadth}종목 < 기준 {s.min_theme_members_up}종목 - "
                        "한 종목만 튀는 건 테마가 아니라 개별 이슈로 봅니다"
                    )
                views.append(ThemeView(
                    name=theme_name, score=0.0, breadth=breadth, intensity=0.0,
                    amount=sum(up_amounts), liquidity=0.0, news_boost=0.0,
                    qualified=False,
                    reason=reason,
                    rank=0, picked=False, members=members,
                ))
                continue

            intensity = statistics.median(up_rates)
            amount = sum(up_amounts)
            liquidity = max(math.log10(max(amount, 1e8) / 1e8), 0.1)
            score = breadth * intensity * liquidity

            boost = 0.0
            if self.news_flow is not None:
                try:
                    boost = self.news_flow.boost_for(theme_name) or 0.0
                except Exception:
                    boost = 0.0
            if boost:
                score *= (1 + boost)

            # 테마 안에서 거래대금 순위(rank_in_theme)를 매겨 대장주를 표시한다.
            ranked = sorted([m for m in members if m.is_up], key=lambda m: m.trading_amount, reverse=True)
            for i, m in enumerate(ranked, start=1):
                m.rank_in_theme = i

            views.append(ThemeView(
                name=theme_name, score=score, breadth=breadth, intensity=intensity,
                amount=amount, liquidity=liquidity, news_boost=boost, qualified=True,
                reason="", rank=0, picked=False, members=members,
            ))

        qualified = sorted([v for v in views if v.qualified], key=lambda v: v.score, reverse=True)
        unqualified = sorted([v for v in views if not v.qualified], key=lambda v: v.breadth, reverse=True)

        for i, v in enumerate(qualified, start=1):
            v.rank = i
            v.picked = i <= s.top_themes

        return qualified + unqualified

    def _hard_filter(self, row: MarketRow) -> str | None:
        """가격범위/상승률/과열/거래대금."""
        s = self.cfg.screen
        if not (s.min_price <= row.last_price <= s.max_price):
            return f"가격 {row.last_price:,.0f}원이 허용 범위({s.min_price:,}~{s.max_price:,}원) 밖입니다."
        if row.change_rate < s.min_change_rate:
            return f"상승률 {row.change_rate*100:+.2f}%로 기준 {s.min_change_rate*100:.0f}% 미달입니다."
        if row.change_rate > s.max_change_rate:
            return f"상승률 {row.change_rate*100:+.2f}%로 기준 {s.max_change_rate*100:.0f}%를 넘어 추격매수 구간입니다."
        if row.trading_amount < s.min_trading_amount:
            return f"거래대금 {row.trading_amount/1e8:,.1f}억으로 기준 {s.min_trading_amount/1e8:,.0f}억 미달입니다."
        return None

    def _news_filter(self, symbol: str) -> str | None:
        """속보 위험 신호. mode=="avoid" 일 때만 작동한다.
        _meta_filter 보다 먼저 검사한다 - 문자열 매칭이라 완벽하지 않지만,
        틀렸을 때의 대가(놓친 기회)가 손실의 대가보다 작다.
        """
        if self.cfg.news.mode != "avoid":
            return None
        words = self.news_risk.get(symbol)
        if words:
            return f"속보 위험 신호 ({', '.join(words[:3])})"
        return None

    def _meta_filter(self, symbol: str) -> str | None:
        """상장상태/우선주/ETF/레버리지/매수유의.
        ★ 유의사항 조회가 실패하면 안전하게 제외한다 - 모르면 사지 않는다.
        """
        s = self.cfg.screen
        meta = self._meta([symbol]).get(symbol, {})

        if meta.get("delisted") or meta.get("tradingHalt"):
            return "상장폐지 또는 거래정지 종목입니다."
        if s.exclude_preferred and meta.get("isPreferred"):
            return "우선주는 제외합니다."
        if s.exclude_etf_etn and (meta.get("isETF") or meta.get("isETN")):
            return "ETF/ETN 은 제외합니다."
        if meta.get("isLeveraged") or meta.get("isInverse"):
            return "레버리지·인버스 상품은 제외합니다."

        try:
            warns = self.client.warnings(symbol)
        except Exception:
            return "유의사항 조회에 실패해 안전하게 제외합니다."

        warn_types = {w.get("type") for w in (warns or [])}
        excluded = set(s.exclude_warnings) & warn_types
        if excluded:
            return f"거래소 지정 경고({', '.join(sorted(excluded))})로 제외합니다."
        return None

    def build_report(self) -> SelectionReport:
        errors: list[str] = []
        market = self.market_snapshot(errors=errors)
        themes = self.score_themes(market)
        picked_themes = [t for t in themes if t.picked]
        s = self.cfg.screen

        # 1차 필터(호출 없음) - 하드 필터만으로 걸러낸다.
        # ★ 테마별로 따로 담는다 - 아래에서 공평하게 배분하려면 출처가 필요하다.
        by_theme: dict = {}
        for theme in picked_themes:
            up_members = sorted([m for m in theme.members if m.is_up], key=lambda m: m.trading_amount, reverse=True)
            bucket = []
            for m in up_members:
                row = market.get(m.symbol)
                reason = self._hard_filter(row) if row else "시장 스냅샷에 없습니다."
                if reason:
                    m.reject = reason
                    self._journal_reject(m.symbol, theme.name, reason)
                    continue
                bucket.append((theme, m))
            if bucket:
                by_theme[theme.name] = bucket

        # ★★★ 실제로 발견한 결함 - 예전엔 모든 테마의 후보를 테마 순서대로
        # 한 리스트에 쌓은 뒤 앞에서부터 limit 만큼 잘랐다. 그러면 1위 테마에
        # 상승 종목이 많을 때(흔한 상황) 2·3위 테마가 통째로 잘려 나가서,
        # "여러 테마에 분산한다"는 top_themes 설정이 무력화되고 한 테마에
        # 전부 몰렸다. 테마를 번갈아 가며(라운드로빈) 뽑아서, 어떤 테마도
        # 통째로 사라지지 않게 한다.
        limit = s.top_themes * s.candidates_per_theme * 3
        theme_candidates: list[tuple] = []
        idx = 0
        while len(theme_candidates) < limit and by_theme:
            progressed = False
            for name in list(by_theme.keys()):
                bucket = by_theme[name]
                if idx < len(bucket):
                    theme_candidates.append(bucket[idx])
                    progressed = True
                    if len(theme_candidates) >= limit:
                        break
            if not progressed:
                break
            idx += 1

        # ★★★ 성능 개선 - _meta_filter() 는 종목마다 self._meta([symbol]) 을
        # 부르는데, 캐시에 없으면 그때마다 stocks() API 를 한 번씩 호출한다
        # (후보 18개면 최악의 경우 18번). 여기서 후보 전체의 메타를 한 번에
        # 배치로 받아 캐시를 데워 두면, 아래 루프의 _meta() 는 전부 캐시
        # 적중이라 추가 호출이 0 이 된다("시세는 배치로 받는다"는 원칙과 동일).
        if theme_candidates:
            self._meta([m.symbol for _, m in theme_candidates])

        candidates: list[Candidate] = []
        per_theme_count: dict[str, int] = {}
        for theme, m in theme_candidates:
            if per_theme_count.get(theme.name, 0) >= s.candidates_per_theme:
                continue
            reason = self._news_filter(m.symbol) or self._meta_filter(m.symbol)
            if reason:
                m.reject = reason
                self._journal_reject(m.symbol, theme.name, reason)
                continue

            m.selected = True
            per_theme_count[theme.name] = per_theme_count.get(theme.name, 0) + 1

            why = (
                f"'{theme.name}' 테마가 오늘 {theme.rank}위입니다"
                f"({theme.breadth}종목 동반 상승, 중앙값 {theme.intensity*100:+.2f}%, "
                f"대금 {theme.amount/1e8:,.0f}억). 그 안에서 이 종목은 "
                f"{'대장주' if m.rank_in_theme == 1 else f'{m.rank_in_theme}위'}이고, "
                f"현재 {m.last_price:,.0f}원({m.change_rate*100:+.2f}%), "
                f"거래대금 {m.trading_amount/1e8:,.0f}억으로 "
                "가격대·상승률·거래대금·유의종목 필터를 모두 통과했습니다."
            )

            if self.journal is not None:
                try:
                    self.journal.log("pick", symbol=m.symbol, name=m.name, theme=theme.name, explain=why)
                except Exception:
                    pass
            candidates.append(Candidate(
                symbol=m.symbol, name=m.name, theme=theme.name,
                last_price=m.last_price, change_rate=m.change_rate, trading_amount=m.trading_amount,
                theme_score=theme.score, theme_rank=theme.rank, rank_in_theme=m.rank_in_theme,
                theme_breadth=theme.breadth, theme_intensity=theme.intensity, why=why,
            ))

        # ★★★ 직접 추가한 관심 종목은 테마와 별개로 거래 대상에 함께 넣는다(사용자 요청). 테마·상승률 조건은
        # 보지 않고, 거래정지·경고 같은 안전 조건만 확인한다. 실제 매수는 그 뒤 매매 기법 판정을 통과해야 한다.
        candidates += self._watchlist_candidates(market, {c.symbol for c in candidates})

        if candidates:
            theme_names_used = sorted({c.theme for c in candidates})
            summary = f"{len(candidates)}개 후보를 찾았습니다 ({', '.join(theme_names_used)})."
        elif not market:
            # ★★★ 실제로 겪은 혼란 - 시세를 하나도 못 받아왔을 때도
            # "오늘은 테마가 없습니다, 거래 없음이 정상입니다"라고 안내해서,
            # 실제로는 API 키 미등록·네트워크 장애인데 정상 상황인 줄 알게
            # 됐다. 이 둘은 완전히 다른 상황이니 반드시 구분해서 알린다.
            # ★★★ 실제로 겪은 혼란 - "토스 API 를 연계했는데도 시세를 못
            # 가져온다"는 문의. 원인이 셋으로 갈리는데 메시지가 둘만
            # 구분하고 있었다:
            #   ① mode 가 sim/replay - 토스 키와 무관하게 가짜 시장을 쓴다.
            #      키를 아무리 등록해도 실제 시세는 안 온다(의도된 동작).
            #   ② 키 미등록 - 랭킹을 받을 수 없다.
            #   ③ 키는 있는데 네트워크·권한 문제.
            mode = getattr(self.cfg, "mode", "")
            has_keys = bool(getattr(self.cfg, "client_id", "") and getattr(self.cfg, "client_secret", ""))
            if mode in ("sim", "replay"):
                summary = (
                    f"지금은 '{mode}' 모드라 실제 시세가 아닌 가상 시장으로 돌고 있습니다 - "
                    "토스 API 키를 등록했더라도 이 모드에서는 실제 시세를 쓰지 않습니다. "
                    "실제 시세로 종목을 선정하려면 [설정] → 국내주식에서 모드를 "
                    "'web(관찰)' 또는 'paper(모의매매)'로 바꾸세요."
                )
            elif not has_keys:
                summary = (
                    "시세를 하나도 받아오지 못해 종목을 선정할 수 없습니다 - 오늘 시장이 조용한 게 아니라 "
                    "토스 API 키가 등록되지 않은 상태입니다. 종목 선정의 재료인 거래대금·급등 랭킹은 "
                    "토스 API 로만 받을 수 있어, 키가 없으면 후보를 만들 수 없습니다. "
                    "[준비·연결]에서 키를 등록하세요."
                )
            else:
                summary = (
                    "시세를 하나도 받아오지 못해 종목을 선정할 수 없습니다 - 오늘 시장이 조용한 게 아니라 "
                    "데이터 자체가 없는 상태입니다. 키는 등록돼 있으니 [준비·연결]의 '연결 진단'에서 "
                    "차단된 항목이 있는지, '연계 테스트'의 거래대금 랭킹이 통과하는지 확인하세요."
                )
            if errors:
                summary += f" (조회 오류: {errors[0]})"
        elif not picked_themes:
            # ★★★ "종목 선정을 안 하는 것 같다"는 문의 - "테마가 없습니다"
            # 만으로는 기준에 얼마나 못 미쳤는지 알 수 없어 고장으로
            # 오해하게 된다. 오늘 가장 많이 오른 종목과 기준을 함께 보여
            # 주면 "시장이 조용한 것"임을 바로 납득할 수 있다.
            # ★★★ 실제로 겪은 버그 - market_snapshot() 은 거래소 전체 등락률 순위
            # (TOP_GAINERS)를 그대로 담고 있어서, themes.yaml 에 없는 워런트·ELW·
            # 신규상장 같은 종목(예: "0010S0" +288.33%)이 "가장 많이 오른 종목"으로
            # 뽑히곤 했다. 그런데 아래 문장은 무조건 "상승 인정 기준에 못 미칩니다"라고
            # 적어서, 기준(3%)보다 훨씬 큰 값을 보여주면서 "기준 미달"이라 말하는 앞뒤가
            # 안 맞는 문장이 됐다("상승률이 기준보다 훨씬 높은데 왜 미달이라고 하냐"는
            # 혼란의 원인). 테마 후보가 될 수 있는 건 애초에 themes.yaml 에 등록된
            # 종목뿐이므로, "가장 많이 오른 종목"도 그 안에서만 찾는다 - 그래야 실제
            # "동반 상승" 판정과 앞뒤가 맞는 이야기가 된다.
            known = {code for codes in self.themes.values() for code in codes}
            best = None
            for row in market.values():
                sym = getattr(row, "symbol", "")
                if sym not in known:
                    continue
                rate = getattr(row, "change_rate", None)
                if rate is not None and (best is None or rate > best[0]):
                    label = self.name_of(sym) or sym
                    best = (rate, label)
            detail = ""
            if best and best[0] < s.min_change_rate:
                detail = (f" 오늘 관심 종목 중 가장 많이 오른 것이 {best[1]} {best[0]*100:+.2f}%로, "
                          f"상승 인정 기준({s.min_change_rate*100:.0f}%)에 못 미칩니다.")
            elif best:
                # ★ 개별 종목은 기준을 넘겼어도, 같은 테마 안에서 함께 오른 종목 수가
                #   min_theme_members_up 에 못 미치면 "동반 상승"으로 인정되지 않는다 -
                #   이때는 "기준 미달"이 아니라 "혼자만 올랐다"가 진짜 이유다.
                detail = (f" {best[1]}({best[0]*100:+.2f}%)처럼 기준을 넘겨 오른 종목이 있어도, "
                          f"같은 테마에서 함께 오른 종목이 {s.min_theme_members_up}개에 못 미쳤습니다.")
            summary = ("오늘은 테마로 인정될 만큼 동반 상승한 종목 묶음이 없습니다."
                       + detail + " 조건이 안 맞는 날 안 하는 것이 규칙입니다.")
        else:
            summary = "테마는 있었지만 가격·상승률·거래대금·유의종목 조건을 통과한 종목이 없습니다."

        # ★★★ "토스 API 를 연계했는데도 시세를 못 가져온다"는 혼란의 핵심 -
        # sim/replay 는 실제 시세가 아니라 가상 시장으로 돈다. 시세가
        # 정상으로 들어와도(가짜 데이터라서) 이 사실을 모르면 "왜 실제
        # 시장과 다르냐"고 계속 헷갈린다. 어떤 결과가 나오든 항상 밝힌다.
        if getattr(self.cfg, "mode", "") in ("sim", "replay") and market:
            summary = f"[가상 시장 · {self.cfg.mode} 모드] " + summary

        if self.journal is not None:
            try:
                top2 = [f"{t.name}({t.score:.3f})" for t in themes if t.qualified][:2]
                self.journal.log("theme_scan", top_themes=top2, total_themes=len(themes))
            except Exception:
                pass

        return SelectionReport(
            at=iso(now_kst()), market_size=len(market), criteria=self.criteria(),
            themes=themes, candidates=candidates, summary=summary, errors=errors,
        )

    WATCH_THEME = "관심종목"

    def _watchlist_candidates(self, market: dict, have: set) -> list:
        raw = getattr(self.cfg.screen, "watchlist", None) or []
        symbols: list = []
        for x in raw:
            code = str(x).strip()
            if not code:
                continue
            code = code.zfill(6) if code.isdigit() else code.upper()
            if code not in symbols and code not in have:
                symbols.append(code)
        if not symbols:
            return []
        self._ensure_names(symbols)
        # 랭킹에 안 잡힌 종목은 시세를 따로 받는다.
        quotes: dict = {}
        missing = [c for c in symbols if c not in market]
        if missing:
            try:
                for r in self.client.prices(missing) or []:
                    if isinstance(r, dict):
                        quotes[str(r.get("symbol", "")).zfill(6)] = r
            except Exception:
                quotes = {}
        out: list = []
        for code in symbols:
            row = market.get(code)
            if row is not None:
                price, change, amount = row.last_price, row.change_rate, row.trading_amount
            else:
                q = quotes.get(code) or {}
                try:
                    price = float(q.get("price") or q.get("lastPrice") or 0)
                    change = float(q.get("changeRate") or 0)
                    amount = float(q.get("tradingAmount") or q.get("tradingValue") or 0)
                except (TypeError, ValueError):
                    price = change = amount = 0.0
            if not price:
                self._journal_reject(code, self.WATCH_THEME, "시세를 받지 못했습니다.")
                continue
            reason = self._news_filter(code) or self._meta_filter(code)
            if reason:
                self._journal_reject(code, self.WATCH_THEME, reason)
                continue
            name = self.name_of(code)
            why = (f"직접 추가한 관심 종목입니다(테마와 무관하게 거래 대상). 현재 {price:,.0f}원({change*100:+.2f}%) - "
                   "거래정지·경고 종목이 아니며, 실제 매수는 매매 기법 판정을 통과할 때만 합니다.")
            if self.journal is not None:
                try:
                    self.journal.log("pick", symbol=code, name=name, theme=self.WATCH_THEME, explain=why)
                except Exception:
                    pass
            out.append(Candidate(
                symbol=code, name=name, theme=self.WATCH_THEME, last_price=price, change_rate=change,
                trading_amount=amount, theme_score=0.0, theme_rank=0, rank_in_theme=0,
                theme_breadth=0, theme_intensity=0.0, why=why,
            ))
        return out

    def _journal_reject(self, symbol: str, theme: str, reason: str) -> None:
        if self.journal is None:
            return
        try:
            self.journal.log("reject", symbol=symbol, theme=theme, reason=reason)
        except Exception:
            pass

    def pick(self) -> list:
        return self.build_report().candidates

    def refresh_prices(self, report: SelectionReport | None = None) -> SelectionReport:
        """★ 시세만 갱신한다. 30초마다 전체 스크리닝을 다시 돌리면 API 한도를 태운다."""
        if report is None:
            return self.build_report()

        symbols = [c.symbol for c in report.candidates]
        if not symbols:
            return report
        try:
            fresh = self.client.prices(symbols)
        except Exception:
            return report

        # ★★★ 실제로 겪은 버그("'str' object has no attribute 'get'") -
        # 시세 응답이 항상 딕셔너리 리스트라고 가정하고 r.get() 을 바로
        # 불렀는데, 소스에 따라 문자열이나 다른 형태가 섞여 오면 그 자리에서
        # 죽어 화면 전체가 안 열렸다. 딕셔너리인 항목만 골라 쓴다.
        by_symbol = {}
        for r in (fresh or []):
            if not isinstance(r, dict):
                continue
            by_symbol[str(r.get("symbol", "")).zfill(6)] = r
        for c in report.candidates:
            r = by_symbol.get(c.symbol)
            if not r:
                continue
            c.last_price = r.get("price") or r.get("lastPrice") or c.last_price
            c.change_rate = r.get("changeRate", c.change_rate)
        return report

    def discover(self, count: int = 40) -> list:
        """themes.yaml 관리용. 오늘 급등했지만 어느 테마에도 없는 종목을 찾아준다.
        여기 나온 종목을 보고 themes.yaml 에 채워 넣으면 프로그램이 다음부터 잡을 수 있다.
        """
        known = {code for codes in self.themes.values() for code in codes}
        try:
            rows = self.client.rankings(
                type="TOP_GAINERS", marketCountry="KR", duration="1d", count=count,
            )
        except Exception:
            return []

        out = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            symbol = str(r.get("symbol", "")).zfill(6)
            if not symbol.strip("0") or symbol in known:
                continue
            # ★ market_snapshot() 에서 고친 것과 같은 버그 - 랭킹 응답의
            # 등락률은 최상위 "changeRate"가 아니라 "price"."changeRate"에 있다.
            price_obj = r.get("price")
            change = price_obj.get("changeRate") if isinstance(price_obj, dict) else r.get("changeRate")
            try:
                change = float(change) if change is not None else 0.0
            except (TypeError, ValueError):
                change = 0.0
            try:
                amount = float(r.get("tradingAmount") or r.get("tradingValue") or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
            out.append({
                "symbol": symbol,
                "name": r.get("name", self.name_of(symbol)),
                "change_rate": change,
                "trading_amount": amount,
            })
        return out
