"""해외주식(미국) 자동매매 엔진.

★★★ 사용자 요청 그대로: 매매 기법은 국내주식과 완전히 동일(Playbook을
그대로 재사용 - entry_order/exit_enabled 도 국내주식 설정을 그대로 따른다),
계좌도 국내주식과 완전히 동일(같은 TossClient, 같은 X-Tossinvest-Account).
다르게 두는 것은 딱 3가지뿐이다:
  1. 실제 매매·포지션 기록 - OverseasState 로 완전히 별도 저장(국내주식
     journal/ledger 와 안 섞는다).
  2. 종목(테마 스크리닝 대신 cfg.overseas.watchlist 관심종목을 그대로 씀).
  3. 거래 시간 - 미국은 평일 24시간(주말·NYSE 휴장일 제외, 서머타임 자동 반영). 프리장·본장·애프터장·
     데이장(야간거래) 4개 세션을 cfg.overseas.trade_premarket/trade_regular/trade_afterhours/
     trade_overnight 로 각각 켜고 끌 수 있다.

★ Playbook.evaluate_entry/evaluate_exit 이 기대하는 ctx 필드 중 "테마"
관련(theme_rank·theme_breadth·theme_intensity·theme_bars)은 해외주식에
의미가 없어 채우지 않는다 - playbook.py 전체가 getattr(ctx, key, 기본값)
으로 안전하게 접근하도록 이미 짜여 있어서(원래 국내주식 코드), 없어도
그 항목만 자연스럽게 "조건 미충족"으로 처리되고 프로그램이 죽지 않는다.
그래서 테마 의존적인 theme_leader 기법은 해외주식에서는 사실상 항상
통과하지 못한다 - 의도된 동작이다(테마가 없는 시장에 테마 기법을 억지로
맞추지 않는다).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone
from types import SimpleNamespace

from daytrader.overseas_broker import (
    LiveOverseasBroker, NotOwnedError, OverseasPosition, OverseasPositionBook, PaperOverseasBroker,
)
from daytrader import news_guard, sizing
from daytrader.orders import OrderBook
from daytrader.perf_stats import summarize_closed_trades
from daytrader.playbook import Bar, Playbook
from daytrader.timeutil import KST, day_str, now_kst as _now_kst_aware

log = logging.getLogger(__name__)

# ★★★ 실제로 겪은 버그: ZoneInfo("America/New_York") 를 모듈 최상단에서
# 곧바로 만들면, Windows 처럼 시스템에 IANA 시간대 DB가 없는 환경에서는
# "No time zone found with key America/New_York" 예외가 나면서 이 모듈을
# import 하는 순간(=해외주식 시작 버튼을 누르는 순간, 지연 import 라서)
# 곧바로 죽는다. requirements.txt 에 tzdata 패키지를 추가했지만, 혹시
# exe 빌드에 빠졌거나 어떤 이유로든 없을 때도 프로그램이 죽지 않도록,
# 지연 생성 + 실패 시 서머타임을 직접 계산하는 폴백을 둔다.
_NY_TZ = None
_NY_TZ_FAILED = False


def _get_ny_tz():
    global _NY_TZ, _NY_TZ_FAILED
    if _NY_TZ is not None or _NY_TZ_FAILED:
        return _NY_TZ
    try:
        from zoneinfo import ZoneInfo
        _NY_TZ = ZoneInfo("America/New_York")
    except Exception as exc:
        _NY_TZ_FAILED = True
        log.warning(
            "미국 동부 시간대(America/New_York)를 zoneinfo 로 찾지 못했습니다(%s) - "
            "'pip install tzdata' 로 해결됩니다. 그때까지는 서머타임 규칙을 직접 계산합니다.",
            exc,
        )
    return _NY_TZ


def _us_eastern_offset_hours(now_utc: datetime) -> int:
    """★ zoneinfo 를 아예 못 쓸 때의 폴백 - 미국 서머타임 규칙(3월 둘째
    일요일 02:00 ~ 11월 첫째 일요일 02:00, 현지시각 기준)을 직접 계산해
    UTC 와의 시차를 돌려준다. 정상 상황에서는 절대 여기까지 오지 않는다
    (zoneinfo+tzdata 조합이면 항상 성공한다).
    """
    year = now_utc.year

    def _nth_sunday(y: int, month: int, n: int) -> datetime:
        d = datetime(y, month, 1, tzinfo=timezone.utc)
        first_sunday_offset = (6 - d.weekday()) % 7
        return d + timedelta(days=first_sunday_offset + 7 * (n - 1))

    dst_start = _nth_sunday(year, 3, 2) + timedelta(hours=7)   # 3월 둘째 일요일 02:00 EST(UTC-5) = 07:00 UTC
    dst_end = _nth_sunday(year, 11, 1) + timedelta(hours=6)    # 11월 첫째 일요일 02:00 EDT(UTC-4) = 06:00 UTC
    return -4 if dst_start <= now_utc < dst_end else -5


def _to_ny(now_kst: datetime) -> datetime:
    """뉴욕 현지 시각으로. zoneinfo 가 서머타임을 자동 반영한다(없으면 직접 계산한 폴백)."""
    ny_tz = _get_ny_tz()
    if ny_tz is not None:
        return now_kst.astimezone(ny_tz)
    now_utc = now_kst.astimezone(timezone.utc)
    return now_utc + timedelta(hours=_us_eastern_offset_hours(now_utc))


def _nth_weekday(year: int, month: int, weekday: int, n: int):
    """month 의 n 번째 weekday(월=0). n=-1 이면 마지막."""
    from datetime import date
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))
        return d
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int):
    from datetime import date
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


_holiday_cache: dict = {}


def us_market_holidays(year: int) -> set:
    """★ 뉴욕증권거래소(NYSE) 전일 휴장일. 토요일이면 앞 금요일, 일요일이면 뒤 월요일로 옮겨 쉬는
    규칙을 따른다(단 새해는 토요일이면 쉬지 않는다). 조기 폐장(반일)은 반영하지 않는다."""
    if year in _holiday_cache:
        return _holiday_cache[year]
    from datetime import date

    def observed(d):
        if d.weekday() == 5:
            return d - timedelta(days=1)
        if d.weekday() == 6:
            return d + timedelta(days=1)
        return d

    days = set()
    ny = date(year, 1, 1)
    if ny.weekday() != 5:
        days.add(observed(ny))
    days.add(_nth_weekday(year, 1, 0, 3))                     # 마틴 루터 킹 주니어의 날
    days.add(_nth_weekday(year, 2, 0, 3))                     # 대통령의 날
    days.add(_easter(year) - timedelta(days=2))               # 성금요일
    days.add(_nth_weekday(year, 5, 0, -1))                    # 메모리얼 데이
    if year >= 2022:
        days.add(observed(date(year, 6, 19)))                 # 준틴스
    days.add(observed(date(year, 7, 4)))                      # 독립기념일
    days.add(_nth_weekday(year, 9, 0, 1))                     # 노동절
    days.add(_nth_weekday(year, 11, 3, 4))                    # 추수감사절
    days.add(observed(date(year, 12, 25)))                    # 크리스마스
    _holiday_cache[year] = days
    return days


def us_session(now_kst: datetime) -> str:
    """★ 미국 주식의 지금 거래 가능 상태: "regular"(정규장 09:30~16:00 뉴욕) | "extended"(정규장 밖 -
    주간·프리마켓·애프터·야간) | "closed"(주말·휴장일).

    미국 주식은 평일 24시간 거래된다(일요일 저녁 8시 뉴욕시각에 시작해 금요일 저녁 8시에 끝난다). 저녁
    8시 이후는 다음 날 세션에 속하므로, 그 "세션 날짜"가 주말이거나 NYSE 휴장일이면 닫힌 것이다.
    """
    now_ny = _to_ny(now_kst)
    session_date = now_ny.date() + (timedelta(days=1) if now_ny.time() >= dtime(20, 0) else timedelta(0))
    if session_date.weekday() >= 5 or session_date in us_market_holidays(session_date.year):
        return "closed"
    if now_ny.date() == session_date and dtime(9, 30) <= now_ny.time() < dtime(16, 0):
        return "regular"
    return "extended"


PHASE_LABELS = {
    "regular": "정규장", "premarket": "프리마켓", "afterhours": "애프터마켓", "overnight": "야간·주간거래", "closed": "휴장",
}


def us_phase(now_kst: datetime) -> str:
    """★ 미국 시장의 현재 국면(장이 바뀔 때마다 테마주를 다시 뽑는 기준):
    regular(09:30~16:00) · afterhours(16:00~20:00) · overnight(20:00~04:00, 한국의 주간거래 시간대) ·
    premarket(04:00~09:30) · closed(주말·휴장일). 시각은 모두 뉴욕 기준(서머타임 자동 반영)."""
    if us_session(now_kst) == "closed":
        return "closed"
    t = _to_ny(now_kst).time()
    if dtime(9, 30) <= t < dtime(16, 0):
        return "regular"
    if dtime(16, 0) <= t < dtime(20, 0):
        return "afterhours"
    if t >= dtime(20, 0) or t < dtime(4, 0):
        return "overnight"
    return "premarket"


def is_us_market_open(now_kst: datetime) -> bool:
    """미국 정규장(09:30~16:00 뉴욕시각) 여부 - 주말·NYSE 휴장일은 제외."""
    return us_session(now_kst) == "regular"


def is_us_tradable(
    now_kst: datetime, *, trade_premarket: bool = True, trade_regular: bool = True,
    trade_afterhours: bool = True, trade_overnight: bool = True,
) -> bool:
    """신규 진입 탐색을 해도 되는 시간인가(청산 관리는 이 값과 무관하게 항상 계속된다).

    ★★★ "본장·프리장·애프터장·데이장 등 장 별로 거래를 할지 사용자가 선택하게 해" 요청에
    따라, 예전의 뭉뚱그린 trade_24h(24시간 전부 켜기/정규장만 켜기) 대신 4개 세션을 각각
    따로 켜고 끈다. 주말·휴장일(us_phase()=="closed")은 어떤 조합이든 항상 거래 불가다."""
    phase = us_phase(now_kst)
    flags = {
        "premarket": trade_premarket, "regular": trade_regular,
        "afterhours": trade_afterhours, "overnight": trade_overnight,
    }
    return flags.get(phase, False)


def _bar_age_seconds(bar):
    """마지막 봉이 몇 초 전 것인지. 시각을 못 읽으면 None(막지 않는다)."""
    ts = str(getattr(bar, "ts", "") or "")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


_FALLBACK_USD_KRW = 1400.0
_usd_krw_cache: dict = {"rate": None, "at": 0.0}


def _usd_krw_rate() -> float:
    """★★★ 원화 배정금액을 달러 주가로 나누려면 반드시 환율이 필요하다 -
    이게 빠져서 수량이 환율 배수(약 1,400배)만큼 부풀려지는 버그가 있었다.
    ★ 환율 조회는 실패할 수 있으니(네트워크·API 장애) 절대 예외를 밖으로
    내지 않는다. 실패하면 보수적 기본값을 쓴다 - 환율이 조금 틀린 것보다
    매수 자체가 죽는 게 더 나쁘다. 10분 캐시한다(환율은 초 단위로 볼
    필요가 없다).
    """
    now = time.time()
    if _usd_krw_cache["rate"] and (now - _usd_krw_cache["at"]) < 600:
        return _usd_krw_cache["rate"]
    try:
        from daytrader import market as market_mod
        snap = market_mod.snapshot()
        if not isinstance(snap, dict):
            raise ValueError("시장 스냅샷 형식이 올바르지 않습니다.")
        for group in snap.get("groups", []):
            if not isinstance(group, dict):
                continue
            for row in group.get("rows", []):
                # ★ 같은 이유 - 딕셔너리가 아닌 행이 섞여도 죽지 않게.
                if not isinstance(row, dict):
                    continue
                if str(row.get("label", "")).startswith("원/달러") and row.get("last"):
                    rate = float(row["last"])
                    if 500 < rate < 5000:  # ★ 말도 안 되는 값이면 안 믿는다.
                        _usd_krw_cache.update({"rate": rate, "at": now})
                        return rate
    except Exception as exc:
        log.warning("환율 조회 실패(%s) - 기본값 %.0f원을 씁니다.", exc, _FALLBACK_USD_KRW)
    _usd_krw_cache.update({"rate": _FALLBACK_USD_KRW, "at": now})
    return _FALLBACK_USD_KRW


def _where(exc) -> str:
    """★★★ "마지막 오류"에 메시지만 남으면 어느 줄에서 났는지 알 수 없어
    같은 오류를 몇 번이나 헛짚게 된다(실제로 겪음). 발생 지점을
    파일:줄 로 함께 남겨서 한 번에 찾을 수 있게 한다.
    """
    import traceback
    tb = exc.__traceback__
    last = None
    while tb is not None:
        fn = tb.tb_frame.f_code.co_filename
        # ★ 우리 코드에서 난 마지막 지점이 원인이다(라이브러리 안쪽이 아니라).
        if "daytrader" in fn:
            last = (os.path.basename(fn), tb.tb_lineno, tb.tb_frame.f_code.co_name)
        tb = tb.tb_next
    return f" [{last[0]}:{last[1]} {last[2]}()]" if last else ""


def _is_us_ticker(symbol) -> bool:
    """★★★ 미국 티커인지 형식으로 판별한다. 국내 종목코드(010620 같은
    6자리 숫자)가 해외 감시목록에 섞여 들어가 야후에서 404 가 나던 문제
    ("시세조회 실패, 종목명이 안 나옴")를 막는다.

    ★ 미국 티커는 알파벳으로 시작하고 알파벳·점·하이픈으로 이뤄진다
    (AAPL, BRK.B, BF-A 등). 숫자로만 된 심볼은 국내 종목코드다.
    ★ API 응답을 그대로 믿지 않는 이유 - marketCountry="US" 로 요청해도
    국내 코드가 섞여 오는 경우를 실제로 겪었다.
    """
    if not symbol:
        return False
    s = str(symbol).strip()
    if not s or not s[0].isalpha():
        return False
    return all(ch.isalpha() or ch in ".-" for ch in s)


def _now_ts() -> float:
    return time.time()


def _fmt_ts(ts: float) -> str:
    """★ 재개 예정 시각을 사람이 읽는 형식으로(코인 엔진의 같은 이름 함수와 동일한 취지)."""
    try:
        dt = datetime.fromtimestamp(ts)
        if dt.date() == datetime.now().date():
            return dt.strftime("%H:%M")
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return "-"


def _is_today_ts(ts) -> bool:
    """★★★ 실제로 겪을 뻔한 버그 - datetime.now()(호스트 로컬 시각)로 "오늘"을
    가르면, UTC 호스트에서는 자정이 한국 시각 오전 9시가 되어 장중에 일일
    손실 한도·거래횟수가 조용히 리셋된다. timeutil 이 강제하는 KST 기준으로
    통일한다.
    """
    if not ts:
        return False
    try:
        return day_str(datetime.fromtimestamp(float(ts), tz=KST)) == day_str(_now_kst_aware())
    except Exception:
        return False


def _closed_today(closed: list, limit: int = 500) -> list:
    """오늘(KST 자정 이후)에 청산된 거래. exit_time 은 유닉스 초.
    ★ 위와 같은 이유로 호스트 로컬 자정이 아니라 KST 자정 기준이어야 한다."""
    start = _now_kst_aware().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    out = [c for c in (closed or []) if (c.get("exit_time") or 0) >= start]
    return out[-limit:]


class OverseasState:
    """포지션 장부 + 청산 기록을 JSON 파일에 남긴다 - 국내주식 journal/ledger 와 완전히 별도다.

    ★★★ 실제로 겪은 버그 - 이 엔진에 쿨다운·일일 거래한도·연속손절 중단이
    전혀 없어서(국내주식 engine.py 에는 다 있는데 여기만 빠짐), 일봉 기준
    돌파 신호가 하루 종일 그대로 살아 있는 상태에서 손절 나가자마자 바로
    재매수가 반복됐다(6시간에 같은 종목만 39번 거래). trades/cooldown/
    consecutive_losses 를 국내주식과 같은 개념으로 여기 추가한다.
    """

    def __init__(self, path: str):
        self.path = path
        self.book = OverseasPositionBook()
        self.closed: list = []
        self.date: str = ""
        self.trades: int = 0
        self.cooldown: dict = {}  # symbol -> 재진입 가능 시각(초 단위 timestamp)
        self.consecutive_losses: int = 0
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            try:
                os.rename(self.path, f"{self.path}.corrupt-{int(time.time())}")
            except Exception:
                pass
            return
        if not isinstance(data, dict):
            return
        for symbol, p in (data.get("positions") or {}).items():
            # ★★★ 실제로 겪은 버그("'str' object has no attribute 'get'") -
            # 상태 파일이 손상되거나 예전 형식이면 값이 딕셔너리가 아닐 수
            # 있다. 그대로 p.get() 을 부르면 엔진이 시작조차 못 하고,
            # "마지막 오류"에 그 메시지만 남아 원인을 알 수 없었다.
            # ★ 한 종목이 깨졌다고 나머지까지 버리지 않는다 - 건너뛴다.
            if not isinstance(p, dict):
                log.warning("해외주식 상태 복원 - %s 기록 형식이 올바르지 않아 건너뜁니다(%s).",
                            symbol, type(p).__name__)
                continue
            try:
                self.book.record_buy(OverseasPosition(
                    symbol=symbol, quantity=p["quantity"], entry_price=p["entry_price"],
                    entry_time=p["entry_time"], peak_price=p["peak_price"],
                    technique=p.get("technique", ""), order_id=p.get("order_id"),
                    name=p.get("name", symbol), theme=p.get("theme", "해외주식"),
                    adds=int(p.get("adds") or 0), last_fill_price=float(p.get("last_fill_price") or p["entry_price"] or 0.0),
                    scaled_out=int(p.get("scaled_out") or 0), invested=float(p.get("invested") or 0.0),
                    realized=float(p.get("realized") or 0.0),
                    sold_qty=float(p.get("sold_qty") or 0.0), sold_value=float(p.get("sold_value") or 0.0),
                    conviction=float(p.get("conviction") if p.get("conviction") is not None else 0.5),
                ))
            except (KeyError, TypeError) as exc:
                log.warning("해외주식 상태 복원 - %s 기록에 빠진 값이 있어 건너뜁니다: %s", symbol, exc)
        closed = data.get("closed", [])
        # ★ 청산 기록도 딕셔너리만 남긴다 - 화면·집계가 .get() 을 쓴다.
        self.closed = [c for c in closed if isinstance(c, dict)] if isinstance(closed, list) else []
        self.date = data.get("date", "")
        self.trades = int(data.get("trades", 0) or 0)
        cooldown = data.get("cooldown", {})
        self.cooldown = cooldown if isinstance(cooldown, dict) else {}
        self.consecutive_losses = int(data.get("consecutive_losses", 0) or 0)
        self._reset_if_new_day()

    def _reset_if_new_day(self) -> None:
        """★ 미국 정규장도 하루 단위 세션이라, 국내주식처럼 날짜가 바뀌면
        오늘의 거래횟수·쿨다운을 리셋한다(코인의 24시간 롤링 방식과는
        다르게, 여기는 '거래일'이라는 개념이 뚜렷해서 이 편이 더 명확하다).

        ★★★ 실제로 겪을 뻔한 버그 - datetime.now()(호스트 로컬 시각)로 날짜를
        가르면, UTC 호스트에서는 한국 장중(오전 9시)에 날짜가 이미 넘어가 있어
        일일 손실 한도·거래횟수가 장중에 조용히 리셋된다. KST 기준으로 통일한다.
        """
        today = day_str(_now_kst_aware())
        if self.date != today:
            self.date = today
            self.trades = 0
            self.cooldown = {}
            self.consecutive_losses = 0

    def save(self) -> None:
        positions = {
            s: {
                "quantity": p.quantity, "entry_price": p.entry_price, "entry_time": p.entry_time,
                "peak_price": p.peak_price, "technique": p.technique, "order_id": p.order_id,
                "adds": p.adds, "last_fill_price": p.last_fill_price, "scaled_out": p.scaled_out,
                "invested": p.invested, "realized": p.realized, "sold_qty": p.sold_qty, "sold_value": p.sold_value, "conviction": p.conviction,
            }
            for s, p in self.book.all().items()
        }
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "positions": positions, "closed": self.closed, "date": self.date,
                "trades": self.trades, "cooldown": self.cooldown,
                "consecutive_losses": self.consecutive_losses,
            }, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


class OverseasEngine:
    def __init__(self, cfg, client=None, state_path: str | None = None, notifier=None):
        self.cfg = cfg
        self.overseas_cfg = cfg.overseas
        # ★★★ "계좌도 동일" - 국내주식과 같은 TossClient 를 그대로 받는다.
        # ★★★ 다만 sim(시뮬레이션) 모드는 예외다. 실제로 겪은 문제 -
        # sim 인데도 진짜 토스 API 를 호출해서, API 키가 없거나 네트워크가
        # 막히면 아무것도 못 하고 조용히 실패했다("시뮬레이션이 전혀 동작
        # 안 한다"는 문의의 정체). 국내주식의 sim 이 SimClient 로 합성
        # 시세를 만들어 도는 것과 같은 취지로, 여기서도 합성 시세를 쓴다.
        if self.overseas_cfg.mode == "sim" and client is None:
            from daytrader.sim_feed import SimFeedClient
            self.client = SimFeedClient(seed=getattr(cfg.simulation, "seed", 42))
        else:
            self.client = client

        state_path = state_path or os.path.join(cfg.state_dir, "overseas_state.json")
        self.state = OverseasState(state_path)

        # ★ 국내주식과 완전히 동일한 기법 선택을 그대로 쓴다(사용자 요청) - entry_order/exit_enabled 는 공유.
        # ★★★ 손절·익절·추적폭·최대보유시간은 분리 - 암호화폐에서 먼저 겪은 것과 같은 버그
        # ("코인 청산이 전부 90분 시간손절로만 찍힌다")를 막으려고 Playbook 이 시장별
        # risk/max_hold_minutes 를 받을 수 있게 돼 있는데, 해외주식만 여태 안 넘기고
        # cfg.risk(국내) 를 그대로 썼다. 원화·달러, 변동성이 다른 시장인데 같은 폭을 쓰는 게
        # 맞느냐는 지적으로 overseas.* 전용 값을 분리한다(기본값은 국내와 동일하게 시작).
        overseas_risk = SimpleNamespace(
            stop_loss_pct=cfg.overseas.stop_loss_pct,
            take_profit_pct=cfg.overseas.take_profit_pct,
            trailing_stop_pct=cfg.overseas.trailing_pct,
            trailing_arm_pct=cfg.overseas.trailing_arm_pct,
        )
        self.playbook = Playbook(
            cfg, risk=overseas_risk, max_hold_minutes=cfg.overseas.max_hold_minutes, market="overseas",
            learning_mode=cfg.overseas.technique_learning_mode,
        )

        self.is_live = (self.overseas_cfg.mode == "live") and bool(cfg.client_id and cfg.client_secret)
        if self.is_live:
            # ★★★ [1-5] 국내주식과 같은 이중 주문 방지 설계 - 별도 디렉터리에
            # 주문 의도를 남긴다(국내주식 orders.jsonl 과 절대 안 섞이게).
            order_book = OrderBook(os.path.join(cfg.state_dir, "overseas_orders"))
            self.broker = LiveOverseasBroker(self.client, book=self.state.book, order_book=order_book)
        else:
            # ★★★ 실제로 겪은 버그(국내주식 engine.py 에서 먼저 발견돼 고쳐진 것과 같은 종류) -
            # 재시작할 때마다 모의매매 현금이 그동안의 손익과 무관하게 매번 총 투자금액 그대로
            # 초기화됐다("대시보드 현금 잔여금액이 안 맞다"는 문의의 원인). 보유 중인 종목은
            # 복원하면서 그 종목을 사는 데 쓴 현금은 되돌려주지 않은 것 - 포지션과 그
            # 값어치만큼의 현금이 동시에 존재하는 이중 계산이었다. 지금까지 실현손익을 더하고,
            # 지금 보유 중인 포지션에 묶여 있는 돈(매수 금액 - 이미 분할 매도로 받은 돈)을
            # 빼서 복원한다.
            realized = sum((c.get("pnl") or 0) for c in self.state.closed)
            locked = sum(max((p.invested or 0.0) - (p.sold_value or 0.0), 0.0) for p in self.state.book.all().values())
            self.broker = PaperOverseasBroker(
                # ★ 이 브로커의 cash·매수금액은 전부 달러 기준이다(실거래
                # 브로커가 buying_power(currency="USD") 를 보는 것과 맞춤).
                # 원화 배정금액을 그대로 넣으면 환율 배수만큼 부풀려진 돈으로
                # 시뮬레이션하게 되므로 달러로 환산해서 넣는다.
                starting_cash=float(self.overseas_cfg.budget_usd or 0.0) + realized - locked,
                book=self.state.book,
                commission_pct=cfg.costs.commission_pct,
            )

        if notifier is not None:
            self.notifier = notifier
        else:
            from daytrader import notify
            self.notifier = notify.Telegram(cfg) if notify.configured_cfg(cfg) else None

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str = ""
        self.loop_count = 0
        # ★★★ "미국주식 선정도 국내주식과 동일하게 자동으로 선정" - auto_select
        # 가 켜져 있으면 매일 한 번씩 다시 뽑은 감시 종목을 여기 담아 둔다.
        self._auto_watchlist: list = []
        # ★ 미국 테마 자동 선정 결과(us_themes.py) - 리포트 전체와 종목->테마 표.
        self._last_prices: dict = {}  # 보유 종목의 마지막 현재가(화면용)
        self._theme_report: dict | None = None
        self._theme_picks: dict = {}
        self._theme_at: float = 0.0
        self._theme_retry_at: float = 0.0
        self._theme_phase: str = ""
        self._auto_watchlist_date: str = ""
        # ★ 잘못된 티커 경고를 매 루프 찍으면 로그가 쓰레기가 된다 - 한 번만.
        self._warned_bad_tickers: set = set()

        # ★★★ "재빌드·재시작해도 실거래 이력은 API 와 연계해서 실제정보로
        # 업데이트해야 한다"는 요청 - 국내주식 engine.py 의 resume()/
        # reconcile() 과 같은 개념을 해외주식에도 둔다. 프로그램이 꺼져
        # 있던 사이 서버(조건부 주문 등)에서 이미 청산됐을 수 있는 포지션을
        # 같은 계좌의 실제 보유 종목과 대조해 바로잡는다.
        if self.is_live:
            self._reconcile_live_positions()

    def _reconcile_live_positions(self) -> None:
        """★ 계좌가 진실이다 - 국내주식과 같은 토스 계좌를 쓰므로
        client.holdings() 에 해외 종목도 함께 나온다. 로컬에는 보유
        중으로 남아 있는데 실제 계좌 수량이 없거나 훨씬 적으면, 프로그램이
        꺼진 사이 서버에서 이미 팔린 것으로 보고 로컬 기록을 정리한다.
        """
        try:
            holdings = self.client.holdings()
        except Exception as exc:
            log.warning("해외주식 실거래 계좌 대조 실패 - 보유 종목 조회 오류: %s", exc)
            return
        items = holdings.get("items", []) if isinstance(holdings, dict) else holdings
        account_by_symbol = {h.get("symbol"): h for h in (items or []) if isinstance(h, dict)}

        changed = False
        for symbol, pos in list(self.state.book.all().items()):
            acc = account_by_symbol.get(symbol)
            try:
                real_qty = float(acc.get("quantity", 0)) if acc else 0.0
            except (TypeError, ValueError):
                real_qty = 0.0
            if pos.quantity <= 0 or real_qty >= pos.quantity * 0.1:
                continue
            try:
                exit_price = self._current_price(symbol)
                estimated_price = False
            except Exception:
                exit_price = pos.entry_price
                estimated_price = True
            pnl = (exit_price - pos.entry_price) * pos.quantity
            self.state.closed.append({
                "symbol": symbol, "quantity": pos.quantity, "entry_price": pos.entry_price,
                "exit_price": exit_price, "pnl": pnl,
                "reason": "계좌 대조 - 프로그램 밖에서 청산됨(체결가 추정)",
                "entry_time": pos.entry_time, "exit_time": _now_ts(),
                "entry_technique": pos.technique, "is_live": True, "estimated": True,
                "estimated_price": estimated_price,
            })
            self.state.book.record_sell(symbol)
            changed = True
            log.warning(
                "[해외주식 실거래 계좌 대조] %s 는 서버에서 이미 정리된 것으로 보여 로컬 기록을 정리합니다 "
                "(내부 수량 %s, 실제 잔고 %s).", symbol, pos.quantity, real_qty,
            )
        if changed:
            self.state.save()

    # ━━ 시작/정지 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="overseas-engine", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        # 진입 탐색(run_once)은 poll_seconds 마다, 그 사이에는 보유 종목만 manage_seconds 마다 살핀다.
        poll = max(5, int(getattr(self.overseas_cfg, "poll_seconds", 60)))
        manage = max(5, min(poll, int(getattr(self.overseas_cfg, "manage_seconds", 60) or poll)))
        next_full = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                if now >= next_full:
                    self.run_once()
                    next_full = time.monotonic() + poll
                else:
                    self.manage_positions_only()
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("해외주식 엔진 루프 오류")
            self._stop.wait(max(1.0, min(manage, next_full - time.monotonic())))

    def manage_positions_only(self) -> None:
        """진입 탐색 없이 보유 종목의 청산 조건(손절·익절·추적·시간)만 확인한다. 보유가 없으면 아무것도 안 한다."""
        held = list(self.state.book.all().items())
        if not held:
            return
        for symbol, pos in held:
            try:
                self._manage_position(symbol, pos)
            except Exception as exc:
                self.last_error = f"{symbol} 청산 관리 중: {exc}{_where(exc)}"
                log.exception("해외주식 청산 관리 실패 %s", symbol)
        self.state.save()

    def _effective_watchlist(self) -> list:
        """★ auto_select 가 켜져 있으면 오늘 자동으로 뽑은 목록을, 아니면
        지금까지처럼 설정에 고정해 둔 watchlist 를 그대로 쓴다.

        ★★★ 어느 쪽이든 미국 티커 형식만 통과시킨다 - 국내 종목코드가
        섞이면 야후 조회에서 404 가 나고 화면에 "조회 실패"만 남는다
        (자동 선정 응답에서도, 사용자가 설정에 직접 넣은 값에서도 실제로
        벌어질 수 있다).
        """
        # ★★★ 거래 대상 = 오늘의 테마주(자동 산정) + 랭킹 자동 선정(켰을 때) + 직접 추가한 관심 종목.
        # 관심 종목은 테마와 별개로 항상 들어간다("테마주 외에 관심 종목도 거래 종목").
        sources: list = []
        if getattr(self.overseas_cfg, "theme_select", False):
            self._theme_select_if_due()
            sources += list(self._theme_picks.keys())
        if getattr(self.overseas_cfg, "auto_select", False):
            self._auto_select_if_new_day()
            sources += list(self._auto_watchlist)
        sources += list(self.overseas_cfg.watchlist)
        seen: set = set()
        uniq = [x for x in sources if not (x in seen or seen.add(x))]
        return self._valid_tickers(uniq)

    def _theme_select_if_due(self) -> None:
        """미국 테마를 평가해 오늘의 테마주를 다시 뽑는다. theme_refresh_minutes 마다(기본 60분) 갱신하고,
        실패하면 10분 뒤 다시 시도한다 - 그동안은 직전 결과를 그대로 쓴다."""
        if self.overseas_cfg.mode == "sim":
            return  # 시뮬레이션은 가짜 시세라 실제 랭킹과 무관하다.
        now = time.time()
        refresh = max(5, int(getattr(self.overseas_cfg, "theme_refresh_minutes", 60))) * 60
        # ★★★ 장이 바뀔 때(프리마켓→정규장→애프터→야간, 휴장→개장)마다 그 국면의 시장 상황으로 테마를 다시 뽑는다.
        phase = us_phase(datetime.now().astimezone())
        phase_changed = phase != self._theme_phase
        if self._theme_report is not None and not phase_changed and now - self._theme_at < refresh:
            return
        if now < self._theme_retry_at:
            return
        try:
            from daytrader.us_themes import scan_us_themes
            report = scan_us_themes(self.client, self.overseas_cfg, self.cfg)
        except Exception as exc:
            self._theme_retry_at = now + 600
            log.warning("미국 테마 선정 실패(10분 뒤 재시도, 직전 목록 유지): %s", exc)
            return
        if not report.get("market_size"):
            self._theme_retry_at = now + 600  # 시세를 못 받았다 - 직전 결과를 지우지 않는다.
            if self._theme_report is None:
                self._theme_report = report
            return
        report["phase"] = phase
        report["phase_label"] = PHASE_LABELS.get(phase, phase)
        self._theme_report = report
        self._theme_at = now
        self._theme_phase = phase
        self._theme_picks = {c["symbol"]: c["theme"] for c in report.get("candidates", [])}
        log.info("미국 테마주 선정(%s%s): %s", PHASE_LABELS.get(phase, phase), " - 장 변경" if phase_changed else "",
                 ", ".join(f"{s}({t})" for s, t in self._theme_picks.items()) or "없음")
        # ★ 오늘 새로 뽑힌 종목에 실험실 백테스트를 자동으로 돌려(백그라운드), "이 종목엔 이
        # 기법이 최근 더 잘 맞았다"는 결과를 진입 기법 채점에 가산점으로 반영한다(technique_prefs.py).
        # 관심 종목도 테마와 별개로 항상 거래 대상이니 같이 넣는다(technique_backtest 와 같은 원칙).
        try:
            from daytrader import auto_backtest, technique_backtest
            theme_cands = [{"symbol": c["symbol"], "name": c.get("name") or c["symbol"], "theme": c.get("theme", "")}
                           for c in (report.get("candidates") or [])]
            cand_rows = technique_backtest.merge_with_watchlist(theme_cands, self.overseas_cfg.watchlist)
            auto_backtest.maybe_trigger(self.cfg, self.client, "overseas", cand_rows)
        except Exception as exc:
            log.warning("실험실 자동 백테스트 트리거 실패(무시하고 계속): %s", exc)

    def theme_report(self) -> dict | None:
        return self._theme_report

    def _snapshot_watchlist(self) -> list:
        # snapshot() 은 자주 불리므로 API 를 새로 호출하지 않고, 이미 뽑아 둔 결과만 합친다.
        out: list = []
        srcs = list(self._theme_picks.keys())
        if getattr(self.overseas_cfg, 'auto_select', False):
            srcs += list(self._auto_watchlist)
        srcs += list(self.overseas_cfg.watchlist)
        for x in srcs:
            if x not in out:
                out.append(x)
        return out

    def _valid_tickers(self, symbols) -> list:
        out = []
        for s in symbols or []:
            if _is_us_ticker(s):
                out.append(s)
            else:
                if s not in self._warned_bad_tickers:
                    self._warned_bad_tickers.add(s)
                    log.warning(
                        "해외주식 감시목록에 미국 티커가 아닌 값(%s)이 있어 제외합니다 - "
                        "[설정] → 해외주식의 관심 종목을 확인하세요(국내 종목코드는 여기 넣을 수 없습니다).", s,
                    )
        return out

    def _auto_select_if_new_day(self) -> None:
        """★★★ 국내주식(screener.py)이 거래대금 상위 + 급등 상위 랭킹을
        합쳐 테마 후보를 스코어링하는 것과 같은 원리를, 해외주식은 테마
        개념이 없으니 랭킹 두 개(거래대금·급등)를 합쳐 상위 N 종목을
        오늘의 감시 목록으로 그대로 쓴다. 하루에 한 번만 다시 뽑는다 -
        장중 시시각각 종목이 바뀌면 판단 기준이 흔들린다.

        ★ 같은 이유(1-10) - KST 기준 날짜로 통일한다(호스트가 UTC 면
        하루에 한 번이어야 할 재선정이 한국 장중에 또 일어날 수 있었다).
        """
        today = day_str(_now_kst_aware())
        if self._auto_watchlist and self._auto_watchlist_date == today:
            return
        count = max(1, getattr(self.overseas_cfg, "auto_select_count", 10))
        picked: dict[str, float] = {}
        for kind, duration in (("MARKET_TRADING_AMOUNT", "realtime"), ("TOP_GAINERS", "1d")):
            try:
                rows = self.client.rankings(type=kind, marketCountry="US", duration=duration, count=count * 2)
            except Exception as exc:
                log.warning("해외주식 자동 선정 - %s 랭킹 조회 실패: %s", kind, exc)
                continue
            for i, r in enumerate(rows or []):
                if not isinstance(r, dict):
                    continue
                symbol = r.get("symbol")
                if not symbol:
                    continue
                # ★★★ 실제로 겪은 버그 - marketCountry="US" 로 요청했는데도
                # 국내 종목코드(010620 같은 6자리 숫자)가 섞여 들어왔고,
                # 그걸 미국 티커로 알고 야후에 조회해서 404 가 났다
                # ("시세조회 실패, 종목명이 안 나옴"). API 응답을 그대로
                # 믿지 말고 형식을 직접 검증한다 - 미국 티커는 알파벳으로
                # 이뤄지고(BRK.B 처럼 점이 섞이기도 한다), 숫자로만 된
                # 심볼은 국내 종목코드다.
                if not _is_us_ticker(symbol):
                    log.warning(
                        "해외주식 자동 선정 - 미국 티커가 아닌 심볼(%s)이 응답에 있어 건너뜁니다.", symbol,
                    )
                    continue
                # ★ 두 랭킹에 다 있으면(거래대금도 크고 급등도 했으면) 더 높은 점수를 준다.
                score = picked.get(symbol, 0.0) + (count * 2 - i)
                picked[symbol] = score
        if not picked:
            log.warning("해외주식 자동 선정 실패 - 랭킹을 하나도 못 가져와 기존 감시 목록을 유지합니다.")
            return
        ranked = sorted(picked.items(), key=lambda kv: kv[1], reverse=True)
        self._auto_watchlist = [sym for sym, _ in ranked[:count]]
        self._auto_watchlist_date = today
        log.info("해외주식 자동 선정 완료(%s): %s", today, ", ".join(self._auto_watchlist))

    def halt_info(self, now: datetime | None = None) -> dict:
        """★★★ 국내주식(engine.py._check_kill_switch)에 있는 4단 방어 중
        핵심 2개(일일 손실 한도·연속 손절 중단)를 해외주식에도 둔다 -
        예전엔 이 엔진에 그런 제어가 전혀 없어서, 손실이 나도 계속
        재진입해 39건까지 과매매로 이어졌다. 청산 관리는 막지 않고
        신규 진입만 막는다(코인·국내주식과 같은 원칙).

        ★ now 는 테스트에서 "지금"을 원하는 시각으로 주입하기 위한 것이다 - 비우면
        실제 현재 시각(datetime.now().astimezone())을 쓴다.
        """
        now = now or datetime.now().astimezone()
        # ★★★ [1-5] 실거래 브로커가 주문 접수 여부를 끝내 확인 못해 멈춘 상태면
        # (network-uncertain 뒤 resolve_uncertain() 도 실패) 다른 조건과 무관하게
        # 신규 진입을 막는다 - 모르는 상태로 계속 사고팔지 않는다.
        if getattr(self.broker, "halted", False):
            return {"halted": True, "resume_at": None, "reason": self.broker.halt_reason}
        r = self.cfg.risk
        self.state._reset_if_new_day()
        today_closed = [c for c in self.state.closed if isinstance(c, dict) and _is_today_ts(c.get("exit_time"))]
        realized = sum(c.get("pnl") or 0 for c in today_closed)
        # ★ 손익(pnl)이 달러라서 기준 금액도 달러(해외 총 투자금액)여야 한다 - 예전엔 원화 배정액과 비교했다.
        allocation = float(self.overseas_cfg.budget_usd or 0.0)
        if allocation and -realized / allocation >= r.daily_loss_limit_pct:
            return {
                "halted": True, "resume_at": None,
                "reason": f"오늘 손실 한도({r.daily_loss_limit_pct*100:.0f}%)에 도달해 신규 진입을 중단합니다 - 내일 자동 재개.",
            }
        if self.state.consecutive_losses >= r.max_consecutive_losses:
            last_loss_at = next(
                (c.get("exit_time") for c in reversed(self.state.closed) if (c.get("pnl") or 0) < 0), None,
            )
            hours = getattr(r, "loss_halt_cooldown_hours", 24.0)
            resume = (last_loss_at or _now_ts()) + hours * 3600
            # ★★★ "프리장·본장·애프터장·데이장(주간거래)이 바뀌면 다시 거래를 재개하도록 해"
            # 요청 - 시간 경과만 보면, 예를 들어 정규장에서 연속 손절해 막혔을 때 하루 종일
            # (24시간) 다음 정규장까지도 계속 막혀 있을 수 있다. 세션마다 유동성·변동성 성격이
            # 달라서(프리장은 얇고, 정규장은 두텁고 등) 한 세션에서의 손절이 다음 세션의 기회까지
            # 막을 이유는 없다 - 마지막 손절이 일어난 세션과 지금 세션이 다르면 시간과 무관하게
            # 즉시 재개한다.
            last_loss_phase = us_phase(datetime.fromtimestamp(last_loss_at).astimezone()) if last_loss_at else None
            current_phase = us_phase(now)
            phase_changed = last_loss_phase is not None and current_phase != last_loss_phase
            if now.timestamp() >= resume or phase_changed:
                return {"halted": False, "reason": "", "resume_at": None}
            return {
                "halted": True, "resume_at": resume,
                "reason": (f"연속 손절 {self.state.consecutive_losses}회 - 신규 진입 중단(청산은 계속함) "
                           f"· 재개 예정 {_fmt_ts(resume)} (설정 {hours:g}시간 뒤, 또는 장이 바뀌면 즉시)"),
            }
        # ★★★ "국장과 해외장 설정도 서로 분리가 안됐게 있는지 확인해 - 일 최대 거래건수는
        # 각각 50회로" 요청 - 예전엔 국내용 risk.daily_max_trades 를 그대로 빌려 썼다.
        # 손절/익절폭과 같은 이유로 overseas_cfg 의 독립된 값을 본다.
        max_trades = getattr(self.overseas_cfg, "daily_max_trades", 50)
        if self.state.trades >= max_trades:
            return {
                "halted": True, "resume_at": None,
                "reason": f"오늘 매매 {self.state.trades}건으로 일일 최대 거래({max_trades}회)에 도달했습니다 - 내일 자동 재개.",
            }
        return {"halted": False, "reason": "", "resume_at": None}

    def _cooldown_left(self, symbol: str) -> float:
        until = self.state.cooldown.get(symbol)
        if not until:
            return 0.0
        return max(0.0, until - _now_ts())

    def run_once(self) -> None:
        self.loop_count += 1
        # ★ 국내주식(engine.py)만 기법별 실적을 playbook 에 채워주고 있어서, 해외주식은
        # technique_learning_mode 를 뭘로 둬도 실적 가산점이 항상 중립(1.0)이었다
        # (2026-09-24 확인) - self.state.closed(해외주식 자체 청산 이력)로 여기서도 채워
        # 준다. 장 개장 여부와 무관하게(휴장 중 청산 관리만 하는 분기 포함) 매 루프 갱신한다.
        try:
            self.playbook.set_performance(
                summarize_closed_trades(self.state.closed, technique_field="entry_technique")["by_technique"])
        except Exception as exc:
            log.warning("기법별 실적 조회 실패(순수 신호 강도로만 판단합니다): %s", exc)
        # ★★★ "시뮬레이션 모드인데 왜 실제 개장 시간과 상관있냐"는 정확한
        # 지적 - sim 은 실제 시장과 무관하게 언제든 매매 로직을 테스트하기
        # 위한 모드다(국내주식의 sim 이 가상 시계로 실제 시간과 무관하게
        # 도는 것과 같은 취지). web/paper/live 는 실제 계좌·실제 시세로
        # 움직이는 실질적 거래라 실제 개장 시간을 지켜야 하지만, sim 은
        # 그 제약을 받을 이유가 없다.
        # ★ 미국 주식은 평일 24시간 거래되지만, 세션별로 사용자가 켜고 끌 수 있다(위 is_us_tradable 참고).
        market_open = True if self.overseas_cfg.mode == "sim" else is_us_tradable(
            datetime.now().astimezone(),
            trade_premarket=getattr(self.overseas_cfg, "trade_premarket", True),
            trade_regular=getattr(self.overseas_cfg, "trade_regular", True),
            trade_afterhours=getattr(self.overseas_cfg, "trade_afterhours", True),
            trade_overnight=getattr(self.overseas_cfg, "trade_overnight", True),
        )
        if not market_open:
            # ★★★ 청산 관리 대상은 반드시 "실제 보유 중인 전체 종목"이어야
            # 한다 - watchlist(오늘의 감시 목록)만 돌면, 자동 선정으로
            # 목록이 바뀌었을 때 어제 산 종목이 오늘 목록에서 빠져 청산
            # 관리를 놓칠 수 있다(실제로 겪을 뻔한 안전 결함). 장 열린
            # 시간이 아니면 신규 진입은 하지 않되, 보유 중인 건 전부 본다.
            for symbol, pos in list(self.state.book.all().items()):
                try:
                    self._manage_position(symbol, pos)
                except Exception as exc:
                    # ★★★ 어느 단계에서 났는지 모르면 원인을 찾을 수 없다 -
                    # 화면에는 단계를 밝히고, 로그에는 전체 스택을 남긴다.
                    self.last_error = f"{symbol} 청산 관리 중: {exc}{_where(exc)}"
                    log.exception("해외주식 청산 관리 실패 %s", symbol)
            self.state.save()
            return

        # ★★★ 자동 선정은 장이 열려 있을 때만 한다 - 장이 닫힌 동안에는 신규
        # 진입이 없으니 목록을 뽑을 이유가 없고, 랭킹 API 호출만 낭비된다
        # (선정 실패 시 매 루프마다 재시도까지 했다). 국내주식의 하루 1회
        # 스크리닝과 같은 주기다.
        watchlist = self._effective_watchlist()

        # ★★★ 장중에도 순서가 중요하다 - 먼저 "실제 보유 중인 전체 종목"을
        # 확실히 관리하고, 그 다음 오늘의 watchlist 중 아직 안 산 종목만
        # 신규 진입을 시도한다. watchlist 만 순회하면(자동 선정으로 목록이
        # 바뀐 경우) 목록에서 빠진 보유 종목의 청산 관리를 놓칠 수 있다.
        for symbol, pos in list(self.state.book.all().items()):
            try:
                self._manage_position(symbol, pos, allow_add=True)
            except Exception as exc:
                self.last_error = f"{symbol} 청산 관리 중: {exc}{_where(exc)}"
                # ★ exception() 은 스택까지 남긴다 - 어느 줄에서 났는지
                #   알아야 고칠 수 있다(warning 은 메시지만 남는다).
                log.exception("해외주식 청산 관리 실패 %s", symbol)

        # ★★★ 손실 한도·연속 손절·일일 거래한도 - 하나라도 걸리면 신규
        # 진입만 멈춘다(위에서 이미 처리한 보유 종목 청산은 계속됨).
        halt = self.halt_info()
        # ★★★ 국내주식(engine.py.try_entries())의 capital.max_positions 와
        # 같은 개념 - 동시 보유 한도에 이미 도달했으면 신규 진입을 더
        # 시도하지 않는다. 예전엔 이 한도가 아예 없어서(감시종목 15개를
        # 자금으로만 나눠 사실상 무제한 보유가 가능했다), 종목당 배정을
        # 항상 15등분해야 했다 - max_positions 로 나눈 지금은 실제로
        # 이 한도를 지켜야 그 계산이 맞는다.
        if len(self.state.book.all()) >= self.overseas_cfg.max_positions:
            self.last_error = f"동시 보유 한도({self.overseas_cfg.max_positions}종목)에 도달해 신규 진입을 보류합니다."
        elif halt["halted"]:
            self.last_error = halt["reason"]
        else:
            for symbol in watchlist:
                if len(self.state.book.all()) >= self.overseas_cfg.max_positions:
                    break
                if self.state.book.owns(symbol):
                    continue  # ★ 이미 위에서 관리했다 - 중복 처리 방지.
                if self._cooldown_left(symbol) > 0:
                    continue  # ★ 방금 이 종목에서 손절/익절이 나 재진입 대기 중.
                try:
                    self._try_entry(symbol)
                except Exception as exc:
                    self.last_error = f"{symbol} 신규 진입 판단 중: {exc}{_where(exc)}"
                    log.exception("해외주식 신규 진입 실패 %s", symbol)
        self.state.save()

    def _process_symbol(self, symbol: str) -> None:
        pos = self.state.book.get(symbol)
        if pos is not None:
            self._manage_position(symbol, pos)
        else:
            self._try_entry(symbol)

    def _fetch_bars(self, symbol: str, count: int = 80, interval: str = "1m") -> list:
        # ★★★ 실제로 겪은 버그 - 일봉("1d")으로 진입 신호를 판단하면서
        # 청산은 실시간가에 2.5%/5% 같은 타이트한 퍼센트로 관리했다. 일봉
        # 돌파 신호는 하루 종일 안 바뀌니, 손절로 빠지자마자 같은 신호가
        # 여전히 살아 있어 곧바로 재매수됐다(6시간에 같은 종목만 39번
        # 거래된 원인). 진입 판단 기본값도 분봉으로 바꿔 청산 시간 프레임과
        # 맞춘다(국내주식 engine.py 가 이미 "1m" 을 쓰는 것과 같은 간격).
        # interval="1d" 로 부르면(과열 필터용) 예전처럼 일봉을 받아온다.
        rows = self.client.candles(symbol, interval, count)
        out = []
        for r in rows:
            # ★★★ 실제로 겪은 버그("'str' object has no attribute 'get'") -
            # 캔들 응답에 딕셔너리가 아닌 항목(문자열 등)이 섞여 오면 아래
            # r.get() 에서 죽어 해외주식 매매가 통째로 실패했다. 게다가
            # "openPrice" in r 은 문자열에서도 예외 없이 통과해(부분 문자열
            # 검사) 그냥 지나쳐 버린다 - 타입을 직접 확인해야 한다.
            if not isinstance(r, dict):
                continue
            if "openPrice" in r:
                out.append(Bar.from_api(r))
            else:
                out.append(Bar(
                    ts=r.get("timestamp", ""),
                    open=float(r.get("open") or 0), high=float(r.get("high") or 0),
                    low=float(r.get("low") or 0), close=float(r.get("close") or 0),
                    volume=float(r.get("volume") or 0),
                ))
        return out

    def _current_price(self, symbol: str) -> float:
        rows = self.client.prices([symbol])
        if not rows:
            raise RuntimeError(f"{symbol} 시세를 받지 못했습니다.")
        # ★★★ 같은 이유 - 응답 첫 항목이 딕셔너리가 아니면 .get() 에서
        # 죽는다. 딕셔너리인 항목을 골라 쓰고, 없으면 명확한 사유로 알린다.
        r = next((x for x in rows if isinstance(x, dict)), None)
        if r is None:
            raise RuntimeError(f"{symbol} 시세 응답 형식이 올바르지 않습니다({type(rows[0]).__name__}).")
        price = r.get("price") or r.get("lastPrice") or r.get("currentPrice")
        if price is None:
            raise RuntimeError(f"{symbol} 시세 응답에 값이 없습니다.")
        return float(price)

    def _make_ctx(self, symbol: str, name: str, now) -> SimpleNamespace:
        # ★ 테마 관련 필드는 아예 안 채운다 - getattr(ctx, key, 기본값) 로
        # 안전하게 처리되어(원래 국내주식 코드) 없어도 죽지 않고, 테마
        # 의존 기법만 자연히 통과 못 한다.
        return SimpleNamespace(
            symbol=symbol, name=name, theme="해외주식", now=now,
            prev_verdict=None, prev_verdicts=[], held_minutes=0, force_close=False,
        )

    def _cash_available(self) -> float:
        """★★★ 실제로 겪은 버그("'>' not supported between instances of
        'NoneType' and 'int'") - 브로커가 현금을 None 으로 돌려주는 경우
        (API 응답에 cash 가 없거나 조회 실패)에 바로 비교식에 넣어서
        TypeError 가 났고, 매수가 통째로 실패했다. 숫자로 정규화한다.
        ★ 알 수 없으면 0 으로 본다 - 잔고를 모르는데 있다고 가정하고
        사는 것보다 안 사는 쪽이 안전하다.
        """
        c = self.broker.cash
        value = c() if callable(c) else c
        try:
            return float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _conviction_of(self, symbol: str):
        """미국 테마 산정에서 이 종목이 얼마나 강한 근거로 뽑혔는지(0~1). 테마 근거가 없는 관심 종목은 중립 0.5."""
        rep = self._theme_report or {}
        for c in rep.get("candidates", []) or []:
            if c.get("symbol") == symbol:
                return sizing.theme_conviction(
                    theme_rank=c.get("theme_rank", 0), breadth=c.get("theme_breadth", 0),
                    intensity=c.get("theme_intensity", 0.0), rank_in_theme=c.get("rank_in_theme", 0))
        return 0.5

    def _try_entry(self, symbol: str) -> None:
        bars = self._fetch_bars(symbol, count=80)
        if len(bars) < 20:
            return
        current_price = bars[-1].close
        # ★ 정규장 밖에는 분봉이 안 오거나 늦을 수 있다 - 마지막 봉이 낡았으면 옛 신호로 사지 않는다.
        if self.overseas_cfg.mode != "sim" and us_session(datetime.now().astimezone()) != "regular":
            age = _bar_age_seconds(bars[-1])
            if age is not None and age > 15 * 60:
                return

        # ★★★ 실제로 겪은 문제 - auto_select 랭킹(거래대금·급등)에 가격·
        # 과열 필터가 없어서 $0.1~0.2대 페니스톡(SNYR)이 뽑혔고, 이런
        # 종목은 원래 급등락이 심해 고정 2.5%/5% 손절과 상성이 최악이라
        # 과다 손절로 이어졌다. 매수 직전에 실제 가격으로 한 번 더 거른다
        # (랭킹 API 응답 필드명을 신뢰하기보다, 이미 받아 둔 실가격을 쓴다).
        min_price = getattr(self.overseas_cfg, "min_price", 0) or 0
        if min_price and current_price < min_price:
            return
        max_change = getattr(self.overseas_cfg, "max_daily_change_pct", 0) or 0
        if max_change:
            try:
                daily = self._fetch_bars(symbol, count=2, interval="1d")
            except Exception:
                daily = []  # ★ 필터 조회 실패로 매매 자체를 막지 않는다 - 원래도 없던 필터다.
            if len(daily) >= 2 and daily[-2].close:
                day_change = abs(daily[-1].close - daily[-2].close) / daily[-2].close
                if day_change > max_change:
                    return

        ctx = self._make_ctx(symbol, symbol, datetime.now())

        winner, _all_verdicts = self.playbook.evaluate_entry(bars, ctx)
        if winner is None:
            return

        if self.overseas_cfg.mode == "web":
            log.info("[해외주식 매수 신호·관찰 전용] %s @ %.2f달러 (%s) - 실제로 사지 않습니다.",
                      symbol, current_price, winner.technique_label)
            return

        # ★★★ 실제로 겪은 문제(2026-09-24, "해외주식 진입가가 이상해" 신고로 발견) - 진입가는
        # 분봉 종가(bars[-1].close, /candles API) 하나만 보고 정하는데, 보유 종목 관리·청산은
        # 실시간 시세(_current_price, /prices API)를 쓴다. 애프터마켓처럼 거래량이 얇은 시간대엔
        # 분봉 하나가 단일 이상 체결(유동성 부족·오류성 호가)로 실제 가격과 크게 어긋날 수 있다 -
        # 실제로 AAPL 이 진짜 가격($336)의 약 77배인 $25,988 로 진입가가 기록된 사례가 있었다
        # (그 시점 /candles 응답 자체가 그렇게 왔다 - 우리 파싱 버그는 아니었다). 매수 직전에
        # 청산과 같은 실시간 시세로 한 번 더 교차 검증해, 5% 넘게 벗어나면 매수를 보류한다.
        try:
            live_price = self._current_price(symbol)
        except Exception:
            live_price = None
        if live_price and live_price > 0:
            diff_pct = abs(current_price - live_price) / live_price
            if diff_pct > 0.05:
                self.last_error = (
                    f"{symbol}: 분봉 종가(${current_price:,.2f})와 실시간가(${live_price:,.2f})가 "
                    f"{diff_pct * 100:.0f}% 차이나 매수를 보류합니다(잘못된 시세로 보임)"
                )
                log.warning("[해외주식 매수 보류] %s: %s", symbol, self.last_error)
                return
            current_price = live_price  # ★ 교차 검증을 통과했으니 더 최신인 실시간가를 실제 체결가로 쓴다.

        # ★★★ 실제로 겪은 버그 - 원화 배정금액(krw_amount)을 달러 주가로
        # 그대로 나눠서 수량을 구하고 있었다. 300만원 ÷ $238 = 1260주 처럼
        # 환율(약 1,400배)만큼 수량이 부풀려진다 - 시뮬레이션에서 "AAPL을
        # 1260주 샀다"는 비현실적 결과로 드러났다. 원화를 달러로 환산한
        # 뒤에 나눠야 한다.
        # ★★★ 실제로 겪은 문제 - 예전엔 감시종목 전체 개수(기본 15)로
        # 나눠서 종목당 배정이 약 6만원(≈$43)에 불과했다. 실제로 동시에
        # 15종목을 다 사는 일은 거의 없어서(보통 1~2종목) 자금 대부분이
        # 항상 묵혀 있었던 셈이다. max_positions(동시 보유 한도)로 나누면
        # "실제로 동시에 들고 갈 만큼"만 나눠 종목당 배정이 커진다 -
        # 동시 보유 한도 자체는 위 run_once() 에서 강제한다.
        # ★★★ 해외주식 총 투자금액(달러)을 동시 보유 종목 수로 나눈 것이 종목당 한도다. 첫 매수는 그 절반에
        # 신호 강도 배수(0.6~1.4)를 곱한 금액이고, 나머지는 이익이 날 때 나눠서 추가 매수한다(sizing.py).
        # ★ 사기 직전 뉴스 위험 필터(거르기 전용, 실패하면 통과)
        allowed, why = news_guard.get_guard(self.cfg).gate(symbol, symbol, "overseas")
        if not allowed:
            self.last_error = f"{symbol}: {why}"
            return
        cap = sizing.position_cap(self.overseas_cfg.budget_usd, self.overseas_cfg.max_positions)
        strength = sizing.signal_strength(winner)
        conviction = self._conviction_of(symbol)
        vol = sizing.vol_ratio(bars, self.overseas_cfg.stop_loss_pct)
        usd_amount = sizing.entry_amount(cap, strength, self.cfg.sizing, conviction=conviction, vol=vol)
        if usd_amount < 1.0:
            self.last_error = f"{symbol}: 투자금액이 너무 작아 매수하지 않습니다(종목당 한도 ${cap:,.0f})"
            return
        # ★ 브로커의 cash 는 달러 기준(LiveOverseasBroker 가
        # buying_power(currency="USD") 를 본다)이므로, 원화가 아니라
        # 달러로 환산한 금액과 비교해야 한다 - 예전엔 원화 금액을 달러
        # 잔고와 직접 비교해서 거의 항상 "현금 부족"으로 막힐 수 있었다.
        if usd_amount > self._cash_available():
            self.last_error = f"{symbol}: 현금 부족으로 매수 보류"
            return

        pos = self.broker.buy(symbol, usd_amount, current_price, technique=winner.technique)
        if pos:
            pos.conviction = 0.5 if conviction is None else conviction
            log.info("[해외주식 매수] %s %.4f주 @ %.2f달러 (%s · 신호 %.2f·테마 근거 %.2f·변동성 %.2f배 → $%.0f)", symbol, pos.quantity,
                     current_price, winner.technique_label, strength, pos.conviction, vol if vol is not None else 1.0, usd_amount)
            self._notify(f"🌍 <b>[해외주식 매수]</b> {symbol}\n{pos.quantity:.4f}주 @ ${current_price:,.2f}\n{winner.technique_label}")

    def _manage_position(self, symbol: str, pos, allow_add: bool = False) -> None:
        current_price = self._current_price(symbol)
        self._last_prices[symbol] = current_price
        self.state.book.update_peak(symbol, current_price)
        pos = self.state.book.get(symbol)

        bars = self._fetch_bars(symbol, count=80)
        # ★★★ 진입 시각이 비어 있으면(상태 파일 손상 등) 뺄셈에서 죽어
        # 청산 관리가 멈춘다 - 못 파는 건 손실로 직결된다. 0 으로 본다
        # (시간 기반 청산만 판단을 보류하고 나머지는 정상 동작한다).
        _entry_ts = pos.entry_time
        held_minutes = ((_now_ts() - _entry_ts) / 60.0) if _entry_ts else 0.0
        ctx = self._make_ctx(symbol, symbol, datetime.now())
        ctx.held_minutes = held_minutes
        sz = self.cfg.sizing
        ctx.scale_out = bool(sz.scale_out)  # 분할 매도를 쓰면 고정 익절(전량)은 끄고, 나눠서 판다.

        # ① 분할 매도 - 익절폭의 절반에서 1차, 익절폭에서 2차(나머지는 추적 손절이 끌고 간다)
        entry = pos.entry_price or 0.0
        take_pct = (sizing.dynamic_take_profit_pct(self.overseas_cfg.take_profit_pct, bars, sz)
                    if self.overseas_cfg.dynamic_take_profit else self.overseas_cfg.take_profit_pct)
        if self.overseas_cfg.technique_learning_mode == "entry_exit_pref":
            from daytrader import exit_efficiency
            widen, _why = exit_efficiency.widen_multiplier(self.cfg, "overseas", symbol)
            take_pct *= widen
        step = sizing.exit_step(scaled_out=pos.scaled_out, entry=entry, price=current_price, sz=sz,
                                take_pct=take_pct, conviction=pos.conviction,
                                vol=sizing.vol_ratio(bars, self.overseas_cfg.stop_loss_pct))
        # ② 1차 매도 뒤 본전 아래로 내려오면 나머지를 정리 - 이미 챙긴 이익을 손실로 돌리지 않는다
        if step is None and sizing.breakeven_hit(scaled_out=pos.scaled_out, entry=entry, price=current_price, sz=sz):
            self._execute_exit(symbol, pos, current_price, "breakeven_stop")
            return

        verdict = self.playbook.evaluate_exit(pos, bars, current_price, ctx)
        # ★★★ 암호화폐에서 겪은 것과 같은 버그 - 모든 청산 기법이 조건
        # 미충족이면 verdict 자체가 None 이다. None 체크 없이 verdict.ok 를
        # 읽으면 죽는다.
        if verdict is not None and verdict.ok:
            self._execute_exit(symbol, pos, current_price, verdict.technique)  # 손절·추적·시간 청산은 분할 없이 전량
            return
        if step is not None:
            self._execute_exit(symbol, pos, current_price, step[1], fraction=step[0])
            return
        if allow_add:
            self._maybe_add(symbol, pos, current_price, bars, ctx)

    def _maybe_add(self, symbol: str, pos, price: float, bars, ctx) -> None:
        """이익 중인 종목에만 추가 매수(피라미딩). 조건: ① 마지막 매수가·평균가보다 (손절폭의 절반) 이상 오름
        ② 진입 신호가 아직 살아 있음(추세 지속 확인) ③ 신규 진입 중단 상태가 아님 ④ 종목당 한도 안."""
        sz = self.cfg.sizing
        if not sizing.add_due(adds=pos.adds, last_fill=pos.last_fill_price or pos.entry_price, avg_entry=pos.entry_price,
                              price=price, scaled_out=pos.scaled_out, sz=sz, stop_pct=self.overseas_cfg.stop_loss_pct):
            return
        if self.halt_info()["halted"]:
            return
        try:
            winner, _ = self.playbook.evaluate_entry(bars, ctx)
        except Exception:
            return
        if winner is None:
            return
        cap = sizing.position_cap(self.overseas_cfg.budget_usd, self.overseas_cfg.max_positions)
        invested = pos.invested or (pos.entry_price * pos.quantity)
        amount = sizing.add_amount(cap, invested, pos.adds, sz, conviction=pos.conviction,
                                   vol=sizing.vol_ratio(bars, self.overseas_cfg.stop_loss_pct))
        if amount < 1.0 or amount > self._cash_available():
            return
        if self.overseas_cfg.mode == "web":
            return
        added = self.broker.add(symbol, amount, price)
        if added is not None:
            log.info("[해외주식 추가 매수] %s @ %.2f달러 (%d/%d회, 평균 %.2f)", symbol, price, added.adds, sz.max_adds, added.entry_price)
            self._notify(f"🌍 <b>[해외주식 추가 매수]</b> {symbol}\n@ ${price:,.2f} · {added.adds}/{sz.max_adds}회 · 평균 ${added.entry_price:,.2f}")

    def _execute_exit(self, symbol: str, pos, current_price: float, reason: str, fraction: float = 1.0) -> bool:
        try:
            result = self.broker.sell(symbol, current_price, reason=reason, fraction=fraction)
        except NotOwnedError:
            log.error("★★★ %s 매도가 NotOwnedError 로 거부됨 - 이 엔진이 사지 않은 종목입니다.", symbol)
            self.last_error = f"{symbol}: NotOwnedError - 이 엔진이 사지 않은 해외주식 매도 시도가 거부됨"
            return False

        result = result if isinstance(result, dict) else {}
        pnl = result.get("pnl")
        partial = bool(result.get("partial"))
        sold = result.get("quantity", pos.quantity)
        if partial:
            # 나눠 파는 중 - 기록은 거래가 끝났을 때 한 건으로 합쳐 남긴다(건수·승률이 부풀지 않게). 여기서는 누적만.
            pos.sold_qty = (pos.sold_qty or 0.0) + sold
            pos.sold_value = (pos.sold_value or 0.0) + sold * current_price
            log.info("[해외주식 분할 매도] %s %.4f주 @ %.2f달러 (%s) - 남은 %.4f주", symbol, sold, current_price, reason, pos.quantity)
            self._notify(f"🌍 <b>[해외주식 분할 매도]</b> {symbol}\n@ ${current_price:,.2f} · 손익 {pnl:+,.2f}달러 · 남은 {pos.quantity:.4f}주\n{reason}")
            return True
        total_qty = (pos.sold_qty or 0.0) + sold
        avg_exit = (((pos.sold_value or 0.0) + sold * current_price) / total_qty) if total_qty else current_price
        total_pnl = result.get("total_pnl", pnl)
        parts = (getattr(pos, "scaled_out", 0) or 0)
        record = {
            "symbol": symbol, "quantity": total_qty,
            "entry_price": pos.entry_price, "exit_price": avg_exit,
            "pnl": total_pnl,
            "reason": reason + (f" (분할 매도 {parts}회 후)" if parts else ""), "entry_time": pos.entry_time, "exit_time": _now_ts(),
            # ★★★ reason 은 청산(매도) 사유다 - 어떤 기법으로 샀는지는
            # Position 에만 있고 거래 기록엔 없어서 화면에서 볼 수 없었다.
            "entry_technique": pos.technique,
            "is_live": self.is_live,
        }
        if getattr(pos, "adds", 0) or parts:
            record["adds"] = getattr(pos, "adds", 0)
            record["scaled_out"] = parts
        self.state.closed.append(record)
        if self.overseas_cfg.technique_learning_mode == "entry_exit_pref" and pos.entry_price:
            from daytrader import exit_efficiency
            mfe_pct = (pos.peak_price or pos.entry_price) / pos.entry_price - 1
            realized_pct = avg_exit / pos.entry_price - 1
            exit_efficiency.record(self.cfg, "overseas", symbol, realized_pct, mfe_pct)
        # ★★★ 국내주식과 같은 과매매 방지 - 여기가 빠져 있어서 손절 후
        # 곧바로 재진입이 반복됐다(halt_info/_cooldown_left 가 이 값들을 본다).
        self.state.trades += 1
        self.state.consecutive_losses = 0 if (total_pnl or 0) >= 0 else self.state.consecutive_losses + 1
        self.state.cooldown[symbol] = _now_ts() + self.cfg.risk.cooldown_minutes * 60
        log.info("[해외주식 매도] %s @ %.2f달러 (%s)", symbol, current_price, reason)
        pnl_text = f"{total_pnl:+,.2f}달러" if total_pnl is not None else "-"
        self._notify(f"🌍 <b>[해외주식 매도]</b> {symbol}\n@ ${current_price:,.2f} · 손익 {pnl_text}\n{reason}")
        return True

    def liquidate_all(self) -> int:
        count = 0
        for symbol, pos in list(self.state.book.all().items()):
            try:
                current_price = self._current_price(symbol)
            except Exception as exc:
                self.last_error = f"{symbol}: 청산 중 시세 조회 실패 - {exc}"
                continue
            if self._execute_exit(symbol, pos, current_price, "force_close"):
                count += 1
        self.state.save()
        return count

    def _notify(self, text: str) -> None:
        if not self.is_live or self.notifier is None:
            return
        try:
            self.notifier.send(text, force=True)
        except Exception:
            log.warning("해외주식 알림 전송 실패", exc_info=True)

    # ━━ 화면용 요약 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def snapshot(self) -> dict:
        positions = {
            s: {
                "quantity": p.quantity, "entry_price": p.entry_price,
                "peak_price": p.peak_price, "technique": p.technique,
                "last_price": self._last_prices.get(s),
                "adds": p.adds, "scaled_out": p.scaled_out, "invested": p.invested,
                "held_hours": round((_now_ts() - p.entry_time) / 3600.0, 2),
            }
            for s, p in self.state.book.all().items()
        }
        return {
            "running": self.is_running(), "is_live": self.is_live, "mode": self.overseas_cfg.mode,
            "market_open": True if self.overseas_cfg.mode == "sim" else is_us_tradable(
                datetime.now().astimezone(),
                trade_premarket=getattr(self.overseas_cfg, "trade_premarket", True),
                trade_regular=getattr(self.overseas_cfg, "trade_regular", True),
                trade_afterhours=getattr(self.overseas_cfg, "trade_afterhours", True),
                trade_overnight=getattr(self.overseas_cfg, "trade_overnight", True),
            ),
            "session": "regular" if self.overseas_cfg.mode == "sim" else us_session(datetime.now().astimezone()),
            "cash": self._cash_available(), "positions": positions,
            "closed_count": len(self.state.closed), "closed": self.state.closed[-10:],
            # ★ 대시보드 지표·손익 추이는 국내주식(오늘 거래)과 같은 "오늘" 기준이어야 한다 -
            #   closed 는 최근 10건이라 그걸로 세면 며칠 전 거래가 섞이거나 오늘 거래가 잘린다.
            "closed_today": _closed_today(self.state.closed),
            "stats": summarize_closed_trades(self.state.closed, technique_field="entry_technique", symbol_field="symbol"),
            # ★★★ 코인 화면에 이미 있는 "재개 예정 시각" 배너를 해외주식에도
            # 똑같이 보여준다 - 지금까지는 이 엔진에 중단 개념 자체가 없었다.
            "halt": self.halt_info(),
            "today_trades": self.state.trades,
            "loop_count": self.loop_count, "last_error": self.last_error,
            # ★ snapshot() 은 자주(수 초마다) 불릴 수 있으니 여기서 새로
            # API 를 호출하지 않는다 - auto_select 면 캐시된 오늘의 자동
            # 선정 결과(_auto_watchlist)를, 아니면 고정 목록을 그대로 보여준다.
            # ★ 화면에 보이는 감시 목록 = 실제로 매매 대상인 목록(테마주 + 자동선정 + 관심 종목).
            "watchlist": self._snapshot_watchlist(),
            "theme_of": dict(self._theme_picks),
            "phase": "regular" if self.overseas_cfg.mode == "sim" else us_phase(datetime.now().astimezone()),
            "theme_select": bool(getattr(self.overseas_cfg, "theme_select", False)),
            "auto_select": getattr(self.overseas_cfg, "auto_select", False),
            # ★ 화면에서 달러 금액 옆에 원화 환산을 같이 보여주려면 현재 환율이
            # 필요하다 - _usd_krw_rate() 는 실패해도 예외를 던지지 않지만,
            # snapshot() 은 화면이 수 초마다 의존하는 API라 한 번 더 방어한다.
            "usd_krw_rate": self._safe_usd_krw_rate(),
        }

    @staticmethod
    def _safe_usd_krw_rate() -> float:
        try:
            return _usd_krw_rate()
        except Exception:
            return _FALLBACK_USD_KRW
