"""이벤트 기반 장중 백테스트 - daytrader/playbook.py 의 진짜 진입·청산 로직을 그대로 쓴다.

★★★ 이것은 technique_backtest.py 의 축소판이 아니다(그건 종목 하나씩 따로 재생하고,
청산도 고정 손절·익절·추적손절만 공통으로 쓴다). 여기서는:
  - 여러 종목을 같은 시각에 나란히 평가해 진짜 엔진(daytrader/engine.py 의 try_entries)처럼
    "그 순간 가장 강한 신호"부터 예산(daily_max_trades·max_positions)을 쓴다.
  - 청산도 실제 청산 기법 5종(FixedExit·TrailingExit·AtrStopExit·TimeStopExit·
    MomentumFadeExit)과 ForceCloseExit 을 Playbook.evaluate_exit() 로 그대로 평가한다.
  - ctx(SimpleNamespace)는 engine.py 의 try_entries()/manage_positions() 가 만드는 것과
    같은 필드를 채운다 - session_vwap/session_high 는 그 날 09:00 부터 진짜로 누적한다
    (engine._session_stats_for() 와 동일한 계산식).

체결 규칙(3-4 와 같은 원칙):
  - 진입: 신호가 확정된 "닫힌 봉" i 의 다음 봉(i+1) 시가에, 설정한 슬리피지를 얹어 체결한다.
    기본 슬리피지 = risk.max_slippage_pct/2 (퍼센트) + 1틱(절대값). 다음 봉이 없으면(그 날
    마지막 봉) 신호를 버린다 - 체결을 확인할 데이터가 없다.
  - 청산: 매 봉마다 그 봉의 저가/고가로 "봉 안에서 손절·익절 둘 다 닿았는지"를 본다. 둘 다
    닿았으면 보수적으로 손절이 먼저 났다고 본다(저가 쪽을 먼저 평가하고, 뭔가 걸리면 그걸로
    확정한다 - 아무것도 안 걸려야 고가 쪽으로 넘어가 익절·추적을 본다).
  - 비용: daytrader.ticks.trade_pnl() 을 그대로 쓴다(매수·매도 수수료 + 매도세, 왕복 비용을
    engine 과 동일한 식으로 계산해야 백테스트 성적과 실거래 성적을 비교할 수 있다).

★ 단순화(정직하게 밝힌다) - 다음은 재현하지 않는다:
  - 분할 매수(피라미딩)·분할 매도: ctx.scale_out 을 켜면(그리드에서 실험 가능) FixedExit 의
    전량 익절이 실제로 꺼지는 것까지는 재현하지만, "나눠서 판다" 자체는 흉내내지 않는다 -
    scale_out=True 인 거래는 사실상 "익절 없이 트레일링에만 의존"하는 실험이 된다.
  - VI(상한가 근접)·호가 잔량·체결 우선순위·뉴스 반응·주간(weekly_loss_limit_pct) 손실 한도.
  - 연속 손절 중단(RiskGate)은 구현하지만, reduce_after_loss=True(기본값)면 실거래처럼
    "정지"가 아니라 "축소 매수"가 정답인데 축소 매수는 흉내내지 않는다 - reduce_after_loss=False
    일 때만(그리드의 max_consecutive_losses 실험) 의미 있게 작동한다.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from daytrader import sizing
from daytrader import session as session_mod
from daytrader.broker import Position
from daytrader.playbook import Bar, Playbook
from daytrader.signals import atr as atr_of
from daytrader.ticks import round_to_tick, tick_size, trade_pnl
from daytrader.timeutil import combine, day_str, iso, parse_dt

NAN = float("nan")

# ── 진입 시각 기준 세션 버킷(보고서용) - 기법 자체의 open/main/close 창(playbook.windows)과는
#   다른 축이다. 그건 "이 시간대엔 이 기법만" 이고, 이건 "매매 성적을 몇 시 구간으로 나눠 본다". ──
DEFAULT_SESSION_BUCKETS = (
    ("09:00", "09:30"), ("09:30", "11:00"), ("11:00", "13:00"), ("13:00", "14:30"), ("14:30", "15:20"),
)


def session_bucket(hhmm: str, buckets=DEFAULT_SESSION_BUCKETS) -> str:
    for start, end in buckets:
        if start <= hhmm < end:
            return f"{start}~{end}"
    if hhmm >= buckets[-1][1]:
        return f"{buckets[-1][0]}~{buckets[-1][1]}"
    return f"{buckets[0][0]}~{buckets[0][1]}"


def _slip_price(price: float, side: str, slippage_pct: float, add_tick: bool = True) -> float:
    """technique_backtest.py 의 _slip_price() 와 같은 방향(매수는 불리하게 위로, 매도는
    불리하게 아래로) + 1틱을 더한다(3-N: 지정가가 한 틱 불리해야 실제로 체결될 확률이 높다는
    playbook.oco_levels() 의 원칙과 같다)."""
    tick = tick_size(price)
    if side == "BUY":
        px = price * (1 + slippage_pct) + (tick if add_tick else 0)
        return round_to_tick(px, "up")
    px = price * (1 - slippage_pct) - (tick if add_tick else 0)
    return round_to_tick(px, "down")


def _window_for(cfg, now_dt) -> tuple:
    """(장 진입 가능 여부, open/main/close 창) - session.phase() 를 그대로 쓴다
    (technique_backtest.py 의 _window_for 와 같은 방식, client=None 이라 휴장일 판정은 하지 않는다 -
    연구용 백테스트는 실제 거래일 봉만 넣으므로 휴장일 여부를 API 로 물을 필요가 없다)."""
    info = session_mod.phase(cfg, now=now_dt, client=None)
    return info.get("phase") == "scan", (info.get("window") or "main")


@dataclass
class TradeRecord:
    symbol: str
    name: str
    theme: str
    entry_technique: str
    exit_technique: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    qty: int
    pnl_krw: float
    pnl_pct: float        # 비용 반영 후, 진입 원가 대비 손익률
    pnl_r: float           # pnl_pct / 진입 시점 손절폭(risk.stop_loss_pct) - "R배수"
    hold_minutes: float
    session: str            # 진입 시각 기준 세션 버킷
    mae_pct: float          # 보유 중 최대 역행폭(음수, 진입가 대비)
    mfe_pct: float          # 보유 중 최대 순행폭(양수, 진입가 대비)
    stopped_fast: bool       # 손절류 청산이 진입 5분 이내에 났는가(진입 타이밍 품질 신호)


@dataclass
class BacktestResult:
    trades: List[TradeRecord] = field(default_factory=list)
    days: List[str] = field(default_factory=list)
    symbols_used: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class _RiskGate:
    """연속 손절·일일 손실 한도로 "오늘 더 이상 신규 진입 안 함"을 판정한다.
    engine.py 의 _check_kill_switch() 축소판 - 주간 한도는 뺐다(모듈 docstring 참고)."""

    def __init__(self, cfg, allocation: float):
        self.cfg = cfg
        self.allocation = max(1.0, float(allocation))
        self.consecutive_losses = 0
        self.daily_realized = 0.0
        self.last_loss_at: Optional[datetime] = None
        self.halted_until: Optional[datetime] = None

    def reset_day(self) -> None:
        self.daily_realized = 0.0
        self.halted_until = None
        # ★ consecutive_losses 는 날짜가 바뀐다고 안 푼다 - 실제 엔진도 세션이 바뀌거나
        #   loss_halt_cooldown_hours 가 지나야 푼다(engine._loss_halt_expired 와 같은 원칙).

    def on_close(self, pnl_krw: float, now_dt: datetime) -> None:
        self.daily_realized += pnl_krw
        r = self.cfg.risk
        if pnl_krw < 0:
            self.consecutive_losses += 1
            self.last_loss_at = now_dt
            if self.consecutive_losses >= r.max_consecutive_losses and not r.reduce_after_loss:
                hours = getattr(r, "loss_halt_cooldown_hours", 3.0)
                self.halted_until = now_dt + timedelta(hours=hours)
        else:
            self.consecutive_losses = 0

    def can_enter(self, now_dt: datetime) -> bool:
        r = self.cfg.risk
        if -self.daily_realized / self.allocation >= r.daily_loss_limit_pct:
            return False
        if self.halted_until is not None:
            if now_dt >= self.halted_until:
                self.halted_until = None
                self.consecutive_losses = 0
            else:
                return False
        return True


def _theme_stats(theme: str, arrived_syms: List[str], change_rate: Dict[str, float], theme_of: Dict[str, str]):
    """★ 근사(technique_backtest.py 가 이미 밝힌 한계와 같은 종류) - 실제 엔진은 스크리너가
    "오늘 시장 전체"에서 테마 순위·동반상승 종목 수·평균 등락률을 계산해 candidate 에 실어
    보낸다. 여기서는 우리가 들고 있는 유니버스(이 백테스트에 넣은 종목들) 안에서만 같은
    계산을 한다 - 유니버스가 작을수록 theme_leader 기법의 신호가 실제 스크리너보다
    보수적으로(또는 아예 안) 나올 수 있다."""
    members = [s for s in arrived_syms if theme_of.get(s) == theme and s in change_rate]
    if not members:
        return NAN, NAN, NAN
    ranked = sorted(members, key=lambda s: change_rate[s], reverse=True)
    breadth = float(sum(1 for s in members if change_rate[s] > 0))
    intensity = sum(change_rate[s] for s in members) / len(members)
    return breadth, intensity, ranked


# ── [7-2] daytrader/screener.py 근사(스크리너 게이팅) ───────────────────────
# ★★★ 실제로 발견한 하네스 편향 - run_backtest() 는 원래 이 함수 없이, 로드해 둔
# 유니버스(캐시에 있는 종목 전부, 예: 56종목)를 매 스캔마다 그대로 진입 후보로
# 평가했다. 그런데 실전(daytrader.screener.Screener.build_report())은 하루 중
# 그 시각까지의 등락률·거래대금으로 테마를 채점해 상위 top_themes(기본 2)개
# 테마에서만, 테마당 candidates_per_theme(기본 2)종목만 후보로 올린다 - 등락률
# 3~12%·거래대금 50억↑·가격대 1천~20만원·테마 동반상승 2종목↑ 조건도 다 걸려
# 있다. 그 결과 실전은 하루에 많아야 후보 몇 종목(대개 2~8종목, 재스크리닝마다
# 갱신)만 사고파는데, 게이팅이 없던 하네스는 유니버스 전체(그날 오른 것도 내린
# 것도, 거래대금이 적은 것도 전부)를 대상으로 매매 신호를 평가했다 - "실전과
# 다른 전략"을 채점하고 있었던 셈이다(거래 수는 부풀고, 품질 낮은 진입이 섞여
# 승률·기대값이 실전보다 나쁘게 나온다). 여기서는 실시간 랭킹 API 없이 캐시된
# 분봉만으로 같은 계산을 근사한다 - 등락률은 그날 시가 대비, 거래대금은
# 그날 09:00부터 누적한 (전형가×거래량)의 합(session_stats 의 vwap_num 을
# 그대로 재사용 - 이미 매 틱 누적하고 있어 추가 비용이 없다). 뉴스·유의종목·
# ETF/우선주 필터는 분봉만으로는 알 수 없어 뺀다(README 에 근사임을 밝힌다).
def _score_themes_approx(cfg, symbols, change_rate: Dict[str, float], trading_amount: Dict[str, float],
                          symbol_themes: Dict[str, str]) -> List[tuple]:
    """screener.Screener.score_themes() 의 근사. (점수, 테마명, 동반상승 종목(거래대금순),
    동반상승 종목수, 상승률 중앙값) 리스트를 점수 내림차순으로 돌려준다."""
    scr = cfg.screen
    by_theme: Dict[str, List[str]] = {}
    for sym in symbols:
        theme = symbol_themes.get(sym, "")
        if not theme:
            continue
        by_theme.setdefault(theme, []).append(sym)

    views = []
    for theme, members in by_theme.items():
        up = [
            m for m in members
            if change_rate.get(m, NAN) == change_rate.get(m, NAN)  # NaN 아님
            and change_rate[m] >= scr.min_change_rate
        ]
        if len(up) < scr.min_theme_members_up:
            continue
        rates = [change_rate[m] for m in up]
        amount = sum(trading_amount.get(m, 0.0) for m in up)
        intensity = statistics.median(rates)
        liquidity = max(math.log10(max(amount, 1e8) / 1e8), 0.1)
        score = len(up) * intensity * liquidity
        ranked = sorted(up, key=lambda m: trading_amount.get(m, 0.0), reverse=True)
        views.append((score, theme, ranked, len(up), intensity))

    views.sort(key=lambda v: v[0], reverse=True)
    return views


def _pick_candidates_approx(
    cfg, symbols, change_rate: Dict[str, float], trading_amount: Dict[str, float],
    last_price: Dict[str, float], symbol_themes: Dict[str, str],
) -> Tuple[List[str], Dict[str, tuple]]:
    """screener.Screener.build_report() 의 후보 선정 근사. (후보 목록, {종목: (테마, 테마순위,
    테마내순위, 테마내동반상승수, 테마강도)}) 를 돌려준다 - 뒤 값은 ThemeLeaderEntry 의 ctx
    필드(theme_rank/theme_breadth/theme_intensity)를 실제 스크리너와 같은 정의로 채우는 데 쓴다."""
    scr = cfg.screen
    views = _score_themes_approx(cfg, symbols, change_rate, trading_amount, symbol_themes)
    candidates: List[str] = []
    meta: Dict[str, tuple] = {}
    for theme_rank, (score, theme, ranked, breadth, intensity) in enumerate(views[: scr.top_themes], start=1):
        count = 0
        for rank_in_theme, sym in enumerate(ranked, start=1):
            if count >= scr.candidates_per_theme:
                break
            price = last_price.get(sym, NAN)
            cr = change_rate.get(sym, NAN)
            amt = trading_amount.get(sym, 0.0)
            if price != price or not (scr.min_price <= price <= scr.max_price):
                continue
            if cr != cr or cr > scr.max_change_rate:
                continue  # min_change_rate 는 이미 up 선정에서 걸렀다(_score_themes_approx)
            if amt < scr.min_trading_amount:
                continue
            candidates.append(sym)
            meta[sym] = (theme, float(theme_rank), float(rank_in_theme), float(breadth), float(intensity))
            count += 1
    return candidates, meta


def run_backtest(
    cfg,
    bars_by_symbol: Dict[str, List[Bar]],
    *,
    symbol_names: Optional[Dict[str, str]] = None,
    symbol_themes: Optional[Dict[str, str]] = None,
    slippage_pct: Optional[float] = None,
    entry_extension_filter: str = "none",
    session_technique_filter: Optional[Dict[str, set]] = None,
    session_buckets=DEFAULT_SESSION_BUCKETS,
    scale_out: Optional[bool] = None,
    min_warmup: int = 30,
    risk_gate: bool = True,
    screener_gate: bool = True,
) -> BacktestResult:
    """여러 종목의 1분봉(날짜가 섞여 있어도 된다)을 시간순으로 재생하며 진짜 Playbook 으로
    사고 판다.

    entry_extension_filter: "none" | "vwap_1.0atr" | "vwap_1.5atr" - breakout·bull_flag·orb
      (추격 성격이 강한 기법)에 한해, 신호가 나도 "종가가 세션 VWAP 위로 이미 ATR 의 k배
      넘게 떨어져 있으면" 진입을 보류한다("매수 진입시기를 신중하게" 요청의 구현). 다른
      기법은 원래도 VWAP 되돌림·재탈환 성격이라 이 필터를 적용하지 않는다.
    session_technique_filter: {"09:00~09:30": {"breakout", ...}, ...} - 세션 버킷별로
      허용할 진입 기법 키 집합. 안 주면 전부 허용(baseline).
    scale_out: None 이면 cfg.sizing.scale_out 을 그대로 쓴다. True/False 로 강제하면 그
      값으로 ctx.scale_out 을 덮어써 FixedExit 의 전량 익절 on/off 를 실험할 수 있다
      (playbook.FixedExit.evaluate 참고 - scale_out=True 면 tp=NaN 이 되어 전량 익절이 꺼진다).
    screener_gate: [7-2] True(기본) 면 daytrader/screener.py 의 테마·등락률·거래대금 게이팅을
      캐시된 분봉으로 근사해(_pick_candidates_approx), 그 순간 스크리너가 실제로 뽑았을
      후보 종목에만 진입 신호를 평가한다(cfg.entry.rescreen_minutes 마다 재선정 - 실전과
      같은 주기). False 로 끄면 예전처럼 유니버스 전체를 매 틱 평가한다("게이팅 없는
      유니버스"를 일부러 보고 싶을 때만 쓴다 - README 의 편향 설명 참고). 이미 보유 중인
      포지션의 청산 판정은 게이팅과 무관하게 항상 계속한다(청산은 스크리너를 안 거친다).
    """
    r = cfg.risk
    if slippage_pct is None:
        slippage_pct = r.max_slippage_pct / 2.0
    if scale_out is None:
        scale_out = bool(cfg.sizing.scale_out)

    symbol_names = symbol_names or {}
    symbol_themes = symbol_themes or {}
    playbook = Playbook(cfg, market="domestic", learning_mode="none")  # 근거: backtest.py 모듈독스트링

    # 날짜별로 쪼갠다.
    by_day: Dict[str, Dict[str, List[Bar]]] = {}
    for sym, bars in bars_by_symbol.items():
        for b in bars:
            d = (b.ts or "")[:10]
            by_day.setdefault(d, {}).setdefault(sym, []).append(b)
    for d in by_day:
        for sym in by_day[d]:
            by_day[d][sym].sort(key=lambda b: b.ts)

    days = sorted(by_day.keys())
    result = BacktestResult(days=days, symbols_used=sorted(bars_by_symbol.keys()))

    # 전일 고가·저가(volatility_breakout 용) - 실전 엔진은 일봉 API 를 따로 불러 얻지만(3-3),
    # 여기서는 이미 들고 있는 연속된 날짜의 분봉에서 그대로 계산한다(매 종목·매 날짜 1회).
    prev_range_by_symbol_day: Dict[str, Dict[str, tuple]] = {}
    for sym, bars in bars_by_symbol.items():
        by_d: Dict[str, list] = {}
        for b in bars:
            by_d.setdefault((b.ts or "")[:10], []).append(b)
        prev_hl = (NAN, NAN)
        per_day: Dict[str, tuple] = {}
        for d in sorted(by_d.keys()):
            per_day[d] = prev_hl
            highs = [x.high for x in by_d[d] if x.high == x.high]
            lows = [x.low for x in by_d[d] if x.low == x.low]
            if highs and lows:
                prev_hl = (max(highs), min(lows))
        prev_range_by_symbol_day[sym] = per_day

    positions: Dict[str, Position] = {}
    cooldown: Dict[str, datetime] = {}
    last_verdicts: dict = {}
    orb_done: dict = {}
    gate = _RiskGate(cfg, cfg.capital.allocation) if risk_gate else None
    position_cap = sizing.position_cap(cfg.capital.allocation, cfg.capital.max_positions)

    for day in days:
        day_bars = by_day[day]
        prev_day_range = {s: prev_range_by_symbol_day.get(s, {}).get(day, (NAN, NAN)) for s in day_bars}
        if gate:
            gate.reset_day()
        trades_today = 0
        session_stats: Dict[str, dict] = {s: {"vwap_num": 0.0, "vwap_den": 0.0, "high": float("-inf")} for s in day_bars}
        day_open: Dict[str, float] = {}
        hist: Dict[str, List[Bar]] = {s: [] for s in day_bars}
        ptr: Dict[str, int] = {s: 0 for s in day_bars}

        timeline = sorted({b.ts for bars in day_bars.values() for b in bars})
        force_close_dt = None
        # [7-2] 스크리너 게이팅 상태(하루 단위로 리셋) - candidates=None 이면 아직 그 날
        # 첫 재선정 전(스캔 시작 전)이다.
        candidates: Optional[set] = None
        candidate_meta: Dict[str, tuple] = {}
        next_rescreen_dt: Optional[datetime] = None

        for t in timeline:
            arrived = []
            for sym, blist in day_bars.items():
                i = ptr[sym]
                if i < len(blist) and blist[i].ts == t:
                    bar = blist[i]
                    hist[sym].append(bar)
                    ptr[sym] += 1
                    arrived.append(sym)
                    day_open.setdefault(sym, bar.open if bar.open == bar.open else bar.close)
                    st = session_stats[sym]
                    if not (math.isnan(bar.high) or math.isnan(bar.low) or math.isnan(bar.close) or math.isnan(bar.volume)):
                        typical = (bar.high + bar.low + bar.close) / 3
                        st["vwap_num"] += typical * bar.volume
                        st["vwap_den"] += bar.volume
                        if bar.high > st["high"]:
                            st["high"] = bar.high
            if not arrived:
                continue

            now_dt = parse_dt(t)
            if force_close_dt is None:
                force_close_dt = combine(now_dt.date(), cfg.exit.force_close_time)
            force_close = now_dt >= force_close_dt

            # ━━ 청산 ━━
            for sym in [s for s in list(positions.keys()) if s in arrived]:
                pos = positions[sym]
                bar = hist[sym][-1]
                pos.peak_price = max(pos.peak_price, bar.high)
                st = session_stats[sym]
                sess_vwap = st["vwap_num"] / st["vwap_den"] if st["vwap_den"] else NAN
                sess_high = st["high"] if st["high"] != float("-inf") else NAN
                ctx = SimpleNamespace(
                    held_minutes=pos.held_minutes(now_dt), force_close=force_close, now=now_dt,
                    prev_verdict=last_verdicts.get(sym), cfg=cfg, scale_out=scale_out,
                    session_vwap=sess_vwap, session_high=sess_high,
                )
                verdict, fill_price = _check_exit(playbook, pos, hist[sym], bar, ctx, cfg, slippage_pct)
                if verdict is None:
                    continue
                last_verdicts[sym] = verdict
                pnl = trade_pnl(pos.entry_price, fill_price, pos.quantity, cfg.costs.commission_pct, cfg.costs.tax_pct)
                held = pos.held_minutes(now_dt)
                cost_basis = pos.entry_price * pos.quantity * (1 + cfg.costs.commission_pct)
                pnl_pct = pnl / cost_basis if cost_basis else 0.0
                risk_pct = r.stop_loss_pct or 0.01
                stop_like = verdict.technique in ("fixed", "atr_stop", "trailing") and _exit_is_loss(verdict)
                result.trades.append(TradeRecord(
                    symbol=sym, name=symbol_names.get(sym, sym), theme=symbol_themes.get(sym, ""),
                    entry_technique=pos.technique, exit_technique=verdict.technique,
                    entry_time=iso(pos.entry_time), entry_price=pos.entry_price,
                    exit_time=iso(now_dt), exit_price=fill_price, qty=pos.quantity,
                    pnl_krw=pnl, pnl_pct=pnl_pct, pnl_r=pnl_pct / risk_pct if risk_pct else 0.0,
                    hold_minutes=held, session=session_bucket(iso(pos.entry_time)[11:16], session_buckets),
                    mae_pct=getattr(pos, "_mae_pct", 0.0), mfe_pct=getattr(pos, "_mfe_pct", 0.0),
                    stopped_fast=bool(stop_like and held <= 5.0),
                ))
                trades_today += 1
                positions.pop(sym, None)
                cooldown[sym] = now_dt + timedelta(minutes=r.cooldown_minutes)
                if gate:
                    gate.on_close(pnl, now_dt)
                continue

            # MAE/MFE 갱신(살아있는 포지션)
            for sym in [s for s in positions if s in arrived]:
                pos = positions[sym]
                bar = hist[sym][-1]
                adverse = (bar.low - pos.entry_price) / pos.entry_price
                favorable = (bar.high - pos.entry_price) / pos.entry_price
                pos._mae_pct = min(getattr(pos, "_mae_pct", 0.0), adverse)
                pos._mfe_pct = max(getattr(pos, "_mfe_pct", 0.0), favorable)

            if force_close:
                continue  # 마감 강제청산 시간대엔 신규 진입 없음(engine.session.phase 와 동일)

            can_scan, window = _window_for(cfg, now_dt)
            if not can_scan:
                continue

            # [7-2] 스크리너 재선정 - cfg.entry.rescreen_minutes 마다(그 날 첫 스캔 틱 포함)
            # 그 시각까지 누적된 등락률·거래대금으로 후보를 다시 뽑는다(screener.py 근사).
            if screener_gate and (next_rescreen_dt is None or now_dt >= next_rescreen_dt):
                last_price_all = {s: bl[-1].close for s, bl in hist.items() if bl}
                change_rate_all = {
                    s: (last_price_all[s] - day_open[s]) / day_open[s]
                    for s in last_price_all if day_open.get(s)
                }
                trading_amount_all = {s: session_stats[s]["vwap_num"] for s in last_price_all}
                cand_list, cand_meta = _pick_candidates_approx(
                    cfg, list(hist.keys()), change_rate_all, trading_amount_all, last_price_all, symbol_themes,
                )
                candidates = set(cand_list)
                candidate_meta = cand_meta
                next_rescreen_dt = now_dt + timedelta(minutes=max(1, cfg.entry.rescreen_minutes))

            # ━━ 진입 ━━
            budget = r.daily_max_trades - trades_today - len(positions)
            if budget <= 0:
                continue
            if gate and not gate.can_enter(now_dt):
                continue

            change_rate = {
                s: (hist[s][-1].close - day_open[s]) / day_open[s]
                for s in arrived if s in day_open and day_open[s]
            }
            bucket = session_bucket(t[11:16], session_buckets)
            allowed_techs = None
            if session_technique_filter is not None:
                allowed_techs = session_technique_filter.get(bucket)

            scored = []
            for sym in arrived:
                if sym in positions:
                    continue
                if screener_gate and (candidates is None or sym not in candidates):
                    continue  # [7-2] 그 순간 스크리너 후보가 아니면 애초에 신호를 안 본다(실전과 동일)
                cd = cooldown.get(sym)
                if cd is not None and cd > now_dt:
                    continue
                bars = hist[sym]
                if len(bars) < min_warmup:
                    continue
                theme = symbol_themes.get(sym, "")
                meta = candidate_meta.get(sym) if screener_gate else None
                if meta is not None:
                    # [7-2] 실제 스크리너와 같은 정의(등락률 3%↑ 동반상승·거래대금순 랭킹·동반상승
                    # 구간의 중앙값)로 채운다 - 예전 _theme_stats() 는 등락률 0%↑를 "동반상승"으로,
                    # 평균(중앙값 아님)을 강도로, 등락률순(거래대금순 아님)을 대장주 순위로 써서
                    # ThemeLeaderEntry 의 판정 문턱이 실제 스크리너보다 낮게 잡혀 있었다(감사 항목 1).
                    _, rank, rank_in_theme, breadth, intensity = meta
                    theme_bars = None
                    for other_sym, other_meta in candidate_meta.items():
                        if other_sym != sym and other_meta[0] == meta[0]:
                            other_bars = hist.get(other_sym)
                            if other_bars:
                                theme_bars = other_bars
                                break
                else:
                    breadth, intensity, ranked = _theme_stats(theme, arrived, change_rate, symbol_themes)
                    rank = float(ranked.index(sym) + 1) if isinstance(ranked, list) and sym in ranked else NAN
                    theme_bars = None
                    if isinstance(ranked, list):
                        for other in ranked:
                            if other != sym:
                                theme_bars = hist.get(other)
                                break
                st = session_stats[sym]
                sess_vwap = st["vwap_num"] / st["vwap_den"] if st["vwap_den"] else NAN
                sess_high = st["high"] if st["high"] != float("-inf") else NAN
                ctx = SimpleNamespace(
                    symbol=sym, name=symbol_names.get(sym, sym), theme=theme,
                    upper_limit=None, theme_bars=theme_bars, now=now_dt,
                    prev_verdict=last_verdicts.get(sym), prev_verdicts=[],
                    change_rate=change_rate.get(sym, NAN), theme_rank=rank,
                    theme_breadth=breadth, theme_intensity=intensity,
                    orb_done=orb_done.get(sym, False),
                    minutes_to_close=(force_close_dt - now_dt).total_seconds() / 60.0,
                    window=window, kr_session=True,
                    session_vwap=sess_vwap, session_high=sess_high,
                )
                if "volatility_breakout" in cfg.strategy.entry_order:
                    ctx.prev_day_high, ctx.prev_day_low = prev_day_range.get(sym, (NAN, NAN))

                winner, all_verdicts = playbook.evaluate_entry(bars, ctx)
                if winner is None:
                    continue
                if allowed_techs is not None and winner.technique not in allowed_techs:
                    continue
                if not _extension_ok(bars, ctx, entry_extension_filter, winner.technique):
                    continue
                score = getattr(winner, "selection_score", None)
                if score is None:
                    score = winner.score or 0.0
                scored.append((score, sym, winner))

            scored.sort(key=lambda x: x[0], reverse=True)
            for score, sym, winner in scored:
                if budget <= 0 or len(positions) >= cfg.capital.max_positions:
                    break
                idx = ptr[sym]
                if idx >= len(day_bars[sym]):
                    continue  # 오늘 마지막 봉 - 체결을 확인할 다음 봉이 없다
                fill_bar = day_bars[sym][idx]
                open_px = fill_bar.open if fill_bar.open == fill_bar.open else fill_bar.close
                fill_price = _slip_price(open_px, "BUY", slippage_pct)
                if fill_price <= 0:
                    continue
                strength = sizing.signal_strength(winner)
                amount = sizing.entry_amount(position_cap, strength, cfg.sizing, conviction=None, vol=None)
                qty = int(amount // fill_price)
                if qty < 1 or qty * fill_price < r.min_order_amount:
                    continue
                pos = Position(
                    symbol=sym, name=symbol_names.get(sym, sym), theme=symbol_themes.get(sym, ""),
                    quantity=qty, entry_price=fill_price, entry_time=parse_dt(fill_bar.ts),
                    peak_price=fill_price, oco_id=None, entry_volume=fill_bar.volume,
                    verdict_id=winner.id, why="", technique=winner.technique,
                    last_fill_price=fill_price, invested=fill_price * qty, conviction=0.5,
                )
                pos._mae_pct = 0.0
                pos._mfe_pct = 0.0
                positions[sym] = pos
                last_verdicts[sym] = winner
                if winner.technique == "orb":
                    orb_done[sym] = True
                budget -= 1

        # 그 날 남은 포지션은 강제 청산(마지막 봉 종가) - 타임라인이 끝났는데 아직 열려 있다면
        # 데이터가 force_close_time 뒤 봉을 안 줬다는 뜻이다(야후 15:00 이후 결측 등).
        for sym in list(positions.keys()):
            pos = positions[sym]
            bars = day_bars.get(sym) or []
            if not bars:
                continue
            last_bar = bars[-1]
            fill_price = _slip_price(last_bar.close, "SELL", slippage_pct, add_tick=False)
            pnl = trade_pnl(pos.entry_price, fill_price, pos.quantity, cfg.costs.commission_pct, cfg.costs.tax_pct)
            now_dt = parse_dt(last_bar.ts)
            cost_basis = pos.entry_price * pos.quantity * (1 + cfg.costs.commission_pct)
            risk_pct = r.stop_loss_pct or 0.01
            held = pos.held_minutes(now_dt)
            result.trades.append(TradeRecord(
                symbol=sym, name=symbol_names.get(sym, sym), theme=symbol_themes.get(sym, ""),
                entry_technique=pos.technique, exit_technique="force_close",
                entry_time=iso(pos.entry_time), entry_price=pos.entry_price,
                exit_time=iso(now_dt), exit_price=fill_price, qty=pos.quantity,
                pnl_krw=pnl, pnl_pct=pnl / cost_basis if cost_basis else 0.0,
                pnl_r=(pnl / cost_basis if cost_basis else 0.0) / risk_pct,
                hold_minutes=held, session=session_bucket(iso(pos.entry_time)[11:16], session_buckets),
                mae_pct=getattr(pos, "_mae_pct", 0.0), mfe_pct=getattr(pos, "_mfe_pct", 0.0),
                stopped_fast=False,
            ))
            if gate:
                gate.on_close(pnl, now_dt)
            positions.pop(sym, None)
            cooldown[sym] = now_dt + timedelta(minutes=r.cooldown_minutes)

    if len(days) < 5:
        result.notes.append(f"거래일이 {len(days)}일뿐입니다 - 표본이 매우 작습니다.")
    return result


def _exit_is_loss(verdict) -> bool:
    t = verdict.term("stop_loss") if hasattr(verdict, "term") else None
    if t is not None:
        return bool(t.passed)
    return verdict.technique in ("atr_stop", "trailing")


def _extension_ok(bars: List[Bar], ctx, mode: str, technique: str) -> bool:
    """"매수 진입시기를 신중하게" - 추격형 기법(breakout·bull_flag·orb·volume_dry_pop)에 한해,
    이미 VWAP 위로 너무 멀리 간 자리는 거른다. 데이터가 부족하면(ATR 계산 불가) 막지 않는다 -
    필터가 데이터 부족을 이유로 매매를 아예 못 하게 만들면 그건 필터가 아니라 다른 버그다."""
    if mode in (None, "none") or technique not in ("breakout", "bull_flag", "orb", "volume_dry_pop"):
        return True
    k = {"vwap_1.0atr": 1.0, "vwap_1.5atr": 1.5}.get(mode)
    if k is None:
        return True
    a = atr_of(bars, 14)
    v = getattr(ctx, "session_vwap", NAN)
    close = bars[-1].close
    if math.isnan(a) or math.isnan(v) or math.isnan(close):
        return True
    return (close - v) <= k * a


def _check_exit(playbook: Playbook, pos: Position, bars: List[Bar], bar: Bar, ctx, cfg, slippage_pct: float):
    """봉 하나 안에서 저가(최악)부터 확인하고, 아무것도 안 걸리면 고가(최선)를 확인한다
    (모듈 독스트링의 "둘 다 닿으면 손절 먼저" 원칙). 반환값: (Verdict|None, 체결가|None)."""
    verdict = playbook.evaluate_exit(pos, bars, bar.low, ctx)
    if verdict is not None and verdict.ok:
        return verdict, _exit_fill_price(pos, verdict, bar, cfg, slippage_pct)
    verdict = playbook.evaluate_exit(pos, bars, bar.high, ctx)
    if verdict is not None and verdict.ok:
        return verdict, _exit_fill_price(pos, verdict, bar, cfg, slippage_pct)
    return None, None


def _exit_fill_price(pos: Position, verdict, bar: Bar, cfg, slippage_pct: float) -> float:
    r = cfg.risk
    tech = verdict.technique
    if tech == "fixed":
        sl = verdict.term("stop_loss")
        raw = pos.entry_price * (1 - r.stop_loss_pct) if sl and sl.passed else pos.entry_price * (1 + r.take_profit_pct)
    elif tech == "atr_stop":
        raw = verdict.inputs.get("line")
        if raw is None or raw != raw:
            raw = bar.close
    elif tech == "trailing":
        raw = pos.peak_price * (1 - r.trailing_stop_pct)
    else:  # force_close, time_stop, momentum_fade - 가격이 아니라 상태로 발동하는 시장가 청산
        raw = bar.close
    raw = min(max(raw, bar.low), bar.high)
    return _slip_price(raw, "SELL", slippage_pct)
