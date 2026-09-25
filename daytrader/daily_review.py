"""하루 매매 복기 - 장이 끝나는 시점에 그날의 매매를 정리해 텔레그램으로 보낸다.

  국내주식: 장 마감(15:30) 10분 뒤   해외주식: 한국시간 아침 06:00(직전 미국 거래일)   암호화폐: 매일 저녁 21:00(24시간 시장이라 마감이 없다)

내용: 성과(건수·승률·손익·손익비) · 기법별·청산 사유별 성적 · 추가 매수/분할 매도 사용 · 점검 포인트(규칙 기반) ·
      AI 는 쓰지 않는다(AI 는 뉴스·공시 위험 필터에만 쓴다).
"""

from __future__ import annotations

import html
import json
import logging
import os
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
MARKET_LABEL = {"domestic": "🇰🇷 국내주식", "overseas": "🌍 해외주식", "crypto": "🪙 암호화폐", "swing": "📈 스윙"}
_WEEKDAY_NUM = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
STOP_REASONS = ("stop_loss", "atr_stop", "breakeven_stop", "손절")


def _money(unit: str, v: float, sign: bool = False) -> str:
    if v is None:
        return "-"
    if unit == "usd":
        return f"{'+' if sign and v > 0 else ''}{'-' if v < 0 else ''}${abs(v):,.2f}"
    return f"{v:+,.0f}원" if sign else f"{v:,.0f}원"


def stats(trades: list) -> dict:
    """거래 목록(공통 형식)의 요약. 각 항목: pnl, name, technique, reason, adds, scaled_out, entry_price, exit_price."""
    rows = [t for t in trades if t.get("pnl") is not None]
    wins = [t for t in rows if t["pnl"] > 0]
    losses = [t for t in rows if t["pnl"] < 0]
    gw, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    by_tech: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in rows:
        b = by_tech[t.get("technique") or "미상"]
        b["n"] += 1
        b["wins"] += 1 if t["pnl"] > 0 else 0
        b["pnl"] += t["pnl"]
    return {
        "n": len(rows), "wins": len(wins), "losses": len(losses),
        "win_rate": (len(wins) / len(rows)) if rows else 0.0,
        "pnl": sum(t["pnl"] for t in rows),
        "profit_factor": (gw / gl) if gl else (None if gw > 0 else 0.0),
        "avg_win": (gw / len(wins)) if wins else 0.0, "avg_loss": (-gl / len(losses)) if losses else 0.0,
        "best": max(rows, key=lambda t: t["pnl"]) if rows else None,
        "worst": min(rows, key=lambda t: t["pnl"]) if rows else None,
        "by_tech": dict(by_tech),
        "by_reason": dict(Counter((t.get("reason") or "?").split(" (")[0] for t in rows)),
        "adds_used": sum(1 for t in rows if t.get("adds")), "scaled_used": sum(1 for t in rows if t.get("scaled_out")),
    }


def observations(market: str, s: dict, stop_pct: float, blocked: int = 0) -> list:
    """규칙 기반 점검 포인트(최대 4개)."""
    out: list = []
    if s["n"] == 0:
        return ["오늘은 청산된 거래가 없습니다."]
    pf = s["profit_factor"]
    if pf is not None and pf < 1.0 and s["losses"]:
        out.append(f"손실 합이 이익 합보다 컸습니다(손익비 {pf:.2f}). 손절 뒤 곧바로 되돌린 진입이 없었는지 확인하세요.")
    stops = sum(v for k, v in s["by_reason"].items() if any(w in k for w in STOP_REASONS))
    if s["losses"] and stops >= max(2, 0.6 * s["n"]):
        out.append(f"손절·본전 청산이 {stops}건으로 절반이 넘습니다. 진입 직후 반대로 움직인 종목이 많았습니다.")
    if s["by_tech"]:
        tech, b = min(s["by_tech"].items(), key=lambda kv: kv[1]["pnl"])
        if b["pnl"] < 0 and b["n"] >= 2:
            out.append(f"'{tech}' 기법이 {b['n']}건에서 가장 부진했습니다(승 {b['wins']}). 표본이 작아 단정은 이릅니다.")
    w = s["worst"]
    if w and w.get("entry_price") and w.get("exit_price") and stop_pct:
        loss_pct = w["exit_price"] / w["entry_price"] - 1.0
        if loss_pct < -stop_pct * 1.5:
            out.append(f"최대 손실 거래({w.get('name', '')})가 {loss_pct * 100:.1f}%로 손절폭({stop_pct * 100:.1f}%)을 크게 넘었습니다. 갭·슬리피지를 확인하세요.")
    if s["adds_used"] or s["scaled_used"]:
        out.append(f"추가 매수 {s['adds_used']}건 · 분할 매도 {s['scaled_used']}건이 쓰였습니다.")
    if blocked:
        out.append(f"뉴스 위험 필터가 {blocked}종목의 매수를 막았습니다.")
    if not out:
        out.append("특이한 점검 사항은 없습니다.")
    return out[:4]


def compose(market: str, day_label: str, s: dict, positions: list, notes: list, unit: str, live: bool) -> str:
    """텔레그램(HTML) 메시지. 계좌번호·키는 넣지 않는다."""
    e = html.escape
    mode = "실거래" if live else "모의매매 · 가상 성적입니다"
    lines = [f"📋 <b>{MARKET_LABEL.get(market, market)} 일일 복기</b> {e(day_label)} [{mode}]"]
    if s["n"] == 0:
        lines.append("청산된 거래가 없습니다.")
    else:
        pf = s["profit_factor"]
        pf_txt = "∞" if pf is None else f"{pf:.2f}"
        lines.append(f"손익 <b>{_money(unit, s['pnl'], True)}</b> · 거래 {s['n']}건(승 {s['wins']}·패 {s['losses']}) · 승률 {s['win_rate'] * 100:.0f}% · 손익비 {pf_txt}")
        lines.append(f"평균 이익 {_money(unit, s['avg_win'])} · 평균 손실 {_money(unit, s['avg_loss'])}")
        if s["best"]:
            lines.append(f"최고 {e(str(s['best'].get('name', '')))} {_money(unit, s['best']['pnl'], True)}")
        if s["worst"] and s["worst"]["pnl"] < 0:
            lines.append(f"최저 {e(str(s['worst'].get('name', '')))} {_money(unit, s['worst']['pnl'], True)}")
        techs = sorted(s["by_tech"].items(), key=lambda kv: -kv[1]["n"])[:4]
        if techs:
            lines.append("기법: " + " · ".join(f"{e(k)} {v['n']}건({_money(unit, v['pnl'], True)})" for k, v in techs))
        reasons = sorted(s["by_reason"].items(), key=lambda kv: -kv[1])[:4]
        if reasons:
            lines.append("청산: " + " · ".join(f"{e(k)} {v}" for k, v in reasons))
    if positions:
        lines.append("보유 중: " + ", ".join(e(f"{p['name']} {p['pnl']:+,.2f}" if unit == "usd" else f"{p['name']} {p['pnl']:+,.0f}") for p in positions[:6]))
    lines.append("")
    lines.append("<b>점검</b>")
    lines += [f"• {e(n)}" for n in notes]
    return "\n".join(lines)


# ━━ 언제 보내나 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def due_markets(now_kst: datetime, cfg, sent: dict, *, us_trading_day, kr_trading_day) -> list:
    """지금 보내야 할 (시장, 키) 목록. 트리거 시각 이후 3시간 안에서 아직 안 보낸 것만.
    us_trading_day(date_ny)/kr_trading_day(date_kst) 는 그날이 거래일인지(주말·휴장 제외) 알려 주는 함수."""
    from daytrader.overseas_engine import _to_ny
    out = []
    # 국내: 장 마감 15:30 + 10분
    if kr_trading_day(now_kst.date()):
        trig = now_kst.replace(hour=15, minute=40, second=0, microsecond=0)
        key = f"domestic-{now_kst.date()}"
        if trig <= now_kst < trig + timedelta(hours=3) and key not in sent:
            out.append(("domestic", key, str(now_kst.date())))
    # 해외: 한국시간 아침(기본 06:00)에 직전 미국 거래일을 복기한다(뉴욕 정규장 마감은 한국시간 05:00~06:00).
    try:
        hh, mm = str(getattr(cfg.notify, "overseas_review_time", "06:00")).split(":")
        trig = now_kst.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except Exception:
        trig = now_kst.replace(hour=6, minute=0, second=0, microsecond=0)
    ny_date = _to_ny(trig).date()  # 그 시각의 뉴욕 날짜 = 방금 끝난 미국 세션의 날짜
    key = f"overseas-{ny_date}"
    if us_trading_day(ny_date) and trig <= now_kst < trig + timedelta(hours=3) and key not in sent:
        out.append(("overseas", key, f"미국 {ny_date} 세션"))
    # 암호화폐: 매일 저녁(기본 21:00 KST). "암호화폐는 일2회 보내고" 요청에 따라 두 번째
    # 시각(crypto_review_time2)을 설정했으면 하루에 한 번 더 보낸다 - 키에 시각을 넣어
    # 첫 번째 발송과 서로 다른 건으로 취급한다(비워두면 예전처럼 하루 한 번만 보낸다).
    try:
        hh, mm = str(getattr(cfg.notify, "crypto_review_time", "21:00")).split(":")
        trig = now_kst.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except Exception:
        trig = now_kst.replace(hour=21, minute=0, second=0, microsecond=0)
    key = f"crypto-{now_kst.date()}"
    if trig <= now_kst < trig + timedelta(hours=3) and key not in sent:
        out.append(("crypto", key, f"{now_kst.date()} {trig.strftime('%H:%M')} 기준 24시간"))
    time2 = str(getattr(cfg.notify, "crypto_review_time2", "") or "").strip()
    if time2:
        try:
            hh2, mm2 = time2.split(":")
            trig2 = now_kst.replace(hour=int(hh2), minute=int(mm2), second=0, microsecond=0)
        except Exception:
            trig2 = None
        if trig2 is not None:
            key2 = f"crypto2-{now_kst.date()}"
            if trig2 <= now_kst < trig2 + timedelta(hours=3) and key2 not in sent:
                out.append(("crypto", key2, f"{now_kst.date()} {trig2.strftime('%H:%M')} 기준 24시간"))
    return out


def due_periodic(now_kst: datetime, cfg, sent: dict) -> list:
    """["암호화폐는 일2회 보내고, 전체 주, 월 보내라고"] 요청의 주간·월간 부분.
    4개 시장을 한 메시지에 묶어 보내는 주기적 복기가 지금 보내야 할 때인지 판단한다.
    (period_key, period_label, since_date, until_date, since_ts, until_ts) 목록을 돌려준다 -
    since_date/until_date 는 국내주식(날짜 문자열 기록)용, since_ts/until_ts 는 나머지
    세 시장(exit_time 이 epoch 로 기록됨)용이다."""
    out = []
    today = now_kst.date()

    if getattr(cfg.notify, "weekly_review_enabled", False):
        want_day = _WEEKDAY_NUM.get(str(getattr(cfg.notify, "weekly_review_day", "mon")), 0)
        try:
            hh, mm = str(getattr(cfg.notify, "weekly_review_time", "08:00")).split(":")
            trig = now_kst.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except Exception:
            trig = now_kst.replace(hour=8, minute=0, second=0, microsecond=0)
        key = f"weekly-{today.isocalendar()[0]}-W{today.isocalendar()[1]}"
        if today.weekday() == want_day and trig <= now_kst < trig + timedelta(hours=3) and key not in sent:
            since_date = today - timedelta(days=7)
            until_date = today - timedelta(days=1)
            since_ts = datetime.combine(since_date, datetime.min.time(), KST).timestamp()
            until_ts = datetime.combine(today, datetime.min.time(), KST).timestamp()
            out.append(("weekly", key, f"주간 복기 {since_date}~{until_date}", str(since_date), str(until_date), since_ts, until_ts))

    if getattr(cfg.notify, "monthly_review_enabled", False):
        want_day = int(getattr(cfg.notify, "monthly_review_day", 1) or 1)
        try:
            hh, mm = str(getattr(cfg.notify, "monthly_review_time", "08:00")).split(":")
            trig = now_kst.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except Exception:
            trig = now_kst.replace(hour=8, minute=0, second=0, microsecond=0)
        key = f"monthly-{today.year}-{today.month:02d}"
        if today.day == want_day and trig <= now_kst < trig + timedelta(hours=3) and key not in sent:
            # 지난 달 전체(1일~말일)를 복기한다.
            first_of_this_month = today.replace(day=1)
            last_of_prev_month = first_of_this_month - timedelta(days=1)
            since_date = last_of_prev_month.replace(day=1)
            until_date = last_of_prev_month
            since_ts = datetime.combine(since_date, datetime.min.time(), KST).timestamp()
            until_ts = datetime.combine(first_of_this_month, datetime.min.time(), KST).timestamp()
            out.append(("monthly", key, f"월간 복기 {since_date.strftime('%Y-%m')}", str(since_date), str(until_date), since_ts, until_ts))

    return out


def compose_period(period_label: str, per_market: dict) -> str:
    """주간·월간처럼 여러 날을 묶어 4개 시장을 한 메시지에 요약한다.
    per_market = {market: (stats(trades로 계산한 dict), unit, is_live)}.

    ★ 시장마다 통화 단위가 달라(국내·암호화폐·스윙은 원, 해외주식은 달러) 하나의 숫자로
    합산하지 않는다 - 시장별로 줄을 나눠 보여준다(daily_review.compose() 와 같은 원칙)."""
    lines = [f"🗓 <b>{period_label}</b>"]
    any_trades = False
    for market in ("domestic", "overseas", "crypto", "swing"):
        if market not in per_market:
            continue
        s, unit, live = per_market[market]
        label = MARKET_LABEL.get(market, market)
        if s["n"] == 0:
            lines.append(f"{label}: 거래 없음")
            continue
        any_trades = True
        pf = s["profit_factor"]
        pf_txt = "∞" if pf is None else f"{pf:.2f}"
        mode_txt = "" if live else "(모의)"
        lines.append(
            f"{label}{mode_txt}: 손익 <b>{_money(unit, s['pnl'], True)}</b> · 거래 {s['n']}건"
            f"(승 {s['wins']}·패 {s['losses']}) · 승률 {s['win_rate'] * 100:.0f}% · 손익비 {pf_txt}"
        )
    if not any_trades:
        lines.append("이 기간 청산된 거래가 없습니다.")
    return "\n".join(lines)


class SentLog:
    """보낸 복기를 파일에 남겨 서버를 다시 켜도 같은 복기를 두 번 보내지 않게 한다."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def mark(self, key: str) -> None:
        with self._lock:
            data = self.load()
            cutoff = time.time() - 14 * 86400
            data = {k: v for k, v in data.items() if isinstance(v, (int, float)) and v >= cutoff}
            data[key] = time.time()
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
