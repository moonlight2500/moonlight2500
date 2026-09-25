"""분할 매수(피라미딩)·분할 매도·신호 강도 배분 엔진 통합 테스트. `python tests/test_scaling.py` 로 실행한다."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from daytrader.config import load_config  # noqa: E402
from daytrader.crypto_engine import CryptoEngine  # noqa: E402
from daytrader.overseas_broker import OverseasPosition  # noqa: E402
from daytrader.overseas_engine import OverseasEngine  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config.yaml")
THEMES_PATH = os.path.join(ROOT, "themes.yaml")

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


class FakeClient:
    """돌파 신호가 항상 살아 있는 가짜 시장(분봉은 매번 같은 돌파 봉). price 를 바꿔 가며 시나리오를 만든다."""

    def __init__(self, price=100.0):
        self.price = price
        self.orders: list = []

    def buying_power(self, currency="KRW"):
        return {"cash": 1e9}

    def prices(self, symbols):
        return [{"symbol": symbols[0], "price": self.price}]

    def candles(self, symbol, interval, count, before=None):
        rows = []
        p = 100.0
        for i in range(count - 1):
            rows.append({"timestamp": str(i), "open": p, "high": p * 1.005, "low": p * 0.995, "close": p, "volume": 500_000})
        rows.append({"timestamp": "last", "open": p, "high": p * 1.03, "low": p * 0.99, "close": p * 1.02, "volume": 2_000_000})
        return rows

    def create_order(self, symbol, side, orderType, quantity, price=None, timeInForce="DAY", clientOrderId=None):
        self.orders.append((symbol, side, quantity))
        return {"orderId": "x"}


def _ov_cfg(d, **sizing):
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = d
    cfg.overseas.watchlist = ["AAPL"]
    cfg.overseas.mode = "paper"
    cfg.overseas.max_positions = 4
    cfg.overseas.budget_usd = 10000.0
    cfg.strategy.entry_order = ["breakout"]
    for k, v in sizing.items():
        setattr(cfg.sizing, k, v)
    return cfg


def test_overseas_full_cycle() -> None:
    print("== 해외: 첫 매수 → 추가 매수 → 분할 매도 → 나머지 정리(기록은 한 건) ==")
    d = tempfile.mkdtemp()
    client = FakeClient(100.0)
    cfg = _ov_cfg(d)
    eng = OverseasEngine(cfg, client=client)
    cap = 10000.0 / 4
    eng._try_entry("AAPL")
    pos = eng.state.book.get("AAPL")
    check("첫 매수됨", pos is not None)
    first = pos.invested
    weak_frac0 = cfg.sizing.initial_ratio * cfg.sizing.min_mult
    strong_frac0 = min(1.0, cfg.sizing.initial_ratio * cfg.sizing.max_mult * 1.2)  # vol_multiplier 상단(1.2배)까지 감안
    check("첫 매수는 종목당 한도의 설정된 배분 범위 안", cap * weak_frac0 - 1 <= first <= cap * strong_frac0 + 1, f"${first:,.0f} / 한도 ${cap:,.0f}")
    check("현금이 첫 매수만큼 줄었음", abs(eng.broker.cash - (10000.0 - first)) < 1e-6)

    add_bump = 1 + 0.4 * cfg.overseas.stop_loss_pct + 0.003  # 추가 매수 간격(손절폭의 40%)보다 여유 있게

    # 오르지 않으면 추가 매수 없음(체결가 = 마지막 봉 종가라 시세를 그 값에 맞춘다)
    client.price = pos.entry_price
    eng._manage_position("AAPL", pos, allow_add=True)
    check("아직 안 올랐으면 추가 매수 안 함", pos.adds == 0)
    # 손실 중에도 안 삼(물타기 금지) - 손절선 안쪽의 소폭 하락
    client.price = pos.entry_price * (1 - min(cfg.overseas.stop_loss_pct * 0.5, 0.01))
    eng._manage_position("AAPL", pos, allow_add=True)
    check("손실 중에는 추가 매수 안 함(물타기 금지)", pos.adds == 0)
    # 오르면 추가
    client.price = pos.entry_price * add_bump  # 손절폭의 40% 이상
    eng._manage_position("AAPL", pos, allow_add=True)
    check("이익 중이고 신호가 살아 있으면 추가 매수(1회)", pos.adds == 1, f"adds={pos.adds}")
    check("평균 매수가가 다시 계산됨(첫 매수가와 현재가 사이)", 100.0 < pos.entry_price < client.price, f"{pos.entry_price:.3f}")
    client.price = pos.last_fill_price * add_bump
    eng._manage_position("AAPL", pos, allow_add=True)
    check("한 번 더 올라 2회째 추가 매수", pos.adds == 2, f"adds={pos.adds}")
    eng._manage_position("AAPL", pos, allow_add=True)
    check("추가 횟수 상한(2회)을 넘지 않음", pos.adds == 2)
    check("총 투입금은 종목당 한도를 넘지 않음", pos.invested <= cap + 1e-6, f"${pos.invested:,.0f}")
    check("추가 매수 여부와 무관하게 거래 기록은 아직 없음", not eng.state.closed)

    # 분할 매도
    entry = pos.entry_price
    qty0 = pos.quantity
    half_take = 1 + 0.5 * cfg.overseas.take_profit_pct + 0.003  # 익절폭의 절반 이상
    full_take = 1 + cfg.overseas.take_profit_pct + 0.003  # 익절폭 이상
    client.price = entry * half_take
    eng._manage_position("AAPL", pos, allow_add=True)
    check("1차 분할 매도: 일부만 팔고 유지", eng.state.book.owns("AAPL") and pos.scaled_out == 1 and 0 < pos.quantity < qty0,
          f"남은 {pos.quantity:.3f}/{qty0:.3f}")
    check("분할 매도 도중에는 거래 기록·거래 수를 늘리지 않음", not eng.state.closed and eng.state.trades == 0)
    check("분할 매도 뒤에는 추가 매수 안 함", pos.adds == 2)
    client.price = entry * full_take
    eng._manage_position("AAPL", pos, allow_add=True)
    check("2차 분할 매도(익절폭 도달)", pos.scaled_out == 2, f"scaled_out={pos.scaled_out}")
    # 고점에서 되돌리면 추적 손절로 나머지 정리
    client.price = entry * full_take * (1 - cfg.overseas.trailing_pct - 0.01)
    eng._manage_position("AAPL", eng.state.book.get("AAPL"), allow_add=True)
    check("나머지는 추적 손절로 정리됨", not eng.state.book.owns("AAPL"))
    check("거래 기록은 정확히 한 건", len(eng.state.closed) == 1, str(len(eng.state.closed)))
    rec = eng.state.closed[0]
    check("기록에 추가 매수·분할 매도 횟수가 남음", rec.get("adds") == 2 and rec.get("scaled_out") == 2, str(rec))
    check("기록의 사유에 분할 매도 표기", "분할 매도 2회" in rec["reason"], rec["reason"])
    check("거래 수 1, 이익이라 연속 손절 0", eng.state.trades == 1 and eng.state.consecutive_losses == 0)
    total_cash_pnl = eng.broker.cash - 10000.0
    check("기록 손익 = 실제 현금 증감(분할 몫 합계)", abs(rec["pnl"] - total_cash_pnl) < 1e-6, f"{rec['pnl']:.4f} vs {total_cash_pnl:.4f}")
    check("총 수량 = 처음+추가로 산 수량 전부", rec["quantity"] > 0 and abs(rec["quantity"] - qty0) < 1e-6, f"{rec['quantity']:.4f} vs {qty0:.4f}")


def test_overseas_breakeven_and_loss() -> None:
    print("== 해외: 1차 매도 후 본전 방어, 분할 도중 손절 ==")
    d = tempfile.mkdtemp()
    client = FakeClient(100.0)
    cfg = _ov_cfg(d, scale_in=False)
    eng = OverseasEngine(cfg, client=client)
    eng._try_entry("AAPL")
    pos = eng.state.book.get("AAPL")
    entry = pos.entry_price
    client.price = entry * (1 + 0.5 * cfg.overseas.take_profit_pct + 0.003)
    eng._manage_position("AAPL", pos)
    check("1차 분할 매도", pos.scaled_out == 1)
    client.price = entry * 1.0005  # 본전 근처로 되돌림
    eng._manage_position("AAPL", pos)
    check("본전 아래로 내려오면 나머지 정리(챙긴 이익 지킴)", not eng.state.book.owns("AAPL"))
    rec = eng.state.closed[-1]
    check("사유가 breakeven_stop + 분할 표기", rec["reason"].startswith("breakeven_stop") and "분할 매도 1회" in rec["reason"], rec["reason"])
    check("전체로는 이익(1차에서 챙긴 이익 > 본전 청산 손실)", rec["pnl"] > 0, f"{rec['pnl']:.4f}")

    d2 = tempfile.mkdtemp()
    client2 = FakeClient(100.0)
    cfg2 = _ov_cfg(d2)
    eng2 = OverseasEngine(cfg2, client=client2)
    eng2._try_entry("AAPL")
    pos2 = eng2.state.book.get("AAPL")
    client2.price = pos2.entry_price * (1 - cfg2.overseas.stop_loss_pct - 0.005)  # 손절선 아래
    eng2._manage_position("AAPL", pos2)
    check("손절은 분할 없이 전량", not eng2.state.book.owns("AAPL") and eng2.state.closed[-1]["pnl"] < 0)
    check("손절이면 연속 손절 1", eng2.state.consecutive_losses == 1)


def test_overseas_signal_sizing_off_and_persistence() -> None:
    print("== 해외: 신호 배분 끄기, 상태 저장·복원 ==")
    d = tempfile.mkdtemp()
    client = FakeClient(100.0)
    cfg = _ov_cfg(d, signal_sizing=False, scale_in=False, scale_out=False)
    eng = OverseasEngine(cfg, client=client)
    eng._try_entry("AAPL")
    pos = eng.state.book.get("AAPL")
    cap = 2500.0
    check("신호 배분·분할을 모두 끄면 종목당 한도의 1/max_mult 만큼(한도 초과 없음)", pos.invested <= cap + 1e-6, f"${pos.invested:,.0f}")
    client.price = pos.entry_price * (1 + cfg.overseas.take_profit_pct + 0.02)
    eng._manage_position("AAPL", pos)
    check("분할 매도를 끄면 익절에서 전량 청산(예전 방식)", not eng.state.book.owns("AAPL"))

    d2 = tempfile.mkdtemp()
    client2 = FakeClient(100.0)
    eng2 = OverseasEngine(_ov_cfg(d2), client=client2)
    eng2._try_entry("AAPL")
    pos2 = eng2.state.book.get("AAPL")
    client2.price = pos2.entry_price * 1.02
    eng2._manage_position("AAPL", pos2, allow_add=True)
    client2.price = pos2.entry_price * 1.03
    eng2._manage_position("AAPL", pos2, allow_add=True)
    eng2.state.save()
    eng3 = OverseasEngine(_ov_cfg(d2), client=client2, state_path=eng2.state.path)
    p3 = eng3.state.book.get("AAPL")
    check("재시작해도 추가 매수·분할 매도 상태가 복원됨",
          p3 is not None and p3.adds == pos2.adds and p3.scaled_out == pos2.scaled_out and abs(p3.invested - pos2.invested) < 1e-6
          and abs(p3.sold_qty - pos2.sold_qty) < 1e-9 and abs(p3.last_fill_price - pos2.last_fill_price) < 1e-9,
          f"{p3.adds}/{pos2.adds} {p3.scaled_out}/{pos2.scaled_out}" if p3 else "없음")


def test_overseas_strength_sizes_differently() -> None:
    print("== 해외: 신호가 강할수록 더 삼 ==")
    from daytrader import sizing
    cfg = load_config(CONFIG_PATH)
    cap = sizing.position_cap(cfg.overseas.budget_usd, cfg.overseas.max_positions)
    weak = sizing.entry_amount(cap, 0.0, cfg.sizing)
    strong = sizing.entry_amount(cap, 1.0, cfg.sizing)
    check("약한 신호 < 강한 신호 금액", weak < strong, f"${weak:,.0f} < ${strong:,.0f}")


# ━━ 암호화폐 ━━

class CryptoClient(FakeClient):
    def candles(self, market, unit=1, count=200, to=None):
        rows = []
        p = 100.0
        for i in range(count - 1):
            rows.append({"candle_date_time_kst": f"2026-09-21T10:{i % 60:02d}:00", "opening_price": p, "high_price": p * 1.005,
                         "low_price": p * 0.995, "trade_price": p, "candle_acc_trade_volume": 500_000})
        rows.append({"candle_date_time_kst": "2026-09-21T11:59:00", "opening_price": p, "high_price": p * 1.03,
                     "low_price": p * 0.99, "trade_price": p * 1.02, "candle_acc_trade_volume": 2_000_000})
        return rows

    def ticker(self, markets):
        return [{"market": markets[0], "trade_price": self.price}]


def test_crypto_full_cycle() -> None:
    print("== 암호화폐: 예산·추가 매수·분할 매도 ==")
    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = d
    cfg.crypto.mode = "paper"
    cfg.crypto.watchlist = ["KRW-BTC"]
    cfg.crypto.auto_top_volume = False
    cfg.crypto.entry_order = ["breakout"]
    cfg.crypto.exit_enabled = ["fixed", "trailing", "atr_stop", "time_stop"]
    cfg.crypto.budget = 10_000_000
    cfg.crypto.max_positions = 5
    client = CryptoClient(100.0)
    eng = CryptoEngine(cfg, client=client)
    check("모의 시작 현금 = 총 투자금액", abs(eng.broker.cash() - 10_000_000) < 1)
    cap = 2_000_000
    eng._try_entry("KRW-BTC")
    pos = eng.state.book.get("KRW-BTC")
    check("첫 매수됨", pos is not None, eng.last_error)
    if pos is None:
        return
    weak_frac = cfg.sizing.initial_ratio * cfg.sizing.min_mult
    strong_frac = min(1.0, cfg.sizing.initial_ratio * cfg.sizing.max_mult * 1.2)  # vol_multiplier 상단(1.2배)까지 감안
    check("첫 매수는 종목당 한도(200만원)의 설정된 배분 범위 안", cap * weak_frac - 1 <= pos.invested <= cap * strong_frac + 1, f"{pos.invested:,.0f}원")
    client.price = pos.entry_price * (1 + 0.4 * cfg.crypto.stop_loss_pct + 0.003)  # 손절폭의 40% 이상
    eng._manage_position("KRW-BTC", pos, allow_add=True)
    check("이익 중 추가 매수", pos.adds == 1, f"adds={pos.adds} {eng.last_error}")
    check("한도 이내", pos.invested <= cap + 1e-6)
    entry = pos.entry_price
    full_take = 1 + cfg.crypto.take_profit_pct + 0.003
    client.price = entry * (1 + 0.5 * cfg.crypto.take_profit_pct + 0.003)  # crypto 익절폭의 절반 이상
    eng._manage_position("KRW-BTC", pos, allow_add=True)
    check("1차 분할 매도", pos.scaled_out == 1 and eng.state.book.owns("KRW-BTC"))
    client.price = entry * full_take
    eng._manage_position("KRW-BTC", pos, allow_add=True)
    check("2차 분할 매도", pos.scaled_out == 2)
    client.price = entry * full_take * (1 - cfg.crypto.trailing_pct - 0.01)
    eng._manage_position("KRW-BTC", eng.state.book.get("KRW-BTC"), allow_add=True)
    check("나머지 정리 후 기록 한 건", not eng.state.book.owns("KRW-BTC") and len(eng.state.closed) == 1, str(len(eng.state.closed)))
    rec = eng.state.closed[0]
    check("기록 손익 = 현금 증감", abs(rec["pnl"] - (eng.broker.cash() - 10_000_000)) < 1e-3, f"{rec['pnl']:.1f} vs {eng.broker.cash() - 10_000_000:.1f}")
    check("기록에 횟수 표기", rec.get("adds") == 1 and rec.get("scaled_out") == 2, str(rec))


def test_crypto_legacy_budget() -> None:
    print("== 암호화폐: 예전 설정(코인당 금액)만 있으면 그 곱을 예산으로 ==")
    d = tempfile.mkdtemp()
    cfg = load_config(CONFIG_PATH)
    cfg.state_dir = d
    cfg.crypto.mode = "paper"
    cfg.crypto.budget = 0
    cfg.crypto.allocation_per_coin = 100000
    cfg.crypto.max_positions = 5
    eng = CryptoEngine(cfg, client=CryptoClient(100.0))
    check("예산 0 이면 코인당 금액 × 동시 보유 수", abs(eng._budget() - 500_000) < 1)


# ━━ 국내 ━━

def test_domestic_scaling() -> None:
    print("== 국내: 분할 매도·추가 매수 기록 ==")
    from daytrader.broker import Position
    from daytrader.clock import SimClock
    from daytrader.engine import Engine
    from daytrader.simulator import SimClient

    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    cfg.capital.allocation = 10_000_000
    cfg.capital.max_positions = 3
    cfg.exit.use_conditional_oco = False
    with tempfile.TemporaryDirectory() as d:
        cfg.state_dir = d
        clock = SimClock(start="10:00", speed=1, day="2026-09-04")
        eng = Engine(cfg, SimClient(cfg, clock=clock, themes_path=THEMES_PATH))
        cap = eng.position_cap
        check("종목당 한도 = 총액/동시보유", abs(cap - 10_000_000 / 3) < 1, f"{cap:,.0f}")
        weak, strong = eng._entry_amount(0.0), eng._entry_amount(1.0)
        weak_frac = cfg.sizing.initial_ratio * cfg.sizing.min_mult
        strong_frac = min(1.0, cfg.sizing.initial_ratio * cfg.sizing.max_mult)
        check("국내도 강한 신호가 더 많이 삼", abs(weak / cap - weak_frac) < 1e-6 and abs(strong / cap - strong_frac) < 1e-6
              and weak < strong, f"{weak:,.0f}/{strong:,.0f}")
        eng.state.consecutive_losses = cfg.risk.max_consecutive_losses
        check("연속 손절 중이면 첫 매수 축소", abs(eng._entry_amount(1.0) - strong * cfg.risk.reduced_size_pct) < 1)
        eng.state.consecutive_losses = 0

        # 직접 만든 포지션으로 분할 매도 절차 검증
        symbol = "005930"
        buy = eng.broker.buy(symbol, 100, 70000.0, reason="t")
        pos = Position(symbol=symbol, name="삼성전자", theme="t", quantity=buy.quantity, entry_price=buy.price,
                       entry_time=clock.now(), peak_price=buy.price, oco_id=None, entry_volume=1.0, verdict_id=None,
                       why="", technique="breakout", last_fill_price=buy.price, invested=buy.price * buy.quantity)
        eng.state.positions[symbol] = pos
        v = SimpleNamespace(technique="scale_out_1", headline="1차", id=None, narrative="n")
        eng._close_position(pos, v, 71800.0, fraction=0.34)
        check("1차 분할 매도: 일부만 팔림", symbol in eng.state.positions and 60 < pos.quantity < 70, f"{pos.quantity}")
        check("분할 도중 원장 기록·거래 수 그대로", not eng.state.closed and eng.state.trades == 0)
        check("나눠 판 횟수·누적 수량 기록", pos.scaled_out == 1 and pos.sold_qty == 34 and pos.realized > 0, f"{pos.scaled_out}/{pos.sold_qty}/{pos.realized}")
        v2 = SimpleNamespace(technique="scale_out_2", headline="2차", id=None, narrative="n")
        eng._close_position(pos, v2, 73500.0, fraction=0.5)
        check("2차 분할 매도", pos.scaled_out == 2 and symbol in eng.state.positions)
        v3 = SimpleNamespace(technique="trailing", headline="추적", id=None, narrative="n")
        eng._close_position(pos, v3, 72500.0)
        check("마지막 청산으로 포지션 정리", symbol not in eng.state.positions)
        check("거래 기록은 한 건, 총 수량 100주", len(eng.state.closed) == 1 and eng.state.closed[0]["qty"] == 100, str(eng.state.closed[-1:]))
        rec = eng.state.closed[0]
        check("평균 청산가가 세 번의 가격 사이", 71800 < rec["exit"] < 73500, f"{rec['exit']:.1f}")
        check("기록에 분할 횟수와 사유 표기", rec["scaled_out"] == 2 and "분할 매도 2회" in rec["reason"], rec["reason"])
        check("거래 수 1, 연속 손절 0", eng.state.trades == 1 and eng.state.consecutive_losses == 0)
        check("기록 손익이 이익", rec["pnl"] > 0, str(rec["pnl"]))
        check("일일 실현손익 = 세 번의 손익 합(중복 없음)", abs(eng.state.realized_pnl - rec["pnl"]) < 2, f"{eng.state.realized_pnl} vs {rec['pnl']}")


def test_domestic_pyramiding() -> None:
    print("== 국내: 추가 매수(피라미딩) ==")
    from daytrader.broker import Position
    from daytrader.clock import SimClock
    from daytrader.engine import Engine
    from daytrader.playbook import Bar
    from daytrader.simulator import SimClient

    cfg = load_config(CONFIG_PATH)
    cfg.mode = "sim"
    cfg.capital.allocation = 10_000_000
    cfg.capital.max_positions = 3
    cfg.exit.use_conditional_oco = False
    with tempfile.TemporaryDirectory() as d:
        cfg.state_dir = d
        clock = SimClock(start="10:00", speed=1, day="2026-09-04")
        eng = Engine(cfg, SimClient(cfg, clock=clock, themes_path=THEMES_PATH))
        winner = SimpleNamespace(id="v", technique_label="돌파", to_dict=lambda: {})
        alive = {"on": True}
        eng.playbook.evaluate_entry = lambda bars, ctx: ((winner if alive["on"] else None), [])
        eng._entry_bars = lambda symbol, count=60: ([Bar(ts="t", open=1, high=1, low=1, close=1, volume=1)], False)
        symbol = "005930"
        cap = eng.position_cap
        # ★ 첫 매수 수량을 cap × initial_ratio 에 맞춘다(실제 entry_amount() 가 만드는 비중과 동일) -
        # 그래야 남은 몫(1-initial_ratio)을 max_adds 번으로 정확히 나눠 채울 여지가 생긴다. 예전처럼
        # 임의의 고정 수량(30주)을 쓰면 initial_ratio 를 낮춰도 첫 매수 비중이 그대로라 3번째 추가
        # 매수를 시험해 볼 여지가 안 생긴다.
        qty0_shares = int((cap * cfg.sizing.initial_ratio) // 70000.0)
        buy = eng.broker.buy(symbol, qty0_shares, 70000.0, reason="t")
        pos = Position(symbol=symbol, name="삼성전자", theme="t", quantity=buy.quantity, entry_price=buy.price, entry_time=clock.now(),
                       peak_price=buy.price, oco_id=None, entry_volume=1.0, verdict_id=None, why="", technique="breakout",
                       last_fill_price=buy.price, invested=buy.price * buy.quantity)
        eng.state.positions[symbol] = pos
        add_bump = 1 + 0.4 * cfg.risk.stop_loss_pct + 0.003  # 추가 매수 간격(손절폭의 40%)보다 여유 있게
        eng._maybe_add(pos, pos.entry_price * (1 + min(cfg.risk.stop_loss_pct * 0.1, 0.005)), [])
        check("조금 올랐을 때는 추가 안 함", pos.adds == 0)
        alive["on"] = False
        eng._maybe_add(pos, pos.entry_price * add_bump, [])
        check("진입 신호가 꺼졌으면 추가 안 함", pos.adds == 0)
        alive["on"] = True
        q0 = pos.quantity
        first_entry = pos.entry_price
        eng._maybe_add(pos, pos.entry_price * add_bump, [])
        check("이익 중 + 신호 유지 -> 추가 매수 1회", pos.adds == 1 and pos.quantity > q0, f"{q0}->{pos.quantity}")
        check("평균 매수가가 올라감(첫 매수가 < 평균 < 현재가)", first_entry < pos.entry_price < first_entry * add_bump, f"{pos.entry_price:.1f}")
        check("투입금이 한도 이내", pos.invested <= cap + 1, f"{pos.invested:,.0f}/{cap:,.0f}")
        check("추가 매수도 브로커 장부에 반영(나중에 전량 매도 가능)", eng.broker._bought_qty.get(symbol) == pos.quantity)
        eng._maybe_add(pos, pos.last_fill_price * add_bump, [])
        check("2회째 추가", pos.adds == 2)
        max_adds = cfg.sizing.max_adds
        eng._maybe_add(pos, pos.last_fill_price * add_bump, [])
        check(f"3회째 추가(설정한 상한 {max_adds}회까지)", pos.adds == min(3, max_adds), f"adds={pos.adds}")
        eng._maybe_add(pos, pos.last_fill_price * add_bump, [])
        check(f"상한({max_adds}회) 초과 안 함", pos.adds == max_adds, f"adds={pos.adds}")
        v = SimpleNamespace(technique="trailing", headline="추적", id=None, narrative="n")
        eng._close_position(pos, v, pos.entry_price * add_bump)
        check("추가 매수한 수량 전부 청산되고 기록 한 건", symbol not in eng.state.positions and len(eng.state.closed) == 1
              and eng.state.closed[0]["qty"] == pos.quantity and eng.state.closed[0].get("adds") == max_adds, str(eng.state.closed[-1:]))


def test_time_of_day_windows_and_techniques() -> None:
    print("== 국내 장 초반·막판 변동성 단타(시간대·기법) ==")
    from datetime import datetime
    from daytrader import session
    from daytrader.playbook import Bar, Playbook, ENTRY_TECHNIQUES
    from daytrader.timeutil import KST

    cfg = load_config(CONFIG_PATH)

    def ph(h, m, extra=True):
        cfg.entry.extra_windows = extra
        return session.phase(cfg, now=datetime(2026, 9, 4, h, m, tzinfo=KST), client=None)

    a = ph(8, 30)
    check("개장 전(08:30)은 pre", a["phase"] == "pre", a["phase"])
    b = ph(9, 5)
    check("09:05 는 장 초반 시간대(open)에서 신규 매수 가능", b["trading"] and b["window"] == "open", str(b["window"]))
    c = ph(9, 30)
    check("09:30 은 기존 매매 시간(main)", c["trading"] and c["window"] == "main")
    d = ph(14, 30)
    check("14:30 은 장 막판 시간대(close)에서 신규 매수 가능", d["trading"] and d["window"] == "close", str(d["window"]))
    e = ph(15, 5)
    check("15:05 는 보유분 관리만", (not e["trading"]) and e["phase"] == "manage")
    f = ph(9, 5, extra=False)
    g = ph(14, 30, extra=False)
    check("확장 시간대를 끄면 예전처럼(09:05 개장 전 취급·14:30 관리만)", f["phase"] == "pre" and g["phase"] == "manage" and not g["trading"])
    cfg.entry.extra_windows = True

    check("두 기법이 등록됨", "open_gap" in ENTRY_TECHNIQUES and "close_squeeze" in ENTRY_TECHNIQUES)

    # ── 시초 갭 돌파 ──
    def bar(i, o, h, l, c, v):
        return Bar(ts=f"t{i}", open=o, high=h, low=l, close=c, volume=v)

    def open_bars(last_vol=3000, last_close=10220):
        bars = [bar(-i, 9900, 9950, 9880, 9920, 800) for i in range(20, 0, -1)]  # 어제 늦은 시간 봉(섞여 들어와도 당일만 골라야 함)
        for i in range(5):
            bars.append(bar(i, 10120, 10150, 10100, 10130, 1000))
        for i in range(5, 12):
            bars.append(bar(i, 10120, 10140, 10100, 10125, 900))
        bars.append(bar(12, 10140, 10230, 10135, last_close, last_vol))
        return bars

    def ctx(hh, mm, chg, window):
        return SimpleNamespace(symbol="X", name="X", theme="t", upper_limit=None, prev_verdict=None, prev_verdicts=[],
                               now=datetime(2026, 9, 4, hh, mm, tzinfo=KST), change_rate=chg, kr_session=True, window=window)

    cfg.strategy.entry_order = ["open_gap"]
    pb = Playbook(cfg)
    w, _ = pb.evaluate_entry(open_bars(), ctx(9, 12, 0.022, "open"))
    check("갭 1.2%·첫 5분 고점 돌파·거래량 급증 -> 시초 갭 돌파 진입", w is not None and w.technique == "open_gap")
    w, _ = pb.evaluate_entry(open_bars(last_vol=900), ctx(9, 12, 0.022, "open"))
    check("거래량이 없으면 진입 안 함", w is None)
    w, _ = pb.evaluate_entry(open_bars(last_close=10140), ctx(9, 12, 0.014, "open"))
    check("첫 5분 고점을 못 넘으면 진입 안 함", w is None)
    w, _ = pb.evaluate_entry(open_bars(), ctx(9, 12, 0.005, "open"))
    check("갭이 작으면(등락률 0.5% → 갭 마이너스) 진입 안 함", w is None)
    w, _ = pb.evaluate_entry(open_bars(), ctx(9, 50, 0.022, "main"))
    check("09:40 이후에는 작동 안 함", w is None)
    nc = SimpleNamespace(symbol="X", name="X", theme="t", upper_limit=None, prev_verdict=None, prev_verdicts=[],
                         now=datetime(2026, 9, 4, 9, 12, tzinfo=KST), change_rate=0.022)
    w, _ = pb.evaluate_entry(open_bars(), nc)
    check("국내 정규장 컨텍스트가 아니면(해외·코인) 작동 안 함", w is None)

    # ── 장 막판 상승 지속 ──
    def close_bars(last_vol=2000, dip=0.0):
        bars = []
        for i in range(60):
            p0 = 10000 + i * 8
            bars.append(bar(i, p0, p0 + 12, p0 - 6, p0 + 8, 1000))
        for i in range(60, 65):
            p0 = 10480 + (i - 60) * 8
            bars.append(bar(i, p0 - dip, p0 + 12, p0 - 6, p0 + 10 - dip, last_vol))
        return bars

    cfg.strategy.entry_order = ["close_squeeze"]
    pb2 = Playbook(cfg)
    w, _ = pb2.evaluate_entry(close_bars(), ctx(14, 40, 0.055, "close"))
    check("14:40 강한 종목이 고점 근처·VWAP 상승·거래량 증가 -> 장 막판 진입", w is not None and w.technique == "close_squeeze")
    w, _ = pb2.evaluate_entry(close_bars(last_vol=1000), ctx(14, 40, 0.055, "close"))
    check("막판 거래량이 안 붙으면 진입 안 함", w is None)
    w, _ = pb2.evaluate_entry(close_bars(), ctx(13, 0, 0.055, "main"))
    check("14:30 전에는 작동 안 함", w is None)
    w, _ = pb2.evaluate_entry(close_bars(), ctx(14, 40, 0.01, "close"))
    check("당일 상승폭이 작으면 진입 안 함", w is None)
    w, _ = pb2.evaluate_entry(close_bars(), ctx(14, 40, 0.30, "close"))
    check("너무 오른 종목(추격)은 진입 안 함", w is None)
    bear = close_bars()
    last = bear[-1]
    bear[-1] = bar(64, last.close + 5, last.high, last.low, last.close - 4, last.volume)  # 시가 > 종가(음봉)
    w, _ = pb2.evaluate_entry(bear, ctx(14, 40, 0.055, "close"))
    check("음봉이면 진입 안 함", w is None)

    # ── 시간대 필터 ──
    cfg.strategy.entry_order = ["breakout", "open_gap", "close_squeeze"]
    pb3 = Playbook(cfg)
    def evaluated(window):
        _, vs = pb3.evaluate_entry(open_bars(), ctx(9, 12, 0.022, window))
        return sorted(v.technique for v in vs)
    check("장 초반에는 시간대 전용 기법(open_gap)만 평가", evaluated("open") == ["open_gap"], str(evaluated("open")))
    check("기존 매매 시간에는 breakout·open_gap·close_squeeze 모두 평가", evaluated("main") == ["breakout", "close_squeeze", "open_gap"], str(evaluated("main")))
    check("장 막판에는 close_squeeze 만 평가", evaluated("close") == ["close_squeeze"], str(evaluated("close")))

    # ── 설정 ──
    base = load_config(CONFIG_PATH)
    check("기본 진입 기법에는 넣지 않음(직접 켜야 작동)", "open_gap" not in base.strategy.entry_order and "close_squeeze" not in base.strategy.entry_order)
    fast = _with_style("fast")
    check("fast: 국내에만 두 기법이 켜짐(해외·코인 목록에는 안 들어감)",
          "open_gap" in fast.strategy.entry_order and "close_squeeze" in fast.strategy.entry_order
          and "open_gap" not in fast.crypto.entry_order and "close_squeeze" not in fast.crypto.entry_order)
    from daytrader.config import validate
    bad = load_config(CONFIG_PATH)
    bad.entry.close_window_end = "15:30"
    try:
        validate(bad)
        check("막판 시간대가 강제 청산 시각(15:10) 뒤면 거절", False)
    except ValueError:
        check("막판 시간대가 강제 청산 시각(15:10) 뒤면 거절", True)


def test_selection_runs_only_when_market_open() -> None:
    print("== ★ 종목 선정(live)은 매수 가능 시간(trading)과 별개로 08:00~21:00 에만 돈다 ==")
    from datetime import datetime
    from daytrader import session
    from daytrader.timeutil import KST

    cfg = load_config(CONFIG_PATH)

    def ph(y, m, d, hh, mm):
        return session.phase(cfg, now=datetime(y, m, d, hh, mm, tzinfo=KST), client=None)

    # 2026-09-04 는 금요일(평일).
    a = ph(2026, 9, 4, 3, 0)
    check("새벽 3시는 phase 는 그대로 pre 지만(표시용) 종목 선정은 돌지 않음(live=False)",
          a["phase"] == "pre" and not a["live"], str(a))
    b = ph(2026, 9, 4, 7, 59)
    check("07:59 도 아직 선정 시작 전(live=False)", not b["live"])
    c = ph(2026, 9, 4, 8, 0)
    check("08:00(선정 시작)부터 live=True(프리마켓 - 아직 매수는 안 됨)", c["live"] and not c["trading"], str(c))
    d = ph(2026, 9, 4, 9, 30)
    check("정규장 중엔 당연히 live 이고 매수도 가능", d["live"] and d["trading"])
    e = ph(2026, 9, 4, 20, 0)
    check("★ 20시(NXT 시간대, 예전엔 여기서 종목 선정이 멈췄음) - 이제도 live=True(매수는 안 됨)",
          e["live"] and not e["trading"], str(e))
    f = ph(2026, 9, 4, 21, 0)
    check("21:00(선정 종료 경계)까지는 live=True", f["live"])
    g = ph(2026, 9, 4, 21, 1)
    check("21:01 부터는 live=False(다음날 08:00 까지 필요 없음)", not g["live"])
    h = ph(2026, 9, 4, 23, 59)
    check("자정 직전도 live=False", not h["live"])

    # 2026-09-05 는 토요일(주말) - 시각과 무관하게 항상 live=False.
    sat = ph(2026, 9, 5, 12, 0)
    check("주말은 대낮이어도 live=False", not sat["live"], str(sat))

    # 공휴일 - 시각과 무관하게 항상 live=False(휴장일 캘린더로 판정).
    class HolidayClient:
        def market_calendar_kr(self):
            return {"open": False}

    session.reset_holiday_cache()
    hol = session.phase(cfg, now=datetime(2026, 9, 4, 12, 0, tzinfo=KST), client=HolidayClient())
    check("공휴일은 대낮이어도 live=False", not hol["live"] and hol["phase"] == "holiday", str(hol))
    session.reset_holiday_cache()


def _with_style(style):
    import yaml
    raw = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8"))
    # ★ "단타 매매 모드를 시장별로 분리해" 이후 style 은 top-level 이 아니라 시장별(risk/overseas/
    # crypto)로 갈라졌다 - 이 헬퍼는 "전부 이 속도로 맞춰서 비교해 본다"는 예전 의도를 그대로
    # 유지하려고 세 시장 모두에 같은 값을 넣는다(lab.py 의 style 오버라이드와 같은 방식).
    raw.setdefault("risk", {})["style"] = style
    raw.setdefault("overseas", {})["style"] = style
    raw.setdefault("crypto", {})["style"] = style
    path = os.path.join(tempfile.mkdtemp(), "c.yaml")
    open(path, "w", encoding="utf-8").write(yaml.safe_dump(raw, allow_unicode=True))
    return load_config(path)


def test_styles_and_budgets() -> None:
    print("== 매매 속도(style)·투자금액 초기값 ==")
    base = load_config(CONFIG_PATH)
    check("투자금액 초기값: 국내 1,000만원·해외 1만 달러·암호화폐 1,000만원",
          base.capital.allocation == 10_000_000 and base.overseas.budget_usd == 10_000 and base.crypto.budget == 10_000_000,
          f"{base.capital.allocation}/{base.overseas.budget_usd}/{base.crypto.budget}")
    check("기본 매매 속도는 normal(국내·해외·암호화폐 전부)",
          base.risk.style == "normal" and base.overseas.style == "normal" and base.crypto.style == "normal")
    fast, scalp = _with_style("fast"), _with_style("scalp")
    check("fast: 시세 확인 주기·재선정·재진입 금지가 짧아짐",
          fast.entry.poll_seconds <= 15 and fast.entry.rescreen_minutes <= 10 and fast.risk.cooldown_minutes <= 5
          and fast.crypto.poll_seconds <= 10 and fast.overseas.manage_seconds <= 15)
    check("fast: 일일 거래 한도가 넉넉해짐", fast.risk.daily_max_trades >= 30)
    check("fast: 진입 기법이 더 다양해짐", set(base.strategy.entry_order) < set(fast.strategy.entry_order) and "vwap_reclaim" in fast.crypto.entry_order)
    check("★ fast: 손절은 예전에 승률 저하가 확인됐던 1.8%보다는 넓은, 검증된 2.5%(국내·크립토 동일)로 보통보다 좁힘",
          fast.risk.stop_loss_pct == 0.025 < base.risk.stop_loss_pct
          and fast.crypto.stop_loss_pct == 0.025 < base.crypto.stop_loss_pct)
    check("★ fast: 익절도 보통보다 좁히되 scalp 보다는 넓음 - 빠른 단타(수십 분 보유)는 보통의 넓은 목표(며칠 보유 전제)"
          "에 현실적으로 안 닿아 전부 ATR 손절 등으로만 잘리는 문제가 실거래(모의매매)에서 확인돼 되돌림",
          scalp.risk.take_profit_pct < fast.risk.take_profit_pct < base.risk.take_profit_pct
          and scalp.crypto.take_profit_pct < fast.crypto.take_profit_pct < base.crypto.take_profit_pct)
    check("scalp 는 여전히 손절폭도 보통보다 좁음(fast 와 달리 scalp 자체가 실험용임을 이미 표시해 둠)",
          base.risk.stop_loss_pct > scalp.risk.stop_loss_pct)
    check("scalp: 작은 목표(익절 2.4%/손절 1.2%, 암호화폐 3%/1.5%)와 짧은 보유",
          scalp.risk.take_profit_pct == 0.024 and scalp.risk.stop_loss_pct == 0.012 and scalp.crypto.take_profit_pct == 0.03
          and scalp.exit.max_hold_minutes <= 30)
    check("normal 은 아무것도 바꾸지 않음(리스트도 원본 그대로)", load_config(CONFIG_PATH).strategy.entry_order == base.strategy.entry_order)
    try:
        _with_style("turbo")
        from daytrader.config import validate
        validate(_with_style("turbo"))
        check("알 수 없는 style 은 거절", False)
    except ValueError:
        check("알 수 없는 style 은 거절", True)
    from daytrader.config import validate
    bad = load_config(CONFIG_PATH)
    bad.sizing.min_mult = 1.5
    try:
        validate(bad)
        check("잘못된 분할 설정(min_mult>1)은 거절", False)
    except ValueError:
        check("잘못된 분할 설정(min_mult>1)은 거절", True)


def test_settings_defaults_fill() -> None:
    print("== 설정 화면: 옛 config 에 없는 새 항목은 기본값으로 채움 ==")
    import daytrader.server as S
    raw = S._fill_new_defaults({"mode": "sim", "overseas": {"enabled": True}, "crypto": {}})
    check("style 기본값(국내·해외·암호화폐 각자 채워짐)",
          raw["risk"]["style"] == "normal" and raw["overseas"]["style"] == "normal" and raw["crypto"]["style"] == "normal")
    check("분할 매매 항목이 채워짐", raw["sizing"]["max_adds"] == 3 and raw["sizing"]["scale_out"] is True)
    check("해외·암호화폐 투자금액 초기값", raw["overseas"]["budget_usd"] == 10000 and raw["crypto"]["budget"] == 10000000)
    check("이미 있는 값은 덮어쓰지 않음", raw["overseas"]["enabled"] is True)
    check("자리 비움 잠금 기본 30분", raw["ui"]["session_idle_minutes"] == 30)


def main() -> None:
    for t in (test_overseas_full_cycle, test_overseas_breakeven_and_loss, test_overseas_signal_sizing_off_and_persistence,
              test_overseas_strength_sizes_differently, test_crypto_full_cycle, test_crypto_legacy_budget, test_domestic_scaling, test_domestic_pyramiding, test_styles_and_budgets, test_time_of_day_windows_and_techniques, test_selection_runs_only_when_market_open, test_settings_defaults_fill):
        t()
    print(f"총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        for n in _failures:
            print("  -", n)
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
