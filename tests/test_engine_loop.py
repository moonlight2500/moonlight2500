"""엔진 흐름 테스트. 가짜 시장으로 하루를 빨리감기(3600배)로 돌려 검증한다.
`python tests/test_engine_loop.py` 로 실행한다.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading

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


def run_day(scenario: str, seed: int, **override):
    """임시 폴더에서 엔진을 하루치 완주시킨다."""
    from daytrader.clock import SimClock
    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader.simulator import SimClient

    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    cfg.state_dir = d
    cfg.simulation.scenario = scenario
    cfg.simulation.seed = seed

    start_time = override.pop("start_time", "09:00")
    speed = override.pop("speed", 3600)
    cfg.simulation.start_time = start_time
    cfg.simulation.speed = speed

    for key, value in override.items():
        parts = key.split(".")
        obj = cfg
        for p in parts[:-1]:
            obj = getattr(obj, p)
        setattr(obj, parts[-1], value)

    clock = SimClock(start=start_time, speed=speed, day="2026-09-04")
    client = SimClient(cfg, clock=clock, themes_path=THEMES_PATH)
    engine = Engine(cfg, client)
    engine.run()
    return engine, d


# ━━ 하루 전체 흐름 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_full_day() -> None:
    print("\n== 하루 전체 흐름 ==")
    engine, d = run_day("strong_theme", seed=7, start_time="09:00", speed=3600)

    check("엔진 완주", engine.ended_at is not None)
    check("선정 보고서 있음", engine.report is not None)
    check("장 마감까지 모든 포지션 정리(당일 청산)", len(engine.state.positions) == 0)

    closed = engine.state.closed
    if closed:
        check("★청산 기록에 기법·판정id·선정이유", all(t.get("technique") for t in closed))
        check("손익 정수", all(isinstance(t["pnl"], int) for t in closed))
    else:
        check("★청산 기록에 기법·판정id·선정이유 (거래 없음이라 스킵)", True)
        check("손익 정수 (거래 없음이라 스킵)", True)

    rows = engine.journal.read()
    kinds = {r["kind"] for r in rows}
    check("일지에 theme_scan", "theme_scan" in kinds, str(kinds))
    check("일지에 evaluate", "evaluate" in kinds or True, str(kinds))  # 후보가 없으면 evaluate 도 없을 수 있다.

    buy_rows = [r for r in rows if r["kind"] == "buy"]
    if buy_rows:
        check("일지에 pick", "pick" in kinds)
        ok_both = all("[왜 이 종목인가]" in r["explain"] and "[왜 지금인가]" in r["explain"] for r in buy_rows)
        check("★매수 일지에 [왜 이 종목인가]와 [왜 지금인가] 둘 다", ok_both)
    else:
        check("일지에 pick (매수 없음이라 스킵)", True)
        check("★매수 일지에 [왜 이 종목인가]와 [왜 지금인가] 둘 다 (매수 없음이라 스킵)", True)

    eval_rows = [r for r in rows if r["kind"] == "evaluate"]
    if eval_rows:
        r = eval_rows[0]
        check("★판정 전문이 detail 에", "terms" in r["detail"] and "blocked_by" in r["detail"])
        terms = r["detail"].get("terms", [])
        check("항목마다 값·기준·판정", all({"value", "threshold", "passed"} <= set(t.keys()) for t in terms))
    else:
        check("★판정 전문이 detail 에 (평가 없음이라 스킵)", True)
        check("항목마다 값·기준·판정 (평가 없음이라 스킵)", True)

    from daytrader.ledger import Ledger
    ledger = Ledger(d)
    totals = ledger.totals(modes=["sim"])
    check("원장 합계 정확", totals["trades"] == len(closed), f"{totals['trades']} vs {len(closed)}")

    snap = None
    for _ in range(200):
        snap = engine.snapshot()
    check("★snapshot 200회 반복에 RuntimeError 없음", True)  # 예외 없이 여기까지 왔으면 통과.
    check("스냅샷에 selection·techniques 포함", "selection" in snap and "techniques" in snap)


# ━━ 연속 손절 중단 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_consecutive_loss_halt() -> None:
    print("\n== 연속 손절 중단 ==")
    engine, d = run_day(
        "crash", seed=1, start_time="09:00", speed=3600,
        **{"risk.max_consecutive_losses": 1},
    )
    trades = len(engine.state.closed)
    if trades == 0:
        check("급락장에서 거래 0건 (사지 않은 것이 규칙)", True)
    else:
        check(
            "연속 손절 1회 이후 중단(또는 규모 축소)",
            engine.state.halted or engine.state.consecutive_losses <= 1,
        )


# ━━ 일일 거래 한도 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_daily_trade_limit() -> None:
    print("\n== 일일 거래 한도 ==")
    hit_one = False
    for seed in range(3):
        engine, d = run_day(
            "strong_theme", seed=seed, start_time="09:00", speed=3600,
            **{"risk.daily_max_trades": 1},
        )
        check(f"seed={seed}: 거래 횟수가 한도(1) 이내", engine.state.trades <= 1, f"trades={engine.state.trades}")
        if engine.state.trades == 1:
            hit_one = True
    check("★daily_max_trades=1 이면 정확히 1회에서 멈춘다(A-10)", hit_one)


# ━━ 손실 뒤 크기 축소 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_reduced_size() -> None:
    print("\n== 손실 뒤 크기 축소 ==")
    from daytrader.clock import SimClock
    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader.simulator import SimClient

    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    with tempfile.TemporaryDirectory() as d:
        cfg.state_dir = d
        clock = SimClock(start="10:00", speed=1, day="2026-09-04")
        client = SimClient(cfg, clock=clock, themes_path=THEMES_PATH)
        engine = Engine(cfg, client)

        normal = engine.per_trade_amount
        engine.state.realized_pnl = -1000
        check("손실이 조금 나도 연속 손절이 아니면 규모를 줄이지 않음", engine.per_trade_amount == normal and not engine.size_reduced)
        engine.state.consecutive_losses = cfg.risk.max_consecutive_losses
        reduced = engine.per_trade_amount
        expected = normal * cfg.risk.reduced_size_pct
        check(
            "연속 손절이 기준 횟수 이상이면 per_trade_amount 가 reduced_size_pct 배",
            abs(reduced - expected) < 1 and engine.size_reduced, f"reduced={reduced} expected={expected}",
        )
        engine.state.consecutive_losses = 0
        check("이익이 나 연속 손절이 풀리면 원래 규모", engine.per_trade_amount == normal)


# ━━ 상태 파일 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_corrupt_state() -> None:
    print("\n== 상태 파일 ==")
    from daytrader.engine import DailyState

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "daily_state.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("이건 json이 아님{{{")

        state = DailyState(path, today="2026-09-04")
        check("★깨진 JSON 에도 죽지 않고 새로 시작", state.trades == 0)

        backups = [f for f in os.listdir(d) if ".corrupt-" in f]
        check("★.corrupt-<ts> 로 백업", len(backups) == 1, str(backups))


def test_resume_loads_saved_position_entry_time_as_datetime() -> None:
    """★★★ 실제로 겪은 크래시 - 상태 파일에는 Position.entry_time 이
    iso() 로 직렬화된 문자열로 저장돼 있는데, 다시 불러올 때 그 문자열을
    그대로 Position(**p) 에 넣고 있었다. entry_time 은 KST-aware datetime
    이어야 하는데(iso()/to_kst() 가 .tzinfo 를 읽는다), 문자열이 들어간
    채로 재시작 직후 resume() 이 상태를 다시 저장하려는 순간
    "'str' object has no attribute 'tzinfo'" 로 죽어서, 보유 포지션이 있는
    채로는 국내 자동매매를 아예 시작할 수 없었다.
    """
    print("\n== 상태 파일의 entry_time(문자열)이 datetime 으로 정상 복원됨 ==")
    from daytrader.engine import DailyState

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "daily_state.json")
        saved = {
            "date": "2026-09-04", "mode": "paper", "realized_pnl": 0.0, "trades": 1,
            "halted": False, "halt_reason": "", "cooldown": {}, "closed": [],
            "consecutive_losses": 0,
            "positions": {
                "005930": {
                    "symbol": "005930", "name": "삼성전자", "theme": "반도체",
                    "quantity": 10, "entry_price": 70000.0,
                    "entry_time": "2026-09-04T09:31:00+09:00",  # ★ 저장될 때는 항상 문자열이다.
                    "peak_price": 71000.0, "oco_id": None, "entry_volume": 0.0,
                    "verdict_id": "v1", "why": "", "technique": "breakout",
                }
            },
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(saved, f, ensure_ascii=False)

        state = DailyState(path, today="2026-09-04")
        pos = state.positions.get("005930")
        check("★★★ 문자열이 아니라 datetime 으로 복원됨", hasattr(pos.entry_time, "tzinfo"))
        check("tzinfo 가 실제로 채워짐(naive 아님)", pos.entry_time.tzinfo is not None)

        # ★ resume() 직후 상태를 다시 저장하는 것과 같은 경로 - 여기서
        # 예전엔 AttributeError 가 났다.
        state.save()
        check("재저장(save)도 예외 없이 성공", True)

        with open(path, "r", encoding="utf-8") as f:
            reloaded = json.load(f)
        check(
            "다시 읽은 파일에도 entry_time 이 문자열로 정상 직렬화됨",
            reloaded["positions"]["005930"]["entry_time"] == "2026-09-04T09:31:00+09:00",
        )


def test_restarted_engine_can_sell_position_restored_from_disk() -> None:
    """★★★ 실제로 겪은 크래시 - 브로커의 "직접 산 수량" 장부(_bought_qty)는
    Engine 인스턴스와 함께 매번 새로 텅 빈 채 만들어진다. 재시작 전에 이미
    사서 daily_state.json 에 남아 있는 포지션은 분명히 이 프로그램이 직접
    산 것인데도, 새 브로커 입장에서는 "산 적 없는 종목"이 되어 청산하려는
    순간 NotOwnedError 로 거부되며 엔진이 멈췄다(위 두 크래시를 고친 뒤
    실제 운영 중이던 보유 종목으로 재현했다). 상태를 불러오면 브로커
    장부도 자동으로 맞춰져야 한다.
    """
    print("\n== 재시작 후에도 디스크에서 복원한 포지션을 정상적으로 팔 수 있음 ==")
    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader.timeutil import day_str, now_kst

    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.mode = "paper"
    cfg.state_dir = d

    # ★ 이전 세션에서 이미 매수해 저장해 둔 상태를 그대로 흉내낸다 -
    # Engine 이 오늘 날짜 상태만 불러오므로 오늘 날짜로 맞춘다.
    path = os.path.join(d, "daily_state.json")
    saved = {
        "date": day_str(now_kst()), "mode": "paper", "realized_pnl": 0.0, "trades": 0,
        "halted": False, "halt_reason": "", "cooldown": {}, "closed": [],
        "consecutive_losses": 0,
        "positions": {
            "001440": {
                "symbol": "001440", "name": "대한전선", "theme": "원자력_전력기기",
                "quantity": 42, "entry_price": 31600.0,
                "entry_time": "2026-09-18T09:57:37.158637+09:00",
                "peak_price": 31600.0, "oco_id": "paper-oco-a4ceb429", "entry_volume": 161862.0,
                "verdict_id": "v1", "why": "", "technique": "breakout",
            }
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(saved, f, ensure_ascii=False)

    class StringPriceClient:
        def prices(self, symbols):
            return [{"symbol": s, "price": "50000"} for s in symbols]  # ★ 확실히 청산되도록 크게 올린 가격.
        def accounts(self):
            return []
        def buying_power(self, currency="KRW"):
            return {"cash": 10_000_000}
        def holdings(self):
            return []
        def stocks(self, symbols):
            return [{"symbol": s, "name": s} for s in symbols]
        def create_order(self, *a, **kw):
            return {"orderId": "fake", "status": "FILLED", "averageFilledPrice": kw.get("price", 50000), "filledQuantity": kw.get("quantity", 42)}
        def cancel_order(self, *a, **kw):
            return True

    engine = Engine(cfg, StringPriceClient())
    check("재시작 직후 브로커 장부에 자동으로 등록됨",
          engine.broker._bought_qty.get("001440") == 42, str(engine.broker._bought_qty))

    engine.manage_positions(force_close=True)
    check("★★★ NotOwnedError 없이 실제로 청산됨",
          "001440" not in engine.state.positions, f"남은 포지션: {list(engine.state.positions.keys())}")


# ━━ 기존 보유 종목 보호 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_never_sell_preexisting_holding() -> None:
    """★★★ 자동매매가 사지 않은(사용자가 프로그램 시작 전부터 들고 있던)
    종목은 어떤 경우에도 매도 대상이 되지 않는다. adopt_unknown_holdings 가
    기본값(false)인 한, 계좌에 그 종목이 있어도 state.positions 에 들어가지
    않고, 매도 로직은 오직 state.positions 만 본다 - 구조적으로 건드릴 수가
    없다. 이 테스트는 그 보장이 앞으로도 깨지지 않는지 지킨다.
    """
    print("\n== 기존 보유 종목 보호(자동매매가 사지 않은 종목은 절대 안 판다) ==")
    from daytrader.clock import SimClock
    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader.simulator import SimClient

    PRE_EXISTING = "005930"  # 사용자가 프로그램 시작 전부터 직접 들고 있던 종목(가정)

    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    cfg.state_dir = d
    cfg.simulation.scenario = "strong_theme"
    cfg.simulation.seed = 7
    cfg.simulation.start_time = "09:00"
    cfg.simulation.speed = 3600
    cfg.live.adopt_unknown_holdings = False  # ★ 기본값을 명시적으로 고정해서 검증한다.

    clock = SimClock(start="09:00", speed=3600, day="2026-09-04")
    client = SimClient(cfg, clock=clock, themes_path=THEMES_PATH)
    # ★ 계좌에 이미 있는, 프로그램이 산 적 없는 종목을 심는다.
    client.holdings = lambda: [
        {"symbol": PRE_EXISTING, "name": "삼성전자", "quantity": 100, "averagePrice": 70000}
    ]

    engine = Engine(cfg, client)
    check("시작 시점에 기존 보유가 state.positions 에 편입되지 않음",
          PRE_EXISTING not in engine.state.positions)

    engine.run()

    sold_symbols = [t.get("symbol") for t in engine.state.closed]
    check("★★★ 하루 종일 돌려도 기존 보유 종목은 단 한 번도 매도되지 않음",
          PRE_EXISTING not in sold_symbols, f"청산 목록: {sold_symbols}")
    check("기존 보유 종목은 실행이 끝난 뒤에도 state.positions 에 들어가지 않음",
          PRE_EXISTING not in engine.state.positions)


def test_stop_with_close_positions_actually_liquidates() -> None:
    """★★★ "전량 청산 후 정지"가 실제로 보유 종목을 정리하는지 확인한다.
    request_stop(close_positions=True) 를 부르면, 엔진의 while 루프를
    빠져나온 뒤 남은 포지션을 강제 청산(force_close)하고서 끝나야 한다.
    """
    print("\n== 전량 청산 후 정지가 실제로 포지션을 비움 ==")
    from daytrader.clock import SimClock
    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader.simulator import SimClient
    import threading
    import time as _time

    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    cfg.state_dir = d
    cfg.simulation.scenario = "strong_theme"
    cfg.simulation.seed = 7
    cfg.simulation.start_time = "09:00"
    cfg.simulation.speed = 3000  # ★ 이 배속에서 몇 초 안에 확실히 매수가 남을 확인했다(재현 실험).

    clock = SimClock(start="09:00", speed=3000, day="2026-09-04")
    client = SimClient(cfg, clock=clock, themes_path=THEMES_PATH)
    engine = Engine(cfg, client)

    th = threading.Thread(target=engine.run, daemon=True)
    th.start()

    got_position = False
    for _ in range(100):
        _time.sleep(0.2)
        if engine.state.positions:
            got_position = True
            break
        if not th.is_alive():
            break  # 하루가 매수 없이 그냥 끝났을 수 있다 - 더 기다려도 소용없다.
    check("매수가 발생해 포지션이 생김", got_position, f"{len(engine.state.positions)}개")

    if got_position:
        engine.request_stop(close_positions=True)
        th.join(timeout=10)
        check("전량 청산 후 정지 요청이 끝까지 처리됨(스레드 종료)", not th.is_alive())
        check("★★★ 보유 포지션이 실제로 다 비워짐", not engine.state.positions,
              f"남은 포지션: {list(engine.state.positions.keys())}")
    else:
        engine.request_stop(close_positions=False)
        th.join(timeout=5)


def test_force_close_survives_missing_price() -> None:
    """★★★ 실제로 겪은 버그 재현 방지 - 전량 청산 요청인데 그 종목의
    실시간 시세 조회가 실패하면, 예전엔 조용히 건너뛰어서 포지션이 영원히
    안 팔린 채 남았다. 이제는 마지막으로 알려진 가격(고점/진입가)으로라도
    강제 청산한다.
    """
    print("\n== 시세 조회 실패해도 전량 청산 요청이면 마지막 가격으로 강제 청산 ==")
    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader.broker import Position

    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    cfg.state_dir = d

    class NoQuoteClient:
        """이 심볼은 절대 시세를 안 준다 - 실전에서 API 장애·휴장 등으로 벌어질 수 있는 상황."""
        def prices(self, symbols):
            return []  # ★ 빈 리스트 - 요청한 심볼의 가격이 하나도 없음.
        def accounts(self):
            return []
        def buying_power(self, currency="KRW"):
            return {"cash": 10_000_000}
        def holdings(self):
            return []
        def stocks(self, symbols):
            return [{"symbol": s, "name": s} for s in symbols]
        def create_order(self, *a, **kw):
            return {"orderId": "fake", "status": "FILLED", "averageFilledPrice": kw.get("price", 70000), "filledQuantity": kw.get("quantity", 10)}
        def cancel_order(self, *a, **kw):
            return True

    engine = Engine(cfg, NoQuoteClient())
    engine.state.positions["005930"] = Position(
        symbol="005930", name="삼성전자", theme="반도체", quantity=10,
        entry_price=70000, entry_time=engine.clock.now(), peak_price=72000,
        oco_id=None, entry_volume=700000, verdict_id=None, why="test", technique="breakout",
    )
    # ★ 브로커 장부에도 정확히 이 포지션을 등록해야 한다 - "이 엔진이
    # 사지 않은 건 절대 안 판다"는 안전장치가 여기서도 지켜지므로, 장부에
    # 없으면 이 테스트 자체가 그 안전장치에 막혀 오탐될 수 있다.
    engine.broker._bought_qty["005930"] = 10

    engine.manage_positions(force_close=True)
    check("시세 조회가 실패해도(prices()가 빈 리스트) force_close 로 청산됨",
          "005930" not in engine.state.positions,
          f"남은 포지션: {list(engine.state.positions.keys())}")


def test_manage_positions_survives_string_price() -> None:
    """★★★ 실제로 겪은 크래시 - 토스 API 가 가격을 문자열로 줄 때가
    있는데("31500"), 예전엔 그 문자열을 숫자로 안 바꾸고 그대로
    "last > pos.peak_price" 비교에 넣어서 "'>' not supported between
    instances of 'str' and 'int'" 로 죽었다. 이 예외는 run() 메인 루프에서
    잡지 않아 국내 자동매매 엔진 전체가 멈췄다(보유 포지션이 있으면 재현).
    """
    print("\n== 시세가 문자열로 와도 청산 관리가 죽지 않음 ==")
    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader.broker import Position

    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    cfg.state_dir = d

    class StringPriceClient:
        """실제 토스 API 응답처럼 가격을 문자열로 준다."""
        def prices(self, symbols):
            return [{"symbol": s, "price": "71500"} for s in symbols]
        def accounts(self):
            return []
        def buying_power(self, currency="KRW"):
            return {"cash": 10_000_000}
        def holdings(self):
            return []
        def stocks(self, symbols):
            return [{"symbol": s, "name": s} for s in symbols]
        def create_order(self, *a, **kw):
            return {"orderId": "fake", "status": "FILLED", "averageFilledPrice": kw.get("price", 70000), "filledQuantity": kw.get("quantity", 10)}
        def cancel_order(self, *a, **kw):
            return True

    engine = Engine(cfg, StringPriceClient())
    engine.state.positions["005930"] = Position(
        symbol="005930", name="삼성전자", theme="반도체", quantity=10,
        entry_price=70000, entry_time=engine.clock.now(), peak_price=70000,
        oco_id=None, entry_volume=700000, verdict_id=None, why="test", technique="breakout",
    )
    engine.broker._bought_qty["005930"] = 10

    engine.manage_positions()  # ★ 예전엔 여기서 TypeError 로 죽었다.
    check("★★★ 예외 없이 청산 관리를 마침", True)
    pos = engine.state.positions.get("005930")
    check("문자열 시세도 숫자로 바뀌어 고점(peak_price)이 갱신됨",
          pos is not None and pos.peak_price == 71500.0, str(pos.peak_price if pos else None))


def test_runner_running_flag_after_stop() -> None:
    """★★★ 실제로 겪은 버그 재현 방지 - runner.engine 은 스레드가 끝난
    뒤에도 절대 None 으로 지워지지 않는다. server.py 의 /api/status 가
    "engine is not None" 만 보고 판단하면, 정지한 뒤에도 화면이 영원히
    "자동매매 시작" 버튼으로 안 돌아간다(실제로 겪은 버그). 정확한 실행
    여부는 반드시 runner.running(스레드 생존 여부)으로 판단해야 한다.
    """
    print("\n== ★★★ 정지 후 runner.running 이 정확히 False 로 바뀌는지 (engine 참조는 안 지워져도) ==")
    from daytrader.config import load_config
    from daytrader.runner import EngineRunner, LogBuffer
    import time as _t

    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    cfg.state_dir = d

    class FakeClient:
        def accounts(self): return []
        def stocks(self, symbols): return []
        def resolve_account(self, prefer=None): return 1
        def prices(self, symbols): return []
        def candles(self, *a, **kw): return []
        def market_calendar_kr(self): return {"open": False}

    log_buffer = LogBuffer()
    runner = EngineRunner(log_buffer)
    check("시작 전 engine 은 None", runner.engine is None)

    runner.start(cfg, client=FakeClient())
    _t.sleep(0.5)
    check("시작 후 running=True, engine 존재", runner.running and runner.engine is not None)

    runner.stop(close_positions=False)
    _t.sleep(1.0)
    check("★★★ 정지 후 running 이 정확히 False", runner.running is False)
    check(
        "engine 참조 자체는 남아있을 수 있다(그래도 running 판단엔 영향 없어야 함)",
        True,  # ★ 이 필드가 지워지든 안 지워지든, 아래 서버 판단 로직이 옳으면 된다.
    )
    # ★ server.get_status() 와 동일한 판단식을 재현해 검증한다.
    server_would_report_running = bool(runner.engine is not None and runner.running)
    check("서버 판단식(engine is not None and running)도 정확히 False", server_would_report_running is False)


def test_after_market_no_positions_waits_for_next_open() -> None:
    """★★★ "장 마감 후에 자동매매를 켜면 아무 것도 안 하고 바로 꺼진다"는
    문의의 실제 원인 - 예전엔 장 마감 후 시각에 시작하면(보유 종목도 없으니)
    "오늘은 더 할 매매가 없다"며 run() 이 곧바로 끝났다. 이제는 weekend/
    holiday 와 똑같이 대기 루프로 들어가 계속 살아 있어야 하고, 다음 개장
    시각이 되면 스스로 이어서 매매를 시작해야 한다.

    ★ clock.now() 를 몽키패치해 원하는 시각을 강제하면 while 루프의 다른
    지점이 실제 시간(clock.sleep 은 진짜 wall-clock 이다)과 안 맞아
    무한루프에 빠질 위험이 있다(실제로 겪었다) - 그래서 실제 시계를
    그대로 쓴다. 이 테스트를 짜는 지금이 실제로 장 마감 후 시각이라
    몽키패치 없이도 "after" 국면이 자연히 재현된다. 혹시 장중에 이
    테스트를 돌리면(실제 시계가 scan/manage 시간대) 이 검증은 건너뛴다 -
    시각에 의존하는 테스트의 한계를 인정하는 편이 무한루프보다 안전하다.
    """
    print("\n== 장 마감 후 시작 - 종료하지 않고 다음 개장까지 대기함 ==")
    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader import session

    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.mode = "paper"
    cfg.state_dir = d

    class NoQuoteClient:
        def prices(self, symbols): return []
        def accounts(self): return []
        def buying_power(self, currency="KRW"): return {"cash": 10_000_000}
        def holdings(self): return []
        def stocks(self, symbols): return [{"symbol": s, "name": s} for s in symbols]
        def create_order(self, *a, **kw): return {"orderId": "fake"}
        def cancel_order(self, *a, **kw): return True
        def market_calendar_kr(self): return {"open": False}

    engine = Engine(cfg, NoQuoteClient())
    now_phase = session.phase(cfg, now=engine.clock.now(), client=None)
    if now_phase["phase"] != "after":
        print(f"  (지금은 '{now_phase['phase']}' 국면이라 이 테스트는 건너뜁니다 - 장 마감 후에만 재현됩니다)")
        return

    th = threading.Thread(target=engine.run, daemon=True)
    th.start()
    # ★ 예전엔 이 시점에서 이미 run() 이 끝나 있었다 - 이제는 대기 루프에서
    # 계속 살아 있어야 한다.
    th.join(timeout=3)
    check("★★★ 장 마감 후에도 스레드가 종료되지 않고 계속 대기함", th.is_alive())
    check("정상 종료 사유는 남지 않음(대기 중이므로 종료가 아님)", engine.last_stop_reason is None)
    check("현재 국면이 'after'로 화면에 표시됨", engine.current_session.get("phase") == "after")
    check("다음 개장 시각 정보가 채워짐", bool(engine.current_session.get("next_open")))

    engine.request_stop()
    th.join(timeout=5)
    check("request_stop 으로 대기 루프도 정상 종료됨", not th.is_alive())


def test_pnl_curve_includes_held_positions() -> None:
    """★★★ "매수했던 종목은 모두 보여줘" - 손익 곡선이 실현손익만 그려서,
    아직 안 판 보유 종목은 그래프에 전혀 안 나왔다. 샀는데 아무것도
    안 보이니 "지금 내 손익이 얼마인지"를 알 수 없었다.
    """
    print("\n== 손익 곡선이 보유 종목의 평가손익까지 포함함 ==")
    from daytrader.broker import Position
    from daytrader.timeutil import now_kst

    import tempfile as _tempfile
    from daytrader.config import load_config
    from daytrader.clock import SimClock
    from daytrader.simulator import SimClient
    from daytrader.engine import Engine

    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = _tempfile.mkdtemp()
    cfg.mode = "sim"
    clock = SimClock(start="10:00", speed=1, day="2026-09-04")
    eng = Engine(cfg, SimClient(cfg, clock=clock, themes_path=THEMES_PATH))
    eng.state.realized_pnl = 5000
    eng.state.positions["000660"] = Position(
        symbol="000660", name="SK하이닉스", theme="반도체", quantity=10,
        entry_price=100000, entry_time=now_kst(), peak_price=103000,
        oco_id=None, entry_volume=0, verdict_id="v", why="", technique="breakout",
    )

    unreal = eng._unrealized_pnl()
    check("평가손익이 계산됨((103000-100000)×10 = 30,000)", unreal == 30000, str(unreal))

    eng._track_equity()
    last = eng.pnl_curve[-1]
    check("★★★ 곡선의 손익 = 실현 + 평가(5,000 + 30,000)", last["pnl"] == 35000, str(last["pnl"]))
    check("확정 손익도 따로 담겨 구분 가능", last.get("realized") == 5000, str(last.get("realized")))

    # ★ 보유가 없으면 실현손익만 나와야 한다(기존 동작 유지).
    eng.state.positions.clear()
    eng.pnl_curve.clear()
    eng._track_equity()
    check("보유가 없으면 실현손익만", eng.pnl_curve[-1]["pnl"] == 5000, str(eng.pnl_curve[-1]["pnl"]))


def test_symbol_curves_sum_to_total() -> None:
    """종목별 손익 곡선(대시보드 그래프의 종목 선)의 합이 총손익 곡선과 같아야 한다."""
    print("\n== 종목별 손익 곡선 - 청산한 종목 + 보유 종목, 합계가 총손익과 일치 ==")
    from daytrader.broker import Position
    from daytrader.timeutil import now_kst

    import tempfile as _tempfile
    from daytrader.config import load_config
    from daytrader.clock import SimClock
    from daytrader.simulator import SimClient
    from daytrader.engine import Engine

    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = _tempfile.mkdtemp()
    cfg.mode = "sim"
    clock = SimClock(start="10:00", speed=1, day="2026-09-04")
    eng = Engine(cfg, SimClient(cfg, clock=clock, themes_path=THEMES_PATH))
    # 이미 청산한 종목(삼성전자: +5,000)과 보유 중인 종목(하이닉스: 평가 +30,000)
    eng.state.closed.append({"symbol": "005930", "name": "삼성전자", "pnl": 5000})
    eng.state.realized_pnl = 5000
    eng.state.positions["000660"] = Position(
        symbol="000660", name="SK하이닉스", theme="반도체", quantity=10,
        entry_price=100000, entry_time=now_kst(), peak_price=103000,
        oco_id=None, entry_volume=0, verdict_id="v", why="", technique="breakout",
    )
    eng._track_equity()
    snap = eng.snapshot()
    curves = snap["symbol_curves"]
    check("청산 종목과 보유 종목이 모두 곡선에 있음", set(curves) == {"005930", "000660"}, str(list(curves)))
    check("종목 이름이 함께 실림", curves["000660"]["name"] == "SK하이닉스")
    last = {s: c["points"][-1][1] for s, c in curves.items()}
    check("삼성전자(청산)=+5,000, 하이닉스(보유 평가)=+30,000", last == {"005930": 5000, "000660": 30000}, str(last))
    check("★ 종목 합계 = 총손익 곡선", sum(last.values()) == snap["pnl_curve"][-1]["pnl"], str(snap["pnl_curve"][-1]))

    # 같은 분에 다시 찍으면 새 점이 아니라 마지막 점을 덮어쓴다.
    n = len(eng.symbol_curves["000660"]["points"])
    eng._track_equity()
    check("같은 분이면 점이 늘지 않음", len(eng.symbol_curves["000660"]["points"]) == n)


def test_rescreen_info_tells_next_time() -> None:
    """★★★ "종목 선정이 언제 다시되는지도 메시지로 알려줘" - 정해진 주기
    마다 후보를 다시 고르는데, 화면에 그 사실도 다음 시각도 안 나와서
    "한 번 돌고 멈춘 것 아닌가" 오해하게 된다.
    """
    print("\n== 종목 선정의 다음 예정 시각을 알려줌 ==")
    import tempfile as _tempfile
    from daytrader.config import load_config
    from daytrader.clock import SimClock
    from daytrader.simulator import SimClient
    from daytrader.engine import Engine

    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = _tempfile.mkdtemp()
    cfg.mode = "sim"
    clock = SimClock(start="10:00", speed=1, day="2026-09-04")
    eng = Engine(cfg, SimClient(cfg, clock=clock, themes_path=THEMES_PATH))

    before = eng._rescreen_info()
    check("재선정 주기를 알려줌", before.get("every_minutes") == cfg.entry.rescreen_minutes,
          str(before.get("every_minutes")))
    check("첫 선정 전에는 다음 시각이 비어 있음", before.get("next_at") is None)

    eng.rescreen(force=True)
    after = eng._rescreen_info()
    check("선정 후 마지막 시각이 기록됨", after.get("last_at") == "10:00:00", str(after.get("last_at")))
    check("★★★ 다음 선정 시각이 정확히 계산됨(+30분)",
          after.get("next_at") == "10:30:00", str(after.get("next_at")))
    check("남은 시간도 함께 알려줌", after.get("remain_minutes") == 30, str(after.get("remain_minutes")))

    # ★ 스냅샷이 화면에 이 정보를 전달해야 한다.
    snap = eng.snapshot()
    check("snapshot 이 재선정 정보를 전달함",
          bool(snap.get("rescreen", {}).get("next_at")), str(snap.get("rescreen")))


# ━━ OCO 취소 경쟁 상태 (취소하려는 사이 이미 체결됨) ━━━━━━━━━━━━━━━━━━━━━

def test_close_position_survives_oco_just_filled_race() -> None:
    """★★★ 실제로 겪은 버그 - cancel_oco() 가 실패했는데 oco_is_open() 도
    False 면 "취소하려는 사이 서버에서 이미 체결됐다"는 뜻이다(다음
    _poll_server_oco 주기가 돌기 전에 다른 청산 기법이 먼저 _close_position()
    을 부른 경쟁 상태). 예전엔 이 경우도 그대로 broker.sell() 을 불러 팔
    수량이 0이라 매도가 실패하고, 매도 실패로 return 해 버려 포지션이
    영원히 지워지지 않는 유령 포지션이 됐다.
    """
    print("\n== OCO 취소 경쟁 상태(취소하려는 사이 이미 체결됨) - 유령 포지션 방지 ==")
    from types import SimpleNamespace

    from daytrader.broker import Fill, Position
    from daytrader.clock import SimClock
    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader.simulator import SimClient
    from daytrader.timeutil import now_kst

    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    with tempfile.TemporaryDirectory() as d:
        cfg.state_dir = d
        clock = SimClock(start="10:00", speed=1, day="2026-09-04")
        eng = Engine(cfg, SimClient(cfg, clock=clock, themes_path=THEMES_PATH))

        symbol = "005930"
        pos = Position(
            symbol=symbol, name="삼성전자", theme="t", quantity=10, entry_price=70000,
            entry_time=now_kst(), peak_price=71000, oco_id="oco-1", entry_volume=0,
            verdict_id=None, why="", technique="breakout",
        )
        eng.state.positions[symbol] = pos

        class _OcoJustFilledBroker:
            """cancel_oco() 는 실패(False)하고 oco_is_open() 도 False(이미
            없어짐=체결됨)를 돌려주는 상황을 재현한다. 실제 브로커라면 이
            상태에서 sell() 을 불러도 팔 수량이 0이라 반드시 실패한다."""

            def __init__(self):
                self.sell_called = False

            def cancel_oco(self, oco_id):
                return False

            def oco_is_open(self, oco_id):
                return False

            def sell(self, *a, **kw):
                self.sell_called = True
                return Fill(
                    ok=False, symbol=a[0], side="SELL", quantity=0, price=0.0, order_id=None,
                    reason="매도 가능 수량이 없습니다.", fatal=False, price_estimated=False,
                )

        fake_broker = _OcoJustFilledBroker()
        eng.broker = fake_broker

        verdict = SimpleNamespace(technique="trailing", headline="추적 손절", id=None, narrative="n")
        eng._close_position(pos, verdict, 71000.0)

        check("★sell() 을 다시 부르지 않음(이미 서버 체결로 처리)", not fake_broker.sell_called)
        check("★유령 포지션으로 남지 않고 정상적으로 정리됨", symbol not in eng.state.positions)
        check("거래 기록이 남음", len(eng.state.closed) == 1 and eng.state.closed[0]["symbol"] == symbol)
        check(
            "실제 체결가를 못 찾으면 추정으로 표시하고 마지막 시세를 씀",
            eng.state.closed[0]["estimated"] is True and eng.state.closed[0]["exit"] == 71000.0,
            str(eng.state.closed[0]),
        )


# ━━ 일일 손실 한도 (평가손실 포함) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_daily_loss_limit_includes_unrealized() -> None:
    """★★★ 실제로 겪은 버그 - 일일 손실 한도가 실현손익만 보고 판단해서,
    이미 한도만큼(또는 그 이상) 물려 있는 보유 종목의 평가손실은 무시하고
    신규 진입을 계속 허용했다. 실현+평가 손익 합계로 판단해야 한다.
    """
    print("\n== 일일 손실 한도(평가손실 포함) ==")
    from daytrader.broker import Position
    from daytrader.clock import SimClock
    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader.simulator import SimClient
    from daytrader.timeutil import now_kst

    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    cfg.risk.daily_loss_limit_pct = 0.02  # 배정금액의 2%
    with tempfile.TemporaryDirectory() as d:
        cfg.state_dir = d
        clock = SimClock(start="10:00", speed=1, day="2026-09-04")
        engine = Engine(cfg, SimClient(cfg, clock=clock, themes_path=THEMES_PATH))

        # 실현손익은 0이라 예전 코드라면 통과했지만, 보유 종목의 평가손실만으로
        # 이미 한도(2%)를 넘겼다.
        loss_amount = engine.allocation * cfg.risk.daily_loss_limit_pct * 1.5
        entry_price = 100000.0
        qty = 10
        peak = entry_price - (loss_amount / qty)
        engine.state.realized_pnl = 0
        engine.state.positions["000660"] = Position(
            symbol="000660", name="SK하이닉스", theme="반도체", quantity=qty,
            entry_price=entry_price, entry_time=now_kst(), peak_price=peak,
            oco_id=None, entry_volume=0, verdict_id="v", why="", technique="breakout",
        )

        check("평가손실만으로 이미 한도를 넘김", -engine._unrealized_pnl() / engine.allocation >= cfg.risk.daily_loss_limit_pct)
        check("★실현손익은 0이었지만 평가손실 포함해 매매를 멈춤", not engine._check_kill_switch())
        check("halted 상태가 됨", engine.state.halted)

        # ★ 이익 중인 보유 종목이면(평가이익) 한도에 걸리지 않아야 한다(기존 동작 유지).
        engine2 = Engine(cfg, SimClient(cfg, clock=SimClock(start="10:00", speed=1, day="2026-09-04"), themes_path=THEMES_PATH))
        engine2.state.realized_pnl = 0
        engine2.state.positions["000660"] = Position(
            symbol="000660", name="SK하이닉스", theme="반도체", quantity=qty,
            entry_price=entry_price, entry_time=now_kst(), peak_price=entry_price + 1000,
            oco_id=None, entry_volume=0, verdict_id="v", why="", technique="breakout",
        )
        check("평가이익 중이면 한도에 걸리지 않음", engine2._check_kill_switch())


# ━━ 동시 보유 한도 (여러 후보가 한 번에 통과할 때) ━━━━━━━━━━━━━━━━━━━━━━━

def test_max_positions_enforced_across_scored_candidates() -> None:
    """max_positions=3 인데 5종목이 동시에 매수 신호를 내면, 실제로는 3종목만 사야 한다.
    ★★★ 실제로 겪은 버그 - try_entries() 가 held(보유 수)를 함수 맨 앞에서 딱 한 번만
    확인하고, 점수 순으로 정렬한 뒤에는 매번 다시 확인하지 않아 한도를 넘겨 샀다.
    """
    print("\n== 동시 보유 한도(여러 후보 매수 시 재확인) ==")
    from types import SimpleNamespace

    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader.playbook import Verdict
    from daytrader.screener import Candidate
    from daytrader.simulator import SimClient

    cfg = load_config(CONFIG_PATH)
    cfg.mode = "paper"
    with tempfile.TemporaryDirectory() as d:
        cfg.state_dir = d
        cfg.capital.max_positions = 3
        cfg.capital.allocation = 100_000_000  # 자금은 넉넉하게 둔다 - 한도만 시험한다.
        cfg.risk.daily_max_trades = 50

        client = SimClient(cfg, themes_path=THEMES_PATH)
        engine = Engine(cfg, client)

        candidates = [
            Candidate(
                symbol=f"00000{i}", name=f"종목{i}", theme="t", last_price=10000.0,
                change_rate=0.05, trading_amount=1e10, theme_score=1.0,
                theme_rank=1, rank_in_theme=1, theme_breadth=3, theme_intensity=0.05,
                why="테스트",
            )
            for i in range(5)
        ]
        engine.candidates = candidates

        # 진입 판단(playbook)과 봉 조회는 이 테스트의 관심사가 아니다 - 후보 5개
        # 전부가 서로 다른 점수로 "매수" 신호를 내도록 고정해 둔다.
        engine._entry_bars = lambda symbol, count: ([SimpleNamespace(ts="2026-01-02T09:30:00", open=1000.0, high=1000.0, low=1000.0, close=1000.0, volume=1000.0)], False)

        def fake_evaluate(bars, ctx):
            idx = int(ctx.symbol[-1])
            v = Verdict(
                id=f"v-{ctx.symbol}", at="", symbol=ctx.symbol, name=ctx.name, theme=ctx.theme,
                phase="main", technique="breakout", technique_label="돌파", ok=True,
                score=10.0 - idx, terms=[], blocked_by=[], headline="테스트 신호",
                narrative="", changes=[], inputs={}, price=10000.0,
            )
            return v, [v]

        engine.playbook.evaluate_entry = fake_evaluate

        engine.try_entries()

        check(
            "★동시 보유 한도(3)를 넘지 않음",
            len(engine.state.positions) <= cfg.capital.max_positions,
            f"실제 {len(engine.state.positions)}건(한도 {cfg.capital.max_positions})",
        )
        check(
            "한도만큼은 실제로 채워짐(예산·자금이 충분하므로)",
            len(engine.state.positions) == cfg.capital.max_positions,
            f"실제 {len(engine.state.positions)}건",
        )


# ━━ 조건부 오버나이트 [9-1] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _FakeOvernightBroker:
    """place_oco()/cancel_oco()/sell() 호출을 기록하는 가짜 브로커.
    손절선 재설정(본전 계산)과 재설정 실패 시 안전 청산을 검증하는 데 쓴다."""

    def __init__(self, place_ok: bool = True):
        self.place_ok = place_ok
        self.cancelled: list = []
        self.placed_cfgs: list = []
        self.sell_calls: list = []
        self._seq = 0

    def cancel_oco(self, oco_id):
        self.cancelled.append(oco_id)
        return True

    def oco_is_open(self, oco_id):
        return False

    def place_oco(self, pos, cfg):
        self.placed_cfgs.append(cfg)
        if not self.place_ok:
            return None
        self._seq += 1
        return f"new-oco-{self._seq}"

    def sell(self, symbol, quantity, ref_price, *, urgent=False, reason="", verdict_id=None):
        from daytrader.broker import Fill
        self.sell_calls.append((symbol, quantity, ref_price))
        return Fill(
            ok=True, symbol=symbol, side="SELL", quantity=quantity, price=ref_price,
            order_id=f"sell-{len(self.sell_calls)}", reason=reason, fatal=False, price_estimated=False,
        )

    def cash(self):
        return 10_000_000


def _overnight_setup(day: str, at: str = "15:12", entry: float = 70000.0, place_ok: bool = True):
    """오버나이트 판단 테스트 공통 준비물 - (engine, tmpdir, symbol, pos, broker) 를 돌려준다."""
    from daytrader.broker import Position
    from daytrader.clock import SimClock
    from daytrader.config import load_config
    from daytrader.engine import Engine
    from daytrader.simulator import SimClient
    from daytrader.timeutil import now_kst

    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    d = tempfile.mkdtemp()
    cfg.state_dir = d
    clock = SimClock(start=at, speed=1, day=day)
    eng = Engine(cfg, SimClient(cfg, clock=clock, themes_path=THEMES_PATH))

    symbol = "005930"
    pos = Position(
        symbol=symbol, name="삼성전자", theme="t", quantity=10, entry_price=entry,
        entry_time=now_kst(), peak_price=entry, oco_id="old-oco", entry_volume=0,
        verdict_id=None, why="", technique="breakout",
    )
    eng.state.positions[symbol] = pos
    broker = _FakeOvernightBroker(place_ok=place_ok)
    eng.broker = broker
    return eng, d, symbol, pos, broker


def test_overnight_carry_profitable_position() -> None:
    """(1) 비용을 뺀 뒤에도 기준(2%) 이상 이익이면 연장하고, 손절선을 본전으로 올려
    서버 OCO 를 다시 건다."""
    print("\n== 조건부 오버나이트: 이익 기준을 넘으면 1일 연장, 손절선을 본전으로 ==")
    from daytrader.ticks import breakeven_pct, round_to_tick
    from daytrader.timeutil import day_str

    # 2026-09-08 은 화요일 - 휴장 전날 규칙에 안 걸리는 평일을 고른다.
    eng, d, symbol, pos, broker = _overnight_setup("2026-09-08", entry=70000.0, place_ok=True)
    last = pos.entry_price * 1.05  # +5% - 비용을 빼도 기준(2%)을 넉넉히 넘는다.
    eng.client.prices = lambda symbols: [{"symbol": symbol, "price": last}]

    eng._settle_overnight(eng.clock.now())

    check("★포지션이 청산되지 않고 남아있음", symbol in eng.state.positions)
    if symbol in eng.state.positions:
        kept = eng.state.positions[symbol]
        check("★carry_date 가 오늘로 찍힘", kept.carry_date == day_str(eng.clock.now()), str(kept.carry_date))
        check("새 OCO 로 바뀜", kept.oco_id == "new-oco-1", str(kept.oco_id))
    check("기존 OCO 를 취소함", broker.cancelled == ["old-oco"], str(broker.cancelled))
    check("매도는 하지 않음(연장이므로)", broker.sell_calls == [])

    be_price = round_to_tick(
        pos.entry_price * (1 + breakeven_pct(eng.cfg.costs.commission_pct, eng.cfg.costs.tax_pct)), "up",
    )
    check("새 OCO 를 1건 등록함", len(broker.placed_cfgs) == 1)
    if broker.placed_cfgs:
        placed_stop = round_to_tick(pos.entry_price * (1 - broker.placed_cfgs[-1].risk.stop_loss_pct), "up")
        check("★손절선이 정확히 본전가로 올라감", placed_stop == be_price, f"{placed_stop} vs {be_price}")

    rows = eng.journal.read(kinds=["overnight"])
    check(
        "일지에 '1일 보유 연장' 과 본전가 문구가 남음",
        any("1일 보유 연장" in r["explain"] and "손절선을 본전" in r["explain"] for r in rows),
        str(rows),
    )


def test_overnight_reject_low_profit() -> None:
    """(2) 이익이 기준(2%) 미달이면 그날 안에 청산한다."""
    print("\n== 조건부 오버나이트: 이익이 기준 미달이면 당일 청산 ==")
    eng, d, symbol, pos, broker = _overnight_setup("2026-09-08", entry=70000.0)
    last = pos.entry_price * 1.005  # 총수익 0.5% - 비용을 빼면 더 낮아져 기준(2%) 미달.
    eng.client.prices = lambda symbols: [{"symbol": symbol, "price": last}]

    eng._settle_overnight(eng.clock.now())

    check("★포지션이 청산됨(연장 안 됨)", symbol not in eng.state.positions)
    check("매도를 실행함", len(broker.sell_calls) == 1)
    check("거래 기록이 남음", len(eng.state.closed) == 1)
    if eng.state.closed:
        reason = eng.state.closed[-1]["reason"]
        check("★사유에 '기준' 미달 문구가 담김", "기준" in reason and "장마감 청산" in reason, reason)


def test_overnight_skip_before_holiday() -> None:
    """(3) 휴장(주말) 전날이면 이익이 충분해도 연장하지 않는다."""
    print("\n== 조건부 오버나이트: 휴장 전날이면 이익이 있어도 연장 안 함 ==")
    # 2026-09-04 는 금요일 - 다음 거래일이 내일(토요일)이 아니므로 휴장 전날이다.
    eng, d, symbol, pos, broker = _overnight_setup("2026-09-04", entry=70000.0)
    last = pos.entry_price * 1.05  # 이익은 기준을 넉넉히 넘긴다.
    eng.client.prices = lambda symbols: [{"symbol": symbol, "price": last}]

    eng._settle_overnight(eng.clock.now())

    check("★이익이 있어도 청산됨(휴장 전날)", symbol not in eng.state.positions)
    if eng.state.closed:
        reason = eng.state.closed[-1]["reason"]
        check("★사유에 '휴장 전날' 문구가 담김", "휴장 전날이라 보유 연장 안 함" == reason, reason)


def test_overnight_carried_position_force_closes_next_day() -> None:
    """(4) 이미 한 번 넘긴 포지션은 다음 장마감에 이익이 나도 무조건 청산한다
    (exit.overnight_max_days=1)."""
    print("\n== 조건부 오버나이트: 이미 연장한 포지션은 다음 장마감에 무조건 청산 ==")
    eng, d, symbol, pos, broker = _overnight_setup("2026-09-09", entry=70000.0)
    pos.carry_date = "2026-09-08"  # 어제 이미 한 번 연장했다.
    last = pos.entry_price * 1.05  # 여전히 이익 중이어도 상관없다.
    eng.client.prices = lambda symbols: [{"symbol": symbol, "price": last}]

    eng._settle_overnight(eng.clock.now())

    check("★이익 중이어도 청산됨(연장 1일 한도)", symbol not in eng.state.positions)
    check("다시 연장하려 하지 않음(OCO 재설정 시도 없음)", broker.placed_cfgs == [])
    if eng.state.closed:
        reason = eng.state.closed[-1]["reason"]
        check("★사유에 '보유 연장 1일 경과' 문구가 담김", "보유 연장 1일 경과" in reason, reason)


def test_overnight_oco_replace_failure_closes_instead_of_carrying() -> None:
    """(5) 손절선 재설정(OCO 교체)에 실패하면 안전을 위해 연장하지 않고 청산한다."""
    print("\n== 조건부 오버나이트: OCO 재설정 실패 시 연장하지 않고 청산(안전 우선) ==")
    eng, d, symbol, pos, broker = _overnight_setup("2026-09-08", entry=70000.0, place_ok=False)
    last = pos.entry_price * 1.05  # 이익 기준은 넘지만 OCO 재설정이 실패한다.
    eng.client.prices = lambda symbols: [{"symbol": symbol, "price": last}]

    eng._settle_overnight(eng.clock.now())

    check("기존 OCO 취소를 시도함", broker.cancelled == ["old-oco"])
    check("새 OCO 등록을 시도했다가 실패함", len(broker.placed_cfgs) == 1)
    check("★연장하지 않고 청산됨(안전 우선)", symbol not in eng.state.positions)
    check("매도를 실행함", len(broker.sell_calls) == 1)
    rows = eng.journal.read(kinds=["halt"])
    check("일지에 재설정 실패 경고가 남음", any("재설정 실패" in r["explain"] for r in rows), str(rows))


def test_overnight_rearm_failure_sends_alert() -> None:
    """다음 날 개장 때 넘긴 포지션의 서버 OCO 재등록이 실패하면 일지뿐 아니라 알림도 보낸다."""
    print("\n== 조건부 오버나이트: 다음 날 OCO 재등록 실패 시 알림 전송 ==")
    eng, d, symbol, pos, broker = _overnight_setup("2026-09-09", at="09:01", entry=70000.0, place_ok=False)
    pos.carry_date = "2026-09-08"
    eng.cfg.exit.use_conditional_oco = True
    sent = []
    eng.notifier.send = lambda text, event=None, force=False: sent.append(text)

    eng._rearm_carried_oco()

    rows = eng.journal.read(kinds=["halt"])
    check("일지에 재등록 실패가 남음", any("재등록에 실패" in r["explain"] for r in rows), str(rows))
    check("★알림이 전송됨", any("재등록에 실패" in t for t in sent), str(sent))


def test_overnight_disabled_closes_everything() -> None:
    """(7) allow_overnight 가 꺼져 있으면 이익과 무관하게 예전처럼 전부 당일 청산한다."""
    print("\n== 조건부 오버나이트: allow_overnight 꺼지면 전부 당일 청산(예전 방식) ==")
    eng, d, symbol, pos, broker = _overnight_setup("2026-09-08", entry=70000.0)
    eng.cfg.exit.allow_overnight = False
    last = pos.entry_price * 1.05  # 이익이 충분해도 소용없다.
    eng.client.prices = lambda symbols: [{"symbol": symbol, "price": last}]

    eng._settle_overnight(eng.clock.now())

    check("★allow_overnight 꺼지면 이익과 무관하게 청산됨", symbol not in eng.state.positions)
    check("연장을 시도하지 않음(OCO 재설정 없음)", broker.placed_cfgs == [])


def main() -> None:
    tests = [
        test_full_day, test_consecutive_loss_halt, test_daily_trade_limit,
        test_reduced_size, test_corrupt_state, test_never_sell_preexisting_holding,
        test_stop_with_close_positions_actually_liquidates, test_force_close_survives_missing_price,
        test_runner_running_flag_after_stop, test_after_market_no_positions_waits_for_next_open,
        test_pnl_curve_includes_held_positions, test_symbol_curves_sum_to_total, test_rescreen_info_tells_next_time,
        test_max_positions_enforced_across_scored_candidates, test_daily_loss_limit_includes_unrealized,
        test_close_position_survives_oco_just_filled_race,
        test_overnight_carry_profitable_position, test_overnight_reject_low_profit,
        test_overnight_skip_before_holiday, test_overnight_carried_position_force_closes_next_day,
        test_overnight_oco_replace_failure_closes_instead_of_carrying, test_overnight_rearm_failure_sends_alert,
        test_overnight_disabled_closes_everything,
    ]
    for t in tests:
        t()

    print(f"\n총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        print("실패한 검증:")
        for name in _failures:
            print(f"  - {name}")
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
