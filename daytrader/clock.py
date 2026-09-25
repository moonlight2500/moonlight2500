from __future__ import annotations

import time as _time
from datetime import date, timedelta

from daytrader.timeutil import combine, day_str, now_kst, parse_hhmm

# ★ 엔진이 시계를 직접 보지 않고 이 객체를 통해서만 보게 했다. 그래서 같은 엔진
# 코드가 실시간에서도, 3600배속 시뮬레이션에서도 똑같이 돈다.
# datetime.now() 를 엔진 안에서 직접 부르는 순간 이 성질이 깨진다.


def _parse_day(s: str) -> date:
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


class RealClock:
    sim = False
    speed = 1.0

    def now(self):
        return now_kst()

    def sleep(self, sim_seconds: float, should_stop=None) -> None:
        # 0.5초씩 잘게 나눠 잔다. 한 번에 30초를 자면 중단 버튼을 눌러도 30초 기다린다.
        remaining = sim_seconds
        step = 0.5
        while remaining > 0:
            if should_stop is not None and should_stop():
                return
            chunk = min(step, remaining)
            _time.sleep(chunk)
            remaining -= chunk


class SimClock:
    sim = True

    def __init__(self, start: str = "09:00", speed: float = 60.0, day: str | None = None):
        self.speed = speed
        self._day = day or day_str(now_kst())
        self._start_dt = combine(_parse_day(self._day), parse_hhmm(start))
        self._t0 = _time.monotonic()

    def now(self):
        elapsed = (_time.monotonic() - self._t0) * self.speed
        return self._start_dt + timedelta(seconds=elapsed)

    def sleep(self, sim_seconds: float, should_stop=None) -> None:
        # 가상시간 기준 sim_seconds 만큼 흐르게 하되, 실제로는 speed 로 나눈 만큼만 존다.
        real_seconds = sim_seconds / self.speed if self.speed else 0.0
        remaining = real_seconds
        step = 0.5
        while remaining > 0:
            if should_stop is not None and should_stop():
                return
            chunk = min(step, remaining)
            _time.sleep(chunk)
            remaining -= chunk


class FrozenClock:
    """멈춘 시계. 장중이면 실제 시각, 장외면 장중 한복판(기본 11:00)을 가리킨다.
    장외에 0시를 가리키면 데이터가 하나도 없는 화면이 나와 고장난 줄 안다.
    """

    sim = True
    speed = 0.0

    def __init__(self, mid_time: str = "11:00", market_open: str = "09:00", market_close: str = "15:30"):
        self._mid_time = mid_time
        self._open = parse_hhmm(market_open)
        self._close = parse_hhmm(market_close)

    def now(self):
        n = now_kst()
        if self._open <= n.time() <= self._close:
            return n
        return combine(n.date(), parse_hhmm(self._mid_time))

    def sleep(self, sim_seconds: float, should_stop=None) -> None:
        return


def make_clock(cfg):
    if cfg.mode in ("sim", "replay"):
        return SimClock(start=cfg.simulation.start_time, speed=cfg.simulation.speed)
    return RealClock()
