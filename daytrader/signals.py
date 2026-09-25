from __future__ import annotations

from math import isnan

from daytrader.timeutil import parse_dt, parse_hhmm

# Bar 는 ts/open/high/low/close/volume 을 가진 객체다 (덕타이핑으로 받는다).
# ★ 모든 함수는 데이터가 부족하면 예외를 던지지 않고 float("nan") 을 반환한다.
#   판정 단계에서 nan 은 자동으로 "데이터 부족"으로 처리되므로,
#   호출하는 쪽이 매번 길이를 확인하지 않아도 된다.

NAN = float("nan")


def _wilder_smooth(values: list, n: int) -> list:
    """와일더 평활. 첫 값은 앞 n개의 단순합, 이후는 s - s/n + v.
    RSI/ADX 가 공유하는 평활 방식이라 여기 하나만 둔다.
    """
    if len(values) < n:
        return []
    smoothed = [sum(values[:n])]
    for v in values[n:]:
        prev = smoothed[-1]
        smoothed.append(prev - prev / n + v)
    return smoothed


def sma(bars, n, key="close"):
    """단순이동평균. 추세가 위를 향하는지 보는 가장 기본적인 기준선."""
    if len(bars) < n:
        return NAN
    vals = [getattr(b, key) for b in bars[-n:]]
    return sum(vals) / n


def ema(bars, n, key="close"):
    """지수이동평균. 최근 값에 더 큰 가중치를 줘 sma 보다 빠르게 반응한다."""
    if len(bars) < n:
        return NAN
    vals = [getattr(b, key) for b in bars]
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def vwap(bars):
    """거래량가중평균가. 전형가=(고가+저가+종가)/3 를 거래량으로 가중한다.
    오늘 시장 참여자들의 평균 매입단가에 가깝다.
    """
    if not bars:
        return NAN
    num = 0.0
    den = 0.0
    for b in bars:
        typical = (b.high + b.low + b.close) / 3
        num += typical * b.volume
        den += b.volume
    if den == 0:
        return NAN
    return num / den


def atr(bars, n=14):
    """평균 진범위(ATR, Wilder 1978). TR=max(고저폭, |고가-전종가|, |저가-전종가|).
    최초값은 앞 n개 TR 의 단순평균으로 시작(seed)하고, 그 뒤로는
    Wilder 평활(이전값*(n-1)+현재TR)/n 로 이어간다 - RSI/ADX 와 같은 방식
    (_wilder_smooth 공유). 단순 SMA 보다 과거 변동성을 더 오래 반영해
    손절선이 덜 출렁인다.
    """
    if len(bars) < n + 1:
        return NAN
    trs = []
    for i in range(1, len(bars)):
        pc = bars[i - 1].close
        tr = max(bars[i].high - bars[i].low, abs(bars[i].high - pc), abs(bars[i].low - pc))
        trs.append(tr)
    tr_s = _wilder_smooth(trs, n)
    if not tr_s:
        return NAN
    return tr_s[-1] / n


def adx(bars, n=14):
    """추세의 세기 0~100 (Wilder, 1978). 방향이 아니라 "추세가 있느냐"만 본다.
    통상 20~25 위면 추세, 아래면 횡보 - 눌림목 기법이 횡보장에서
    무너지는 것을 막는 필터로 쓴다. 데이터가 2n+1개 미만이면 nan.
    """
    if len(bars) < 2 * n + 1:
        return NAN

    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, len(bars)):
        up_move = bars[i].high - bars[i - 1].high
        down_move = bars[i - 1].low - bars[i].low
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
        pc = bars[i - 1].close
        tr = max(bars[i].high - bars[i].low, abs(bars[i].high - pc), abs(bars[i].low - pc))
        trs.append(tr)
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    tr_s = _wilder_smooth(trs, n)
    plus_s = _wilder_smooth(plus_dms, n)
    minus_s = _wilder_smooth(minus_dms, n)
    if not tr_s:
        return NAN

    dxs = []
    for tr_v, p_v, m_v in zip(tr_s, plus_s, minus_s):
        if tr_v == 0:
            dxs.append(0.0)
            continue
        plus_di = 100 * p_v / tr_v
        minus_di = 100 * m_v / tr_v
        denom = plus_di + minus_di
        dx = 0.0 if denom == 0 else 100 * abs(plus_di - minus_di) / denom
        dxs.append(dx)

    adx_s = _wilder_smooth(dxs, n)
    if not adx_s:
        return NAN
    return adx_s[-1] / n


def rsi(bars, n=14):
    """상대강도지수. 최근 상승폭과 하락폭의 비로 과매수·과매도를 가늠한다."""
    if len(bars) < n + 1:
        return NAN
    gains, losses = [], []
    for i in range(1, len(bars)):
        diff = bars[i].close - bars[i - 1].close
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    gain_s = _wilder_smooth(gains, n)
    loss_s = _wilder_smooth(losses, n)
    if not gain_s or not loss_s:
        return NAN
    avg_gain = gain_s[-1] / n
    avg_loss = loss_s[-1] / n
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def highest_high(bars, n):
    """돌파 판정의 기준선 - 최근 n봉 중 가장 높은 고가."""
    if len(bars) < n:
        return NAN
    return max(b.high for b in bars[-n:])


def lowest_low(bars, n):
    """최근 n봉 중 가장 낮은 저가."""
    if len(bars) < n:
        return NAN
    return min(b.low for b in bars[-n:])


def volume_ratio(bars, n):
    """마지막 봉 거래량 / 직전 n봉 평균. "가격만 오르는 돌파는 대개 실패한다"."""
    if len(bars) < n + 1:
        return NAN
    prev = bars[-(n + 1):-1]
    avg = sum(b.volume for b in prev) / n
    if avg == 0:
        return NAN
    return bars[-1].volume / avg


def body_ratio(bar):
    """몸통 비율 (종가-시가)/(고가-저가). 음봉이면 음수."""
    rng = bar.high - bar.low
    if rng == 0:
        return NAN
    return (bar.close - bar.open) / rng


def consecutive_up(bars, n):
    """종가 기준 연속 상승 봉 수 (최근에서 거슬러 올라가며, 최대 n)."""
    if len(bars) < 2:
        return NAN
    limit = min(n, len(bars) - 1)
    count = 0
    for i in range(len(bars) - 1, len(bars) - 1 - limit, -1):
        if bars[i].close > bars[i - 1].close:
            count += 1
        else:
            break
    return count


def consecutive_down(bars, n):
    """종가 기준 연속 하락 봉 수 (최근에서 거슬러 올라가며, 최대 n)."""
    if len(bars) < 2:
        return NAN
    limit = min(n, len(bars) - 1)
    count = 0
    for i in range(len(bars) - 1, len(bars) - 1 - limit, -1):
        if bars[i].close < bars[i - 1].close:
            count += 1
        else:
            break
    return count


def range_of(bars, start_hhmm: str, end_hhmm: str):
    """지정한 시간대(start~end) 안 봉들의 (고가, 저가). 오프닝 레인지 브레이크아웃(ORB)에 쓴다."""
    start_t = parse_hhmm(start_hhmm)
    end_t = parse_hhmm(end_hhmm)
    highs, lows = [], []
    for b in bars:
        dt = parse_dt(b.ts)
        if dt is None:
            continue
        t = dt.time()
        if start_t <= t <= end_t:
            highs.append(b.high)
            lows.append(b.low)
    if not highs:
        return (NAN, NAN)
    return (max(highs), min(lows))


def slope(values):
    """최소제곱 회귀 기울기. 값들이 시간에 따라 오르는지 내리는지의 방향과 크기."""
    n = len(values)
    if n < 2:
        return NAN
    xs = list(range(n))
    sx = sum(xs)
    sy = sum(values)
    sxy = sum(x * y for x, y in zip(xs, values))
    sxx = sum(x * x for x in xs)
    denom = n * sxx - sx * sx
    if denom == 0:
        return NAN
    return (n * sxy - sx * sy) / denom


def price_position(bars, n):
    """최근 n봉 레인지에서 현재가의 위치 0~1. 1에 가까울수록 고점권."""
    if len(bars) < n:
        return NAN
    window = bars[-n:]
    hi = max(b.high for b in window)
    lo = min(b.low for b in window)
    if hi == lo:
        return NAN
    return (bars[-1].close - lo) / (hi - lo)


def tight_range_pct(bars, n):
    """최근 n봉의 (고가-저가)/저가. 값이 작을수록 변동성이 수축된 상태."""
    if len(bars) < n:
        return NAN
    window = bars[-n:]
    hi = max(b.high for b in window)
    lo = min(b.low for b in window)
    if lo == 0:
        return NAN
    return (hi - lo) / lo


def change_from_open(bars):
    """(마지막 종가 - 첫 봉 시가) / 첫 봉 시가. 오늘 하루의 누적 등락률."""
    if not bars:
        return NAN
    o = bars[0].open
    if o == 0:
        return NAN
    return (bars[-1].close - o) / o


def dry_up_ratio(bars, recent, base):
    """최근 recent봉 평균 거래량 / 그 직전 base봉 평균.
    ★ base 는 '전체 조회 길이'가 아니라 이전 구간의 봉 수다.
    필요 길이는 recent + base 다. 값이 작을수록 거래가 마른(관심이 식은) 상태.
    """
    if len(bars) < recent + base:
        return NAN
    a = bars[-recent:]
    b_ = bars[-(recent + base):-recent]
    avg_a = sum(x.volume for x in a) / recent
    avg_b = sum(x.volume for x in b_) / base
    if avg_b == 0:
        return NAN
    return avg_a / avg_b


def relative_strength(bars, peer_bars):
    """비교군(테마) 대비 상대강도. 등락률의 비(ratio)다 - (1+mine)/(1+peer) 가 아니다.
    테마가 오를 때 이 종목이 더 갔는지를 본다.
    """
    mine = change_from_open(bars)
    if isnan(mine):
        return NAN
    if not peer_bars:
        return 1.0
    peer = change_from_open(peer_bars)
    if isnan(peer) or abs(peer) < 1e-6:
        return 1.0
    if peer < 0:
        return 1.0 if mine >= 0 else mine / peer
    return mine / peer
