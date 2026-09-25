"""투자금액 배분과 분할 매수·분할 매도 - 국내·해외·암호화폐가 같은 규칙을 쓴다.

사용자가 정하는 것은 시장별 "총 투자금액"과 "동시 보유 종목 수"뿐이다. 나머지는 여기서 자동으로 정한다.

  종목당 한도(cap)   = 시장 총 투자금액 / 동시 보유 종목 수          - 한 종목에 이 이상은 절대 넣지 않는다
  첫 매수            = cap × 첫 매수 비율(기본 50%) × 배수(0.6~1.4) × 변동성 배수(0.6~1.2)
                        배수 = 신호 강도 60% + 테마 근거 크기 40%(순위·동반 상승·상승률·대장주)
                        변동성 배수 = 장중 ATR 이 크면 적게(같은 위험을 맞추기 위해)
  추가 매수(피라미딩) = 이익 중인 종목에만, 남은 몫을 균등 분할(기본 2번 × 25%)
                        - 마지막 매수가보다 (손절폭의 40%)만큼 더 올랐을 때만
  분할 매도          = 1차: 익절폭의 절반에 도달하면 보유량의 34%(근거가 크면 덜, 출렁임이 크면 더) → 그 뒤 본전 이하면 나머지 정리
                        2차: 익절폭에 도달하면 남은 수량의 50%
                        나머지: 추적 손절·ATR 손절 등으로 끌고 간다(이익을 더 키울 여지)

★ 이 규칙들은 "이익 나는 종목에 더 싣고, 지는 종목에는 더 싣지 않으며, 이익은 나눠 챙기되 일부는 끌고 간다"는
   추세 추종 단타의 일반 원칙(Van Tharp의 포지션 사이징, 피라미딩)을 따른 것이다. 실제 시장에서 최적임이
   증명된 값은 아니므로, 모의매매 기록으로 검증한 뒤 조정해야 한다.
"""

from __future__ import annotations

from math import isnan, nan as NAN

from daytrader.signals import atr, adx


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def signal_strength(verdict) -> float:
    """진입 판정의 강도 0~1. 통과 기준을 얼마나 여유 있게 넘었는지(필수 항목 여유의 평균)로 잰다.

    기준선에 간신히 걸친 신호는 0 에 가깝고, 기준의 100% 이상 초과한 항목이 많을수록 1 에 가깝다.
    비교 방향이 있는 숫자 항목(>=, <=, >, <)만 본다 - bool·구간 항목은 여유를 잴 수 없다.
    """
    try:
        terms = [t for t in (getattr(verdict, "terms", None) or [])
                 if getattr(t, "required", False) and getattr(t, "passed", False)
                 and getattr(t, "op", "") in (">=", "<=", ">", "<")]
    except Exception:
        return 0.5
    ratios = []
    for t in terms:
        th, mg = getattr(t, "threshold", None), getattr(t, "margin", None)
        if th is None or mg is None:
            continue
        try:
            th, mg = float(th), float(mg)
        except (TypeError, ValueError):
            continue
        if isnan(th) or isnan(mg) or abs(th) < 1e-12:
            continue
        ratios.append(_clamp(mg / abs(th), 0.0, 1.0))
    if not ratios:
        return 0.5  # 여유를 잴 수 없으면 중간으로 본다(크게도 작게도 사지 않는다).
    return sum(ratios) / len(ratios)


# ━━ 근거의 크기(테마)와 장중 변동성 - 매수·매도 금액을 자동으로 조절하는 재료 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def theme_conviction(*, theme_rank: int, breadth: int, intensity: float, rank_in_theme: int) -> float:
    """테마 근거의 크기 0~1. 오늘 그 테마가 몇 위인지, 몇 종목이 함께 올랐는지, 얼마나 올랐는지(중앙값), 그 안에서 대장주인지.

      순위 25%(1위=1, 2위=0.5 …) + 동반 상승 종목 수 20%(5종목 이상=1) + 상승률 중앙값 35%(5%=1) + 대장주 여부 20%(1위 1, 2위 0.6, 그 밖 0.4)

    테마 근거가 없는 종목(직접 추가한 관심 종목 등)은 중립 0.5 - 크게도 작게도 사지 않는다.
    """
    if not theme_rank or theme_rank <= 0:
        return 0.5
    rank_s = 1.0 / theme_rank
    breadth_s = _clamp((float(breadth or 0) - 1.0) / 4.0, 0.0, 1.0)
    try:
        intens_s = _clamp(float(intensity or 0.0) / 0.05, 0.0, 1.0)
    except (TypeError, ValueError):
        intens_s = 0.0
    leader_s = 1.0 if rank_in_theme == 1 else 0.6 if rank_in_theme == 2 else 0.4
    return _clamp(0.25 * rank_s + 0.20 * breadth_s + 0.35 * intens_s + 0.20 * leader_s, 0.0, 1.0)


def vol_ratio(bars, stop_pct: float, period: int = 14):
    """장중 변동성 비율 = (분봉 ATR ÷ 현재가) ÷ 기준(손절폭의 20%). 1 이면 평소 수준, 2 면 손절폭에 비해 두 배로 출렁이는 종목.
    봉이 모자라거나 값이 이상하면 None(조절하지 않는다)."""
    try:
        a = atr(bars, period)
        price = bars[-1].close
    except Exception:
        return None
    if a is None or isnan(a) or not price or price <= 0 or not stop_pct or stop_pct <= 0:
        return None
    return (a / price) / (0.2 * stop_pct)


def vol_multiplier(ratio, sz) -> float:
    """출렁임이 클수록 적게 산다(같은 손절폭에서 같은 위험을 맞추기 위해). 0.6~1.2배."""
    if not sz.dynamic or ratio is None or ratio <= 0:
        return 1.0
    return _clamp(1.0 / ratio, 0.6, 1.2)


def combined_strength(signal: float, conviction, sz) -> float:
    """신호 강도(60%)와 테마 근거의 크기(40%)를 합친 매수 강도. 근거 정보가 없거나 자동 조절을 끄면 신호 강도만."""
    if not sz.dynamic or conviction is None:
        return signal
    return 0.6 * signal + 0.4 * conviction


def signal_multiplier(strength: float, sz) -> float:
    """신호 강도 → 매수 배수(min_mult ~ max_mult). 신호 배분을 끄면 항상 1."""
    if not sz.signal_sizing:
        return 1.0
    return sz.min_mult + (sz.max_mult - sz.min_mult) * _clamp(strength, 0.0, 1.0)


def position_cap(budget: float, max_positions: int) -> float:
    """종목 하나에 넣을 수 있는 최대 금액."""
    return max(0.0, float(budget or 0.0)) / max(1, int(max_positions or 1))


def _first_ratio(sz) -> float:
    """첫 매수가 종목당 한도에서 차지하는 기본 비율. 추가 매수를 끄면 신호가 가장 강할 때 한도를 꽉 채우도록 잡는다."""
    if sz.scale_in and sz.max_adds > 0:
        return sz.initial_ratio
    return 1.0 / max(1.0, sz.max_mult) if sz.signal_sizing else 1.0


def entry_amount(cap: float, strength: float, sz, *, conviction=None, vol=None) -> float:
    """첫 매수 금액(한도를 절대 넘지 않는다). strength=신호 강도, conviction=테마 근거 크기(없으면 None), vol=변동성 비율(vol_ratio)."""
    mult = signal_multiplier(combined_strength(strength, conviction, sz), sz) * vol_multiplier(vol, sz)
    return min(cap, cap * _first_ratio(sz) * mult)


def add_amount(cap: float, invested: float, adds: int, sz, *, conviction=None, vol=None) -> float:
    """추가 매수 1회 금액. 한도 안에서 남은 몫을 (남은 횟수)로 균등 분할하고, 근거가 큰 종목은 더·출렁이는 종목은 덜(0.7~1.3배).
    더 살 수 없으면 0."""
    if not sz.scale_in or adds >= sz.max_adds or sz.max_adds <= 0:
        return 0.0
    room = cap - invested
    if room <= 0:
        return 0.0
    each = cap * (1.0 - sz.initial_ratio) / sz.max_adds
    if sz.dynamic:
        conv = 0.5 if conviction is None else conviction
        each *= _clamp(0.7 + 0.6 * conv, 0.7, 1.3) * vol_multiplier(vol, sz)
    return max(0.0, min(each, room))


def add_step(sz, stop_pct: float) -> float:
    """추가 매수 간격(수익률). 자동이면 손절폭의 40% - 두 번 추가해도(80%) 1차 분할 매도(익절폭의 절반, 보통 손절폭과 같음)보다 아래라서
    "더 싣자마자 바로 덜어내는" 일이 없다."""
    return sz.add_step_pct if sz.add_step_pct and sz.add_step_pct > 0 else 0.4 * stop_pct


def add_due(*, adds: int, last_fill: float, avg_entry: float, price: float, scaled_out: int, sz, stop_pct: float) -> bool:
    """지금 추가 매수 시점인가(가격 조건만 본다 - 신호 재확인·현금은 엔진이 따로 본다)."""
    if not sz.scale_in or adds >= sz.max_adds or scaled_out > 0:
        return False
    if not last_fill or not avg_entry or not price:
        return False
    step = add_step(sz, stop_pct)
    return price >= last_fill * (1 + step) and price >= avg_entry * (1 + step)


def dynamic_take_profit_pct(base_take_pct: float, bars, sz) -> float:
    """기본 익절폭은 그대로 두되, 추세가 강할 때(ADX)만 목표를 넓혀 크게 먹는다.

    ADX 는 "추세가 있느냐"만 재는 지표(방향 무관) - 20~25 위면 추세, 아래는 횡보로 본다.
    추세가 약하거나(횡보) 데이터가 부족해 ADX 를 못 재면 기본값을 그대로 쓴다(공격적으로 넓히지 않는다).
    강한 추세(sz.take_profit_adx_threshold~take_profit_adx_full)에서는 최대 sz.take_profit_max_widen
    배까지 선형으로 넓힌다(설정값 - sizing.py 를 안 고치고 [설정] 화면에서 조절 가능).
    "목표에 못 미쳐도 미리 정리"하는 쪽은 여기가 아니라 모멘텀 소멸(momentum_fade) 청산 기법이 맡는다.
    """
    strong_adx, full_adx, max_mult = sz.take_profit_adx_threshold, sz.take_profit_adx_full, sz.take_profit_max_widen
    a = adx(bars) if bars else NAN
    if isnan(a) or a < strong_adx:
        return base_take_pct
    mult = 1.0 + (max_mult - 1.0) * _clamp((a - strong_adx) / (full_adx - strong_adx), 0.0, 1.0)
    return base_take_pct * mult


def exit_first_at(sz, take_pct: float) -> float:
    return sz.first_exit_at if sz.first_exit_at and sz.first_exit_at > 0 else 0.5 * take_pct


def exit_ratio(base: float, sz, *, conviction=None, vol=None) -> float:
    """분할 매도 비율을 상황에 맞게 조절한다: 근거가 큰 종목은 덜 팔고(더 끌고 감), 출렁임이 큰 장은 더 판다(먼저 챙김).
    자동 조절을 끄면 설정한 비율 그대로. 결과는 15%~60%."""
    if not sz.dynamic:
        return base
    conv = 0.5 if conviction is None else conviction
    v = 1.0 if vol is None else _clamp(vol, 0.5, 2.0)
    return _clamp(base * (1.25 - 0.5 * conv) * (1.0 + 0.3 * (v - 1.0)), 0.15, 0.60)


def exit_step(*, scaled_out: int, entry: float, price: float, sz, take_pct: float, conviction=None, vol=None):
    """분할 매도 시점이면 (파는 비율, 표식)을 돌려준다. 비율은 '지금 보유 수량' 기준. 아니면 None."""
    if not sz.scale_out or not entry or not price:
        return None
    gain = (price - entry) / entry
    if scaled_out == 0 and gain >= exit_first_at(sz, take_pct):
        return exit_ratio(sz.first_exit_ratio, sz, conviction=conviction, vol=vol), "scale_out_1"
    if scaled_out == 1 and gain >= take_pct:
        return exit_ratio(sz.second_exit_ratio, sz, conviction=conviction, vol=vol), "scale_out_2"
    return None


def breakeven_hit(*, scaled_out: int, entry: float, price: float, sz, buffer: float = 0.001) -> bool:
    """1차 분할 매도 뒤 본전(+수수료 여유) 아래로 내려오면 나머지를 정리한다 - 이미 챙긴 이익을 손실로 돌리지 않는다."""
    if not sz.scale_out or not sz.breakeven_after_first or scaled_out < 1 or not entry or not price:
        return False
    return price <= entry * (1 + buffer)


def split_quantity(quantity: float, fraction: float, *, integer: bool) -> float:
    """quantity 의 fraction 만큼 팔 수량. 정수 종목은 내림하되 최소 1주, 전부 팔게 되면 전량."""
    if quantity <= 0:
        return 0.0
    fraction = _clamp(fraction, 0.0, 1.0)
    if integer:
        q = int(quantity * fraction)
        q = max(1, q)
        return float(quantity if q >= quantity else q)
    return quantity * fraction
