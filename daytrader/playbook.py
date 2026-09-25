from __future__ import annotations

from dataclasses import dataclass, field
from math import isnan

from daytrader.signals import (
    adx,
    atr,
    body_ratio,
    consecutive_down,
    dry_up_ratio,
    ema,
    highest_high,
    range_of,
    relative_strength,
    rsi,
    sma,
    slope,
    tight_range_pct,
    volume_ratio,
    vwap,
)
from daytrader.timeutil import iso, josa, now_kst, parse_hhmm

NAN = float("nan")


# ━━ 1) Bar 와 oco_levels ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Bar:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float

    @staticmethod
    def from_api(d: dict) -> "Bar":
        """★★★ 실제로 겪은 버그 - API 응답의 가격 필드가 None 이거나 아예
        없을 때 float(None) 에서 죽어 매매가 통째로 멈췄다. 봉 하나가
        불완전하다고 전체를 포기하면 안 된다.
        ★ 값이 없으면 NaN 으로 둔다 - 0 으로 채우면 "가격이 0원"이라는
        잘못된 사실이 되어 판정을 오염시킨다. NaN 은 각 기법이 이미
        "판단 불가"로 처리하고 있다.
        """
        def _f(key):
            v = d.get(key)
            if v is None:
                return NAN
            try:
                return float(v)
            except (TypeError, ValueError):
                return NAN

        return Bar(
            ts=d.get("timestamp", ""),
            open=_f("openPrice"),
            high=_f("highPrice"),
            low=_f("lowPrice"),
            close=_f("closePrice"),
            volume=_f("volume"),
        )

    def to_api(self) -> dict:
        return {
            "timestamp": self.ts,
            "openPrice": self.open,
            "highPrice": self.high,
            "lowPrice": self.low,
            "closePrice": self.close,
            "volume": self.volume,
        }


def oco_levels(entry_price: float, cfg, round_to_tick) -> dict:
    """조건부 OCO 주문에 넣을 4개 가격을 계산한다.
    ★ 트리거에 닿아도 지정가가 안 붙으면 손절이 안 된 것과 같다.
    그래서 주문가를 한 틱 불리하게 잡아 체결 확률을 높인다.
    """
    tp = round_to_tick(entry_price * (1 + cfg.take_profit_pct), "down")
    sl = round_to_tick(entry_price * (1 - cfg.stop_loss_pct), "up")
    return {
        "take_profit_trigger": tp,
        "take_profit_price": round_to_tick(tp * 0.998, "down"),
        "stop_loss_trigger": sl,
        "stop_loss_price": round_to_tick(sl * 0.99, "down"),
    }


# ━━ 2) 자료구조 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _clean_nan(v):
    """nan 은 JSON 파서(Starlette 는 allow_nan=False)를 깨뜨린다 - 화면으로 나가기 전에 None 으로 바꾼다.
    ★ 실제로 겪은 버그: Verdict.inputs 안의 or_high/or_low 가 개장 레인지가 아직 안 찼을 때 nan 인 채로
    그대로 나가서, 그 후보가 섞인 응답 전체가 500 으로 죽었다(종목선정 화면·국내 스냅샷이 통째로 안 뜸)."""
    if isinstance(v, float) and isnan(v):
        return None
    if isinstance(v, dict):
        return {k: _clean_nan(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean_nan(x) for x in v]
    return v


@dataclass
class Term:
    """판정 항목 하나 = 화면 체크리스트 한 줄."""

    key: str
    label: str
    value: float
    threshold: float
    op: str  # ">" ">=" "<" "<=" "between" "bool"
    unit: str = ""  # "원" "배" "%" "봉" "개" "분"
    passed: bool = False
    required: bool = True  # 필수면 하나만 틀려도 전체 실패
    weight: float = 1.0
    margin: float = 0.0
    delta: float | None = None  # ★ 직전 평가 대비 value 변화
    flipped: str | None = None  # ★ "pass"|"fail" - 판정이 뒤집혔으면
    explain: str = ""

    def to_dict(self) -> dict:
        # nan 은 JSON 파서를 깨뜨린다. None 으로 바꿔 내보낸다.
        def clean(v):
            if isinstance(v, float) and isnan(v):
                return None
            return v

        return {
            "key": self.key,
            "label": self.label,
            "value": clean(self.value),
            "threshold": clean(self.threshold),
            "op": self.op,
            "unit": self.unit,
            "passed": self.passed,
            "required": self.required,
            "weight": self.weight,
            "margin": clean(self.margin),
            "delta": clean(self.delta),
            "flipped": self.flipped,
            "explain": self.explain,
        }


@dataclass
class Verdict:
    id: str
    at: str
    symbol: str
    name: str
    theme: str
    phase: str
    technique: str
    technique_label: str
    ok: bool
    score: float
    terms: list  # list[Term]
    blocked_by: list  # list[str]
    headline: str  # 후보 표에 그대로
    narrative: str  # 2~4문장
    changes: list  # ★ 뒤집힌 항목 설명
    inputs: dict  # 재현용 원자료
    price: float = 0.0

    def term(self, key: str) -> Term | None:
        for t in self.terms:
            if t.key == key:
                return t
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "at": self.at,
            "symbol": self.symbol,
            "name": self.name,
            "theme": self.theme,
            "phase": self.phase,
            "technique": self.technique,
            "technique_label": self.technique_label,
            "ok": self.ok,
            "score": self.score,
            "terms": [t.to_dict() for t in self.terms],
            "blocked_by": self.blocked_by,
            "headline": self.headline,
            "narrative": self.narrative,
            "changes": self.changes,
            "inputs": _clean_nan(self.inputs),
            "price": self.price,
        }


# ━━ 3) 헬퍼 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fmt_val(v, unit: str = "") -> str:
    """값을 단위에 맞춰 사람이 읽기 좋은 문자열로 바꾼다."""
    if v is None or (isinstance(v, float) and isnan(v)):
        return "-"
    if unit == "원":
        return f"{v:,.0f}원"
    if unit == "%":
        return f"{v * 100:+.2f}%"
    if unit == "배":
        return f"{v:.2f}배"
    if unit in ("분", "봉", "개"):
        return f"{v:.0f}{unit}"
    return f"{v:.2f}{unit}" if unit else f"{v:.2f}"


def _auto_explain(label: str, value: float, threshold: float, op: str, unit: str, passed: bool) -> str:
    v_s = fmt_val(value, unit)
    t_s = fmt_val(threshold, unit)
    return f"{label} {v_s} (기준 {t_s}) - {'충족' if passed else '미충족'}."


def mk_term(
    key: str,
    label: str,
    value: float,
    threshold: float,
    op: str,
    unit: str = "",
    required: bool = True,
    weight: float = 1.0,
    explain: str = "",
    upper: float | None = None,
) -> Term:
    """판정 항목 하나를 만든다. op 에 따라 통과 여부와 margin 을 계산한다."""
    # ★★★ 실제로 겪은 버그("'>' not supported between instances of
    # 'NoneType' and 'int'") - NaN 은 걸러냈지만 None 은 그대로 통과해
    # 아래 비교에서 죽었다. 시세·지표 계산이 값을 못 만들면 NaN 이 아니라
    # None 을 돌려주는 경로가 있다(API 응답에 필드가 없을 때 등).
    # ★ 여기는 모든 기법 판정이 지나는 길목이라, 한 번 죽으면 매매가
    #   통째로 멈춘다. 값이 없으면 "판단 불가"로 처리한다.
    if value is None or threshold is None or (isinstance(value, float) and isnan(value)):
        return Term(
            key=key, label=label, value=NAN if value is None else value,
            threshold=NAN if threshold is None else threshold, op=op,
            unit=unit, passed=False, required=required, weight=weight,
            margin=NAN, explain=explain or "데이터가 부족해 판단할 수 없습니다.",
        )

    if op == ">":
        passed = value > threshold
    elif op == ">=":
        passed = value >= threshold
    elif op == "<":
        passed = value < threshold
    elif op == "<=":
        passed = value <= threshold
    elif op == "between":
        hi = upper if upper is not None else threshold
        passed = threshold <= value <= hi
    elif op == "bool":
        passed = bool(value)
    else:
        raise ValueError(f"mk_term: 알 수 없는 연산자 {op!r}")

    if op in ("<", "<="):
        margin = threshold - value
    else:
        margin = value - threshold

    if not explain:
        explain = _auto_explain(label, value, threshold, op, unit, passed)

    return Term(
        key=key, label=label, value=value, threshold=threshold, op=op,
        unit=unit, passed=passed, required=required, weight=weight,
        margin=margin, explain=explain,
    )


def finish(
    *,
    phase: str,
    technique: str,
    technique_label: str,
    symbol: str,
    name: str,
    theme: str,
    terms: list,
    prev: Verdict | None,
    price: float = 0.0,
    inputs: dict | None = None,
    lead: str = "",
    at: str | None = None,
) -> Verdict:
    """Term 목록을 모아 최종 판정(Verdict)을 만든다."""
    blocked_by = [t.key for t in terms if t.required and not t.passed]
    ok = not blocked_by

    required_terms = [t for t in terms if t.required]
    optional_terms = [t for t in terms if not t.required]
    req_rate = (sum(1 for t in required_terms if t.passed) / len(required_terms)) if required_terms else 1.0
    opt_rate = (sum(1 for t in optional_terms if t.passed) / len(optional_terms)) if optional_terms else 1.0
    score = req_rate * 0.75 + opt_rate * 0.25

    changes: list[str] = []
    if prev is not None:
        for t in terms:
            pt = prev.term(t.key)
            if pt is None:
                continue
            if (
                isinstance(t.value, float) and isinstance(pt.value, float)
                and not isnan(t.value) and not isnan(pt.value)
            ):
                t.delta = t.value - pt.value
            if pt.passed != t.passed:
                t.flipped = "pass" if t.passed else "fail"
                verb = "넘겨 통과" if t.passed else "벗어나 실패"
                changes.append(
                    f"{t.label} {fmt_val(pt.value, t.unit)} → {fmt_val(t.value, t.unit)} "
                    f"(기준 {fmt_val(t.threshold, t.unit)}{josa(t.label)} {verb})"
                )

    at = at or iso(now_kst())
    passed_terms = [t for t in terms if t.passed]
    total = len(terms)
    passed_count = len(passed_terms)

    if ok:
        headline = f"{technique_label} · 통과"
        if passed_terms:
            headline += f" — {passed_terms[0].label} {fmt_val(passed_terms[0].value, passed_terms[0].unit)}"
    else:
        failed = next((t for t in required_terms if not t.passed), None)
        headline = f"{technique_label} · {passed_count}/{total} 충족"
        if failed:
            headline += f" — {failed.label} {fmt_val(failed.value, failed.unit)} (기준 {fmt_val(failed.threshold, failed.unit)})"

    narrative_parts = [lead] if lead else []
    passed_desc = ", ".join(f"{t.label} {fmt_val(t.value, t.unit)}" for t in passed_terms[:3])
    if passed_desc:
        narrative_parts.append(f"{passed_desc} 조건을 확인했습니다.")
    if changes:
        narrative_parts.append("직전 평가 대비 " + "; ".join(changes))
    narrative = " ".join(p for p in narrative_parts if p)

    return Verdict(
        id=f"{symbol}-{technique}-{at}",
        at=at,
        symbol=symbol, name=name, theme=theme, phase=phase,
        technique=technique, technique_label=technique_label,
        ok=ok, score=score, terms=terms, blocked_by=blocked_by,
        headline=headline, narrative=narrative, changes=changes,
        inputs=inputs or {}, price=price,
    )


# ━━ 4) 진입 기법 2종 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EntryBase:
    """모든 진입 기법이 공유하는 골격.
    origin/standard 는 각 기법이 덮어쓴다 - 이 기법이 어디서 왔고 우리가
    원전에서 무엇을 바꿨는지 화면에서 그대로 볼 수 있게 하기 위해서다.
    """

    key: str = ""
    label: str = ""
    description: str = ""
    origin: str = ""
    standard: str = ""
    # ★ 새로 살 수 있는 시간대 - main=기존 매매 시간, open=장 초반, close=장 막판. 기본은 main 만.
    windows: tuple = ("main",)

    def __init__(self, cfg, params: dict | None = None):
        self.cfg = cfg  # 최상위 Config 객체 (entry/screen/risk 를 모두 참조한다)
        self.params = params or {}

    def _get(self, key: str, fallback):
        return self.params.get(key, fallback)

    def _vi_term(self, close: float, upper: float | None) -> Term | None:
        """상한가 여유 판정. upper(상한가) 정보가 없으면 판정하지 않는다."""
        if upper is None:
            return None
        gap = (upper - close) / upper if upper else NAN
        return mk_term(
            "vi_gap", "상한가 여유", gap, self.cfg.entry.max_vi_gap_pct, ">=", unit="%",
            required=True,
            explain=(
                f"상한가 {fmt_val(upper,'원')} 대비 여유가 {fmt_val(gap,'%')}로 "
                f"기준 {fmt_val(self.cfg.entry.max_vi_gap_pct,'%')} 이상입니다."
                if not isnan(gap) and gap >= self.cfg.entry.max_vi_gap_pct
                else f"상한가 {fmt_val(upper,'원')}에 근접해 여유가 {fmt_val(gap,'%')} 뿐입니다."
            ),
        )

    def evaluate(self, bars, ctx) -> Verdict:
        raise NotImplementedError


class BreakoutEntry(EntryBase):
    # 이미 오르고 있는 종목이 최근 고점을 수급을 동반해 뚫는 순간을 잡는다.
    # 떨어지는 것을 싸다고 사지 않는다.
    key = "breakout"
    label = "돌파 추종"
    description = (
        "이미 오르고 있는 종목이 최근 고점을 수급을 동반해 뚫는 순간을 잡는다. "
        "떨어지는 것을 싸다고 사지 않는다."
    )
    origin = (
        "돈치안 채널 돌파(Richard Donchian, 1960년대) · 다베스 박스(Nicolas Darvas, 1960). "
        "거래량 확인은 리버모어의 피벗 포인트"
    )
    standard = (
        "원전의 N일 최고가 돌파를 1분봉 스캘핑에 맞춰 breakout_lookback 봉으로 단축하고, "
        "거래량 급증(volume_ratio)·추세선(sma) 조건을 필수로 덧붙였다."
    )

    def evaluate(self, bars, ctx) -> Verdict:
        e = self.cfg.entry
        lookback = self._get("breakout_lookback", e.breakout_lookback)
        window = self._get("volume_window", e.volume_window)
        surge = self._get("volume_surge_ratio", e.volume_surge_ratio)
        min_body_ratio = self._get("min_body_ratio", 0.35)

        need = max(lookback, window) + 2
        price = bars[-1].close if bars else 0.0

        terms = [mk_term(
            "bars_enough", "확보된 봉 수", float(len(bars)), float(need), ">=",
            unit="봉", required=True,
            explain=f"판정에 필요한 {need}봉 중 {len(bars)}봉이 모였습니다.",
        )]

        if len(bars) < need:
            return finish(
                phase="entry", technique=self.key, technique_label=self.label,
                symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
                terms=terms, prev=ctx.prev_verdict, price=price,
                inputs={"lookback": lookback, "window": window, "surge": surge},
                lead=self.description,
            )

        close = bars[-1].close
        open_ = bars[-1].open
        hh = highest_high(bars[:-1], lookback)
        vr = volume_ratio(bars, window)
        sma_v = sma(bars, window)

        gap = close - hh if not isnan(hh) else NAN
        gap_pct = gap / hh if not isnan(hh) and hh else NAN
        terms.append(mk_term(
            "breakout", "종가 vs 직전 N봉 고가", close, hh, ">", unit="원", required=True,
            explain=(
                f"종가 {fmt_val(close,'원')}이 직전 {lookback}봉 고가 {fmt_val(hh,'원')}을 "
                f"{fmt_val(gap,'원')}({fmt_val(gap_pct,'%')}) 넘었습니다."
                if not isnan(hh) and close > hh else
                f"종가 {fmt_val(close,'원')}이 직전 {lookback}봉 고가 {fmt_val(hh,'원')}을 넘지 못했습니다."
            ),
        ))
        terms.append(mk_term(
            "bullish", "양봉 여부", close - open_, 0.0, ">", unit="원", required=True,
            explain=(
                f"종가 {fmt_val(close,'원')}이 시가 {fmt_val(open_,'원')}보다 높은 양봉입니다."
                if close > open_ else "종가가 시가보다 낮아 음봉입니다."
            ),
        ))
        terms.append(mk_term(
            "volume", "거래량 급증", vr, surge, ">=", unit="배", required=True,
            explain=f"거래량이 최근 {window}봉 평균 대비 {fmt_val(vr,'배')}로 기준 {fmt_val(surge,'배')} 이상입니다."
            if not isnan(vr) and vr >= surge else f"거래량이 최근 {window}봉 평균 대비 {fmt_val(vr,'배')}에 그칩니다.",
        ))
        terms.append(mk_term(
            "trend", "추세선 위 여부", close, sma_v, ">=", unit="원", required=True,
            explain=f"종가 {fmt_val(close,'원')}이 {window}봉 평균선 {fmt_val(sma_v,'원')} 위에 있습니다."
            if not isnan(sma_v) and close >= sma_v else f"종가가 {window}봉 평균선 {fmt_val(sma_v,'원')} 아래에 있습니다.",
        ))

        vi_term = self._vi_term(close, getattr(ctx, "upper_limit", None))
        if vi_term is not None:
            terms.append(vi_term)

        # 가점(선택) - 없어도 진입은 하되, 있으면 신뢰도를 더한다.
        body_r = body_ratio(bars[-1])
        terms.append(mk_term(
            "body", "몸통 비율", body_r, min_body_ratio, ">=", unit="", required=False,
            explain=f"몸통 비율 {fmt_val(body_r)}로 매수세가 {'뚜렷합니다' if not isnan(body_r) and body_r >= min_body_ratio else '약합니다'} (기준 {fmt_val(min_body_ratio)}).",
        ))
        theme_bars = getattr(ctx, "theme_bars", None)
        rs = relative_strength(bars, theme_bars) if theme_bars else NAN
        terms.append(mk_term(
            "rs", "테마 대비 상대강도", rs, 1.0, ">=", unit="배", required=False,
            explain=f"테마 대비 상대강도 {fmt_val(rs,'배')}입니다.",
        ))

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=close,
            inputs={"lookback": lookback, "window": window, "surge": surge, "min_body_ratio": min_body_ratio},
            lead=self.description,
        )


class ThemeLeaderEntry(EntryBase):
    # 같은 테마 종목이 동시에 오르는 진짜 테마 장세에서 유동성이 가장 큰
    # 대장주를 잡는다. 한 종목만 튀는 건 테마가 아니라 개별 이슈다.
    key = "theme_leader"
    label = "테마 대장주"
    description = (
        "같은 테마 종목이 동시에 오르는 진짜 테마 장세에서 유동성이 가장 큰 "
        "대장주를 잡는다. 한 종목만 튀는 건 테마가 아니라 개별 이슈다."
    )
    origin = "상대강도 주도주 — William O'Neil, CAN SLIM 의 L (『How to Make Money in Stocks』 1988)"
    standard = (
        "CAN SLIM 의 상대강도(RS) 개념을 개별 종목 지표에서 "
        "테마 내 순위·상승폭 비교(theme_rank, theme_breadth)로 확장했다."
    )

    def evaluate(self, bars, ctx) -> Verdict:
        e = self.cfg.entry
        s = self.cfg.screen
        window = self._get("volume_window", e.volume_window)
        min_rs = self._get("min_relative_strength", 1.05)

        price = bars[-1].close if bars else 0.0
        close = bars[-1].close if bars else NAN
        sma_v = sma(bars, window) if bars else NAN

        theme_bars = getattr(ctx, "theme_bars", None)
        rs = relative_strength(bars, theme_bars) if theme_bars else NAN

        terms = [
            mk_term(
                "theme_breadth", "테마 내 상승 종목 수", float(getattr(ctx, "theme_breadth", NAN)),
                float(s.min_theme_members_up), ">=", unit="개", required=True,
                explain=f"테마 내 {fmt_val(getattr(ctx,'theme_breadth',NAN),'개')}종목이 상승 중입니다 (기준 {s.min_theme_members_up}개).",
            ),
            mk_term(
                "is_leader", "테마 순위", float(getattr(ctx, "theme_rank", NAN)), 1.0, "<=", unit="",
                required=True,
                explain=f"테마 내 순위 {fmt_val(getattr(ctx,'theme_rank',NAN))}위로 대장주 자리를 지키고 있습니다.",
            ),
            mk_term(
                "theme_intensity", "테마 강도", float(getattr(ctx, "theme_intensity", NAN)),
                s.min_change_rate, ">=", unit="%", required=True,
                explain=f"테마 평균 등락률 {fmt_val(getattr(ctx,'theme_intensity',NAN),'%')}로 기준 {fmt_val(s.min_change_rate,'%')} 이상입니다.",
            ),
            mk_term(
                "leader_rs", "종목 상대강도", rs, min_rs, ">=", unit="배", required=True,
                explain=f"테마 대비 상대강도 {fmt_val(rs,'배')}로 기준 {fmt_val(min_rs,'배')} 이상입니다.",
            ),
            mk_term(
                "trend", "추세선 위 여부", close, sma_v, ">=", unit="원", required=True,
                explain=f"종가 {fmt_val(close,'원')}이 {window}봉 평균선 {fmt_val(sma_v,'원')} 위에 있습니다."
                if not isnan(sma_v) and not isnan(close) and close >= sma_v
                else f"종가가 {window}봉 평균선 {fmt_val(sma_v,'원')} 아래에 있습니다.",
            ),
            mk_term(
                "not_extended", "단기 과열 여부", float(getattr(ctx, "change_rate", NAN)),
                s.max_change_rate, "<=", unit="%", required=True,
                explain=f"당일 등락률 {fmt_val(getattr(ctx,'change_rate',NAN),'%')}로 기준 {fmt_val(s.max_change_rate,'%')} 이하입니다.",
            ),
        ]

        vi_term = self._vi_term(close, getattr(ctx, "upper_limit", None))
        if vi_term is not None:
            terms.append(vi_term)

        # 가점(선택) - theme_accel: 테마가 힘을 받는 중인지.
        # ★ 원전에 정해진 식이 없어, 최근 5봉 종가 기울기로 가속도를 근사했다.
        #   더 나은 식이 확인되면 이 부분만 고치면 된다.
        accel = slope([b.close for b in bars[-5:]]) if len(bars) >= 5 else NAN
        terms.append(mk_term(
            "theme_accel", "가격 가속도(근사)", accel, 0.0, ">", unit="", required=False,
            explain=f"최근 5봉 종가 기울기 {fmt_val(accel)}로 상승이 {'가속' if not isnan(accel) and accel > 0 else '둔화'}되고 있습니다.",
        ))

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=price,
            inputs={"window": window, "min_rs": min_rs},
            lead=self.description,
        )


class VwapPullbackEntry(EntryBase):
    # 강한 종목이 잠깐 쉬며 VWAP 근처까지 눌렸다가 다시 돌아설 때 잡는다.
    # 고점 추격보다 손절 폭이 짧다.
    key = "vwap_pullback"
    label = "VWAP 눌림목"
    description = (
        "강한 종목이 잠깐 쉬며 VWAP 근처까지 눌렸다가 다시 돌아설 때 잡는다. "
        "고점 추격보다 손절 폭이 짧다."
    )
    origin = (
        "VWAP — Berkowitz·Logue·Noser(1988, Journal of Finance)가 "
        "기관 체결단가 벤치마크로 정립. 되돌림 매수는 데이트레이딩 표준"
    )
    standard = (
        "30봉 구간마다 VWAP 을 다시 계산하면 O(n^2)이 되므로, 현재 VWAP 값 하나로 "
        "근사해 그 위/아래 비율을 판정한다."
    )

    def evaluate(self, bars, ctx) -> Verdict:
        window = self._get("vwap_window", 30)
        hold_ratio = self._get("hold_ratio", 0.8)
        pullback_band = self._get("pullback_band", 0.008)

        price = bars[-1].close if bars else 0.0
        recent = bars[-window:] if len(bars) >= window else bars
        v = vwap(bars)
        close = bars[-1].close if bars else NAN
        open_ = bars[-1].open if bars else NAN

        above_ratio = (
            sum(1 for b in recent if not isnan(v) and b.close > v) / len(recent)
            if recent and not isnan(v) else NAN
        )
        pull_diff = (close - v) / v if not isnan(v) and v else NAN

        # ★ "되돌림구간"과 "상승구간"을 나누는 원전 식이 없어,
        #   현재 VWAP 을 기준으로 그 아래(눌림)/위(상승) 구간으로 근사했다.
        rally_bars = [b for b in recent if not isnan(v) and b.close > v]
        pull_bars = [b for b in recent if not isnan(v) and b.close <= v]
        if rally_bars and pull_bars:
            avg_rally = sum(b.volume for b in rally_bars) / len(rally_bars)
            avg_pull = sum(b.volume for b in pull_bars) / len(pull_bars)
            volume_calm_val = avg_pull / avg_rally if avg_rally else NAN
        else:
            volume_calm_val = NAN

        prev_bar = bars[-2] if len(bars) >= 2 else None
        # ★★★ 실제로 겪은 버그 - 부등호 방향이 뒤집혀 있었다. "직전봉보다
        # 낮은 저가를 찍고도 양봉 복귀"를 확인하려면 현재 저가가 직전
        # 저가보다 낮아야(bars[-1].low < prev_bar.low) 하는데, 반대로
        # 적혀 있어서(prev_bar.low < bars[-1].low) 오히려 "저가를 갱신
        # 하지 않은" 경우에 통과했다 - 되돌림 없이도 진입 신호가 나가는
        # 원인이었다.
        reclaim_ok = (
            prev_bar is not None and bars[-1].low < prev_bar.low and close > open_
        )

        terms = [
            mk_term(
                "above_vwap_day", "VWAP 위 유지 비율", above_ratio, hold_ratio, ">=", unit="%",
                required=True,
                explain=f"최근 {window}봉 중 {fmt_val(above_ratio,'%')}가 VWAP 위였습니다 (기준 {fmt_val(hold_ratio,'%')}).",
            ),
            mk_term(
                "pullback", "VWAP 근접 눌림", pull_diff, 0.0, "between", unit="%",
                upper=pullback_band, required=True,
                explain=f"종가가 VWAP 대비 {fmt_val(pull_diff,'%')} 떨어진 눌림 구간입니다 (허용폭 {fmt_val(pullback_band,'%')}).",
            ),
            mk_term(
                "reclaim", "저가 갱신 후 양봉 복귀", 1.0 if reclaim_ok else 0.0, 1.0, "bool", unit="",
                required=True,
                explain="직전봉보다 낮은 저가를 찍고도 양봉으로 마감해 복귀 신호가 나왔습니다."
                if reclaim_ok else "아직 저가를 갱신한 뒤 양봉으로 복귀하지 못했습니다.",
            ),
            mk_term(
                "volume_calm", "눌림 구간 거래량 진정", volume_calm_val, 0.7, "<=", unit="배",
                required=True,
                explain=f"눌림 구간 평균 거래량이 상승 구간의 {fmt_val(volume_calm_val,'배')}로 진정되었습니다."
                if not isnan(volume_calm_val) and volume_calm_val <= 0.7
                else f"눌림 구간 거래량이 상승 구간의 {fmt_val(volume_calm_val,'배')}로 아직 안 식었습니다.",
            ),
            mk_term(
                "day_gain", "당일 상승폭", float(getattr(ctx, "change_rate", NAN)),
                self.cfg.screen.min_change_rate, ">=", unit="%", required=True,
                explain=f"당일 등락률 {fmt_val(getattr(ctx,'change_rate',NAN),'%')}로 기준 {fmt_val(self.cfg.screen.min_change_rate,'%')} 이상입니다.",
            ),
        ]

        vi_term = self._vi_term(close, getattr(ctx, "upper_limit", None))
        if vi_term is not None:
            terms.append(vi_term)

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=price,
            inputs={"window": window, "hold_ratio": hold_ratio, "pullback_band": pullback_band},
            lead=self.description,
        )


class VwapReclaimEntry(EntryBase):
    # VWAP 아래로 밀렸던 종목이 거래량을 동반해 VWAP 위로 다시 올라서는 순간을 잡는다.
    # 눌림목(vwap_pullback)이 "위에서 쉬다가 다시 가는" 종목이라면, 이건 "아래로 밀렸다가 되돌리는" 종목이다.
    key = "vwap_reclaim"
    label = "VWAP 재탈환"
    description = (
        "VWAP 아래로 밀렸던 종목이 거래량을 동반해 VWAP 위로 다시 올라서는 순간을 잡는다. "
        "하락이 실패로 끝나 되돌려질 때라 손절(VWAP 바로 아래)이 짧다."
    )
    origin = (
        "VWAP 재탈환 — 기관 체결단가 벤치마크 VWAP(Berkowitz·Logue·Noser, 1988)와 "
        "실패한 하락 반전(Failed Breakdown, Al Brooks, 2009)의 결합"
    )
    standard = (
        "VWAP 은 지금까지 받은 봉 전체로 계산한 현재 값 하나를 쓴다(과거 각 시점의 VWAP 이 아니다). "
        "'아래에 있었다'는 판정도 그 한 값을 기준으로 근사한다."
    )

    def evaluate(self, bars, ctx) -> Verdict:
        lookback = self._get("lookback", 12)
        min_below = self._get("min_below", 3)
        reclaim_margin = self._get("reclaim_margin", 0.001)
        max_gap = self._get("max_gap", 0.02)
        vol_window = self._get("vol_window", 20)
        vol_mult = self._get("vol_mult", 1.5)
        close_pos = self._get("close_pos", 0.6)

        price = bars[-1].close if bars else 0.0
        v = vwap(bars)
        cur = bars[-1] if bars else None
        prev = bars[-2] if len(bars) >= 2 else None
        close = cur.close if cur else NAN

        recent = bars[-(lookback + 1):-1] if len(bars) > lookback else bars[:-1]
        below_cnt = (
            float(sum(1 for b in recent if b.close < v)) if recent and not isnan(v) else NAN
        )
        gap = (close - v) / v if not isnan(v) and v and not isnan(close) else NAN
        crossed = prev is not None and not isnan(v) and prev.close <= v < close

        base = [b.volume for b in bars[-(vol_window + 1):-1] if not isnan(b.volume)]
        avg_vol = sum(base) / len(base) if base else NAN
        vol_ratio = cur.volume / avg_vol if cur and avg_vol and not isnan(avg_vol) else NAN

        rng = (cur.high - cur.low) if cur else NAN
        pos = (cur.close - cur.low) / rng if cur and rng and not isnan(rng) and rng > 0 else NAN
        bullish = cur is not None and cur.close > cur.open

        terms = [
            mk_term(
                "was_below", "VWAP 아래에 있던 봉 수", below_cnt, float(min_below), ">=", unit="개",
                required=True,
                explain=f"직전 {lookback}봉 중 {fmt_val(below_cnt,'개')}가 VWAP 아래에 있었습니다 (기준 {min_below}개 이상).",
            ),
            mk_term(
                "crossed", "직전 봉 VWAP 아래 → 이번 봉 위", 1.0 if crossed else 0.0, 1.0, "bool", unit="",
                required=True,
                explain="직전 봉은 VWAP 이하였고 이번 봉이 VWAP 위로 올라섰습니다."
                if crossed else "아직 VWAP 을 위로 막 돌파한 봉이 아닙니다.",
            ),
            mk_term(
                "reclaim_gap", "VWAP 위 이격", gap, reclaim_margin, "between", unit="%",
                upper=max_gap, required=True,
                explain=f"종가가 VWAP 보다 {fmt_val(gap,'%')} 위입니다 (허용 {fmt_val(reclaim_margin,'%')}~{fmt_val(max_gap,'%')} - 너무 멀면 추격입니다).",
            ),
            mk_term(
                "volume_surge", "거래량 급증", vol_ratio, float(vol_mult), ">=", unit="배",
                required=True,
                explain=f"이번 봉 거래량이 최근 평균의 {fmt_val(vol_ratio,'배')}로 기준 {vol_mult}배 이상입니다."
                if not isnan(vol_ratio) and vol_ratio >= vol_mult
                else f"이번 봉 거래량이 최근 평균의 {fmt_val(vol_ratio,'배')}로 재탈환을 뒷받침하기엔 부족합니다.",
            ),
            mk_term(
                "close_strength", "봉 안 종가 위치", pos, float(close_pos), ">=", unit="%",
                required=True,
                explain=f"종가가 이번 봉 범위의 {fmt_val(pos,'%')} 지점으로 위쪽에서 마감했습니다 (기준 {fmt_val(close_pos,'%')} 이상).",
            ),
            mk_term(
                "bullish", "양봉", 1.0 if bullish else 0.0, 1.0, "bool", unit="", required=True,
                explain="양봉으로 마감했습니다." if bullish else "음봉이라 재탈환으로 보지 않습니다.",
            ),
        ]

        vi_term = self._vi_term(close, getattr(ctx, "upper_limit", None))
        if vi_term is not None:
            terms.append(vi_term)

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=price,
            inputs={"lookback": lookback, "min_below": min_below, "vol_mult": vol_mult, "max_gap": max_gap},
            lead=self.description,
        )


class OpenGapEntry(EntryBase):
    # 장 시작 직후 갭(전일 종가 대비 시가 격차)이 나고 그 갭이 메워지지 않은 채 첫 몇 분의 고점을 뚫는 순간을 잡는다.
    key = "open_gap"
    label = "시초 갭 돌파"
    windows = ("open", "main")
    description = (
        "장 시작 직후 위로 갭이 난 종목이 갭을 지키면서(시가 위 유지) 개장 첫 5분 고점을 거래량과 함께 뚫을 때 잡는다. "
        "09:40 이후에는 작동하지 않는다."
    )
    origin = "갭 앤 고(Gap and Go) — Ross Cameron 등 데이트레이딩 표준 · 개장 레인지 돌파(Crabel, 1990)의 갭 버전"
    standard = (
        "전일 종가는 현재가와 당일 등락률에서 거꾸로 계산한다(종가 = 현재가 ÷ (1+등락률)). "
        "당일 봉은 현재 시각(09:00 기준 경과 분)만큼의 최근 분봉으로 잘라 쓴다."
    )

    def evaluate(self, bars, ctx) -> Verdict:
        gap_min = self._get("gap_min", 0.01)
        gap_max = self._get("gap_max", 0.08)
        orb_bars = self._get("orb_bars", 5)
        vol_mult = self._get("vol_mult", 1.5)
        until = self._get("until_min", 40)

        now = getattr(ctx, "now", None)
        kr = bool(getattr(ctx, "kr_session", False))
        mins = (now.hour * 60 + now.minute - 540) if now is not None else NAN
        in_time = kr and now is not None and 0 <= mins <= until
        today = bars[-(int(mins) + 1):] if in_time and bars else []
        chg = float(getattr(ctx, "change_rate", NAN))
        close = bars[-1].close if bars else NAN
        price = close if not isnan(close) else 0.0

        day_open = today[0].open if today else NAN
        prev_close = close / (1.0 + chg) if not isnan(chg) and not isnan(close) and chg > -0.9 else NAN
        gap = day_open / prev_close - 1.0 if not isnan(day_open) and not isnan(prev_close) and prev_close else NAN
        or_high = max((b.high for b in today[:orb_bars]), default=NAN) if len(today) > orb_bars else NAN
        v = vwap(today) if today else NAN
        base = [b.volume for b in today[-11:-1] if not isnan(b.volume)]
        avg_vol = sum(base) / len(base) if base else NAN
        vol_ratio_now = bars[-1].volume / avg_vol if bars and avg_vol and not isnan(avg_vol) else NAN
        bullish = bool(bars) and bars[-1].close > bars[-1].open

        terms = [
            mk_term("kr_time", "장 초반 시간대(국내 09:00~09:40)", 1.0 if in_time else 0.0, 1.0, "bool", unit="", required=True,
                    explain="국내 장 초반(개장 후 40분 이내)입니다." if in_time else "국내 장 초반 시간대가 아니라 쓰지 않습니다."),
            mk_term("gap", "시가 갭", gap, gap_min, "between", unit="%", upper=gap_max, required=True,
                    explain=f"시가가 전일 종가보다 {fmt_val(gap,'%')} 높게 시작했습니다 (허용 {fmt_val(gap_min,'%')}~{fmt_val(gap_max,'%')} - 너무 크면 추격입니다)."),
            mk_term("gap_hold", "갭 유지(종가 ≥ 시가)", (close / day_open - 1.0) if not isnan(day_open) and day_open else NAN, 0.0, ">=", unit="%", required=True,
                    explain=f"현재가가 시가보다 {fmt_val((close / day_open - 1.0) if not isnan(day_open) and day_open else NAN,'%')} 위라 갭이 메워지지 않았습니다."),
            mk_term("orb_break", "개장 첫 5분 고점 돌파", close, or_high, ">", unit="원", required=True,
                    explain=f"현재가 {fmt_val(close,'원')}이 개장 첫 {orb_bars}분 고점 {fmt_val(or_high,'원')}을 넘었습니다."
                    if not isnan(or_high) and close > or_high else f"아직 개장 첫 {orb_bars}분 고점 {fmt_val(or_high,'원')}을 넘지 못했습니다."),
            mk_term("above_vwap", "VWAP 위", close, v, ">", unit="원", required=True,
                    explain=f"현재가가 당일 VWAP {fmt_val(v,'원')} 위에 있습니다."),
            mk_term("volume_surge", "거래량 급증", vol_ratio_now, float(vol_mult), ">=", unit="배", required=True,
                    explain=f"이번 봉 거래량이 직전 평균의 {fmt_val(vol_ratio_now,'배')}입니다 (기준 {vol_mult}배 이상)."),
            mk_term("bullish", "양봉", 1.0 if bullish else 0.0, 1.0, "bool", unit="", required=True,
                    explain="양봉으로 마감했습니다." if bullish else "음봉이라 돌파로 보지 않습니다."),
        ]
        vi_term = self._vi_term(close, getattr(ctx, "upper_limit", None))
        if vi_term is not None:
            terms.append(vi_term)
        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=price,
            inputs={"gap_min": gap_min, "gap_max": gap_max, "orb_bars": orb_bars, "until_min": until},
            lead=self.description,
        )


class CloseSqueezeEntry(EntryBase):
    # 장 막판(14:30~15:00)에도 당일 고점 근처에서 거래량이 붙으며 VWAP 이 오르는 종목을 마감 전 10~30분만 탄다.
    key = "close_squeeze"
    label = "장 막판 상승 지속"
    windows = ("main", "close")
    description = (
        "장 막판(14:30~15:00)에 당일 강하게 오른 종목이 고점 근처를 지키고 VWAP 이 오르며 거래량이 붙을 때 잡는다. "
        "15:10 강제 청산까지 짧게 보유한다."
    )
    origin = "마감 모멘텀(Closing Momentum) — 일중 수익률의 마지막 30분 지속 효과: Heston·Korajczyk·Sadka(2010, Journal of Finance)"
    standard = "당일 고점은 받아 온 최근 분봉(최대 80분)의 최고가로 근사한다. VWAP 상승 여부는 최근 10봉 전과 지금의 VWAP 을 비교한다."

    def evaluate(self, bars, ctx) -> Verdict:
        start_min = self._get("start_min", 14 * 60 + 30)
        end_min = self._get("end_min", 15 * 60)
        min_gain = self._get("min_gain", 0.03)
        near_high = self._get("near_high", 0.007)
        vol_mult = self._get("vol_mult", 1.3)

        now = getattr(ctx, "now", None)
        kr = bool(getattr(ctx, "kr_session", False))
        minute = (now.hour * 60 + now.minute) if now is not None else NAN
        in_time = kr and now is not None and start_min <= minute <= end_min
        chg = float(getattr(ctx, "change_rate", NAN))
        close = bars[-1].close if bars else NAN
        price = close if not isnan(close) else 0.0
        day_high = max((b.high for b in bars), default=NAN)
        off_high = (day_high - close) / day_high if not isnan(day_high) and day_high else NAN
        v_now = vwap(bars) if bars else NAN
        v_before = vwap(bars[:-10]) if len(bars) > 20 else NAN
        vwap_up = (v_now / v_before - 1.0) if not isnan(v_now) and not isnan(v_before) and v_before else NAN
        recent = [b.volume for b in bars[-5:]]
        prior = [b.volume for b in bars[-25:-5]]
        vr = (sum(recent) / len(recent)) / (sum(prior) / len(prior)) if recent and prior and sum(prior) else NAN
        bullish = bool(bars) and bars[-1].close > bars[-1].open
        max_gain = self.cfg.screen.max_change_rate

        terms = [
            mk_term("kr_time", "장 막판 시간대(국내 14:30~15:00)", 1.0 if in_time else 0.0, 1.0, "bool", unit="", required=True,
                    explain="국내 장 막판입니다." if in_time else "국내 장 막판 시간대가 아니라 쓰지 않습니다."),
            mk_term("day_gain", "당일 상승폭", chg, min_gain, "between", unit="%", upper=max_gain, required=True,
                    explain=f"당일 등락률 {fmt_val(chg,'%')} (허용 {fmt_val(min_gain,'%')}~{fmt_val(max_gain,'%')} - 너무 오른 종목은 추격입니다)."),
            mk_term("near_high", "당일 고점과의 거리", off_high, near_high, "<=", unit="%", required=True,
                    explain=f"현재가가 당일 고점보다 {fmt_val(off_high,'%')} 아래로 고점 근처를 지키고 있습니다 (기준 {fmt_val(near_high,'%')} 이내)."),
            mk_term("vwap_up", "VWAP 상승", vwap_up, 0.0, ">", unit="%", required=True,
                    explain=f"당일 VWAP 이 최근 10봉 전보다 {fmt_val(vwap_up,'%')} 올랐습니다."),
            mk_term("above_vwap", "VWAP 위", close, v_now, ">", unit="원", required=True,
                    explain=f"현재가가 VWAP {fmt_val(v_now,'원')} 위에 있습니다."),
            mk_term("volume_surge", "막판 거래량 증가", vr, float(vol_mult), ">=", unit="배", required=True,
                    explain=f"최근 5봉 거래량이 그 앞 20봉 평균의 {fmt_val(vr,'배')}입니다 (기준 {vol_mult}배 이상)."),
            mk_term("bullish", "양봉", 1.0 if bullish else 0.0, 1.0, "bool", unit="", required=True,
                    explain="양봉으로 마감했습니다." if bullish else "음봉이라 사지 않습니다."),
        ]
        vi_term = self._vi_term(close, getattr(ctx, "upper_limit", None))
        if vi_term is not None:
            terms.append(vi_term)
        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=price,
            inputs={"min_gain": min_gain, "near_high": near_high, "vol_mult": vol_mult},
            lead=self.description,
        )


class OrbEntry(EntryBase):
    # 09:00~09:30 에 만들어진 하루의 첫 레인지를 상단으로 뚫는 흐름을 잡는다.
    # 시간대가 한정되므로 scan_start 를 or_end 이하로 둬야 의미가 있다.
    key = "orb"
    label = "개장 레인지 돌파"
    description = (
        "09:00~09:30 에 만들어진 하루의 첫 레인지를 상단으로 뚫는 흐름을 잡는다. "
        "시간대가 한정되므로 scan_start 를 or_end 이하로 둬야 의미가 있다."
    )
    origin = "오프닝 레인지 브레이크아웃 — Toby Crabel(1990)"
    standard = (
        "원전의 레인지 구간을 or_start/or_end 파라미터로 노출하고, "
        "레인지 폭이 너무 좁거나(노이즈) 너무 넓은(이미 다 움직인) 경우를 "
        "or_width 조건으로 걸러냈다."
    )

    def evaluate(self, bars, ctx) -> Verdict:
        or_start = self._get("or_start", "09:00")
        or_end = self._get("or_end", "09:30")
        min_width_pct = self._get("min_width_pct", 0.01)
        max_width_pct = self._get("max_width_pct", 0.08)

        price = bars[-1].close if bars else 0.0
        close = bars[-1].close if bars else NAN
        or_high, or_low = range_of(bars, or_start, or_end)
        width_pct = (or_high - or_low) / or_low if not isnan(or_low) and or_low else NAN

        now_time = ctx.now.time() if hasattr(ctx.now, "time") else ctx.now
        window_ready = now_time > parse_hhmm(or_end)

        terms = [
            mk_term(
                "window_ready", "레인지 형성 완료", 1.0 if window_ready else 0.0, 1.0, "bool", unit="",
                required=True,
                explain=f"개장 레인지({or_start}~{or_end}) 형성이 끝났습니다." if window_ready
                else f"아직 개장 레인지({or_start}~{or_end}) 형성 중입니다.",
            ),
            mk_term(
                "or_break", "레인지 상단 돌파", close, or_high, ">", unit="원", required=True,
                explain=f"종가 {fmt_val(close,'원')}이 개장 레인지 고가 {fmt_val(or_high,'원')}을 넘었습니다."
                if not isnan(or_high) and close > or_high
                else f"종가 {fmt_val(close,'원')}이 개장 레인지 고가 {fmt_val(or_high,'원')}을 넘지 못했습니다.",
            ),
            mk_term(
                "or_width", "레인지 폭 적정성", width_pct, min_width_pct, "between", unit="%",
                upper=max_width_pct, required=True,
                explain=f"개장 레인지 폭 {fmt_val(width_pct,'%')}로 적정 범위({fmt_val(min_width_pct,'%')}~{fmt_val(max_width_pct,'%')}) 안에 있습니다.",
            ),
            mk_term(
                "volume", "거래량 급증", volume_ratio(bars, self.cfg.entry.volume_window),
                self.cfg.entry.volume_surge_ratio, ">=", unit="배", required=True,
                explain="거래량이 평소 대비 충분히 늘었습니다.",
            ),
            # ★★★ "오버나이트·장기 보유 허용" - 이 항목은 "오늘 안에 강제청산해야 하니 남은
            # 시간이 최소 보유시간은 돼야 진입한다"는 전제인데, allow_overnight 이 켜지면
            # 그 전제 자체가 없어진다(포지션을 다음날로 넘길 수 있으므로). 그대로 두면
            # max_hold_minutes 를 며칠 단위로 넓힌 순간 하루 안에는 절대 통과할 수 없는
            # 조건이 되어 ORB 진입이 사실상 영구히 막힌다 - 켜져 있으면 이 조건을 건너뛴다.
            mk_term(
                "time_left", "강제청산까지 남은 시간",
                1.0 if self.cfg.exit.allow_overnight else float(getattr(ctx, "minutes_to_close", NAN)),
                1.0 if self.cfg.exit.allow_overnight else float(self.cfg.exit.max_hold_minutes),
                ">=", unit="분", required=True,
                explain="오버나이트 보유가 허용돼 있어 남은 시간과 무관하게 진입할 수 있습니다."
                if self.cfg.exit.allow_overnight
                else f"강제청산까지 {fmt_val(getattr(ctx,'minutes_to_close',NAN),'분')} 남아, 최소 보유시간 {self.cfg.exit.max_hold_minutes}분을 확보할 수 있습니다.",
            ),
            mk_term(
                "first_break", "최초 돌파 여부", 0.0 if getattr(ctx, "orb_done", False) else 1.0, 1.0,
                "bool", unit="", required=True,
                explain="이 종목의 오늘 첫 ORB 진입입니다." if not getattr(ctx, "orb_done", False)
                else "이 종목은 오늘 이미 ORB 로 진입한 적이 있습니다.",
            ),
        ]

        vi_term = self._vi_term(close, getattr(ctx, "upper_limit", None))
        if vi_term is not None:
            terms.append(vi_term)

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=price,
            inputs={"or_start": or_start, "or_end": or_end, "or_high": or_high, "or_low": or_low},
            lead=self.description,
        )


class VolumeDryPopEntry(EntryBase):
    # 거래가 말라붙어 횡보하던 종목에 갑자기 대량 거래가 붙는 첫 순간을 잡는다.
    key = "volume_dry_pop"
    label = "거래량 마름 후 분출"
    description = "거래가 말라붙어 횡보하던 종목에 갑자기 대량 거래가 붙는 첫 순간을 잡는다."
    origin = "변동성 수축 패턴(VCP) — Mark Minervini(2013) · NR7 — Crabel(1990)"
    standard = (
        "VCP/NR7 이 말하는 '변동성 수축'을 dry_up_ratio(거래량 마름)와 "
        "tight_range_pct(가격 수축)로 각각 수치화하고, 두 조건이 동시에 "
        "만족된 뒤 거래량이 터지는 시점만 잡는다."
    )

    def evaluate(self, bars, ctx) -> Verdict:
        recent = self._get("recent", 20)
        base = self._get("base", 40)
        dry_ratio_th = self._get("dry_ratio", 0.6)
        tight_th = self._get("tight_range_pct", 0.02)
        pop_multiple = self._get("pop_multiple", 1.5)
        window = self.cfg.entry.volume_window
        surge = self.cfg.entry.volume_surge_ratio

        price = bars[-1].close if bars else 0.0
        close = bars[-1].close if bars else NAN
        open_ = bars[-1].open if bars else NAN

        dry_val = dry_up_ratio(bars, recent, base)
        tight_val = tight_range_pct(bars, recent)
        vr = volume_ratio(bars, window)
        hh = highest_high(bars[:-1], recent) if len(bars) > recent else NAN

        terms = [
            mk_term(
                "dry", "거래량 마름", dry_val, dry_ratio_th, "<=", unit="배", required=True,
                explain=f"최근 {recent}봉 평균 거래량이 그 전 {base}봉의 {fmt_val(dry_val,'배')}로 말라붙었습니다."
                if not isnan(dry_val) and dry_val <= dry_ratio_th
                else f"거래량 마름 정도가 {fmt_val(dry_val,'배')}로 기준({fmt_val(dry_ratio_th,'배')})에 못 미칩니다.",
            ),
            mk_term(
                "tight", "가격 수축", tight_val, tight_th, "<=", unit="%", required=True,
                explain=f"최근 {recent}봉 변동폭이 {fmt_val(tight_val,'%')}로 수축되었습니다."
                if not isnan(tight_val) and tight_val <= tight_th
                else f"최근 {recent}봉 변동폭이 {fmt_val(tight_val,'%')}로 아직 넓습니다.",
            ),
            mk_term(
                "pop", "거래량 분출", vr, surge * pop_multiple, ">=", unit="배", required=True,
                explain=f"거래량이 평균 대비 {fmt_val(vr,'배')}로 터졌습니다 (기준 {fmt_val(surge*pop_multiple,'배')}).",
            ),
            mk_term(
                "bullish", "양봉 여부", close - open_, 0.0, ">", unit="원", required=True,
                explain="양봉으로 분출이 매수 쪽입니다." if close > open_ else "음봉이라 분출이 매도 쪽입니다.",
            ),
            mk_term(
                "break_tight", "수축 구간 상단 돌파", close, hh, ">", unit="원", required=True,
                explain=f"종가 {fmt_val(close,'원')}이 수축 구간 고가 {fmt_val(hh,'원')}을 넘었습니다."
                if not isnan(hh) and close > hh
                else f"종가 {fmt_val(close,'원')}이 수축 구간 고가 {fmt_val(hh,'원')}을 넘지 못했습니다.",
            ),
        ]

        vi_term = self._vi_term(close, getattr(ctx, "upper_limit", None))
        if vi_term is not None:
            terms.append(vi_term)

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=price,
            inputs={"recent": recent, "base": base, "dry_ratio": dry_ratio_th, "tight": tight_th},
            lead=self.description,
        )


class BullFlagEntry(EntryBase):
    # 급등(깃대) 이후 짧게 눌린(깃발) 뒤 다시 상단을 돌파하는 지속형 패턴을 잡는다.
    key = "bull_flag"
    label = "강세 깃발형"
    description = "급등(깃대) 이후 짧게 눌린(깃발) 뒤 다시 상단을 돌파하는 지속형 패턴을 잡는다."
    origin = (
        "강세 깃발형(Bull Flag) — Edwards & Magee, 『Technical Analysis of Stock Trends』(1948). "
        "조정이 급등폭의 절반을 넘으면 깃발로 보지 않는다."
    )
    standard = (
        "원전은 되돌림 38.2~50% 이내 + 조정 중 거래량 감소 + 돌파 시 거래량 회복. "
        "여기서는 깃대 10봉 +2.5%, 깃발 8봉, 되돌림 50% 이내, 돌파 거래량 1.5배로 파라미터화했다."
    )

    def evaluate(self, bars, ctx) -> Verdict:
        pole_bars = self._get("pole_bars", 10)
        pole_gain_pct = self._get("pole_gain_pct", 0.025)
        flag_bars = self._get("flag_bars", 8)
        max_retrace = self._get("max_retrace", 0.5)
        surge = self._get("volume_surge_ratio", 1.5)
        window = self.cfg.entry.volume_window

        price = bars[-1].close if bars else 0.0
        need = pole_bars + flag_bars + window + 2
        terms = [mk_term(
            "bars_enough", "확보된 봉 수", float(len(bars)), float(need), ">=", unit="봉", required=True,
            explain=f"판정에 필요한 {need}봉 중 {len(bars)}봉이 모였습니다.",
        )]

        if len(bars) < need:
            return finish(
                phase="entry", technique=self.key, technique_label=self.label,
                symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
                terms=terms, prev=ctx.prev_verdict, price=price,
                inputs={"pole_bars": pole_bars, "flag_bars": flag_bars}, lead=self.description,
            )

        close = bars[-1].close
        # ★ 현재 봉은 어느 구간에도 넣지 않는다 - 아직 확정되지 않은 흐름이기 때문이다.
        flag = bars[-(flag_bars + 1):-1]
        pole = bars[-(flag_bars + pole_bars + 1):-(flag_bars + 1)]

        pole_high = max(b.high for b in pole)
        pole_low = min(b.low for b in pole)
        flag_low = min(b.low for b in flag)
        flag_high = max(b.high for b in flag)

        pole_gain = (pole_high - pole_low) / pole_low if pole_low else NAN
        retrace = (pole_high - flag_low) / (pole_high - pole_low) if (pole_high - pole_low) else NAN
        vr = volume_ratio(bars, window)

        terms.append(mk_term(
            "pole", "깃대 상승", pole_gain, pole_gain_pct, ">=", unit="%", required=True,
            explain=f"깃대 구간 상승폭 {fmt_val(pole_gain,'%')}로 기준 {fmt_val(pole_gain_pct,'%')} 이상입니다."
            if not isnan(pole_gain) and pole_gain >= pole_gain_pct
            else f"깃대 구간 상승폭 {fmt_val(pole_gain,'%')}로 기준에 못 미칩니다.",
        ))
        terms.append(mk_term(
            "retrace", "조정 되돌림", retrace, max_retrace, "<=", unit="%", required=True,
            explain=(
                f"깃대 상승폭의 {fmt_val(retrace,'%')}까지 되돌렸습니다(기준 {fmt_val(max_retrace,'%')} 이내)."
                if isnan(retrace) or retrace <= max_retrace else
                f"깃대 상승폭의 {fmt_val(retrace,'%')}까지 되돌렸습니다(기준 {fmt_val(max_retrace,'%')} 이내). "
                "너무 깊어 깃발이 아니라 추세 전환으로 봅니다."
            ),
        ))
        terms.append(mk_term(
            "flag_break", "깃발 상단 돌파", close, flag_high, ">", unit="원", required=True,
            explain=f"종가 {fmt_val(close,'원')}이 깃발 상단 {fmt_val(flag_high,'원')}을 넘었습니다."
            if close > flag_high else f"종가 {fmt_val(close,'원')}이 깃발 상단 {fmt_val(flag_high,'원')}을 넘지 못했습니다.",
        ))
        terms.append(mk_term(
            "volume", "돌파 거래량", vr, surge, ">=", unit="배", required=True,
            explain=f"거래량이 평균 대비 {fmt_val(vr,'배')}로 기준 {fmt_val(surge,'배')} 이상입니다.",
        ))

        vi_term = self._vi_term(close, getattr(ctx, "upper_limit", None))
        if vi_term is not None:
            terms.append(vi_term)

        pole_avg_vol = sum(b.volume for b in pole) / len(pole) if pole else NAN
        flag_avg_vol = sum(b.volume for b in flag) / len(flag) if flag else NAN
        flag_dry = flag_avg_vol / pole_avg_vol if pole_avg_vol else NAN
        terms.append(mk_term(
            "flag_dry", "깃발 구간 거래량 감소", flag_dry, 0.8, "<=", unit="배", required=False,
            explain=f"깃발 구간 평균 거래량이 깃대 구간의 {fmt_val(flag_dry,'배')}로 진정되었습니다."
            if not isnan(flag_dry) and flag_dry <= 0.8
            else f"깃발 구간 거래량이 깃대 구간의 {fmt_val(flag_dry,'배')}로 아직 안 식었습니다.",
        ))

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=close,
            inputs={
                "pole_bars": pole_bars, "flag_bars": flag_bars,
                "pole_gain_pct": pole_gain_pct, "max_retrace": max_retrace,
            },
            lead=self.description,
        )


class MaPullbackEntry(EntryBase):
    # ADX 로 추세가 확인된 종목이 20EMA 로 살짝 눌린 뒤
    # 직전 봉 고가를 돌파하는 순간을 잡는다.
    key = "ma_pullback"
    label = "이동평균 눌림목"
    description = "ADX 로 추세가 확인된 종목이 20EMA 로 살짝 눌린 뒤 직전 봉 고가를 돌파하는 순간을 잡는다."
    origin = (
        "'홀리 그레일(Holy Grail)' — Linda Raschke & Laurence Connors, 『Street Smarts』(1995). "
        "ADX(14)>30 인 추세 종목이 20EMA 로 되돌림했을 때 직전 봉 고가 돌파로 진입한다."
    )
    standard = "원전은 ADX 30. 1분봉은 잡음이 많아 25로 낮추고 거래량 조건을 하나 더 얹었다."

    def evaluate(self, bars, ctx) -> Verdict:
        ma_length = self._get("ma_length", 20)
        min_adx = self._get("min_adx", 25.0)
        touch_band = self._get("touch_band", 0.004)
        window = self.cfg.entry.volume_window

        price = bars[-1].close if bars else 0.0
        need = max(2 * 14 + 2, ma_length + window + 2)
        terms = [mk_term(
            "bars_enough", "확보된 봉 수", float(len(bars)), float(need), ">=", unit="봉", required=True,
            explain=f"판정에 필요한 {need}봉 중 {len(bars)}봉이 모였습니다.",
        )]

        if len(bars) < need:
            return finish(
                phase="entry", technique=self.key, technique_label=self.label,
                symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
                terms=terms, prev=ctx.prev_verdict, price=price,
                inputs={"ma_length": ma_length, "min_adx": min_adx}, lead=self.description,
            )

        close = bars[-1].close
        # ★ 되돌림 판정은 직전 봉의 저가로 한다. 현재 봉은 이미 반등 중이라
        #   현재 봉 저가로 재면 거의 모든 봉이 통과해 버린다.
        prev_bar = bars[-2]
        adx_v = adx(bars, 14)
        ema20 = ema(bars, ma_length)
        touch = abs(prev_bar.low - ema20) / ema20 if not isnan(ema20) and ema20 else NAN
        vr = volume_ratio(bars, window)

        terms.append(mk_term(
            "adx", "추세 세기(ADX 14)", adx_v, min_adx, ">=", unit="", required=True,
            explain=(
                f"ADX(14) {fmt_val(adx_v)}로 기준 {fmt_val(min_adx)} 이상입니다 - "
                "추세가 있는 상태의 눌림목입니다."
                if not isnan(adx_v) and adx_v >= min_adx else
                f"ADX(14) {fmt_val(adx_v)}로 기준에 못 미칩니다 - "
                "추세가 없는데 눌림목을 사면 그냥 떨어지는 것을 사는 것입니다."
            ),
        ))
        terms.append(mk_term(
            "above_ma", "20봉 EMA 위", close, ema20, ">=", unit="원", required=True,
            explain=f"종가 {fmt_val(close,'원')}이 20봉 EMA {fmt_val(ema20,'원')} 위에 있습니다."
            if not isnan(ema20) and close >= ema20 else f"종가가 20봉 EMA {fmt_val(ema20,'원')} 아래에 있습니다.",
        ))
        terms.append(mk_term(
            "touched", "EMA 되돌림 접촉", touch, touch_band, "<=", unit="%", required=True,
            explain=f"직전 봉 저가가 EMA20 대비 {fmt_val(touch,'%')} 이내로 접촉했습니다 (기준 {fmt_val(touch_band,'%')})."
            if not isnan(touch) and touch <= touch_band
            else f"직전 봉 저가가 EMA20 에서 {fmt_val(touch,'%')} 떨어져 있어 접촉하지 못했습니다.",
        ))
        terms.append(mk_term(
            "trigger", "직전 봉 고가 돌파", close, prev_bar.high, ">", unit="원", required=True,
            explain=f"종가 {fmt_val(close,'원')}이 직전 봉 고가 {fmt_val(prev_bar.high,'원')}을 넘었습니다."
            if close > prev_bar.high else f"종가 {fmt_val(close,'원')}이 직전 봉 고가 {fmt_val(prev_bar.high,'원')}을 넘지 못했습니다.",
        ))

        vi_term = self._vi_term(close, getattr(ctx, "upper_limit", None))
        if vi_term is not None:
            terms.append(vi_term)

        terms.append(mk_term(
            "volume", "거래량 확인", vr, 1.0, ">=", unit="배", required=False,
            explain=f"거래량이 평균 대비 {fmt_val(vr,'배')}입니다.",
        ))

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=close,
            inputs={"ma_length": ma_length, "min_adx": min_adx, "touch_band": touch_band},
            lead=self.description,
        )


class VolatilityBreakoutEntry(EntryBase):
    """★★ 원래 crypto_playbook.py 에 코인 전용으로만 있던 기법을, "암호화폐도
    국내주식과 동일한 기법 체계를 쓰게 해달라"는 요청에 따라 이 공용
    레지스트리로 옮겨왔다 - 이제 국내주식·암호화폐가 같은 클래스를 공유한다.
    """
    key = "volatility_breakout"
    label = "변동성 돌파"
    description = (
        "직전 구간의 변동폭(고가-저가)에 계수 k를 곱한 값을 오늘 시가에 더한 "
        "목표가를 현재가가 넘으면 매수한다. 봉을 앞뒤 절반으로 나눠 "
        "'직전 구간 vs 오늘 구간'을 만든다(장 마감이 없는 시장을 고려)."
    )
    origin = (
        "Larry Williams 의 변동성 돌파(주식·선물용)를 국내 코인 트레이더들이 "
        "암호화폐에 맞게 응용한 형태 - 학술 검증이 아니라 수개월~1년 단위 "
        "실전 운용 사례로 뒷받침된 '커뮤니티 검증' 기법입니다."
    )
    standard = "표준 k=0.5. [설정] → 암호화폐 섹션의 '변동성 돌파 계수(k)' 값을 그대로 씁니다."

    def evaluate(self, bars, ctx) -> Verdict:
        # ★ k 는 기법 파라미터(strategy.params.volatility_breakout.k)를 먼저
        # 보고, 없으면 암호화폐 설정(cfg.crypto.k)을 그대로 쓴다 - 국내주식과
        # 암호화폐가 이 기법 하나를 공유하니, 설정도 하나만 고치면 되게 한다.
        k = self._get("k", None)
        if k is None:
            k = getattr(getattr(self.cfg, "crypto", None), "k", 0.5)

        price = bars[-1].close if bars else 0.0
        if len(bars) < 2:
            terms = [mk_term(
                "bars_enough", "확보된 구간 수", float(len(bars)), 2.0, ">=", unit="개", required=True,
                explain="변동성 돌파를 계산하려면 최소 2개 구간(직전·오늘)이 필요합니다.",
            )]
            return finish(
                phase="entry", technique=self.key, technique_label=self.label,
                symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
                terms=terms, prev=ctx.prev_verdict, price=price,
                inputs={"k": k}, lead=self.description,
            )

        mid = len(bars) // 2
        prev_chunk, today_chunk = bars[:mid], bars[mid:]
        prev_high = max(b.high for b in prev_chunk)
        prev_low = min(b.low for b in prev_chunk)
        today_open = today_chunk[0].open
        target = today_open + k * (prev_high - prev_low)
        current_price = bars[-1].close

        terms = [
            mk_term(
                "breakout", "목표가 돌파", current_price, target, ">", unit="원", required=True,
                explain=(
                    f"현재가 {fmt_val(current_price,'원')}이 목표가 {fmt_val(target,'원')}"
                    f"(오늘 시가+직전 변동폭×{k})을 넘었습니다."
                    if current_price > target else
                    f"현재가 {fmt_val(current_price,'원')}이 아직 목표가 {fmt_val(target,'원')}에 못 미칩니다."
                ),
            ),
        ]
        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=current_price,
            inputs={"k": k, "target": target, "prev_high": prev_high, "prev_low": prev_low},
            lead=self.description,
        )


class RsiPullbackEntry(EntryBase):
    # 상승 추세 중 RSI(2)가 짧게 과매도까지 떨어진 순간을 되돌림 매수 기회로 본다.
    key = "rsi_pullback"
    label = "RSI 단기 되돌림"
    description = "상승 추세 중 RSI(2)가 과매도 구간까지 짧게 떨어진 순간을 되돌림 매수 기회로 본다."
    origin = (
        "RSI(2) 평균회귀 전략 — Larry Connors, 『Short Term Trading Strategies That Work』(2009). "
        "다수의 프롭 트레이딩 데스크·퀀트 전략에서 변형해 쓰는 잘 알려진 단기 평균회귀 기법이다."
    )
    standard = (
        "원전은 일봉 기준(RSI(2)<10 + 종가>200일선)이다. 1분봉 데이트레이딩에 맞춰 "
        "기준선을 200일선 대신 20봉 EMA로, RSI 과매도 기준을 10→15로 완화했다."
    )

    def evaluate(self, bars, ctx) -> Verdict:
        rsi_n = self._get("rsi_n", 2)
        oversold = self._get("oversold", 15.0)
        ma_length = self._get("ma_length", 20)
        window = self.cfg.entry.volume_window

        price = bars[-1].close if bars else 0.0
        need = max(rsi_n + 5, ma_length + 2)
        terms = [mk_term(
            "bars_enough", "확보된 봉 수", float(len(bars)), float(need), ">=", unit="봉", required=True,
            explain=f"판정에 필요한 {need}봉 중 {len(bars)}봉이 모였습니다.",
        )]

        if len(bars) < need:
            return finish(
                phase="entry", technique=self.key, technique_label=self.label,
                symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
                terms=terms, prev=ctx.prev_verdict, price=price,
                inputs={"rsi_n": rsi_n, "oversold": oversold}, lead=self.description,
            )

        close = bars[-1].close
        rsi_v = rsi(bars, rsi_n)
        ma = ema(bars, ma_length)
        vr = volume_ratio(bars, window)

        terms.append(mk_term(
            "trend", f"{ma_length}봉 EMA 위(상승 추세)", close, ma, ">=", unit="원", required=True,
            explain=f"종가 {fmt_val(close,'원')}이 {ma_length}봉 EMA {fmt_val(ma,'원')} 위에 있습니다 - 상승 추세 중입니다."
            if not isnan(ma) and close >= ma
            else f"종가가 {ma_length}봉 EMA {fmt_val(ma,'원')} 아래라 상승 추세로 보지 않습니다.",
        ))
        terms.append(mk_term(
            "rsi_oversold", f"RSI({rsi_n}) 과매도", rsi_v, oversold, "<=", unit="", required=True,
            explain=f"RSI({rsi_n}) {fmt_val(rsi_v)}로 과매도 기준 {fmt_val(oversold)} 이하입니다 - "
            "상승 추세 중 짧게 눌린 상태입니다."
            if not isnan(rsi_v) and rsi_v <= oversold
            else f"RSI({rsi_n}) {fmt_val(rsi_v)}로 아직 눌리지 않았습니다.",
        ))

        vi_term = self._vi_term(close, getattr(ctx, "upper_limit", None))
        if vi_term is not None:
            terms.append(vi_term)

        terms.append(mk_term(
            "volume", "거래량 확인(마르지 않음)", vr, 0.7, ">=", unit="배", required=False,
            explain=f"거래량이 평균 대비 {fmt_val(vr,'배')}입니다 - 관심이 완전히 식지는 않았습니다.",
        ))

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=close,
            inputs={"rsi_n": rsi_n, "oversold": oversold, "ma_length": ma_length},
            lead=self.description,
        )


# ━━ 청산 5종 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def finish_exit(
    *,
    technique: str,
    technique_label: str,
    symbol: str,
    name: str,
    theme: str,
    terms: list,
    prev: Verdict | None,
    ok: bool,
    price: float = 0.0,
    inputs: dict | None = None,
    lead: str = "",
    at: str | None = None,
) -> Verdict:
    """청산 기법 전용 마무리.
    ★ 청산은 "걸리면 청산"이므로 Verdict.ok = 청산해야 함 을 뜻한다.
    진입과 반대 의미다. blocked_by 는 쓰지 않는다 (항상 빈 리스트).
    """
    at = at or iso(now_kst())
    total = len(terms)
    passed_count = sum(1 for t in terms if t.passed)
    score = (passed_count / total) if total else 0.0

    changes: list[str] = []
    if prev is not None:
        for t in terms:
            pt = prev.term(t.key)
            if pt is None:
                continue
            if (
                isinstance(t.value, float) and isinstance(pt.value, float)
                and not isnan(t.value) and not isnan(pt.value)
            ):
                t.delta = t.value - pt.value
            if pt.passed != t.passed:
                t.flipped = "pass" if t.passed else "fail"
                changes.append(f"{t.label} {fmt_val(pt.value, t.unit)} → {fmt_val(t.value, t.unit)}")

    headline = f"{technique_label} · {'청산' if ok else '유지'} ({passed_count}/{total})"
    passed_terms = [t for t in terms if t.passed]
    passed_desc = ", ".join(f"{t.label} {fmt_val(t.value, t.unit)}" for t in passed_terms)

    narrative_parts = [lead] if lead else []
    if passed_desc:
        narrative_parts.append(f"{passed_desc} 조건이 확인되었습니다.")
    if changes:
        narrative_parts.append("직전 평가 대비 " + "; ".join(changes))
    narrative = " ".join(p for p in narrative_parts if p)

    return Verdict(
        id=f"{symbol}-{technique}-{at}", at=at, symbol=symbol, name=name, theme=theme,
        phase="exit", technique=technique, technique_label=technique_label,
        ok=ok, score=score, terms=terms, blocked_by=[],
        headline=headline, narrative=narrative, changes=changes,
        inputs=inputs or {}, price=price,
    )


class ExitBase:
    """모든 청산 기법이 공유하는 골격. origin/standard 는 각 기법이 덮어쓴다."""

    key: str = ""
    label: str = ""
    description: str = ""
    origin: str = ""
    standard: str = ""

    def __init__(self, cfg, params: dict | None = None, risk=None, max_hold_minutes: float | None = None):
        self.cfg = cfg
        self.params = params or {}
        # ★★★ 실제로 겪은 버그 - 이 클래스들이 항상 cfg.risk/cfg.exit(국내
        # 주식 설정)를 직접 읽어서, 암호화폐가 [설정]→암호화폐에서 따로
        # 정한 손절·익절·트레일링·최대보유시간(cfg.crypto.*)이 통째로
        # 무시되고 있었다("코인 청산이 전부 90분 시간손절로만 찍힌다"의
        # 정체). Playbook 이 시장별로 risk/max_hold_minutes 를 넘겨주면
        # 그것을 쓰고, 안 넘기면(국내주식) 지금까지처럼 cfg 값을 그대로 쓴다.
        self.risk = risk if risk is not None else cfg.risk
        self.max_hold_minutes = (
            max_hold_minutes if max_hold_minutes is not None else cfg.exit.max_hold_minutes
        )

    def _get(self, key: str, fallback):
        return self.params.get(key, fallback)

    def evaluate(self, pos, bars, last, ctx) -> Verdict:
        raise NotImplementedError


class FixedExit(ExitBase):
    # 규칙의 뼈대. 항상 켜져 있어야 한다 - 손절 없는 매매는 없다.
    key = "fixed"
    label = "고정 손절·익절"
    description = "손절가와 익절가를 진입 즉시 고정해 둔다. 규칙의 뼈대 - 항상 켜져 있어야 한다."
    origin = "R 배수 손절·목표 — Van Tharp(1998). 손절 2%/익절 4% = 정확히 2R"
    standard = "원전의 R배수 개념을 퍼센트 기준 risk.stop_loss_pct/take_profit_pct 로 직접 표현했다."

    def evaluate(self, pos, bars, last, ctx) -> Verdict:
        r = self.risk
        # ★★★ 여기는 손절·익절이라 가장 중요한 안전장치다. 진입가가
        # 비어 있으면(상태 파일 손상·API 응답 누락) 곱셈에서 죽고,
        # 그러면 보유 종목을 아예 못 팔게 된다 - 손실로 직결된다.
        # ★ 값이 없으면 NaN 으로 두어 "판단 불가"로 흘려보낸다. 다른
        #   청산 기법(시간 기반·강제청산)이 여전히 동작할 수 있다.
        entry = pos.entry_price
        if entry is None or (isinstance(entry, float) and isnan(entry)):
            sl = tp = NAN
        else:
            sl = entry * (1 - r.stop_loss_pct)
            tp = entry * (1 + r.take_profit_pct)
        # ★ 분할 매도를 쓰면 익절은 엔진이 나눠서 처리하므로(sizing.py) 여기서는 전량 익절을 끈다(손절은 그대로).
        if getattr(ctx, "scale_out", False):
            tp = NAN

        terms = [
            mk_term(
                "stop_loss", "손절가 도달", last, sl, "<=", unit="원", required=False,
                explain=f"현재가 {fmt_val(last,'원')}이 손절가 {fmt_val(sl,'원')} 이하로 내려갔습니다."
                if last <= sl else f"현재가 {fmt_val(last,'원')}이 손절가 {fmt_val(sl,'원')} 위에 있습니다.",
            ),
            mk_term(
                "take_profit", "익절가 도달", last, tp, ">=", unit="원", required=False,
                explain=f"현재가 {fmt_val(last,'원')}이 익절가 {fmt_val(tp,'원')} 이상입니다."
                if last >= tp else f"현재가 {fmt_val(last,'원')}이 익절가 {fmt_val(tp,'원')}에 못 미칩니다.",
            ),
        ]
        ok = any(t.passed for t in terms)
        return finish_exit(
            technique=self.key, technique_label=self.label,
            symbol=pos.symbol, name=pos.name, theme=pos.theme,
            terms=terms, prev=ctx.prev_verdict, ok=ok, price=last,
            inputs={"stop_loss": sl, "take_profit": tp}, lead=self.description,
        )


class TrailingExit(ExitBase):
    key = "trailing"
    label = "트레일링 스탑"
    description = "일정 수익에 도달한 뒤 고점 대비 되돌리면, 남은 수익을 지키기 위해 청산한다."
    origin = "추적 손절 — 리버모어 이래의 표준"
    standard = "발동선(trailing_arm_pct)과 되돌림 폭(trailing_stop_pct)을 분리해 익절 전에는 건드리지 않게 했다."

    def evaluate(self, pos, bars, last, ctx) -> Verdict:
        r = self.risk
        peak_gain = (pos.peak_price - pos.entry_price) / pos.entry_price if pos.entry_price else NAN
        drop = (pos.peak_price - last) / pos.peak_price if pos.peak_price else NAN

        terms = [
            mk_term(
                "armed", "발동 조건(고점 수익률)", peak_gain, r.trailing_arm_pct, ">=", unit="%",
                required=True,
                explain=f"고점 수익률 {fmt_val(peak_gain,'%')}로 발동 기준 {fmt_val(r.trailing_arm_pct,'%')}를 넘었습니다."
                if not isnan(peak_gain) and peak_gain >= r.trailing_arm_pct
                else f"고점 수익률 {fmt_val(peak_gain,'%')}로 아직 발동 기준에 못 미칩니다.",
            ),
            mk_term(
                "drop", "고점 대비 하락폭", drop, r.trailing_stop_pct, ">=", unit="%", required=True,
                explain=f"고점 {fmt_val(pos.peak_price,'원')} 대비 {fmt_val(drop,'%')} 하락했습니다."
                if not isnan(drop) and drop >= r.trailing_stop_pct
                else f"고점 대비 하락폭 {fmt_val(drop,'%')}로 기준에 못 미칩니다.",
            ),
        ]
        ok = all(t.passed for t in terms)
        return finish_exit(
            technique=self.key, technique_label=self.label,
            symbol=pos.symbol, name=pos.name, theme=pos.theme,
            terms=terms, prev=ctx.prev_verdict, ok=ok, price=last,
            inputs={"peak_price": pos.peak_price}, lead=self.description,
        )


class AtrStopExit(ExitBase):
    key = "atr_stop"
    label = "ATR 변동성 손절"
    description = "종목 고유의 변동성(ATR)만큼 물러난 자리를 손절선으로 쓴다."
    origin = "샹들리에 엑시트 — Chuck LeBeau. ATR 은 Wilder(1978)"
    standard = "원전의 ATR 배수(통상 3배)를 atr_multiple 파라미터로 노출해 종목·전략별로 조정할 수 있게 했다."

    def evaluate(self, pos, bars, last, ctx) -> Verdict:
        period = self._get("atr_period", 14)
        multiple = self._get("atr_multiple", 3.0)
        a = atr(bars, period)
        # ★★★ 실제로 겪은 버그 - entry_price 가 None 이면(상태 파일 손상 등)
        # 곱셈 이전에 바로 뺄셈에서 죽는다. FixedExit 이 이미 겪은 것과
        # 같은 문제 - atr_stop 이 기본 켜짐이 되면서 처음으로 드러났다.
        # 값이 없으면 NaN 으로 흘려보내 "판단 불가"로 처리하고, 다른
        # 청산 기법(고정 손절 등)이 계속 동작할 수 있게 한다.
        entry = pos.entry_price
        if entry is None or (isinstance(entry, float) and isnan(entry)) or isnan(a):
            line = NAN
        else:
            line = entry - a * multiple

        terms = [mk_term(
            "atr_breach", "ATR 손절선 이탈", last, line, "<=", unit="원", required=True,
            explain=f"현재가 {fmt_val(last,'원')}이 ATR 손절선 {fmt_val(line,'원')}(진입가 - ATR×{multiple}) 아래로 내려갔습니다."
            if not isnan(line) and last <= line
            else f"현재가 {fmt_val(last,'원')}이 ATR 손절선 {fmt_val(line,'원')} 위에 있습니다.",
        )]
        ok = terms[0].passed
        return finish_exit(
            technique=self.key, technique_label=self.label,
            symbol=pos.symbol, name=pos.name, theme=pos.theme,
            terms=terms, prev=ctx.prev_verdict, ok=ok, price=last,
            inputs={"atr": a, "line": line}, lead=self.description,
        )


class TimeStopExit(ExitBase):
    key = "time_stop"
    label = "시간 손절"
    description = "정해진 시간을 넘기거나 오래 진전이 없으면 자리를 비워준다."
    origin = "표준. 셋업 시간 프레임의 2~3배"
    standard = "max_hold_minutes 는 exit 설정을 그대로 쓰고, no_progress_minutes 는 0이면 검사를 생략한다."

    def evaluate(self, pos, bars, last, ctx) -> Verdict:
        max_hold = self.max_hold_minutes
        no_progress_minutes = self._get("no_progress_minutes", 0)
        held = ctx.held_minutes

        terms = [mk_term(
            "held", "보유 시간", float(held), float(max_hold), ">=", unit="분", required=False,
            explain=f"보유 시간 {fmt_val(held,'분')}이 최대 보유 {fmt_val(max_hold,'분')}을 넘었습니다."
            if held >= max_hold else f"보유 시간 {fmt_val(held,'분')}으로 아직 여유가 있습니다.",
        )]

        if no_progress_minutes > 0:
            gain = (last - pos.entry_price) / pos.entry_price if pos.entry_price else NAN
            no_progress = held >= no_progress_minutes and not isnan(gain) and gain < 0.01
            terms.append(mk_term(
                "no_progress", "무진전 시간", float(held if no_progress else 0.0),
                float(no_progress_minutes), ">=", unit="분", required=False,
                explain=f"{fmt_val(no_progress_minutes,'분')} 동안 수익률이 +1%를 넘지 못했습니다."
                if no_progress else "아직 무진전 기준에 해당하지 않습니다.",
            ))

        ok = any(t.passed for t in terms)
        return finish_exit(
            technique=self.key, technique_label=self.label,
            symbol=pos.symbol, name=pos.name, theme=pos.theme,
            terms=terms, prev=ctx.prev_verdict, ok=ok, price=last,
            inputs={"held_minutes": held}, lead=self.description,
        )


class MomentumFadeExit(ExitBase):
    key = "momentum_fade"
    label = "모멘텀 소멸"
    description = "거래량 위축·연속 하락·VWAP 이탈 등 상승 동력이 꺼지는 징후가 겹치면 정리한다."
    origin = "모멘텀 소멸 — 다우 이론의 '거래량은 추세를 확인한다'"
    standard = "세 징후 중 2개 이상 겹칠 때만 청산해, 단일 지표의 노이즈에 흔들리지 않게 했다."

    def evaluate(self, pos, bars, last, ctx) -> Verdict:
        fade_ratio = self._get("volume_fade_ratio", 0.5)
        bear_streak_n = self._get("bear_streak", 3)
        # ★★★ "momentum_fade 가 계속 발생하는데 문제가 뭐야?" 로 발견 - 해외주식
        # 페이퍼매매 21건 중 17건(81%)이 momentum_fade 로 청산됐고, 승률 35%·평균
        # 손익 마이너스, 최소 보유 4.1분 만에 잘린 거래도 있었다. 두 가지가 원인이었다.
        min_hold_minutes = self._get("min_hold_minutes", 10)
        vol_avg_bars = self._get("vol_avg_bars", 3)

        entry_volume = getattr(pos, "entry_volume", None)
        # ① "거래량 위축" 이 봉 하나(cur_volume) 대 봉 하나(entry_volume) 비교였다.
        # 진입 봉은 신호가 강해서 산 봉이라 거래량이 원래 튀어 있는 경우가 많고,
        # 이후 정상적으로 움직여도 거래량은 자연히 가라앉는다 - 그래서 이 조건이
        # 진입 직후부터 거의 공짜로 "통과" 상태였다. 최근 vol_avg_bars(기본 3)개
        # 봉의 평균으로 바꿔 단일 봉 노이즈를 줄인다.
        recent_vols = [b.volume for b in bars[-vol_avg_bars:] if not isnan(b.volume)] if bars else []
        cur_volume = (sum(recent_vols) / len(recent_vols)) if recent_vols else NAN
        vol_fade_val = (cur_volume / entry_volume) if entry_volume else NAN
        streak = consecutive_down(bars, bear_streak_n) if bars else NAN
        v = vwap(bars) if bars else NAN

        fade_terms = [
            mk_term(
                "vol_fade", "거래량 위축", vol_fade_val, fade_ratio, "<=", unit="배", required=False,
                explain=f"최근 {vol_avg_bars}봉 평균 거래량이 진입 시점의 {fmt_val(vol_fade_val,'배')}로 기준 {fmt_val(fade_ratio,'배')} 이하입니다."
                if not isnan(vol_fade_val) and vol_fade_val <= fade_ratio
                else f"최근 {vol_avg_bars}봉 평균 거래량이 진입 시점의 {fmt_val(vol_fade_val,'배')}로 아직 여유가 있습니다.",
            ),
            mk_term(
                "bear_streak", "연속 하락", streak, bear_streak_n, ">=", unit="봉", required=False,
                explain=f"{fmt_val(streak,'봉')} 연속 하락 중입니다 (기준 {bear_streak_n}봉)."
                if not isnan(streak) and streak >= bear_streak_n
                else f"연속 하락 {fmt_val(streak,'봉')}으로 기준에 못 미칩니다.",
            ),
            mk_term(
                "below_vwap", "VWAP 이탈", last, v, "<", unit="원", required=False,
                explain=f"현재가 {fmt_val(last,'원')}이 VWAP {fmt_val(v,'원')} 아래로 내려갔습니다."
                if not isnan(v) and last < v else f"현재가 {fmt_val(last,'원')}이 VWAP {fmt_val(v,'원')} 위에 있습니다.",
            ),
        ]
        fade_count = sum(1 for t in fade_terms if t.passed)

        # ② 진입 직후부터 매 평가마다 이 청산을 검사해, 4분 만에 잘린 거래도
        # 있었다. 최소 보유시간(기본 10분) 동안은 이 청산을 보류한다 - 손절/익절/
        # ATR 손절 등 다른 청산 기법은 이 유예와 무관하게 그대로 동작한다.
        held = ctx.held_minutes
        hold_ok = held >= min_hold_minutes
        hold_term = mk_term(
            "min_hold", "최소 보유시간", float(held), float(min_hold_minutes), ">=", unit="분", required=True,
            explain=f"보유 {fmt_val(held,'분')}으로 최소 보유시간 {min_hold_minutes:g}분을 채워 평가합니다."
            if hold_ok
            else f"보유 {fmt_val(held,'분')}으로 최소 보유시간 {min_hold_minutes:g}분에 못 미쳐 아직 청산을 보류합니다.",
        )

        terms = [hold_term] + fade_terms
        ok = hold_ok and fade_count >= 2
        return finish_exit(
            technique=self.key, technique_label=self.label,
            symbol=pos.symbol, name=pos.name, theme=pos.theme,
            terms=terms, prev=ctx.prev_verdict, ok=ok, price=last,
            inputs={}, lead=self.description,
        )


class ForceCloseExit(ExitBase):
    """장 마감 강제청산. 항상 존재하고 끌 수 없다 -
    EXIT_TECHNIQUES 에 넣지 않고 Playbook 이 직접 들고 있는다.
    """
    key = "force_close"
    label = "장 마감 강제청산"
    description = "동시호가 혼란을 피하기 위해 마감 전 정해진 시각에는 보유를 정리한다."
    origin = "표준 - 데이트레이딩 원칙상 포지션을 다음날로 넘기지 않는다."
    standard = "config.yaml 의 exit.force_close_time·force_close_deadline_min 을 그대로 기준으로 쓴다."

    def evaluate(self, pos, bars, last, ctx) -> Verdict:
        force = bool(ctx.force_close)
        terms = [mk_term(
            "force_close", "장 마감 강제청산 시각", 1.0 if force else 0.0, 1.0, ">=", unit="",
            required=False,
            explain="장 마감 강제청산 시각이 되어 정리합니다." if force else "아직 강제청산 시각이 아닙니다.",
        )]
        return finish_exit(
            technique=self.key, technique_label=self.label,
            symbol=pos.symbol, name=pos.name, theme=pos.theme,
            terms=terms, prev=ctx.prev_verdict, ok=force, price=last,
            inputs={}, lead=self.description,
        )


# ━━ 스윙 매매 진입 기법 3종 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★★★ "단타 외에 스윙 매매도 추가해달라"는 요청 - 단타 기법과 결정적으로 다른 점은 봉 하나가
# 1분이 아니라 하루(daytrader/swing_engine.py 가 client.candles(symbol, "1d", ...)로 받는다)라는
# 것뿐이다. sma/ema/highest_high 같은 지표 함수는 봉이 뭘 나타내는지 모르는 채로 그대로 동작하니,
# 기법 코드 자체는 위 단타 기법들과 완전히 같은 방식(Term·Verdict·finish())으로 짠다 - 다만 문턱값은
# 하루 단위 시세에 맞게(예: 20분이 아니라 20거래일) 훨씬 느슨하고 넓게 잡는다.

class SwingMaPullbackEntry(EntryBase):
    # 상승 추세(장기 이동평균 위)에서 단기 이동평균까지 눌린 뒤 반등하는 자리를 잡는다.
    # 단타의 ma_pullback(20분 EMA)과 같은 발상을 20거래일(한 달)·60거래일(석 달) 단위로 늘렸다.
    key = "swing_ma_pullback"
    label = "스윙 이동평균 눌림목"
    description = (
        "60거래일 이동평균 위(중기 상승 추세)에서 20거래일 이동평균까지 눌렸다가 "
        "전날 고가를 다시 넘어서는 날을 잡는다."
    )
    origin = "이동평균 되돌림 - 추세추종 매매의 표준 진입 방식(윌리엄 오닐 CAN SLIM 등에서 반복 등장)."
    standard = (
        "단타의 이동평균 눌림목(20분 EMA)과 같은 구조를, 봉을 1분봉 대신 일봉으로 바꿔 "
        "20거래일(약 한 달)·60거래일(약 석 달) 단위로 늘렸다 - 스윙은 하루 안에 끝내지 않으므로 "
        "짧은 눌림이 아니라 여러 날에 걸친 되돌림을 본다."
    )
    windows: tuple = ("main",)

    def evaluate(self, bars, ctx) -> Verdict:
        trend_ma = self._get("trend_ma", 60)
        pullback_ma = self._get("pullback_ma", 20)
        touch_band = self._get("touch_band", 0.03)  # 단타(0.4%)보다 훨씬 넓다 - 일봉은 하루 변동폭 자체가 크다.

        price = bars[-1].close if bars else 0.0
        need = max(trend_ma, pullback_ma) + 2
        terms = [mk_term(
            "bars_enough", "확보된 봉 수(일봉)", float(len(bars)), float(need), ">=", unit="일",
            required=True, explain=f"판정에 필요한 {need}거래일 중 {len(bars)}일이 모였습니다.",
        )]
        if len(bars) < need:
            return finish(
                phase="entry", technique=self.key, technique_label=self.label,
                symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
                terms=terms, prev=ctx.prev_verdict, price=price,
                inputs={"trend_ma": trend_ma, "pullback_ma": pullback_ma}, lead=self.description,
            )

        close = bars[-1].close
        prev_bar = bars[-2]
        trend_v = sma(bars, trend_ma)
        pull_v = sma(bars, pullback_ma)
        touch = abs(prev_bar.low - pull_v) / pull_v if not isnan(pull_v) and pull_v else NAN

        terms.append(mk_term(
            "uptrend", f"{trend_ma}일 이동평균 위(중기 상승 추세)", close, trend_v, ">=", unit="원",
            required=True,
            explain=f"종가 {fmt_val(close,'원')}이 {trend_ma}일 이동평균 {fmt_val(trend_v,'원')} 위에 있습니다."
            if not isnan(trend_v) and close >= trend_v else f"종가가 {trend_ma}일 이동평균 아래에 있어 중기 상승 추세가 아닙니다.",
        ))
        terms.append(mk_term(
            "touched", f"{pullback_ma}일 이동평균 되돌림 접촉", touch, touch_band, "<=", unit="%",
            required=True,
            explain=f"전날 저가가 {pullback_ma}일 이동평균 대비 {fmt_val(touch,'%')} 이내로 접촉했습니다."
            if not isnan(touch) and touch <= touch_band else f"전날 저가가 이동평균에서 {fmt_val(touch,'%')} 떨어져 있습니다.",
        ))
        terms.append(mk_term(
            "trigger", "전날 고가 재돌파", close, prev_bar.high, ">", unit="원", required=True,
            explain=f"종가 {fmt_val(close,'원')}이 전날 고가 {fmt_val(prev_bar.high,'원')}을 다시 넘었습니다."
            if close > prev_bar.high else f"종가가 전날 고가 {fmt_val(prev_bar.high,'원')}을 아직 못 넘었습니다.",
        ))

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=close,
            inputs={"trend_ma": trend_ma, "pullback_ma": pullback_ma, "touch_band": touch_band},
            lead=self.description,
        )


class SwingBreakoutEntry(EntryBase):
    # 한 달가량의 박스권(횡보 구간) 상단을 거래량을 동반해 뚫는 날을 잡는다.
    key = "swing_breakout"
    label = "스윙 박스권 돌파"
    description = "최근 20거래일(약 한 달) 고가를 거래량을 동반해 뚫는 날을 잡는다."
    origin = "돈치안 채널 돌파(N일 최고가 돌파) - 추세추종의 가장 오래된 형태 중 하나."
    standard = "단타 돌파 추종(breakout_lookback봉=분)을 20거래일(한 달) 단위로 늘렸다."
    windows: tuple = ("main",)

    def evaluate(self, bars, ctx) -> Verdict:
        lookback = self._get("lookback", 20)
        surge = self._get("volume_surge_ratio", 1.5)

        price = bars[-1].close if bars else 0.0
        need = lookback + 2
        terms = [mk_term(
            "bars_enough", "확보된 봉 수(일봉)", float(len(bars)), float(need), ">=", unit="일",
            required=True, explain=f"판정에 필요한 {need}거래일 중 {len(bars)}일이 모였습니다.",
        )]
        if len(bars) < need:
            return finish(
                phase="entry", technique=self.key, technique_label=self.label,
                symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
                terms=terms, prev=ctx.prev_verdict, price=price,
                inputs={"lookback": lookback}, lead=self.description,
            )

        close = bars[-1].close
        open_ = bars[-1].open
        hh = highest_high(bars[:-1], lookback)
        vr = volume_ratio(bars, lookback)

        terms.append(mk_term(
            "breakout", f"종가 vs 최근 {lookback}거래일 고가", close, hh, ">", unit="원", required=True,
            explain=f"종가 {fmt_val(close,'원')}이 최근 {lookback}거래일 고가 {fmt_val(hh,'원')}을 넘었습니다."
            if not isnan(hh) and close > hh else f"종가가 최근 {lookback}거래일 고가 {fmt_val(hh,'원')}을 넘지 못했습니다.",
        ))
        terms.append(mk_term(
            "bullish", "양봉 여부", close - open_, 0.0, ">", unit="원", required=True,
            explain="종가가 시가보다 높은 양봉입니다." if close > open_ else "종가가 시가보다 낮아 음봉입니다.",
        ))
        terms.append(mk_term(
            "volume", "거래량 급증", vr, surge, ">=", unit="배", required=True,
            explain=f"거래량이 최근 {lookback}거래일 평균 대비 {fmt_val(vr,'배')}로 기준 {fmt_val(surge,'배')} 이상입니다."
            if not isnan(vr) and vr >= surge else f"거래량이 평균 대비 {fmt_val(vr,'배')}에 그쳐 확인이 부족합니다.",
        ))

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=close,
            inputs={"lookback": lookback, "volume_surge_ratio": surge}, lead=self.description,
        )


class SwingGoldenCrossEntry(EntryBase):
    # 단기 이동평균이 장기 이동평균을 아래에서 위로 뚫고 올라간 지 얼마 안 된(추세 전환 초입) 날을 잡는다.
    key = "swing_golden_cross"
    label = "스윙 골든크로스"
    description = "20거래일 이동평균이 60거래일 이동평균을 최근 며칠 안에 상향 돌파한(골든크로스) 종목을 잡는다."
    origin = "골든크로스(이동평균 교차) - 가장 널리 쓰이는 장기 추세 전환 신호."
    standard = "막 교차한 지 lookback_days 이내인 것만 인정해 이미 많이 오른 뒤(교차 후 한참 지난) 뒤늦게 따라 사는 것을 막는다."
    windows: tuple = ("main",)

    def evaluate(self, bars, ctx) -> Verdict:
        short_ma = self._get("short_ma", 20)
        long_ma = self._get("long_ma", 60)
        lookback_days = self._get("lookback_days", 5)

        price = bars[-1].close if bars else 0.0
        need = long_ma + lookback_days + 2
        terms = [mk_term(
            "bars_enough", "확보된 봉 수(일봉)", float(len(bars)), float(need), ">=", unit="일",
            required=True, explain=f"판정에 필요한 {need}거래일 중 {len(bars)}일이 모였습니다.",
        )]
        if len(bars) < need:
            return finish(
                phase="entry", technique=self.key, technique_label=self.label,
                symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
                terms=terms, prev=ctx.prev_verdict, price=price,
                inputs={"short_ma": short_ma, "long_ma": long_ma}, lead=self.description,
            )

        close = bars[-1].close
        short_now = sma(bars, short_ma)
        long_now = sma(bars, long_ma)
        short_prev = sma(bars[:-lookback_days], short_ma)
        long_prev = sma(bars[:-lookback_days], long_ma)

        terms.append(mk_term(
            "crossed_up", f"{short_ma}일선 > {long_ma}일선(지금)", short_now, long_now, ">", unit="원",
            required=True,
            explain=f"{short_ma}일 이동평균 {fmt_val(short_now,'원')}이 {long_ma}일 이동평균 {fmt_val(long_now,'원')} 위에 있습니다."
            if not isnan(short_now) and not isnan(long_now) and short_now > long_now
            else f"{short_ma}일 이동평균이 아직 {long_ma}일 이동평균 아래에 있습니다.",
        ))
        terms.append(mk_term(
            "was_below", f"{lookback_days}거래일 전엔 {short_ma}일선 <= {long_ma}일선", short_prev, long_prev, "<=",
            unit="원", required=True,
            explain=f"{lookback_days}거래일 전에는 {short_ma}일 이동평균이 {long_ma}일 이동평균 아래였습니다(막 교차) - "
            "이미 오래전에 교차해 많이 오른 뒤가 아닙니다."
            if not isnan(short_prev) and not isnan(long_prev) and short_prev <= long_prev
            else f"{lookback_days}거래일 전에도 이미 {short_ma}일선이 위였습니다 - 교차한 지 오래돼 뒤늦게 따라 사는 자리입니다.",
        ))

        return finish(
            phase="entry", technique=self.key, technique_label=self.label,
            symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
            terms=terms, prev=ctx.prev_verdict, price=close,
            inputs={"short_ma": short_ma, "long_ma": long_ma, "lookback_days": lookback_days},
            lead=self.description,
        )


# ━━ 파사드 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ENTRY_TECHNIQUES = {
    "open_gap": OpenGapEntry,
    "close_squeeze": CloseSqueezeEntry,
    "breakout": BreakoutEntry,
    "theme_leader": ThemeLeaderEntry,
    "vwap_pullback": VwapPullbackEntry,
    "vwap_reclaim": VwapReclaimEntry,
    "orb": OrbEntry,
    "volume_dry_pop": VolumeDryPopEntry,
    "bull_flag": BullFlagEntry,
    "ma_pullback": MaPullbackEntry,
    "rsi_pullback": RsiPullbackEntry,
    "volatility_breakout": VolatilityBreakoutEntry,
    "swing_ma_pullback": SwingMaPullbackEntry,
    "swing_breakout": SwingBreakoutEntry,
    "swing_golden_cross": SwingGoldenCrossEntry,
}

EXIT_TECHNIQUES = {
    "fixed": FixedExit,
    "trailing": TrailingExit,
    "atr_stop": AtrStopExit,
    "time_stop": TimeStopExit,
    "momentum_fade": MomentumFadeExit,
}
# ★ ForceCloseExit 는 이 dict 에 넣지 않는다. 끌 수 없는 규칙이므로
#   Playbook 이 항상 직접 들고 있는다.

EXIT_PRIORITY = [
    "force_close", "fixed_stop", "atr_stop", "fixed_take",
    "trailing", "momentum_fade", "time_stop",
]


class Playbook:
    """진입/청산 기법을 config 대로 조립해 순서대로 평가한다.

    ★ entry_order/exit_enabled 를 명시적으로 넘기면 cfg.strategy.* 대신
    그걸 쓴다 - 암호화폐·해외주식이 "국내주식과 같은 기법 레지스트리"를
    쓰면서도 "어떤 기법을 켤지"는 시장별로 따로 정할 수 있게 하기 위해서다
    (예: 암호화폐만 변동성 돌파를 켜고 국내주식은 안 켤 수 있다).
    """

    def __init__(
        self, cfg, entry_order: list | None = None, exit_enabled: list | None = None,
        risk=None, max_hold_minutes: float | None = None, entry_params: dict | None = None,
        market: str = "domestic", learning_mode: str = "entry_pref",
    ):
        """★★★ risk/max_hold_minutes - 암호화폐·해외주식처럼 국내주식과
        다른 손절·익절·최대보유시간을 쓰는 시장을 위한 오버라이드. 안
        넘기면(국내주식) 지금까지처럼 cfg.risk/cfg.exit.max_hold_minutes 를
        그대로 쓴다 - 기존 동작에 아무 영향이 없다.

        ★★★ entry_params - 같은 이유로 진입 기법 파라미터도 시장별로
        다를 수 있다. 예를 들어 ma_pullback/rsi_pullback 의 추세 필터
        (ma_length)는 원래 국내주식의 "당일 청산" 전제(1분봉 20개=20분)로
        골랐는데, 코인은 최대 보유시간이 24시간이라 20분짜리 추세 필터로는
        의미가 없다(추세가 아니라 잡음을 본다). cfg.strategy.params 는
        시장 구분 없이 공유되므로, 여기서 시장별 값을 얹어 덮어쓸 수
        있게 한다. 안 넘기면 기존처럼 cfg.strategy.p(key) 그대로 쓴다.

        ★ market - technique_backtest.py 가 실제 시세로 매긴 "이 종목/테마는 이 기법이 최근
        더 잘 맞았다"는 선호도(technique_prefs.py)를 어느 시장 몫에서 찾을지 구분한다.

        ★★★ learning_mode - "거래시장별로 어떤 모델을 선택할지는 사용자가 선택" 요청에 따른
        3단계 매매 모델(시장마다 cfg.risk/overseas/crypto/swing.technique_learning_mode 로 고른다):
          "none"        - 기본적인 단타 룰로만(과거 실적·백테스트 가산점을 전혀 안 씀, 순수 신호 강도)
          "entry_pref"  - (기본값) 진입만 가산점 반영 - 지금까지의 동작 그대로(실전 실적 ±30%,
                          [실험실] 최근 시세 백테스트 선호 1.15배)
          "entry_exit_pref" - entry_pref 에 더해, 보유 중 계속 추적한 "이 종목은 청산을 얼마나
                          효율적으로 했는지"(exit_efficiency.py) 기록을 다음에 같은 종목에 들어갈 때
                          익절 목표를 추가로 넓히는 가산점으로 쓴다.
        """
        self.cfg = cfg
        self.market = market
        self.learning_mode = learning_mode if learning_mode in ("none", "entry_pref", "entry_exit_pref") else "entry_pref"
        entry_order = entry_order if entry_order is not None else cfg.strategy.entry_order
        exit_enabled = exit_enabled if exit_enabled is not None else cfg.strategy.exit_enabled
        self.entry_order = entry_order
        self.exit_enabled = exit_enabled
        entry_params = entry_params or {}
        self.entries = [
            ENTRY_TECHNIQUES[key](cfg, {**cfg.strategy.p(key), **entry_params.get(key, {})})
            for key in entry_order
        ]
        self.exits = [
            EXIT_TECHNIQUES[key](cfg, cfg.strategy.p(key), risk=risk, max_hold_minutes=max_hold_minutes)
            for key in exit_enabled
        ]
        self.force_close = ForceCloseExit(cfg)
        # ★ 기법별 과거 실적 - 엔진이 set_performance() 로 넣어 준다.
        # 비어 있으면 순수 신호 강도만으로 판단한다(안전한 기본값).
        self._performance: dict = {}

    def set_performance(self, stats: dict | None) -> None:
        """★ 기법별 과거 실적(ledger.by_technique() 결과)을 받아 둔다.
        엔진이 하루 시작·재개 시 넣어 주고, 없으면 순수 신호 강도만 쓴다.
        """
        self._performance = stats or {}

    def _performance_multiplier(self, technique_key: str) -> tuple:
        """★★★ 과거 실적을 신호 점수에 얼마나 반영할지 계산한다.
        돌려주는 값: (곱할 배수, 화면에 보여줄 설명)

        설계 의도 - 실적 반영은 양날의 검이다. 거래 표본이 적으면 승률은
        운에 크게 좌우돼서, 우연히 잘 맞았던 기법에 과적합될 위험이 크다.
        그래서 세 가지 안전장치를 둔다:
          1. min_trades_for_weight(기본 20건) 미만이면 아예 반영하지 않는다.
          2. 반영하더라도 performance_weight(기본 0.3)만큼만 - 신호 강도가
             여전히 주인공이고 실적은 보조다.
          3. 배수를 0.7~1.3 으로 묶는다 - 어떤 기법도 실적만으로 완전히
             배제되거나 독점하지 않게 한다.
        """
        s = self.cfg.strategy
        weight = getattr(s, "performance_weight", 0.0) or 0.0
        if weight <= 0:
            return 1.0, ""
        stats = getattr(self, "_performance", {}) or {}
        row = stats.get(technique_key)
        if not row:
            return 1.0, ""
        trades = row.get("trades", 0)
        min_trades = getattr(s, "min_trades_for_weight", 20)
        if trades < min_trades:
            return 1.0, f"과거 {trades}건뿐이라 실적 반영 안 함(최소 {min_trades}건)"

        win_rate = row.get("win_rate", 0.0)
        # ★ 승률 0.5(반반)를 기준으로 위아래를 본다 - 0.5 면 1.0배(중립),
        # 0.7 이면 1 + 0.3*0.4 = 1.12배, 0.3 이면 0.88배가 된다.
        raw = 1.0 + weight * ((win_rate - 0.5) * 2)
        mult = max(0.7, min(1.3, raw))
        return mult, f"과거 {trades}건 승률 {win_rate*100:.0f}% → 신호 {mult:.2f}배"

    def _preference_multiplier(self, technique_key: str, ctx) -> tuple:
        """★ technique_backtest.py 가 실제 시세로 매긴 "이 종목/테마는 이 기법이 최근 더
        잘 맞았다"는 결과를 가산점으로 준다. 강제 규칙이 아니라 가산점이다 - 표본이 하루치
        시세뿐이라 다른 기법을 막지는 않는다(신호가 없으면 안 산다는 원칙은 그대로다).
        선호 기법을 찾지 못하면(백테스트를 돌린 적 없거나 이 종목·테마가 아니면) 중립(1.0)."""
        try:
            from daytrader import technique_prefs
            symbol = getattr(ctx, "symbol", "")
            theme = getattr(ctx, "theme", "")
            best = technique_prefs.best_for(self.cfg, self.market, symbol, theme)
        except Exception:
            return 1.0, ""
        if best and best == technique_key:
            return technique_prefs.PREFERENCE_BOOST, f"최근 시세 백테스트에서 이 종목에 더 잘 맞은 기법 → {technique_prefs.PREFERENCE_BOOST:.2f}배"
        return 1.0, ""

    def evaluate_entry(self, bars, ctx):
        """★★★ "시세 변동을 모니터링해서 최적의 기법을 적용해 승률을
        높인다"는 요청에 따라 바꾼 부분.

        예전 방식 - entry_order 에 적힌 순서대로 평가해서 "처음 통과한"
        기법을 그냥 썼다. 그래서 목록 첫 기법이 간신히 통과하면, 뒤쪽
        기법이 훨씬 강한 신호를 내도 무시됐다(각 기법이 score 를 계산해
        두는데도 승자 선정에 안 썼다).

        지금 방식(best_signal) - 통과한 기법을 전부 모아, 신호 강도(score)에
        과거 실적 배수를 곱한 "최종 점수"가 가장 높은 것을 승자로 뽑는다.
        동점이면 entry_order 순서를 따른다(설정한 우선순위를 존중).

        ★ 통과 못한 평가도 전부 돌려준다 - 안 산 이유가 화면에 남아야 한다.
        """
        all_verdicts = []
        passed = []
        window = getattr(ctx, "window", "main") or "main"
        for idx, technique in enumerate(self.entries):
            if window not in getattr(technique, "windows", ("main",)):
                continue  # 이 시간대에는 쓰지 않는 기법
            v = technique.evaluate(bars, ctx)
            all_verdicts.append(v)
            if v.ok:
                passed.append((idx, technique, v))

        if not passed:
            return None, all_verdicts

        if not getattr(self.cfg.strategy, "best_signal", False):
            # ★ 예전 방식(목록 순서대로 첫 통과)을 그대로 쓰고 싶을 때.
            return passed[0][2], all_verdicts

        best = None
        for idx, technique, v in passed:
            if self.learning_mode == "none":
                # ★ "기본적인 단타 룰로만" - 과거 실적·백테스트 가산점을 전부 끄고 순수 신호 강도만 본다.
                mult, why, pref_mult, pref_why = 1.0, "", 1.0, ""
            else:
                mult, why = self._performance_multiplier(technique.key)
                pref_mult, pref_why = self._preference_multiplier(technique.key, ctx)
            final = (v.score or 0.0) * mult * pref_mult
            # ★ 왜 이 기법이 뽑혔는지(또는 안 뽑혔는지) 화면에 남긴다.
            v.selection_score = final
            reasons = [f"신호 강도 {v.score:.2f}"]
            if why and "→" in why:
                reasons.append(f"× {why.split('→')[-1].strip()}")
            if pref_why:
                reasons.append(f"× {pref_mult:.2f}(종목 백테스트)")
            v.selection_reason = (
                " ".join(reasons) + f" = 최종 {final:.2f}"
                + (f" ({why})" if why and "→" not in why else "")
            )
            if best is None or final > best[0] or (final == best[0] and idx < best[1]):
                best = (final, idx, v)

        winner = best[2]
        winner.selection_note = (
            f"통과한 {len(passed)}개 기법 중 최종 점수가 가장 높아 선택했습니다."
            if len(passed) > 1 else "통과한 기법이 이것 하나였습니다."
        )
        return winner, all_verdicts

    def evaluate_exit(self, pos, bars, last, ctx):
        """우선순위 고정: 손실을 막는 규칙이 항상 먼저다.
        force_close > fixed.stop_loss > atr_stop > fixed.take_profit
                    > trailing > momentum_fade > time_stop
        청산 결정이 나면 narrative 마지막에 "닿지 않은 다른 기법"을 덧붙인다.
        """
        exit_by_key = {t.key: t for t in self.exits}
        fixed = exit_by_key.get("fixed")

        fc_verdict = self.force_close.evaluate(pos, bars, last, ctx)
        fixed_verdict = fixed.evaluate(pos, bars, last, ctx) if fixed else None

        # 우선순위 슬롯 = (검증결과, 발동여부, 화면표시용 이름)
        slots: dict[str, tuple] = {"force_close": (fc_verdict, fc_verdict.ok, self.force_close.label)}

        if fixed_verdict is not None:
            sl_term = fixed_verdict.term("stop_loss")
            tp_term = fixed_verdict.term("take_profit")
            slots["fixed_stop"] = (fixed_verdict, bool(sl_term and sl_term.passed), "고정 손절")
            slots["fixed_take"] = (fixed_verdict, bool(tp_term and tp_term.passed), "고정 익절")

        for key in ("atr_stop", "trailing", "momentum_fade", "time_stop"):
            tech = exit_by_key.get(key)
            if tech is None:
                continue
            v = tech.evaluate(pos, bars, last, ctx)
            slots[key] = (v, v.ok, tech.label)

        winner_key = next((k for k in EXIT_PRIORITY if k in slots and slots[k][1]), None)
        if winner_key is None:
            return None

        winner_verdict, _, _ = slots[winner_key]
        untouched = [label for key, (_, ok, label) in slots.items() if key != winner_key and not ok]
        if untouched:
            winner_verdict.narrative = (
                f"{winner_verdict.narrative} 닿지 않은 다른 기법: {', '.join(untouched)}."
            ).strip()

        return winner_verdict

    def bonus_table(self) -> list:
        """["각 매매기법별로 가산점이 어떻게 되어 있는지 거래시장별로 볼수 있게해"] 요청의 구현.
        지금 이 시장(self.market)에서 진입 기법별로 실전 승률 기반 가산점(_performance_multiplier)이
        실제로 몇 배 걸려 있는지 보여준다. 종목별 [실험실] 백테스트 선호도(_preference_multiplier)는
        기법이 아니라 "종목×기법" 조합에 매겨지는 값이라 여기(기법별 고정 표)에는 넣지 않는다 -
        보고 싶으면 해당 종목의 매매 사유(selection_reason)에서 확인해야 한다.
        learning_mode == "none" 이면 애초에 반영을 안 하니 전부 중립(1.0)으로 보여준다 - "꺼져
        있다"는 사실 자체가 화면에서 확인돼야 한다."""
        out = []
        for tech in self.entries:
            if self.learning_mode == "none":
                mult, why = 1.0, "매매 모델이 '기본 룰만'이라 실적 가산점을 쓰지 않습니다."
            else:
                mult, why = self._performance_multiplier(tech.key)
                if not why:
                    why = "표본이 없거나 부족해 아직 중립(1.0)입니다."
            out.append({"key": tech.key, "label": tech.label, "multiplier": mult, "why": why})
        return out

    def describe(self) -> list:
        """켜져 있는 기법들의 출처와 설명. 원칙 10 문서와 설정 화면이 이 목록을 그대로 쓴다."""
        out = []
        for tech in self.entries:
            out.append({
                "phase": "entry", "key": tech.key, "label": tech.label,
                "description": tech.description, "origin": tech.origin, "standard": tech.standard,
            })
        out.append({
            "phase": "exit", "key": self.force_close.key, "label": self.force_close.label,
            "description": self.force_close.description,
            "origin": self.force_close.origin, "standard": self.force_close.standard,
        })
        for tech in self.exits:
            out.append({
                "phase": "exit", "key": tech.key, "label": tech.label,
                "description": tech.description, "origin": tech.origin, "standard": tech.standard,
            })
        return out
