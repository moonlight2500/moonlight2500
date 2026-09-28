"""종목별·테마별 "이 기법이 최근 더 잘 맞았다" 선호도 저장소.

technique_backtest.py 가 실제 시세로 기법별 손익을 재생해 순위를 매긴 뒤 여기 저장하면,
daytrader.playbook.Playbook 이 진입 기법을 채점할 때(_performance_multiplier 옆) 이 선호도를
가산점으로 반영한다(daytrader/playbook.py 의 _preference_multiplier 참고).

★ 이건 강제 규칙이 아니라 가산점이다 - 표본이 하루치 시세뿐이라 과신하면 안 되므로, 선호 기법이라고
다른 기법을 막지는 않는다(신호가 없으면 안 산다는 원칙은 그대로). 파일 하나(state/technique_prefs.json)
에 시장별로 담아 두고, 새 백테스트를 돌리면 그 시장 몫만 통째로 새 결과로 바뀐다(오래된 종목이
남아 있지 않게).

★★ 자동 실행(auto_backtest.py) - 종목이 새로 선정될 때마다 매번 다시 돌리면 API 호출이 낭비되므로,
"오늘·이 종목 조합으로 이미 돌렸는지"를 candidate_sig 로 기억해 둔다(already_ran_today 참고).

★★ 기록 - 수동이든 자동이든 실행할 때마다 SQLite(daytrader.db 의 technique_backtest_log 표)에
한 줄 남겨서 [실험실] 화면에서 "언제·어떤 계기로·무슨 결과가 나왔는지"를 나중에도 볼 수 있게
한다(db.py 상단 "왜 SQLite 인가" 참고 - 예전엔 state/technique_backtest_log.jsonl 이었다).
"""

from __future__ import annotations

import json
import os
import threading

from daytrader import db

_lock = threading.Lock()

# ★ 선호 기법과 일치하면 이만큼 점수를 올린다 - _performance_multiplier 의 실적 배수(0.7~1.3)와
# 비슷한 크기로 맞춘다. 하루치 표본으로 다른 기법을 완전히 못 쓰게 만들 정도로 세게 주지 않는다.
PREFERENCE_BOOST = 1.15
LOG_MAX_LINES = 200  # [실험실] 화면에는 최근 이만큼만 보여준다(표 자체는 더 오래 남는다)


def _path(cfg) -> str:
    return os.path.join(cfg.state_dir, "technique_prefs.json")


def _load(cfg) -> dict:
    p = _path(cfg)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_raw(cfg, data: dict) -> None:
    p = _path(cfg)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = f"{p}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def candidate_signature(candidates) -> str:
    """오늘 뽑힌 종목 조합을 짧은 서명으로 - 순서와 무관하게 같은 조합이면 같은 값이 나온다.
    candidates 는 [{"symbol":...}, ...] 또는 문자열 리스트 둘 다 받는다."""
    symbols = sorted({(c["symbol"] if isinstance(c, dict) else c) for c in (candidates or [])})
    return "|".join(symbols)


def save_from_backtest(cfg, market: str, backtest_result: dict, *, trigger: str = "manual") -> None:
    """technique_backtest.run() 의 결과를 저장한다 - 이 시장 몫만 갈아 끼우고, 기록 한 줄을 남긴다.
    ★ 백테스트를 실행할 때 이미 들고 있던 cfg 를 그대로 받는다(따로 다시 읽지 않는다) - 테스트가
    cfg.state_dir 을 임시 폴더로 바꿔 넣어도 그대로 존중되고, 실제 실행 시 config.yaml 을 두 번
    읽지 않는다."""
    by_symbol_rows = backtest_result.get("by_symbol", [])
    sig = candidate_signature(by_symbol_rows)

    def _perf_of(row: dict) -> dict:
        win = next((r for r in row.get("results", []) if r.get("technique") == row.get("best_technique")), None)
        return {
            "total_pnl_pct": (win or {}).get("total_pnl_pct", 0.0),
            "win_rate": (win or {}).get("win_rate", 0.0),
            "trades": (win or {}).get("trades", 0),
        }

    with _lock:
        data = _load(cfg)
        data[market] = {
            "at": backtest_result.get("at"),
            "days": backtest_result.get("days"),
            "trigger": trigger,
            "candidate_sig": sig,
            "by_symbol": {
                row["symbol"]: {
                    "technique": row["best_technique"], "name": row.get("name", ""), "theme": row.get("theme", ""),
                    **_perf_of(row),
                }
                for row in by_symbol_rows if row.get("best_technique")
            },
            "by_theme": {
                row["theme"]: row["best_technique"]
                for row in backtest_result.get("by_theme", []) if row.get("theme") and row.get("best_technique")
            },
        }
        _save_raw(cfg, data)
        _append_log(cfg, {
            "at": backtest_result.get("at"), "market": market, "trigger": trigger,
            "days": backtest_result.get("days"), "candidates": backtest_result.get("candidates", 0),
            "picked": sum(1 for row in by_symbol_rows if row.get("best_technique")),
            "errors": backtest_result.get("errors", []),
            "by_symbol": [
                {"symbol": row["symbol"], "name": row.get("name", ""), "theme": row.get("theme", ""),
                 "best_technique": row.get("best_technique"), **_perf_of(row)}
                for row in by_symbol_rows
            ],
        })


def _append_log(cfg, entry: dict) -> None:
    os.makedirs(cfg.state_dir, exist_ok=True)
    db.insert_json_row(cfg.state_dir, "technique_backtest_log", {
        "at": entry.get("at"), "market": entry.get("market") or "", "trigger_": entry.get("trigger"),
    }, entry)


def history(cfg, limit: int = 20) -> list:
    """최근 실행 기록(수동+자동)을 최신순으로. [실험실] 화면의 "최근 실행 기록"에 쓴다."""
    os.makedirs(cfg.state_dir, exist_ok=True)
    conn = db.get_connection(cfg.state_dir)
    limit = limit or LOG_MAX_LINES
    cur = conn.execute(
        "SELECT data FROM (SELECT id, data FROM technique_backtest_log ORDER BY id DESC LIMIT ?) ORDER BY id DESC",
        (limit,),
    )
    return db.load_data_rows(cur.fetchall())


def already_ran_today(cfg, market: str, sig: str) -> bool:
    """오늘 이 종목 조합으로 이미 백테스트를 돌렸으면 True(자동 실행 중복 방지용).
    종목 조합이 비어 있으면(아직 후보가 없음) 항상 False - 돌 것도 없으므로 호출부에서 걸러진다."""
    if not sig:
        return False
    data = _load(cfg)
    bucket = data.get(market) or {}
    at = bucket.get("at") or ""
    return bucket.get("candidate_sig") == sig and at[:10] == _today()


def _today() -> str:
    from daytrader.timeutil import day_str, now_kst
    return day_str(now_kst())


def best_for(cfg, market: str, symbol: str, theme: str = "") -> str | None:
    """이 종목(우선) 또는 테마에 대해 저장된 선호 기법 키. 없으면 None."""
    data = _load(cfg)
    bucket = data.get(market) or {}
    row = (bucket.get("by_symbol") or {}).get(symbol)
    if row and row.get("technique"):
        return row["technique"]
    if theme:
        by_theme = bucket.get("by_theme") or {}
        return by_theme.get(theme)
    return None


def summary(cfg) -> dict:
    """준비·연결/실험실 화면에 그대로 보여줄 수 있는 현재 저장된 선호도 전체."""
    return _load(cfg)


def clear(cfg, market: str | None = None) -> None:
    """전체 또는 한 시장의 선호도를 지운다(백테스트 결과를 실전 반영에서 빼고 싶을 때)."""
    with _lock:
        if market is None:
            _save_raw(cfg, {})
            return
        data = _load(cfg)
        data.pop(market, None)
        _save_raw(cfg, data)
