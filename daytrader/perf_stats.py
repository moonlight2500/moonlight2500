"""해외주식·암호화폐 청산 기록(closed 리스트)에서 승률·손익비·기법별
성과를 계산한다. 국내주식은 이미 ledger.py 가 훨씬 정교하게 하고 있지만,
해외주식/암호화폐는 journal/ledger 를 안 쓰고 자기 state 파일에 closed 를
직접 쌓기 때문에(engine 간 매매기록을 절대 섞지 않기 위한 설계), 화면에
승률을 보여주려면 이 계산이 따로 필요하다. 두 엔진이 같은 함수를 써서
계산 방식이 갈리지 않게 한다.
"""

from __future__ import annotations

from collections import Counter


def summarize_closed_trades(closed: list, technique_field: str = "entry_technique", symbol_field: str = "symbol") -> dict:
    """청산 기록 리스트 하나를 받아 화면용 요약을 만든다.
    pnl 이 없는(계산 실패) 기록은 통계에서 제외한다 - 없는 값을 0으로
    섞으면 승률·평균이 왜곡된다.
    """
    trades = [c for c in (closed or []) if isinstance(c, dict) and c.get("pnl") is not None]
    n = len(trades)
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] < 0]
    pnl = sum(t["pnl"] for t in trades)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    by_technique: dict = {}
    for t in trades:
        key = t.get(technique_field) or "미상"
        row = by_technique.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0})
        row["trades"] += 1
        row["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            row["wins"] += 1
    for row in by_technique.values():
        row["win_rate"] = (row["wins"] / row["trades"]) if row["trades"] else 0.0

    symbol_counts = Counter(t.get(symbol_field) or "?" for t in trades)

    return {
        "trades": n,
        "win_rate": (len(wins) / n) if n else 0.0,
        "pnl": pnl,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        # ★★★ float("inf") 을 그대로 내보내면 표준 JSON 문법에 없는
        # "Infinity" 토큰이 응답 본문에 그대로 찍힌다 - 브라우저의
        # JSON.parse 는 이걸 못 읽고 그 자리에서 예외를 던져서, 화면
        # 전체(상태 조회)가 그 요청부터 깨진다(실제로 겪을 뻔한 문제).
        # 손실이 하나도 없으면(무한대) None 으로 보내고, 화면이 "∞"로 표시한다.
        "profit_factor": (gross_win / gross_loss) if gross_loss else (None if gross_win > 0 else 0.0),
        "by_reason": dict(Counter(t.get("reason") or "?" for t in trades)),
        "by_technique": by_technique,
        # ★ 과다거래 감지용 - UI 가 상위 항목만 뽑아 배지로 보여준다.
        "top_symbols": symbol_counts.most_common(5),
    }
