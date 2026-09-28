from __future__ import annotations

import os
import re
from dataclasses import MISSING, dataclass, field, fields
from datetime import time
from typing import Any

import yaml

from daytrader.paths import app_dir, app_path, ensure_user_files
from daytrader.ticks import breakeven_pct
from daytrader.timeutil import parse_hhmm

# 진입/청산 기법 키. 뒤에서 늘어난다.
# STAGE 27 이 NotifyCfg 를 더한다. 그때 이 파일도 같이 고쳐야 검증을 통과한다.
ENTRY_TECHNIQUE_KEYS = (
    "breakout", "theme_leader", "vwap_pullback", "orb", "volume_dry_pop",
    "bull_flag", "ma_pullback", "vwap_reclaim", "open_gap", "close_squeeze",
)
EXIT_TECHNIQUE_KEYS = ("fixed", "trailing", "atr_stop", "time_stop", "momentum_fade")


def _build(cls, section: str, raw: dict):
    """섹션 딕셔너리를 dataclass 로 만든다.
    ★ 알 수 없는 키는 ValueError 로 거부한다. 오타를 조용히 삼키면 사용자는
    값을 바꿨다고 믿는데 프로그램은 기본값으로 돈다 - 그게 가장 나쁘다.
    """
    raw = dict(raw or {})
    allowed = {f.name for f in fields(cls)}
    unknown = set(raw.keys()) - allowed
    if unknown:
        raise ValueError(f"[{section}] 알 수 없는 설정 키: {', '.join(sorted(unknown))}")
    # ★★★ 실제로 겪은 크래시 - config.yaml 에 값이 빈 채로("max_positions:")
    # 남아 있으면 YAML 은 그걸 None 으로 읽고, 그 None 이 dataclass 기본값을
    # 덮어써서 해외주식 엔진 루프가 매번 "'>=' not supported between 'int' and
    # 'NoneType'" 으로 죽었다(대시보드에 type 오류로 표시됨). 기본값이 있고
    # None 을 허용하지 않는 항목은 빈 값을 "설정 안 함"으로 보고 기본값을 쓴다.
    for f in fields(cls):
        has_default = f.default is not MISSING or f.default_factory is not MISSING
        if f.name in raw and raw[f.name] is None and has_default and "None" not in str(f.type):
            del raw[f.name]
    return cls(**raw)


@dataclass
class CapitalCfg:
    allocation: int
    max_positions: int
    per_trade_pct: float = 0.33  # (폐지) 이제 종목당 한도 = allocation / max_positions 로 자동 계산한다(sizing.py)


@dataclass
class SizingCfg:
    """★★★ 투자금액 배분·분할 매수/매도 - 세 시장(국내·해외·암호화폐)이 같은 규칙을 쓴다(sizing.py).
    시장별 총 투자금액만 사용자가 정하고, 나머지는 손절·익절 폭과 신호 강도에서 자동으로 정해진다.
    """
    # ★★★ "매매원칙을 잃지않는다가 아니라 수익을 최대한 추구한다로 - 베팅 크기를 더
    # 공격적으로, 익절폭은 정해놓지 말고 실시간 판단(트레일링)에 따른다" - 신호가 강할 때
    # 더 크게 베팅한다(min_mult~max_mult 폭을 넓힘). 추가 매수도 더 여러 번 허용한다(max_adds↑).
    # 분할 매도는 일찍 더 많이 팔아 이익을 서둘러 챙기던 것에서, 적게만 챙기고 나머지 대부분을
    # 트레일링 스탑에 맡겨 추세가 사는 한 계속 태우는 쪽으로 바꿨다(first_exit_ratio·second_exit_ratio↓).
    # ★ initial_ratio 는 오히려 낮췄다(0.5→0.6→0.3) - initial_ratio 를 max_mult 와 함께 올리면
    # 신호가 웬만큼만 강해도 첫 매수에서 종목당 한도를 거의 다 써버려(entry_amount 는 cap 을
    # 절대 안 넘는다), max_adds 를 늘려도 room = cap-invested 가 0 에 가까워 피라미딩이 정작
    # 강한 신호일수록 발동을 못 하는 문제가 있었다(실제로 tests/test_scaling.py 로 확인됨).
    # 첫 매수 비중을 낮춰 추가 매수(피라미딩)가 쓸 여지를 남긴다.
    signal_sizing: bool = True      # 신호가 강할수록 더 사고, 약하면 덜 산다
    dynamic: bool = True            # 테마 근거의 크기(순위·동반 상승·상승률·대장주 여부)와 장중 변동성(ATR)으로 매수·매도 금액을 자동 조절
    min_mult: float = 0.6           # 가장 약한 신호의 매수 배수
    max_mult: float = 2.2           # 가장 강한 신호의 매수 배수
    scale_in: bool = True           # 이익 중인 종목에 추가 매수(피라미딩)
    initial_ratio: float = 0.3      # 종목당 한도 중 첫 매수 비율(나머지는 추가 매수 몫)
    max_adds: int = 3               # 추가 매수 최대 횟수
    add_step_pct: float = 0.0       # 0=자동(손절폭의 40%) - 마지막 매수가보다 이만큼 오르면 추가
    scale_out: bool = True          # 여러 번 나눠 매도
    first_exit_at: float = 0.0      # 0=자동(익절폭의 절반) - 1차 분할 매도 수익률
    first_exit_ratio: float = 0.20  # 1차에 파는 비율(보유량 기준) - 적게만 챙기고 대부분은 계속 태운다
    second_exit_ratio: float = 0.35  # 2차(익절폭 도달)에 파는 비율(남은 수량 기준) - 나머지는 추적 손절로 끌고 감
    breakeven_after_first: bool = True  # 1차 매도 뒤 본전 이하로 내려오면 나머지 정리
    # ★★★ "정해진 익절폭으로 운영하되 실시간 시그널이 강할 때 폭을 넓힌다" - ADX(추세 세기, 방향
    # 무관 0~100)가 take_profit_adx_threshold 이상이면 추세가 있다고 보고 익절 목표를 넓히기
    # 시작한다. take_profit_adx_full 에서 take_profit_max_widen 배(기본 2배)까지 선형으로 넓어지고,
    # 그 사이는 비례 배분한다. ADX 를 못 재면(데이터 부족 등) 넓히지 않고 기본값 그대로 쓴다.
    take_profit_adx_threshold: float = 25.0  # 이 이상이어야 넓히기 시작(통상 20~25=추세)
    take_profit_adx_full: float = 50.0       # 이 값에서 최대 배수에 도달
    take_profit_max_widen: float = 2.0       # 최대 몇 배까지 넓힐지


@dataclass
class RiskCfg:
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    trailing_arm_pct: float
    daily_loss_limit_pct: float
    weekly_loss_limit_pct: float
    daily_max_trades: int
    max_consecutive_losses: int
    reduce_after_loss: bool
    reduced_size_pct: float
    max_slippage_pct: float
    cooldown_minutes: int
    min_order_amount: int
    # ★★★ "연속 손절후 다음 진입시기가 너무 늦어. 모든 장에서 3시간 또는
    # 세션이 바뀌면 진입 가능하게 변경해"라는 요청. 예전엔 국내·해외가
    # 24시간, 암호화폐가 6시간, 스윙이 3일이라 제각각이었고 시간 경과에만
    # 기대 하루 종일 막히는 일이 잦았다. 이제 4개 시장 전부 기본 3시간으로
    # 통일하고(crypto.loss_halt_cooldown_hours, swing.loss_halt_cooldown_hours
    # 도 동일 기본값), 국내·해외는 그 전에 세션(프리장·본장·NXT장/애프터장 등)이
    # 바뀌면 시간과 무관하게 즉시 재개한다(engine.py._loss_halt_expired,
    # overseas_engine.py halt_info 참고) - 세션마다 유동성·변동성이 달라
    # 한 세션의 손절이 다음 세션의 기회까지 막을 이유가 없다. 암호화폐는
    # 24시간 시장이라 세션 개념이 없어 시간 경과만 본다.
    # ★ 기본값이 있으므로 반드시 기본값 없는 필드들 뒤에 와야 한다.
    loss_halt_cooldown_hours: float = 3.0
    # ★★★ "시장 상황에 따라 목표를 가변토록 하는 것도 사용자가 선택하게 해" - ADX 기반 익절폭
    # 자동 확대(sizing.dynamic_take_profit_pct, 폭 조절값은 sizing.take_profit_adx_* 에 공통)를
    # 시장별로 켜고 끌 수 있게 한다. 꺼두면 이 시장은 정해진 take_profit_pct 를 그대로 쓴다.
    dynamic_take_profit: bool = True
    # ★★★ "거래시장별로 어떤 모델을 선택할지는 사용자가 선택" - 3단계 매매 모델(daytrader/playbook.py
    # 의 Playbook.learning_mode 참고):
    #   "none"             - 기본적인 단타 룰로만(과거 실적·백테스트 가산점 전혀 안 씀)
    #   "entry_pref"       - (기본값) 진입만 최근 시세 백테스트·실전 실적 가산점 반영(지금까지 동작)
    #   "entry_exit_pref"  - entry_pref 에 더해, 보유 중 청산 효율을 계속 추적(exit_efficiency.py)해
    #                        다음에 같은 종목에 들어갈 때 익절 목표를 추가로 넓힌다
    technique_learning_mode: str = "entry_pref"
    # ★★★ "단타 매매 모드를 시장별로 분리해" - 예전엔 Config.style 하나로 국내·해외·암호화폐가
    # 전부 같은 속도를 썼다. 이제 시장마다 따로 고른다(apply_style() 참고) - 국내가 fast 여도
    # 해외·암호화폐는 normal 로 그대로 둘 수 있다. 스윙은 원래 며칠~몇 주 보유가 전제라 이 개념이
    # 없다(자기 목표값을 그대로 쓴다).
    style: str = "normal"  # normal | fast(짧은 주기·좁은 목표·다양한 기법) | scalp(실험용, 더 작은 목표)
    # ★★★ "국장도 8시부터 9시까지 프리장 9시부터 3시반까지 본장 이후 nxt장 - 장 별로 거래를
    # 할지 사용자가 선택하게 해" - 해외주식의 세션별 토글(trade_premarket 등)과 같은 개념을
    # 국내주식에도 둔다(daytrader/session.py 의 domestic_phase() 참고). 프리장·NXT장은
    # 기본 꺼짐 - 지금까지 국내주식은 정규장(09:00~15:30)에서만 신규 매수를 했으니, 새 기능을
    # 올려도 아무것도 안 켜면 예전과 똑같이 동작해야 한다.
    trade_premarket: bool = False  # 프리장(08:00~09:00)
    trade_regular: bool = True     # 본장(09:00~15:30, 정규장)
    trade_nxt: bool = False        # NXT장(15:30~20:00)


@dataclass
class ScreenCfg:
    ranking_count: int
    min_trading_amount: int
    min_price: int
    max_price: int
    min_change_rate: float
    max_change_rate: float
    exclude_warnings: list
    exclude_preferred: bool
    exclude_etf_etn: bool
    top_themes: int
    candidates_per_theme: int
    min_theme_members_up: int
    allow_unmapped: bool
    auto_daily: bool
    auto_time: str
    meta_cache_minutes: int
    # ★ 직접 추가한 관심 종목(6자리 코드) - 테마와 별개로 항상 거래 대상 후보에 함께 들어간다.
    watchlist: list = field(default_factory=list)


@dataclass
class EntryCfg:
    scan_start: time
    scan_end: time
    breakout_lookback: int
    volume_surge_ratio: float
    volume_window: int
    poll_seconds: int
    rescreen_minutes: int
    max_vi_gap_pct: float
    use_closed_bars_only: bool
    # ★★★ 장 초반(09:00~scan_start)과 장 막판(scan_end~close_window_end)의 변동성 단타 - 이 두 시간대에는 그 시간대 전용
    #   기법(시초 갭 돌파·장 막판 상승 지속)만 새로 살 수 있다(기법을 진입 기법에서 켰을 때). 나머지 기법은 기존 매매 시간(scan_start~scan_end)에만.
    extra_windows: bool = True
    open_window_start: str = "09:00"
    close_window_end: str = "15:00"
    # ★★★ "장이 열려 있을 때만 종목 선정 로직을 돌려라" - 매수 가능 시간(scan_start~scan_end,
    # open/close_window)과는 별개로, 국내 시장이 어떤 형태로든 열려 있는(프리마켓·정규장·NXT)
    # 하루 전체 구간을 잡아 둔다. 이 구간 밖(주말·공휴일·이 시간 밖)에는 종목 선정을 아예 돌리지
    # 않는다(session.py 의 live/screening 판정) - 랭킹 API 호출만 낭비되기 때문이다. 실제로 살 수
    # 있는 시간은 여전히 위 scan_start~close_window_end 뿐이다(이 값들은 그 판단에 영향 없음).
    selection_day_start: str = "08:00"
    selection_day_end: str = "21:00"


@dataclass
class ExitCfg:
    force_close_time: time
    force_close_deadline_min: int
    use_conditional_oco: bool
    max_hold_minutes: int
    # ★★★ "매매원칙을 잃지않는다가 아니라 수익을 최대한 추구한다로, 매매로직과 방향도 전면
    # 수정해. 오버나이트·장기 보유 허용" - 켜면 장 마감 시각(force_close_time)이 지나도
    # 국내 단타 포지션을 강제 청산하지 않고 다음 거래일로 넘긴다(engine.py 의 run() 참고).
    # 손절·트레일링·시간 손절 같은 다른 청산 기법은 그대로 계속 감시한다 - "무조건 오늘
    # 안에 판다"는 규칙만 빠진다.
    # ★★★ "조건부 오버나이트" - allow_overnight 는 더 이상 무조건 넘기는 스위치가
    # 아니라 "이익 중인 포지션만, 최대 며칠까지" 넘기는 규칙의 총 스위치다(engine.py
    # Engine._settle_overnight 참고). 아래 overnight_* 값들이 그 세부 조건이다.
    allow_overnight: bool = True
    # 장마감 시점 평가손익이 왕복비용을 뺀 뒤에도 이 비율 이상 이익이어야 넘긴다. 못
    # 미치면(또는 손실이면) 오늘 안에 판다. 0이면 "본전만 넘으면" 넘기는 셈이다.
    overnight_min_profit_pct: float = 0.02
    # 한 번 넘긴 포지션을 다시 넘길 수 있는 최대 일수. 지금은 1일만 지원한다 - 연속
    # 오버나이트가 하루를 넘어가면 데이트레이딩이 아니라 스윙이 된다(그건 swing 엔진의 몫).
    overnight_max_days: int = 1
    # 주말·공휴일 앞 마지막 거래일에는 넘기지 않는다(session.py 의 다음 거래일 판정을
    # 쓴다) - 쉬는 날 동안 뉴스·급락에 그대로 노출되는 기간을 최소화한다.
    overnight_skip_before_holiday: bool = True
    # 넘기기로 한 포지션은 손절선을 평균 매수가(본전, 비용 포함) 위로 올리고 서버
    # OCO 를 그 값으로 다시 건다 - "이익 중이던 포지션이 다음날 손실로 마감되는" 최악을
    # 막는다. 끄면 손절선은 원래 값 그대로 두고 수량만 넘긴다.
    overnight_breakeven_stop: bool = True


@dataclass
class StrategyCfg:
    entry_order: list
    exit_enabled: list
    params: dict = field(default_factory=dict)
    # ★★★ "시세 변동을 모니터링해서 최적의 종목과 기법을 골라 승률을
    # 높여야 한다"는 요청. 예전 방식은 두 가지 모두 "목록 순서"를 따랐다:
    #   - 종목: 아침 스크리닝 때 정한 순서대로 먼저 걸리는 걸 샀다.
    #   - 기법: entry_order 에 적힌 순서대로 처음 통과한 걸 썼다.
    # 그래서 지금 이 순간 신호가 더 강한 종목·기법이 있어도 무시됐다.
    # best_signal 을 켜면 "전부 평가한 뒤 가장 강한 신호"를 고른다.
    best_signal: bool = True
    # ★ 과거 실적(기법별 승률·손익비)을 신호 점수에 얼마나 반영할지.
    # 0 이면 순수 신호 강도만 본다. 표본이 적을 때 과적합되지 않도록
    # min_trades_for_weight 를 넘긴 기법에만, 아래 강도만큼만 반영한다.
    performance_weight: float = 0.3
    min_trades_for_weight: int = 20

    def p(self, key: str) -> dict:
        """params 에서 기법 파라미터를 꺼낸다. 없으면 빈 dict - 미설정을 예외로 만들지 않는다."""
        return self.params.get(key, {})


@dataclass
class LiveCfg:
    require_preflight: bool
    reconcile_every_loops: int
    degrade_after_failures: int
    degrade_halt_minutes: int
    adopt_unknown_holdings: bool
    cancel_orphans_on_start: bool


@dataclass
class UiCfg:
    stream: bool
    tick_ms: int
    chart_window: str
    animate: bool
    grid_density: str
    # ★ 화면을 아무도 안 만지고 이 시간이 지나면 비밀번호를 다시 받는다(0=사용 안 함). 자동 갱신은 '사용'으로 치지 않는다.
    session_idle_minutes: int = 30


@dataclass
class SimulationCfg:
    scenario: str
    speed: int
    seed: int
    start_time: str
    history_days: int
    days: int


@dataclass
class NewsCfg:
    mode: str
    risk_hours: int
    enabled: bool
    sources: list
    theme_queries: bool
    lookback_hours: int
    refresh_minutes: int
    auto_refresh_seconds: int
    reaction_window_min: int
    theme_boost: float
    # ★ AI 뉴스·공시 위험 필터(news_guard.py) - Groq 키가 등록돼 있을 때만 작동한다. 사기 전에 종목의 최근 헤드라인을 읽어 악재가 있으면 제외한다.
    ai_filter: bool = True
    ai_max_calls_per_day: int = 300
    groq_model: str = "openai/gpt-oss-20b"


@dataclass
class CostCfg:
    commission_pct: float
    tax_pct: float


@dataclass
class NotifyCfg:
    """★★ enabled()는 '실거래에서 자동 알림을 보낼지'의 판정이다. 텔레그램
    토큰·채팅ID 는 여기 두지 않는다 - [준비·연결] → 외부 연동에서 관리한다.
    이 설정 화면에는 '무엇을 보낼지'만 남긴다.
    """
    enabled: bool = True
    telegram_token: str = ""
    telegram_chat_id: str = ""
    events: list = field(default_factory=lambda: ["trade", "daily", "monthly", "yearly"])
    # ★★★ "텔레그램 메시지를 못 보낸다"의 흔한 원인 - 기본적으로 실거래
    # (live)에서만 알림을 보낸다. 연습 매매까지 알림이 오면 실제 주문과
    # 헷갈릴 수 있어서 둔 안전장치인데, "모의매매도 알림으로 확인하고
    # 싶다"는 경우엔 방법이 없었다. 이 값을 켜면 연습 모드에서도 보낸다
    # (메시지에 [연습] 표시가 붙어 실거래와 구분된다).
    notify_in_practice: bool = False
    # ★ 하루 매매 복기를 텔레그램으로 보낸다(daily_review.py): 국내 장 마감 10분 뒤(15:40), 미국은 한국시간 아침 06:00, 암호화폐는 매일 21:00(각각 설정 가능).
    daily_review: bool = True
    crypto_review_time: str = "21:00"
    overseas_review_time: str = "06:00"  # 한국시간 - 직전 미국 거래일(정규장 마감 후)을 복기한다
    # ★ "암호화폐는 일2회 보내고" - 24시간 시장이라 하루 한 번으로는 너무 뜸하다는 요청.
    # 비워두면(기본값) 예전처럼 하루 한 번만 보낸다 - crypto_review_time 과 같은 시각이면
    # 사실상 안 켠 것과 같다(due_markets() 가 자연히 걸러 한 번만 보낸다).
    crypto_review_time2: str = ""
    # ★ "전체 주, 월 보내라고" - 4개 시장을 한 메시지에 묶어 보내는 주간·월간 복기(daily_review.py
    # compose_period 참고). 시장별 일일 복기와 달리 기본은 꺼짐(매일 오던 알림에 더해 갑자기
    # 주간·월간까지 오면 부담스러울 수 있어, 원하는 사람만 켜게 한다).
    weekly_review_enabled: bool = False
    weekly_review_day: str = "mon"  # mon~sun - 그 요일에 "지난 7일"을 복기해서 보낸다
    weekly_review_time: str = "08:00"
    monthly_review_enabled: bool = False
    monthly_review_day: int = 1  # 1~28 - 그 날짜에 "지난 달 전체"를 복기해서 보낸다
    monthly_review_time: str = "08:00"
    # ★★★ "본장이 끝나고 시장에 대한 평가를 다른 투자사나 증권사 데이타나 외부 기관의
    # 데이터를 참고해서 정리해 텔레그램으로 보내는 기능" 요청(market_commentary.py). 내
    # 매매 복기(daily_review)와 달리 "시장 자체"에 대한 외부 뉴스·증권사 코멘트를 Groq 로
    # 요약한다. 기본은 꺼짐(daily_review 에 더해 추가로 오는 알림이라 원하는 사람만 켠다) -
    # 또한 [설정] → 속보에서 Groq 키를 등록해야 실제로 보내진다(키가 없으면 조용히 건너뜀).
    market_review_enabled: bool = False
    market_review_time: str = "15:40"  # 국내 본장(15:30) 마감 10분 뒤 - daily_review 의 국내 복기와 같은 시각.


@dataclass
class CryptoCfg:
    """★ 코인 자동매매 설정. 국내주식(strategy/risk)과 완전히 분리한다 -
    코인은 24시간 시장이고 변동성이 훨씬 커서 같은 파라미터를 못 쓴다.

    ★★★ live 는 국내주식의 mode(=="live") 와 완전히 독립된 스위치다.
    처음엔 코인 엔진이 cfg.is_live(국내주식 모드)를 그대로 재사용했는데,
    이러면 "국내주식만 실거래로 켜고 싶었는데 빗썸 키가 등록돼 있으면
    코인까지 자동으로 실거래 대상이 되는" 위험한 조합이 생긴다. 두 시장은
    사용자가 각자 따로 실거래 여부를 결정해야 한다.
    ★★★ mode 는 국내주식의 mode 와 완전히 독립된 값이다. 처음엔 코인
    엔진이 cfg.is_live(국내주식 모드)를 그대로 재사용했는데, 이러면
    "국내주식만 실거래로 켜고 싶었는데 코인까지 자동으로 실거래 대상이
    되는" 위험한 조합이 생긴다. 두 시장은 사용자가 각자 따로 결정해야 한다.
    """
    enabled: bool = False
    mode: str = "paper"  # ★★ "web"(관찰만)|"paper"(모의매매)|"live"(실거래). 국내주식과 같은 3단계.
    live: bool = False  # ★ 하위호환용 - load_config() 가 mode 로부터 다시 계산해 덮어쓴다.
    entry_order: list = field(default_factory=lambda: ["volatility_breakout"])
    # ★★ 국내주식 EXIT_TECHNIQUES 와 같은 키를 쓴다(fixed=고정 손절·익절,
    # trailing=추적 청산, time_stop=시간 청산) - "stop_loss"/"take_profit"
    # 처럼 이전에 코인 전용으로 썼던 이름은 국내 레지스트리에 없어서
    # KeyError 가 난다. 같은 Playbook 클래스를 공유하므로 키도 맞춰야 한다.
    exit_enabled: list = field(
        default_factory=lambda: ["fixed", "trailing", "atr_stop", "momentum_fade", "time_stop"]
    )
    watchlist: list = field(default_factory=lambda: ["KRW-BTC", "KRW-ETH", "KRW-XRP"])
    budget: float = 10000000.0  # ★ 암호화폐 총 투자금액(원). 종목당 한도 = budget / max_positions
    allocation_per_coin: float = 0.0  # (폐지) 예전 종목당 금액 - 0 이 아니면 budget 이 비었을 때만 참고
    poll_seconds: int = 30
    k: float = 0.5  # 변동성 돌파 계수
    # ★★★ "암호화폐는 거래량이 적은 경우 거래를 하지 않도록 보완해" 요청. 코인마다 단위 가치가
    # 달라(BTC 1개와 잡코인 1개는 전혀 다른 돈이다) 원화 환산 거래대금(종가×거래량)으로 거른다 -
    # 국내주식 화면(screen.min_trading_amount)과 같은 원칙이다. 최근 여러 봉의 평균으로 보는
    # 이유는 봉 하나(특히 1분봉)는 우연히 거래가 뜸했을 뿐인데 오판할 수 있어서다.
    # 0이면 끈다(기본은 5백만원 - 유동성이 극히 얇은 코인만 걸러내는 보수적인 값).
    min_trading_value_krw: float = 5_000_000.0
    min_trading_value_bars: int = 10  # 평균을 낼 최근 봉 개수
    # ★★★ "크립토 목표도 주식하고 동일하게 세팅해" - 한때 암호화폐는 변동성이 커서 국내·해외
    # 주식보다 넓게 잡았었는데(손절 6%/익절 12%), 사용자 요청으로 국내 risk.* 와 완전히 같은
    # 값으로 맞췄다(2026-09-24). 필요하면 [설정] 화면에서 암호화폐만 다시 넓힐 수 있다.
    stop_loss_pct: float = 0.035
    take_profit_pct: float = 0.08
    trailing_pct: float = 0.05
    max_hold_hours: float = 336.0  # 14일 - 예전 24시간(당일 청산에 가까움)에서 장기 보유로.
    dynamic_take_profit: bool = True  # ADX 기반 익절폭 자동 확대 사용 여부(시장별 선택)
    technique_learning_mode: str = "entry_pref"  # 매매 모델 3단계(RiskCfg 주석 참고) - 시장별 선택
    style: str = "normal"  # 매매 속도(RiskCfg 주석 참고) - "단타 매매 모드를 시장별로 분리해" 요청, 시장별 선택
    daily_loss_limit_pct: float = 0.05  # ★ 국내주식처럼 하루 손실 한도를 둔다(24시간 롤링).
    consecutive_loss_halt: int = 3       # ★ 연속 손절 N회면 신규 진입을 멈춘다.
    # ★★★ "연속 손절 후 언제 재개되는지 알 수 있게 하고, 그 간격을 설정
    # 하게 해달라"는 요청. 예전엔 "직전 24시간 창에서 손절이 빠질 때까지"
    # 라는 암묵적 규칙뿐이라 사용자가 재개 시점을 알 수 없었다.
    # ★★★ "모든 장에서 3시간이면 재개" 요청으로 기본값을 다른 시장(RiskCfg.
    # loss_halt_cooldown_hours 참고)과 동일한 3시간으로 통일한다. 암호화폐는
    # 24시간 시장이라 세션 개념이 없어 시간 경과만 본다(국내·해외처럼 세션
    # 변경 조기 재개는 없음).
    loss_halt_cooldown_hours: float = 3.0
    # ★★★ 실제 이력 분석 - 손절 직후 30분 안에 같은 코인을 다시 산 10건이 전부 손실(승률 0%)이었다.
    # 손절이 난 자리는 아직 흔들리는 구간이라 바로 재진입하면 같은 노이즈에 또 걸린다. 국내·해외에는
    # 있던 손실 후 재진입 쿨다운이 암호화폐에만 없었다. 0 이면 끈다.
    reentry_cooldown_minutes: float = 30.0
    # ★★★ "전날 거래대금 상위 10종목만 대상으로" - 켜면 매일(KST 자정 이후 처음) 빗썸 KRW 마켓 중 전날
    # 거래대금 상위 top_volume_count 종목(스테이블코인 제외)만 감시한다. 위 watchlist 는 이 선정이
    # 실패했거나 끄면 쓰는 기본 목록이다. 이미 보유한 코인은 청산 관리를 위해 항상 포함한다.
    auto_top_volume: bool = True
    top_volume_count: int = 10
    # 감시 종목이 늘어도 동시에 들고 있는 코인 수는 이 값으로 제한한다(과도한 분산·자금 부족 방지).
    max_positions: int = 5
    # ★★★ 모의매매에 빗썸 수수료(편도 약 0.04%)를 반영한다 - 예전엔 0으로
    # 취급돼 실제보다 성과가 좋게 보였다.
    commission_pct: float = 0.0004
    # ★★★ "설정 검증" 요청 중 발견 - 국내·해외주식(risk.daily_max_trades)에는
    # 있는 과매매 방지 장치가 암호화폐에는 아예 없었다. 코인은 24시간
    # 시장이라 "하루"가 아니라 직전 24시간 롤링 창(halt_info() 의 다른
    # 한도들과 같은 방식)으로 센다. 기본 50회(사용자 요청). 스윙은 "단타가
    # 아니다"라는 판단에 따라 이 상한을 두지 않는다.
    daily_max_trades: int = 50


@dataclass
class OverseasCfg:
    """★★ 해외주식(미국) 자동매매. 국내주식과 완전히 같은 매매 기법
    (Playbook, entry_order/exit_enabled 도 국내주식 설정 그대로)과 같은
    계좌(TossClient)를 쓴다 - 사용자 요청. 다르게 두는 것은 딱 2가지뿐:
    실제 매매·성과 기록(OverseasEngine 이 별도 파일에 저장), 장 열리는
    시간(미국 정규장). 종목 선정도 auto_select 를 켜면 국내주식과 같은
    방식(거래대금·급등 랭킹 기반 자동 선정)으로 맞출 수 있다.
    """
    enabled: bool = False
    mode: str = "web"  # web(관찰)|paper(모의매매)|live(실거래) - 국내주식과 같은 3단계.
    # ★★★ 기본 관심 종목 - 매그니피센트 7(미국 시총 상위 대형 기술주)과
    # AI반도체 대표주를 담는다. 예전엔 3종목뿐이라 선택지가 지나치게
    # 좁았다(종목당 배정도 1/3씩 쏠렸다).
    #   시가총액 상위(대략): AAPL MSFT GOOGL AMZN NVDA META TSLA AVGO TSM BRK.B
    #   AI반도체(추가): AMD(GPU) MU(HBM) ASML(노광장비) ARM(설계) SMCI(AI서버) INTC(인텔)
    #   지수·반도체 ETF: SPY(S&P500) SMH·SOXX(반도체)
    # ★ NVDA·AVGO·TSM 은 시가총액 상위이면서 AI반도체이기도 해 두 묶음에 모두 속하지만
    # 중복 없이 한 번만 넣는다. ★ "muu dram ram spy 지수 관련 etf 추가" 요청 중 정확히
    # 어떤 티커를 말하는지 불확실했던 부분(muu/dram/ram)은 가장 널리 쓰이는 반도체·메모리
    # 관련 ETF(SMH, SOXX)와 S&P500 지수 ETF(SPY)로 채웠다 - 원하는 정확한 티커가 따로 있으면
    # [설정] 화면에서 직접 추가해 달라.
    watchlist: list = field(default_factory=lambda: [
        # 시가총액 상위
        "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "TSM", "BRK.B",
        # AI반도체(시가총액 상위에 없는 것만 추가)
        "AMD", "MU", "ASML", "ARM", "SMCI", "INTC",
        # 지수·반도체 ETF
        "SPY", "SMH", "SOXX",
    ])
    allocation_pct: float = 0.3  # (폐지) 예전 비율 - 이제 budget_usd 를 쓴다.
    # ★★★ 실제로 겪은 문제 - 종목당 배정금액을 감시종목 전체 개수(기본
    # 15종목)로 나누고 있었다. 실제로 동시에 15종목을 다 사는 일은 거의
    # 없는데도(1주일 백테스트에서 동시 보유 1~2종목이 대부분) 자금을 항상
    # 15등분해 버리니 종목당 배정이 약 6만원(≈$43)에 불과했다 - 승률이
    # 괜찮아도 건당 손익이 통계적으로 무의미할 만큼 작아 수익을 낼 수
    # 없었다. 국내주식의 capital.max_positions 와 같은 개념으로, "동시에
    # 최대 몇 종목까지 들고 갈 생각인지"를 따로 두고 그 값으로 나눈다
    # (감시종목 수가 아니라). 감시종목은 그대로 15개를 다 스캔하되, 자금은
    # 실제로 동시에 들고 갈 만큼만 나눈다.
    max_positions: int = 4
    poll_seconds: int = 300  # ★ 미국 주식은 국내처럼 초 단위로 급하게 안 본다 - 기본 5분.
    # ★★★ "본장·프리장·애프터장·데이장 등 장 별로 거래를 할지 사용자가 선택하게 해" -
    # 예전엔 trade_24h 하나로 "24시간 전부" 또는 "정규장만" 둘 중 하나만 고를 수 있었다.
    # 이제 미국 주식의 4개 세션을 각각 따로 켜고 끈다(overseas_engine.py 의 us_phase() 참고).
    # ★ "해외장도 본장이 디폴트 선택되도록" - 기본값을 국내주식과 같은 원칙(정규장만 켜짐)으로
    # 바꿨다. 시간외에는 호가 간격이 넓고 유동성이 얇아 슬리피지가 커질 수 있어, 원하는
    # 사람만 켜는 쪽이 더 안전한 기본값이다(예전 24시간 기본값에서 바뀐 것 - 켜져 있던
    # 사람은 [설정]에서 다시 켜야 한다).
    trade_premarket: bool = False   # 프리장(뉴욕 04:00~09:30)
    trade_regular: bool = True      # 본장(뉴욕 09:30~16:00, 정규장)
    trade_afterhours: bool = False  # 애프터장(뉴욕 16:00~20:00)
    trade_overnight: bool = False   # 데이장·야간거래(뉴욕 20:00~04:00, 한국의 주간)
    budget_usd: float = 10000.0  # ★ 해외주식 총 투자금액(달러). 종목당 한도 = budget_usd / max_positions
    # ★ 보유 종목의 손절·익절 확인 주기(초). 진입 탐색은 poll_seconds(기본 5분)마다 하지만, 이미 산 종목을
    # 5분에 한 번만 보면 -2.5% 손절선을 지나쳐 더 크게 잃을 수 있어 보유 종목만 더 자주 본다.
    manage_seconds: int = 60
    # ★★★ "미국주식 선정도 국내주식과 동일하게 자동으로 선정" - 켜면
    # watchlist(고정 목록) 대신, 매일 토스 rankings API(거래대금·급등
    # 상위, marketCountry="US")로 오늘의 감시 종목을 자동으로 다시 뽑는다.
    # 기본값은 False - 켜지 않으면 지금까지와 동일하게 고정 watchlist 를 쓴다.
    auto_select: bool = False
    auto_select_count: int = 10  # 자동 선정 시 몇 종목을 뽑을지.
    # ★★★ 실제로 겪은 문제 - auto_select 랭킹(거래대금·급등)에 가격·품질
    # 필터가 전혀 없어서 그날 가장 많이 움직인 초저가·초소형주(예:
    # $0.1~0.2대 페니스톡)가 뽑혔다. 이런 종목은 원래 급등락이 심해
    # 고정 2.5%/5% 손절과 상성이 최악이라 과다 손절로 이어진다.
    min_price: float = 5.0
    # ★ 이미 크게 오른 뒤 쫓아 사는 것도 같은 문제를 일으킨다 - 국내주식
    # 스크리너의 max_change_rate(과열 제외) 개념을 해외주식 자동선정에도 둔다.
    max_daily_change_pct: float = 0.35
    # ★★★ "미국증시도 한국처럼 테마주로 분류하고 테마주를 자동으로 산정" - 켜면 미국 종목을 테마로 묶어 오늘
    # 동반 상승한 테마의 강한 종목을 자동으로 거래 대상에 넣는다(us_themes.py). 관심 종목(watchlist)은
    # 테마와 별개로 항상 함께 거래 대상이다.
    theme_select: bool = True
    top_themes: int = 3
    candidates_per_theme: int = 3
    min_theme_members_up: int = 2
    theme_min_change_rate: float = 0.01
    theme_max_change_rate: float = 0.12
    theme_min_trading_amount: float = 20000000.0  # 달러
    theme_refresh_minutes: int = 60
    # ★★★ "해외주식 손절/익절폭을 국내와 별도로 분리하되 기본값은 국내와 동일하게" - 원화·달러,
    # 변동성이 다른 시장인데도 지금까지는 국내용 risk.* 를 그대로 썼다(암호화폐는 이미 crypto.*
    # 로 자기 값을 갖고 있던 것과 다른 점). 암호화폐와 같은 방식으로 Playbook(risk=...) 오버라이드에
    # 실제로 연결해(overseas_engine.py) 화면에만 보이고 청산 판정엔 안 쓰이는 일이 없게 한다.
    # 기본값은 지금까지 공유해 온 국내 risk.* 값과 동일하게 맞춰 시작한다(원한다면 따로 조정).
    stop_loss_pct: float = 0.035
    take_profit_pct: float = 0.08
    trailing_pct: float = 0.05
    trailing_arm_pct: float = 0.03
    max_hold_minutes: float = 4320.0
    dynamic_take_profit: bool = True  # ADX 기반 익절폭 자동 확대 사용 여부(시장별 선택)
    technique_learning_mode: str = "entry_pref"  # 매매 모델 3단계(RiskCfg 주석 참고) - 시장별 선택
    style: str = "normal"  # 매매 속도(RiskCfg 주석 참고) - "단타 매매 모드를 시장별로 분리해" 요청, 시장별 선택
    # ★★★ "국장과 해외장 설정도 서로 분리가 안됐게 있는지 확인해 - 일 최대 거래건수는
    # 각각 50회로" 요청 - 예전엔 해외주식이 국내용 risk.daily_max_trades 를 그대로
    # 빌려 썼다(위 stop_loss_pct 등과 같은 문제). 손절/익절폭과 같은 이유로 독립된
    # 값을 둔다(overseas_engine.halt_info() 도 이제 이 값을 본다).
    daily_max_trades: int = 50


@dataclass
class SwingCfg:
    """★★★ "단타 매매 외에 스윙 매매도 추가해달라"는 요청 - 국내주식 계좌를 그대로 쓰되
    (같은 TossClient), 봉 하나가 1분이 아니라 하루(일봉)인 완전히 다른 시간축으로 돈다.
    그래서 국내주식(strategy/risk)과 파라미터를 공유하지 않고 CryptoCfg/OverseasCfg 처럼
    독립된 설정을 둔다 - 스윙은 손절·익절 폭이 며칠~몇 주치 변동을 견뎌야 하므로 단타보다
    몇 배 넓다(예: 손절 2.5% vs 8%).

    ★ mode 는 국내주식 day-trading mode 와 완전히 독립된 값이다(암호화폐·해외주식과 같은 원칙) -
    "국내 단타만 실거래로 켜고 싶었는데 스윙까지 같이 실거래가 되는" 위험한 조합을 막는다.
    """
    enabled: bool = False
    mode: str = "paper"  # web(관찰)|paper(모의매매)|live(실거래) - 국내주식과 같은 3단계.
    live: bool = False  # ★ 하위호환용 - load_config() 가 mode 로부터 다시 계산해 덮어쓴다.
    entry_order: list = field(default_factory=lambda: [
        "swing_ma_pullback", "swing_breakout", "swing_golden_cross",
    ])
    exit_enabled: list = field(default_factory=lambda: ["fixed", "trailing", "time_stop", "momentum_fade"])
    # ★★★ "스윙매매 대상은 국내주식·해외주식·암호화폐 모두 해당되도록" - 세 시장을 동시에 감시·
    # 보유하는 하나의 엔진이다(시장마다 따로 켜고 끄지 않는다). watchlist 는 국내(6자리 코드),
    # overseas_watchlist 는 해외(티커), crypto_watchlist 는 암호화폐(빗썸 마켓 코드) 전용 관심
    # 목록이다 - 각 시장의 단타 관심 종목(overseas.watchlist/crypto.watchlist)과는 별개다.
    watchlist: list = field(default_factory=list)
    overseas_watchlist: list = field(default_factory=list)
    crypto_watchlist: list = field(default_factory=list)
    budget: float = 10000000.0  # ★ 스윙 총 투자금액(원 기준). 종목당 한도 = budget / max_positions.
    # ★ 해외주식은 달러 그대로, 암호화폐는 원화 그대로를 이 예산과 환산 없이 섞어 쓴다 - "통합" 화면이
    # 원화·달러를 환산 없이 단순 합산하는 것과 같은 근사다(정확한 금액보다 하나의 예산으로 관리하는
    # 편의를 우선했다).
    max_positions: int = 5
    poll_seconds: int = 1800  # ★ 일봉 기반이라 국내 단타(초 단위)처럼 자주 볼 필요가 없다 - 기본 30분.
    # ★★★ "수익을 최대한 추구한다 - 익절폭을 넓혀 크게 먹기, 장기 보유 허용" - 스윙은
    # 원래도 며칠~몇 주 보유가 전제였지만, 목표를 더 넓히고 최대 보유 기간도 늘렸다.
    stop_loss_pct: float = 0.10
    take_profit_pct: float = 0.40
    trailing_pct: float = 0.10
    max_hold_days: float = 60.0
    dynamic_take_profit: bool = True  # ADX 기반 익절폭 자동 확대 사용 여부(시장별 선택)
    technique_learning_mode: str = "entry_pref"  # 매매 모델 3단계(RiskCfg 주석 참고) - 시장별 선택
    # ★ 후보 필터 1 - 이 일수 이동평균 위(중기 상승 추세)인 종목만 스윙 후보로 인정한다.
    #   단타처럼 당일 변동성만 보고 사면 스윙(며칠~몇 주 보유)에서는 추세 자체가 꺾인 종목을
    #   잡기 쉽다.
    trend_filter_ma: int = 60
    # ★★★ 후보 필터 2 - "종목선정시 단타매매와는 다른 기법 사용하고, 최근 일주일 동안 실제
    # 주가 변동에 따라 선정" - 단타는 오늘 하루의 실시간 등락률·거래량으로 고르지만, 스윙은
    # 실제로 지난 5거래일(약 1주일) 동안 종가가 얼마나 움직였는지(확정된 결과)로 고른다.
    # 이 값(기본 0=보합 이상, 즉 최근 1주 하락 종목은 제외) 이상 오른 종목만 후보로 남긴다.
    week_momentum_min_pct: float = 0.0
    consecutive_loss_halt: int = 3
    # ★★★ "모든 장에서 3시간이면 재개" 요청 - 예전엔 스윙만 단위가 일(day)이라
    # loss_halt_cooldown_days(기본 3일)였다. 다른 3개 시장과 단위·기본값을
    # 맞추기 위해 시간(hour) 단위 필드로 바꾸고 기본값도 3시간으로 통일한다.
    loss_halt_cooldown_hours: float = 3.0


@dataclass
class Config:
    mode: str
    capital: CapitalCfg
    risk: RiskCfg
    screen: ScreenCfg
    entry: EntryCfg
    exit: ExitCfg
    strategy: StrategyCfg
    live: LiveCfg
    ui: UiCfg
    simulation: SimulationCfg
    news: NewsCfg
    costs: CostCfg
    notify: NotifyCfg
    overseas: OverseasCfg
    crypto: CryptoCfg
    swing: SwingCfg
    themes_file: str
    state_dir: str
    log_dir: str
    sizing: SizingCfg = field(default_factory=SizingCfg)
    client_id: str = ""
    client_secret: str = ""
    bithumb_access_key: str = ""
    bithumb_secret_key: str = ""
    groq_api_key: str = ""
    groq_api_key2: str = ""
    account_seq: int | None = None

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def is_sim(self) -> bool:
        return self.mode == "sim"

    @property
    def is_web(self) -> bool:
        return self.mode == "web"

    @property
    def is_replay(self) -> bool:
        return self.mode == "replay"

    @property
    def uses_fake_data(self) -> bool:
        return self.mode in ("sim", "replay")

    @property
    def needs_toss_key(self) -> bool:
        return self.mode in ("paper", "live")


def load_env_file(path: str) -> None:
    """.env 형식 파일을 읽어 환경변수로 등록한다.
    이미 설정되어 있는 환경변수가 우선한다 (setdefault).
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


FAST_EXTRA_ENTRIES = ("vwap_pullback", "orb", "volume_dry_pop", "bull_flag", "ma_pullback", "vwap_reclaim")
DOMESTIC_ONLY_ENTRIES = ("open_gap", "close_squeeze")  # 국내 정규장 시각에 묶인 기법 - 해외·암호화폐에는 넣지 않는다
STYLES = ("normal", "fast", "scalp")


def apply_style(cfg: Config) -> None:
    """★★★ 매매 속도(style) - 사용자가 관련 값을 일일이 손대지 않아도 되도록 한 번에 정한다.

    ★★★ "단타 매매 모드를 시장별로 분리해" (2026-09-24) - 예전엔 Config.style 하나로 국내·해외·
    암호화폐가 전부 같은 속도를 썼다. 이제 cfg.risk.style/cfg.overseas.style/cfg.crypto.style 을
    각자 따로 판단한다 - 국내만 fast 로 두고 해외·암호화폐는 normal 로 둘 수 있다. 스윙은 원래
    며칠~몇 주 보유가 전제라 이 개념이 없다(자기 목표값을 그대로 쓴다).

    ★★★ 구조적 한계 - 해외주식은 국내주식과 진입 기법 목록(cfg.strategy.entry_order)을 공유한다
    (Playbook 이 entry_order 를 안 넘기면 cfg.strategy.entry_order 를 기본값으로 쓰기 때문 -
    overseas_engine.py 참고). 그래서 "fast 일 때 진입 기법을 더 추가한다"는 동작은 국내(cfg.risk.style)
    기준으로만 판단한다 - 해외만 fast 로 둬도 해외의 손절·익절·시세 확인 주기는 fast 값을 쓰지만,
    진입 기법 목록 자체는 국내 설정을 따른다. 암호화폐는 자기만의 entry_order 가 있어 크립토 자신의
    style 로 독립적으로 기법 목록까지 판단한다.

    fast(빠른 단타): 시세 확인 주기를 짧게, 재진입 금지를 짧게, 하루 거래 한도를 넉넉히, 진입 기법을
      더 다양하게(6개 추가, 국내·암호화폐만). 손절·익절·추적폭은 "매매원칙" 개편의 넓은 값(3.5%/8~15%
      등)보다 훨씬 도달하기 쉬운 폭으로 좁힌다 - 그 넓은 값은 며칠씩 들고 가는 것도 허용하는 전제라,
      수십 분 안에 정리하는 빠른 단타에서는 익절에 닿기 전에 거의 항상 다른 청산(ATR 손절 등)으로 먼저 잘렸다.
    scalp(스캘핑·실험용): fast 보다도 더 작은 목표로 자주 거래.

    ★★★ 2026-09-23 이력(당시엔 전체 공통이었다) - "익절폭만 보통보다 좁게"였던 옛 규칙을 없애고
    fast 를 "속도만" 담당하게 했더니 하루도 안 돼 실거래(모의매매)에서 되짚혔다 - 크립토·해외주식
    32건 중 30건이 손실로, 전부 ATR 손절·모멘텀 소멸로만 청산되고 고정 익절·트레일링은 한 번도 안
    나왔다. 원인: ① atr_multiple 이 익절폭을 넓힐 때 같이 안 넓어짐(strategy.params 로 조정) ②
    넓힌 익절폭 자체가 빠른 단타의 보유시간(수십 분)안에는 현실적으로 거의 안 닿는 크기였음 - 그래서
    fast 는 손절은 검증된 2.5%(한때 1.8%로 좁혔다가 실거래 승률이 눈에 띄게 나빠져 되돌린 값)를
    유지하고 익절·추적폭만 비례해서 좁힌다. normal 로 돌리면 각 시장 설정값(config.yaml) 그대로 쓴다.
    """
    r, o, c = cfg.risk, cfg.overseas, cfg.crypto

    # ★ 국내(및 국내와 진입기법을 공유하는 해외) 기법 목록 확장은 국내 style 기준으로만 판단한다.
    if r.style in ("fast", "scalp"):
        for key in FAST_EXTRA_ENTRIES:
            if key not in cfg.strategy.entry_order:
                cfg.strategy.entry_order.append(key)
        for key in DOMESTIC_ONLY_ENTRIES:
            if key not in cfg.strategy.entry_order:
                cfg.strategy.entry_order.append(key)

    if r.style in ("fast", "scalp"):
        r.cooldown_minutes = min(r.cooldown_minutes, 5)
        r.daily_max_trades = max(r.daily_max_trades, 30)
        cfg.entry.poll_seconds = min(cfg.entry.poll_seconds, 15)
        cfg.entry.rescreen_minutes = min(cfg.entry.rescreen_minutes, 10)
        if r.style == "fast":
            # ★ 손절은 2.5%(검증된 값) 유지, 익절·추적폭만 "수십 분 안에 실제로 닿을 수 있는" 크기로 좁힌다.
            # ADX 가 강할 때 넓혀주는 dynamic_take_profit_pct(sizing.py)가 진짜 추세가 붙은 거래는 여전히
            # 최대 2배까지 더 태워준다 - 기본값은 작게, 강한 신호만 자동으로 커지는 구조는 그대로 유지된다.
            r.stop_loss_pct, r.take_profit_pct = 0.025, 0.05
            r.trailing_stop_pct, r.trailing_arm_pct = 0.03, 0.02
        elif r.style == "scalp":
            r.stop_loss_pct, r.take_profit_pct = 0.012, 0.024
            r.trailing_arm_pct, r.trailing_stop_pct = 0.008, 0.008
            r.max_consecutive_losses = max(r.max_consecutive_losses, 3)
            cfg.exit.max_hold_minutes = min(cfg.exit.max_hold_minutes, 30)

    if o.style in ("fast", "scalp"):
        o.poll_seconds = min(o.poll_seconds, 60)
        o.manage_seconds = min(o.manage_seconds, 15)
        if o.style == "fast":
            o.stop_loss_pct, o.take_profit_pct = 0.025, 0.05
            o.trailing_pct, o.trailing_arm_pct = 0.03, 0.02
        elif o.style == "scalp":
            # ★ scalp 전용 해외 값은 예전엔 아예 없었다(국내·크립토만 있었음) - 시장별로 완전히
            # 분리하는 김에, 국내 scalp 와 같은 비율로 채워 일관되게 했다(비워두면 혼란스럽다).
            o.stop_loss_pct, o.take_profit_pct = 0.012, 0.024
            o.trailing_arm_pct, o.trailing_pct = 0.008, 0.008

    if c.style in ("fast", "scalp"):
        c.poll_seconds = min(c.poll_seconds, 10)
        c.reentry_cooldown_minutes = min(c.reentry_cooldown_minutes, 10)
        for key in FAST_EXTRA_ENTRIES:
            if key not in c.entry_order:
                c.entry_order.append(key)
        if c.style == "fast":
            # ★ "크립토 목표도 주식하고 동일하게" - fast 에서도 국내·해외와 완전히 같은 값을 쓴다.
            c.stop_loss_pct, c.take_profit_pct = 0.025, 0.05
            c.trailing_pct = 0.03
        elif c.style == "scalp":
            c.stop_loss_pct, c.take_profit_pct, c.trailing_pct = 0.015, 0.03, 0.01
            c.max_hold_hours = min(c.max_hold_hours, 3.0)


# config.yaml 최상위에 올 수 있는 항목 전부(load_config 의 미확인 항목 검사와
# server.py 의 POST /api/config 가 둘 다 이 목록 하나를 쓴다 - 두 곳에 따로 적으면
# 한쪽만 고치고 잊어버리기 쉽다).
KNOWN_TOP_LEVEL_KEYS = {
    "mode", "capital", "risk", "screen", "entry", "exit", "strategy",
    "live", "ui", "simulation", "news", "notify", "overseas", "crypto", "swing", "costs",
    "themes_file", "state_dir", "log_dir", "account_seq", "sizing",
}

_UNSAFE_PATH_SEGMENT = re.compile(r"^[A-Za-z]:")  # "C:\..." 같은 윈도우 드라이브 문자


def _looks_like_escape(raw_value: str) -> bool:
    """★★★ 실제로 겪을 수 있는 구멍 - themes_file/state_dir/log_dir 는 config.yaml 에 사용자가
    적는 "app_dir() 기준 상대경로"인데, os.path.join(base, value) 는 value 가 절대경로면
    base 를 통째로 무시하고 value 그대로를 돌려준다(파이썬의 흔한 함정). 그래서 절대경로나
    ".." 를 포함한 값을 여기서 미리 걸러낸다 - 그러지 않으면 설정 화면(POST /api/config)에서
    이 값을 아무 경로로나 바꿔, 이 프로그램이 상태·로그 파일을 임의의 위치에 쓰거나(덮어쓰기)
    임의의 위치에서 테마 파일을 읽게 만들 수 있다."""
    v = str(raw_value).replace("\\", "/").strip()
    if not v:
        return True
    if v.startswith("/") or v.startswith("//"):
        return True
    if _UNSAFE_PATH_SEGMENT.match(v):
        return True
    if ".." in v.split("/"):
        return True
    return False


def _safe_user_path(base: str, raw_value: Any, default: str, field_name: str) -> str:
    """themes_file/state_dir/log_dir 을 base(app_dir()) 밖으로 벗어날 수 없게 만들어 반환한다."""
    value = raw_value if raw_value is not None else default
    if not isinstance(value, str) or _looks_like_escape(value):
        raise ValueError(
            f"config.yaml 의 {field_name} 값이 올바르지 않습니다: {value!r}. "
            "절대경로나 상위 폴더(..)는 쓸 수 없습니다 - 프로그램 폴더 밑의 상대경로만 허용됩니다."
        )
    resolved = os.path.normpath(os.path.join(base, value))
    base_norm = os.path.normpath(base)
    if resolved != base_norm and not resolved.startswith(base_norm + os.sep):
        raise ValueError(f"config.yaml 의 {field_name} 값이 프로그램 폴더 밖을 가리킵니다: {value!r}")
    return resolved


def load_config(path: str | None = None) -> Config:
    # exe 첫 실행이면 config.yaml·themes.yaml 을 exe 옆으로 먼저 꺼내 놓는다.
    ensure_user_files()

    if path is None:
        path = app_path("config.yaml")

    with open(path, "r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    unknown_top = set(raw.keys()) - KNOWN_TOP_LEVEL_KEYS
    if unknown_top:
        raise ValueError(f"config.yaml 에 알 수 없는 항목이 있습니다: {', '.join(sorted(unknown_top))}")

    if "mode" not in raw:
        raise ValueError("config.yaml 에 mode 항목이 없습니다.")

    # 시각 문자열은 datetime.time 으로 바꿔 비교·계산이 가능하게 한다.
    entry_raw = dict(raw.get("entry", {}))
    entry_raw["scan_start"] = parse_hhmm(entry_raw["scan_start"])
    entry_raw["scan_end"] = parse_hhmm(entry_raw["scan_end"])

    exit_raw = dict(raw.get("exit", {}))
    exit_raw["force_close_time"] = parse_hhmm(exit_raw["force_close_time"])

    # ★ exe 로 돌 때 상대경로는 임시 폴더를 가리킨다.
    # themes_file/state_dir/log_dir 을 app_dir() 기준 절대경로로 바꾼다.
    base = app_dir()
    themes_file = _safe_user_path(base, raw.get("themes_file"), "themes.yaml", "themes_file")
    state_dir = _safe_user_path(base, raw.get("state_dir"), "state", "state_dir")
    log_dir = _safe_user_path(base, raw.get("log_dir"), "logs", "log_dir")

    cfg = Config(
        mode=raw["mode"],
        capital=_build(CapitalCfg, "capital", raw.get("capital", {})),
        risk=_build(RiskCfg, "risk", raw.get("risk", {})),
        screen=_build(ScreenCfg, "screen", raw.get("screen", {})),
        entry=_build(EntryCfg, "entry", entry_raw),
        exit=_build(ExitCfg, "exit", exit_raw),
        strategy=_build(StrategyCfg, "strategy", raw.get("strategy", {})),
        live=_build(LiveCfg, "live", raw.get("live", {})),
        ui=_build(UiCfg, "ui", raw.get("ui", {})),
        simulation=_build(SimulationCfg, "simulation", raw.get("simulation", {})),
        news=_build(NewsCfg, "news", raw.get("news", {})),
        costs=_build(CostCfg, "costs", raw.get("costs", {})),
        notify=_build(NotifyCfg, "notify", raw.get("notify", {})),
        overseas=_build(OverseasCfg, "overseas", raw.get("overseas", {})),
        crypto=_build(CryptoCfg, "crypto", raw.get("crypto", {})),
        swing=_build(SwingCfg, "swing", raw.get("swing", {})),
        sizing=_build(SizingCfg, "sizing", raw.get("sizing", {})),
        themes_file=themes_file,
        state_dir=state_dir,
        log_dir=log_dir,
        account_seq=raw.get("account_seq"),
    )
    apply_style(cfg)

    # ★★★ API 키는 secrets.yaml 한 곳에서 읽는다(환경변수가 있으면 우선).
    # 예전에는 토스·빗썸이 .env, 텔레그램이 config.yaml 에 흩어져 있어서
    # ① config.yaml 을 공유하면 텔레그램 토큰이 함께 새고
    # ② 키를 옮기거나 지우려면 두 파일을 다 봐야 했다.
    # 이제 secrets.yaml 하나만 백업·공유에서 빼면 된다.
    from daytrader import secrets as _secrets
    cfg.client_id = _secrets.get("toss_client_id")
    cfg.client_secret = _secrets.get("toss_client_secret")
    cfg.bithumb_access_key = _secrets.get("bithumb_access_key")
    cfg.bithumb_secret_key = _secrets.get("bithumb_secret_key")
    cfg.groq_api_key = _secrets.get("groq_api_key")
    cfg.groq_api_key2 = _secrets.get("groq_api_key2")
    # ★ 텔레그램도 같은 곳에서 - config.yaml 에 값이 남아 있으면 그건
    #   예전 버전에서 쓰던 것이므로, 새 위치에 없을 때만 폴백으로 쓴다.
    tg_token = _secrets.get("telegram_token")
    tg_chat = _secrets.get("telegram_chat_id")
    if tg_token:
        cfg.notify.telegram_token = tg_token
    if tg_chat:
        cfg.notify.telegram_chat_id = tg_chat

    # ★★ crypto.live 는 하위호환용 파생값이다 - crypto.mode 하나만 사용자가
    # 정하면 되고, 실제 엔진이 보는 live 플래그는 여기서 mode 로부터 계산한다.
    if cfg.crypto.mode not in ("web", "sim", "paper", "live"):
        raise ValueError(f"crypto.mode 는 web/sim/paper/live 중 하나여야 합니다: {cfg.crypto.mode}")
    cfg.crypto.live = (cfg.crypto.mode == "live")

    # ★★ "암호화폐도 국내주식과 동일한 기법 체계" - 이제 국내
    # playbook.ENTRY_TECHNIQUES/EXIT_TECHNIQUES 레지스트리를 그대로
    # 기준으로 검증한다(예전엔 crypto_playbook.py 전용 레지스트리를 썼다).
    from daytrader.playbook import ENTRY_TECHNIQUES as _ENTRY_REGISTRY
    from daytrader.playbook import EXIT_TECHNIQUES as _EXIT_REGISTRY
    if not cfg.crypto.entry_order:
        raise ValueError("crypto.entry_order 가 비어 있습니다. 최소 1개 기법이 필요합니다.")
    unknown_crypto_entry = set(cfg.crypto.entry_order) - set(_ENTRY_REGISTRY.keys())
    if unknown_crypto_entry:
        raise ValueError(f"crypto.entry_order 에 알 수 없는 기법이 있습니다: {', '.join(sorted(unknown_crypto_entry))}")
    unknown_crypto_exit = set(cfg.crypto.exit_enabled) - set(_EXIT_REGISTRY.keys())
    if unknown_crypto_exit:
        raise ValueError(f"crypto.exit_enabled 에 알 수 없는 기법이 있습니다: {', '.join(sorted(unknown_crypto_exit))}")
    if "fixed" not in cfg.crypto.exit_enabled:
        raise ValueError("crypto.exit_enabled 에 fixed 가 반드시 있어야 합니다 (손절 없는 설정은 금지합니다).")

    if cfg.overseas.mode not in ("web", "sim", "paper", "live"):
        raise ValueError(f"overseas.mode 는 web/sim/paper/live 중 하나여야 합니다: {cfg.overseas.mode}")

    # ★★ 스윙도 코인과 같은 이유로 국내 day-trading mode 와 독립된 값이다.
    if cfg.swing.mode not in ("web", "paper", "live"):
        raise ValueError(f"swing.mode 는 web/paper/live 중 하나여야 합니다: {cfg.swing.mode}")
    cfg.swing.live = (cfg.swing.mode == "live")
    if not cfg.swing.entry_order:
        raise ValueError("swing.entry_order 가 비어 있습니다. 최소 1개 기법이 필요합니다.")
    unknown_swing_entry = set(cfg.swing.entry_order) - set(_ENTRY_REGISTRY.keys())
    if unknown_swing_entry:
        raise ValueError(f"swing.entry_order 에 알 수 없는 기법이 있습니다: {', '.join(sorted(unknown_swing_entry))}")
    unknown_swing_exit = set(cfg.swing.exit_enabled) - set(_EXIT_REGISTRY.keys())
    if unknown_swing_exit:
        raise ValueError(f"swing.exit_enabled 에 알 수 없는 기법이 있습니다: {', '.join(sorted(unknown_swing_exit))}")
    if "fixed" not in cfg.swing.exit_enabled:
        raise ValueError("swing.exit_enabled 에 fixed 가 반드시 있어야 합니다 (손절 없는 설정은 금지합니다).")

    validate(cfg)
    return cfg


def _is_hhmm(s: str) -> bool:
    try:
        parse_hhmm(s)
        return True
    except Exception:
        return False


def validate(cfg: Config) -> None:
    """설정값을 검증한다. 오류 메시지는 무엇이 왜 안 되는지 한국어로 남긴다."""

    if cfg.mode not in {"sim", "replay", "web", "paper", "live"}:
        raise ValueError(f"mode 값이 올바르지 않습니다: {cfg.mode!r}. sim/replay/web/paper/live 중 하나여야 합니다.")

    if cfg.simulation.scenario not in {"normal", "strong_theme", "choppy", "crash"}:
        raise ValueError(f"simulation.scenario 값이 올바르지 않습니다: {cfg.simulation.scenario!r}.")
    if not (1 <= cfg.simulation.speed <= 3600):
        raise ValueError("simulation.speed 는 1~3600 사이여야 합니다.")
    if not (1 <= cfg.simulation.history_days <= 30):
        raise ValueError("simulation.history_days 는 1~30 사이여야 합니다.")
    if not (1 <= cfg.simulation.days <= 60):
        raise ValueError("simulation.days 는 1~60 사이여야 합니다.")

    if not (0 < cfg.capital.per_trade_pct <= 1):
        raise ValueError("capital.per_trade_pct 는 0보다 크고 1 이하여야 합니다.")
    if cfg.capital.max_positions < 1:
        raise ValueError("capital.max_positions 는 1 이상이어야 합니다.")

    if not (0 < cfg.risk.weekly_loss_limit_pct <= 0.5):
        raise ValueError("risk.weekly_loss_limit_pct 는 0보다 크고 0.5 이하여야 합니다.")
    if cfg.risk.weekly_loss_limit_pct < cfg.risk.daily_loss_limit_pct:
        raise ValueError("risk.weekly_loss_limit_pct 는 daily_loss_limit_pct 이상이어야 합니다.")
    if cfg.risk.max_consecutive_losses < 1:
        raise ValueError("risk.max_consecutive_losses 는 1 이상이어야 합니다.")
    if not (0.1 <= cfg.risk.reduced_size_pct <= 1.0):
        raise ValueError("risk.reduced_size_pct 는 0.1~1.0 사이여야 합니다.")
    _LEARNING_MODES = {"none", "entry_pref", "entry_exit_pref"}
    for _sec, _lm in (("risk", cfg.risk.technique_learning_mode), ("overseas", cfg.overseas.technique_learning_mode),
                      ("crypto", cfg.crypto.technique_learning_mode), ("swing", cfg.swing.technique_learning_mode)):
        if _lm not in _LEARNING_MODES:
            raise ValueError(f"{_sec}.technique_learning_mode 값이 올바르지 않습니다: {_lm!r}. "
                              f"{'/'.join(sorted(_LEARNING_MODES))} 중 하나여야 합니다.")
    _WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    if cfg.notify.weekly_review_day not in _WEEKDAYS:
        raise ValueError(f"notify.weekly_review_day 값이 올바르지 않습니다: {cfg.notify.weekly_review_day!r}. "
                          f"{'/'.join(sorted(_WEEKDAYS))} 중 하나여야 합니다.")
    if not (1 <= cfg.notify.monthly_review_day <= 28):
        raise ValueError("notify.monthly_review_day 는 1~28 사이여야 합니다(모든 달에 있는 날짜만 허용).")
    try:
        _o, _c = parse_hhmm(cfg.entry.open_window_start), parse_hhmm(cfg.entry.close_window_end)
    except Exception:
        raise ValueError("entry.open_window_start / close_window_end 는 HH:MM 형식이어야 합니다.")
    if cfg.entry.extra_windows and not (_o <= cfg.entry.scan_start and cfg.entry.scan_end <= _c <= cfg.exit.force_close_time):
        raise ValueError("장 초반·막판 시간대는 open_window_start ≤ scan_start, scan_end ≤ close_window_end ≤ force_close_time 이어야 합니다.")
    for _sec, _st in (("risk", cfg.risk.style), ("overseas", cfg.overseas.style), ("crypto", cfg.crypto.style)):
        if _st not in STYLES:
            raise ValueError(f"{_sec}.style 은 normal · fast · scalp 중 하나여야 합니다.")
    z = cfg.sizing
    if not (0.1 <= z.min_mult <= 1.0 <= z.max_mult <= 3.0):
        raise ValueError("sizing.min_mult 는 0.1~1.0, sizing.max_mult 는 1.0~3.0 이어야 합니다.")
    if not (0.2 <= z.initial_ratio <= 1.0):
        raise ValueError("sizing.initial_ratio 는 0.2~1.0 사이여야 합니다.")
    if not (0 <= z.max_adds <= 5):
        raise ValueError("sizing.max_adds 는 0~5 사이여야 합니다.")
    if not (0.05 <= z.first_exit_ratio <= 0.9 and 0.05 <= z.second_exit_ratio <= 0.95):
        raise ValueError("sizing.first_exit_ratio/second_exit_ratio 는 0.05~0.9 사이여야 합니다.")
    if not (0 <= z.take_profit_adx_threshold < z.take_profit_adx_full <= 100):
        raise ValueError("sizing.take_profit_adx_threshold < take_profit_adx_full 이고 0~100 사이여야 합니다.")
    if z.take_profit_max_widen < 1.0:
        raise ValueError("sizing.take_profit_max_widen 은 1 이상이어야 합니다(1=안 넓힘).")
    if cfg.overseas.budget_usd < 0 or cfg.crypto.budget < 0 or cfg.capital.allocation < 0 or cfg.swing.budget < 0:
        raise ValueError("투자금액은 0 이상이어야 합니다.")
    if cfg.risk.stop_loss_pct <= 0:
        raise ValueError("risk.stop_loss_pct 는 0보다 커야 합니다.")
    if cfg.risk.take_profit_pct <= 0:
        raise ValueError("risk.take_profit_pct 는 0보다 커야 합니다.")
    if cfg.overseas.stop_loss_pct <= 0 or cfg.overseas.take_profit_pct <= 0:
        raise ValueError("overseas.stop_loss_pct/take_profit_pct 는 0보다 커야 합니다.")

    be = breakeven_pct(cfg.costs.commission_pct, cfg.costs.tax_pct)
    if cfg.risk.take_profit_pct <= be * 2:
        raise ValueError(
            f"익절폭({cfg.risk.take_profit_pct:.4%})이 왕복 비용({be:.4%}) 대비 너무 좁습니다. "
            "비용을 이기지 못하는 설정입니다."
        )

    # ★★★ 3-5 - 위 검증은 국내주식(cfg.risk.take_profit_pct)뿐이었다. 다른 시장도 익절폭이
    # 왕복 비용보다 좁으면 손절·익절을 오가기만 해도 계좌가 갉아 먹히는 건 마찬가지인데
    # 검증이 없었다. 각 시장이 실전 손익 계산에서 실제로 쓰는 비용 상수를 그대로 재사용한다
    # (다른 값을 새로 지어내면 검증과 실제 채점이 서로 다른 기준을 쓰게 된다):
    #   - 암호화폐: crypto.commission_pct(빗썸 수수료) - 거래세 없음(crypto_engine.py/
    #     bithumb_api.py 어디에도 세금 모델이 없다).
    #   - 해외주식: cfg.costs.commission_pct 를 국내와 그대로 공유해서 쓴다(overseas_broker.py
    #     PaperOverseasBroker 가 실제로 이 값을 받아 왕복 수수료를 계산한다 - "국내 수수료율을
    #     근사로 씀" 주석 참고). 거래세는 모델링하지 않는다(0).
    #   - 스윙: swing_engine.py PaperSwingBroker 도 국내 cfg.costs(수수료+거래세)를 그대로 받는다
    #     - 국내주식과 완전히 같은 비용식(be)이라 새로 계산할 것 없이 그대로 재사용한다.
    be_crypto = breakeven_pct(cfg.crypto.commission_pct, 0.0)
    if cfg.crypto.take_profit_pct <= be_crypto * 2:
        raise ValueError(
            f"crypto.take_profit_pct({cfg.crypto.take_profit_pct:.4%})이 암호화폐 왕복 비용"
            f"({be_crypto:.4%}, crypto.commission_pct={cfg.crypto.commission_pct:.4%} 기준) 대비 "
            "너무 좁습니다. 비용을 이기지 못하는 설정입니다."
        )

    be_overseas = breakeven_pct(cfg.costs.commission_pct, 0.0)
    if cfg.overseas.take_profit_pct <= be_overseas * 2:
        raise ValueError(
            f"overseas.take_profit_pct({cfg.overseas.take_profit_pct:.4%})이 해외주식 왕복 비용"
            f"({be_overseas:.4%}, 국내와 공유하는 costs.commission_pct 기준) 대비 너무 좁습니다. "
            "비용을 이기지 못하는 설정입니다."
        )

    if cfg.swing.take_profit_pct <= be * 2:
        raise ValueError(
            f"swing.take_profit_pct({cfg.swing.take_profit_pct:.4%})이 스윙 왕복 비용"
            f"({be:.4%}, 국내와 공유하는 costs.commission_pct/tax_pct 기준) 대비 너무 좁습니다. "
            "비용을 이기지 못하는 설정입니다."
        )

    if not _is_hhmm(cfg.screen.auto_time):
        raise ValueError("screen.auto_time 은 HH:MM 형식이어야 합니다.")

    if not (cfg.entry.scan_start < cfg.entry.scan_end < cfg.exit.force_close_time):
        raise ValueError("entry.scan_start < entry.scan_end < exit.force_close_time 순서를 지켜야 합니다.")

    if not (1 <= cfg.exit.force_close_deadline_min <= 20):
        raise ValueError("exit.force_close_deadline_min 은 1~20 사이여야 합니다.")

    # ★★★ 실제로 겪을 뻔한 사고 - force_close_time 이후 force_close_deadline_min
    # 분의 유예를 두고 강제청산을 시도하는데, 그 유예가 15:20 단일가(종가) 매매
    # 시작 시각을 넘기면 이미 접속성 매매(연속경쟁매매)가 끝난 뒤라 지정가/시장가
    # 강제청산 주문이 정상적으로 체결되지 않는다. 유예가 끝나는 시각이 반드시
    # 15:19까지여야 한다(15:20부터는 단일가 매매 - 그 전에 끝나야 한다).
    _force_close_end_min = (
        cfg.exit.force_close_time.hour * 60 + cfg.exit.force_close_time.minute
        + cfg.exit.force_close_deadline_min
    )
    if _force_close_end_min > 15 * 60 + 19:
        _end_h, _end_m = divmod(_force_close_end_min, 60)
        raise ValueError(
            f"exit.force_close_time({cfg.exit.force_close_time.strftime('%H:%M')})"
            f" + force_close_deadline_min({cfg.exit.force_close_deadline_min}분)이"
            f" {_end_h:02d}:{_end_m:02d}에 끝나 15:20 단일가(종가) 매매 시작 전(15:19까지)에"
            " 강제청산을 마치지 못합니다. force_close_time 을 앞당기거나"
            " force_close_deadline_min 을 줄이세요."
        )

    # ★★★ "조건부 오버나이트" 세부값 검증.
    for _bf, _bn in (
        (cfg.exit.allow_overnight, "exit.allow_overnight"),
        (cfg.exit.overnight_skip_before_holiday, "exit.overnight_skip_before_holiday"),
        (cfg.exit.overnight_breakeven_stop, "exit.overnight_breakeven_stop"),
    ):
        if not isinstance(_bf, bool):
            raise ValueError(f"{_bn} 은 true/false 여야 합니다.")
    if not (0 <= cfg.exit.overnight_min_profit_pct <= 0.5):
        raise ValueError("exit.overnight_min_profit_pct 는 0~0.5 사이여야 합니다.")
    if cfg.exit.overnight_max_days != 1:
        raise ValueError(
            "exit.overnight_max_days 는 지금은 1만 지원합니다 - 하루를 넘는 연속 보유는 "
            "데이트레이딩이 아니라 스윙 매매의 영역이라 이 엔진에서는 아직 지원하지 않습니다."
        )

    if not (1 <= cfg.live.degrade_after_failures <= 20):
        raise ValueError("live.degrade_after_failures 는 1~20 사이여야 합니다.")
    if not (1 <= cfg.live.degrade_halt_minutes <= 60):
        raise ValueError("live.degrade_halt_minutes 는 1~60 사이여야 합니다.")
    if not (2 <= cfg.live.reconcile_every_loops <= 120):
        raise ValueError("live.reconcile_every_loops 는 2~120 사이여야 합니다.")

    if cfg.ui.chart_window not in {"5m", "30m", "all"}:
        raise ValueError(f"ui.chart_window 값이 올바르지 않습니다: {cfg.ui.chart_window!r}.")
    if not (200 <= cfg.ui.tick_ms <= 10000):
        raise ValueError("ui.tick_ms 는 200~10000 사이여야 합니다.")

    if not cfg.strategy.entry_order:
        raise ValueError("strategy.entry_order 가 비어 있습니다. 최소 1개 기법이 필요합니다.")
    unknown_entry = set(cfg.strategy.entry_order) - set(ENTRY_TECHNIQUE_KEYS)
    if unknown_entry:
        raise ValueError(f"strategy.entry_order 에 알 수 없는 기법이 있습니다: {', '.join(sorted(unknown_entry))}")

    unknown_exit = set(cfg.strategy.exit_enabled) - set(EXIT_TECHNIQUE_KEYS)
    if unknown_exit:
        raise ValueError(f"strategy.exit_enabled 에 알 수 없는 기법이 있습니다: {', '.join(sorted(unknown_exit))}")
    if "fixed" not in cfg.strategy.exit_enabled:
        raise ValueError("strategy.exit_enabled 에 fixed 가 반드시 있어야 합니다 (손절 없는 설정은 금지합니다).")

    if cfg.is_live and not cfg.live.require_preflight:
        raise ValueError("mode=live 인 경우 live.require_preflight 는 반드시 true 여야 합니다.")

    if cfg.news.mode not in {"off", "view", "avoid", "boost"}:
        raise ValueError(f"news.mode 값이 올바르지 않습니다: {cfg.news.mode!r}. off/view/avoid/boost 중 하나여야 합니다.")
    if not (1 <= cfg.news.risk_hours <= 72):
        raise ValueError("news.risk_hours 는 1~72 사이여야 합니다.")
