"""스윙(며칠~몇 주 보유) 자동매매 엔진.

★★★ "단타 매매 외에 스윙 매매도 추가해달라. 스윙매매는 기법이 달라야겠지. 최적의 매매기법을
찾아서 적용해. 종목선정 방법도 달라야할거야." + "스윙매매 대상은 국내주식 해외주식 암호화폐
모두 해당되도록해. 종목선정시 단타매매와는 다른 기법 사용하고, 최근 일주일 동안 실제 주가
변동에 따라 선정하도록 해." - 국내·해외주식은 같은 TossClient, 암호화폐는 빗썸 계좌를 그대로
쓰되, 봉 하나가 1분이 아니라 하루(일봉)인 완전히 다른 시간축으로 도는 하나의 엔진이다. 세
시장을 각각 켜고 끄지 않는다 - 스윙은 "보유 기간이 다른 매매 방식" 하나이지 시장 하나가
아니라서, 하나의 시작/정지·상태 파일(swing_state.json)·포지션 장부(SwingPositionBook)로
세 시장의 포지션을 함께 관리한다(포지션마다 market 필드로 어느 시장 것인지 구분한다).

★ 기법 - playbook.py 의 SwingMaPullbackEntry/SwingBreakoutEntry/SwingGoldenCrossEntry 3종
(일봉 전용, technique_backtest.py 가 실제 시세로 어떤 게 이 종목에 최근 더 잘 맞았는지 찾아
가산점을 준다 - Playbook._preference_multiplier). 청산은 국내 단타보다 훨씬 넓은 손절·익절·
추적손절·최대보유일수(cfg.swing.*)만 쓴다 - ForceCloseExit(장 마감 강제청산)은 ctx.force_close
를 항상 False 로 두어(_make_ctx) 절대 발동시키지 않는다(스윙은 당일 청산이 아니므로).

★ 종목선정 - "단타매매와는 다른 기법" 을 시장별로 둔다:
  - 국내: 단타와 같은 테마 후보 풀(Screener) + cfg.swing.watchlist.
  - 해외: cfg.overseas.watchlist(단타 관심 종목) + cfg.swing.overseas_watchlist(스윙 전용).
  - 암호화폐: cfg.crypto.watchlist(단타 관심 종목) + cfg.swing.crypto_watchlist(스윙 전용).
  단타는 "오늘 하루의 실시간 등락률·거래량"으로 고르지만, 스윙은 이 후보 풀에서 실제 일봉을
  가져와 ① trend_filter_ma 일 이동평균 위(중기 상승 추세)인지, ② 최근 5거래일(약 1주일) 동안
  종가가 실제로 얼마나 움직였는지(week_momentum_min_pct 이상 올랐는지) 두 가지를 확인해서만
  후보로 남긴다 - "확정된 지난 결과"로 고른다는 점이 당일 실시간 지표로 고르는 단타와 다르다.

★ 진입 판정은 하루에 한 번, 그날 국내 정규장이 끝난 뒤(당일 일봉이 확정된 뒤)만 돈다 - 국내
장 마감 시각을 기준 삼는 것은 해외·암호화폐도 어차피 하루 한 번이면 충분하고(스윙은 몇 시간
차이로 신호가 크게 안 변한다), 기준 시각을 하나로 통일해야 관리가 단순하기 때문이다. 보유
종목의 손절·추적손절 관리는 장중에도 poll_seconds(기본 30분)마다 계속한다 - 그건 며칠을
기다릴 이유가 없다(손실은 빨리 끊어야 한다).

★ 실거래(live)는 아직 지원하지 않는다(daytrader/swing_broker.py 의 LiveSwingBroker 설명 참고) -
서버(daytrader/server.py)가 /api/swing/start 에서 mode=="live" 를 막는다. web(관찰)·paper(모의
매매)로 충분히 검증한 뒤에 추가한다.

★★★ 핵심 안전 원칙(국내 단타·해외주식·암호화폐와 동일) - 이 엔진이 buy() 로 직접 사서
SwingPositionBook 에 기록해 두지 않은 종목은 절대 매도하지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from types import SimpleNamespace

from daytrader import news_guard, session, sizing
from daytrader.perf_stats import summarize_closed_trades
from daytrader.playbook import Bar, Playbook
from daytrader.screener import Screener
from daytrader.signals import sma
from daytrader.swing_broker import NotOwnedError, SwingPosition, SwingPositionBook, PaperSwingBroker
from daytrader.timeutil import day_str, iso, now_kst, parse_dt

log = logging.getLogger(__name__)

WEEK_TRADING_DAYS = 5  # ★ "최근 일주일" = 최근 5거래일(주말 제외) 종가 비교.


def _now_ts() -> float:
    return time.time()


def _fmt_ts(ts: float) -> str:
    try:
        dt = datetime.fromtimestamp(ts)
        if dt.date() == datetime.now().date():
            return dt.strftime("%H:%M")
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return "-"


class SwingState:
    """포지션 장부 + 청산 기록을 JSON 파일에 남긴다. 스윙은 며칠~몇 주 보유하므로
    (국내 단타의 DailyState 와 달리) 날짜가 바뀌어도 절대 초기화하지 않는다."""

    def __init__(self, path: str):
        self.path = path
        self.book = SwingPositionBook()
        self.closed: list = []
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
            if not isinstance(p, dict):
                log.warning("스윙 상태 복원 - %s 기록 형식이 올바르지 않아 건너뜁니다(%s).", symbol, type(p).__name__)
                continue
            try:
                self.book.record_buy(SwingPosition(
                    symbol=symbol, quantity=p["quantity"], entry_price=p["entry_price"],
                    entry_time=p["entry_time"], peak_price=p["peak_price"],
                    technique=p.get("technique", ""), order_id=p.get("order_id"),
                    name=p.get("name", symbol), theme=p.get("theme", "스윙"),
                    adds=int(p.get("adds") or 0), last_fill_price=float(p.get("last_fill_price") or p["entry_price"] or 0.0),
                    scaled_out=int(p.get("scaled_out") or 0), invested=float(p.get("invested") or 0.0),
                    realized=float(p.get("realized") or 0.0),
                    sold_qty=float(p.get("sold_qty") or 0.0), sold_value=float(p.get("sold_value") or 0.0),
                    conviction=float(p.get("conviction") if p.get("conviction") is not None else 0.5),
                    market=p.get("market", "domestic"),
                ))
            except (KeyError, TypeError) as exc:
                log.warning("스윙 상태 복원 - %s 기록에 빠진 값이 있어 건너뜁니다: %s", symbol, exc)
        closed = data.get("closed", [])
        self.closed = [c for c in closed if isinstance(c, dict)] if isinstance(closed, list) else []

    def save(self) -> None:
        positions = {
            s: {
                "quantity": p.quantity, "entry_price": p.entry_price, "entry_time": p.entry_time,
                "peak_price": p.peak_price, "technique": p.technique, "order_id": p.order_id,
                "name": p.name, "theme": p.theme, "market": p.market,
                "adds": p.adds, "last_fill_price": p.last_fill_price, "scaled_out": p.scaled_out,
                "invested": p.invested, "realized": p.realized, "sold_qty": p.sold_qty, "sold_value": p.sold_value,
                "conviction": p.conviction,
            }
            for s, p in self.book.all().items()
        }
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"positions": positions, "closed": self.closed}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


class SwingEngine:
    def __init__(self, cfg, client, state_path: str | None = None, notifier=None):
        self.cfg = cfg
        self.swing_cfg = cfg.swing
        self.client = client  # 국내·해외 - 같은 TossClient/QuoteRouter

        # ★★★ "스윙매매 대상은 국내주식·해외주식·암호화폐 모두" - 암호화폐 레그는 완전히
        # 다른 계좌·API(빗썸)라 별도 클라이언트가 필요하다. 크립토 엔진(crypto_engine.py)과
        # 같은 원칙: 가짜 시세 모드에서는 합성 클라이언트, 키가 없으면 그냥 크립토 레그를
        # 건너뛴다(엔진 전체가 죽으면 안 된다 - 국내·해외 스윙은 키 없이도 동작해야 한다).
        self.crypto_client = None
        if cfg.uses_fake_data:
            from daytrader.sim_feed import SimFeedClient
            self.crypto_client = SimFeedClient(seed=getattr(cfg.simulation, "seed", 42))
        elif cfg.bithumb_access_key and cfg.bithumb_secret_key:
            from daytrader.bithumb_api import BithumbClient
            self.crypto_client = BithumbClient(cfg.bithumb_access_key, cfg.bithumb_secret_key)

        # ★★★ 국내 단타의 cfg.risk(2.5%/4%대, 1분봉 기준)를 그대로 쓰면 일봉 변동폭에 거의
        # 매번 손절로 잡힌다 - 코인·해외주식과 같은 이유로 스윙 전용 risk 네임스페이스를 만들어
        # Playbook 에 명시적으로 넘긴다.
        swing_risk = SimpleNamespace(
            stop_loss_pct=cfg.swing.stop_loss_pct, take_profit_pct=cfg.swing.take_profit_pct,
            trailing_stop_pct=cfg.swing.trailing_pct, trailing_arm_pct=cfg.swing.trailing_pct,
        )
        self.playbook = Playbook(
            cfg, entry_order=cfg.swing.entry_order, exit_enabled=cfg.swing.exit_enabled,
            risk=swing_risk, max_hold_minutes=cfg.swing.max_hold_days * 24 * 60.0, market="swing",
            learning_mode=cfg.swing.technique_learning_mode,
        )

        state_path = state_path or os.path.join(cfg.state_dir, "swing_state.json")
        self.state = SwingState(state_path)

        # ★ 실거래는 아직 지원하지 않는다(모듈 설명 참고) - 항상 모의매매 브로커를 쓴다.
        # 서버가 /api/swing/start 에서 mode=="live" 를 막으므로 여기까지 오지 않는다.
        self.is_live = False
        # ★★★ 실제로 겪은 버그(국내주식 engine.py 에서 먼저 발견돼 고쳐진 것과 같은 종류) -
        # 재시작할 때마다 모의매매 현금이 그동안의 손익과 무관하게 매번 총 투자금액 그대로
        # 초기화됐다("대시보드 현금 잔여금액이 안 맞다"는 문의의 원인). 보유 중인 포지션은
        # 복원하면서 그 포지션을 사는 데 쓴 현금은 되돌려주지 않은 것 - 포지션과 그
        # 값어치만큼의 현금이 동시에 존재하는 이중 계산이었다. 지금까지 실현손익을 더하고,
        # 지금 보유 중인 포지션에 묶여 있는 돈(매수 금액 - 이미 분할 매도로 받은 돈)을
        # 빼서 복원한다.
        realized = sum((c.get("pnl") or 0) for c in self.state.closed)
        locked = sum(max((p.invested or 0.0) - (p.sold_value or 0.0), 0.0) for p in self.state.book.all().values())
        self.broker = PaperSwingBroker(
            starting_cash=float(cfg.swing.budget or 0.0) + realized - locked, book=self.state.book,
            commission_pct=cfg.costs.commission_pct, tax_pct=cfg.costs.tax_pct,
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
        self._last_prices: dict = {}
        self.candidates: list = []  # 화면용 - 최근에 뽑은(두 필터를 통과한) 후보, market 태그 포함
        self._last_entry_date: str = ""  # 오늘 이미 진입 판정을 돌렸으면 다시 안 돈다.

    def _notify(self, text: str) -> None:
        if not self.is_live or self.notifier is None:
            return
        try:
            self.notifier.send(text, force=True)
        except Exception:
            log.warning("스윙 알림 전송 실패", exc_info=True)

    # ━━ 시작/정지 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="swing-engine", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("스윙 엔진 루프 오류")
            self._stop.wait(self.swing_cfg.poll_seconds)

    # ━━ 한 바퀴 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def run_once(self) -> None:
        self.loop_count += 1
        ph = session.phase(self.cfg, client=self.client)
        # ★ 보유 종목 관리는 시세가 움직이는 동안(주말·공휴일 제외)은 계속한다 - 신규 진입만
        # halt/시간대 제약을 받는다. 해외·암호화폐 포지션도 국내 장 시간 기준으로 함께 돈다 -
        # 스윙은 어차피 하루 한 번 판단이면 충분해 시장별로 따로 시각을 잴 필요가 없다.
        if ph["live"]:
            for symbol, pos in list(self.state.book.all().items()):
                try:
                    self._manage_position(symbol, pos)
                except Exception as exc:
                    self.last_error = f"{symbol}: {exc}"
                    log.warning("스윙 보유 관리 실패 %s: %s", symbol, exc)
        # ★ 신규 진입 판정은 하루에 한 번, 그날 국내 정규장이 끝난 뒤(일봉이 확정된 뒤)만 -
        # 모듈 설명 참고.
        today = day_str(now_kst())
        if ph["phase"] in ("manage", "after") and self._last_entry_date != today:
            self._last_entry_date = today
            try:
                self._daily_entry_cycle()
            except Exception as exc:
                self.last_error = f"종목 선정: {exc}"
                log.warning("스윙 일일 진입 판정 실패: %s", exc)
        self.state.save()

    def halt_info(self) -> dict:
        """★ 연속 손절 후 재개 시각을 함께 계산한다(코인·국내 단타와 같은 원칙) - "마지막
        손절 + 쿨다운시간"으로 재개 시각을 잰다. ★★★ "모든 장에서 3시간이면 재개" 요청으로
        예전의 "쿨다운일"(days) 단위를 다른 시장과 동일한 "쿨다운시간"(hours) 단위로
        바꿨다(swing_cfg.loss_halt_cooldown_hours, 기본 3시간)."""
        # ★ "일 최대 거래건수는 스윙은 필요없어" - 신규 진입 판정 자체가 하루 한 번뿐이고
        # 보유도 며칠~몇 주 단위라 과매매 방지용 일일 상한이 의미가 없다는 판단 - 국내·
        # 해외주식·암호화폐에만 둔다.
        streak = 0
        last_loss_at = None
        for c in reversed(self.state.closed):
            if (c.get("pnl") or 0) < 0:
                streak += 1
                if last_loss_at is None:
                    last_loss_at = c.get("exit_time")
            else:
                break
        if streak < self.swing_cfg.consecutive_loss_halt:
            return {"halted": False, "reason": "", "resume_at": None}
        hours = float(getattr(self.swing_cfg, "loss_halt_cooldown_hours", 3.0))
        cooldown = hours * 3600
        resume = (last_loss_at or _now_ts()) + cooldown
        if _now_ts() >= resume:
            return {"halted": False, "reason": "", "resume_at": None}
        return {
            "halted": True, "resume_at": resume,
            "reason": (f"연속 손절 {streak}회 - 신규 진입 중단(보유분 관리는 계속함) "
                       f"· 재개 예정 {_fmt_ts(resume)} (설정 {cooldown/3600:g}시간 뒤)"),
        }

    def _cap(self) -> float:
        return sizing.position_cap(float(self.swing_cfg.budget or 0.0), int(self.swing_cfg.max_positions or 1))

    # ━━ 시장별 시세 조회 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ★★★ 국내·해외는 같은 TossClient(1일봉 "1d")를 쓰고, 암호화폐만 빗썸의 일봉 API
    # (day_candles)가 따로 있다 - 시장마다 여기서만 분기하고, 위(선정·진입·관리) 로직은
    # market 이 뭐든 똑같은 Bar 리스트만 다룬다.

    def _fetch_daily_bars(self, symbol: str, market: str, count: int = 260) -> list:
        if market == "crypto":
            if self.crypto_client is None:
                return []
            day_fn = getattr(self.crypto_client, "day_candles", None)
            if day_fn:
                rows = day_fn(symbol, min(200, count))
            else:  # SimFeedClient 등 day_candles 가 없는 가짜 클라이언트 - 분봉 형식으로 대신 받는다.
                rows = self.crypto_client.candles(symbol, unit=1, count=min(200, count))
            bars = [
                Bar(
                    ts=str(r.get("candle_date_time_kst") or r.get("timestamp") or ""),
                    open=float(r.get("opening_price") or r.get("open") or 0),
                    high=float(r.get("high_price") or r.get("high") or 0),
                    low=float(r.get("low_price") or r.get("low") or 0),
                    close=float(r.get("trade_price") or r.get("close") or 0),
                    volume=float(r.get("candle_acc_trade_volume") or r.get("volume") or 0),
                )
                for r in (rows or []) if isinstance(r, dict)
            ]
            bars.sort(key=lambda b: b.ts or "")
            return bars
        raw = getattr(self.client, "primary", None) or self.client
        rows = raw.candles(symbol, "1d", min(200, count))
        return [Bar.from_api(r) for r in (rows or []) if isinstance(r, dict)]

    def _current_price(self, symbol: str, market: str) -> float:
        if market == "crypto":
            if self.crypto_client is None:
                raise RuntimeError(f"{symbol} - 암호화폐 클라이언트가 없습니다(빗썸 키 미등록).")
            rows = self.crypto_client.ticker([symbol])
            r = next((x for x in (rows or []) if isinstance(x, dict)), None)
            if r is None:
                raise RuntimeError(f"{symbol} 시세를 받지 못했습니다.")
            price = float(r.get("trade_price") or 0)
            if price <= 0:
                raise RuntimeError(f"{symbol} 시세 값이 올바르지 않습니다.")
            return price
        rows = self.client.candles(symbol, "1m", 1)
        bars = [Bar.from_api(r) for r in (rows or []) if isinstance(r, dict)]
        if not bars:
            raise RuntimeError(f"{symbol} 시세를 받지 못했습니다.")
        last = bars[-1].close
        if last != last:  # NaN
            raise RuntimeError(f"{symbol} 시세 값이 올바르지 않습니다.")
        return last

    def _make_ctx(self, symbol: str, name: str, theme: str, now, held_minutes: float = 0.0) -> SimpleNamespace:
        # ★★★ force_close 는 절대 True 로 만들지 않는다 - 스윙은 당일 청산 개념이 없다
        # (playbook.ForceCloseExit 는 ctx.force_close 만 보고 판단하므로, 여기서 항상 False 를
        # 주는 것만으로 국내 단타의 "장 마감 강제청산"이 스윙 포지션에는 절대 발동하지 않는다).
        return SimpleNamespace(
            symbol=symbol, name=name, theme=theme, now=now,
            prev_verdict=None, prev_verdicts=[], held_minutes=held_minutes, force_close=False,
            scale_out=bool(self.cfg.sizing.scale_out),
        )

    # ━━ 종목 선정(하루 한 번, 국내·해외·암호화폐 세 시장) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _raw_pool(self, market: str) -> list:
        """시장별 "단타와 다른" 원재료 후보 풀. 실제 필터(추세·1주 모멘텀)는 _select_candidates
        에서 실제 일봉으로 확인한다 - 여기는 아직 아무 시세도 안 본 이름 목록일 뿐이다."""
        if market == "domestic":
            try:
                report = Screener(self.client, self.cfg).build_report()
                pool = [{"symbol": c.symbol, "name": c.name, "theme": c.theme} for c in report.candidates]
            except Exception as exc:
                log.warning("스윙 후보(국내) - 테마 스크리닝 실패(관심 종목만으로 진행): %s", exc)
                pool = []
            have = {c["symbol"] for c in pool}
            for symbol in (self.swing_cfg.watchlist or []):
                if symbol not in have:
                    pool.append({"symbol": symbol, "name": symbol, "theme": "관심 종목"})
                    have.add(symbol)
            return pool
        if market == "overseas":
            have = set()
            pool = []
            for symbol in list(self.cfg.overseas.watchlist or []) + list(self.swing_cfg.overseas_watchlist or []):
                if symbol not in have:
                    pool.append({"symbol": symbol, "name": symbol, "theme": "관심 종목"})
                    have.add(symbol)
            return pool
        if market == "crypto":
            if self.crypto_client is None:
                return []
            have = set()
            pool = []
            for symbol in list(self.cfg.crypto.watchlist or []) + list(self.swing_cfg.crypto_watchlist or []):
                if symbol not in have:
                    pool.append({"symbol": symbol, "name": symbol.replace("KRW-", ""), "theme": "관심 종목"})
                    have.add(symbol)
            return pool
        return []

    def _passes_swing_filters(self, bars: list) -> bool:
        """★★★ "종목선정시 단타매매와는 다른 기법 사용하고, 최근 일주일 동안 실제 주가 변동에
        따라 선정" - 단타는 오늘 하루의 실시간 등락률·거래량으로 고르지만, 스윙은 실제로 확정된
        과거 일봉 두 가지를 본다: ① trend_filter_ma 일 이동평균 위(중기 상승 추세가 살아 있는가),
        ② 최근 5거래일(약 1주일) 실제 종가 변동률이 week_momentum_min_pct 이상인가(당장 눌림목이
        와도 최근 흐름 자체는 상승이었는가). 둘 다 통과해야 후보로 남는다."""
        trend_ma = int(getattr(self.swing_cfg, "trend_filter_ma", 60) or 60)
        need = max(trend_ma, WEEK_TRADING_DAYS + 1) + 2
        if len(bars) < need:
            return False
        ma = sma(bars, trend_ma)
        close = bars[-1].close
        if not (ma == ma and close == close and close >= ma):  # NaN 이 아니고 추세 위
            return False
        week_ago = bars[-1 - WEEK_TRADING_DAYS].close
        if not week_ago:
            return False
        week_return = (close - week_ago) / week_ago
        return week_return >= float(getattr(self.swing_cfg, "week_momentum_min_pct", 0.0) or 0.0)

    def _select_candidates(self) -> list:
        # ★ 시장별로 먼저 필터를 통과한 목록을 만든 뒤 번갈아(round-robin) 섞는다 - 그냥 순서대로
        # (국내 먼저) 이어 붙이면 국내 후보가 많을 때 max_positions 를 국내가 다 채워버려서
        # "세 시장 모두 해당" 이 사실상 국내 전용이 되어버린다.
        by_market: dict = {}
        for market in ("domestic", "overseas", "crypto"):
            found = []
            for c in self._raw_pool(market):
                try:
                    bars = self._fetch_daily_bars(c["symbol"], market, count=max(self.swing_cfg.trend_filter_ma + 5, 70))
                except Exception:
                    continue
                if not bars or not self._passes_swing_filters(bars):
                    continue
                found.append({**c, "market": market})
            by_market[market] = found

        out = []
        pools = [by_market["domestic"], by_market["overseas"], by_market["crypto"]]
        i = 0
        while any(pools):
            p = pools[i % 3]
            if p:
                out.append(p.pop(0))
            i += 1
        return out

    def _daily_entry_cycle(self) -> None:
        # ★ 국내주식(engine.py)만 기법별 실적을 playbook 에 채워주고 있어서, 스윙은
        # technique_learning_mode 를 뭘로 둬도 실적 가산점이 항상 중립(1.0)이었다
        # (2026-09-24 확인) - self.state.closed(스윙 자체 청산 이력)로 여기서도 채워 준다.
        try:
            self.playbook.set_performance(
                summarize_closed_trades(self.state.closed, technique_field="entry_technique")["by_technique"])
        except Exception as exc:
            log.warning("기법별 실적 조회 실패(순수 신호 강도로만 판단합니다): %s", exc)
        self.candidates = self._select_candidates()
        cand_rows = [{"symbol": c["symbol"], "name": c["name"], "theme": c["theme"], "market": c["market"]}
                     for c in self.candidates]
        # ★ "종목이 선정되면 실험실을 자동으로 돌린다" - 다른 세 시장과 같은 원칙. days=240 은
        # technique_backtest.run() 이 스윙일 때 알아서 쓰는 기본값과 맞춘다(60일선 웜업 포함).
        # ★ candidates 에 market 태그가 섞여 있으므로 client 는 국내·해외용(self.client)만 넘기고,
        # 암호화폐 후보의 시세는 technique_backtest.py 가 각 후보의 market 태그를 보고 자체적으로
        # 빗썸 클라이언트를 만들어 받는다(daytrader/technique_backtest.py 의 swing 분기 참고).
        if not self.cfg.uses_fake_data:
            try:
                from daytrader import auto_backtest
                # ★ symbol_limit 을 기본값(10)보다 넉넉히 준다 - 세 시장이 번갈아(round-robin) 담겨
                # 있어도, 한도가 너무 작으면 뒤쪽 시장 후보가 아예 하나도 못 들어갈 수 있다.
                auto_backtest.maybe_trigger(self.cfg, self.client, "swing", cand_rows, days=240, symbol_limit=30)
            except Exception as exc:
                log.warning("실험실 자동 백테스트 트리거 실패(무시하고 계속): %s", exc)

        halted = self.halt_info().get("halted", False)
        cap = int(self.swing_cfg.max_positions or 0)
        for c in self.candidates:
            symbol = c["symbol"]
            if self.state.book.owns(symbol):
                continue
            if halted:
                continue
            if cap and len(self.state.book.all()) >= cap:
                break
            try:
                self._try_entry(symbol, c["name"], c["theme"], c["market"])
            except Exception as exc:
                self.last_error = f"{symbol}: {exc}"
                log.warning("스윙 진입 판정 실패 %s: %s", symbol, exc)

    def _try_entry(self, symbol: str, name: str, theme: str, market: str) -> None:
        bars = self._fetch_daily_bars(symbol, market, count=200)
        if len(bars) < 30:
            return
        ctx = self._make_ctx(symbol, name, theme, now_kst())
        winner, _all_verdicts = self.playbook.evaluate_entry(bars, ctx)
        if winner is None:
            return

        current_price = bars[-1].close
        if self.swing_cfg.mode == "web":
            log.info("[스윙 매수 신호·관찰 전용] %s(%s) @ %s (%s) - 실제로 사지 않습니다.",
                      symbol, market, current_price, winner.technique_label)
            return

        allowed, why = news_guard.get_guard(self.cfg).gate(symbol, name, market)
        if not allowed:
            self.last_error = f"{symbol}: {why}"
            return

        strength = sizing.signal_strength(winner)
        vol = sizing.vol_ratio(bars, self.swing_cfg.stop_loss_pct)
        amount = sizing.entry_amount(self._cap(), strength, self.cfg.sizing, conviction=0.5, vol=vol)
        min_order = 5000 if market == "crypto" else float(self.cfg.risk.min_order_amount)
        if amount < min_order:
            self.last_error = f"{symbol}: 투자금액이 너무 작아 매수하지 않습니다(종목당 한도 {self._cap():,.0f})"
            return
        try:
            available = float(self.broker.cash or 0)
        except (TypeError, ValueError):
            available = 0.0
        if amount > available:
            self.last_error = f"{symbol}: 현금 부족으로 매수 보류"
            return

        pos = self.broker.buy(symbol, name, amount, current_price, technique=winner.technique, market=market)
        if pos:
            pos.theme = theme
            log.info("[스윙 매수] %s(%s) %s @ %s (%s · 신호 %.2f)", symbol, market, pos.quantity,
                      current_price, winner.technique_label, strength)
            self._notify(f"📈 <b>[스윙 매수]</b> {name}({symbol})\n{pos.quantity} @ {current_price:,.4f}\n{winner.technique_label}")

    def _manage_position(self, symbol: str, pos) -> None:
        market = getattr(pos, "market", "domestic")
        current_price = self._current_price(symbol, market)
        self._last_prices[symbol] = current_price
        self.state.book.update_peak(symbol, current_price)
        pos = self.state.book.get(symbol)

        held_minutes = ((_now_ts() - pos.entry_time) / 60.0) if pos.entry_time else 0.0
        ctx = self._make_ctx(symbol, pos.name, pos.theme, now_kst(), held_minutes=held_minutes)

        # ★ 일봉을 받아 ADX 로 추세 세기를 재고(강하면 익절폭을 넓힌다), momentum_fade
        # 청산 기법(거래량 위축·연속 하락·VWAP 이탈)에도 그대로 넘긴다. 30분마다 도는
        # 느린 루프라 매번 받아도 부담이 크지 않다.
        bars = self._fetch_daily_bars(symbol, market, count=40)

        sz = self.cfg.sizing
        take_pct = (sizing.dynamic_take_profit_pct(self.swing_cfg.take_profit_pct, bars, sz)
                    if self.swing_cfg.dynamic_take_profit else self.swing_cfg.take_profit_pct)
        if self.swing_cfg.technique_learning_mode == "entry_exit_pref":
            from daytrader import exit_efficiency
            widen, _why = exit_efficiency.widen_multiplier(self.cfg, "swing", symbol)
            take_pct *= widen
        step = sizing.exit_step(scaled_out=pos.scaled_out, entry=pos.entry_price or 0.0, price=current_price, sz=sz,
                                 take_pct=take_pct, conviction=pos.conviction, vol=None)
        if step is None and sizing.breakeven_hit(scaled_out=pos.scaled_out, entry=pos.entry_price or 0.0, price=current_price, sz=sz):
            self._execute_exit(symbol, pos, current_price, "breakeven_stop")
            return

        verdict = self.playbook.evaluate_exit(pos, bars, current_price, ctx)
        if verdict is not None and verdict.ok:
            self._execute_exit(symbol, pos, current_price, verdict.technique)
            return
        if step is not None:
            self._execute_exit(symbol, pos, current_price, step[1], fraction=step[0])

    def _execute_exit(self, symbol: str, pos, current_price: float, reason: str, fraction: float = 1.0) -> bool:
        try:
            result = self.broker.sell(symbol, current_price, reason=reason, fraction=fraction)
        except NotOwnedError:
            log.error("★★★ %s 매도가 NotOwnedError 로 거부됨 - 이 엔진이 사지 않은 종목입니다.", symbol)
            self.last_error = f"{symbol}: NotOwnedError - 이 엔진이 사지 않은 종목 매도 시도가 거부됨"
            return False

        result = result if isinstance(result, dict) else {}
        partial = bool(result.get("partial"))
        sold = result.get("quantity", pos.quantity)
        if partial:
            pos.sold_qty = (pos.sold_qty or 0.0) + sold
            pos.sold_value = (pos.sold_value or 0.0) + sold * current_price
            pnl = result.get("pnl")
            log.info("[스윙 분할 매도] %s @ %s (%s) - 남은 %s", symbol, current_price, reason, pos.quantity)
            self._notify(f"📉 <b>[스윙 분할 매도]</b> {pos.name}({symbol})\n@ {current_price:,.4f} · 손익 {pnl:+,.0f} · 남은 {pos.quantity}\n{reason}")
            return True

        total_qty = (pos.sold_qty or 0.0) + sold
        avg_exit = (((pos.sold_value or 0.0) + sold * current_price) / total_qty) if total_qty else current_price
        total_pnl = result.get("total_pnl", result.get("pnl"))
        parts = getattr(pos, "scaled_out", 0) or 0
        record = {
            "symbol": symbol, "name": pos.name, "theme": pos.theme, "market": getattr(pos, "market", "domestic"),
            "quantity": total_qty, "entry_price": pos.entry_price, "exit_price": avg_exit, "pnl": total_pnl,
            "reason": reason + (f" (분할 매도 {parts}회 후)" if parts else ""),
            "entry_time": pos.entry_time, "exit_time": _now_ts(),
            "entry_technique": pos.technique, "is_live": self.is_live,
        }
        if getattr(pos, "adds", 0) or parts:
            record["adds"] = getattr(pos, "adds", 0)
            record["scaled_out"] = parts
        self.state.closed.append(record)
        if self.swing_cfg.technique_learning_mode == "entry_exit_pref" and pos.entry_price:
            from daytrader import exit_efficiency
            mfe_pct = (pos.peak_price or pos.entry_price) / pos.entry_price - 1
            realized_pct = avg_exit / pos.entry_price - 1
            exit_efficiency.record(self.cfg, "swing", symbol, realized_pct, mfe_pct)
        log.info("[스윙 매도] %s @ %s (%s)", symbol, current_price, reason)
        pnl_text = f"{total_pnl:+,.0f}" if total_pnl is not None else "-"
        self._notify(f"📉 <b>[스윙 매도]</b> {pos.name}({symbol})\n@ {current_price:,.4f} · 손익 {pnl_text}\n{reason}")
        return True

    def liquidate_all(self) -> int:
        count = 0
        for symbol, pos in list(self.state.book.all().items()):
            try:
                current_price = self._current_price(symbol, getattr(pos, "market", "domestic"))
            except Exception as exc:
                self.last_error = f"{symbol}: 청산 중 시세 조회 실패 - {exc}"
                continue
            if self._execute_exit(symbol, pos, current_price, "force_close"):
                count += 1
        self.state.save()
        return count

    # ━━ 화면용 요약 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def snapshot(self) -> dict:
        positions = {
            s: {
                "name": p.name, "theme": p.theme, "market": p.market, "quantity": p.quantity,
                "entry_price": p.entry_price, "peak_price": p.peak_price, "technique": p.technique,
                "last_price": self._last_prices.get(s),
                "adds": p.adds, "scaled_out": p.scaled_out, "invested": p.invested,
                "held_days": round((_now_ts() - p.entry_time) / 86400.0, 2) if p.entry_time else 0.0,
            }
            for s, p in self.state.book.all().items()
        }
        return {
            "running": self.is_running(), "is_live": self.is_live, "mode": self.swing_cfg.mode,
            "cash": self.broker.cash, "positions": positions,
            "closed_count": len(self.state.closed), "closed": self.state.closed[-10:],
            "stats": summarize_closed_trades(self.state.closed, technique_field="entry_technique", symbol_field="symbol"),
            "loop_count": self.loop_count, "last_error": self.last_error,
            "candidates": self.candidates,
            "entry_techniques": [t.label for t in self.playbook.entries],
            "halt": self.halt_info(),
            "crypto_available": self.crypto_client is not None,
        }
