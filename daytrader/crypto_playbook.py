"""코인(암호화폐) 전용 매매 기법.

★★ 국내/해외 주식과 매매 방식이 근본적으로 다르다:
  - 24시간 쉬지 않는다 - "장 마감"이 없다. "전일"이라는 개념 대신 직전
    24시간 롤링 윈도우로 계산한다.
  - 수량이 소수점이다(0.001 BTC 등) - playbook.py(정수 주식 수량) 코드와
    절대 섞지 않는다.
  - 상·하한가가 없다 - 하루에도 수십% 급등락이 흔하다. 손절·익절 폭을
    국내주식보다 훨씬 넓게 잡는다.
  - "테마" 스크리닝이 의미가 없다 - 코인 하나하나가 이미 각자 하나의
    시장이다. 종목을 테마로 묶어 강도를 매기는 screener.py 의 방식이
    통하지 않는다.

★ 변동성 돌파(Volatility Breakout)는 Larry Williams 가 주식·선물용으로
제안한 것을 국내 코인 트레이더들이 암호화폐에 맞게 응용해 널리 쓰는
형태다. playbook.py 의 국내주식 기법들과 달리 학술 논문 수준의 검증이
아니라, 수개월~1년 단위 실전 운용 사례(커뮤니티 보고)로 뒷받침된
"커뮤니티 검증" 기법이라는 점을 구분해서 밝힌다 - 없는 근거를 있는 것처럼
적지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

ORIGIN_NOTE = (
    "Larry Williams 의 변동성 돌파(주식·선물용)를 국내 코인 트레이더들이 암호화폐에 "
    "맞게 응용한 형태 - 학술 검증이 아니라 수개월~1년 단위 실전 운용 사례로 뒷받침된 "
    "'커뮤니티 검증' 기법입니다."
)

# ★ 변동성이 국내주식보다 훨씬 크다 - 기본값을 그만큼 넓게 둔다.
DEFAULT_RISK = {
    "stop_loss_pct": 0.05,     # 5%
    "take_profit_pct": 0.10,   # 10%
    "trailing_pct": 0.03,      # 고점 대비 3% 하락 시 추적 청산
    "max_hold_hours": 24.0,    # 장 마감이 없으니 "하루"를 시간으로 대신한다.
}


@dataclass
class CryptoVerdict:
    ok: bool
    technique: str
    reason: str
    target_price: float | None = None


# ━━ 진입 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def volatility_breakout_target(prev_high: float, prev_low: float, today_open: float, k: float = 0.5) -> float:
    """변동성 돌파 목표가 = 오늘 시가 + (직전 24시간 고가-저가) × k.
    ★ k 가 클수록 신호가 드물어지고(노이즈 감소), 작을수록 자주 나온다(가짜 돌파 증가).
    """
    range_ = max(0.0, prev_high - prev_low)
    return today_open + range_ * k


def check_volatility_breakout(candles: list, current_price: float, k: float = 0.5) -> CryptoVerdict:
    """candles: 시간순으로 정렬된 [{open, high, low, close}, ...] - 각 항목이
    "24시간"에 해당하는 구간(예: 1일봉, 또는 24개의 1시간봉을 하나로 합친 것)이어야 한다.
    ★ 코인은 장 마감이 없으므로 "전일"을 "직전 24시간"으로 대체한다 - 마지막
    항목을 "오늘", 그 앞을 "직전 24시간"으로 본다.
    """
    if len(candles) < 2:
        return CryptoVerdict(False, "volatility_breakout", "표본 부족 - 24시간 구간이 2개 미만입니다.")
    prev, today = candles[-2], candles[-1]
    target = volatility_breakout_target(prev["high"], prev["low"], today["open"], k)
    if current_price >= target:
        return CryptoVerdict(
            True, "volatility_breakout",
            f"직전 24시간 변동폭의 {k * 100:.0f}%를 돌파했습니다 (목표가 {target:,.0f}).",
            target_price=target,
        )
    return CryptoVerdict(False, "volatility_breakout", f"아직 목표가({target:,.0f})에 못 미칩니다.", target_price=target)


def ma_cross_confirm(prices: list, short: int = 5, long: int = 20) -> CryptoVerdict:
    """단기 이동평균이 장기 이동평균 위에 있으면 상승 추세로 본다.
    ★ 변동성 돌파 신호가 났을 때 하락 추세 속의 "가짜 돌파"를 거르는 보조
    확인용으로 함께 쓰는 것을 권한다 - 이것 하나만으로 진입 결정을 내리지 않는다.
    """
    if len(prices) < long:
        return CryptoVerdict(False, "ma_cross", f"표본 부족 - {long}개 필요한데 {len(prices)}개 있습니다.")
    short_ma = sum(prices[-short:]) / short
    long_ma = sum(prices[-long:]) / long
    if short_ma > long_ma:
        return CryptoVerdict(True, "ma_cross", f"단기 이평({short_ma:,.0f}) > 장기 이평({long_ma:,.0f}) - 상승 추세.")
    return CryptoVerdict(False, "ma_cross", f"단기 이평({short_ma:,.0f}) ≤ 장기 이평({long_ma:,.0f}) - 상승 추세 아님.")


def rsi(prices: list, period: int = 14) -> float | None:
    """표준 RSI(Wilder). 표본이 모자라면 None."""
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = prices[-i] - prices[-i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def check_rsi_oversold_bounce(prices: list, period: int = 14, oversold: float = 30.0) -> CryptoVerdict:
    """RSI 과매도 반등 - 변동성 돌파(추세추종)와는 반대 성격인 평균회귀 보조
    기법이다. 두 기법을 동시에 켜면 서로 다른 국면(추세장/횡보장)을 나눠 맡길 수 있다.
    """
    r = rsi(prices, period)
    if r is None:
        return CryptoVerdict(False, "rsi_oversold", "표본 부족 - RSI 계산에 필요한 캔들이 모자랍니다.")
    if r <= oversold:
        return CryptoVerdict(True, "rsi_oversold", f"RSI {r:.1f} - 과매도 구간입니다.")
    return CryptoVerdict(False, "rsi_oversold", f"RSI {r:.1f} - 과매도가 아닙니다.")


# ━━ 청산 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_exit(entry_price: float, current_price: float, peak_price: float, held_hours: float,
                stop_loss_pct: float = DEFAULT_RISK["stop_loss_pct"],
                take_profit_pct: float = DEFAULT_RISK["take_profit_pct"],
                trailing_pct: float = DEFAULT_RISK["trailing_pct"],
                max_hold_hours: float = DEFAULT_RISK["max_hold_hours"]) -> CryptoVerdict:
    """★ 우선순위: 손절 > 익절 > 추적청산 > 시간청산. 손절을 가장 먼저 보는 건
    국내주식 playbook.py 와 같은 원칙이다 - "먼저 잃지 않는다".
    """
    change = (current_price - entry_price) / entry_price if entry_price else 0.0

    if change <= -stop_loss_pct:
        return CryptoVerdict(True, "stop_loss", f"손절 {change * 100:+.1f}%")

    if change >= take_profit_pct:
        return CryptoVerdict(True, "take_profit", f"익절 {change * 100:+.1f}%")

    if peak_price > entry_price and peak_price > 0:
        drawdown = (peak_price - current_price) / peak_price
        if drawdown >= trailing_pct:
            return CryptoVerdict(True, "trailing_stop", f"고점 대비 {drawdown * 100:.1f}% 하락 - 추적 청산")

    if held_hours >= max_hold_hours:
        return CryptoVerdict(True, "time_stop", f"{max_hold_hours:.0f}시간 초과 보유 - 시간 청산")

    return CryptoVerdict(False, "hold", "보유 유지")


# ━━ 화면·문서용 설명 (원전 표기 포함) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TECHNIQUES = [
    {
        "key": "volatility_breakout", "phase": "entry", "label": "변동성 돌파",
        "description": "직전 24시간 변동폭의 일정 비율(k)만큼 오늘 시가 위로 오르면 추세가 붙었다고 보고 진입합니다.",
        "origin": "Larry Williams(주식·선물) → 국내 코인 커뮤니티가 24시간 시장에 맞게 응용",
        "standard": "표준 k=0.5. 낮추면 신호가 잦아지고(가짜 돌파 증가), 높이면 신호가 드물어집니다(노이즈 감소).",
    },
    {
        "key": "ma_cross", "phase": "entry", "label": "이동평균 추세 확인",
        "description": "단기 이동평균이 장기 이동평균 위에 있을 때만 진입을 허용하는 보조 필터입니다.",
        "origin": "고전 추세추종(이동평균 교차) - 학술·실전 모두 널리 검증된 개념",
        "standard": "표준 단기 5 / 장기 20. 변동성 돌파와 함께 켜서 하락 추세 속 가짜 돌파를 거릅니다.",
    },
    {
        "key": "rsi_oversold", "phase": "entry", "label": "RSI 과매도 반등",
        "description": "RSI 가 과매도 구간(기본 30 이하)에 들어오면 반등을 노리고 진입합니다.",
        "origin": "Wilder RSI(1978) - 평균회귀 보조 기법",
        "standard": "표준 기간 14, 과매도 30. 변동성 돌파(추세추종)와 반대 성격이라 국면을 나눠 맡길 수 있습니다.",
    },
    {
        "key": "stop_loss", "phase": "exit", "label": "손절",
        "description": "진입가 대비 일정 비율 이상 떨어지면 손실을 자르고 나옵니다.",
        "origin": "리스크 관리 표준 원칙",
        "standard": f"기본 {DEFAULT_RISK['stop_loss_pct']*100:.0f}% - 국내주식(약 2.5%)보다 훨씬 넓습니다. 변동성이 큰 시장에서 좁게 잡으면 정상 등락에도 자주 털립니다.",
    },
    {
        "key": "take_profit", "phase": "exit", "label": "익절",
        "description": "진입가 대비 일정 비율 이상 오르면 이익을 확정합니다.",
        "origin": "리스크 관리 표준 원칙",
        "standard": f"기본 {DEFAULT_RISK['take_profit_pct']*100:.0f}%.",
    },
    {
        "key": "trailing_stop", "phase": "exit", "label": "추적 청산",
        "description": "고점 대비 일정 비율 하락하면, 아직 손절선에 닿지 않았어도 이익을 지키기 위해 나옵니다.",
        "origin": "추적 손절(Trailing Stop) - 추세추종 전략의 표준 짝",
        "standard": f"기본 고점 대비 {DEFAULT_RISK['trailing_pct']*100:.0f}% 하락.",
    },
    {
        "key": "time_stop", "phase": "exit", "label": "시간 청산",
        "description": "일정 시간 이상 보유했는데 손절도 익절도 안 됐으면 정리합니다.",
        "origin": "국내주식 데이트레이딩의 '당일 청산' 원칙을 24시간 시장에 맞게 시간 단위로 바꾼 것",
        "standard": f"기본 {DEFAULT_RISK['max_hold_hours']:.0f}시간 - 코인은 장 마감이 없어 '하루'를 시간으로 대신합니다.",
    },
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★★★ 국내주식 playbook.py 와 같은 구조로 재구성한다: 기법마다 클래스를
# 만들고, 사용자가 설정(crypto.entry_order/exit_enabled)으로 순서·on/off를
# 고르면, CryptoPlaybook 이 그 순서대로 평가해 첫 번째로 통과한 것을 쓴다.
# 위에서 만든 순수 함수(check_volatility_breakout 등)는 그대로 두고 여기서
# 감싸기만 한다 - 이미 42건으로 검증된 계산 로직을 다시 짜지 않는다.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CryptoEntryBase:
    key = ""
    label = ""

    def __init__(self, cfg):
        self.crypto_cfg = cfg.crypto

    def evaluate(self, candles: list, current_price: float) -> CryptoVerdict:
        raise NotImplementedError


class VolatilityBreakoutEntry(CryptoEntryBase):
    key = "volatility_breakout"
    label = "변동성 돌파"

    def evaluate(self, candles, current_price) -> CryptoVerdict:
        # ★ 봉을 절반씩 "직전 구간"/"최근 구간" 두 덩어리로 묶어 변동성
        # 돌파의 "전일 vs 오늘" 개념을 재현한다 - 코인은 장 마감이 없어
        # 달력일 경계가 없으므로, 확보한 봉의 앞뒤 절반을 그 대용으로 쓴다.
        if len(candles) < 2:
            return CryptoVerdict(False, self.key, "표본 부족")
        mid = len(candles) // 2
        prev_chunk, today_chunk = candles[:mid], candles[mid:]
        if not prev_chunk or not today_chunk:
            return CryptoVerdict(False, self.key, "표본 부족")
        two_chunk = [
            {"open": prev_chunk[0]["open"], "high": max(c["high"] for c in prev_chunk),
             "low": min(c["low"] for c in prev_chunk), "close": prev_chunk[-1]["close"]},
            {"open": today_chunk[0]["open"], "high": max(c["high"] for c in today_chunk),
             "low": min(c["low"] for c in today_chunk), "close": today_chunk[-1]["close"]},
        ]
        return check_volatility_breakout(two_chunk, current_price, k=self.crypto_cfg.k)


class MaCrossEntry(CryptoEntryBase):
    key = "ma_cross"
    label = "이동평균 추세 확인"

    def evaluate(self, candles, current_price) -> CryptoVerdict:
        prices = [c["close"] for c in candles]
        return ma_cross_confirm(prices)


class RsiOversoldEntry(CryptoEntryBase):
    key = "rsi_oversold"
    label = "RSI 과매도 반등"

    def evaluate(self, candles, current_price) -> CryptoVerdict:
        prices = [c["close"] for c in candles]
        return check_rsi_oversold_bounce(prices)


ENTRY_TECHNIQUES = {
    "volatility_breakout": VolatilityBreakoutEntry,
    "ma_cross": MaCrossEntry,
    "rsi_oversold": RsiOversoldEntry,
}


class CryptoExitBase:
    key = ""

    def __init__(self, cfg):
        self.crypto_cfg = cfg.crypto

    def evaluate(self, entry_price, current_price, peak_price, held_hours) -> CryptoVerdict:
        raise NotImplementedError


class StopLossExit(CryptoExitBase):
    key = "stop_loss"

    def evaluate(self, entry_price, current_price, peak_price, held_hours) -> CryptoVerdict:
        change = (current_price - entry_price) / entry_price if entry_price else 0.0
        if change <= -self.crypto_cfg.stop_loss_pct:
            return CryptoVerdict(True, self.key, f"손절 {change * 100:+.1f}%")
        return CryptoVerdict(False, self.key, "손절 기준 안 됨")


class TakeProfitExit(CryptoExitBase):
    key = "take_profit"

    def evaluate(self, entry_price, current_price, peak_price, held_hours) -> CryptoVerdict:
        change = (current_price - entry_price) / entry_price if entry_price else 0.0
        if change >= self.crypto_cfg.take_profit_pct:
            return CryptoVerdict(True, self.key, f"익절 {change * 100:+.1f}%")
        return CryptoVerdict(False, self.key, "익절 기준 안 됨")


class TrailingStopExit(CryptoExitBase):
    key = "trailing_stop"

    def evaluate(self, entry_price, current_price, peak_price, held_hours) -> CryptoVerdict:
        if peak_price > entry_price and peak_price > 0:
            drawdown = (peak_price - current_price) / peak_price
            if drawdown >= self.crypto_cfg.trailing_pct:
                return CryptoVerdict(True, self.key, f"고점 대비 {drawdown * 100:.1f}% 하락 - 추적 청산")
        return CryptoVerdict(False, self.key, "추적 청산 기준 안 됨")


class TimeStopExit(CryptoExitBase):
    key = "time_stop"

    def evaluate(self, entry_price, current_price, peak_price, held_hours) -> CryptoVerdict:
        if held_hours >= self.crypto_cfg.max_hold_hours:
            return CryptoVerdict(True, self.key, f"{self.crypto_cfg.max_hold_hours:.0f}시간 초과 보유 - 시간 청산")
        return CryptoVerdict(False, self.key, "시간 청산 기준 안 됨")


EXIT_TECHNIQUES = {
    "stop_loss": StopLossExit,
    "take_profit": TakeProfitExit,
    "trailing_stop": TrailingStopExit,
    "time_stop": TimeStopExit,
}

ENTRY_TECHNIQUE_KEYS = set(ENTRY_TECHNIQUES.keys())
EXIT_TECHNIQUE_KEYS = set(EXIT_TECHNIQUES.keys())


class CryptoPlaybook:
    """국내주식 playbook.Playbook 과 같은 역할 - config 대로 기법을 조립해
    순서대로 평가한다. entry_order 에 없는 기법은 아예 평가되지 않는다.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.entries = [ENTRY_TECHNIQUES[k](cfg) for k in cfg.crypto.entry_order]
        # ★★ 청산은 항상 손절을 맨 먼저 본다 - 설정에 어떤 순서로 적혀
        # 있어도 "먼저 잃지 않는다"는 원칙이 깨지면 안 된다.
        exit_keys = list(cfg.crypto.exit_enabled)
        exit_keys.sort(key=lambda k: 0 if k == "stop_loss" else 1)
        self.exits = [EXIT_TECHNIQUES[k](cfg) for k in exit_keys]

    def evaluate_entry(self, candles: list, current_price: float) -> CryptoVerdict:
        for entry in self.entries:
            v = entry.evaluate(candles, current_price)
            if v.ok:
                return v
        return CryptoVerdict(False, "", "진입 조건을 충족하는 기법이 없습니다.")

    def evaluate_exit(self, entry_price: float, current_price: float, peak_price: float, held_hours: float) -> CryptoVerdict:
        for exit_t in self.exits:
            v = exit_t.evaluate(entry_price, current_price, peak_price, held_hours)
            if v.ok:
                return v
        return CryptoVerdict(False, "hold", "보유 유지")

    def describe(self) -> list:
        out = []
        for t in TECHNIQUES:
            enabled = (t["key"] in self.cfg.crypto.entry_order) if t["phase"] == "entry" else (t["key"] in self.cfg.crypto.exit_enabled)
            out.append({**t, "enabled": enabled})
        return out
