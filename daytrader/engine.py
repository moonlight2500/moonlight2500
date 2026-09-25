"""엔진은 하루 동안의 포지션·손익·안전장치 상태를 들고 있다가, 시세를 읽고
Playbook 의 판정에 따라 진입·청산을 지시한다.
★ 상태는 실제로 바뀐 경우에만 디스크에 쓴다. 30초마다 무조건 쓰면 디스크만 닳는다.
★ 하루가 바뀌면 어제 상태를 그대로 들고 오지 않는다 - 안전장치가 어제 숫자로
계산되면 오늘 걸려야 할 제동이 걸리지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from daytrader import notify, session
from daytrader import news_guard, sizing
from daytrader.broker import LiveBroker, PaperBroker, Position
from daytrader.clock import make_clock
from daytrader.journal import Journal, explain_buy, explain_sell
from daytrader.ledger import Ledger
from daytrader.orders import OrderBook
from daytrader.playbook import Bar, Playbook
from daytrader.screener import Screener
from daytrader.ticks import breakeven_pct, round_trip_cost_pct, trade_pnl
from daytrader.timeutil import combine, day_str, hhmm, hhmmss, iso, minutes_between, monday_of, now_kst, parse_dt, won

DEAD_OR_FILLED_OCO_STATES = {"FILLED", "TRIGGERED"}
log = logging.getLogger(__name__)


class DailyState:
    """하루 동안 엔진이 들고 있는 상태.
    파일의 date 가 오늘과 다르면 무시하고 새로 시작한다 - 어제 숫자로 오늘의
    안전장치를 계산하면 안 되기 때문이다.
    """

    def __init__(self, path: str, today: str | None = None, mode: str = ""):
        self.path = path
        self.today = today or day_str(now_kst())
        self.mode = mode
        self.date = self.today
        self.realized_pnl = 0.0
        self.trades = 0
        self.halted = False
        self.halt_reason = ""
        self.positions: dict = {}
        self.cooldown: dict = {}
        self.closed: list = []
        self.consecutive_losses = 0
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            # ★ 깨졌으면 .corrupt-<ts> 로 백업하고 새로 시작한다. 조용히 지우면 원인을 알 수 없다.
            backup = f"{self.path}.corrupt-{int(time.time())}"
            try:
                os.replace(self.path, backup)
            except Exception:
                pass
            return

        stale_date = raw.get("date") != self.today
        # ★★★ 실제로 겪을 뻔한 버그 - "오버나이트·장기 보유 허용"(cfg.exit.allow_overnight)이
        # 켜져 있으면 포지션이 날짜를 넘겨 남아 있는 게 정상인데, 예전 코드는 저장된 날짜가
        # 오늘과 다르면 파일 전체(포지션 포함)를 통째로 버렸다 - 재시작한 순간 실제로는 계좌에
        # 있는 종목을 "산 적 없다"고 잊어버려서, 이후 그 종목을 팔 때 NotOwnedError 로 거부될
        # 뻔했다(이미 강제청산이 실패해 넘어온 경우에도 같은 문제가 있었다 - 두 경우 모두 실제
        # 보유는 그대로인데 내부 장부만 잊는다). 날짜가 달라도 포지션·쿨다운은 그대로 이어받고,
        # 그날 한정 통계(실현손익·거래수·중단 여부·연속손절)만 오늘 것으로 새로 시작한다.
        if stale_date:
            self.realized_pnl, self.trades = 0.0, 0
            self.halted, self.halt_reason = False, ""
            self.closed, self.consecutive_losses = [], 0
        else:
            self.mode = raw.get("mode", self.mode)
            self.realized_pnl = raw.get("realized_pnl", 0.0)
            self.trades = raw.get("trades", 0)
            self.halted = raw.get("halted", False)
            self.halt_reason = raw.get("halt_reason", "")
            self.closed = raw.get("closed", [])
            self.consecutive_losses = raw.get("consecutive_losses", 0)
        # ★★★ 실제로 겪은 크래시 - Position.entry_time 은 KST-aware
        # datetime 이어야 하는데(broker.py 의 iso()/to_kst() 가 .tzinfo 를
        # 읽는다), 상태 파일에는 이미 문자열(iso 직렬화 결과)로 저장돼
        # 있다. 그 문자열을 그대로 Position(**p) 에 넣으면, 재시작 직후
        # resume() 이 상태를 다시 저장하려는 순간 "'str' object has no
        # attribute 'tzinfo'" 로 죽어 보유 포지션이 있으면 국내 자동매매를
        # 아예 시작할 수 없었다. 불러올 때 반드시 datetime 으로 되돌린다.
        self.positions = {}
        for s, p in raw.get("positions", {}).items():
            p = dict(p)
            p["entry_time"] = parse_dt(p.get("entry_time")) or now_kst()
            self.positions[s] = Position(**p)
        self.cooldown = raw.get("cooldown", {})

    def to_dict(self) -> dict:
        return {
            "date": self.date, "mode": self.mode, "realized_pnl": self.realized_pnl,
            "trades": self.trades, "halted": self.halted, "halt_reason": self.halt_reason,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "cooldown": self.cooldown, "closed": self.closed,
            "consecutive_losses": self.consecutive_losses,
        }

    def save(self) -> None:
        """.tmp 에 쓰고 os.replace 로 원자적으로 교체한다.
        ★★ 윈도우에서 백신이 파일을 훑는 중이면 os.replace 가 WinError 5 로 실패한다.
        여기서 죽으면 포지션 상태를 잃으므로 5회 짧게 재시도한다(0.1초씩 늘려가며).
        """
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)

        delay = 0.1
        for attempt in range(5):
            try:
                os.replace(tmp_path, self.path)
                return
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(delay)
                delay += 0.1


class Engine:
    def __init__(self, cfg, client, event_bus=None):
        self.cfg = cfg
        self.client = client
        self.event_bus = event_bus

        # ★ 웹 스레드가 snapshot() 을 부르는 동안 엔진 스레드가 딕셔너리를 바꾸면
        # 순회 중 변경 오류가 난다.
        self.lock = threading.RLock()

        # 시계: sim/replay 면 가짜 시장(client)이 들고 있는 시계를 공유한다 -
        # 엔진과 가짜 시장이 서로 다른 시각을 보면 미래 데이터가 새어 나갈 수 있다.
        if cfg.mode in ("sim", "replay"):
            self.clock = getattr(client, "clock", None) or make_clock(cfg)
        else:
            self.clock = make_clock(cfg)

        prefix = {"sim": "sim-", "replay": "replay-"}.get(cfg.mode, "")
        self._prefix = prefix

        self.playbook = Playbook(cfg, market="domestic", learning_mode=cfg.risk.technique_learning_mode)
        self.journal = Journal(cfg.state_dir, mode=cfg.mode, clock=self.clock)
        self.ledger = Ledger(cfg.state_dir)
        self.order_book = OrderBook(cfg.state_dir)
        self.screener = Screener(client, cfg, journal=self.journal)
        # ★ 싱글턴 금지(A-18) - 엔진마다 자기 인스턴스를 갖는다.
        self.notifier = notify.Telegram(cfg)

        self.report = None
        self.current_session: dict = {}  # ★ 지금 장 국면 - 화면이 "왜 매매를 안 하는지" 보여주는 데 쓴다.
        self.candidates: list = []
        self._allow_add = False  # 추가 매수(피라미딩)는 신규 진입이 가능한 시간·상태에서만
        self._window = "main"  # 지금 매수 가능한 시간대: open(장 초반)·main·close(장 막판)
        self.candidate_status: dict = {}
        self.candidate_verdicts: dict = {}
        self._last_verdicts: dict = {}
        self._orb_done: dict = {}

        self.pnl_curve: list = []
        self.equity_curve: list = []
        # ★ 종목별 손익 곡선 - {symbol: {"name": str, "points": [[시각, 손익], ...]}}.
        #   pnl_curve(총손익)와 같은 분 단위로 찍는다.
        self.symbol_curves: dict = {}
        self._curve_limit = 720

        self.degraded = False
        self.degraded_since = None
        self._api_fail_streak = 0
        self.reconcile_alerts: list = []

        self._weekly_pnl_cache: float | None = None
        self._weekly_pnl_cache_at = 0.0

        self._last_screen = None
        self._opened_today = False
        self._session_date = day_str(self.clock.now())
        self._last_phase = None
        self._stop = False
        self._close_all = False
        self._force_close_deadline = None
        self._loop_count = 0
        self.last_stop_reason: str | None = None  # ★ 정상 종료 사유(예: 오늘은 더 할 매매가 없음)를 화면에 보여주기 위해.
        self.ended_at = None

        state_path = os.path.join(cfg.state_dir, f"{prefix}daily_state.json")
        self.state = DailyState(state_path, today=day_str(self.clock.now()), mode=cfg.mode)

        if cfg.is_live:
            self.broker = LiveBroker(cfg, client, self.order_book)
            self.allocation = cfg.capital.allocation or self.broker.cash()
        else:
            self.allocation = cfg.capital.allocation or 3_000_000
            # ★★★ 실제로 겪은 버그 - 재시작할 때마다 모의투자 현금이 오늘 손익과
            # 무관하게 매번 배정액 그대로 초기화됐다("손절만 계속 나는데 현금은
            # 왜 자꾸 다시 올라가느냐"는 문의의 원인). daily_state.json 에서 보유
            # 종목은 복원하면서(register_holding, 아래) 그 종목을 사는 데 쓴 현금은
            # 되돌려주지 않았던 것 - 포지션과 그 값어치만큼의 현금이 동시에 존재하는
            # 이중 계산이었다. 오늘 실현손익을 더하고, 지금 보유 중인 포지션에
            # 묶여 있는 돈(매수 금액 - 이미 분할 매도로 받은 돈)을 빼서 복원한다.
            locked = sum(max(p.invested - p.sold_value, 0.0) for p in self.state.positions.values())
            starting_cash = self.allocation + self.state.realized_pnl - locked
            self.broker = PaperBroker(cfg, client, starting_cash=starting_cash)

        # ★★★ 실제로 겪은 크래시 - 브로커의 "이 브로커가 직접 산 수량"
        # 장부(_bought_qty)는 이 Engine 인스턴스와 함께 매번 새로 만들어져
        # 텅 빈 채로 시작한다. 재시작 전에 이미 사서 daily_state.json 에
        # 남아 있는 포지션은, 분명히 이 프로그램이 직접 산 것인데도 새
        # 브로커 입장에서는 "산 적 없는 종목"이 되어, 청산하려는 순간
        # NotOwnedError 로 거부되며 엔진이 통째로 멈췄다(보유 종목이 있는
        # 채로 재시작하면 항상 재현). 상태를 불러온 직후 그 수량만큼
        # 등록해 브로커 장부와 실제 상태를 일치시킨다.
        for symbol, pos in self.state.positions.items():
            self.broker.register_holding(symbol, pos.quantity)

        # 시그널은 메인 스레드일 때만 등록한다 - signal.signal() 은 메인 스레드가
        # 아니면 예외를 던진다 (웹서버 백그라운드 스레드에서 엔진을 돌리는 경우가 있다).
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, self._handle_signal)
                signal.signal(signal.SIGTERM, self._handle_signal)
            except (ValueError, OSError):
                pass

    def _handle_signal(self, signum, frame) -> None:
        """★ halted 플래그만 세우면 안 된다. halted 는 신규 진입만 막을 뿐
        run() 의 메인 루프 자체는 계속 돈다 - 프로세스가 SIGTERM/SIGINT 를
        받고도 종료되지 않는 상태가 된다(실제로 겪은 문제). request_stop() 을
        반드시 함께 불러 루프를 멈춰야 한다.
        """
        self.journal.write("halt", f"신호({signum})를 받아 매매를 멈춥니다.")
        self.request_stop(close_positions=False)

    # ━━ 매매 규모 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @property
    def position_cap(self) -> float:
        """종목 하나에 넣을 수 있는 최대 금액 = 국내 총 투자금액 / 동시 보유 종목 수."""
        return sizing.position_cap(self.allocation, self.cfg.capital.max_positions)

    def _entry_amount(self, strength: float = 0.5, conviction=None, vol=None) -> float:
        """첫 매수 금액 = 종목당 한도 × 첫 매수 비율 × (신호 강도+테마 근거 크기) 배수 × 변동성 배수. 연속 손절 중이면 축소 비율을 곱한다."""
        amount = sizing.entry_amount(self.position_cap, strength, self.cfg.sizing, conviction=conviction, vol=vol)
        if self.size_reduced:
            # ★ 잃고 있을 때 크기를 키우는 건 물타기이고, 계좌가 가장 빨리 망가지는 길이다.
            amount *= self.cfg.risk.reduced_size_pct
        return amount

    @property
    def per_trade_amount(self) -> float:
        """중간 강도 신호의 첫 매수 금액(화면 안내·테스트용)."""
        return self._entry_amount(0.5)

    @property
    def size_reduced(self) -> bool:
        """연속 손절이 max_consecutive_losses 회 이상 이어진 동안만 매매 규모를 줄인다(이익이 나면 원래대로).
        예전에는 오늘 실현손익이 조금이라도 마이너스면 무조건 줄어서, 화면 문구('연속 손실')와 실제 조건이 달랐다."""
        r = self.cfg.risk
        return bool(r.reduce_after_loss and self.state.consecutive_losses >= r.max_consecutive_losses)

    # ━━ API 저하 모드 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _api_ok(self) -> None:
        self._api_fail_streak = 0
        if self.degraded:
            self.degraded = False
            self.degraded_since = None
            self.journal.write("session", "API 연결이 회복되어 저하 모드를 해제합니다.")

    def _api_fail(self, exc) -> None:
        self._api_fail_streak += 1
        self.journal.write("halt", f"시세 조회 실패({self._api_fail_streak}회): {exc}")

        if self._api_fail_streak >= self.cfg.live.degrade_after_failures and not self.degraded:
            self.degraded = True
            self.degraded_since = self.clock.now()
            # ★ 시세를 못 보는 상태에서 사는 것은 눈 감고 사는 것이다.
            self.journal.write("halt", "시세 조회가 반복 실패해 저하 모드로 전환합니다. 신규 진입을 멈춥니다.")

        if self.degraded and self.degraded_since is not None:
            elapsed_min = minutes_between(self.degraded_since, self.clock.now())
            if elapsed_min >= self.cfg.live.degrade_halt_minutes:
                self._halt("저하 모드가 지속되어 매매를 완전히 멈춥니다.")

    # ━━ 안전장치 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _halt(self, reason: str, detail: dict | None = None) -> None:
        if self.state.halted:
            return
        self.state.halted = True
        self.state.halt_reason = reason
        self.journal.write("halt", reason, detail=detail)
        self.state.save()

    def _weekly_pnl(self) -> float:
        """★15초 캐시. 거래가 나면(청산 시) 무효화한다."""
        now = time.monotonic()
        if self._weekly_pnl_cache is not None and now - self._weekly_pnl_cache_at < 15:
            return self._weekly_pnl_cache
        monday = monday_of(self.clock.now())
        rows = self.ledger.trades(modes=[self.cfg.mode])
        total = sum(r["pnl"] for r in rows if r["date"] >= monday)
        self._weekly_pnl_cache = total
        self._weekly_pnl_cache_at = now
        return total

    def _check_kill_switch(self) -> bool:
        """4단 방어. 하나라도 걸리면 신규 진입을 멈추고 일지에 남긴다."""
        r = self.cfg.risk
        allocation = self.allocation

        weekly_pnl = self._weekly_pnl()
        if allocation and -weekly_pnl / allocation >= r.weekly_loss_limit_pct:
            self._halt(
                f"주간 손실 한도({r.weekly_loss_limit_pct*100:.0f}%)에 도달했습니다. "
                "이번 주는 더 하지 않습니다. 손실은 이미 계좌에 반영되어 있습니다 - "
                "주말에 회고하고 다시 시작하세요."
            )
            return False

        if allocation and -self.state.realized_pnl / allocation >= r.daily_loss_limit_pct:
            self._halt(
                f"일일 손실 한도({r.daily_loss_limit_pct*100:.0f}%)에 도달했습니다. "
                "손실이 난 날 더 하려는 충동을 막는 것이 이 한도의 목적입니다."
            )
            return False

        if self.state.consecutive_losses >= r.max_consecutive_losses and not r.reduce_after_loss:
            # ★★★ 설정한 쿨다운이 지났거나 세션(프리장·본장·NXT장)이 바뀌었으면
            # 카운트를 풀고 매매를 재개한다(해외주식 overseas_engine.halt_info()와
            # 같은 원칙 - "연속 손절후 다음 진입시기가 너무 늦어. 모든 장에서 3시간
            # 또는 세션이 바뀌면 진입 가능하게 변경해" 요청). 예전엔 날짜가 바뀌어야만
            # 리셋되거나(과거) 설정 시간 경과만 봤어서, 세션이 바뀐 뒤에도 다음 세션
            # 기회까지 계속 막혀 있는 경우가 있었다.
            if self._loss_halt_expired():
                self.state.consecutive_losses = 0
                self.journal.write(
                    "session",
                    f"연속 손절 중단이 설정한 {getattr(self.cfg.risk, 'loss_halt_cooldown_hours', 3):g}시간이 "
                    "지났거나 세션이 바뀌어 해제됐습니다. 신규 진입을 재개합니다.",
                )
            else:
                # ★★★ "언제 매매가 재개되는지 시간을 표기해달라"는 요청 -
                # 마지막 손절 시각 + 설정한 쿨다운이 재개 시각이다. 세션이 먼저
                # 바뀌면 이 시각 전에도 즉시 재개된다(_loss_halt_expired 참고).
                resume_txt = self._loss_halt_resume_text()
                self._halt(
                    f"연속 손절 {self.state.consecutive_losses}회입니다. 지금은 시장과 안 맞습니다. "
                    "여기서 더 하면 손실을 만회하려는 매매가 되기 쉬워 멈춥니다."
                    + (f" · 재개 예정 {resume_txt}(또는 세션이 바뀌면 즉시)" if resume_txt else "")
                )
                return False

        if self.state.trades >= r.daily_max_trades:
            self._halt(
                f"오늘 매매 {self.state.trades}건으로 일일 최대 거래에 도달했습니다. "
                "과매매는 비용으로 계좌를 갉아먹습니다."
            )
            return False

        return True

    def _recent_bars(self, symbol: str, count: int = 60) -> list:
        try:
            rows = self.client.candles(symbol, "1m", count)
            self._api_ok()
        except Exception as exc:
            self._api_fail(exc)
            return []
        return [Bar.from_api(r) for r in rows]

    def _now(self):
        return self.clock.now()

    def _emit(self, event: str, data) -> None:
        if self.event_bus is not None:
            try:
                self.event_bus.publish(event, data)
            except Exception:
                pass

    def _notify_trade(self, kind: str, t: dict) -> None:
        """★ 예외를 전부 삼킨다. 알림이 실패해도 매매는 계속돼야 한다."""
        try:
            self.notifier.trade(kind, t)
        except Exception:
            pass

    def _entry_bars(self, symbol: str, count: int = 60):
        """★ 진입 판정에서 봉을 읽는 모든 곳은 반드시 이걸 거친다.
        진행 중인 봉은 거래량이 부분값이라 '직전 평균의 2배' 판정이 왜곡되고
        종가도 확정이 아니다. entry.use_closed_bars_only(기본 true)로 끌 수 있다.
        """
        bars = self._recent_bars(symbol, count)
        if not bars or not self.cfg.entry.use_closed_bars_only:
            return bars, None
        cur_min = hhmmss(self._now())[:5]
        if str(bars[-1].ts)[11:16] == cur_min:
            return bars[:-1], bars[-1]
        return bars, None

    # ━━ 청산 (STAGE 12b) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def manage_positions(self, force_close: bool = False) -> None:
        with self.lock:
            if not self.state.positions:
                return
            symbols = list(self.state.positions.keys())

        try:
            price_rows = self.client.prices(symbols)
            self._api_ok()
        except Exception as exc:
            self._api_fail(exc)
            return

        # ★★★ 실제로 겪은 크래시 - 토스 API 가 가격을 문자열로 줄 때가
        # 있는데(예: "31500"), 그대로 넣으면 조금 뒤 "last > pos.peak_price"
        # 에서 str 과 int/float 를 비교하다 TypeError 로 죽는다. 이 예외는
        # manage_positions() 호출부(run() 메인 루프)에서 잡지 않아 엔진
        # 전체가 멈췄다. 숫자로 못 바꾸는 값은 아예 버려 시세 없음과
        # 같이 취급한다(잘못된 값으로 청산 판단을 하는 것보다 안전하다).
        prices = {}
        for r in price_rows:
            s = str(r.get("symbol", "")).zfill(6)
            p = r.get("price") or r.get("lastPrice")
            if not s or not p:
                continue
            try:
                prices[s] = float(p)
            except (TypeError, ValueError):
                continue

        if self.cfg.is_live and self.cfg.exit.use_conditional_oco:
            self._poll_server_oco(prices)

        self._track_equity()

        peak_changed = False
        with self.lock:
            items = list(self.state.positions.items())

        for symbol, pos in items:
            last = prices.get(symbol)
            if last is None:
                if force_close:
                    # ★★★ "전량 청산 후 정지"는 사용자가 명시적으로 "지금
                    # 당장 다 정리해라"라고 한 긴급 조치다 - 실시간 시세
                    # 조회가 실패했다고 그냥 포기하면 포지션이 영원히 안
                    # 팔리는 채로 남는다(실제로 겪은 버그: 정지는 되는데
                    # 전량 청산은 조용히 실패). 마지막으로 알려진 가격
                    # (고점 또는 진입가)으로라도 청산을 시도한다.
                    last = pos.peak_price or pos.entry_price
                    log.warning(
                        "%s 실시간 시세를 받지 못해 마지막 가격(%.0f원)으로 강제 청산합니다.",
                        symbol, last,
                    )
                else:
                    continue

            pos.last_price = last  # ★ 화면의 현재가·평가손익은 고점이 아니라 이 값을 쓴다.
            if last > pos.peak_price:
                pos.peak_price = last
                peak_changed = True

            self._track(pos, last)

            sz = self.cfg.sizing
            ctx = SimpleNamespace(
                held_minutes=pos.held_minutes(self.clock.now()),
                force_close=force_close, now=self.clock.now(),
                prev_verdict=self._last_verdicts.get(symbol), cfg=self.cfg,
                scale_out=bool(sz.scale_out),  # 분할 매도를 쓰면 고정 익절(전량)은 끄고 나눠서 판다.
            )
            bars = self._recent_bars(symbol)
            verdict = self.playbook.evaluate_exit(pos, bars, last, ctx)
            if verdict is not None:
                self._last_verdicts[symbol] = verdict
            if verdict is not None and verdict.ok:
                self._close_position(pos, verdict, last)  # 손절·추적·시간·강제 청산은 전량
                continue
            if force_close:
                continue
            # 분할 매도: 익절폭의 절반에서 1차, 익절폭에서 2차(나머지는 추적 손절이 끌고 감)
            vol_now = sizing.vol_ratio(bars, self.cfg.risk.stop_loss_pct)
            take_pct = (sizing.dynamic_take_profit_pct(self.cfg.risk.take_profit_pct, bars, sz)
                        if self.cfg.risk.dynamic_take_profit else self.cfg.risk.take_profit_pct)
            if self.cfg.risk.technique_learning_mode == "entry_exit_pref":
                from daytrader import exit_efficiency
                widen, _why = exit_efficiency.widen_multiplier(self.cfg, "domestic", symbol)
                take_pct *= widen
            step = sizing.exit_step(scaled_out=pos.scaled_out, entry=pos.entry_price, price=last, sz=sz,
                                    take_pct=take_pct, conviction=pos.conviction, vol=vol_now)
            if step is not None:
                frac, tag = step
                label = "1차 분할 매도(익절폭의 절반)" if tag == "scale_out_1" else "2차 분할 매도(익절폭 도달)"
                self._close_position(pos, SimpleNamespace(
                    technique=tag, headline=label, id=None,
                    narrative=f"{label} - 수익률 {(last / pos.entry_price - 1) * 100:+.2f}%에서 보유량의 {frac * 100:.0f}%를 팔고 나머지는 이익을 더 키우도록 남깁니다.",
                ), last, fraction=frac)
                continue
            # 1차 매도 뒤 본전 아래로 내려오면 나머지를 정리 - 챙긴 이익을 손실로 돌리지 않는다
            if sizing.breakeven_hit(scaled_out=pos.scaled_out, entry=pos.entry_price, price=last, sz=sz):
                self._close_position(pos, SimpleNamespace(
                    technique="breakeven_stop", headline="본전 방어 청산", id=None,
                    narrative="1차 분할 매도로 이익을 챙긴 뒤 가격이 평균 매수가(본전) 아래로 내려와 나머지를 정리합니다.",
                ), last)
                continue
            if self._allow_add:
                self._maybe_add(pos, last, bars)

        # ★ 고점이 갱신됐을 때만 저장한다. 30초마다 무조건 쓰면 디스크만 닳는다.
        if peak_changed:
            self.state.save()

    def _poll_server_oco(self, prices: dict) -> None:
        """live + use_conditional_oco 일 때만 부른다. conditional_orders() 한 번으로
        전체 조건부 주문을 받아 색인한다.
        ★ 이걸 안 하면 실제로는 팔렸는데 프로그램은 보유 중으로 알고, 계속 청산을
        시도하다 실패하며 포지션이 영원히 남는다.
        """
        try:
            rows = self.client.conditional_orders(status=None)
        except Exception:
            return
        rows = rows if isinstance(rows, list) else rows.get("orders", [])
        by_id = {r.get("conditionalOrderId") or r.get("id"): r for r in rows}

        with self.lock:
            items = list(self.state.positions.items())

        for symbol, pos in items:
            if not pos.oco_id:
                continue
            row = by_id.get(pos.oco_id)
            if row is None:
                continue
            if row.get("status", "") in DEAD_OR_FILLED_OCO_STATES:
                last = prices.get(symbol, pos.entry_price)
                # 손절이면 서버 OCO 가 대신 처리한다 - 여기서는 매도 주문을 내지 않는다.
                self._close_position(pos, None, last, server_filled=row)

    def _close_position(self, pos, verdict, last_price, *, server_filled=None, fraction: float = 1.0) -> None:
        """포지션을 정리한다.
        ★ oco_id 가 있으면 먼저 취소한다. 취소가 실패하고 oco_is_open() 이 True 면
        매도하지 않고 그냥 돌아간다 (이중 매도 방지).
        """
        symbol = pos.symbol

        if server_filled is None and pos.oco_id:
            cancelled = self.broker.cancel_oco(pos.oco_id)
            if not cancelled and self.broker.oco_is_open(pos.oco_id):
                return  # 서버에 주문이 살아있다 - 여기서 또 팔면 이중 매도가 된다.

        if server_filled is not None:
            exit_price = server_filled.get("averageFilledPrice") or server_filled.get("price") or last_price
            qty = server_filled.get("filledQuantity") or pos.quantity
            estimated = False
            headline = "서버 OCO 체결"
            technique = "fixed"
            narrative = "증권사 서버에 걸어둔 손절/익절 주문이 먼저 체결되었습니다."
            verdict_id = None
        else:
            urgent = verdict.technique in ("time_stop", "momentum_fade", "force_close", "breakeven_stop")
            sell_qty = pos.quantity if fraction >= 1.0 else int(sizing.split_quantity(pos.quantity, fraction, integer=True))
            fill = self.broker.sell(
                symbol, sell_qty, last_price, urgent=urgent,
                reason=verdict.headline, verdict_id=verdict.id,
            )
            if not fill.ok:
                self.journal.write("halt", f"{symbol} 청산 실패: {fill.reason}", symbol=symbol, name=pos.name)
                if fill.fatal:
                    self._halt(f"{symbol} 청산에 실패해 매매를 멈춥니다: {fill.reason}")
                return
            exit_price, qty, estimated = fill.price, fill.quantity, fill.price_estimated
            headline, technique, narrative, verdict_id = verdict.headline, verdict.technique, verdict.narrative, verdict.id

        pnl = won(trade_pnl(pos.entry_price, exit_price, qty, self.cfg.costs.commission_pct, self.cfg.costs.tax_pct))
        held = pos.held_minutes(self.clock.now())

        held_qty = pos.quantity
        partial = qty < held_qty  # 나눠 파는 중(일부만 팔았다)
        total_pnl = pnl + (0 if partial else (pos.realized or 0))  # 거래 전체 손익 = 이번 몫 + 앞서 나눠 판 몫
        with self.lock:
            if not partial:
                # 나눠 팔던 거래가 끝났을 때 전체 손익으로 한 번만 센다(일일 거래 수·연속 손절).
                self.state.consecutive_losses = 0 if total_pnl >= 0 else self.state.consecutive_losses + 1
                self.state.trades += 1

            trade = {
                "date": day_str(self.clock.now()), "symbol": symbol, "name": pos.name, "theme": pos.theme,
                "qty": qty, "entry": pos.entry_price, "exit": exit_price, "pnl": pnl,
                "reason": headline, "entry_time": iso(pos.entry_time), "exit_time": iso(self.clock.now()),
                # ★★★ "거래내역의 기법이 매도 기법인 것 같다 - 매수 기법도
                # 추가해달라"는 지적. 맞다 - technique 는 청산 판정
                # (verdict.technique)이라 매도 기법이다. 어떤 기법으로 샀는지는
                # Position 에 이미 담겨 있는데 거래 기록에 옮기지 않아
                # 화면에서 볼 수 없었다. 둘 다 남긴다.
                "technique": technique,          # 매도(청산) 기법 - 기존 필드명 유지(하위호환).
                "entry_technique": pos.technique,  # 매수(진입) 기법.
                "verdict_id": verdict_id, "estimated": estimated,
            }
            if not partial:
                # 나눠 팔았다면 마지막에 한 건으로 합쳐 기록한다(건수·승률이 부풀지 않게): 총 수량, 평균 청산가, 전체 손익.
                if pos.scaled_out:
                    total_qty = int((pos.sold_qty or 0) + qty)
                    trade["qty"] = total_qty
                    trade["exit"] = ((pos.sold_value or 0) + qty * exit_price) / total_qty if total_qty else exit_price
                    trade["pnl"] = won(total_pnl)
                    trade["reason"] = f"{headline} (분할 매도 {pos.scaled_out}회 후)"
                if pos.adds or pos.scaled_out:
                    trade["adds"], trade["scaled_out"] = pos.adds, pos.scaled_out
                self.state.closed.append(trade)
                if self.cfg.risk.technique_learning_mode == "entry_exit_pref" and pos.entry_price:
                    from daytrader import exit_efficiency
                    mfe_pct = (pos.peak_price or pos.entry_price) / pos.entry_price - 1
                    realized_pct = trade["exit"] / pos.entry_price - 1
                    exit_efficiency.record(self.cfg, "domestic", symbol, realized_pct, mfe_pct)
            self.state.realized_pnl += pnl
            self._weekly_pnl_cache = None  # 거래가 났으니 주간 손익 캐시를 무효화한다.

            remaining = pos.quantity - qty
            if remaining > 0:
                pos.invested = (pos.invested or 0) * (remaining / pos.quantity) if pos.quantity else 0
                pos.quantity = remaining  # 부분 청산이면 남은 수량으로 포지션을 유지한다.
                if verdict is not None and getattr(verdict, "technique", "") in ("scale_out_1", "scale_out_2"):
                    pos.scaled_out += 1
                pos.realized = (pos.realized or 0) + pnl
                pos.sold_qty = (pos.sold_qty or 0) + qty
                pos.sold_value = (pos.sold_value or 0) + qty * exit_price
            else:
                self.state.positions.pop(symbol, None)

            if not partial:
                cooldown_until = self.clock.now() + timedelta(minutes=self.cfg.risk.cooldown_minutes)
                self.state.cooldown[symbol] = iso(cooldown_until)

        if not partial:
            self.ledger.append_trade(self.cfg.mode, trade)
        balance = self.allocation + self.state.realized_pnl
        self.ledger.record_equity(
            self.cfg.mode, day_str(self.clock.now()), self.allocation,
            self.state.realized_pnl, len(self.state.closed), balance,
        )

        if partial and self.cfg.exit.use_conditional_oco and server_filled is None:
            try:
                pos.oco_id = self.broker.place_oco(pos, self._oco_cfg())
            except Exception:
                pos.oco_id = None  # 프로그램 내부 손절은 계속 작동한다
        explain = explain_sell(pos, exit_price, verdict, pnl, held)
        self.journal.write("sell", explain, symbol=symbol, name=pos.name, theme=pos.theme, detail=trade)

        self._notify_trade("exit", trade)

        self.state.save()
        self._check_kill_switch()

    def _maybe_add(self, pos, price: float, bars) -> None:
        """이익 중인 종목에만 추가 매수(피라미딩). 조건: ① 마지막 매수가·평균가보다 (손절폭의 절반) 이상 오름 ② 진입 신호가
        아직 살아 있음(추세 지속 확인) ③ 종목당 한도 안 ④ 장 마감 30분 전이 아님."""
        sz = self.cfg.sizing
        symbol = pos.symbol
        if not sizing.add_due(adds=pos.adds, last_fill=pos.last_fill_price or pos.entry_price, avg_entry=pos.entry_price,
                              price=price, scaled_out=pos.scaled_out, sz=sz, stop_pct=self.cfg.risk.stop_loss_pct):
            return
        minutes_left = minutes_between(self.clock.now(), self._force_close_dt())
        if minutes_left < 30:
            return
        cand = next((c for c in self.candidates if c.symbol == symbol), None)
        entry_bars, _partial = self._entry_bars(symbol, max(80, self.cfg.entry.breakout_lookback * 4))
        if not entry_bars:
            return
        ctx = SimpleNamespace(
            symbol=symbol, name=pos.name, theme=pos.theme, upper_limit=None, theme_bars=None, now=self.clock.now(),
            prev_verdict=None, prev_verdicts=[], change_rate=(cand.change_rate if cand else 0.0),
            theme_rank=(cand.theme_rank if cand else 0), theme_breadth=(cand.theme_breadth if cand else 0),
            theme_intensity=(cand.theme_intensity if cand else 0.0), orb_done=True, minutes_to_close=minutes_left,
            window=self._window, kr_session=True,
        )
        try:
            winner, _ = self.playbook.evaluate_entry(entry_bars, ctx)
        except Exception:
            return
        if winner is None:
            return
        amount = sizing.add_amount(self.position_cap, pos.invested or pos.entry_price * pos.quantity, pos.adds, sz,
                                   conviction=pos.conviction, vol=sizing.vol_ratio(entry_bars, self.cfg.risk.stop_loss_pct))
        if self.size_reduced:
            amount *= self.cfg.risk.reduced_size_pct
        qty = int(amount // price)
        if self.cfg.is_live:
            try:
                qty = min(qty, int(self.broker.cash() // price))
            except Exception:
                pass
        if qty < 1 or price * qty < self.cfg.risk.min_order_amount:
            return
        if pos.oco_id:
            if not self.broker.cancel_oco(pos.oco_id) and self.broker.oco_is_open(pos.oco_id):
                return  # 서버 주문이 살아 있다 - 이 상태로 수량을 바꾸면 안 된다
            pos.oco_id = None
        fill = self.broker.buy(symbol, qty, price, reason="추가 매수", verdict_id=winner.id)
        if not fill.ok:
            if self.cfg.exit.use_conditional_oco:
                pos.oco_id = self.broker.place_oco(pos, self._oco_cfg())  # 취소했던 서버 손절을 원래대로 복구
            return
        total = pos.quantity + fill.quantity
        pos.entry_price = (pos.quantity * pos.entry_price + fill.quantity * fill.price) / total
        pos.quantity = total
        pos.adds += 1
        pos.last_fill_price = fill.price
        pos.invested = (pos.invested or 0) + fill.price * fill.quantity
        if self.cfg.exit.use_conditional_oco:
            pos.oco_id = self.broker.place_oco(pos, self._oco_cfg())
        self.state.save()
        explain = (f"{pos.name} 추가 매수 {pos.adds}/{sz.max_adds}회: {fill.price:,.0f}원에 {fill.quantity}주. "
                   f"이익 중({(price / pos.entry_price - 1) * 100:+.2f}%)이고 진입 신호({winner.technique_label})가 유지돼 더 실었습니다. "
                   f"평균 매수가 {pos.entry_price:,.0f}원, 총 {pos.quantity}주.")
        self.journal.write("buy", explain, symbol=symbol, name=pos.name, theme=pos.theme,
                           detail={"verdict": winner.to_dict(), "add": pos.adds})

    def _track(self, pos, last) -> None:
        """포지션 가격 궤적을 기록한다.
        ★ 같은 분(HH:MM)에 여러 점이 오면 마지막 값으로 갱신한다 - 안 그러면
        그래프가 지저분해진다.
        """
        if not hasattr(pos, "_track"):
            pos._track = {}
        minute = hhmm(self.clock.now().time())
        pos._track[minute] = last

    def _unrealized_pnl(self) -> float:
        """★★★ "매수했던 종목은 모두 보여줘" - 손익 곡선이 실현손익만
        그려서, 아직 안 판 보유 종목은 그래프에 전혀 안 나왔다. 지금
        팔면 얼마인지를 보려면 평가손익도 더해야 한다.
        ★ 실시간 현재가가 없으면 고점(peak_price)으로 근사한다 - 정확한
        값은 아니지만 흐름을 보는 데는 충분하다.
        """
        total = 0.0
        for pos in self.state.positions.values():
            now = getattr(pos, "last_price", None) or pos.peak_price or pos.entry_price
            total += (now - pos.entry_price) * pos.quantity
        return total

    def _track_equity(self) -> None:
        """자산·손익 곡선에 현재 시각 점을 찍는다. 같은 분이면 마지막 값으로 덮어쓰고,
        최대 720점까지만 보관한다 (그 이상은 화면에 그릴 필요가 없다).
        """
        minute = hhmm(self.clock.now().time())
        # ★ 실현 + 미실현 = 지금 청산하면 확정될 손익. 보유 종목이 그래프에
        #   빠지면 "샀는데 아무것도 안 보인다"가 된다.
        unrealized = self._unrealized_pnl()
        total_pnl = self.state.realized_pnl + unrealized
        balance = self.allocation + total_pnl

        if self.equity_curve and self.equity_curve[-1]["time"] == minute:
            self.equity_curve[-1]["balance"] = balance
        else:
            self.equity_curve.append({"time": minute, "balance": balance})
            if len(self.equity_curve) > self._curve_limit:
                self.equity_curve = self.equity_curve[-self._curve_limit:]

        if self.pnl_curve and self.pnl_curve[-1]["time"] == minute:
            self.pnl_curve[-1]["pnl"] = total_pnl
            self.pnl_curve[-1]["realized"] = self.state.realized_pnl
        else:
            self.pnl_curve.append({
                "time": minute, "pnl": total_pnl,
                # ★ 확정분을 따로 담아 둔다 - 화면이 "이 중 얼마가 확정
                #   손익인지" 구분해서 보여줄 수 있어야 한다.
                "realized": self.state.realized_pnl,
            })
            if len(self.pnl_curve) > self._curve_limit:
                self.pnl_curve = self.pnl_curve[-self._curve_limit:]

        for symbol, (name, pnl) in self._symbol_pnl_now().items():
            pts = self.symbol_curves.setdefault(symbol, {"name": name, "points": []})["points"]
            if pts and pts[-1][0] == minute:
                pts[-1][1] = pnl
            else:
                pts.append([minute, pnl])
                if len(pts) > self._curve_limit:
                    del pts[:-self._curve_limit]

    def _symbol_pnl_now(self) -> dict:
        """★ 종목별 오늘 손익 = 이미 청산한 분(실현) + 보유 중인 분(평가).
        전부 더하면 pnl_curve(총손익)와 같아지게 같은 기준으로 계산한다.
        반환: {symbol: (name, pnl)}"""
        out: dict = {}
        for c in self.state.closed:
            sym = c.get("symbol")
            if not sym:
                continue
            name, pnl = out.get(sym, (c.get("name") or sym, 0))
            out[sym] = (name, pnl + (c.get("pnl") or 0))
        for sym, pos in self.state.positions.items():
            now = getattr(pos, "last_price", None) or pos.peak_price or pos.entry_price
            name, pnl = out.get(sym, (pos.name or sym, 0))
            out[sym] = (name, pnl + (now - pos.entry_price) * pos.quantity)
        return {s: (n, round(p)) for s, (n, p) in out.items()}

    # ━━ 진입 (STAGE 12c) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _force_close_dt(self):
        return combine(self.clock.now().date(), self.cfg.exit.force_close_time)

    def _last_loss_time(self):
        """★ 가장 최근 손절이 난 시각. 연속 손절 중단의 재개 시점을
        계산하는 기준이 된다."""
        for c in reversed(self.state.closed):
            if (c.get("pnl") or 0) < 0:
                ts = c.get("exit_time")
                if ts:
                    try:
                        return datetime.fromisoformat(ts)
                    except Exception:
                        return None
        return None

    def _loss_halt_resume_at(self):
        """★★★ "연속 손절 후 재개 시간을 설정하게 해달라" - 마지막 손절
        시각 + risk.loss_halt_cooldown_hours(기본 24시간)가 재개 시각이다."""
        last = self._last_loss_time()
        if last is None:
            return None
        hours = getattr(self.cfg.risk, "loss_halt_cooldown_hours", 24.0)
        return last + timedelta(hours=hours)

    def _loss_halt_resume_text(self) -> str:
        resume = self._loss_halt_resume_at()
        if resume is None:
            return ""
        now = self.clock.now()
        if resume.date() == now.date():
            return resume.strftime("%H:%M")
        return resume.strftime("%m/%d %H:%M")

    def _loss_halt_expired(self) -> bool:
        """★★★ 설정한 쿨다운이 지났거나(마지막 손절 + risk.loss_halt_cooldown_hours
        경과) 세션이 바뀌었으면(해외주식 overseas_engine.halt_info()와 같은 원칙 -
        프리장·본장·NXT장은 유동성·변동성 성격이 달라 한 세션의 손절이 다음 세션의
        기회까지 막을 이유가 없다) 연속 손절 카운트를 풀어 준다. 예전엔 날짜가
        바뀌어야만 리셋되거나 설정 시간 경과만 봤어서, 세션이 바뀐 뒤에도 다음 세션
        기회까지 계속 막혀 있는 경우가 있었다."""
        resume = self._loss_halt_resume_at()
        if resume is not None and self.clock.now() >= resume:
            return True
        last = self._last_loss_time()
        if last is not None and session.domestic_phase(self.clock.now()) != session.domestic_phase(last):
            return True
        return False

    def try_entries(self) -> None:
        if self.degraded:
            self.journal.write("halt", "시세 끊김 - 진입 보류")
            return

        with self.lock:
            held = len(self.state.positions)
            trades = self.state.trades

        if held >= self.cfg.capital.max_positions:
            self.journal.write("halt", "보유 한도 도달")
            return

        # ★★ budget = daily_max_trades - trades - len(held)
        # 한도 1회인데 2종목을 열면 둘을 파는 순간 2회가 된다. 강제청산도 거래로
        # 세어지므로 진입 시점에 열려 있는 것까지 포함해 예산을 본다.
        budget = self.cfg.risk.daily_max_trades - trades - held
        if budget <= 0:
            self.journal.write("halt", f"남은 거래 횟수 없음 ({trades}/{self.cfg.risk.daily_max_trades})")
            return

        if not self._check_kill_switch():
            return

        if not self.candidates:
            return

        # ★★★ "시세 변동을 모니터링해서 최적의 종목을 골라 승률을 높인다"는
        # 요청에 따라 바꾼 부분. 예전에는 이 루프 안에서 조건을 통과하는
        # 즉시 매수해 버려서, 후보 목록 앞쪽에 있다는 이유만으로(= 아침
        # 스크리닝 때 정한 순서) 신호가 약한 종목을 사고 예산을 다 썼다.
        # 지금 이 순간 더 강한 신호를 내는 종목이 뒤에 있어도 못 샀다.
        # 이제 두 단계로 나눈다: ①후보를 전부 평가해 신호를 모으고
        # ②그중 최종 점수가 높은 순서로 예산만큼 매수한다.
        scored: list = []
        for cand in self.candidates:
            with self.lock:
                already_held = cand.symbol in self.state.positions
                cooldown_until = self.state.cooldown.get(cand.symbol)

            if already_held:
                self.candidate_status[cand.symbol] = "보유중"
                continue
            if cooldown_until and cooldown_until > iso(self.clock.now()):
                self.candidate_status[cand.symbol] = "재진입 대기"
                continue

            lookback = max(self.cfg.entry.breakout_lookback, self.cfg.entry.volume_window)
            need = max(80, lookback * 4)
            bars, _partial = self._entry_bars(cand.symbol, need)
            if not bars:
                self.candidate_status[cand.symbol] = "시세 없음"
                continue

            try:
                limits = self.client.price_limits(cand.symbol)
                upper_limit = limits.get("upperLimit")
            except Exception:
                upper_limit = None

            theme_bars = None
            for other in self.candidates:
                if other.theme == cand.theme and other.symbol != cand.symbol:
                    other_bars, _ = self._entry_bars(other.symbol, need)
                    if other_bars:
                        theme_bars = other_bars
                        break

            ctx = SimpleNamespace(
                symbol=cand.symbol, name=cand.name, theme=cand.theme,
                upper_limit=upper_limit, theme_bars=theme_bars, now=self.clock.now(),
                prev_verdict=self._last_verdicts.get(cand.symbol),
                prev_verdicts=self.candidate_verdicts.get(cand.symbol, []),
                change_rate=cand.change_rate, theme_rank=cand.theme_rank,
                theme_breadth=cand.theme_breadth, theme_intensity=cand.theme_intensity,
                orb_done=self._orb_done.get(cand.symbol, False),
                minutes_to_close=minutes_between(self.clock.now(), self._force_close_dt()),
                window=self._window, kr_session=True,
            )

            winner, all_verdicts = self.playbook.evaluate_entry(bars, ctx)
            self.candidate_verdicts[cand.symbol] = all_verdicts

            if winner is not None:
                self.candidate_status[cand.symbol] = winner.headline
            elif all_verdicts:
                best = max(all_verdicts, key=lambda v: v.score)
                self.candidate_status[cand.symbol] = best.headline

            for v in all_verdicts:
                logged = self.journal.evaluate(v)
                if logged and self.event_bus is not None:
                    try:
                        self.event_bus.publish("evaluate", v.to_dict())
                    except Exception:
                        pass

            if winner is not None:
                self._last_verdicts[cand.symbol] = winner
                # ★ 여기서 바로 사지 않는다 - 모든 후보를 본 뒤 신호가
                # 강한 순서로 사기 위해 일단 모아 둔다.
                final_score = getattr(winner, "selection_score", None)
                if final_score is None:
                    final_score = winner.score or 0.0
                # ★★★ 실제로 겪은 버그 - momentum_fade 청산이 "진입 시점 대비
                # 거래량 위축"을 보려고 entry_volume 을 참조하는데, 여기 담기는
                # 값이 봉 하나의 체결 거래량이 아니라 cand.trading_amount(당일
                # 누적 거래대금, 원 단위·수십억)였다. 단위가 완전히 달라
                # cur_volume/entry_volume 이 항상 0에 수렴해 "거래량 위축"
                # 조건이 사실상 항상 통과된 것으로 오판됐고, 나머지 두 조건
                # (연속 하락·VWAP 이탈) 중 하나만 겹쳐도 청산되는 셈이 되어
                # momentum_fade 가 다른 모든 청산 기법보다 먼저, 너무 이르게
                # 발동했다(승리한 진입도 이익을 키우기 전에 잘렸다). 진입 시점
                # 봉 하나의 거래량을 같은 단위로 넘긴다.
                scored.append((final_score, cand, winner, bars[-1].volume,
                               sizing.vol_ratio(bars, self.cfg.risk.stop_loss_pct)))
            else:
                if all_verdicts:
                    self._last_verdicts[cand.symbol] = all_verdicts[0]
                    self.journal.watch(cand.symbol, cand.name, all_verdicts[0].headline)

        if not scored:
            return

        # ★★★ 최종 점수가 높은 순서로 매수한다 - "지금 가장 강한 신호"부터
        # 예산을 쓴다. 예전처럼 목록 앞쪽이라는 이유로 약한 신호를 먼저
        # 사서 예산을 소진하는 일이 없어진다.
        scored.sort(key=lambda x: x[0], reverse=True)
        if len(scored) > 1:
            ranking = ", ".join(f"{c.name}({s:.2f})" for s, c, _, _, _ in scored[:5])
            self.journal.write(
                "session",
                f"진입 신호 {len(scored)}건을 비교해 강한 순서로 매수합니다 - {ranking}",
            )

        for final_score, cand, winner, entry_bar_volume, vol in scored:
            if budget <= 0:
                # ★ 예산이 다 떨어져 못 산 종목도 이유를 남긴다 - 화면에서
                # "왜 신호가 떴는데 안 샀나"를 알 수 있어야 한다.
                self.candidate_status[cand.symbol] = (
                    f"{winner.headline} · 오늘 거래 한도를 다 써서 매수하지 않았습니다"
                )
                continue
            if winner.technique == "orb":
                self._orb_done[cand.symbol] = True
            self._open_position(cand, winner, entry_bar_volume, vol=vol)
            budget -= 1

    @staticmethod
    def _conviction_of(cand):
        """후보의 테마 근거 크기(0~1). 테마 근거가 없는 관심 종목은 중립 0.5."""
        if cand is None:
            return None
        return sizing.theme_conviction(
            theme_rank=getattr(cand, "theme_rank", 0), breadth=getattr(cand, "theme_breadth", 0),
            intensity=getattr(cand, "theme_intensity", 0.0), rank_in_theme=getattr(cand, "rank_in_theme", 0),
        )

    def _open_position(self, cand, verdict, entry_bar_volume: float = 0.0, vol=None) -> None:
        # ★★★ 사기 직전 마지막 확인: 최근 헤드라인에 악재(상장폐지·횡령·유상증자·소송 등)가 있으면 사지 않는다(거르기 전용, 실패하면 통과).
        allowed, why = news_guard.get_guard(self.cfg).gate(cand.symbol, cand.name, "domestic")
        if not allowed:
            self.candidate_status[cand.symbol] = why
            self.journal.watch(cand.symbol, cand.name, why)
            return
        strength = sizing.signal_strength(verdict)
        conviction = self._conviction_of(cand)
        amount = self._entry_amount(strength, conviction, vol)
        price = verdict.price or cand.last_price
        if not price or price <= 0:
            return
        qty = int(amount // price)

        if self.cfg.is_live:
            # ★ live 면 주문 직전 broker.cash() 를 다시 확인한다 - 다른 경로로
            # 자금이 빠져나갔을 수 있다.
            try:
                cash = self.broker.cash()
                qty = min(qty, int(cash // price))
            except Exception:
                pass

        if qty < 1 or price * qty < self.cfg.risk.min_order_amount:
            self.journal.watch(cand.symbol, cand.name, f"주문 금액 미달 ({price * max(qty, 1):,.0f}원)")
            return

        fill = self.broker.buy(cand.symbol, qty, price, reason=verdict.headline, verdict_id=verdict.id)
        if not fill.ok:
            self.journal.write("halt", f"{cand.symbol} 매수 실패: {fill.reason}", symbol=cand.symbol, name=cand.name)
            if fill.fatal:
                self._halt(f"{cand.symbol} 매수에 실패해 매매를 멈춥니다: {fill.reason}")
            return

        pos = Position(
            symbol=cand.symbol, name=cand.name, theme=cand.theme, quantity=fill.quantity,
            entry_price=fill.price, entry_time=self.clock.now(), peak_price=fill.price,
            oco_id=None, entry_volume=entry_bar_volume, verdict_id=verdict.id,
            why=cand.why, technique=verdict.technique,
            last_fill_price=fill.price, invested=fill.price * fill.quantity,
            conviction=(0.5 if conviction is None else conviction),
        )
        if self.cfg.exit.use_conditional_oco:
            pos.oco_id = self.broker.place_oco(pos, self._oco_cfg())

        with self.lock:
            self.state.positions[cand.symbol] = pos
            # ★ trades 는 청산(_close_position) 시점에만 늘린다. 여기서도 늘리면
            # 왕복 거래 1건이 2건으로 이중 계산돼 daily_max_trades 가 절반만
            # 작동한다. 진입 시점에 열려 있는 것은 budget 계산에서 len(positions)
            # (held) 로 따로 반영한다.
            self._weekly_pnl_cache = None
        self.state.save()

        r = self.cfg.risk
        stop = pos.entry_price * (1 - r.stop_loss_pct)
        target = pos.entry_price * (1 + r.take_profit_pct)
        explain = explain_buy(cand, verdict, fill.quantity, fill.price, stop, target, bool(pos.oco_id))
        explain += (f" [신호 강도 {strength:.2f} · 테마 근거 {conviction if conviction is not None else 0.5:.2f} · "
                    f"변동성 {vol if vol is not None else 1.0:.2f}배 → 첫 매수 {amount:,.0f}원(종목당 한도 {self.position_cap:,.0f}원)]")
        # ★★★ 자동 선정 로직이 왜 이 기법을 골랐는지 근거를 함께 남긴다 -
        # 매매기법 화면에서 "선택 근거는 매매일지에 남는다"고 안내하므로
        # 실제로 남아야 한다. 재현 가능한 판단이어야 한다는 원칙과도 맞다.
        note = getattr(verdict, "selection_note", "")
        reason = getattr(verdict, "selection_reason", "")
        if note or reason:
            explain += f"\n[기법 선정] {note} {reason}".rstrip()
        self.journal.write(
            "buy", explain, symbol=cand.symbol, name=cand.name, theme=cand.theme,
            detail={"verdict": verdict.to_dict(), "why": cand.why,
                    "selection_note": note, "selection_reason": reason},
        )

        self._notify_trade("entry", {
            "symbol": cand.symbol, "name": cand.name, "theme": cand.theme,
            "qty": fill.quantity, "entry": fill.price, "technique": verdict.technique_label,
            "why": cand.why,
        })

    def _oco_cfg(self):
        """서버 OCO 에 걸 손절·익절 설정. 분할 매도를 쓰면 익절선을 2배로 멀리 둔다 - 서버 익절이 먼저 전량을
        팔아 버리면 분할 매도(일부는 끌고 가기)가 무력해지기 때문이다. 손절은 그대로다."""
        if not self.cfg.sizing.scale_out:
            return self.cfg
        from dataclasses import replace
        risk = replace(self.cfg.risk, take_profit_pct=self.cfg.risk.take_profit_pct * 2)
        return SimpleNamespace(risk=risk, exit=self.cfg.exit, costs=self.cfg.costs)

    def _rescreen_info(self) -> dict:
        """★★★ "종목 선정이 언제 다시되는지" - 마지막 스크리닝 시각 +
        rescreen_minutes 가 다음 재선정 시각이다. 화면이 이걸 알아야
        "30분마다 다시 고른다"는 사실과 남은 시간을 보여줄 수 있다.
        ★ 계산에 실패해도 빈 값을 돌려준다 - 이것 때문에 화면이 죽으면 안 된다.
        """
        try:
            minutes = self.cfg.entry.rescreen_minutes
            info = {"every_minutes": minutes, "last_at": None, "next_at": None}
            if self._last_screen is None:
                return info
            info["last_at"] = self._last_screen.strftime("%H:%M:%S")
            nxt = self._last_screen + timedelta(minutes=minutes)
            info["next_at"] = nxt.strftime("%H:%M:%S")
            remain = (nxt - self.clock.now()).total_seconds()
            info["remain_minutes"] = max(0, round(remain / 60))
            return info
        except Exception:
            return {}

    def _reload_screen_cfg(self) -> None:
        """★★★ 실제로 겪은 문제 - "[설정] 화면에서 종목선정 기준(예: 최소
        거래대금)을 올렸는데도 반영이 안 된다"는 문의의 원인. 엔진은 시작할
        때 받은 cfg 객체를 매매가 끝날 때까지 그대로 들고 있어서, 실행
        도중 설정을 저장해도 이미 떠 있는 엔진은 그 값을 다시 읽지 않는다
        (rescreen() 이 몇 번을 돌든, 화면의 "지금 갱신" 버튼을 눌러도
        마찬가지 - 전부 이 엔진의 낡은 cfg 를 그대로 쓰기 때문이다).
        ★ 손절·익절 같은 위험 설정(risk/entry/exit)은 매매 중 바뀌면 위험할
        수 있어 그대로 두고(그 값들은 화면에서도 거래 중엔 잠긴다), 종목
        선정 기준(screen.*)은 이미 산 포지션의 안전과 무관하므로 재선정
        시점마다 설정 파일에서 최신 값을 다시 읽어 반영한다.
        """
        try:
            from daytrader.config import load_config
            from daytrader.paths import config_path
            fresh = load_config(config_path())
            self.cfg.screen = fresh.screen
        except Exception as exc:
            log.warning("설정 파일에서 종목 선정 기준을 다시 읽지 못했습니다(기존 값을 계속 씁니다): %s", exc)

    def rescreen(self, force: bool = False) -> None:
        """rescreen_minutes 마다 전체 스크리닝을 다시 돈다. 그 사이에는 시세만 갱신한다.
        ★ "30분 지났으니까"라는 부수효과에 기대지 않는다 - 오늘 아직 한 번도
        스캔하지 않았으면(first_of_day) 무조건 다시 돈다. 어제 후보로 오늘
        아침을 시작하는 일이 있으면 안 된다.
        """
        now = self.clock.now()
        first_of_day = not self._opened_today
        due = (
            first_of_day or self._last_screen is None or force
            or minutes_between(self._last_screen, now) >= self.cfg.entry.rescreen_minutes
        )
        if due:
            self._reload_screen_cfg()
            self.report = self.screener.build_report()
            self.candidates = self.report.candidates
            self._last_screen = now
            self._opened_today = True
            # 후보의 뉴스 위험을 백그라운드에서 미리 판정해 둔다(살 때 기다리지 않게)
            news_guard.get_guard(self.cfg).warm([(c.symbol, c.name) for c in self.candidates], "domestic")
            # ★ 오늘 새로 뽑힌 종목에 실험실 백테스트를 자동으로 돌려(백그라운드), "이 종목엔
            # 이 기법이 최근 더 잘 맞았다"는 결과를 진입 기법 채점에 가산점으로 반영한다
            # (technique_prefs.py). 가짜 시세(sim/replay)에는 의미가 없어 건너뛴다.
            if not self.cfg.uses_fake_data:
                try:
                    from daytrader import auto_backtest
                    cand_rows = [{"symbol": c.symbol, "name": c.name, "theme": c.theme} for c in self.candidates]
                    auto_backtest.maybe_trigger(self.cfg, self.client, "domestic", cand_rows)
                except Exception as exc:
                    log.warning("실험실 자동 백테스트 트리거 실패(무시하고 계속): %s", exc)
            for c in self.candidates:
                self.candidate_status.setdefault(c.symbol, "관찰 중")
            # ★★★ "실제로 잘 맞았던 기법에 우선권을 준다" - 기법별 과거
            # 실적을 Playbook 에 넣어 준다. 매 루프 계산하면 원장 전체를
            # 매번 읽어 비싸므로, 스크리닝을 새로 도는 이 시점에만 갱신한다.
            # ★ 실적 조회가 실패해도 매매 자체는 계속돼야 한다(실적은
            # 보조 정보일 뿐이고, 없으면 순수 신호 강도로 판단한다).
            try:
                self.playbook.set_performance(self.ledger.by_technique(modes=[self.cfg.mode]))
            except Exception as exc:
                log.warning("기법별 실적 조회 실패(순수 신호 강도로만 판단합니다): %s", exc)
        elif self.report is not None:
            self.report = self.screener.refresh_prices(self.report)
            self.candidates = self.report.candidates

    # ━━ preflight / reconcile (세부 점검 항목은 이후 스테이지에서 채운다) ━━━━━

    # ━━ preflight / reconcile (STAGE 13, daytrader/safety.py) ━━━━━━━━━━━

    def _preflight(self) -> bool:
        """실거래 시작 전 계좌 상태를 먼저 확인한다 (원칙 6)."""
        from daytrader.safety import preflight
        result = preflight(self.cfg, self.client, ledger=self.ledger)
        for c in result["checks"]:
            self.journal.write(
                "session", f"[사전점검] {c['label']}: {c['detail']}",
                detail={"key": c["key"], "ok": c["ok"], "level": c["level"]},
            )
        if not result["ok"]:
            for c in result["blocking"]:
                self.reconcile_alerts.append(c["detail"])
        return result["ok"]

    def reconcile(self) -> None:
        """내부 상태와 실제 계좌를 대조한다. 계좌가 진실이다."""
        from daytrader.safety import reconcile as do_reconcile
        with self.lock:
            last_prices = {s: p.entry_price for s, p in self.state.positions.items()}
        diffs = do_reconcile(self.client, self.state, self.cfg, self.journal, self.ledger, last_prices=last_prices)
        if diffs:
            with self.lock:
                self.reconcile_alerts.extend(d.detail for d in diffs)
            self.state.save()

    # ━━ 날짜 경계 (STAGE 25) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _rollover_if_new_day(self) -> bool:
        """24시간 켜두는 것이 기본 사용법이라 이게 없으면 조용히 틀린다.
        어제의 실현손익이 오늘의 일일 손실 한도를 잡아먹고, 어제 도달한 중단
        상태가 풀리지 않고, 원장에 어제 날짜로 기록된다.
        """
        today = day_str(self._now())
        if today == self._session_date:
            return False

        prev_date = self._session_date
        prev_positions = dict(self.state.positions)  # 강제청산 실패로 남는 일이 있어 그대로 이어받는다.

        # ★ 여기가 유일하게 확실한 마감 시점이다.
        self._notify_closes(prev_date, today)

        with self.lock:
            state_path = os.path.join(self.cfg.state_dir, f"{self._prefix}daily_state.json")
            new_state = DailyState(state_path, today=today, mode=self.cfg.mode)
            new_state.positions = prev_positions
            self.state = new_state

            self._session_date = today
            self._opened_today = False
            self._last_screen = None
            self._last_phase = None
            self._force_close_deadline = None

            self.candidates = []
            self.candidate_status = {}
            self.candidate_verdicts = {}
            self._last_verdicts = {}
            self.report = None
            self.equity_curve = []
            self.pnl_curve = []
            self.symbol_curves = {}

        self.journal.write("session", f"{prev_date} → {today} 로 날짜가 바뀌어 새 세션을 시작합니다.")
        session.reset_holiday_cache()
        if self.event_bus is not None:
            try:
                self.event_bus.publish("alert", {"message": f"날짜가 바뀌어 새 세션을 시작합니다: {today}"})
            except Exception:
                pass

        self.state.save()
        return True

    def _notify_closes(self, prev_date: str, today: str) -> None:
        """전날 세션을 마감하며 요약을 남기고, 실거래면 일·월·연 마감 알림을 보낸다.
        ★ 맨 앞에서 cfg.is_live 를 확인해 원장 읽는 비용도 아낀다.
        ★ 예외를 전부 삼킨다. 알림이 실패해도 매매는 계속돼야 한다.
        """
        try:
            self.report_summary()
        except Exception as exc:
            self.journal.write("halt", f"[{prev_date} 마감 요약 실패] {exc}")
            return

        if not self.cfg.is_live:
            return

        try:
            modes = ("live",)
            daily_rows = self.ledger.daily(modes=modes)
            today_row = next((r for r in daily_rows if r["date"] == prev_date), None)
            if today_row:
                open_symbols = list(self.state.positions.keys())
                self.notifier.daily(prev_date, {
                    "pnl": today_row.get("pnl", 0), "trades": today_row.get("trades", 0),
                    "win_rate": today_row.get("win_rate", 0),
                    "balance": today_row.get("balance") or (self.allocation + self.state.realized_pnl),
                    "open_positions": open_symbols,
                })

            if prev_date[:7] != today[:7]:
                monthly_rows = self.ledger.monthly(modes=modes)
                m = next((r for r in monthly_rows if r["month"] == prev_date[:7]), None)
                if m:
                    self.notifier.monthly(prev_date[:7], {
                        "pnl": m.get("pnl", 0),
                        "return_pct": (m.get("pnl", 0) / self.allocation) if self.allocation else 0,
                        "win_rate": m.get("win_rate", 0), "trading_days": m.get("days", 0),
                        "up_days": m.get("up_days", 0),
                        "best_day": (m.get("best") or {}).get("date"), "best_day_pnl": (m.get("best") or {}).get("pnl", 0),
                        "worst_day": (m.get("worst") or {}).get("date"), "worst_day_pnl": (m.get("worst") or {}).get("pnl", 0),
                    })

            if prev_date[:4] != today[:4]:
                yearly_rows = self.ledger.yearly(modes=modes)
                y = next((r for r in yearly_rows if r["year"] == prev_date[:4]), None)
                if y:
                    self.notifier.yearly(prev_date[:4], {
                        "pnl": y.get("pnl", 0), "return_pct": y.get("return_pct") or 0,
                        "win_rate": y.get("win_rate", 0), "up_months": y.get("up_months", 0),
                        "best_month": (y.get("best_month") or {}).get("month"),
                        "best_month_pnl": (y.get("best_month") or {}).get("pnl", 0),
                        "worst_month": (y.get("worst_month") or {}).get("month"),
                        "worst_month_pnl": (y.get("worst_month") or {}).get("pnl", 0),
                    })
        except Exception:
            pass

    # ━━ 재개 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def resume(self) -> dict:
        """중단했다 다시 켰을 때 이어서 시작할 수 있게 맞춘다.
        DailyState 가 하루치 상태를 복원하지만 그것만으로는 부족하다.
        """
        result = {
            "resumed": bool(self.state.positions), "positions": len(self.state.positions),
            "realized": self.state.realized_pnl, "trades": self.state.trades,
            "reconciled": False, "priced": False,
        }

        # ★ live 만 계좌가 진실이다. paper/web/sim 의 포지션은 가상이라
        # 계좌와 대조하면 전부 '없는 것'으로 지워진다 - 모드를 반드시 구분한다.
        if self.cfg.is_live:
            try:
                self.reconcile()
                result["reconciled"] = True
            except Exception as exc:
                self.journal.write("halt", f"재개 중 계좌 대조 실패: {exc}")

        if self.state.positions:
            symbols = list(self.state.positions.keys())
            try:
                rows = self.client.prices(symbols)
                price_map = {}
                for r in rows:
                    s = str(r.get("symbol", "")).zfill(6)
                    p = r.get("price") or r.get("lastPrice")
                    if not s or not p:
                        continue
                    try:
                        price_map[s] = float(p)
                    except (TypeError, ValueError):
                        continue
                # 보유 포지션을 지금 시세로 다시 매긴다 - 낡은 가격을 보여주면 안 된다.
                for symbol, pos in self.state.positions.items():
                    last = price_map.get(symbol)
                    if last is not None and last > pos.peak_price:
                        pos.peak_price = last
                result["priced"] = bool(price_map)
            except Exception as exc:
                self.journal.write("halt", f"재개 중 시세 갱신 실패: {exc}")

        # equity/pnl 곡선에 현재 실현손익을 첫 점으로 심는다 - 비워 두면
        # 그래프가 0에서 다시 시작해 오늘 성적이 사라져 보인다.
        minute = hhmm(self._now().time())
        balance = self.allocation + self.state.realized_pnl
        self.equity_curve = [{"time": minute, "balance": balance}]
        self.pnl_curve = [{"time": minute, "pnl": self.state.realized_pnl}]
        self.symbol_curves = {}

        self.state.save()
        return result

    # ━━ 메인 루프 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def run(self) -> None:
        mode_labels = {"sim": "시뮬레이션", "replay": "리플레이", "web": "관찰", "paper": "모의매매", "live": "실거래"}
        techniques = ", ".join(t["label"] for t in self.playbook.describe())
        r = self.cfg.risk
        rt_cost = round_trip_cost_pct(self.cfg.costs.commission_pct, self.cfg.costs.tax_pct)
        be = breakeven_pct(self.cfg.costs.commission_pct, self.cfg.costs.tax_pct)
        banner = (
            f"[{mode_labels.get(self.cfg.mode, self.cfg.mode)}] 배정 {self.allocation:,.0f}원 · "
            f"켜진 기법: {techniques} · 손절 {r.stop_loss_pct*100:.1f}% / 익절 {r.take_profit_pct*100:.1f}% · "
            f"일일 한도 {r.daily_max_trades}회 · 왕복비용 {rt_cost*100:.3f}% (본전 {be*100:.3f}%)"
        )
        self.journal.write("session", banner)

        if self.cfg.is_live and not self._preflight():
            raise RuntimeError("실거래 사전 점검(preflight)에 실패해 시작할 수 없습니다.")

        self.resume()

        self._stop = False
        self._close_all = False
        self._force_close_deadline = None

        while not self._stop:
            self._rollover_if_new_day()
            now = self.clock.now()

            ses = session.phase(self.cfg, now=now, client=self.client)
            # ★★★ "장이 열려 있는데 왜 종목 선정도 매매도 안 하냐"는 물음에
            # 답하려면 화면이 지금 국면을 알아야 한다 - 이 프로그램은 장
            # 시간 전체가 아니라 설정된 진입 구간에만 신규 진입하는데,
            # 그 사유가 화면에 전혀 안 나와 고장으로 오해하게 됐다.
            self.current_session = {
                "phase": ses.get("phase"), "label": ses.get("label"),
                "trading": ses.get("trading"), "why": ses.get("why"),
                "scan_start": ses.get("scan_start"), "scan_end": ses.get("scan_end"),
                "next_open": ses.get("next_open"),
            }
            if ses["phase"] != self._last_phase:
                # 국면이 바뀌었을 때만 남긴다 - 매 루프 찍으면 로그가 쓰레기가 된다.
                log.info("장 국면 전환: %s (%s)", ses["phase"], ses["why"])
                self._last_phase = ses["phase"]
                if ses["phase"] == "after" and not self.state.positions and not self.cfg.uses_fake_data:
                    # ★★★ 예전엔 "장이 이미 끝난 시각에 시작하면 오늘은 할 매매가
                    # 없다"며 곧바로 run()을 끝냈다 - 사용자가 장 마감 후 자동매매를
                    # 켜면 아무 것도 안 하고 바로 꺼지는 것처럼 보였다. web/paper/
                    # live 처럼 실시간으로 24시간 켜두는 모드는 이제는 weekend/
                    # holiday 와 같은 대기 루프로 흘러들어가, 다음 개장 시각이 되면
                    # 이어서 스스로 종목 선정·매매를 시작한다.
                    reason = (
                        f"장이 이미 끝난 시각({now.strftime('%H:%M')})에 시작해 오늘은 더 할 매매가 없습니다 - "
                        f"다음 개장({ses.get('next_open', '')})까지 대기합니다."
                    )
                    log.info(reason)
                    self.journal.write("session", reason)

            # ★ sim/replay 는 정해진 하루치만 재생하는 백테스트라, 예전처럼
            # 장 마감 후 보유 종목이 없으면 그대로 끝나야 한다 - 안 그러면
            # 다음 개장까지 대기하느라 시뮬레이션이 영원히 끝나지 않는다.
            # web/paper/live 는 실제 24시간 상주 모드라 대기 루프로 흘러간다
            # (아래 not ses["live"] 분기).
            if ses["phase"] == "after" and not self.state.positions and self.cfg.uses_fake_data:
                reason = (
                    f"장이 이미 끝난 시각({now.strftime('%H:%M')})에 시작해 오늘은 더 할 매매가 없습니다 - "
                    f"보유 종목도 없어 정상 종료합니다. 다음 개장: {ses.get('next_open', '')}."
                )
                log.info(reason)
                self.journal.write("session", reason)
                self.last_stop_reason = reason
                break

            # ★★ "after"(장 마감 후)에 보유 종목이 있으면 강제청산 유예·세션
            # 종료 절차를 반드시 거쳐야 한다. 보유 종목이 없으면(실시간 모드)
            # weekend/holiday 와 똑같이 다음 개장까지 대기 루프로 빠진다.
            # ★★★ "오버나이트·장기 보유 허용" - cfg.exit.allow_overnight 가 켜져 있으면
            # 이 강제청산 유예 절차 자체를 건너뛴다. 포지션은 손절·트레일링·시간손절
            # 같은 다른 청산 기법으로만 관리되고, 장 마감 강제청산(ForceCloseExit)만
            # 빠진다 - 다음 개장 때 이어서 관리한다(아래 "장이 닫힘" 대기 루프로 그대로 빠짐).
            # ★★★ 실제로 겪은 버그 - sim/replay(uses_fake_data)는 하루치 시나리오만
            # 재생하는 백테스트라 "장 마감 후 포지션이 없으면 끝난다"(윗 블록)는 전제로
            # 돌아간다. allow_overnight 로 이 유예 절차를 건너뛰면 포지션이 하루를 넘겨도
            # 안 팔리고, 시나리오에 다음 날 시세가 없어 영원히 청산되지 않아 테스트가
            # 무한 루프에 빠졌다 - sim/replay 에서는 allow_overnight 여부와 무관하게
            # 항상 당일 강제청산한다(실거래·모의매매는 그대로 오버나이트 허용).
            if ses["phase"] == "after" and self.state.positions and (not self.cfg.exit.allow_overnight or self.cfg.uses_fake_data):
                if self._force_close_deadline is None:
                    self._force_close_deadline = now + timedelta(minutes=self.cfg.exit.force_close_deadline_min)
                if now > self._force_close_deadline:
                    remaining = ", ".join(self.state.positions.keys())
                    self._halt(
                        f"장 마감까지 청산하지 못한 종목이 있습니다: {remaining}. "
                        "오버나이트로 넘어갑니다. 토스 앱에서 직접 확인하세요."
                    )
                    break  # ★ 유예 시간이 지나면 세션을 끝낸다. 무한 재시도 금지.
                self.manage_positions(force_close=True)
                self.clock.sleep(self.cfg.entry.poll_seconds, should_stop=lambda: self._stop)
                continue

            if not ses["live"]:  # weekend, holiday, 장 마감 후(보유 종목 없음 또는 allow_overnight) - 다음 개장까지 대기
                for c in self.candidates:
                    self.candidate_status[c.symbol] = ses["label"]
                # ★ 장이 닫혀도 살아 있다는 것은 알려야 한다 - 안 그러면 화면이
                # 멈춘 것처럼 보여 죽은 줄 안다.
                self._emit("tick", self.tick())
                self.clock.sleep(60, should_stop=lambda: self._stop)
                continue

            # pre, scan, manage - 장중이다.
            self._allow_add = bool(not self.state.halted and ses["trading"])
            self._window = ses.get("window") or "main"
            self.manage_positions()

            if not self._check_kill_switch() and self.state.positions:
                self.manage_positions(force_close=True)

            # ★ 종목 선정(rescreen)은 이제 매수 가능 시간(trading)보다 넓은 "장이 열려 있는
            # 시간"(live - 프리마켓·정규장·NXT, session.py 참고) 동안 돈다. 실제 매수(try_entries)는
            # 여전히 trading(ph 기반) 시간에만 - live 를 넓혀도 매수 가능 시간대는 그대로다.
            if not self.state.halted and ses["live"]:
                self.rescreen()
            if not self.state.halted and ses["trading"]:
                self.try_entries()
            elif not self.state.halted:
                for c in self.candidates:
                    self.candidate_status[c.symbol] = ses["why"]

            if self.cfg.is_live:
                self._loop_count += 1
                if self._loop_count % self.cfg.live.reconcile_every_loops == 0:
                    self.reconcile()

            self.clock.sleep(self.cfg.entry.poll_seconds, should_stop=lambda: self._stop)

        if self._close_all:
            for _ in range(6):
                if not self.state.positions:
                    break
                self.manage_positions(force_close=True)

        self.ended_at = self.clock.now()  # ★ 끝난 뒤 화면의 장중 시각이 계속 흐르지 않게 고정한다.
        self.report_summary()
        self.notifier.stop()

    def request_stop(self, close_positions: bool = False) -> None:
        self._stop = True
        self._close_all = close_positions

    def tick(self) -> dict:
        """1초마다 보내는 가벼운 상태 - 화면을 부드럽게 갱신하기 위한 것."""
        with self.lock:
            return {
                "at": iso(self.clock.now()), "mode": self.cfg.mode,
                "halted": self.state.halted, "degraded": self.degraded,
                "positions": len(self.state.positions), "trades": self.state.trades,
                "realized_pnl": self.state.realized_pnl, "cash": self._cash_for_display(),
            }

    def _cash_for_display(self):
        """화면용 현금. 모의는 장부 값을 그대로, 실거래는 1초마다 계좌를 부르지 않도록 표시하지 않는다(None)."""
        if self.cfg.is_live:
            return None
        try:
            return self.broker.cash()
        except Exception:
            return None

    def snapshot(self) -> dict:
        """★ lock 안에서 전체 상태를 한 번에 그러모은다 - 화면이 중간에 바뀐 상태를 보지 않게."""
        from daytrader.ticks import round_trip_cost_pct
        with self.lock:
            headroom = max(0, self.cfg.risk.daily_max_trades - self.state.trades - len(self.state.positions))
            return {
                "mode": self.cfg.mode, "degraded": self.degraded,
                "halted": self.state.halted, "halt_reason": self.state.halt_reason,
                "reconcile_alerts": list(self.reconcile_alerts),
                "positions": {s: p.to_dict() for s, p in self.state.positions.items()},
                "closed": list(self.state.closed),
                "candidates": [
                    {
                        "symbol": c.symbol, "name": c.name, "theme": c.theme,
                        "last_price": c.last_price, "change_rate": c.change_rate,
                        "why": c.why, "status": self.candidate_status.get(c.symbol, ""),
                        "verdicts": [v.to_dict() for v in self.candidate_verdicts.get(c.symbol, [])],
                    }
                    for c in self.candidates
                ],
                "selection": self.report.to_dict() if self.report else None,
                "session": dict(self.current_session),
                # ★★★ "종목 선정이 언제 다시되는지 알려줘" - rescreen_minutes
                # 마다 다시 도는데 화면에 그 사실도, 다음 시각도 안 나와서
                # "멈춘 것 아닌가" 오해하게 된다.
                "rescreen": self._rescreen_info(),
                "techniques": self.playbook.describe(),
                "equity_curve": list(self.equity_curve),
                "pnl_curve": list(self.pnl_curve),
                "symbol_curves": {
                    s: {"name": v["name"], "points": [list(p) for p in v["points"]]}
                    for s, v in self.symbol_curves.items()
                },
                "headroom": headroom,
                "weekly_pnl": self._weekly_pnl(),
                "size_reduced": self.size_reduced,
                "allocation": self.allocation, "cash": self._cash_for_display(),
                "realized_pnl": self.state.realized_pnl,
                "trades": self.state.trades,
                "daily_max_trades": self.cfg.risk.daily_max_trades,
                "max_positions": self.cfg.capital.max_positions,
                "daily_loss_limit_pct": self.cfg.risk.daily_loss_limit_pct,
                "round_trip_cost_pct": round_trip_cost_pct(self.cfg.costs.commission_pct, self.cfg.costs.tax_pct),
            }

    def report_summary(self) -> dict:
        """오늘 하루 요약. 사람이 훑어볼 수 있는 문장 + 숫자."""
        n = len(self.state.closed)
        wins = sum(1 for t in self.state.closed if t["pnl"] > 0)
        pnl = self.state.realized_pnl
        summary = (
            f"오늘 {n}건 매매, {wins}승 {n - wins}패, 손익 {pnl:,.0f}원."
            if n else "오늘은 매매가 없었습니다. 조건을 충족하는 신호가 없었던 것으로 보입니다."
        )
        self.journal.write("session", summary)
        return {"trades": n, "wins": wins, "pnl": pnl, "summary": summary}
