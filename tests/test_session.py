"""장 국면 판정(session.py) 테스트 - 특히 휴장일 판정. `python tests/test_session.py`로 실행한다."""

from __future__ import annotations

import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader.session import _HolidayCache  # noqa: E402

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


class _FakeClient:
    def __init__(self, resp):
        self.resp = resp

    def market_calendar_kr(self):
        return self.resp


def test_toss_shape_holiday() -> None:
    print("== ★ 실제로 겪은 버그(2026-09-24, 추석 연휴에도 종목 선정을 시도) - 진짜 토스 API 응답 모양 ==")
    # ★ 실제 토스 /market-calendar/KR 응답은 {"open": bool} 이 아니라
    # {"today": {"integrated": null 이면 휴장}} 모양이다 - 2026-09-24 에 직접 호출해 확인함.
    resp = {
        "today": {"date": "2026-09-24", "integrated": None},
        "previousBusinessDay": {"date": "2026-09-23", "integrated": {"regularMarket": {}}},
        "nextBusinessDay": {"date": "2026-09-28", "integrated": {"regularMarket": {}}},
    }
    cache = _HolidayCache()
    is_holiday, known = cache.check("2026-09-24", _FakeClient(resp))
    check("토스 응답에서 today.integrated 가 null 이면 휴장으로 판정", is_holiday is True and known is True,
          (is_holiday, known))


def test_toss_shape_business_day() -> None:
    print("== 토스 응답 모양 - 정상 거래일 ==")
    resp = {"today": {"date": "2026-09-23", "integrated": {"regularMarket": {}}}}
    cache = _HolidayCache()
    is_holiday, known = cache.check("2026-09-23", _FakeClient(resp))
    check("today.integrated 가 값이 있으면 거래일로 판정", is_holiday is False and known is True, (is_holiday, known))


def test_web_fallback_shape_still_works() -> None:
    print("== 인터넷 폴백(webquote.py)의 {\"open\": bool} 모양도 여전히 인식하는지 ==")
    cache_open = _HolidayCache()
    is_holiday, known = cache_open.check("2026-09-24", _FakeClient({"open": True, "date": "2026-09-24"}))
    check("웹 폴백: open=True 는 거래일", is_holiday is False and known is True, (is_holiday, known))

    cache_closed = _HolidayCache()
    is_holiday, known = cache_closed.check("2026-09-24", _FakeClient({"open": False, "date": "2026-09-24"}))
    check("웹 폴백: open=False 는 휴장일", is_holiday is True and known is True, (is_holiday, known))


def test_cache_reuses_same_date() -> None:
    print("== 같은 날짜로 다시 물으면 client 를 다시 안 부르고 캐시를 그대로 씀 ==")
    calls = {"n": 0}

    class CountingClient:
        def market_calendar_kr(self):
            calls["n"] += 1
            return {"today": {"date": "2026-09-24", "integrated": None}}

    cache = _HolidayCache()
    cache.check("2026-09-24", CountingClient())
    cache.check("2026-09-24", CountingClient())
    check("같은 날짜 두 번 호출해도 실제 조회는 1번뿐", calls["n"] == 1, calls["n"])


def test_client_none_defaults_to_trading_day() -> None:
    print("== client 가 없으면(sim 등) 거래일로 본다 - 대가가 작은 쪽으로 기운다 ==")
    cache = _HolidayCache()
    is_holiday, known = cache.check("2026-09-24", None)
    check("client=None -> (휴장 아님, 모름)", is_holiday is False and known is False, (is_holiday, known))


def test_client_exception_fails_open() -> None:
    print("== client 조회가 예외를 던지면 거래일로 본다(fail-open) ==")

    class BrokenClient:
        def market_calendar_kr(self):
            raise RuntimeError("네트워크 오류")

    cache = _HolidayCache()
    is_holiday, known = cache.check("2026-09-24", BrokenClient())
    check("조회 실패 -> (휴장 아님, 모름)", is_holiday is False and known is False, (is_holiday, known))


def test_domestic_phase_distinguishes_sessions() -> None:
    print("== domestic_phase() 가 프리장/본장/NXT장을 구분하는지(세션별 매매 토글의 전제) ==")
    from daytrader.session import domestic_phase
    import datetime as dt
    premarket = dt.datetime(2026, 9, 9, 8, 30)
    regular = dt.datetime(2026, 9, 9, 11, 0)
    nxt = dt.datetime(2026, 9, 9, 17, 0)
    outside = dt.datetime(2026, 9, 9, 22, 0)
    check("프리장 판정", domestic_phase(premarket) == "premarket", domestic_phase(premarket))
    check("본장 판정", domestic_phase(regular) == "regular", domestic_phase(regular))
    check("NXT장 판정", domestic_phase(nxt) == "nxt", domestic_phase(nxt))
    check("그 외 시간(심야)은 closed", domestic_phase(outside) == "closed", domestic_phase(outside))


def test_phase_blocks_entry_when_session_toggle_off() -> None:
    print("== ★ \"국장도 8시부터 9시까지 프리장... 장 별로 거래를 할지 사용자가 선택하게 해\" - "
          "세션 토글이 꺼져 있으면 신호 평가 시간대(scan)라도 매수는 막힘(청산과는 무관) ==")
    from daytrader.session import phase
    from daytrader.timeutil import parse_hhmm
    from types import SimpleNamespace
    import datetime as dt

    def _cfg(trade_premarket, trade_regular, trade_nxt):
        return SimpleNamespace(
            risk=SimpleNamespace(trade_premarket=trade_premarket, trade_regular=trade_regular, trade_nxt=trade_nxt),
            entry=SimpleNamespace(
                scan_start=parse_hhmm("09:20"), scan_end=parse_hhmm("14:00"),
                extra_windows=True, open_window_start="09:00", close_window_end="15:00",
            ),
            exit=SimpleNamespace(force_close_time=parse_hhmm("15:10")),
        )

    now = dt.datetime(2026, 9, 9, 11, 0)  # 정규장 시간대(본장)

    cfg = _cfg(False, True, False)  # 기본값과 같음 - 본장만 켜짐
    info = phase(cfg, now=now, client=None)
    check("본장 켜짐 + 정규장 시간 -> 매수 가능", info["trading"] is True, info)

    cfg2 = _cfg(False, False, False)  # 본장을 꺼둠
    info2 = phase(cfg2, now=now, client=None)
    check("본장 꺼짐 - 신호 평가 시간대(scan)여도 매수는 불가", info2["phase"] == "scan" and info2["trading"] is False, info2)
    check("이유 문구에 세션을 꺼 뒀다는 설명이 남음", "꺼" in info2["why"], info2["why"])
    check("session 필드가 본장으로 표시됨", info2["session"] == "regular", info2["session"])


def main() -> None:
    for t in (test_toss_shape_holiday, test_toss_shape_business_day, test_web_fallback_shape_still_works,
              test_cache_reuses_same_date, test_client_none_defaults_to_trading_day, test_client_exception_fails_open,
              test_domestic_phase_distinguishes_sessions, test_phase_blocks_entry_when_session_toggle_off):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
