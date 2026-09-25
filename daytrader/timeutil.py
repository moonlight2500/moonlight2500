from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

# 프로그램 안의 모든 datetime 은 KST-aware 로 통일한다.
# naive 와 aware 를 섞으면 TypeError 가 나거나, 더 나쁘게는 9시간 어긋난
# 보유시간이 조용히 계산된다. 파일에 쓸 때만 문자열이 되고,
# 읽을 때는 반드시 parse_dt() 를 거친다.

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(KST)


def to_kst(dt: datetime) -> datetime:
    """naive 는 KST 로 간주하고, aware 는 KST 로 변환한다."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def parse_dt(value) -> Optional[datetime]:
    """ISO 문자열 / 'Z' 표기 / naive(KST 로 간주) / datetime 객체를 모두 받는다.
    형식이 깨졌으면 예외 대신 None 을 돌려준다 - 호출부에서 건너뛰기 쉽게 하기 위해서다.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return to_kst(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        return to_kst(dt)
    return None


def iso(dt: datetime) -> str:
    return to_kst(dt).isoformat()


def day_str(dt: datetime) -> str:
    return to_kst(dt).strftime("%Y-%m-%d")


def hhmmss(dt: datetime) -> str:
    return to_kst(dt).strftime("%H:%M:%S")


def hhmm(t: time) -> str:
    return t.strftime("%H:%M")


def parse_hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def combine(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=KST)


def minutes_between(a: datetime, b: datetime) -> float:
    return (to_kst(b) - to_kst(a)).total_seconds() / 60.0


def monday_of(dt: datetime) -> str:
    d = to_kst(dt)
    monday = d - timedelta(days=d.weekday())
    return day_str(monday)


def trading_days_back(end: datetime, count: int) -> str:
    """end 로부터 주말을 건너뛰며 count 거래일 전 날짜를 돌려준다.
    공휴일은 다루지 않는다 - 별도 휴장일 캘린더가 생기면 그때 이 함수를 고친다.
    """
    d = to_kst(end)
    remaining = count
    while remaining > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            remaining -= 1
    return day_str(d)


def won(value) -> int:
    """float 로 손익을 누적하면 합계가 1원씩 안 맞는다. 원장에 넣기 전에 반드시 거친다."""
    return int(round(float(value)))


def josa(word: str, pair: str = "을/를") -> str:
    """받침 유무로 조사를 고른다. pair 는 "받침있음/받침없음" 순서의 문자열."""
    has, none = pair.split("/")
    if not word:
        return has
    ch = word[-1]
    code = ord(ch) - 0xAC00
    if 0 <= code <= 11171:
        return has if code % 28 != 0 else none
    return has


def with_josa(word: str, pair: str = "을/를") -> str:
    return f"{word}{josa(word, pair)}"
