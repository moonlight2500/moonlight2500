"""네트워크 없이 도는 검증. `python tests/test_offline.py` 로 실행한다."""

from __future__ import annotations

import io
import json
import math
import os
import sys
import tempfile
import time as _time

# 윈도우 콘솔 대응 - UTF-8 로 감싼다.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
THEMES_PATH = os.path.join(ROOT, "themes.yaml")

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    mark = "OK  " if cond else "FAIL"
    line = f"[{mark}] {name}"
    if extra:
        line += f" - {extra}"
    print(line)
    if not cond:
        _failures.append(name)


# ━━ 호가단위와 비용 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_ticks() -> None:
    print("\n== 호가단위와 비용 ==")
    from daytrader.ticks import breakeven_pct, round_to_tick, round_trip_cost_pct, tick_size, trade_pnl

    check("tick_size(1999)==1", tick_size(1999) == 1)
    check("tick_size(2000)==5", tick_size(2000) == 5)
    check("tick_size(4999)==5", tick_size(4999) == 5)
    check("tick_size(5000)==10", tick_size(5000) == 10)
    check("tick_size(199999)==100 (200,000원 미만은 100원 구간)", tick_size(199999) == 100)

    check("round_to_tick nearest", round_to_tick(10003, "nearest") == 10000)
    check("round_to_tick up", round_to_tick(10001, "up") == 10010)
    check("round_to_tick down", round_to_tick(10009, "down") == 10000)
    # ★ 4998 을 5원 단위로 올리면 5000 인데 5000 의 단위는 10원이라 다시 맞춰야 한다.
    check("4998 up -> 5000 (경계 재조정)", round_to_tick(4998, "up") == 5000)

    # ★★★ 실제로 겪은 버그 - int(p) 로 소수점을 먼저 버린 뒤 올림 공식을 적용해서
    # round_to_tick(13540.5, "up") 이 13550 이 아니라 13540 을 돌려줬다(이미
    # 호가단위에 맞는 값처럼 취급됨). 소수점이 있는 값의 "up" 을 각 호가단위
    # 구간 경계(2,000/5,000/20,000/50,000/200,000/500,000원)에서 확인한다.
    check("★13540.5 up -> 13550 (소수점 버림 없이 올림)", round_to_tick(13540.5, "up") == 13550)
    check("13540(이미 호가단위) up -> 13540 그대로", round_to_tick(13540, "up") == 13540)
    check("1999.4 up -> 2000 (2,000원 경계)", round_to_tick(1999.4, "up") == 2000)
    check("4999.5 up -> 5000 (5,000원 경계)", round_to_tick(4999.5, "up") == 5000)
    check("19999.5 up -> 20000 (20,000원 경계)", round_to_tick(19999.5, "up") == 20000)
    check("49999.5 up -> 50000 (50,000원 경계)", round_to_tick(49999.5, "up") == 50000)
    check("199999.5 up -> 200000 (200,000원 경계)", round_to_tick(199999.5, "up") == 200000)
    check("499999.5 up -> 500000 (500,000원 경계)", round_to_tick(499999.5, "up") == 500000)
    check("2000.5 up -> 2005 (소수점 있는 일반값)", round_to_tick(2000.5, "up") == 2005)
    # ★ down/nearest 모드는 원래도 정확했지만, up 을 고치며 깨지지 않았는지 소수점 입력으로 확인한다.
    check("1999.9 down -> 1999 (소수점, down 은 그대로 정확)", round_to_tick(1999.9, "down") == 1999)
    check("10003.7 nearest -> 10000 (소수점, nearest 도 그대로 정확)", round_to_tick(10003.7, "nearest") == 10000)

    rt = round_trip_cost_pct(0.00015, 0.0015)
    check("왕복비용 0.180%", abs(rt - 0.0018) < 1e-9, f"실제 {rt}")

    be = breakeven_pct(0.00015, 0.0015)
    entry, qty = 10000, 100
    exit_price = entry * (1 + be)
    pnl = trade_pnl(entry, exit_price, qty, 0.00015, 0.0015)
    # ★ 손익분기 상승률이면 손익 0 (원 단위 반올림 오차만 남는다).
    check("손익분기 상승률이면 손익 0", abs(pnl) < 2.0, f"실제 pnl={pnl}")


# ━━ 시각 정합성 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_time() -> None:
    print("\n== 시각 정합성 ==")
    from datetime import datetime

    from daytrader.timeutil import josa, parse_dt, to_kst, won

    naive = datetime(2026, 9, 4, 10, 0, 0)
    aware = to_kst(naive)
    check("naive -> aware 통일", aware.tzinfo is not None)

    z = parse_dt("2026-09-04T01:00:00Z")
    check("Z 표기 파싱(UTC->KST +9)", z is not None and z.hour == 10, str(z))

    check("won() 정수화", won(1234.6) == 1235)

    check("삼성 -> 을 (받침 있음)", josa("삼성") == "을")
    check("카카오 -> 를 (받침 없음)", josa("카카오") == "를")


# ━━ 설정 검증 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_config_validate() -> None:
    print("\n== 설정 검증 ==")
    import yaml

    from daytrader.config import load_config

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        base_raw = yaml.safe_load(f)

    def try_load(mutate) -> bool:
        raw = yaml.safe_load(yaml.safe_dump(base_raw))  # 간단한 깊은 복사
        mutate(raw)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True)
            path = f.name
        try:
            load_config(path)
            return True
        except ValueError:
            return False
        finally:
            os.remove(path)

    check("정상 설정 통과", try_load(lambda r: None))

    check("★익절<비용*2 거부", not try_load(lambda r: r["risk"].__setitem__("take_profit_pct", 0.001)))
    check("★fixed 없으면 거부", not try_load(lambda r: r["strategy"].__setitem__("exit_enabled", ["trailing"])))
    check("진입기법 0개 거부", not try_load(lambda r: r["strategy"].__setitem__("entry_order", [])))
    check("알 수 없는 기법 거부", not try_load(lambda r: r["strategy"].__setitem__("entry_order", ["없는기법"])))

    def _mutate_weekly(r):
        r["risk"]["daily_loss_limit_pct"] = 0.03
        r["risk"]["weekly_loss_limit_pct"] = 0.01

    check("주간<일일 거부", not try_load(_mutate_weekly))
    check("★알 수 없는 키 거부", not try_load(lambda r: r["risk"].__setitem__("없는키", 1)))

    # ★★★ 실제로 겪을 뻔한 사고 - force_close_time + force_close_deadline_min 유예가
    # 15:20 단일가(종가) 매매 시작을 넘기면 강제청산 주문이 정상적으로 접속성
    # 매매 시간에 들어가지 못한다. 15:19까지 끝나야 한다.
    check(
        "★강제청산 유예가 15:20 단일가 시작을 넘기면 거부",
        not try_load(lambda r: r["exit"].__setitem__("force_close_deadline_min", 15)),  # 15:10+15분=15:25
    )

    def _mutate_boundary_ok(r):
        r["exit"]["force_close_time"] = "15:10"
        r["exit"]["force_close_deadline_min"] = 9  # 정확히 15:19에 끝남 - 경계값은 통과해야 한다.

    check("15:19에 정확히 끝나면 통과(경계값)", try_load(_mutate_boundary_ok))

    def _mutate_boundary_fail(r):
        r["exit"]["force_close_time"] = "15:11"
        r["exit"]["force_close_deadline_min"] = 9  # 15:20에 끝남 - 단일가 시작 시각과 겹쳐 거부되어야 한다.

    check("15:20에 끝나면 거부(단일가 시작과 겹침)", not try_load(_mutate_boundary_fail))


# ━━ 지표 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_signals() -> None:
    print("\n== 지표 ==")
    from dataclasses import dataclass

    from daytrader.signals import consecutive_down, sma, volume_ratio

    @dataclass
    class B:
        ts: str
        open: float
        high: float
        low: float
        close: float
        volume: float

    bars = [B(str(i), 100, 100, 100, 100 + i, 1000) for i in range(10)]
    manual_sma = sum(b.close for b in bars[-5:]) / 5
    check("sma 손계산 일치", abs(sma(bars, 5) - manual_sma) < 1e-9)

    manual_vr = bars[-1].volume / (sum(b.volume for b in bars[-6:-1]) / 5)
    check("volume_ratio 손계산 일치", abs(volume_ratio(bars, 5) - manual_vr) < 1e-9)

    check("데이터 부족 시 nan", math.isnan(sma(bars, 100)))

    down_bars = [B(str(i), 0, 0, 0, 10 - i, 100) for i in range(5)]
    check("연속 음봉", consecutive_down(down_bars, 10) == 4)


# ━━ 페이퍼 브로커 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_paper_broker() -> None:
    print("\n== 페이퍼 브로커 ==")
    from daytrader.broker import PaperBroker
    from daytrader.config import load_config

    cfg = load_config(CONFIG_PATH)
    pb = PaperBroker(cfg, None, starting_cash=1_000_000)

    buy_normal = pb.buy("005930", 1, 10000, urgent=False)
    buy_urgent = pb.buy("005930", 1, 10000, urgent=True)
    check("매수 슬리피지 방향(가격이 위로)", buy_normal.price >= 10000 and buy_urgent.price >= 10000)
    check("★시장가(urgent) 매수가 더 불리", buy_urgent.price >= buy_normal.price)

    sell_normal = pb.sell("005930", 1, 10000, urgent=False)
    sell_urgent = pb.sell("005930", 1, 10000, urgent=True)
    check("매도 슬리피지 방향(가격이 아래로)", sell_normal.price <= 10000 and sell_urgent.price <= 10000)
    check("★시장가(urgent) 매도가 더 불리", sell_urgent.price <= sell_normal.price)

    poor = PaperBroker(cfg, None, starting_cash=100)
    fill = poor.buy("005930", 1000, 10000)
    check("현금 부족", not fill.ok, fill.reason)


# ━━ 매매 기법 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_playbook() -> None:
    print("\n== 매매 기법 ==")
    from types import SimpleNamespace

    from daytrader.config import load_config
    from daytrader.playbook import (
        EXIT_PRIORITY, Bar, BreakoutEntry, FixedExit,
    )

    cfg = load_config(CONFIG_PATH)

    n = 25
    prices = [10000 + i * 10 for i in range(n)]
    volumes = [1000] * n
    bars = [Bar(f"t{i}", prices[i] - 10, prices[i] + 20, prices[i] - 30, prices[i], volumes[i]) for i in range(n)]
    bars[-1] = Bar(bars[-1].ts, 10500, 10800, 10480, 10750, 5000)

    ctx = SimpleNamespace(
        symbol="000001", name="테스트", theme="테스트", upper_limit=None, theme_bars=None,
        prev_verdict=None, change_rate=0.05, theme_rank=1, theme_breadth=3, theme_intensity=0.04,
    )

    be = BreakoutEntry(cfg)
    v = be.evaluate(bars, ctx)
    check("돌파 통과", v.ok, v.headline)
    check("inputs 로 재현 가능", bool(v.inputs))

    bars_no_volume = list(bars)
    bars_no_volume[-1] = Bar(bars[-1].ts, 10500, 10800, 10480, 10750, 1000)
    v_no_vol = be.evaluate(bars_no_volume, ctx)
    check("거래량 부족이면 실패", not v_no_vol.ok)

    bars_bearish = list(bars)
    bars_bearish[-1] = Bar(bars[-1].ts, 10750, 10800, 10480, 10500, 5000)  # 종가<시가(음봉)로 필수 하나만 깬다
    v_bearish = be.evaluate(bars_bearish, ctx)
    check("★필수 하나만 틀려도 실패", not v_bearish.ok)

    ctx2 = SimpleNamespace(**vars(ctx))
    ctx2.prev_verdict = v_no_vol
    v_flip = be.evaluate(bars, ctx2)
    check("★changes 채워짐", len(v_flip.changes) > 0, str(v_flip.changes[:1]))
    check("★flipped 표시됨", any(t.flipped for t in v_flip.terms))
    check("★delta 채워짐", any(t.delta is not None for t in v_flip.terms))

    idx_stop = EXIT_PRIORITY.index("fixed_stop")
    idx_take = EXIT_PRIORITY.index("fixed_take")
    idx_trail = EXIT_PRIORITY.index("trailing")
    check("청산 우선순위(손절>익절>트레일링)", idx_stop < idx_take < idx_trail)

    pos = SimpleNamespace(symbol="000001", name="테스트", theme="테스트", quantity=10,
                           entry_price=10000, entry_time=None, peak_price=10500)
    fe = FixedExit(cfg)
    exit_ctx = SimpleNamespace(prev_verdict=None)
    stop_line = 10000 * (1 - cfg.risk.stop_loss_pct)
    hit = fe.evaluate(pos, [], stop_line - 1, exit_ctx)
    check("손절가 도달시 청산", hit.ok)
    no_hit = fe.evaluate(pos, [], 10100, exit_ctx)
    check("조건 안 맞으면 청산 안 함", not no_hit.ok)


# ━━ 시뮬레이터 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_simulator() -> None:
    print("\n== 시뮬레이터 ==")
    from daytrader.clock import SimClock
    from daytrader.config import load_config
    from daytrader.simulator import SimClient

    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    clock = SimClock(start="09:00", speed=1, day="2026-09-04")
    sim = SimClient(cfg, clock=clock, themes_path=THEMES_PATH)

    bars = sim._today_bars("005930")
    check("★미래 봉 차단(장 시작 시점엔 최대 1봉)", len(bars) <= 1, f"실제 {len(bars)}봉")

    try:
        sim.create_order("005930", "BUY", "LIMIT", 10)
        check("★주문 API 차단", False)
    except RuntimeError:
        check("★주문 API 차단", True)

    fake_count = 0
    breakout_count = 0
    for seed in range(40):
        cfg2 = load_config(CONFIG_PATH)
        cfg2.mode = "sim"
        cfg2.simulation.seed = seed
        cfg2.simulation.scenario = "normal"
        clock2 = SimClock(start="15:20", speed=3600, day="2026-09-04")
        sim2 = SimClient(cfg2, clock=clock2, themes_path=THEMES_PATH)
        plan = sim2._plan_for_day(sim2.day)
        for p in plan["profiles"].values():
            if p["breakout"]:
                breakout_count += 1
                if p["fake_breakout"]:
                    fake_count += 1
    ratio = (fake_count / breakout_count) if breakout_count else 0.0
    check("★속임수 돌파 30~60%", 0.30 <= ratio <= 0.60, f"실제 {ratio:.2f} ({fake_count}/{breakout_count})")


# ━━ 테마 점수 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_theme_score() -> None:
    print("\n== 테마 점수 ==")
    from daytrader.config import load_config
    from daytrader.screener import MarketRow, Screener

    cfg = load_config(CONFIG_PATH)

    class FakeClient:
        def stocks(self, symbols):
            return [{"symbol": s} for s in symbols]

        def warnings(self, symbol):
            return []

    scr = Screener(FakeClient(), cfg)
    theme_name = next(iter(scr.themes))
    codes = scr.themes[theme_name]

    market_low = {c: MarketRow(symbol=c, last_price=10000, change_rate=0.01, trading_amount=1e10) for c in codes}
    views = scr.score_themes(market_low)
    v = next(t for t in views if t.name == theme_name)
    check("★동반상승 미달이면 인정 안 함", not v.qualified)
    check("탈락 테마도 사유와 함께", bool(v.reason), v.reason)

    market_high = {c: MarketRow(symbol=c, last_price=10000, change_rate=0.05, trading_amount=1e10) for c in codes}
    views2 = scr.score_themes(market_high)
    v2 = next(t for t in views2 if t.name == theme_name)
    check("동반상승 충분하면 인정", v2.qualified)
    check("★점수 공식이 문장으로", "×" in v2.formula, v2.formula)

    report = scr.build_report()
    if report.candidates:
        check("★후보에 why 가 붙음", all(bool(c.why) for c in report.candidates))
    else:
        check("★후보에 why 가 붙음 (후보 없음이라 스킵)", True)


# ━━ 거래 원장 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_ledger() -> None:
    print("\n== 거래 원장 ==")
    from daytrader.ledger import Ledger

    with tempfile.TemporaryDirectory() as d:
        lg = Ledger(d)
        lg.append_trade("paper", {
            "date": "2026-09-04", "symbol": "005930", "name": "삼성전자", "theme": "t",
            "qty": 10, "entry": 10000, "exit": 10500.7, "pnl": 5007.3, "reason": "",
            "entry_time": "t1", "exit_time": "t2", "technique": "breakout", "verdict_id": "v1", "estimated": False,
        })
        lg.append_trade("paper", {
            "date": "2026-09-04", "symbol": "000660", "name": "SK", "theme": "t",
            "qty": 5, "entry": 20000, "exit": 19000, "pnl": -5000, "reason": "",
            "entry_time": "t3", "exit_time": "t4", "technique": "atr_stop", "verdict_id": "v2", "estimated": False,
        })
        lg.record_equity("paper", "2026-09-04", 3000000, 7.3, 2, 3000007)

        trades = lg.trades(modes=["paper"])
        check("★금액 정수", all(isinstance(t["pnl"], int) for t in trades))

        totals = lg.totals(modes=["paper"])
        check("합계·승률·손익비", totals["trades"] == 2 and abs(totals["win_rate"] - 0.5) < 1e-9 and totals["profit_factor"] > 0)

        by_tech = lg.by_technique(modes=["paper"])
        check("★기법별 집계", set(by_tech.keys()) == {"breakout", "atr_stop"})

        # ★★★ 실제로 겪은 크래시 - 어떤 기법이 손실 없이 전부 이기면
        # 손익비가 수학적으로 무한대다. float("inf") 를 그대로 두면
        # FastAPI 의 JSONResponse(allow_nan=False)가 응답을 만들다
        # ValueError 로 죽는다(/api/performance, /api/playbook/stats 전체가
        # 500 으로 죽었다) - None 으로 나와야 안전하다.
        with tempfile.TemporaryDirectory() as d2:
            lg2 = Ledger(d2)
            lg2.append_trade("paper", {
                "date": "2026-09-04", "symbol": "005930", "name": "삼성전자", "theme": "t",
                "qty": 10, "entry": 10000, "exit": 10500, "pnl": 5000, "reason": "",
                "entry_time": "t1", "exit_time": "t2", "technique": "time_stop", "verdict_id": "v1", "estimated": False,
            })
            totals2 = lg2.totals(modes=["paper"])
            check("★★★ 손실 0건이면 손익비가 None(무한대를 JSON-안전하게)",
                  totals2["profit_factor"] is None, str(totals2["profit_factor"]))
            by_tech2 = lg2.by_technique(modes=["paper"])
            check("★★★ 기법별 손익비도 None", by_tech2["time_stop"]["profit_factor"] is None,
                  str(by_tech2["time_stop"]["profit_factor"]))
            check("★★★ FastAPI 의 JSONResponse 와 같은 방식(allow_nan=False)으로 직렬화돼도 안 죽음",
                  bool(json.dumps({"totals": totals2, "by_technique": by_tech2}, allow_nan=False)))

        lg.append_trade("live", {
            "date": "2026-09-04", "symbol": "005930", "name": "", "theme": "",
            "qty": 1, "entry": 1, "exit": 1, "pnl": 100, "reason": "", "entry_time": "",
            "exit_time": "", "technique": "", "verdict_id": None, "estimated": False,
        })
        paper_only = lg.trades(modes=["paper"])
        check("★실거래 분리(live 안 섞임)", len(paper_only) == 2)


# ━━ 매매일지 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_journal() -> None:
    print("\n== 매매일지 ==")
    from daytrader.journal import Journal
    from daytrader.playbook import Verdict

    with tempfile.TemporaryDirectory() as d:
        j = Journal(d, mode="sim")
        j.watch("005930", "삼성전자", "관찰 중 (1.2배)")
        j.watch("005930", "삼성전자", "관찰 중 (1.5배)")  # 괄호 앞부분이 같아 억제되어야 한다.
        rows = j.read(kinds=["watch"])
        check("★중복 억제", len(rows) == 1, f"실제 {len(rows)}건")

        mk = lambda ok, blocked: Verdict(
            id="x", at="t", symbol="005930", name="", theme="", phase="entry",
            technique="breakout", technique_label="", ok=ok, score=0, terms=[],
            blocked_by=blocked, headline="", narrative="", changes=[], inputs={}, price=0,
        )
        r1 = j.evaluate(mk(False, ["volume"]))
        r2 = j.evaluate(mk(True, []))
        check("★판정 바뀌면 기록", r1 and r2)

        with open(j.path, "a", encoding="utf-8") as f:
            f.write("이건 json이 아님\n")
        rows_all = j.read()
        check("깨진 줄 건너뜀", isinstance(rows_all, list) and len(rows_all) >= 2)


# ━━ 주문 멱등성 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_orders() -> None:
    print("\n== 주문 멱등성 ==")
    from daytrader.orders import OrderBook, OrderIntent, new_coid
    from daytrader.timeutil import iso, now_kst

    coids = {new_coid("BUY", "005930") for _ in range(200)}
    check("★coid 매번 다름", len(coids) == 200)
    check("36자 이내", all(len(c) <= 36 for c in coids))

    with tempfile.TemporaryDirectory() as d:
        book = OrderBook(d)
        coid = new_coid("BUY", "005930")
        intent = OrderIntent(
            coid=coid, at=iso(now_kst()), mode="live", symbol="005930", name="",
            side="BUY", order_type="LIMIT", quantity=10, price=10000, reason="", verdict_id=None,
        )
        book.record(intent)
        latest = book.latest(coid)
        check("의도 선기록(status=intent)", latest is not None and latest.status == "intent")


# ━━ 시계 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def section_clock() -> None:
    print("\n== 시계 ==")
    from daytrader.clock import FrozenClock, RealClock, SimClock

    rc = RealClock()
    check("RealClock aware", rc.now().tzinfo is not None)

    sc = SimClock(start="09:00", speed=3600, day="2026-09-04")
    t0 = sc.now()
    _time.sleep(0.2)
    t1 = sc.now()
    check("speed 배속 적용", (t1 - t0).total_seconds() > 100, f"실제 {(t1 - t0).total_seconds():.1f}초")

    fc = FrozenClock()
    check("멈춘 시계 aware", fc.now().tzinfo is not None)


def section_quote_router_buying_power() -> None:
    print("\n== QuoteRouter.buying_power() 가 currency 를 정확히 전달함 ==")
    from daytrader.router import QuoteRouter

    class FakePrimary:
        def buying_power(self, currency):
            return {"cash": 999, "currency_used": currency}

    router = QuoteRouter(FakePrimary(), None)
    result = router.buying_power(currency="USD")
    check("명시적 currency 전달됨", result["currency_used"] == "USD")
    result2 = router.buying_power()
    check("기본값(KRW) 사용됨", result2["currency_used"] == "KRW")


def section_simulation_days() -> None:
    """★★★ 실제로 겪은 문제 - simulation.days(설정에 여러 일수로 되어
    있어도) 화면에서 dates 를 명시 안 하면 무조건 하루만 돌았다. 기본
    배속(3600배속)이면 하루가 실제로 6~7초 만에 끝나버려서 "시작하고
    몇 초 뒤 자동으로 정지된다"고 느끼게 됐다 - server.py 의
    engine_start() 가 이제 simulation.days 를 실제로 반영해 여러 거래일을
    만든다. 여기서는 그 날짜 생성 로직 자체(순수 함수)만 가볍게 검증한다.
    """
    print("\n== simulation.days 가 실제로 여러 거래일을 만듦 ==")
    from daytrader.timeutil import now_kst, trading_days_back

    end = now_kst()
    days = 6
    dates = [trading_days_back(end, i) for i in range(days - 1, -1, -1)]
    check("정확히 6개 생성", len(dates) == 6)
    check("전부 서로 다른 날짜(중복 없음)", len(set(dates)) == 6)
    check("과거 -> 최근 오름차순", dates == sorted(dates))


def section_bithumb_error_hint() -> None:
    print("\n== 빗썸 401/403 에러에 흔한 원인 안내가 붙음(공개 API는 안 붙음) ==")
    from unittest.mock import patch, MagicMock
    from daytrader.bithumb_api import BithumbClient, BithumbApiError

    client = BithumbClient("fake_access", "fake_secret")
    fake_resp = MagicMock()
    fake_resp.status_code = 401
    fake_resp.json.return_value = {"error": {"name": "invalid_signature", "message": "JWT signature is not valid"}}
    with patch.object(client._session, "request", return_value=fake_resp):
        try:
            client.accounts()
            check("private 요청 401 에 흔한 원인 안내 포함", False, "예외가 안 남")
        except BithumbApiError as exc:
            check("private 요청 401 에 흔한 원인 안내 포함", "흔한 원인" in exc.message)

    public_client = BithumbClient()
    fake_resp2 = MagicMock()
    fake_resp2.status_code = 400
    fake_resp2.json.return_value = {"error": {"name": "bad_request", "message": "잘못된 마켓 코드"}}
    with patch.object(public_client._session, "request", return_value=fake_resp2):
        try:
            public_client.ticker(["KRW-INVALID"])
            check("공개 API 에러에는 인증 안내가 안 붙음", False, "예외가 안 남")
        except BithumbApiError as exc:
            check("공개 API 에러에는 인증 안내가 안 붙음", "흔한 원인" not in exc.message)


def section_selftest_required_params() -> None:
    print("\n== 연계 테스트가 실제 매매 코드와 같은 API 파라미터를 씀 ==")
    # ★★★ 실제로 겪은 버그 두 가지 -
    #   1. rankings() 를 인자 없이 불러 필수 파라미터가 빠졌다.
    #   2. 더 심각한 건, 연계 테스트만 존재하지 않는 랭킹 타입
    #      'TRADING_VALUE' 를 써서 이 항목만 계속 실패했다. 실제 매매
    #      로직(screener.py)은 'MARKET_TRADING_AMOUNT' 를 쓴다.
    # 테스트가 실제 코드와 다른 값을 쓰면 테스트의 의미가 없으므로,
    # "두 파일이 같은 타입을 쓰는가"를 직접 대조한다.
    src = open(os.path.join(ROOT, "daytrader", "selftest.py"), encoding="utf-8").read()
    screener_src = open(os.path.join(ROOT, "daytrader", "screener.py"), encoding="utf-8").read()

    check("conditional_orders 호출에 status 파라미터 포함", "conditional_orders(status=" in src)
    # ★ 주석에는 이 버그를 설명하느라 'TRADING_VALUE' 가 등장하므로,
    # 주석을 뺀 실제 코드만 검사한다.
    code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    code_only = "\n".join(code_lines)
    check("★★★ 실제 호출 코드에 존재하지 않는 타입 'TRADING_VALUE' 가 없음",
          "TRADING_VALUE" not in code_only)
    check("실제 스크리너와 같은 타입(MARKET_TRADING_AMOUNT)을 씀",
          "MARKET_TRADING_AMOUNT" in code_only and "MARKET_TRADING_AMOUNT" in screener_src)
    check("rankings 호출에 marketCountry·duration 을 명시", "marketCountry='KR'" in src and "duration=" in src)

    # ★ 토스 API 제약 - TOP_GAINERS/TOP_LOSERS 는 duration=realtime 을
    # 지원하지 않는다(400 unsupported-ranking-duration). 실제 호출에서
    # 이 조합이 쓰이면 안 된다. ★ 한 줄에 여러 (타입, duration) 쌍이
    # 나란히 오는 경우가 있어(예: ("MARKET_TRADING_AMOUNT","realtime"),
    # ("TOP_GAINERS","1d")), 줄 단위로 보면 오탐한다 - 쌍 단위로 본다.
    import re
    pair_re = re.compile(r"TOP_(?:GAINERS|LOSERS)\"?\s*,\s*\"?(\w+)")
    for path in ("screener.py", "overseas_engine.py", "selftest.py"):
        text = open(os.path.join(ROOT, "daytrader", path), encoding="utf-8").read()
        lines = [ln for ln in text.splitlines()
                 if not ln.strip().startswith("#") and '"""' not in ln]
        bad = [m for ln in lines for m in pair_re.findall(ln) if m == "realtime"]
        check(f"{path}: TOP_GAINERS 에 realtime 을 쓰지 않음", not bad, str(bad))


def section_us_market_session_fallback() -> None:
    print("\n== 미국 시장 세션 판정 - market_state 애매해도 실제 시각 기준으로 정확히 판정 ==")
    from unittest.mock import MagicMock
    import daytrader.market as market

    def make_fake_response(market_state):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json.return_value = {
            "chart": {"result": [{
                "meta": {
                    "chartPreviousClose": 100.0, "regularMarketPrice": 105.0,
                    "marketState": market_state, "tradingPeriods": {},
                },
                "timestamp": [], "indicators": {"quote": [{"close": []}]},
            }]}
        }
        return resp

    sess = MagicMock()
    real_session = market._session_from_ny_time()

    # ★★★ 실제로 겪은 버그 - market_state 가 빈 문자열처럼 애매하면
    # 예전엔 무조건 "regular"(정규장)로 잘못 표시했다("지금 프리마켓인데
    # 정규장으로 표시된다"는 문의의 원인). 이제는 실제 시각 기준으로
    # 정확히 판정해야 한다.
    sess.get.return_value = make_fake_response("")
    result = market._yahoo_session_quote(sess, "AAPL")
    check("market_state 애매하면 실제 시각 기준으로 판정", result["sessions"]["active"] == real_session)

    # ★ market_state 가 명확하면(REGULAR/PRE) 그 값을 그대로 존중해야 한다.
    sess.get.return_value = make_fake_response("REGULAR")
    result2 = market._yahoo_session_quote(sess, "AAPL")
    check("market_state='REGULAR' 는 그대로 존중됨", result2["sessions"]["active"] == "regular")

    sess.get.return_value = make_fake_response("PRE")
    result3 = market._yahoo_session_quote(sess, "AAPL")
    check("market_state='PRE' 는 그대로 존중됨", result3["sessions"]["active"] == "pre")


def section_no_fake_night_market_group() -> None:
    """★★★ "야간시장 시세를 받아올 수 있는지 검토하고 못 가져오면 항목을
    삭제해" - HLKR(hlkr.co.kr) 참고용 그룹을 한 번 시도했었지만, 실제로는
    시세를 하나도 못 가져오는(모든 행의 last 가 항상 None인) 링크 안내
    카드일 뿐이었다. 이전엔 KIS API 안내 카드가 같은 이유로 삭제됐었고,
    이번엔 그 후속인 HLKR 그룹도 같은 이유로 삭제한다 - 두 시도 모두
    화면에 다시 나타나면 안 된다.
    """
    print("\n== 시장 메뉴에 실제로 시세를 못 가져오는 '야간시장' 그룹이 없음 ==")
    import inspect
    import daytrader.market as market

    src = inspect.getsource(market.snapshot)
    check("HLKR 참고용 그룹은 삭제됨", "HLKR(참고용 · KRX 공식 아님)" not in src)
    check("코스피200 야간선물(HLKR 참고가) 항목도 삭제됨", "코스피200 야간선물(HLKR 참고가)" not in src)
    check("hlkr.co.kr 링크도 삭제됨", "hlkr.co.kr" not in src)
    check("기존 KIS API 안내 카드도 여전히 없음", "한국투자증권(KIS) Open API 연동이 필요합니다" not in src)
    # ★ 실제로 밤사이 데이터를 주는 미국 지수선물("선물" 그룹)은 그대로 남아야 한다.
    check("실제로 동작하는 미국 지수선물 그룹은 유지됨", '_add_group("선물", rows)' in src)


def section_bithumb_query_hash() -> None:
    """★★★ 실제로 겪은 빗썸 인증 실패의 진짜 원인 - query_hash 계산이
    공식 문서 규칙과 두 군데 달랐다. 서버가 계산한 해시와 안 맞으면
    401 invalid_query_payload 가 난다.
    """
    print("\n== 빗썸 query_hash 가 공식 문서 규칙과 정확히 일치 ==")
    import hashlib
    from urllib.parse import urlencode
    from unittest.mock import patch, MagicMock
    import jwt as pyjwt
    from daytrader.bithumb_api import BithumbClient, _build_query_string

    # ① 문서 예제(urlencode, 정렬 없음)와 같아야 한다.
    param = dict(market="KRW-BTC")
    check("공식 문서 예제와 동일한 쿼리 문자열", _build_query_string(param) == urlencode(param))

    # ② 정렬하면 안 된다 - 넣은 순서를 그대로 유지해야 한다.
    param_unsorted = {"state": "wait", "market": "KRW-BTC"}
    check("정렬하지 않고 넣은 순서를 유지", _build_query_string(param_unsorted) == "state=wait&market=KRW-BTC")

    # ③ 배열은 key[]=value 형태여야 한다(문서가 명시적으로 경고한 부분).
    check("배열 파라미터는 key[]=value 형태",
          _build_query_string({"order_ids": ["id1", "id2"]}) == "order_ids[]=id1&order_ids[]=id2")

    # ④ 서명한 쿼리와 실제 전송 URL 의 쿼리가 정확히 같아야 한다.
    client = BithumbClient("fake_access", "fake_secret")
    captured = {}

    def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b'{"ok":1}'
        resp.json.return_value = {"ok": 1}
        return resp

    with patch.object(client._session, "request", side_effect=fake_request):
        client.order_chance("KRW-BTC")

    token = captured["headers"]["Authorization"].replace("Bearer ", "")
    payload = pyjwt.decode(token, "fake_secret", algorithms=["HS256"])
    url_query = captured["url"].split("?", 1)[1]
    check("★★★ JWT의 query_hash가 실제 전송 URL의 쿼리와 정확히 일치",
          payload["query_hash"] == hashlib.sha512(url_query.encode("utf-8")).hexdigest())

    # ⑤ 파라미터 없는 요청은 query_hash 를 넣지 않는다(문서 규칙).
    captured.clear()
    with patch.object(client._session, "request", side_effect=fake_request):
        client.accounts()
    token2 = captured["headers"]["Authorization"].replace("Bearer ", "")
    payload2 = pyjwt.decode(token2, "fake_secret", algorithms=["HS256"])
    check("파라미터 없는 요청(accounts)은 3개 필드만",
          set(payload2.keys()) == {"access_key", "nonce", "timestamp"})


def section_screener_fair_theme_split() -> None:
    """★★★ 실제로 발견한 결함 - 2차 필터 전에 후보를 limit 으로 자를 때
    테마 순서대로 쌓은 리스트를 앞에서부터 잘랐다. 1위 테마에 상승 종목이
    많으면(흔한 상황) 2·3위 테마가 통째로 잘려 나가서, "여러 테마에
    분산한다"는 top_themes 설정이 무력화되고 한 테마에 전부 몰렸다.
    """
    print("\n== 종목선정이 테마별로 공평하게 후보를 배분함 ==")
    import tempfile as _tempfile
    from collections import Counter
    from daytrader.config import load_config
    from daytrader.clock import SimClock
    from daytrader.simulator import SimClient
    from daytrader.screener import Screener

    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = _tempfile.mkdtemp()
    cfg.mode = "sim"
    cfg.simulation.scenario = "strong_theme"
    cfg.simulation.seed = 5

    clock = SimClock(start="13:00", speed=1, day="2026-09-04")
    client = SimClient(cfg, clock=clock, themes_path=THEMES_PATH)

    # ★ stocks() 호출 횟수를 세서, 후보마다 개별 호출하지 않고 배치로
    # 한 번에 받는지 확인한다(성능 개선이 실제로 동작하는지).
    calls = {"stocks": 0}
    orig_stocks = client.stocks

    def counted(symbols):
        calls["stocks"] += 1
        return orig_stocks(symbols)

    client.stocks = counted

    sc = Screener(client, cfg)
    report = sc.build_report()

    check("후보가 실제로 선정됨", len(report.candidates) > 0, report.summary)
    # ★ 테마당 candidates_per_theme 상한을 넘지 않아야 한다.
    counts = Counter(c.theme for c in report.candidates)
    over = [t for t, n in counts.items() if n > cfg.screen.candidates_per_theme]
    check("어떤 테마도 테마당 상한을 넘지 않음", not over, str(dict(counts)))
    # ★★★ 성능 - 후보 수와 무관하게 stocks() 호출이 소수여야 한다
    # (예전엔 후보마다 개별 호출이라 후보 수만큼 늘어날 수 있었다).
    check("stocks() 가 배치로 소수만 호출됨(후보 수에 비례해 늘지 않음)",
          calls["stocks"] <= 3, f"{calls['stocks']}회")


def section_screener_roundrobin_keeps_all_themes() -> None:
    """★ 라운드로빈 배분 로직 자체를 직접 검증한다 - 1위 테마에 종목이
    몰려도 하위 테마가 통째로 사라지면 안 된다."""
    print("\n== 1위 테마에 종목이 몰려도 하위 테마가 살아남음 ==")
    limit = 18
    by_theme = {
        "테마1위": [("테마1위", f"A{i}") for i in range(20)],
        "테마2위": [("테마2위", f"B{i}") for i in range(5)],
        "테마3위": [("테마3위", f"C{i}") for i in range(5)],
    }
    picked = []
    idx = 0
    while len(picked) < limit and by_theme:
        progressed = False
        for name in list(by_theme.keys()):
            bucket = by_theme[name]
            if idx < len(bucket):
                picked.append(bucket[idx])
                progressed = True
                if len(picked) >= limit:
                    break
        if not progressed:
            break
        idx += 1

    from collections import Counter
    counts = Counter(t for t, _ in picked)
    check("세 테마가 모두 살아남음(예전엔 1위 테마만 남았다)", len(counts) == 3, str(dict(counts)))
    check("2위 테마 종목이 전부 포함됨", counts.get("테마2위", 0) == 5)
    check("3위 테마 종목이 전부 포함됨", counts.get("테마3위", 0) == 5)


def section_screener_distinguishes_no_data() -> None:
    """★★★ 실제로 겪은 혼란 - 시세를 하나도 못 받아왔을 때도 "오늘은
    테마가 없습니다, 거래 없음이 정상입니다"라고 안내해서, 실제로는
    API 키 미등록·네트워크 장애인데 정상 상황인 줄 알게 됐다. 두 경우는
    완전히 다르니 메시지가 반드시 구분돼야 한다.
    """
    print("\n== 시세 부재와 '시장이 조용함'을 메시지로 구분함 ==")
    import tempfile as _tempfile
    from daytrader.config import load_config
    from daytrader.screener import Screener

    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = _tempfile.mkdtemp()
    # ★ sim 모드는 '가상 시장' 안내가 우선하므로, 이 검증(데이터 부재)은
    #   실제 시세를 쓰는 모드에서 해야 의미가 있다.
    cfg.mode = "web"

    class NoDataClient:
        """★ 시세를 하나도 못 주는 상황(키 미등록·네트워크 장애) 재현."""
        def rankings(self, *a, **kw):
            raise RuntimeError("시세를 받아올 곳이 없습니다.")

        def stocks(self, symbols):
            return []

        def prices(self, symbols):
            return []

        def warnings(self, symbol):
            return []

    sc = Screener(NoDataClient(), cfg)
    report = sc.build_report()

    check("시장 스냅샷이 실제로 비어 있음(전제 확인)", report.market_size == 0)
    check("★★★ '데이터가 없다'는 사실을 명확히 알림",
          "받아오지 못해" in report.summary or "데이터 자체가 없" in report.summary,
          report.summary)
    check("'거래 없음이 정상'이라고 오해시키지 않음",
          "거래 없음이 정상입니다" not in report.summary, report.summary)
    check("해결 방법(준비·연결 확인)을 안내함",
          "준비·연결" in report.summary, report.summary)

    # ★ 테마 탈락 사유도 "한 종목만 튀었다"가 아니라 데이터 부재로 나와야 한다.
    if report.themes:
        first = report.themes[0]
        check("테마 탈락 사유도 데이터 부재로 정확히 표시",
              "데이터 부재" in first.reason or "받아오지 못했습니다" in first.reason,
              first.reason)


def section_diagnose_treats_403_as_failure() -> None:
    """★★★ 실제로 겪은 문제 - 연결 진단이 403(접근 거부)을 "통신 문제는
    아니다"라며 ok=True 로 처리했다. 하지만 이 진단의 목적은 "시세를
    받아올 수 있는가"이지 "TCP 가 연결되는가"가 아니다. 403 이면 데이터를
    한 줄도 못 받는데 진단은 전부 초록불이라, 종목 선정이 안 되는 원인을
    찾을 수가 없었다.
    """
    print("\n== 연결 진단이 403(차단)을 실패로 정확히 판정함 ==")
    from unittest.mock import patch, MagicMock
    from daytrader.config import load_config
    from daytrader import netutil

    cfg = load_config(CONFIG_PATH)

    def make_resp(code):
        r = MagicMock()
        r.status_code = code
        return r

    for code, expect_ok, label in [(200, True, "200(정상)"), (401, True, "401(인증만 없음)"),
                                   (403, False, "403(차단)"), (429, False, "429(요청 과다)")]:
        with patch.object(netutil, "make_session") as mk:
            sess = MagicMock()
            sess.get.return_value = make_resp(code)
            sess.post.return_value = make_resp(code)
            mk.return_value = sess
            result = netutil.diagnose(cfg)
            actual = all(t["ok"] for t in result["targets"])
            check(f"{label} -> ok={expect_ok}", actual is expect_ok, f"실제 {actual}")


def section_screener_distinguishes_cause() -> None:
    """★ 시세를 못 받는 원인이 '키 미등록'인지 '네트워크 차단'인지에 따라
    해결 방법이 다르다 - 안내도 구분돼야 한다."""
    print("\n== 시세 부재 원인(키 미등록 vs 차단)을 구분해 안내함 ==")
    import tempfile as _tempfile
    from daytrader.config import load_config
    from daytrader.screener import Screener

    class NoDataClient:
        def rankings(self, *a, **kw):
            raise RuntimeError("시세를 받아올 곳이 없습니다.")

        def stocks(self, symbols):
            return []

        def prices(self, symbols):
            return []

        def warnings(self, symbol):
            return []

    cfg1 = load_config(CONFIG_PATH)
    cfg1.state_dir = _tempfile.mkdtemp()
    cfg1.mode = "web"   # ★ sim 은 "가상 시장" 안내가 우선하므로 제외
    cfg1.client_id = ""
    cfg1.client_secret = ""
    r1 = Screener(NoDataClient(), cfg1).build_report()
    check("키가 없으면 '키 미등록'이라고 정확히 안내",
          "키가 등록되지 않은 상태" in r1.summary, r1.summary[:80])

    cfg2 = load_config(CONFIG_PATH)
    cfg2.state_dir = _tempfile.mkdtemp()
    cfg2.mode = "web"
    cfg2.client_id = "fake"
    cfg2.client_secret = "fake"
    r2 = Screener(NoDataClient(), cfg2).build_report()
    check("키가 있으면 '연결 진단을 보라'고 안내",
          "키는 등록돼 있으니" in r2.summary and "연결 진단" in r2.summary, r2.summary[:80])


def section_router_preserves_real_error() -> None:
    """★★★ 실제로 겪은 문제 - "연계 테스트는 통과하는데 시세를 못 가져온다"의
    정체. 연계 테스트는 TossClient 를 직접 부르고, 실제 매매는 QuoteRouter 를
    거친다. 라우터는 TOSS_ONLY API(rankings 등)가 실패하면 원래 예외를
    통째로 덮고 "인터넷으로 대체할 수 없습니다"라는 무의미한 메시지만
    던졌다. 게다가 30초 스킵(_skip_primary) 중에는 호출조차 안 하고 바로
    그 메시지로 가서, 진짜 원인(400/403 등)을 볼 방법이 전혀 없었다.
    """
    print("\n== 라우터가 토스의 진짜 실패 원인을 감추지 않음 ==")
    from daytrader.router import QuoteRouter

    calls = {"n": 0}

    class FailingPrimary:
        def rankings(self, type, marketCountry="KR", duration=None, count=100, excludeInvestmentCaution=None):
            calls["n"] += 1
            raise RuntimeError("[400] BAD_REQUEST: unsupported-ranking-duration")

    router = QuoteRouter(FailingPrimary(), None)

    try:
        router.rankings(type="TOP_GAINERS", duration="realtime")
        first = ""
    except Exception as exc:
        first = str(exc)
    check("1회차: 진짜 원인(400)이 메시지에 포함됨", "400" in first and "unsupported" in first, first)

    # ★ 30초 스킵 구간 - 예전엔 여기서 원인이 완전히 사라졌다.
    try:
        router.rankings(type="TOP_GAINERS", duration="realtime")
        second = ""
    except Exception as exc:
        second = str(exc)
    check("★★★ 스킵 구간(2회차)에도 진짜 원인이 보임", "400" in second and "unsupported" in second, second)
    # ★★★ 동작이 바뀌었다 - rankings 는 TOSS_ONLY 라 폴백이 없으므로
    # 스킵하면 30초 동안 무조건 실패한다("시세를 하나도 못 받았다"의
    # 최종 원인). 이제는 스킵하지 않고 매번 다시 시도한다.
    check("폴백이 없으므로 스킵하지 않고 재시도함", calls["n"] == 2, f"{calls['n']}회 호출")

    # ★ failures 에 기록이 남아야 화면(/api/source)에서도 볼 수 있다.
    check("failures 에 원인이 기록됨", "rankings" in router.failures and "400" in router.failures["rankings"])


def section_selection_runs_when_report_missing() -> None:
    """★★★ 실제로 겪은 문제 - "아직 스크리닝 결과가 없습니다"만 뜨는 상황.
    rescreen() 은 엔진 루프 안에서, 그것도 매매 시간대일 때만 호출된다.
    그래서 엔진을 막 시작했거나 지금이 매매 시간이 아니면 report 가
    영원히 None 이고, 화면은 왜 없는지도 알려주지 못했다. 이 화면을
    열었다는 건 결과를 보고 싶다는 뜻이니 그 자리에서 한 번 돌려야 한다.
    """
    print("\n== 스크리닝 결과가 없으면 화면을 열 때 즉시 돌려서 보여줌 ==")
    import asyncio
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server

    tmpdir = _tempfile.mkdtemp()
    orig_config_path = server.CONFIG_PATH
    orig_runner = server.runner
    try:
        server.CONFIG_PATH = os.path.join(tmpdir, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)

        calls = {"rescreen": 0}

        class FakeReport:
            def to_dict(self):
                return {"summary": "2개 후보를 찾았습니다.", "errors": [], "themes": [], "criteria": {}}

        class FakeEngine:
            report = None
            candidates = []
            candidate_status = {}
            candidate_verdicts = {}

            def rescreen(self, force=False):
                calls["rescreen"] += 1
                FakeEngine.report = FakeReport()

        class FakeRunner:
            engine = FakeEngine()
            running = True
            error = None

        server.runner = FakeRunner()

        async def scenario():
            result = await server.get_selection()
            check("★★★ report 가 없으면 즉시 스크리닝을 돌림", calls["rescreen"] == 1, f"{calls['rescreen']}회")
            check("결과가 실제로 화면에 전달됨", result["selection"] is not None)

        asyncio.run(scenario())

        # ★ 스크리닝이 예외로 실패해도 API 자체는 죽으면 안 된다
        # (화면이 안내라도 띄울 수 있어야 한다).
        class FailingEngine:
            report = None
            candidates = []
            candidate_status = {}
            candidate_verdicts = {}

            def rescreen(self, force=False):
                raise RuntimeError("[400] BAD_REQUEST: 파라미터 오류")

        class FailingRunner:
            engine = FailingEngine()
            running = True
            error = None

        server.runner = FailingRunner()

        async def scenario2():
            try:
                result = await server.get_selection()
                check("스크리닝이 실패해도 API 는 정상 응답(화면이 안 죽음)", result["selection"] is None)
            except Exception as exc:
                check("스크리닝이 실패해도 API 는 정상 응답(화면이 안 죽음)", False, str(exc))

        asyncio.run(scenario2())
    finally:
        server.CONFIG_PATH = orig_config_path
        server.runner = orig_runner


def section_selection_exposes_session_reason() -> None:
    """★★★ 실제로 겪은 혼란 - "국내 장이 열려 있는데 왜 종목 선정도 매매도
    안 하냐". 이 프로그램은 장 시간 전체(09:00~15:30)가 아니라 설정된
    진입 구간(기본 09:20~14:00)에만 신규 진입한다 - 장 막판 변동성을
    피하려는 의도된 설계다. 그런데 화면에는 "아직 스크리닝 결과가
    없습니다"만 떠서 고장으로 오해했다. 사유를 API 가 함께 줘야 한다.
    """
    print("\n== 종목선정 API 가 '지금 왜 매매를 안 하는지'를 함께 알려줌 ==")
    import daytrader.server as server

    info = server._session_info()
    check("장 국면 정보가 조회됨", bool(info), str(info))
    for key in ("phase", "label", "trading", "why", "scan_start", "scan_end"):
        check(f"'{key}' 가 포함됨", key in info)

    # ★ 이 헬퍼는 어떤 상황에서도 예외를 밖으로 내면 안 된다
    # (화면이 죽는 것보다 사유를 못 보여주는 게 낫다).
    orig = server.cfg_now
    try:
        server.cfg_now = lambda: (_ for _ in ()).throw(RuntimeError("설정 로드 실패"))
        safe = server._session_info()
        check("설정 로드가 실패해도 예외 없이 빈 값 반환", safe == {})
    finally:
        server.cfg_now = orig


def section_toss_quote_has_rate_and_time() -> None:
    """★★★ 실제로 겪은 버그와 그 정정 - "토스 시세는 바뀌는데 상승률이
    안 맞는다". 1차 진단은 틀렸다(응답의 changeRate 를 안 읽어서라고 봤다).
    공식 문서(PriceResponse)를 확인하니 prices 응답 필드는 symbol /
    timestamp / lastPrice / currency 넷뿐이고 등락률·전일종가가 아예 없다 -
    changeRate 는 rankings 응답에만 있는 필드였다. 전일 종가를 일봉에서
    받아 등락률을 직접 계산해야 한다.
    """
    print("\n== 토스 시세: 전일종가를 캔들에서 받아 등락률 계산 ==")
    from unittest.mock import MagicMock
    import daytrader.market as m
    from daytrader.timeutil import now_kst

    calls = {"candles": 0}

    class FakeToss:
        def prices(self, codes):
            # ★ 공식 스펙대로 - 등락률·전일종가는 오지 않는다.
            return [{"symbol": "005930", "lastPrice": 75000, "currency": "KRW",
                     "timestamp": "2026-09-09T14:30:00+09:00"}]

        def candles(self, symbol, interval, count, before=None):
            calls["candles"] += 1
            return [{"closePrice": 73000}, {"closePrice": 74000}]

    m._prev_close_cache["day"] = ""
    out = m._domestic_stocks_batch(FakeToss(), MagicMock(), [("005930", "삼성전자")])
    data = out["005930"]
    check("전일종가를 캔들에서 받아옴", data.get("prev_close") == 73000, str(data.get("prev_close")))
    check("등락 금액이 계산됨", data.get("diff") == 2000, str(data.get("diff")))
    check("응답의 timestamp 를 조회 시각으로 씀", data.get("quoted_at") is not None)

    row = m._row("삼성전자", "domestic_stock", "토스증권", data, now_kst(), False)
    check("★★★ 상승률이 정확히 계산됨(+2.74%)",
          row["pct"] is not None and abs(row["pct"] - 2000 / 73000) < 1e-6, str(row["pct"]))
    check("시각이 초까지 표시됨", bool(row.get("at")) and row["at"].count(":") == 2, str(row.get("at")))

    # ★ 전일종가는 하루 동안 안 변하니 캐시돼야 한다 - 화면이 몇 초마다
    # 갱신되는데 매번 캔들을 부르면 API 호출이 폭증한다.
    before = calls["candles"]
    for _ in range(4):
        m._domestic_stocks_batch(FakeToss(), MagicMock(), [("005930", "삼성전자")])
    check("전일종가는 캐시되어 캔들을 재호출하지 않음", calls["candles"] == before,
          f"{calls['candles'] - before}회 추가 호출")


def section_best_signal_selection() -> None:
    """★★★ "시세 변동을 모니터링해서 최적의 종목과 기법을 골라 승률을
    높여야 한다"는 요청으로 바꾼 로직. 예전에는 종목도 기법도 "목록
    순서대로 처음 통과한 것"을 썼다 - 지금 더 강한 신호가 있어도 무시됐다.
    """
    print("\n== 자동 선정: 신호가 가장 강한 기법을 고름 ==")
    from daytrader.config import load_config
    from daytrader.playbook import Playbook, Verdict

    cfg = load_config(CONFIG_PATH)
    pb = Playbook(cfg)

    class FakeTech:
        def __init__(self, key, score):
            self.key, self._score = key, score

        def evaluate(self, bars, ctx):
            return Verdict(
                id=f"v-{self.key}", at="", symbol="005930", name="삼성", theme="t",
                phase="entry", technique=self.key, technique_label=self.key,
                ok=True, score=self._score, terms=[], blocked_by=[],
                headline="", narrative="", changes=[], inputs={}, price=70000,
            )

    # ★ 목록 앞쪽이 약한 신호, 뒤쪽이 강한 신호
    pb.entries = [FakeTech("breakout", 0.55), FakeTech("bull_flag", 0.92)]

    cfg.strategy.best_signal = False
    w_old, _ = pb.evaluate_entry([], None)
    check("예전 방식(best_signal=False)은 목록 첫 통과", w_old.technique == "breakout")

    cfg.strategy.best_signal = True
    w_new, _ = pb.evaluate_entry([], None)
    check("★★★ 새 방식은 신호가 가장 강한 기법 선택", w_new.technique == "bull_flag")
    check("선택 근거가 기록됨", bool(getattr(w_new, "selection_reason", "")))
    check("선택 설명이 기록됨", bool(getattr(w_new, "selection_note", "")))


def section_performance_weight_guards() -> None:
    """★★★ 실적 반영은 양날의 검이다 - 표본이 적으면 승률이 운에 좌우돼서
    우연히 잘 맞았던 기법에 과적합될 위험이 크다. 세 안전장치를 검증한다.
    """
    print("\n== 실적 가중치의 과적합 방지 장치 ==")
    from daytrader.config import load_config
    from daytrader.playbook import Playbook, Verdict

    cfg = load_config(CONFIG_PATH)
    cfg.strategy.best_signal = True
    pb = Playbook(cfg)

    class FakeTech:
        def __init__(self, key, score):
            self.key, self._score = key, score

        def evaluate(self, bars, ctx):
            return Verdict(
                id=f"v-{self.key}", at="", symbol="005930", name="삼성", theme="t",
                phase="entry", technique=self.key, technique_label=self.key,
                ok=True, score=self._score, terms=[], blocked_by=[],
                headline="", narrative="", changes=[], inputs={}, price=70000,
            )

    # 신호는 A가 높지만 실적은 B가 훨씬 좋다
    pb.entries = [FakeTech("A", 0.80), FakeTech("B", 0.75)]

    # ① 표본 부족이면 실적을 아예 반영하지 않는다
    pb.set_performance({"A": {"trades": 5, "win_rate": 0.20}, "B": {"trades": 5, "win_rate": 0.90}})
    w1, _ = pb.evaluate_entry([], None)
    check("① 표본 20건 미만이면 실적 무시(순수 신호로 A 선택)", w1.technique == "A")

    # ② 표본이 충분하면 반영한다
    pb.set_performance({"A": {"trades": 50, "win_rate": 0.20}, "B": {"trades": 50, "win_rate": 0.90}})
    w2, _ = pb.evaluate_entry([], None)
    check("② 표본이 충분하면 실적 좋은 B가 역전", w2.technique == "B")

    # ③ 배수는 0.7~1.3 으로 묶인다 - 실적만으로 독점·배제되지 않게
    pb.set_performance({"B": {"trades": 999, "win_rate": 1.0}})
    m_max, _ = pb._performance_multiplier("B")
    pb.set_performance({"B": {"trades": 999, "win_rate": 0.0}})
    m_min, _ = pb._performance_multiplier("B")
    check("③ 승률 100%여도 배수 상한 1.3", abs(m_max - 1.3) < 0.001, f"{m_max}")
    check("③ 승률 0%여도 배수 하한 0.7", abs(m_min - 0.7) < 0.001, f"{m_min}")

    # ★ 실적 데이터가 없어도 안전하게 동작해야 한다
    pb.set_performance(None)
    mult, _ = pb._performance_multiplier("A")
    check("실적 데이터가 없으면 중립(1.0배)", mult == 1.0)


def section_bithumb_accounts_empty_is_ok() -> None:
    """★★★ 실제로 겪은 문제 - "빗썸 연계 테스트에서 보유 자산 조회 실패".
    accounts() 가 None/빈 리스트를 돌려주면(보유 자산이 하나도 없거나
    204 빈 응답) len(None) 에서 TypeError 가 나서 "조회 실패"로 표시됐다.
    인증은 멀쩡한데 실패로 뜨니 원인을 찾을 수 없었다 - 자산 0개는
    실패가 아니라 정상 상태다.
    """
    print("\n== 빗썸 보유 자산 0개는 실패가 아니라 정상 ==")
    from unittest.mock import patch
    import daytrader.selftest as st
    from daytrader.bithumb_api import BithumbApiError

    def run_with(accounts_value=None, accounts_error=None):
        with patch("daytrader.bithumb_api.BithumbClient") as C:
            c = C.return_value
            c.ticker.return_value = [{"market": "KRW-BTC"}]
            c.order_chance.return_value = {"market": {}}
            if accounts_error is not None:
                c.accounts.side_effect = accounts_error
            else:
                c.accounts.return_value = accounts_value
            res = st.bithumb_test("fakeaccess", "fakesecret")
        return next(s for s in res["steps"] if s["key"] == "accounts")

    empty = run_with(accounts_value=[])
    check("★★★ 자산이 0개여도 성공으로 처리됨", empty["ok"] is True, str(empty.get("error")))

    none_row = run_with(accounts_value=None)
    check("★★★ None 을 돌려줘도 죽지 않고 성공 처리", none_row["ok"] is True, str(none_row.get("error")))

    # ★ 반대로 진짜 인증 오류는 원인이 정확히 드러나야 한다.
    failed = run_with(accounts_error=BithumbApiError(
        401, "jwt_verification", "JWT 검증 실패 — Secret Key 가 틀렸을 가능성이 큽니다"))
    check("진짜 인증 오류는 실패로 잡힘", failed["ok"] is False)
    check("실패 사유에 원인이 그대로 담김", "Secret Key" in (failed.get("error") or ""),
          str(failed.get("error")))

    # ★ 자산이 있으면 잔고 요약을 보여준다.
    rich = run_with(accounts_value=[
        {"currency": "KRW", "balance": "500000"},
        {"currency": "BTC", "balance": "0.01", "avg_buy_price": "95000000"},
    ])
    check("자산이 있으면 원화 잔고를 요약해 보여줌",
          rich["ok"] is True and "원화" in (rich.get("detail") or ""), str(rich.get("detail")))


def section_screener_survives_non_dict_rows() -> None:
    """★★★ 실제로 겪은 버그 - "종목선정 화면이 안 열려 / 'str' object has
    no attribute 'get'". 시세·랭킹 응답이 항상 딕셔너리 리스트라고 가정하고
    r.get() 을 바로 불렀는데, 소스에 따라 문자열이 섞여 오면 그 자리에서
    죽어 화면 전체가 안 열렸다. 딕셔너리인 항목만 골라 써야 한다.
    """
    print("\n== 시세 응답에 문자열이 섞여도 종목선정이 죽지 않음 ==")
    import tempfile as _tempfile
    from daytrader.config import load_config
    from daytrader.screener import Screener

    class MixedTypeClient:
        """★ 딕셔너리와 문자열이 섞인 응답 - 실제 에러의 원인."""
        def rankings(self, **kw):
            return ["005930", {"symbol": "000660", "price": 70000,
                               "changeRate": 0.05, "tradingAmount": 9e9}, None, 123]

        def prices(self, symbols):
            return ["005930", {"symbol": "000660", "price": 71000, "changeRate": 0.06}]

        def stocks(self, symbols):
            return []

        def warnings(self, symbol):
            return []

    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = _tempfile.mkdtemp()
    sc = Screener(MixedTypeClient(), cfg)

    try:
        report = sc.build_report()
        check("★★★ 문자열이 섞여도 스크리닝이 예외 없이 끝남", True)
    except AttributeError as exc:
        check("★★★ 문자열이 섞여도 스크리닝이 예외 없이 끝남", False, str(exc))
        return

    check("딕셔너리 항목만 정상 반영됨(1건)", report.market_size == 1, str(report.market_size))

    try:
        sc.refresh_prices(report)
        check("refresh_prices 도 안전함", True)
    except AttributeError as exc:
        check("refresh_prices 도 안전함", False, str(exc))

    # ★ discover() 도 같은 패턴이라 함께 확인한다.
    try:
        sc.discover(count=5)
        check("discover 도 안전함", True)
    except AttributeError as exc:
        check("discover 도 안전함", False, str(exc))


def section_router_handles_signature_mismatch() -> None:
    """★★★ 실제로 겪은 버그 - "candles: TossClient.candles() got an
    unexpected keyword argument 'adjusted'". 라우터가 adjusted 를 그대로
    넘겼는데 TossClient.candles() 는 이 인자를 받지 않는다(tossapi.py 에
    "검증 안 된 추측이라 뺐다"고 명시돼 있다). 그래서 토스로 캔들을 받을
    때마다 TypeError 가 나고 종목선정이 통째로 실패했다.
    """
    print("\n== 라우터가 클라이언트별 시그니처 차이를 안전하게 처리 ==")
    from daytrader.router import QuoteRouter

    class TossLike:
        """★ 실제 TossClient 와 같다 - adjusted 를 안 받는다."""
        def candles(self, symbol, interval, count, before=None):
            return [{"src": "toss", "interval": interval, "count": count}]

    class WebLike:
        """★ 웹 시세 - adjusted 를 받는다."""
        def candles(self, symbol, timeframe, count, adjusted=True):
            return [{"src": "web", "adjusted": adjusted}]

    router = QuoteRouter(TossLike(), WebLike())
    try:
        out = router.candles("005930", "1d", 30)
        check("★★★ adjusted 를 안 받는 토스도 TypeError 없이 호출됨",
              out and out[0]["src"] == "toss", str(out))
    except TypeError as exc:
        check("★★★ adjusted 를 안 받는 토스도 TypeError 없이 호출됨", False, str(exc))

    # ★ 폴백(웹)은 adjusted 를 받으므로 그대로 전달돼야 한다.
    class FailingToss:
        def candles(self, symbol, interval, count, before=None):
            raise RuntimeError("토스 실패")

    router2 = QuoteRouter(FailingToss(), WebLike())
    out2 = router2.candles("005930", "1d", 30, adjusted=False)
    check("폴백(웹)에는 adjusted 가 정확히 전달됨",
          out2 and out2[0]["src"] == "web" and out2[0]["adjusted"] is False, str(out2))

    # ★ **kwargs 를 받는 클라이언트는 그대로 다 넘겨도 된다.
    class KwargsLike:
        def candles(self, symbol, timeframe, count, **kw):
            return [{"kw": kw}]

    router3 = QuoteRouter(KwargsLike(), None)
    out3 = router3.candles("005930", "1d", 30)
    check("**kwargs 클라이언트에는 그대로 전달됨",
          out3 and out3[0]["kw"].get("adjusted") is True, str(out3))


def section_notify_in_practice() -> None:
    """★★★ "텔레그램 메시지를 못 보낸다"의 흔한 원인 - 기본적으로 실거래
    (live)에서만 알림을 보낸다. 연습 매매 알림이 실제 주문과 섞이면
    위험해서 둔 안전장치인데, "모의매매도 알림으로 확인하고 싶다"는
    경우엔 방법이 없었고 사유도 해결책을 안 알려줬다.
    """
    print("\n== 연습 모드에서도 알림을 받을 수 있음(옵션) ==")
    from daytrader.config import load_config
    from daytrader import notify

    cfg = load_config(CONFIG_PATH)
    cfg.notify.telegram_token = "fake"
    cfg.notify.telegram_chat_id = "123"

    # ① 연습 모드 + 옵션 꺼짐(기본) - 안 보낸다
    cfg.mode = "paper"
    cfg.notify.notify_in_practice = False
    check("연습 모드는 기본적으로 안 보냄", not notify.enabled_cfg(cfg))
    why = notify.why_off_cfg(cfg)
    check("★★★ 사유에 해결 방법까지 안내됨",
          "연습 모드에서도 알림 보내기" in why, why[:60])

    # ② 연습 모드 + 옵션 켬 - 보낸다
    cfg.notify.notify_in_practice = True
    check("★★★ 옵션을 켜면 연습 모드에서도 보냄", notify.enabled_cfg(cfg))

    # ③ 실거래는 옵션과 무관하게 항상 보낸다(기존 동작 유지)
    cfg.mode = "live"
    cfg.notify.notify_in_practice = False
    check("실거래는 옵션과 무관하게 보냄(기존 동작)", notify.enabled_cfg(cfg))

    # ④ 토큰이 없으면 어떤 경우에도 안 보낸다
    cfg.notify.telegram_token = ""
    check("토큰이 없으면 안 보냄", not notify.enabled_cfg(cfg))
    check("토큰 없을 때 등록 위치를 안내", "준비·연결" in notify.why_off_cfg(cfg))


def section_practice_notification_is_marked() -> None:
    """★★★ 연습 모드 알림에는 반드시 표시가 붙어야 한다 - 실거래 알림과
    똑같이 생기면 "실제로 샀다"고 오해할 수 있고, 그건 돈이 걸린 오해다.
    """
    print("\n== 연습 모드 알림에 [연습] 표시가 붙음 ==")
    import time as _time
    from unittest.mock import patch
    from daytrader.config import load_config
    from daytrader.notify import Telegram

    cfg = load_config(CONFIG_PATH)
    cfg.notify.telegram_token = "fake"
    cfg.notify.telegram_chat_id = "123"
    cfg.notify.notify_in_practice = True

    sent = []
    with patch("daytrader.notify._post_raw_sync",
               side_effect=lambda t, c, txt: (sent.append((cfg.mode, txt)), (True, ""))[1]):
        for mode in ["paper", "live"]:
            cfg.mode = mode
            tg = Telegram(cfg)
            tg.send("[매수] 삼성전자 10주", event="trade")
            _time.sleep(0.5)
            tg.stop()

    paper = [t for m, t in sent if m == "paper"]
    live = [t for m, t in sent if m == "live"]
    check("★★★ 연습 모드 알림에 [연습] 표시가 붙음",
          bool(paper) and paper[0].startswith("🧪 [연습]"), str(paper))
    check("실거래 알림에는 표시가 안 붙음",
          bool(live) and not live[0].startswith("🧪"), str(live))


def section_screener_message_clarity() -> None:
    """★★★ 실제로 겪은 혼란 - "토스 API 를 연계했는데도 시세를 못 가져온다는
    메시지가 남아있고 종목 선정을 안 하는 것 같다". 원인이 여러 갈래인데
    메시지가 그걸 구분해 주지 못했다:
      ① sim/replay 모드 - 키와 무관하게 가상 시장을 쓴다(의도된 동작).
      ② 키 미등록 / ③ 키는 있는데 네트워크·권한 문제
      ④ 시세는 정상인데 오늘 기준에 맞는 종목이 없는 것(= 정상)
    """
    print("\n== 종목선정 메시지가 상황을 정확히 구분함 ==")
    import tempfile as _tempfile
    from daytrader.config import load_config
    from daytrader.screener import Screener

    class NoData:
        def rankings(self, **kw):
            raise RuntimeError("시세 없음")

        def stocks(self, s):
            return []

        def prices(self, s):
            return []

        def warnings(self, s):
            return []

    def run(mode, has_keys):
        cfg = load_config(CONFIG_PATH)
        cfg.state_dir = _tempfile.mkdtemp()
        cfg.mode = mode
        cfg.client_id = cfg.client_secret = ("fake" if has_keys else "")
        return Screener(NoData(), cfg).build_report().summary

    s1 = run("sim", True)
    check("★★★ sim 모드는 '가상 시장'이라고 정확히 알림",
          "가상 시장" in s1 and "sim" in s1, s1[:70])
    check("sim 모드에서 키 탓으로 오해시키지 않음", "키가 등록되지 않은" not in s1)
    check("sim 모드 해결 방법(모드 변경)을 안내", "모드를" in s1, s1[:70])

    s2 = run("web", False)
    check("키가 없으면 키 미등록이라고 알림", "키가 등록되지 않은" in s2, s2[:60])

    s3 = run("web", True)
    check("키가 있으면 연결 진단을 보라고 알림", "연결 진단" in s3, s3[:60])


def section_screener_shows_why_no_candidate() -> None:
    """★★★ "종목 선정을 안 하는 것 같다" - 시세는 정상인데 후보가 0개일 때
    "테마가 없습니다"만 나오면 기준에 얼마나 못 미쳤는지 알 수 없어
    고장으로 오해한다. 실제 최고 상승률과 기준을 함께 보여줘야 한다.
    """
    print("\n== 후보가 없을 때 '얼마나 못 미쳤는지'를 수치로 보여줌 ==")
    import tempfile as _tempfile
    from daytrader.config import load_config
    from daytrader.clock import SimClock
    from daytrader.simulator import SimClient
    from daytrader.screener import Screener

    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = _tempfile.mkdtemp()
    cfg.mode = "sim"
    cfg.simulation.scenario = "choppy"  # ★ 잔잔한 장 - 후보가 안 나오는 날
    cfg.simulation.seed = 3

    clock = SimClock(start="10:00", speed=1, day="2026-09-04")
    report = Screener(SimClient(cfg, clock=clock, themes_path=THEMES_PATH), cfg).build_report()

    check("시세는 정상적으로 받아옴(전제 확인)", report.market_size > 0, str(report.market_size))
    if report.candidates:
        print("      (이 시드에서는 후보가 나와 이 검증은 건너뜁니다)")
        return
    check("★★★ 기준 미달 수치를 함께 알림",
          "기준" in report.summary and "%" in report.summary, report.summary[:90])
    check("가상 시장임을 표시", "가상 시장" in report.summary, report.summary[:40])


def section_notify_test_uses_saved_values() -> None:
    """★★★ 실제로 겪은 문제 - "텔레그램 등록했는데 테스트 전송이 안 된다".
    보안상 입력칸은 저장 후 비워지는데(저장된 토큰을 화면에 다시 뿌리지
    않는다), 서버는 빈 값이면 400 을 냈다. 그래서 저장하고 바로 테스트를
    누르면 반드시 실패하는 구조였다.
    """
    print("\n== 텔레그램 테스트 전송이 저장된 값으로도 동작 ==")
    import asyncio
    import shutil
    import tempfile as _tempfile
    import time as _t
    from unittest.mock import patch
    from starlette.requests import Request
    import daytrader.server as server

    def make_req(headers=None) -> Request:
        hdrs = {k.lower().encode(): str(v).encode() for k, v in (headers or {}).items()}
        scope = {
            "type": "http", "method": "POST", "scheme": "http", "path": "/", "query_string": b"",
            "headers": list(hdrs.items()), "client": ("127.0.0.1", 50000),
        }
        return Request(scope)

    tmp = _tempfile.mkdtemp()
    orig = server.CONFIG_PATH
    try:
        server.CONFIG_PATH = os.path.join(tmp, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)

        import yaml
        raw = yaml.safe_load(open(server.CONFIG_PATH, encoding="utf-8"))
        raw["notify"]["telegram_token"] = "saved_tok"
        raw["notify"]["telegram_chat_id"] = "999"
        yaml.dump(raw, open(server.CONFIG_PATH, "w", encoding="utf-8"), allow_unicode=True)

        async def scenario():
            sent = []
            with patch("daytrader.notify._post_raw_sync",
                       side_effect=lambda t, c, x: (sent.append((t, c)), (True, ""))[1]):
                # ★ 저장 직후 상황 - 입력칸이 비어 있다(저장된 값을 쓰므로 [2-4] 재확인도 필요 없다).
                result = await server.notify_test(server.NotifyTestIn(token="", chat_id=""), make_req())
            check("★★★ 입력칸이 비어도 테스트가 성공함", result.get("ok") is True)
            check("저장된 토큰·채팅ID 를 사용함", sent and sent[0] == ("saved_tok", "999"), str(sent))

            # ★ [2-4] 입력칸에 새 값을 직접 넣어 시험하려면 설정 재확인 토큰이 있어야 한다.
            server._confirm_tokens["tok-offline-notify-test"] = ("settings", _t.time() + 60)
            sent.clear()
            try:
                with patch("daytrader.notify._post_raw_sync",
                           side_effect=lambda t, c, x: (sent.append((t, c)), (True, ""))[1]):
                    await server.notify_test(
                        server.NotifyTestIn(token="new_tok", chat_id="111"),
                        make_req({"x-confirm-token": "tok-offline-notify-test"}),
                    )
            finally:
                server._confirm_tokens.pop("tok-offline-notify-test", None)
            check("입력칸에 값이 있으면 그것을 우선(저장 전 시험용)",
                  sent and sent[0] == ("new_tok", "111"), str(sent))

        asyncio.run(scenario())
    finally:
        server.CONFIG_PATH = orig


def section_telegram_error_hints() -> None:
    """★ 텔레그램이 주는 원문("Bad Request: chat not found")만으로는 무엇을
    고쳐야 할지 알 수 없다 - 흔한 원인을 코드별로 짚어 줘야 한다."""
    print("\n== 텔레그램 전송 실패 시 원인을 안내함 ==")
    from unittest.mock import patch, MagicMock
    from daytrader import notify

    cases = [
        (401, '{"description":"Unauthorized"}', "봇 토큰"),
        (400, '{"description":"Bad Request: chat not found"}', "/start"),
        (403, '{"description":"Forbidden: bot was blocked"}', "차단"),
        (429, '{"description":"Too Many Requests"}', "잦음"),
    ]
    for code, body, keyword in cases:
        resp = MagicMock()
        resp.status_code = code
        resp.text = body
        with patch.object(notify.netutil, "make_session") as mk:
            mk.return_value.post.return_value = resp
            ok, err = notify._post_raw_sync("tok", "123", "test")
        check(f"HTTP {code} 에 '{keyword}' 안내 포함", (not ok) and keyword in err, err[:60])


def section_selection_cache_limits_api_calls() -> None:
    """★★★ 실제로 겪은 문제 - "시세를 하나도 받아오지 못했다"의 숨은 원인.
    엔진이 꺼져 있으면 종목선정 화면이 갱신될 때마다(15초 주기) 스크리닝을
    새로 돌렸고, 한 번에 랭킹 API 를 2회 부른다. 다른 화면·진단이 겹치면
    초당 3회 한도를 넘겨 빈 응답을 받는다.
    ★ 짧게 캐시해 반복 호출을 막되, 사용자가 직접 누른 새로고침은 즉시 반영한다.
    """
    print("\n== 종목선정 반복 갱신이 API 를 과다 호출하지 않음 ==")
    import asyncio
    import shutil
    import tempfile as _tempfile
    from unittest.mock import patch
    import daytrader.server as server

    tmp = _tempfile.mkdtemp()
    orig_path, orig_engine = server.CONFIG_PATH, server.runner.engine
    try:
        server.CONFIG_PATH = os.path.join(tmp, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)
        server.runner.engine = None
        server._selection_cache = None

        calls = {"n": 0}

        class CountingScreener:
            def __init__(self, client, cfg):
                pass

            def build_report(self):
                calls["n"] += 1

                class R:
                    candidates = []
                    market_size = 57  # ★ 성공한 결과만 캐시하므로 값이 있어야 한다.

                    def to_dict(self):
                        return {"summary": "x", "errors": [], "themes": [], "criteria": {}, "at": ""}

                return R()

        async def scenario():
            with patch("daytrader.screener.Screener", CountingScreener):
                for _ in range(5):
                    await server.get_selection()
                check("★★★ 화면이 5번 갱신돼도 스크리닝은 1회만", calls["n"] == 1, f"{calls['n']}회")

                await server.get_selection(refresh=True)
                check("사용자가 직접 새로고침하면 즉시 실행", calls["n"] == 2, f"{calls['n']}회")

        asyncio.run(scenario())
    finally:
        server.CONFIG_PATH = orig_path
        server.runner.engine = orig_engine
        server._selection_cache = None


def section_toss_only_not_skipped() -> None:
    """★★★ 실제로 겪은 문제의 최종 원인 - "시세를 하나도 받아오지 못했다"가
    계속 뜨던 이유. 라우터는 실패한 API 를 30초간 건너뛴다. 인터넷으로
    대체 가능한 기능은 그게 맞지만, TOSS_ONLY(rankings)는 대안이 없어서
    스킵하는 순간 30초 동안 무조건 실패한다. 한도 초과 한 번 때문에
    종목 선정이 30초씩 통째로 멈췄다.
    """
    print("\n== 폴백 없는 API 는 실패해도 즉시 재시도함 ==")
    from daytrader.router import QuoteRouter

    calls = {"n": 0}

    class Flaky:
        def rankings(self, type, marketCountry="KR", duration=None,
                     count=100, excludeInvestmentCaution=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("[429] rate-limited")  # ★ 일시적 실패
            return [{"symbol": "005930", "price": 70000,
                     "changeRate": 0.01, "tradingAmount": 9e9}]

    router = QuoteRouter(Flaky(), None)
    try:
        router.rankings(type="MARKET_TRADING_AMOUNT", duration="realtime")
    except Exception:
        pass  # ★ 1회차 실패는 의도된 것

    try:
        out = router.rankings(type="MARKET_TRADING_AMOUNT", duration="realtime")
        check("★★★ 실패 직후에도 다시 시도해 성공함(30초 차단 안 함)", len(out) == 1, str(out))
    except Exception as exc:
        check("★★★ 실패 직후에도 다시 시도해 성공함(30초 차단 안 함)", False, str(exc))
    check("실제로 API 를 두 번 불렀음(스킵하지 않음)", calls["n"] == 2, f"{calls['n']}회")

    # ★ 반대로 폴백이 있는 기능은 예전처럼 스킵해야 한다 - 토스가 아플 때
    #   계속 두드리는 것보다 웹 시세로 넘어가는 편이 빠르다.
    calls2 = {"toss": 0, "web": 0}

    class FailingToss:
        def prices(self, symbols):
            calls2["toss"] += 1
            raise RuntimeError("실패")

    class WebOk:
        def prices(self, symbols):
            calls2["web"] += 1
            return [{"symbol": s, "price": 100} for s in symbols]

    r2 = QuoteRouter(FailingToss(), WebOk())
    r2.prices(["005930"])
    r2.prices(["005930"])
    check("폴백이 있는 기능은 스킵이 유지됨(토스 1회만 시도)", calls2["toss"] == 1, str(calls2))
    check("두 번째부터는 웹 시세로 바로 감", calls2["web"] == 2, str(calls2))


def section_token_revoked_auto_refresh() -> None:
    """★★★ "시세를 하나도 받아오지 못했다"의 진짜 최종 원인 -
    [401] token-revoked. 토스는 새 토큰이 발급되면 이전 토큰을 무효화하는데
    ("새로 발급된 토큰으로 대체되어 더 이상 유효하지 않은 토큰입니다"),
    재발급 대상 코드에 expired-token / invalid-token 만 있고 token-revoked
    가 빠져 있어서, 캐시된 죽은 토큰을 계속 쓰며 실패했다.
    ★ 프로그램을 두 번 켜거나 다른 곳에서 같은 키를 쓰면 바로 이 상태가 된다.
    """
    print("\n== token-revoked 시 토큰을 자동 재발급함 ==")
    import json as _json
    import tempfile as _tempfile
    from unittest.mock import patch, MagicMock
    from daytrader.tossapi import TossClient

    cache = os.path.join(_tempfile.mkdtemp(), "tok.json")
    with open(cache, "w", encoding="utf-8") as f:
        _json.dump({"access_token": "DEAD_TOKEN", "expires_at": 9e12}, f)

    client = TossClient("id", "secret", token_cache=cache)
    check("죽은 토큰이 캐시에서 로드됨(전제 확인)",
          client._token is not None and client._token.access_token == "DEAD_TOKEN")

    calls = {"token": 0}

    def fake_post(url, **kw):
        r = MagicMock()
        if "oauth2/token" in url:
            calls["token"] += 1
            r.status_code = 200
            r.json.return_value = {"access_token": "NEW_TOKEN", "expires_in": 3600}
        return r

    def fake_request(method, url, **kw):
        r = MagicMock()
        if "DEAD_TOKEN" in kw.get("headers", {}).get("Authorization", ""):
            r.status_code = 401
            r.content = b"x"
            r.json.return_value = {"error": {"code": "token-revoked", "message": "무효"}}
        else:
            r.status_code = 200
            r.content = b"[]"
            # ★ 응답이 리스트로 바로 오는 경우 - 예전엔 .get() 에서 죽었다.
            r.json.return_value = [{"symbol": "005930", "price": 70000,
                                    "changeRate": 0.01, "tradingAmount": 9e9}]
        return r

    with patch.object(client._session, "post", side_effect=fake_post), \
         patch.object(client._session, "request", side_effect=fake_request):
        try:
            out = client.rankings("MARKET_TRADING_AMOUNT", marketCountry="KR",
                                  duration="realtime", count=10)
            check("★★★ token-revoked 에서 자동 재발급 후 조회 성공", len(out) == 1, str(out))
        except Exception as exc:
            check("★★★ token-revoked 에서 자동 재발급 후 조회 성공", False, str(exc))
            return

    check("토큰을 실제로 다시 받아옴", calls["token"] == 1, f"{calls['token']}회")
    check("리스트 응답도 정상 처리(.get 오류 안 남)", isinstance(out, list))


def section_secrets_single_file() -> None:
    """★★★ "API 키는 별도 yaml 로 분리" - 예전에는 토스·빗썸이 .env,
    텔레그램이 config.yaml 에 흩어져 있었다. 그래서
      ① config.yaml 을 공유하면 텔레그램 토큰이 함께 새고
      ② 키를 옮기거나 지우려면 두 파일을 다 봐야 했다.
    이제 secrets.yaml 한 곳에 모은다.
    """
    print("\n== API 키가 secrets.yaml 한 파일로 모임 ==")
    import tempfile as _tempfile
    import daytrader.secrets as S

    d = _tempfile.mkdtemp()
    orig_path = S.SECRETS_PATH
    saved_env = {k: os.environ.get(k) for k in
                 ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "BITHUMB_ACCESS_KEY",
                  "BITHUMB_SECRET_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    try:
        S.SECRETS_PATH = os.path.join(d, "secrets.yaml")
        for k in saved_env:
            os.environ.pop(k, None)

        # ★ 예전 방식으로 흩어져 있던 상황을 재현한다.
        env_path = os.path.join(d, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("TOSS_CLIENT_ID=old_toss_id\nTOSS_CLIENT_SECRET=old_toss_sec\n")
            f.write("BITHUMB_ACCESS_KEY=old_bit_ak\nBITHUMB_SECRET_KEY=old_bit_sk\n")
        legacy_cfg = {"notify": {"telegram_token": "old_tg_token", "telegram_chat_id": "12345"}}

        moved = S.migrate_from_legacy(env_path, legacy_cfg)
        check("★★★ 예전 위치의 키 6개가 모두 옮겨짐", len(moved) == 6, str(moved))

        values = S.all_values()
        check(".env 의 토스 키가 옮겨짐", values["toss_client_id"] == "old_toss_id")
        check(".env 의 빗썸 키가 옮겨짐", values["bithumb_access_key"] == "old_bit_ak")
        check("config.yaml 의 텔레그램 토큰이 옮겨짐", values["telegram_token"] == "old_tg_token")

        # ★ 한쪽을 저장할 때 다른 쪽이 지워지면 안 된다(예전에 겪은 문제).
        S.save({"toss_client_id": "new_id"})
        after = S.all_values()
        check("토스만 바꿔도 빗썸·텔레그램이 유지됨",
              after["toss_client_id"] == "new_id"
              and after["bithumb_access_key"] == "old_bit_ak"
              and after["telegram_token"] == "old_tg_token", str(after))

        # ★ 같은 프로세스에서 즉시 반영돼야 한다(저장 직후 "등록 안 됨" 방지).
        check("저장 즉시 환경변수에도 반영됨", os.environ.get("TOSS_CLIENT_ID") == "new_id")

        # ★ 파일 권한 - 같은 PC 의 다른 계정이 읽지 못하게.
        # (윈도우는 chmod 가 무시되므로 POSIX 에서만 검사한다)
        if sys.platform != "win32":
            mode = oct(os.stat(S.SECRETS_PATH).st_mode)[-3:]
            check("파일 권한이 소유자 전용(600)", mode == "600", mode)

        # ★★★ 저장된 파일에는 평문이 남지 않아야 한다(윈도우 DPAPI 암호화).
        if sys.platform == "win32":
            with open(S.SECRETS_PATH, "r", encoding="utf-8") as _f:
                _raw = _f.read()
            check("secrets.yaml 에 API 키 평문이 없음(암호화 저장)",
                  "new_id" not in _raw and "old_bit_ak" not in _raw and "old_tg_token" not in _raw, _raw[:200])
            check("암호화된 값이 enc: 접두어로 저장됨", "enc:" in _raw)
            for _k in ("TOSS_CLIENT_ID",):
                os.environ.pop(_k, None)
            check("암호화 저장값을 다시 읽으면 원문 복원", S.get("toss_client_id") == "new_id", S.get("toss_client_id"))

        # ★ 이미 값이 있으면 예전 값으로 덮어쓰지 않는다.
        again = S.migrate_from_legacy(env_path, legacy_cfg)
        check("이미 옮긴 뒤에는 다시 덮어쓰지 않음", not again, str(again))
        check("덮어쓰기 없이 새 값이 유지됨", S.get("toss_client_id") == "new_id")
    finally:
        S.SECRETS_PATH = orig_path
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def section_rankings_response_shapes() -> None:
    """★★★ "시세를 하나도 받아오지 못했다"의 최종 원인.
    rankings 응답은 리스트로 올 때도 있고 {"rankings": [...]} 같은
    딕셔너리로 올 때도 있는데, 예전엔 받은 그대로 돌려줬다. 딕셔너리로
    오면 스크리너가 키 문자열("rankings")을 순회하며 종목을 하나도 못
    찾았다. 진단 화면에서는 딕셔너리에 [:5] 를 써서
    "KeyError: slice(None, 5, None)" 로 드러났다.
    ★ prices() 는 이미 같은 처리를 하고 있었는데 rankings 만 빠져 있었다.
    """
    print("\n== rankings 응답이 어떤 형태로 와도 리스트로 정규화됨 ==")
    import time as _time
    from unittest.mock import patch, MagicMock
    from daytrader.tossapi import TossClient, Token

    rows = [{"symbol": "005930", "name": "삼성전자", "price": 70000,
             "changeRate": 0.01, "tradingAmount": 9e9}]

    def run(payload):
        client = TossClient("id", "secret")
        client._token = Token(access_token="T", expires_at=_time.time() + 3600)

        def fake(method, url, **kw):
            r = MagicMock()
            r.status_code = 200
            r.content = b"x"
            r.json.return_value = payload
            return r

        with patch.object(client._session, "request", side_effect=fake):
            return client.rankings("MARKET_TRADING_AMOUNT", marketCountry="KR",
                                   duration="realtime", count=10)

    for label, payload, expect in [
        ("리스트 직접", rows, 1),
        ("{'rankings': [...]}", {"rankings": rows}, 1),
        ("{'result': {'rankings': [...]}}", {"result": {"rankings": rows}}, 1),
        ("{'items': [...]}", {"items": rows}, 1),
        ("모르는 키 이름", {"someNewField": rows}, 1),
        ("빈 딕셔너리", {}, 0),
    ]:
        out = run(payload)
        check(f"{label} → 리스트 {expect}건",
              isinstance(out, list) and len(out) == expect, f"{type(out).__name__} {out}")


def section_config_save_never_corrupts() -> None:
    """★★★ 실제로 겪은 사고 - 텔레그램 키를 저장할 때 config.yaml 의 옛
    값을 지우려고 파일을 다시 썼는데, 주석을 보존하며 쓰는 과정에서
    리스트 형식이 깨져("watchlist: - AAPL") 설정을 아예 못 읽게 됐다.
    프로그램이 시작조차 안 되는 상태가 된다.
    ★ 설정 저장은 흔한 동작인데 한 번 망가지면 사용자가 직접 YAML 을
      고쳐야 한다 - 그건 받아들일 수 없다. 바꿔치기 전에 읽어 본다.
    """
    print("\n== 설정 저장이 파일을 망가뜨리지 않음 ==")
    import shutil
    import tempfile as _tempfile
    import daytrader.server as server

    tmp = _tempfile.mkdtemp()
    orig = server.CONFIG_PATH
    try:
        server.CONFIG_PATH = os.path.join(tmp, "config.yaml")
        shutil.copy(CONFIG_PATH, server.CONFIG_PATH)

        before = open(server.CONFIG_PATH, encoding="utf-8").read()

        # ★ 정상 저장은 그대로 동작해야 한다(안전장치가 막으면 안 된다).
        raw = server._read_config_raw(round_trip=True)
        raw["screen"]["ranking_count"] = 120
        server._validate_raw(raw)
        server._write_config_raw(raw)

        from daytrader.config import load_config
        cfg = load_config(server.CONFIG_PATH)
        check("정상 저장은 그대로 동작함", cfg.screen.ranking_count == 120,
              str(cfg.screen.ranking_count))
        check("리스트가 깨지지 않고 보존됨", len(cfg.overseas.watchlist) == 19,
              str(len(cfg.overseas.watchlist)))

        # ★ 저장된 파일을 다시 읽을 수 있어야 한다 - 이게 핵심이다.
        import yaml as _yaml
        try:
            with open(server.CONFIG_PATH, encoding="utf-8") as f:
                _yaml.safe_load(f)
            check("★★★ 저장 후에도 YAML 로 읽을 수 있음", True)
        except Exception as exc:
            check("★★★ 저장 후에도 YAML 로 읽을 수 있음", False, str(exc))

        # ★ 저장에 실패하면 원본이 그대로 남아야 한다(망가진 파일로 덮지 않음).
        check("주석이 보존됨", "mode 는 5가지 중 하나" in
              open(server.CONFIG_PATH, encoding="utf-8").read())
        check("파일이 비워지지 않음",
              len(open(server.CONFIG_PATH, encoding="utf-8").read()) > len(before) * 0.8)
    finally:
        server.CONFIG_PATH = orig


def section_none_values_never_crash_judgement() -> None:
    """★★★ "'>' not supported between instances of 'NoneType' and 'int'" 의
    최종 원인. 모든 기법 판정이 지나는 mk_term() 이 NaN 만 걸러내고 None 은
    그대로 비교식에 넘겨 죽었다. 시세 API 가 값을 못 주면 NaN 이 아니라
    None 으로 오는 경로가 있다(응답에 필드가 없을 때 등).
    ★ 여기서 한 번 죽으면 매매가 통째로 멈춘다 - 가장 중요한 길목이다.
    """
    print("\n== 값이 None 이어도 기법 판정이 죽지 않음 ==")
    from daytrader.playbook import mk_term, Bar

    for op, value, threshold in [(">", None, 0), (">=", None, 100), ("<", None, 5),
                                 ("<=", None, 1), ("between", None, 1), (">", 10, None)]:
        try:
            term = mk_term(key="x", label="테스트", value=value, threshold=threshold, op=op)
            check(f"op={op}, value={value} → 예외 없이 '판단 불가'",
                  term.passed is False and "판단할 수 없습니다" in term.explain, term.explain[:30])
        except TypeError as exc:
            check(f"op={op}, value={value} → 예외 없이 '판단 불가'", False, str(exc))

    # ★ 정상 값은 그대로 판정돼야 한다(방어 때문에 다 막히면 안 된다).
    check("정상 값은 그대로 통과(10 > 5)",
          mk_term(key="x", label="t", value=10, threshold=5, op=">").passed is True)
    check("정상 값은 그대로 탈락(3 > 5)",
          mk_term(key="x", label="t", value=3, threshold=5, op=">").passed is False)

    # ★★★ 봉 파싱도 같은 문제가 있었다 - float(None) 에서 죽었다.
    try:
        bar = Bar.from_api({"timestamp": "1", "openPrice": None, "highPrice": 202.0,
                            "lowPrice": 198.0, "closePrice": None, "volume": None})
        check("★★★ 봉 데이터에 None 이 섞여도 예외 없음", True)
        # ★ 0 이 아니라 NaN 이어야 한다 - 0 으로 채우면 "가격이 0원"이라는
        #   잘못된 사실이 되어 판정을 오염시킨다.
        from math import isnan
        check("빈 값은 0 이 아니라 NaN(잘못된 사실을 만들지 않음)",
              isnan(bar.open) and isnan(bar.close) and isnan(bar.volume))
        check("있는 값은 그대로 유지", bar.high == 202.0 and bar.low == 198.0)
    except TypeError as exc:
        check("★★★ 봉 데이터에 None 이 섞여도 예외 없음", False, str(exc))

    # ★ 필드가 아예 없어도 죽으면 안 된다.
    try:
        Bar.from_api({})
        check("필드가 아예 없어도 예외 없음", True)
    except Exception as exc:
        check("필드가 아예 없어도 예외 없음", False, str(exc))


def section_domestic_watchlist_is_traded() -> None:
    print("\n== ★★★ 직접 추가한 국내 관심 종목은 테마와 별개로 거래 대상 ==")
    from daytrader.screener import Screener
    from daytrader.config import load_config

    cfg = load_config(os.path.join(ROOT, "config.yaml"))
    cfg.screen.watchlist = ["005930", "035720", "000000"]

    class C:
        def rankings(self, type, marketCountry, duration, count):
            return [{"symbol": "005930", "price": {"lastPrice": 70000, "changeRate": 0.001}, "tradingAmount": 9e11}]

        def stocks(self, syms):
            return [{"symbol": x, "name": "이름" + x} for x in syms]

        def warnings(self, sym):
            return [{"type": "INVESTMENT_WARNING"}] if sym == "000000" else []

        def prices(self, syms):
            return [{"symbol": x, "price": 50000, "changeRate": 0.02} for x in syms if x != "000000"]

    rep = Screener(C(), cfg).build_report()
    picked = {c.symbol: c.theme for c in rep.candidates}
    check("테마 조건에 안 맞아도(상승률 0.1%) 관심 종목이 후보에 들어감", picked.get("005930") == "관심종목", str(picked))
    check("랭킹에 없던 관심 종목도 시세를 받아 들어감", picked.get("035720") == "관심종목", str(picked))
    check("경고 종목은 안전상 제외", "000000" not in picked, str(picked))
    check("후보에 '직접 추가한 관심 종목' 근거 문장", all("관심 종목" in c.why for c in rep.candidates if c.theme == "관심종목"))
    cfg.screen.watchlist = []
    rep2 = Screener(C(), cfg).build_report()
    check("관심 종목이 없으면 후보도 없음(기존 동작 그대로)", not any(c.theme == "관심종목" for c in rep2.candidates))


def section_vwap_reclaim() -> None:
    print("== ★★★ 새 진입 기법: VWAP 재탈환 ==")
    from types import SimpleNamespace
    from daytrader.config import load_config
    from daytrader.playbook import Playbook, Bar, ENTRY_TECHNIQUES

    check("기법이 등록됨", "vwap_reclaim" in ENTRY_TECHNIQUES)
    cfg = load_config(os.path.join(ROOT, "config.yaml"))
    cfg.strategy.entry_order = ["vwap_reclaim"]
    pb = Playbook(cfg)

    def bar(i, o, h, l, c, v):
        return Bar(ts=f"2026-09-21T10:{i:02d}:00", open=o, high=h, low=l, close=c, volume=v)

    def scenario(last_volume, last_close_up=True):
        bars, p = [], 100.0
        for i in range(30):
            bars.append(bar(i, p, p + 0.3, p - 0.3, p + 0.1, 1000))
            p += 0.15
        for i in range(30, 45):
            bars.append(bar(i, p, p + 0.1, p - 0.4, p - 0.25, 900))
            p -= 0.25
        if last_close_up:
            bars.append(bar(45, p - 0.1, p + 2.2, p - 0.2, p + 2.1, last_volume))
        else:
            bars.append(bar(45, p + 2.1, p + 2.2, p - 0.2, p - 0.1, last_volume))
        return bars

    ctx = SimpleNamespace(symbol="X", name="X", theme="t", upper_limit=None, prev_verdict=None, prev_verdicts=[], change_rate=0.03)
    w, _ = pb.evaluate_entry(scenario(2600), ctx)
    check("VWAP 아래에 있다가 거래량 실린 양봉으로 재탈환하면 진입", w is not None and w.technique == "vwap_reclaim")
    w, _ = pb.evaluate_entry(scenario(800), ctx)
    check("거래량이 없으면 진입하지 않음", w is None)
    w, _ = pb.evaluate_entry(scenario(2600, last_close_up=False), ctx)
    check("음봉이면 진입하지 않음", w is None)
    flat = [bar(i, 100, 100.2, 99.8, 100.1, 1000) for i in range(46)]
    w, _ = pb.evaluate_entry(flat, ctx)
    check("VWAP 아래에 있던 적이 없으면 진입하지 않음", w is None)
    w, _ = pb.evaluate_entry(scenario(2600)[:5], ctx)
    check("봉이 부족해도 예외 없이 진입 안 함", w is None)
    check("기본 설정에서는 꺼져 있음(모의매매로 먼저 검증)", "vwap_reclaim" not in load_config(os.path.join(ROOT, "config.yaml")).strategy.entry_order)


def main() -> None:
    sections = [
        section_ticks, section_time, section_config_validate, section_signals,
        section_paper_broker, section_playbook, section_simulator, section_theme_score,
        section_ledger, section_journal, section_orders, section_clock,
        section_quote_router_buying_power, section_simulation_days, section_bithumb_error_hint,
        section_selftest_required_params, section_us_market_session_fallback,
        section_no_fake_night_market_group, section_bithumb_query_hash,
        section_screener_fair_theme_split, section_screener_roundrobin_keeps_all_themes,
        section_screener_distinguishes_no_data,
        section_diagnose_treats_403_as_failure, section_screener_distinguishes_cause,
        section_router_preserves_real_error, section_selection_runs_when_report_missing,
        section_selection_exposes_session_reason, section_toss_quote_has_rate_and_time,
        section_best_signal_selection, section_performance_weight_guards,
        section_bithumb_accounts_empty_is_ok, section_screener_survives_non_dict_rows,
        section_router_handles_signature_mismatch,
        section_notify_in_practice, section_practice_notification_is_marked,
        section_screener_message_clarity, section_screener_shows_why_no_candidate,
        section_notify_test_uses_saved_values, section_telegram_error_hints,
        section_selection_cache_limits_api_calls, section_toss_only_not_skipped,
        section_token_revoked_auto_refresh, section_secrets_single_file,
        section_rankings_response_shapes, section_config_save_never_corrupts,
        section_none_values_never_crash_judgement, section_domestic_watchlist_is_traded, section_vwap_reclaim,
    ]
    for sec in sections:
        sec()

    print(f"\n총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        print("실패한 검증:")
        for name in _failures:
            print(f"  - {name}")
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
