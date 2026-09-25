"""장이 열려 있는가. 판정을 여러 곳에 흩으면 또 어긋난다. 여기 하나만 둔다.

엔진은 장 시간을 알고 있었지만 화면은 몰랐다. 그래서 밤에도, 주말에도
종목 선정 화면이 15초마다 새로 뽑으며 "이 종목들만 매수 신호를 확인합니다"
를 보여줬다. 실제로는 아무것도 사지 않는데 화면만 일하는 것처럼 보였다.
24시간 켜두는 프로그램이라 이 괴리가 계속 눈에 띈다.
★ 엔진 루프도 시각만 봤다. 시각만 보면 토요일 10시도 '매매 시간대'로
통과한다. 시세가 안 움직여 실제로 사지는 않겠지만, 로그와 화면이
매매하는 것처럼 보이고 남의 서버를 헛되이 두드린다.
"""

from __future__ import annotations

import datetime

from daytrader.timeutil import combine, day_str, iso, now_kst, parse_hhmm

# 국면 여섯.
CAN_ENTER = ("scan",)  # 새로 살 수 있나
LIVE = ("pre", "scan", "manage")  # 시세가 움직이나

_PHASE_LABELS = {
    "weekend": "주말",
    "holiday": "휴장일",
    "pre": "개장 전",
    "scan": "매매 시간",
    "manage": "보유분 관리",
    "after": "장 종료",
}

_WHY = {
    "weekend": "주말은 거래일이 아닙니다.",
    "holiday": "오늘은 휴장일입니다.",
    "manage": "신규 진입 시간이 끝나 보유분만 관리합니다.",
    "after": "장이 끝났습니다.",
}


class _HolidayCache:
    """하루에 한 번만 client.market_calendar_kr() 을 부른다.
    ★ 15초마다 부르면 남의 서버를 두드리는 것이고 호출 제한에도 걸린다.
    """

    def __init__(self):
        self._date: str | None = None
        self._is_holiday = False
        self._known = False

    def check(self, date: str, client) -> tuple:
        if self._date == date:
            return self._is_holiday, self._known

        self._date = date
        if client is None:
            self._is_holiday, self._known = False, False
            return self._is_holiday, self._known

        try:
            cal = client.market_calendar_kr()
            # ★★★ 실제로 겪은 버그(2026-09-24, 추석 연휴에도 종목 선정을 계속 시도한다는 신고로
            # 발견) - 이 코드는 줄곧 {"open": bool} 모양(인터넷 폴백 webquote.py 가 주는 합성
            # 값)만 상정하고 있었다. 그런데 실제 토스 Open API의 /market-calendar/KR 응답은
            # 전혀 다른 모양이다: {"today": {"date": ..., "integrated": null 이면 휴장, 정규장
            # 시각 객체면 거래일}, "previousBusinessDay": {...}, "nextBusinessDay": {...}} -
            # "open" 키 자체가 없다. cal.get("open", True) 는 그래서 토스 응답에서는 항상
            # 기본값 True 만 돌려줘, 실제로 휴장일이어도 절대 휴장으로 판정되지 않았다.
            # 두 모양을 다 알아본다 - "today" 가 있으면 진짜 토스 응답, 없으면 웹 폴백.
            if "today" in cal:
                self._is_holiday = (cal.get("today") or {}).get("integrated") is None
            else:
                self._is_holiday = not bool(cal.get("open", True))
            self._known = True
        except Exception:
            # ★ 못 받으면 거래일로 본다. 임의로 쉬는 날이라 단정해 매매를 막으면
            # 멀쩡한 거래일을 통째로 날린다. 반대로 휴장일에 잘못 도는 것은
            # 시세가 없어 조건이 안 맞으므로 손해가 없다 - 대가가 작은 쪽으로 기운다.
            self._is_holiday, self._known = False, False

        return self._is_holiday, self._known


_cache = _HolidayCache()


def is_day_before_break(now: datetime.datetime, client) -> bool:
    """오늘이 주말·공휴일 앞 마지막 거래일이면 True - "조건부 오버나이트"가 넘기지
    않아야 할 날을 판정한다(exit.overnight_skip_before_holiday).
    ★ 실제 토스 캘린더(/market-calendar/KR)는 "오늘" 응답 안에 nextBusinessDay 도
    함께 준다(_HolidayCache.check() 의 버그 기록 참고) - 그걸 우선 쓰고, 달력상 내일
    날짜와 다르면(=내일이 거래일이 아니면) 쉬는 날 앞이라고 본다. client 가 없거나
    그 정보가 없으면(웹 폴백·시뮬레이션) 주말 여부만으로 판단한다 - 공휴일을 몰라도
    넘기지 않는 쪽(더 보수적인 기본값)이 아니라, 이 함수의 기본 fallback 은 "모르면
    거래일로 본다"는 이 파일의 다른 곳과 같은 원칙을 따른다(과도하게 자주 막지 않는다).
    """
    tomorrow = now.date() + datetime.timedelta(days=1)
    if client is not None:
        try:
            cal = client.market_calendar_kr()
            nxt = (cal or {}).get("nextBusinessDay") or {}
            nxt_date = nxt.get("date")
            if nxt_date:
                return str(nxt_date)[:10] != tomorrow.strftime("%Y-%m-%d")
        except Exception:
            pass
    return tomorrow.weekday() >= 5


def reset_holiday_cache() -> None:
    """날짜가 바뀌면 버린다. 어제 판정을 오늘 쓰면 안 된다."""
    global _cache
    _cache = _HolidayCache()


def _next_weekday(d: datetime.date) -> datetime.date:
    d = d + datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


def _fmt_next_open(now: datetime.datetime, target_date: datetime.date, scan_start: datetime.time) -> str:
    label_time = scan_start.strftime("%H:%M")
    if target_date == now.date():
        return f"오늘 {label_time}"
    if target_date == now.date() + datetime.timedelta(days=1):
        return f"내일 {label_time}"
    return f"{target_date.month}월 {target_date.day}일 {label_time}"


def domestic_phase(now: datetime.datetime) -> str:
    """★★★ "국장도 8시부터 9시까지 프리장, 9시부터 3시반까지 본장, 이후 NXT장" 요청 - 국내
    시장을 실제 거래 세션 종류(동시호가·정규장·NXT)로 나눈다(해외주식 overseas_engine.us_phase()
    와 같은 취지). ★ 위 phase()의 pre/scan/manage/after 는 "지금 신호를 평가해도 되는 시간대"
    (기법 창구) 구분이라 이것과는 다른 축이다 - 실제 매수는 두 축을 모두 통과해야 한다
    (trading = ph in CAN_ENTER and 이 함수가 돌려준 세션의 토글이 켜져 있음)."""
    t = now.time()
    if datetime.time(8, 0) <= t < datetime.time(9, 0):
        return "premarket"
    if datetime.time(9, 0) <= t < datetime.time(15, 30):
        return "regular"
    if datetime.time(15, 30) <= t < datetime.time(20, 0):
        return "nxt"
    return "closed"


def phase(cfg, now=None, client=None) -> dict:
    """지금이 어느 국면인지 판정한다. 화면과 엔진이 이 함수 하나만 본다."""
    now = now or now_kst()
    date = day_str(now)

    is_holiday, holiday_known = _cache.check(date, client)
    is_weekend = now.weekday() >= 5

    scan_start = cfg.entry.scan_start
    scan_end = cfg.entry.scan_end
    force_close = cfg.exit.force_close_time
    # ★ 장 초반·막판 변동성 단타 시간대: 이 시간대 전용 기법만 새로 살 수 있다(window 로 구분).
    open_start, close_end = scan_start, scan_end
    if getattr(cfg.entry, "extra_windows", False):
        try:
            open_start = min(scan_start, parse_hhmm(cfg.entry.open_window_start))
            close_end = max(scan_end, min(force_close, parse_hhmm(cfg.entry.close_window_end)))
        except Exception:
            open_start, close_end = scan_start, scan_end
    t = now.time()
    window = ""

    if is_weekend:
        ph = "weekend"
    elif is_holiday:
        ph = "holiday"
    elif t < open_start:
        ph = "pre"
    elif t < scan_start:
        ph, window = "scan", "open"
    elif t <= scan_end:
        ph, window = "scan", "main"
    elif t <= close_end:
        ph, window = "scan", "close"
    elif t <= force_close:
        ph = "manage"
    else:
        ph = "after"

    d_phase = domestic_phase(now)
    session_flags = {
        "premarket": getattr(cfg.risk, "trade_premarket", False),
        "regular": getattr(cfg.risk, "trade_regular", True),
        "nxt": getattr(cfg.risk, "trade_nxt", False),
    }
    trading = (ph in CAN_ENTER) and session_flags.get(d_phase, False)

    # ★★★ "장이 열려 있을 때만 종목 선정 로직을 돌려라" - live 는 이제 매수 가능 시간(ph)과는
    # 별개로, 국내 시장이 프리마켓·정규장·NXT 어떤 형태로든 열려 있다고 볼 수 있는 하루 전체
    # 구간(기본 08:00~21:00, 주말·공휴일 제외)으로 잰다. rescreen() 같은 배경 작업은 이 구간
    # 에서만 돈다 - 그 구간 밖(밤·주말·공휴일)에는 살 수도 없으니 새로 뽑아 봐야 쓸 곳이 없고
    # 랭킹 API 호출만 낭비된다. 실제 매수 가능 여부는 여전히 trading(ph 기반)만 본다 - 이 값을
    # 넓힌다고 매수 가능 시간대가 늘어나지는 않는다.
    try:
        day_start = parse_hhmm(getattr(cfg.entry, "selection_day_start", "08:00"))
        day_end = parse_hhmm(getattr(cfg.entry, "selection_day_end", "21:00"))
        live = not is_weekend and not is_holiday and day_start <= t <= day_end
    except Exception:
        live = ph in LIVE  # 설정값이 이상하면 예전 방식(매매 시간대 기준)으로 대신한다.

    if ph == "pre":
        target_date = now.date()
    elif ph in ("weekend", "holiday", "after"):
        target_date = _next_weekday(now.date())
    else:  # scan, manage - 오늘 몫은 이미 지났거나 쓰는 중이다. 다음은 다음 평일이다.
        target_date = _next_weekday(now.date())
    next_open = _fmt_next_open(now, target_date, scan_start)

    _SESSION_LABELS = {"premarket": "프리장", "regular": "본장", "nxt": "NXT장", "closed": "장 시간 아님"}
    if ph == "scan" and not trading:
        # ★ ph 는 "지금 신호를 평가해도 되는 시간대"만 볼 뿐, 사용자가 세션(프리장/본장/NXT장)
        # 자체를 꺼 뒀으면 여기서 막힌다 - 그걸 "지금은 매매 시간입니다"로 잘못 알리면 안 된다.
        why = f"{_SESSION_LABELS.get(d_phase, d_phase)} 매매를 [설정]에서 꺼 두셨습니다 - 신규 매수를 하지 않습니다(청산은 계속됩니다)."
    elif ph == "pre":
        why = f"아직 매매 시간 전입니다. {open_start.strftime('%H:%M')}부터 새로 살 수 있습니다."
    elif ph == "scan" and window == "open":
        why = "장 초반 변동성 시간대입니다 - 시초 갭 돌파 같은 이 시간대 전용 기법만 새로 삽니다."
    elif ph == "scan" and window == "close":
        why = "장 막판 변동성 시간대입니다 - 장 막판 상승 지속 같은 이 시간대 전용 기법만 새로 삽니다."
    elif ph == "scan":
        why = "지금은 매매 시간입니다."
    else:
        why = _WHY[ph]

    return {
        "phase": ph, "label": _PHASE_LABELS[ph], "trading": trading, "live": live, "window": window,
        "session": d_phase, "session_label": _SESSION_LABELS.get(d_phase, d_phase),
        "now": iso(now), "date": date,
        "scan_start": scan_start.strftime("%H:%M"), "scan_end": scan_end.strftime("%H:%M"),
        "force_close": force_close.strftime("%H:%M"),
        "holiday_known": holiday_known, "next_open": next_open, "why": why,
    }
