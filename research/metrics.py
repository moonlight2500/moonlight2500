"""거래 기록(backtest.TradeRecord) 을 기법×세션 단위로 묶어 통계를 낸다.

전부 순수 함수다(부작용 없음) - grid.py·run.py 가 이 함수들을 반복해서 부른다.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from research.backtest import TradeRecord


@dataclass
class CellStats:
    """기법 하나 × 세션 하나(또는 "전체")의 성적표."""

    technique: str
    session: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    expectancy_pct: float = 0.0     # 거래당 평균 손익률(비용 반영 후)
    expectancy_r: float = 0.0       # 거래당 평균 R 배수
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0   # 이 셀만의 손익률 누적곡선 기준
    avg_hold_minutes: float = 0.0
    pct_stopped_within_5min: float = 0.0
    mae_p50: float = 0.0
    mae_p90: float = 0.0
    mfe_p50: float = 0.0
    mfe_p90: float = 0.0
    ci90_low: float = 0.0
    ci90_high: float = 0.0

    def to_row(self) -> Dict:
        return {
            "기법": self.technique, "세션": self.session, "거래수": self.trades,
            "승률": self.win_rate, "평균이익%": self.avg_win_pct, "평균손실%": self.avg_loss_pct,
            "기대값%": self.expectancy_pct, "기대값R": self.expectancy_r,
            "손익비": self.profit_factor, "MDD%": self.max_drawdown_pct,
            "평균보유분": self.avg_hold_minutes, "5분내손절%": self.pct_stopped_within_5min,
            "기대값90%CI": (self.ci90_low, self.ci90_high),
        }


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _max_drawdown(pnl_pcts: List[float]) -> float:
    """누적 손익률(단순 합산) 곡선의 최대 낙폭. 거래 순서(시간순)를 유지해서 넘겨야 한다."""
    peak = 0.0
    cum = 0.0
    mdd = 0.0
    for p in pnl_pcts:
        cum += p
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return mdd


def bootstrap_ci(values: List[float], iters: int = 2000, alpha: float = 0.10, seed: int = 42) -> Tuple[float, float]:
    """평균의 90% 부트스트랩 신뢰구간(복원추출). 표본이 2건 미만이면 (0,0)."""
    if len(values) < 2:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iters):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = _percentile(means, alpha / 2)
    hi = _percentile(means, 1 - alpha / 2)
    return (lo, hi)


def _stats_for(trades: List[TradeRecord], technique: str, session: str) -> CellStats:
    if not trades:
        return CellStats(technique=technique, session=session)
    trades = sorted(trades, key=lambda t: t.entry_time)
    pnl_pcts = [t.pnl_pct for t in trades]
    pnl_rs = [t.pnl_r for t in trades]
    wins = [p for p in pnl_pcts if p > 0]
    losses = [p for p in pnl_pcts if p <= 0]
    gains = sum(wins)
    loss_sum = -sum(losses)
    pf = (gains / loss_sum) if loss_sum > 0 else (float("inf") if gains > 0 else 0.0)
    mae = [t.mae_pct for t in trades]
    mfe = [t.mfe_pct for t in trades]
    ci_lo, ci_hi = bootstrap_ci(pnl_pcts)
    return CellStats(
        technique=technique, session=session, trades=len(trades),
        wins=len(wins), losses=len(losses), win_rate=len(wins) / len(trades),
        avg_win_pct=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss_pct=(sum(losses) / len(losses)) if losses else 0.0,
        expectancy_pct=sum(pnl_pcts) / len(pnl_pcts),
        expectancy_r=sum(pnl_rs) / len(pnl_rs),
        profit_factor=pf if pf != float("inf") else 99.99,
        max_drawdown_pct=_max_drawdown(pnl_pcts),
        avg_hold_minutes=sum(t.hold_minutes for t in trades) / len(trades),
        pct_stopped_within_5min=sum(1 for t in trades if t.stopped_fast) / len(trades),
        mae_p50=_percentile(mae, 0.5), mae_p90=_percentile(mae, 0.9),
        mfe_p50=_percentile(mfe, 0.5), mfe_p90=_percentile(mfe, 0.9),
        ci90_low=ci_lo, ci90_high=ci_hi,
    )


def by_technique_session(trades: List[TradeRecord]) -> List[CellStats]:
    """기법×세션 조합마다 하나씩. "전체" 세션 행도 기법마다 하나 더 넣는다."""
    groups: Dict[Tuple[str, str], List[TradeRecord]] = {}
    tech_all: Dict[str, List[TradeRecord]] = {}
    for t in trades:
        groups.setdefault((t.entry_technique, t.session), []).append(t)
        tech_all.setdefault(t.entry_technique, []).append(t)
    out = [_stats_for(rows, tech, sess) for (tech, sess), rows in groups.items()]
    out += [_stats_for(rows, tech, "전체") for tech, rows in tech_all.items()]
    out.sort(key=lambda c: (c.technique, c.session))
    return out


def overall(trades: List[TradeRecord]) -> CellStats:
    return _stats_for(trades, "전체", "전체")


def by_session(trades: List[TradeRecord]) -> List[CellStats]:
    groups: Dict[str, List[TradeRecord]] = {}
    for t in trades:
        groups.setdefault(t.session, []).append(t)
    out = [_stats_for(rows, "전체", sess) for sess, rows in groups.items()]
    out.sort(key=lambda c: c.session)
    return out
