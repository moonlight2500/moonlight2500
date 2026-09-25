"""월간 리뷰 · 최적화 제안 · 반영 이력.

★★ 이 모듈은 설정을 스스로 바꾸지 않는다. propose() 는 제안만 만든다.
실제 반영은 사용자가 화면에서 고른 것만 server.py 의 apply 라우트로 들어간다.
자동 최적화는 과최적화로 가는 가장 빠른 길이고, 무엇보다 사용자가 자기
돈이 걸린 규칙이 언제 바뀌었는지 몰라선 안 된다.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

# ★ 표본이 작으면 제안하지 않는다. 근거 없는 제안보다 판단 보류가 낫다.
MIN_TRADES_TECHNIQUE = 12  # 기법 하나를 판단하는 데 필요한 거래 수
MIN_TRADES_MONTH = 20  # 월 전체 제안을 시작하는 최소 거래 수
MIN_TRADES_SLOT = 8  # 시간대 하나를 판단하는 데 필요한 거래 수

SLOTS = [("09:00", "10:00"), ("10:00", "11:30"), ("11:30", "13:30"), ("13:30", "14:30"), ("14:30", "15:30")]

_WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

_REASON_KEYWORDS = [
    # ★ "시간 손절"에는 "손절"도 들어있어 순서를 잘못 두면 stop_loss 로
    # 먼저 잡혀버린다. 더 구체적인 패턴(시간/트레일링/모멘텀/마감)을
    # 일반적인 "손절"보다 먼저 검사한다.
    ("time_stop", ("시간",)),
    ("trailing", ("트레일링", "추적")),
    ("momentum_fade", ("모멘텀",)),
    ("force_close", ("마감", "강제")),
    ("stop_loss", ("손절",)),
    ("take_profit", ("익절",)),
]


def _reason_key(reason: str) -> str:
    reason = reason or ""
    for key, kws in _REASON_KEYWORDS:
        if any(kw in reason for kw in kws):
            return key
    return "기타"


def _slot_of(entry_time: str) -> str:
    t = (entry_time or "")[11:16]
    for start, end in SLOTS:
        if start <= t < end:
            return f"{start}~{end}"
    return "기타"


def _held_minutes(row: dict) -> float:
    try:
        et = datetime.fromisoformat(row["entry_time"])
        xt = datetime.fromisoformat(row["exit_time"])
        return (xt - et).total_seconds() / 60.0
    except Exception:
        return 0.0


def _group_by(rows: list, keyfn) -> dict:
    groups: dict = {}
    for r in rows:
        groups.setdefault(keyfn(r), []).append(r)
    return groups


# ━━ 통계 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _stat(rows: list) -> dict:
    """어디서든 같은 방식으로 센다."""
    n = len(rows)
    pnl = sum(r["pnl"] for r in rows)
    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]

    win_rate = (len(wins) / n) if n else 0.0
    avg_win = (sum(r["pnl"] for r in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(r["pnl"] for r in losses) / len(losses)) if losses else 0.0
    # ★ JSON 은 Infinity 를 표현할 수 없다. 손실이 없으면(분모 0) None 을 준다 -
    # 화면에서 "∞"로 보여주면 된다.
    payoff = (avg_win / abs(avg_loss)) if avg_loss else (None if avg_win > 0 else 0.0)

    gross_win = sum(r["pnl"] for r in wins)
    gross_loss = abs(sum(r["pnl"] for r in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss else (None if gross_win > 0 else 0.0)

    # ★ 승률보다 이게 진실이다 - 한 거래당 평균적으로 얼마를 벌고 잃는지.
    expectancy = (pnl / n) if n else 0.0

    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for r in rows:
        cum += r["pnl"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)

    return {
        "trades": n, "pnl": pnl, "wins": len(wins), "win_rate": win_rate,
        "profit_factor": profit_factor, "expectancy": expectancy,
        "avg_win": avg_win, "avg_loss": avg_loss, "payoff": payoff,
        "mdd": mdd, "enough": n >= MIN_TRADES_TECHNIQUE,
    }


def month_stats(trades: list, month: str, technique_labels: dict, reason_labels: dict) -> dict:
    """다섯 각도로 자른다: 기법별 · 청산사유별 · 시간대별 · 테마별 · 요일별."""
    rows = [t for t in trades if (t.get("date") or "")[:7] == month]
    n = len(rows)

    overall = _stat(rows)
    overall["enough"] = n >= MIN_TRADES_MONTH

    by_technique = {}
    for key, group in _group_by(rows, lambda r: r.get("technique", "") or "기타").items():
        s = _stat(group)
        s["label"] = technique_labels.get(key, key)
        by_technique[key] = s

    by_reason = {}
    for key, group in _group_by(rows, lambda r: _reason_key(r.get("reason", ""))).items():
        s = _stat(group)
        s["label"] = reason_labels.get(key, key)
        by_reason[key] = s

    by_slot = {}
    for key, group in _group_by(rows, lambda r: _slot_of(r.get("entry_time", ""))).items():
        s = _stat(group)
        s["enough"] = len(group) >= MIN_TRADES_SLOT
        by_slot[key] = s

    by_theme = {}
    for key, group in _group_by(rows, lambda r: r.get("theme", "") or "기타").items():
        by_theme[key] = _stat(group)

    def _weekday_of(r):
        try:
            return _WEEKDAY_KO[datetime.fromisoformat(r["entry_time"]).weekday()]
        except Exception:
            return "?"

    by_weekday = {}
    for key, group in _group_by(rows, _weekday_of).items():
        by_weekday[key] = _stat(group)

    daily_pnl: dict = {}
    for r in rows:
        daily_pnl[r["date"]] = daily_pnl.get(r["date"], 0) + r["pnl"]
    cum = 0
    days = []
    for d in sorted(daily_pnl.keys()):
        cum += daily_pnl[d]
        days.append({"date": d, "pnl": daily_pnl[d], "cumulative": cum})

    win_rows = [r for r in rows if r["pnl"] > 0]
    loss_rows = [r for r in rows if r["pnl"] <= 0]
    # ★ 이긴 거래와 진 거래의 보유시간을 나눠서 낸다 - 진 거래가 더 길면
    # 손절을 미루고 있다는 뜻이다.
    holds = {
        "avg": (sum(_held_minutes(r) for r in rows) / n) if n else 0.0,
        "win_avg": (sum(_held_minutes(r) for r in win_rows) / len(win_rows)) if win_rows else 0.0,
        "loss_avg": (sum(_held_minutes(r) for r in loss_rows) / len(loss_rows)) if loss_rows else 0.0,
    }

    return {
        "month": month, "empty": n == 0, "overall": overall,
        "by_technique": by_technique, "by_reason": by_reason, "by_slot": by_slot,
        "by_theme": by_theme, "by_weekday": by_weekday,
        "days": days, "holds": holds,
    }


# ━━ 제안 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _cfg_get(raw: dict, path: str):
    node = raw
    for part in path.split("."):
        node = node[part]
    return node


def propose(stats: dict, cfg_raw: dict, technique_labels: dict) -> list:
    """제안만 만든다. 실제 반영은 사용자가 고른 것만 들어간다."""
    proposals: list = []
    if stats.get("empty") or not stats["overall"]["enough"]:
        return proposals

    overall = stats["overall"]
    entry_order = list(_cfg_get(cfg_raw, "strategy.entry_order") or [])

    tech_in_use = [(k, v) for k, v in stats["by_technique"].items() if k in entry_order]

    # 1. 기법 기대값<0, 12거래↑ → entry_order 에서 제외 (★ 마지막 하나는 남긴다)
    for key, s in tech_in_use:
        if s["trades"] >= MIN_TRADES_TECHNIQUE and s["expectancy"] < 0 and len(entry_order) > 1:
            new_order = [t for t in entry_order if t != key]
            proposals.append({
                "id": f"drop_{key}", "kind": "config", "path": "strategy.entry_order",
                "label": f"{technique_labels.get(key, key)} 진입 끄기",
                "current": entry_order, "proposed": new_order,
                "reason": (
                    f"{technique_labels.get(key, key)} 기법의 기대값이 거래당 "
                    f"{s['expectancy']:,.0f}원으로 마이너스입니다 ({s['trades']}건 기준)."
                ),
                "evidence": {"trades": s["trades"], "expectancy": s["expectancy"], "win_rate": s["win_rate"]},
                "confidence": "높음" if s["trades"] >= MIN_TRADES_TECHNIQUE * 2 else "보통",
                "risk": "이 기법이 통하는 다른 국면을 놓칠 수 있습니다. 한 달치 표본이라 다음 달엔 결과가 다를 수 있습니다.",
            })

    # 2. 기대값 최상위인데 1순위 아님 → 순서 맨 앞으로
    if len(tech_in_use) >= 2:
        best_key, best_s = max(tech_in_use, key=lambda kv: kv[1]["expectancy"])
        if best_s["trades"] >= MIN_TRADES_TECHNIQUE and entry_order and entry_order[0] != best_key:
            new_order = [best_key] + [t for t in entry_order if t != best_key]
            proposals.append({
                "id": f"promote_{best_key}", "kind": "config", "path": "strategy.entry_order",
                "label": f"{technique_labels.get(best_key, best_key)} 를 1순위로",
                "current": entry_order, "proposed": new_order,
                "reason": (
                    f"{technique_labels.get(best_key, best_key)} 의 기대값이 거래당 "
                    f"{best_s['expectancy']:,.0f}원으로 가장 높은데 지금은 1순위가 아닙니다."
                ),
                "evidence": {"trades": best_s["trades"], "expectancy": best_s["expectancy"]},
                "confidence": "보통",
                "risk": "진입은 순서대로 처음 통과한 기법 하나만 씁니다. 순서를 바꾸면 다른 기법이 밀려 덜 쓰이게 됩니다.",
            })

    # 3. 켜져 있는데 이번 달 0건 → 끄거나 완화 (확신 '낮음')
    for key in entry_order:
        if key not in stats["by_technique"]:
            proposals.append({
                "id": f"unused_{key}", "kind": "note", "path": None,
                "label": f"{technique_labels.get(key, key)} 이번 달 0건",
                "current": None, "proposed": None,
                "reason": f"{technique_labels.get(key, key)} 기법은 켜져 있지만 이번 달 한 번도 매수하지 않았습니다.",
                "evidence": {"trades": 0},
                "confidence": "낮음",
                "risk": "끄면 이 기법이 맞는 장세가 왔을 때 놓칩니다. 표본이 없어 결론 내리기 이릅니다.",
            })

    # 4. 손절 청산 비중 > 50% → stop_loss_pct × 1.25 (상한 0.04)
    stop = stats["by_reason"].get("stop_loss")
    if stop and overall["trades"] and (stop["trades"] / overall["trades"]) > 0.5:
        cur = _cfg_get(cfg_raw, "risk.stop_loss_pct")
        new = min(0.04, round(cur * 1.25, 5))
        if new > cur:
            proposals.append({
                "id": "widen_stop", "kind": "config", "path": "risk.stop_loss_pct",
                "label": "손절 폭 넓히기",
                "current": cur, "proposed": new,
                "reason": f"이번 달 청산의 {stop['trades'] / overall['trades'] * 100:.0f}%가 손절이었습니다.",
                "evidence": {"stop_trades": stop["trades"], "total_trades": overall["trades"]},
                "confidence": "보통",
                "risk": "손절 폭을 넓히면 한 번의 손실이 커집니다. 비중이 높은 건 시장이 원래 거칠었던 탓일 수도 있습니다.",
            })

    # 5. 승률≥55% 인데 payoff<1.0 → take_profit_pct × 1.3
    if overall["win_rate"] >= 0.55 and overall["payoff"] is not None and overall["payoff"] < 1.0:
        cur = _cfg_get(cfg_raw, "risk.take_profit_pct")
        new = round(cur * 1.3, 5)
        proposals.append({
            "id": "widen_take_profit", "kind": "config", "path": "risk.take_profit_pct",
            "label": "익절 폭 넓히기",
            "current": cur, "proposed": new,
            "reason": (
                f"승률은 {overall['win_rate'] * 100:.0f}%로 높은데 평균 이익이 평균 손실보다 작습니다"
                f"(payoff {overall['payoff']:.2f}). 너무 일찍 파는 것일 수 있습니다."
            ),
            "evidence": {"win_rate": overall["win_rate"], "payoff": overall["payoff"]},
            "confidence": "보통",
            "risk": "익절 폭을 넓히면 갈 데까지 못 가고 반락해 오히려 이익이 줄어들 수 있습니다.",
        })

    # 6. 시간손절 다수, 기대값≈0 → max_hold_minutes × 0.7 (하한 10)
    time_stop = stats["by_reason"].get("time_stop")
    if time_stop and time_stop["trades"] >= MIN_TRADES_TECHNIQUE and abs(time_stop["expectancy"]) < 500:
        cur = _cfg_get(cfg_raw, "exit.max_hold_minutes")
        new = max(10, int(cur * 0.7))
        if new < cur:
            proposals.append({
                "id": "shorten_hold", "kind": "config", "path": "exit.max_hold_minutes",
                "label": "최대 보유 시간 줄이기",
                "current": cur, "proposed": new,
                "reason": (
                    f"시간 손절로 끝난 거래가 {time_stop['trades']}건인데 기대값이 거의 0"
                    f"({time_stop['expectancy']:,.0f}원)입니다. 그 시간만큼 자본이 묶여 있었을 뿐입니다."
                ),
                "evidence": {"trades": time_stop["trades"], "expectancy": time_stop["expectancy"]},
                "confidence": "보통",
                "risk": "보유 시간을 줄이면 나중에 오를 종목을 일찍 팔아버릴 수 있습니다.",
            })

    # 7. 특정 시간대 기대값<0 → scan_end 당기기, 불가면 note
    for slot_key, s in stats["by_slot"].items():
        if s.get("enough") and s["expectancy"] < 0:
            if slot_key.startswith("14:30"):
                cur = _cfg_get(cfg_raw, "entry.scan_end")
                proposals.append({
                    "id": f"narrow_scan_end_{slot_key}", "kind": "config", "path": "entry.scan_end",
                    "label": "매매 시간 앞당겨 마감",
                    "current": cur, "proposed": "14:30",
                    "reason": f"{slot_key} 시간대의 기대값이 거래당 {s['expectancy']:,.0f}원으로 마이너스입니다.",
                    "evidence": {"trades": s["trades"], "expectancy": s["expectancy"]},
                    "confidence": "보통",
                    "risk": "그 시간대에도 가끔 좋은 기회가 있을 수 있습니다. 한 달 표본만으로 접기엔 이릅니다.",
                })
            else:
                proposals.append({
                    "id": f"note_slot_{slot_key}", "kind": "note", "path": None,
                    "label": f"{slot_key} 시간대 부진",
                    "current": None, "proposed": None,
                    "reason": f"{slot_key} 시간대의 기대값이 거래당 {s['expectancy']:,.0f}원으로 마이너스입니다. "
                              "이 시간대만 따로 끄는 설정은 없습니다.",
                    "evidence": {"trades": s["trades"], "expectancy": s["expectancy"]},
                    "confidence": "낮음",
                    "risk": "다음 달에는 이 시간대가 좋아질 수도 있습니다.",
                })

    # 8. 월 전체 손실 → daily_max_trades × 0.6 (하한 2)
    if overall["pnl"] < 0:
        cur = _cfg_get(cfg_raw, "risk.daily_max_trades")
        new = max(2, int(cur * 0.6))
        if new < cur:
            proposals.append({
                "id": "cut_daily_trades", "kind": "config", "path": "risk.daily_max_trades",
                "label": "일일 최대 거래 줄이기",
                "current": cur, "proposed": new,
                "reason": f"이번 달 전체 손익이 {overall['pnl']:+,.0f}원으로 마이너스입니다.",
                "evidence": {"pnl": overall["pnl"], "trades": overall["trades"]},
                "confidence": "보통",
                "risk": "거래 횟수를 줄이면 반등했을 때 기회도 함께 줄어듭니다.",
            })

    # 9. 특정 테마 기대값<0 → note (테마 편집은 테마 탭에서)
    for theme_key, s in stats["by_theme"].items():
        if s["trades"] >= MIN_TRADES_TECHNIQUE and s["expectancy"] < 0:
            proposals.append({
                "id": f"note_theme_{theme_key}", "kind": "note", "path": None,
                "label": f"'{theme_key}' 테마 부진",
                "current": None, "proposed": None,
                "reason": f"'{theme_key}' 테마에서 이번 달 기대값이 거래당 {s['expectancy']:,.0f}원으로 마이너스입니다"
                          f"({s['trades']}건).",
                "evidence": {"trades": s["trades"], "expectancy": s["expectancy"]},
                "confidence": "낮음",
                "risk": "테마 편집은 [테마] 탭에서 직접 하세요. 여기서는 참고만 하세요.",
            })

    return proposals


# ━━ 자체안 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def house_ideas(stats: dict) -> list:
    """★ 교과서에 실린 기법이 아니다. 검증된 기법을 조합했거나 조건을 더한 것이고
    실전 기록이 없다. 기본값으로 켜지지 않고, 접힌 섹션에 경고와 함께 들어간다.
    """
    return [
        {
            "label": "상대강도 ORB", "base": "ORB(Crabel, 1990) + 테마 대장주(O'Neil, 1988)",
            "idea": "개장 레인지를 돌파하는 순간, 그 종목이 테마 내 상대강도 1위일 때만 진입합니다.",
            "why": "ORB 와 테마 대장주는 각각 문헌에 있는 기법이지만, 둘을 '동시 만족'으로 묶은 "
                   "조합 자체는 이 프로그램이 만든 것이라 검증된 문헌이 없습니다.",
            "expect": "가짜 돌파를 줄여 승률이 오를 수 있지만, 조건이 겹쳐 기회 자체가 줄어듭니다.",
            "check": "시뮬레이션에서 최소 60거래 이상 모아 기존 ORB 단독과 비교해보세요.",
            "status": "검증 안 됨 — 시뮬레이션 먼저",
        },
        {
            "label": "2차 눌림목만", "base": "이동평균 눌림목(Raschke & Connors, 1995)",
            "idea": "추세가 시작된 뒤 첫 번째 되돌림은 건너뛰고, 두 번째 되돌림에서만 진입합니다.",
            "why": "\"첫 되돌림은 속임수가 많다\"는 트레이더들 사이의 경험칙일 뿐, 이를 뒷받침하는 "
                   "학술적·서적상 근거는 없습니다.",
            "expect": "가짜 되돌림에 덜 낚이겠지만, 진짜 좋은 첫 진입 기회도 함께 놓칩니다.",
            "check": "1차 vs 2차 눌림목의 승률·기대값을 각각 따로 기록해 비교해보세요.",
            "status": "검증 안 됨 — 시뮬레이션 먼저",
        },
        {
            "label": "ATR 사이징", "base": "변동성 기반 포지션 사이징(Van Tharp, 1998)",
            "idea": "모든 종목에 같은 비율을 투입하는 대신, ATR(변동성)이 클수록 수량을 줄입니다.",
            "why": "사이징 개념 자체는 Van Tharp 의 표준 이론이지만, 지금 구조(정액 비율 투입)를 "
                   "바꿔야 하는 구조 변경이라 자동 반영 대상에서 뺐습니다.",
            "expect": "변동성 큰 종목에서 손실 폭을 억제할 수 있지만, 계산이 복잡해지고 매매 규모가 매번 달라집니다.",
            "check": "같은 기간을 정액 사이징과 ATR 사이징으로 각각 시뮬레이션해 MDD 를 비교해보세요.",
            "status": "검증 안 됨 — 시뮬레이션 먼저",
        },
        {
            "label": "지수 역행일 중단", "base": "시장 국면 필터 (다우 이론 등 오래된 원칙)",
            "idea": "코스피가 전일 대비 -1% 이상 빠진 날은 신규 진입을 하지 않습니다.",
            "why": "지수와 개별 종목의 관계를 보고 매매를 거른다는 원칙 자체는 오래됐지만, "
                   "'-1%'라는 구체적인 수치는 이 프로그램이 임의로 정한 값이라 문헌 근거가 없습니다.",
            "expect": "지수 급락일의 동반 손실을 줄일 수 있지만, 그런 날 반등하는 개별 종목의 기회도 놓칩니다.",
            "check": "지수 급락일과 평상일의 승률·기대값을 나눠 비교하고, -1% 기준값도 여러 값으로 시험해보세요.",
            "status": "검증 안 됨 — 시뮬레이션 먼저",
        },
    ]


# ━━ 반영 이력 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class OptimizeHistory:
    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, "optimize_history.jsonl")

    def append(self, entry: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read(self, limit: int = 200) -> list:
        if not os.path.exists(self.path):
            return []
        rows = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue  # 깨진 줄은 건너뛴다.
        return rows[-limit:]

    def find(self, entry_id: str):
        for row in self.read(limit=100000):
            if row.get("id") == entry_id:
                return row
        return None


def new_id() -> str:
    return uuid.uuid4().hex[:12]
