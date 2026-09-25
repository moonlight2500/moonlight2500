"""리서치 백테스트 CLI.

    python -m research.run --source synthetic --days 10 --out research/out/
    python -m research.run --source yahoo --days 30 --out research/out/
    python -m research.run --source csv --csv-path my_bars.csv --out research/out/
    python -m research.run --source toss --days 30 --out research/out/   # 로컬 세션(Toss 키 등록) 전용

산출물: <out>/report.md (한국어 마크다운 보고서).
"""

from __future__ import annotations

import argparse
import os
import sys
import time as _time
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from daytrader.config import load_config  # noqa: E402
from daytrader.playbook import Bar  # noqa: E402
from research import data as data_mod  # noqa: E402
from research import grid as grid_mod  # noqa: E402
from research import metrics as metrics_mod  # noqa: E402
from research.backtest import DEFAULT_SESSION_BUCKETS, run_backtest  # noqa: E402


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="AutoDayTrading 리서치 백테스트")
    p.add_argument("--source", choices=["synthetic", "csv", "yahoo", "toss"], default="synthetic")
    p.add_argument("--days", type=int, default=30, help="최근 며칠치(영업일 기준, csv 는 무시)")
    p.add_argument("--symbols", type=str, default="", help="쉼표로 구분한 종목코드 목록(생략 시 기본 유니버스)")
    p.add_argument("--csv-path", type=str, default="", help="--source csv 일 때 읽을 CSV 파일 경로")
    p.add_argument("--out", type=str, default=os.path.join(ROOT, "research", "out"))
    p.add_argument("--config", type=str, default="", help="config.yaml 경로(생략 시 프로젝트 기본값)")
    p.add_argument("--min-trades", type=int, default=30, help="in-sample 최선 설정 채택에 필요한 최소 거래 수")
    p.add_argument("--in-sample-frac", type=float, default=0.7)
    p.add_argument("--skip-grid", action="store_true", help="가설 그리드를 건너뛰고 기준선만 낸다(빠른 확인용)")
    p.add_argument(
        "--grid-symbols", type=str, default="",
        help="가설 그리드는 이 쉼표목록(유니버스의 부분집합)만으로 돌린다(1절 기준선 표는 "
             "그래도 전체 유니버스를 쓴다) - 유니버스가 커서 그리드가 오래 걸릴 때 씀",
    )
    p.add_argument("--scenario-days", type=str, default="", help="synthetic 전용: 'normal:5,crash:2' 처럼 날짜별 시나리오 수")
    return p.parse_args(argv)


def _load_bars(args, cfg) -> tuple:
    """(bars_by_symbol, errors, source_note) 를 돌려준다."""
    if args.symbols.strip():
        symbols = [s.strip().zfill(6) if s.strip().isdigit() else s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = list(data_mod.default_universe(cfg).keys())

    if args.source == "synthetic":
        scenario_by_day = None
        if args.scenario_days:
            # "normal:5,crash:2,choppy:3" -> 날짜 목록에 순서대로 배정.
            from datetime import timedelta

            from daytrader.timeutil import now_kst

            counts = []
            for part in args.scenario_days.split(","):
                name, n = part.split(":")
                counts.extend([name.strip()] * int(n))
            end = now_kst().date() - timedelta(days=1)
            business_days = []
            d = end
            while len(business_days) < len(counts):
                if d.weekday() < 5:
                    business_days.append(d.strftime("%Y-%m-%d"))
                d -= timedelta(days=1)
            business_days.reverse()
            scenario_by_day = dict(zip(business_days, counts))
        bars = data_mod.generate_synthetic(cfg, symbols, days=args.days, scenario_by_day=scenario_by_day)
        return bars, [], f"합성 시세(시나리오 {cfg.simulation.scenario if not scenario_by_day else args.scenario_days}, {args.days}영업일)"

    if args.source == "csv":
        if not args.csv_path:
            raise SystemExit("--source csv 는 --csv-path 가 필요합니다.")
        bars = data_mod.load_csv(args.csv_path)
        if args.symbols.strip():
            bars = {s: b for s, b in bars.items() if s in symbols}
        return bars, [], f"CSV({args.csv_path})"

    if args.source == "yahoo":
        bars, errors = data_mod.fetch_yahoo(symbols, days=args.days)
        return bars, errors, f"Yahoo Finance(최근 {args.days}일)"

    if args.source == "toss":
        bars, errors = data_mod.fetch_toss(cfg, symbols, days=args.days)
        return bars, errors, f"Toss Open API(최근 {args.days}일)"

    raise SystemExit(f"알 수 없는 source: {args.source}")


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def _fmt_row(c: metrics_mod.CellStats) -> str:
    ci = f"[{_fmt_pct(c.ci90_low)}, {_fmt_pct(c.ci90_high)}]" if c.trades >= 2 else "-"
    return (
        f"| {c.technique} | {c.session} | {c.trades} | {c.win_rate*100:.0f}% | "
        f"{_fmt_pct(c.avg_win_pct)} | {_fmt_pct(c.avg_loss_pct)} | {_fmt_pct(c.expectancy_pct)} | "
        f"{c.expectancy_r:+.2f}R | {c.profit_factor:.2f} | {_fmt_pct(c.max_drawdown_pct)} | "
        f"{c.avg_hold_minutes:.1f}분 | {c.pct_stopped_within_5min*100:.0f}% | {ci} |"
    )


_TABLE_HEADER = (
    "| 기법 | 세션 | 거래수 | 승률 | 평균이익 | 평균손실 | 기대값 | 기대값(R) | 손익비 | MDD | 평균보유 | 5분내손절 | 기대값 90%CI |\n"
    "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|\n"
)


def _recommend_session_profile(baseline_cells: List[metrics_mod.CellStats], hyp_results: List[dict], min_trades: int) -> Dict:
    """세션×기법 표에서 "표본이 충분한데 기대값이 뚜렷하게 마이너스"인 조합을 끄도록 권고하고,
    OOS 에서 기준을 이긴 가설의 대표값을 세션 공통 권고 파라미터로 얹는다.
    ★ 이건 통계적으로 확정된 결론이 아니라 "다음에 시험해 볼 가설"이다(README 경고 참고)."""
    off_by_session: Dict[str, List[str]] = {}
    for c in baseline_cells:
        if c.session == "전체" or c.technique == "전체":
            continue
        if c.trades >= max(10, min_trades // 2) and c.expectancy_pct < 0:
            off_by_session.setdefault(c.session, []).append(f"{c.technique}(기대값 {_fmt_pct(c.expectancy_pct)}, n={c.trades})")

    param_recs = []
    for r in hyp_results:
        if r["beats_baseline_oos"] and r["robust"] and not r["low_confidence"]:
            param_recs.append(
                f"[{r['hypothesis']}] {r['chosen'].label} - OOS 기대값 {_fmt_pct(r['chosen_oos'].expectancy_pct)} "
                f"(기준 {_fmt_pct(r['baseline_oos'].expectancy_pct)}), n={r['chosen_oos'].trades}"
            )
    return {"off_by_session": off_by_session, "param_recommendations": param_recs}


def build_report(
    *, source_note: str, universe: List[str], errors: List[str], baseline_cells: List[metrics_mod.CellStats],
    baseline_overall: metrics_mod.CellStats, hyp_results: List[dict], in_days: List[str], oos_days: List[str],
    recommendation: Dict, min_trades: int, skipped_grid: bool, grid_note: str = "",
) -> str:
    lines = []
    lines.append("# AutoDayTrading 리서치 백테스트 보고서\n")
    lines.append(f"- 시세 소스: {source_note}")
    lines.append(f"- 유니버스: {len(universe)}종목")
    lines.append(f"- 거래일: 총 {len(in_days) + len(oos_days)}일 (in-sample {len(in_days)}일, out-of-sample {len(oos_days)}일)")
    lines.append(f"- 전체 거래 {baseline_overall.trades}건, 기대값 {_fmt_pct(baseline_overall.expectancy_pct)}, "
                  f"승률 {baseline_overall.win_rate*100:.0f}%, 손익비 {baseline_overall.profit_factor:.2f}\n")
    if errors:
        lines.append("## 시세 조회 경고\n")
        for e in errors:
            lines.append(f"- {e}")
        lines.append("")

    lines.append("## 1. 기준선(현재 config.yaml) - 기법×세션 성적\n")
    lines.append(_TABLE_HEADER.rstrip("\n"))
    for c in baseline_cells:
        lines.append(_fmt_row(c))
    lines.append("")

    if skipped_grid:
        lines.append("## 2. 가설 그리드\n\n--skip-grid 로 건너뛰었습니다.\n")
    else:
        lines.append(f"## 2. 가설별 워크포워드 검증(in-sample 로 고르고 out-of-sample 로 확인){grid_note}\n")
        lines.append(
            "| 가설 | 선택된 설정 | in-sample 기대값(n) | 강건함(이웃 20%이내) | OOS 기대값(기준→선택) | 기준 이김(OOS) |\n"
            "|---|---|---|---|---|---|"
        )
        for r in hyp_results:
            robust = "예" if r["robust"] else "아니오"
            if r["low_confidence"]:
                robust += "(표본부족·주의)"
            oos_str = f"{_fmt_pct(r['baseline_oos'].expectancy_pct)}(n={r['baseline_oos'].trades}) → {_fmt_pct(r['chosen_oos'].expectancy_pct)}(n={r['chosen_oos'].trades})"
            beat = "예" if r["beats_baseline_oos"] else "아니오"
            lines.append(
                f"| {r['description']} | {r['chosen'].label} | {_fmt_pct(r['chosen_is_stats'].expectancy_pct)}"
                f"(n={r['chosen_is_stats'].trades}) | {robust} | {oos_str} | {beat} |"
            )
        lines.append("")

    lines.append("## 3. 추천 세션 프로필(가설 - 확정 아님)\n")
    off = recommendation["off_by_session"]
    if off:
        lines.append("표본이 어느 정도 있는데 기대값이 뚜렷하게 마이너스라 그 세션에서 끄는 것을 검토할 만한 조합:\n")
        for sess, techs in off.items():
            lines.append(f"- **{sess}**: {', '.join(techs)}")
    else:
        lines.append("표본 부족 또는 뚜렷한 마이너스 조합 없음 - 세션별 기법 on/off 를 바꿀 근거가 아직 약합니다.")
    lines.append("")
    recs = recommendation["param_recommendations"]
    if recs:
        lines.append("OOS 에서 기준을 이기고 강건하다고 나온 파라미터(다음 시험 후보):\n")
        for r in recs:
            lines.append(f"- {r}")
    else:
        lines.append("OOS 에서 기준을 뚜렷이 이긴 설정이 없습니다 - **현재 config.yaml 설정을 유지**할 근거가 더 강합니다.")
    lines.append("")

    lines.append("## 4. 한계·주의사항\n")
    lines.append(
        "- **표본 크기**: 한 달치 데이터는 기법당 30건도 못 채우는 경우가 흔합니다. 거래수가 적은 행은 "
        "승률·기대값이 운에 크게 좌우됩니다 - 90% 신뢰구간이 0을 크게 걸치면 신뢰하지 마세요.\n"
        "- **한 달 = 한 장세**: 이 결과는 이 기간의 시장 국면(상승/횡보/급락 중 하나)에만 맞을 수 있습니다. "
        "장세가 바뀌면 최적값도 바뀝니다 - 결론이 아니라 가설로 쓰세요.\n"
        "- **다중검정**: 가설·조합을 여러 개 시험했으므로 그중 일부는 순전히 우연으로 좋아 보일 수 있습니다.\n"
        "- **Yahoo 데이터 결측**: 야후 1분봉은 09:00~15:00 까지만 오고 15:00~15:30(동시호가 포함) 구간이 "
        "빠질 수 있습니다 - 이 구간에서는 장 막판 강제청산·close_squeeze 기법의 타이밍을 정확히 재현하지 못합니다.\n"
        "- **themes.yaml 의 생존편향**: 지금 이 테마 사전은 현재 시점 기준으로 고른 종목들입니다. 한 달 전에도 "
        "같은 종목을 골랐을 거라는 보장이 없습니다(테마가 바뀌면 실제 스크리너가 골랐을 종목도 달라집니다).\n"
        "- **재현하지 않은 것**: 분할 매수(피라미딩)·VI(상한가 근접)·호가 잔량·체결 우선순위·뉴스 반응·"
        "주간(weekly) 손실 한도. backtest.py 모듈 독스트링에 전부 밝혀 두었습니다.\n"
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    args = _parse_args(argv)
    t0 = _time.time()
    cfg = load_config(args.config or None)

    print(f"[1/4] 시세 로딩 중 (source={args.source}, days={args.days}) ...")
    bars_by_symbol, errors, source_note = _load_bars(args, cfg)
    bars_by_symbol = {s: b for s, b in bars_by_symbol.items() if b}
    if not bars_by_symbol:
        print("받은 봉이 하나도 없습니다.")
        for e in errors:
            print(" -", e)
        return 1

    names = data_mod.default_universe(cfg)
    themes = data_mod.symbol_theme_map(cfg)
    for s in bars_by_symbol:
        names.setdefault(s, s)
        themes.setdefault(s, "관심종목")

    all_days = sorted({(b.ts or "")[:10] for bars in bars_by_symbol.values() for b in bars})
    print(f"      종목 {len(bars_by_symbol)}개, 거래일 {len(all_days)}일, 총 봉 {sum(len(b) for b in bars_by_symbol.values())}개")
    if errors:
        print(f"      경고 {len(errors)}건 (report.md 에 기록)")

    print("[2/4] 기준선(config.yaml 그대로) 백테스트 중 ...")
    baseline = run_backtest(cfg, bars_by_symbol, symbol_names=names, symbol_themes=themes)
    baseline_cells = metrics_mod.by_technique_session(baseline.trades)
    baseline_overall = metrics_mod.overall(baseline.trades)
    print(f"      거래 {baseline_overall.trades}건, 기대값 {baseline_overall.expectancy_pct*100:+.2f}%")

    in_days, oos_days = grid_mod.split_walk_forward(all_days, args.in_sample_frac)
    hyp_results: List[dict] = []
    grid_bars = bars_by_symbol
    grid_note = ""
    if args.grid_symbols.strip():
        wanted = {s.strip().zfill(6) if s.strip().isdigit() else s.strip() for s in args.grid_symbols.split(",") if s.strip()}
        grid_bars = {s: b for s, b in bars_by_symbol.items() if s in wanted}
        grid_note = f" (그리드는 대표 {len(grid_bars)}종목 부분집합으로 실행 - 실행 시간 절약)"
    if not args.skip_grid and len(all_days) >= 4:
        print(f"[3/4] 가설 그리드 실행 중 (in-sample {len(in_days)}일 / out-of-sample {len(oos_days)}일){grid_note} ...")
        hyps = grid_mod.build_default_hypotheses(cfg)
        for h in hyps:
            t1 = _time.time()
            r = grid_mod.evaluate_hypothesis(cfg, h, grid_bars, names, themes, in_days, oos_days, args.min_trades)
            hyp_results.append(r)
            print(f"      - {h.name}: 선택={r['chosen'].label} OOS기대값={r['chosen_oos'].expectancy_pct*100:+.2f}% "
                  f"기준이김={r['beats_baseline_oos']} ({_time.time()-t1:.1f}s)")
    else:
        print("[3/4] 가설 그리드 건너뜀")

    recommendation = _recommend_session_profile(baseline_cells, hyp_results, args.min_trades)

    print("[4/4] 보고서 작성 중 ...")
    os.makedirs(args.out, exist_ok=True)
    report = build_report(
        source_note=source_note, universe=list(bars_by_symbol.keys()), errors=errors,
        baseline_cells=baseline_cells, baseline_overall=baseline_overall, hyp_results=hyp_results,
        in_days=in_days, oos_days=oos_days, recommendation=recommendation, min_trades=args.min_trades,
        skipped_grid=args.skip_grid or len(all_days) < 4, grid_note=grid_note,
    )
    out_path = os.path.join(args.out, "report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"완료 ({_time.time()-t0:.1f}s) -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
