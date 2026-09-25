"""엔진의 하루치 상태는 그날이 지나면 잊힌다. 성과를 일자별·월별로 보려면
청산된 거래가 한곳에 계속 쌓여야 한다. SQLite(daytrader.db, daytrader/db.py)에 담아
인덱스로 찾으므로 몇 년치가 쌓여도 조회가 느려지지 않는다(예전 ledger.jsonl 은 매번
파일 전체를 다시 읽고 파싱했다 - 왜 SQLite 로 옮겼는지는 db.py 상단 설명 참고).
모든 줄에 mode 가 들어간다 - 연습과 실거래를 절대 섞어서 보여주지 않기 위해서다.
"""

from __future__ import annotations

import os
import statistics
from collections import defaultdict

from daytrader import db
from daytrader.timeutil import won

VIRTUAL_MODES = ("sim", "replay", "web", "paper")
REAL_MODES = ("live",)

MODE_LABELS = {
    "sim": "시뮬레이션", "replay": "리플레이", "web": "관찰", "paper": "모의매매", "live": "실거래",
}


def modes_in(group: str) -> tuple:
    if group == "virtual":
        return VIRTUAL_MODES
    if group == "real":
        return REAL_MODES
    return VIRTUAL_MODES + REAL_MODES


class Ledger:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        db.get_connection(state_dir)  # 스키마 준비 + 기존 ledger.jsonl/equity.jsonl 1회성 가져오기

    def append_trade(self, mode: str, trade: dict) -> None:
        row = {
            "mode": mode,
            "date": trade.get("date"),
            "symbol": trade.get("symbol"),
            "name": trade.get("name"),
            "theme": trade.get("theme"),
            "qty": trade.get("qty"),
            "entry": trade.get("entry"),
            "exit": trade.get("exit"),
            "pnl": won(trade.get("pnl", 0)),  # ★ pnl 은 won() 으로 정수화한다.
            "reason": trade.get("reason", ""),
            "entry_time": trade.get("entry_time"),
            "exit_time": trade.get("exit_time"),
            "technique": trade.get("technique", ""),
            "entry_technique": trade.get("entry_technique", ""),
            "adds": trade.get("adds", 0), "scaled_out": trade.get("scaled_out", 0),
            "verdict_id": trade.get("verdict_id"),
            "estimated": trade.get("estimated", False),
        }
        db.insert_json_row(self.state_dir, "trades", {
            "mode": row["mode"], "date": row["date"], "symbol": row["symbol"],
            "technique": row["technique"], "exit_time": row["exit_time"], "pnl": row["pnl"],
        }, row)

    def record_equity(self, mode: str, day: str, allocation: float, realized: float, trades: int, balance: float) -> None:
        row = {
            "mode": mode, "date": day, "allocation": allocation,
            "realized": realized, "trades": trades, "balance": balance,
        }
        db.insert_json_row(self.state_dir, "equity", {"mode": mode, "date": day}, row)

    def _select(self, table: str, modes=None) -> list:
        conn = db.get_connection(self.state_dir)
        if modes is None:
            cur = conn.execute(f"SELECT data FROM {table} ORDER BY id")  # table 은 고정 상수만 온다
        else:
            modes = list(modes)
            placeholders = ", ".join("?" for _ in modes)
            cur = conn.execute(
                f"SELECT data FROM {table} WHERE mode IN ({placeholders}) ORDER BY id",  # noqa: S608
                tuple(modes),
            )
        return db.load_data_rows(cur.fetchall())

    def trades(self, modes=None) -> list:
        return self._select("trades", modes)

    def equity_rows(self, modes=None) -> list:
        return self._select("equity", modes)

    def daily(self, modes=None) -> list:
        rows = self.trades(modes)
        by_date: dict[str, list] = defaultdict(list)
        for r in rows:
            by_date[r["date"]].append(r)

        eq_by_date = {r["date"]: r for r in self.equity_rows(modes)}

        out = []
        cumulative = 0
        first_allocation = None
        for d in sorted(by_date.keys()):
            day_trades = by_date[d]
            pnl = sum(t["pnl"] for t in day_trades)
            wins = sum(1 for t in day_trades if t["pnl"] > 0)
            n = len(day_trades)
            cumulative += pnl

            eq = eq_by_date.get(d, {})
            allocation = eq.get("allocation")
            if first_allocation is None and allocation:
                first_allocation = allocation
            balance = eq.get("balance")

            out.append({
                "date": d, "pnl": pnl, "trades": n, "wins": wins,
                "last_exit_time": max((t.get("exit_time") or "" for t in day_trades), default="") or None,
                "win_rate": (wins / n) if n else 0.0,
                "return_pct": (pnl / allocation) if allocation else None,
                "cumulative": cumulative,
                "cumulative_return_pct": (cumulative / first_allocation) if first_allocation else None,
                "balance": balance, "allocation": allocation,
                "modes": sorted({t["mode"] for t in day_trades}),
            })
        return out

    def monthly(self, modes=None) -> list:
        daily_rows = self.daily(modes)
        by_month: dict[str, list] = defaultdict(list)
        for r in daily_rows:
            by_month[r["date"][:7]].append(r)

        out = []
        cumulative = 0
        for m in sorted(by_month.keys()):
            days = by_month[m]
            pnl = sum(d["pnl"] for d in days)
            trades = sum(d["trades"] for d in days)
            wins = sum(d["wins"] for d in days)
            up_days = sum(1 for d in days if d["pnl"] > 0)
            cumulative += pnl
            best = max(days, key=lambda d: d["pnl"])
            worst = min(days, key=lambda d: d["pnl"])
            out.append({
                "month": m, "pnl": pnl, "trades": trades, "wins": wins,
                "win_rate": (wins / trades) if trades else 0.0,
                "days": len(days), "up_days": up_days,
                "best": {"date": best["date"], "pnl": best["pnl"]},
                "worst": {"date": worst["date"], "pnl": worst["pnl"]},
                "cumulative": cumulative,
            })
        return out

    def yearly(self, modes=None) -> list:
        """monthly() 결과를 연도로 다시 묶는다.
        해가 바뀌면 시장도 규칙도 달라진다. 작년과 올해를 같은 표에서 비교할 수
        있어야 "올해가 나아졌는지"를 말할 수 있다.
        """
        months = self.monthly(modes)
        daily_rows = self.daily(modes)
        by_year: dict[str, list] = defaultdict(list)
        for m in months:
            by_year[m["month"][:4]].append(m)

        first_allocation = None
        for d in daily_rows:
            if d["allocation"] and first_allocation is None:
                first_allocation = d["allocation"]

        out = []
        cumulative = 0
        for y in sorted(by_year.keys()):
            year_months = by_year[y]
            pnl = sum(m["pnl"] for m in year_months)
            trades = sum(m["trades"] for m in year_months)
            wins = sum(m["wins"] for m in year_months)
            days = sum(m["days"] for m in year_months)
            up_days = sum(m["up_days"] for m in year_months)
            up_months = sum(1 for m in year_months if m["pnl"] > 0)
            cumulative += pnl
            best_month = max(year_months, key=lambda m: m["pnl"])
            worst_month = min(year_months, key=lambda m: m["pnl"])

            year_daily = [d for d in daily_rows if d["date"][:4] == y]
            best = max(year_daily, key=lambda d: d["pnl"]) if year_daily else None
            worst = min(year_daily, key=lambda d: d["pnl"]) if year_daily else None

            out.append({
                "year": y, "pnl": pnl, "trades": trades, "wins": wins,
                "win_rate": (wins / trades) if trades else 0.0,
                "days": days, "up_days": up_days,
                "months": len(year_months), "up_months": up_months,
                "best_month": {"month": best_month["month"], "pnl": best_month["pnl"]},
                "worst_month": {"month": worst_month["month"], "pnl": worst_month["pnl"]},
                "best": {"date": best["date"], "pnl": best["pnl"]} if best else None,
                "worst": {"date": worst["date"], "pnl": worst["pnl"]} if worst else None,
                "return_pct": (pnl / first_allocation) if first_allocation else None,
                "cumulative": cumulative,
            })
        return out

    def totals(self, modes=None, daily_rows=None) -> dict:
        daily_rows = daily_rows if daily_rows is not None else self.daily(modes)
        trades = self.trades(modes)
        n = len(trades)
        pnl = sum(t["pnl"] for t in trades)
        wins = [t["pnl"] for t in trades if t["pnl"] > 0]
        losses = [t["pnl"] for t in trades if t["pnl"] < 0]
        win_rate = (len(wins) / n) if n else 0.0
        avg_win = statistics.mean(wins) if wins else 0.0
        avg_loss = statistics.mean(losses) if losses else 0.0
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        # ★★★ 실제로 겪은 크래시 - 손실이 하나도 없으면 손익비가 수학적으로
        # 무한대인데, float("inf") 를 그대로 응답에 넣으면 표준 JSON 문법에
        # 없는 값이라 FastAPI 의 JSONResponse(allow_nan=False)가 직렬화하다
        # ValueError 로 죽는다(승률 100%인 기법이 하나만 있어도 /api/performance
        # 전체가 500 으로 죽었다). None 으로 보내고 화면이 "∞"로 표시한다 -
        # perf_stats.summarize_closed_trades() 가 이미 쓰는 것과 같은 방식이다.
        profit_factor = (gross_win / gross_loss) if gross_loss else (None if gross_win > 0 else 0.0)

        allocation, start_balance, end_balance = None, None, None
        for d in daily_rows:
            if d["allocation"] and allocation is None:
                allocation = d["allocation"]
            if d["balance"] is not None:
                if start_balance is None:
                    start_balance = d["balance"] - d["pnl"]
                end_balance = d["balance"]

        # MDD: 누적 손익 곡선의 최대 낙폭. ★ 거래 단위(청산 시각순)로 계산한다 - 화면이 해외·암호화폐·
        # 통합을 같은 방식으로 계산하고 by_technique() 도 거래 단위라, 국내만 "일별 누적" 기준이면
        # 같은 화면의 값끼리 서로 안 맞았다.
        mdd = 0.0
        peak = 0.0
        running = 0.0
        for t in sorted(trades, key=lambda r: r.get("exit_time") or ""):
            running += t["pnl"]
            peak = max(peak, running)
            mdd = min(mdd, running - peak)
        mdd_pct = (mdd / allocation) if allocation else None

        best = max(daily_rows, key=lambda d: d["pnl"]) if daily_rows else None
        worst = min(daily_rows, key=lambda d: d["pnl"]) if daily_rows else None

        return {
            "allocation": allocation, "start_balance": start_balance, "end_balance": end_balance,
            "cumulative_return_pct": (pnl / allocation) if allocation else None,
            "trades": n, "pnl": pnl, "win_rate": win_rate, "profit_factor": profit_factor,
            "avg_win": avg_win, "avg_loss": avg_loss, "mdd": mdd, "mdd_pct": mdd_pct,
            "best": {"date": best["date"], "pnl": best["pnl"]} if best else None,
            "worst": {"date": worst["date"], "pnl": worst["pnl"]} if worst else None,
            "days": len(daily_rows),
            "first_date": daily_rows[0]["date"] if daily_rows else None,
            "last_date": daily_rows[-1]["date"] if daily_rows else None,
        }

    def by_technique(self, modes=None) -> dict:
        """★ 기법별 거래수·승률·손익비·평균·총손익·mdd."""
        trades = self.trades(modes)
        by_tech: dict[str, list] = defaultdict(list)
        for t in trades:
            by_tech[t.get("technique") or "미상"].append(t)

        out = {}
        for tech, rows in by_tech.items():
            n = len(rows)
            pnl = sum(r["pnl"] for r in rows)
            wins = [r["pnl"] for r in rows if r["pnl"] > 0]
            losses = [r["pnl"] for r in rows if r["pnl"] < 0]
            gross_win = sum(wins)
            gross_loss = abs(sum(losses))
            # ★ totals() 와 같은 이유 - None 으로 보낸다(위 설명 참고).
            profit_factor = (gross_win / gross_loss) if gross_loss else (None if gross_win > 0 else 0.0)

            running, peak, mdd = 0.0, 0.0, 0.0
            for r in sorted(rows, key=lambda r: r.get("exit_time") or ""):
                running += r["pnl"]
                peak = max(peak, running)
                mdd = min(mdd, running - peak)

            out[tech] = {
                "last_time": max((r.get("exit_time") or "" for r in rows), default="") or None,
                "trades": n, "wins": len(wins),
                "win_rate": (len(wins) / n) if n else 0.0,
                "profit_factor": profit_factor,
                "avg_win": statistics.mean(wins) if wins else 0.0,
                "avg_loss": statistics.mean(losses) if losses else 0.0,
                "pnl": pnl, "mdd": mdd,
            }
        return out

    def by_mode(self) -> dict:
        trades = self.trades()
        by_mode: dict[str, list] = defaultdict(list)
        for t in trades:
            by_mode[t.get("mode", "")].append(t)

        out = {}
        for mode, rows in by_mode.items():
            n = len(rows)
            pnl = sum(r["pnl"] for r in rows)
            wins = sum(1 for r in rows if r["pnl"] > 0)
            out[mode] = {
                "label": MODE_LABELS.get(mode, mode), "trades": n, "pnl": pnl,
                "win_rate": (wins / n) if n else 0.0,
            }
        return out

    def reset(self, modes=None) -> None:
        """원장을 지운다. modes 를 주면 그 모드에 해당하는 줄만 지운다."""
        conn = db.get_connection(self.state_dir)
        if modes is None:
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM equity")
            return
        modes = list(modes)
        placeholders = ", ".join("?" for _ in modes)
        conn.execute(f"DELETE FROM trades WHERE mode IN ({placeholders})", tuple(modes))  # noqa: S608
        conn.execute(f"DELETE FROM equity WHERE mode IN ({placeholders})", tuple(modes))  # noqa: S608
