"""워크포워드(walk-forward) 파라미터 스윕.

거래일을 시간순으로 나눠(기본 앞 70%=in-sample, 뒤 30%=out-of-sample) in-sample 에서
"기대값(비용 반영 후 손익률)이 가장 높으면서, 표본이 30건 이상이고, 이웃 설정들과도
비슷한(과최적화가 아닌) 설정"을 고른 뒤, out-of-sample 에서 기준(baseline)과 비교한다.

★★★ 다중검정 경고 - 가설을 여러 개, 조합을 여러 개 시험할수록 "우연히 좋아 보이는" 조합이
나올 확률이 커진다. 여기서 "OOS 에서도 기준을 이겼다"로 표시된 것만 신뢰하고, 그마저도
"한 달치 한 가지 장세"에서 나온 결과라는 것(README·report.md 의 경고)을 반드시 함께 읽어야
한다 - 이 모듈은 종목·구간을 늘려 재검증하기 전까지 결론이 아니라 가설을 만드는 도구다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Dict, List, Optional

from daytrader.playbook import Bar
from daytrader.timeutil import parse_hhmm
from research import metrics
from research.backtest import BacktestResult, run_backtest

MAX_COMBINATIONS = 500  # "총 조합 수는 500 미만으로" 요청 - 넘으면 build_default_hypotheses() 가 예외를 던진다.


def split_walk_forward(days: List[str], in_sample_frac: float = 0.7) -> tuple:
    days = sorted(set(days))
    n = len(days)
    if n <= 1:
        return days, []
    k = max(1, min(n - 1, round(n * in_sample_frac)))
    return days[:k], days[k:]


def _filter_days(bars_by_symbol: Dict[str, List[Bar]], day_set: set) -> Dict[str, List[Bar]]:
    out: Dict[str, List[Bar]] = {}
    for sym, bars in bars_by_symbol.items():
        sel = [b for b in bars if (b.ts or "")[:10] in day_set]
        if sel:
            out[sym] = sel
    return out


def _set_path(cfg, path: str, value) -> None:
    parts = path.split(".")
    obj = cfg
    for p in parts[:-1]:
        obj = obj.setdefault(p, {}) if isinstance(obj, dict) else getattr(obj, p)
    last = parts[-1]
    if isinstance(obj, dict):
        obj[last] = value
        return
    cur = getattr(obj, last, None)
    if isinstance(cur, dtime) and isinstance(value, str):
        value = parse_hhmm(value)
    setattr(obj, last, value)


def apply_overrides(cfg, overrides: Dict[str, object]):
    """cfg 를 깊은 복사해 dotted-path 오버라이드(예: "entry.volume_surge_ratio")를 적용한다.
    원본 cfg 는 절대 건드리지 않는다 - 그리드의 변형(variant)들이 서로 영향을 주면 안 된다."""
    new_cfg = copy.deepcopy(cfg)
    for path, value in (overrides or {}).items():
        _set_path(new_cfg, path, value)
    return new_cfg


@dataclass
class Variant:
    label: str
    overrides: Dict[str, object] = field(default_factory=dict)
    run_kwargs: Dict[str, object] = field(default_factory=dict)
    technique_filter: Optional[set] = None  # None=전체 기법 채점, 있으면 그 기법 거래만 채점


@dataclass
class Hypothesis:
    name: str
    description: str
    variants: List[Variant]  # variants[0] = 기준(baseline)


def run_variant(cfg_base, variant: Variant, bars_by_symbol, names, themes) -> List:
    cfg = apply_overrides(cfg_base, variant.overrides)
    res: BacktestResult = run_backtest(cfg, bars_by_symbol, symbol_names=names, symbol_themes=themes, **variant.run_kwargs)
    trades = res.trades
    if variant.technique_filter:
        trades = [t for t in trades if t.entry_technique in variant.technique_filter]
    return trades


def evaluate_hypothesis(
    cfg_base, hyp: Hypothesis, bars_by_symbol, names, themes, in_days, oos_days, min_trades: int = 30,
) -> dict:
    is_bars = _filter_days(bars_by_symbol, set(in_days))
    oos_bars = _filter_days(bars_by_symbol, set(oos_days))

    is_results = []
    for v in hyp.variants:
        trades = run_variant(cfg_base, v, is_bars, names, themes)
        is_results.append((v, metrics.overall(trades)))

    eligible = [(v, s) for v, s in is_results if s.trades >= min_trades]
    pool = eligible if eligible else is_results
    best_v, best_s = max(pool, key=lambda vs: vs[1].expectancy_pct)
    low_confidence = not eligible

    def _close(a: float, b: float) -> bool:
        if b == 0:
            return abs(a - b) < 1e-6
        return abs(a - b) <= abs(b) * 0.2

    neighbors = [
        v.label for v, s in pool
        if v.label != best_v.label and s.trades > 0 and _close(s.expectancy_pct, best_s.expectancy_pct)
    ]

    baseline_v = hyp.variants[0]
    base_oos_trades = run_variant(cfg_base, baseline_v, oos_bars, names, themes)
    chosen_oos_trades = (
        base_oos_trades if best_v.label == baseline_v.label
        else run_variant(cfg_base, best_v, oos_bars, names, themes)
    )
    base_oos = metrics.overall(base_oos_trades)
    chosen_oos = metrics.overall(chosen_oos_trades)
    beats_baseline_oos = bool(
        chosen_oos.trades > 0 and best_v.label != baseline_v.label
        and chosen_oos.expectancy_pct > base_oos.expectancy_pct
    )

    return {
        "hypothesis": hyp.name, "description": hyp.description,
        "in_sample": is_results, "chosen": best_v, "chosen_is_stats": best_s,
        "low_confidence": low_confidence, "robust": bool(neighbors), "neighbors": neighbors,
        "baseline": baseline_v, "baseline_oos": base_oos, "chosen_oos": chosen_oos,
        "beats_baseline_oos": beats_baseline_oos,
    }


# ── 실제로 시험할 가설들(코디네이터 지정 순서) ───────────────────────────────

_BREAKOUT_LIKE = {"breakout", "bull_flag", "orb", "volume_dry_pop"}


def _breakout_lookback_atr_hypothesis(cfg) -> Hypothesis:
    """variants[0] 이 반드시 "현재 config.yaml 값"이 되도록 기준을 먼저 만들고, 나머지
    조합에서 기준과 겹치는 것만 뺀다(evaluate_hypothesis 는 variants[0]을 기준으로 삼는다).

    ★ 4×3 전체 교차곱(12개)이 아니라 "한 번에 하나씩 바꾸는"(one-factor-at-a-time) 6개로
    줄였다 - 실측 결과, 이 조합의 백테스트 비용은 대부분 룩백이 클수록 늘어나는 지표
    재계산 비용이라 12개를 다 돌리면 유니버스가 클 때 특히 느리다. 두 값을 동시에 바꾼
    상호작용까지 보려면 report.md 의 결과를 보고 필요할 때만 --skip-grid 없이 범위를
    좁혀 따로 돌리면 된다."""
    base_lb = cfg.entry.breakout_lookback
    base_am = cfg.strategy.p("atr_stop").get("atr_multiple", 3.0)
    variants = [Variant(
        f"lookback={base_lb}, atr_mult={base_am:g}(기준)",
        {"entry.breakout_lookback": base_lb, "strategy.params.atr_stop.atr_multiple": base_am}, {},
    )]
    for lb in (10, 15, 20, 30):
        if lb == base_lb:
            continue
        variants.append(Variant(
            f"lookback={lb}, atr_mult={base_am:g}(기준)",
            {"entry.breakout_lookback": lb, "strategy.params.atr_stop.atr_multiple": base_am}, {},
        ))
    for am in (2.5, 3.5, 4.5):
        if am == base_am:
            continue
        variants.append(Variant(
            f"lookback={base_lb}(기준), atr_mult={am:g}",
            {"entry.breakout_lookback": base_lb, "strategy.params.atr_stop.atr_multiple": am}, {},
        ))
    return Hypothesis(
        "g_breakout_lookback_atr_multiple",
        "돌파 룩백 봉수(10/15/20/30) · ATR 손절 배수(2.5/3.5/4.5) - 한 번에 하나씩",
        variants,
    )


def build_default_hypotheses(cfg) -> List[Hypothesis]:
    entry_order = set(cfg.strategy.entry_order)
    breakout_in_use = entry_order & _BREAKOUT_LIKE

    hyps = [
        Hypothesis(
            "a_extension_filter",
            "돌파형 기법(breakout/bull_flag/orb/volume_dry_pop)에 VWAP+ATR 이격 상한을 걸면 "
            "추격 매수가 줄어 진입 타이밍이 나아지는가",
            [
                Variant("필터 없음(기준)", {}, {"entry_extension_filter": "none"}, technique_filter=breakout_in_use or None),
                Variant("VWAP+1.5ATR 이내만", {}, {"entry_extension_filter": "vwap_1.5atr"}, technique_filter=breakout_in_use or None),
                Variant("VWAP+1.0ATR 이내만", {}, {"entry_extension_filter": "vwap_1.0atr"}, technique_filter=breakout_in_use or None),
            ],
        ),
        Hypothesis(
            "b_disable_midday_breakout",
            "11:00~13:00 점심 눌림 구간에서 돌파형 기법을 끄면 성적이 나아지는가",
            [
                Variant("제한 없음(기준)", {}, {"session_technique_filter": None}),
                Variant("11~13시 돌파형 제외", {}, {
                    "session_technique_filter": {"11:00~13:00": entry_order - _BREAKOUT_LIKE},
                }),
            ],
        ),
        Hypothesis(
            "c_trailing_and_scaleout",
            "트레일링 발동폭/추적폭과 분할매도(scale_out) 켬/끔이 기대값에 미치는 영향",
            [
                Variant("기준(설정값 그대로)", {}, {"scale_out": False}),
                Variant("타이트(발동2%/추적3%)", {"risk.trailing_arm_pct": 0.02, "risk.trailing_stop_pct": 0.03}, {"scale_out": False}),
                Variant("와이드(발동4%/추적7%)", {"risk.trailing_arm_pct": 0.04, "risk.trailing_stop_pct": 0.07}, {"scale_out": False}),
                Variant("기준 폭 + scale_out 켬", {}, {"scale_out": True}),
            ],
        ),
        Hypothesis(
            "d_max_consecutive_losses",
            "연속 손절 후 정지 문턱(2회 vs 4회, reduce_after_loss=false 로 실제 정지시켜 비교)",
            [
                Variant("기준(정지 없음, reduce_after_loss=true)", {}, {}),
                Variant("연속 2회 손절시 정지", {"risk.max_consecutive_losses": 2, "risk.reduce_after_loss": False}, {}),
                Variant("연속 4회 손절시 정지", {"risk.max_consecutive_losses": 4, "risk.reduce_after_loss": False}, {}),
            ],
        ),
        Hypothesis(
            "e_volume_surge_ratio",
            "거래량 급증 배수(volume_surge_ratio) 1.5배/2배/3배 비교",
            [
                Variant(f"volume_surge_ratio={cfg.entry.volume_surge_ratio:g}(기준)", {}, {}),
                Variant("volume_surge_ratio=1.5", {"entry.volume_surge_ratio": 1.5}, {}),
                Variant("volume_surge_ratio=2", {"entry.volume_surge_ratio": 2.0}, {}),
                Variant("volume_surge_ratio=3", {"entry.volume_surge_ratio": 3.0}, {}),
            ],
        ),
        Hypothesis(
            "f_scan_start",
            "신규 진입 시작 시각을 09:05/09:20/09:30 으로 늦출수록(개장 초반 노이즈 회피) 성적이 나아지는가",
            [
                Variant(f"scan_start={cfg.entry.scan_start.strftime('%H:%M')}(기준)", {}, {}),
                Variant("scan_start=09:05", {"entry.scan_start": "09:05"}, {}),
                Variant("scan_start=09:20", {"entry.scan_start": "09:20"}, {}),
                Variant("scan_start=09:30", {"entry.scan_start": "09:30"}, {}),
            ],
        ),
        _breakout_lookback_atr_hypothesis(cfg),
    ]

    total = sum(len(h.variants) for h in hyps)
    if total > MAX_COMBINATIONS:
        raise ValueError(f"그리드 조합이 {total}개로 한도({MAX_COMBINATIONS})를 넘습니다 - 가설 수를 줄이세요.")
    return hyps
