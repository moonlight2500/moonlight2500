"""코인 자동매매 엔진.

★★★ 핵심 안전 원칙 (국내주식과 동일):
  이 엔진이 buy() 로 직접 사서 CryptoPositionBook 에 기록해 두지 않은
  코인은 절대 매도하지 않는다. 계좌에 있는 코인이라도 이 엔진이 산 적
  없으면 sell() 자체가 구조적으로 거부한다(bithumb_broker.NotOwnedError).

★ 24시간 시장이라 "장 마감"이 없다 - 국내주식 engine.py 와 달리 하루
단위로 끝나지 않고 계속 돈다. 멈추려면 stop() 을 직접 불러야 한다.

★★★ "암호화폐도 국내주식과 동일한 기법 체계" - playbook.py 의 Playbook 을
그대로 재사용한다(옛 crypto_playbook.CryptoPlaybook 은 더 이상 쓰지
않는다). 어떤 기법을 켤지는 cfg.crypto.entry_order/exit_enabled 로
국내주식과 독립적으로 정하되, 실제 판정 클래스(변동성 돌파·손절익절 등)는
국내주식과 완전히 같은 것을 쓴다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from types import SimpleNamespace

from daytrader import news_guard, sizing
from daytrader.bithumb_broker import (
    CryptoPosition, CryptoPositionBook, LiveBithumbBroker, NotOwnedError, PaperBithumbBroker,
)
from daytrader.perf_stats import summarize_closed_trades
from daytrader.playbook import Bar, Playbook

log = logging.getLogger(__name__)


def _fmt_ts(ts: float) -> str:
    """★ 재개 예정 시각을 사람이 읽는 형식으로. 오늘 안이면 시:분만,
    날짜가 넘어가면 날짜까지 붙여 헷갈리지 않게 한다."""
    try:
        dt = datetime.fromtimestamp(ts)
        if dt.date() == datetime.now().date():
            return dt.strftime("%H:%M")
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return "-"


def _now_ts() -> float:
    return time.time()


def fetch_top_volume_markets(client, today: str, count: int = 10) -> list:
    """전날(KST) 일봉 거래대금 기준 상위 top_volume_count 종목의 KRW 마켓.
    1단계로 24시간 누적 거래대금(티커 몇 번)으로 상위 후보를 줄이고, 2단계로 그 후보만 일봉을
    받아 "전날" 값으로 순위를 매긴다 - 전체 마켓(수백 개)의 일봉을 다 받지 않기 위해서다."""
    if not (hasattr(client, "markets") and hasattr(client, "day_candles")):
        return []
    # 스테이블코인은 가격이 고정이라 변동성 매매 대상이 아니다(거래대금은 커도 신호가 안 나온다).
    stable = {"KRW-USDT", "KRW-USDC", "KRW-DAI", "KRW-TUSD", "KRW-BUSD", "KRW-FDUSD"}
    markets = [m.get("market") for m in (client.markets() or [])
               if str(m.get("market", "")).startswith("KRW-") and m.get("market") not in stable]
    if not markets:
        return []
    rows: list = []
    for i in range(0, len(markets), 100):
        rows += client.ticker(markets[i:i + 100]) or []
    rows.sort(key=lambda r: float(r.get("acc_trade_price_24h") or 0), reverse=True)
    want = max(1, int(count or 10))
    shortlist = [r["market"] for r in rows[: max(want * 4, want + 15)]]
    scored = []
    for m in shortlist:
        try:
            candles = client.day_candles(m, 2) or []
        except Exception:
            continue
        prev = [c for c in candles if str(c.get("candle_date_time_kst", ""))[:10] < today]
        if prev:
            scored.append((float(prev[0].get("candle_acc_trade_price") or 0), m))
        time.sleep(0.05)
    scored.sort(reverse=True)
    return [m for _v, m in scored[:want]]


def _closed_today(closed: list, limit: int = 500) -> list:
    """오늘(로컬 자정 이후)에 청산된 거래. exit_time 은 유닉스 초."""
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    out = [c for c in (closed or []) if (c.get("exit_time") or 0) >= start]
    return out[-limit:]


class CryptoState:
    """포지션 장부 + 청산 기록을 JSON 파일에 남긴다. 재시작해도 기존 보유를 잊지 않는다."""

    def __init__(self, path: str):
        self.path = path
        self.book = CryptoPositionBook()
        self.closed: list = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # ★ 깨진 상태 파일 때문에 죽지 않는다 - 백업하고 새로 시작한다.
            try:
                os.rename(self.path, f"{self.path}.corrupt-{int(time.time())}")
            except Exception:
                pass
            return
        if not isinstance(data, dict):
            return
        for market, p in (data.get("positions") or {}).items():
            # ★★★ 해외주식에서 겪은 것과 같은 버그 방지 - 상태 파일이
            # 손상되거나 예전 형식이면 값이 딕셔너리가 아닐 수 있고,
            # 그대로 p.get() 을 부르면 엔진이 시작조차 못 한다.
            if not isinstance(p, dict):
                log.warning("암호화폐 상태 복원 - %s 기록 형식이 올바르지 않아 건너뜁니다(%s).",
                            market, type(p).__name__)
                continue
            try:
                self.book.record_buy(CryptoPosition(
                    market=market, quantity=p["quantity"], entry_price=p["entry_price"],
                    entry_time=p["entry_time"], peak_price=p["peak_price"],
                    technique=p.get("technique", ""), order_id=p.get("order_id"),
                    name=p.get("name", market), theme=p.get("theme", "암호화폐"),
                    adds=int(p.get("adds") or 0), last_fill_price=float(p.get("last_fill_price") or p["entry_price"] or 0.0),
                    scaled_out=int(p.get("scaled_out") or 0), invested=float(p.get("invested") or 0.0),
                    realized=float(p.get("realized") or 0.0),
                    sold_qty=float(p.get("sold_qty") or 0.0), sold_value=float(p.get("sold_value") or 0.0),
                    conviction=float(p.get("conviction") if p.get("conviction") is not None else 0.5),
                ))
            except (KeyError, TypeError) as exc:
                log.warning("암호화폐 상태 복원 - %s 기록에 빠진 값이 있어 건너뜁니다: %s", market, exc)
        closed = data.get("closed", [])
        self.closed = [c for c in closed if isinstance(c, dict)] if isinstance(closed, list) else []

    def save(self) -> None:
        positions = {
            m: {
                "quantity": p.quantity, "entry_price": p.entry_price, "entry_time": p.entry_time,
                "peak_price": p.peak_price, "technique": p.technique, "order_id": p.order_id,
                "adds": p.adds, "last_fill_price": p.last_fill_price, "scaled_out": p.scaled_out,
                "invested": p.invested, "realized": p.realized, "sold_qty": p.sold_qty, "sold_value": p.sold_value, "conviction": p.conviction,
            }
            for m, p in self.book.all().items()
        }
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"positions": positions, "closed": self.closed}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


class CryptoEngine:
    def __init__(self, cfg, client=None, state_path: str | None = None, notifier=None):
        self.cfg = cfg
        self.crypto_cfg = cfg.crypto
        # ★★ "암호화폐도 국내주식과 동일한 기법 체계" - 같은 Playbook 클래스와
        # 같은 기법 레지스트리(ENTRY_TECHNIQUES/EXIT_TECHNIQUES)를 그대로
        # 쓰되, "어떤 기법을 켤지"는 cfg.crypto.entry_order/exit_enabled 로
        # 독립적으로 정한다 - 국내주식과 암호화폐가 서로 다른 기법 조합을
        # 켤 수 있어야 하기 때문이다(변동성이 전혀 다른 시장이므로).
        # ★★★ 실제로 겪은 버그 - Playbook 이 시장 구분 없이 항상
        # cfg.risk/cfg.exit.max_hold_minutes(국내주식 값)만 읽어서, 여기서
        # 넘기는 cfg.crypto.stop_loss_pct/take_profit_pct/trailing_pct/
        # max_hold_hours 가 화면에만 보이고 실제 청산 판정에는 전혀 안 쓰였다
        # ("코인 청산이 전부 90분 시간손절로만 찍힌다"의 정체). 코인 전용
        # risk 네임스페이스를 만들어 Playbook 에 명시적으로 넘긴다.
        crypto_risk = SimpleNamespace(
            stop_loss_pct=cfg.crypto.stop_loss_pct,
            take_profit_pct=cfg.crypto.take_profit_pct,
            trailing_stop_pct=cfg.crypto.trailing_pct,
            trailing_arm_pct=cfg.crypto.trailing_pct,
        )
        # ★★★ 실제로 겪은 문제 - ma_pullback/rsi_pullback 의 추세 필터
        # (ma_length=20)는 국내주식 1분봉 기준 "20분 추세"인데, 코인은
        # poll_seconds=30·1분봉을 쓰면서도 최대 24시간을 들고 갈 수 있다.
        # 20분짜리 이동평균으로는 그 시간 규모의 추세를 걸러내지 못하고
        # 잡음을 추세로 오인하기 쉽다(실제로 이 세션에서 ma_pullback 이
        # 하락장 중 반등을 잘못 잡아 연속 손절로 이어진 적이 있다). 코인만
        # 더 긴(60분) 추세 필터를 쓰도록 기법 파라미터를 오버라이드한다.
        crypto_entry_params = {
            "ma_pullback": {"ma_length": 60},
            "rsi_pullback": {"ma_length": 60},
        }
        self.playbook = Playbook(
            cfg, entry_order=cfg.crypto.entry_order, exit_enabled=cfg.crypto.exit_enabled,
            risk=crypto_risk, max_hold_minutes=cfg.crypto.max_hold_hours * 60.0,
            entry_params=crypto_entry_params, market="crypto",
            learning_mode=cfg.crypto.technique_learning_mode,
        )
        # ★★★ sim(시뮬레이션) 모드는 실제 빗썸 API 를 부르지 않는다.
        # 실제로 겪은 문제 - sim 인데도 진짜 API 를 호출해서, 키가 없거나
        # 네트워크가 막히면 아무것도 못 하고 조용히 실패했다("시뮬레이션이
        # 전혀 동작 안 한다"는 문의의 정체). 국내주식의 sim 이 SimClient 로
        # 합성 시세를 만들어 도는 것과 같은 취지다.
        if client is not None:
            self.client = client
        elif cfg.crypto.mode == "sim":
            from daytrader.sim_feed import SimFeedClient
            self.client = SimFeedClient(seed=getattr(cfg.simulation, "seed", 42))
        else:
            from daytrader.bithumb_api import BithumbClient
            self.client = BithumbClient(cfg.bithumb_access_key, cfg.bithumb_secret_key)

        state_path = state_path or os.path.join(cfg.state_dir, "crypto_state.json")
        self.state = CryptoState(state_path)
        self._last_prices: dict = {}  # 보유 코인의 마지막 현재가(화면용)

        # ★★★ 실거래는 crypto.live(국내주식 mode 와 완전히 독립) 이고
        # 빗썸 키가 등록되어 있을 때만. 국내주식을 live 로 켜도 이 값이
        # false 면 코인은 그대로 모의매매로 남는다 - 두 시장을 각자 따로
        # 결정한다.
        self.is_live = bool(cfg.crypto.live) and bool(cfg.bithumb_access_key and cfg.bithumb_secret_key)
        if self.is_live:
            self.broker = LiveBithumbBroker(self.client, book=self.state.book)
        else:
            # ★★★ 실제로 겪은 버그(국내주식 engine.py 에서 먼저 발견돼 고쳐진 것과 같은 종류) -
            # 재시작할 때마다 모의매매 현금이 그날까지의 손익과 무관하게 매번 총 투자금액
            # 그대로 초기화됐다("대시보드 현금 잔여금액이 안 맞다"는 문의의 원인). 보유 중인
            # 코인은 복원하면서 그 코인을 사는 데 쓴 현금은 되돌려주지 않은 것 - 포지션과 그
            # 값어치만큼의 현금이 동시에 존재하는 이중 계산이었다. 지금까지 실현손익을 더하고,
            # 지금 보유 중인 포지션에 묶여 있는 돈(매수 금액 - 이미 분할 매도로 받은 돈)을
            # 빼서 복원한다.
            realized = sum((c.get("pnl") or 0) for c in self.state.closed)
            locked = sum(max((p.invested or 0.0) - (p.sold_value or 0.0), 0.0) for p in self.state.book.all().values())
            self.broker = PaperBithumbBroker(
                starting_cash=self._budget() + realized - locked,
                book=self.state.book,
                commission_pct=cfg.crypto.commission_pct,
            )

        # ★ 텔레그램 알림은 notify.enabled_cfg() (국내주식 cfg.is_live 를 본다)
        # 를 거치지 않는다 - self.is_live(코인 전용 판정)로 직접 결정해서
        # force=True 로 보낸다. 그래야 "코인만 실거래" 조합에서도 알림이 간다.
        if notifier is not None:
            self.notifier = notifier
        else:
            from daytrader import notify
            self.notifier = notify.Telegram(cfg) if notify.configured_cfg(cfg) else None

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str = ""
        self.loop_count = 0

        # ★★★ "재빌드·재시작해도 실거래 이력은 API 와 연계해서 실제정보로
        # 업데이트해야 한다"는 요청 - 국내주식 engine.py 의 resume()/
        # reconcile() 과 같은 개념을 코인에도 둔다. 여태 이 엔진은 로컬
        # state 파일만 믿고 그대로 이어서 돌았는데, 실거래 중에 프로그램이
        # 꺼져 있던 사이 서버(빗썸 자체 스탑오더 등)에서 이미 청산됐을
        # 수 있다 - 그 경우를 실제 계좌 잔고와 대조해 바로잡는다.
        if self.is_live:
            self._reconcile_live_positions()

    def _reconcile_live_positions(self) -> None:
        """★ 계좌가 진실이다 - 로컬에는 보유 중으로 남아 있는데 실제
        잔고가 (수수료 차감분을 감안해도) 훨씬 적으면, 프로그램이 꺼진
        사이 서버에서 이미 팔린 것으로 보고 로컬 기록을 정리한다.
        정확한 체결가를 알 방법이 없어(주문/체결 내역 API 없이는) 지금
        시세로 추정한다 - 국내주식 reconcile() 이 못 찾을 때 쓰는
        방식과 같다.
        """
        try:
            accounts = self.client.accounts()
        except Exception as exc:
            log.warning("실거래 계좌 대조 실패 - 잔고 조회 오류: %s", exc)
            return
        balance_by_currency: dict = {}
        for a in accounts or []:
            if not isinstance(a, dict):
                continue
            currency = a.get("currency")
            if not currency:
                continue
            try:
                balance_by_currency[currency] = float(a.get("balance", 0) or 0)
            except (TypeError, ValueError):
                balance_by_currency[currency] = 0.0

        changed = False
        for market, pos in list(self.state.book.all().items()):
            currency = market.split("-")[-1] if "-" in market else market
            real_qty = balance_by_currency.get(currency, 0.0)
            # ★ 완전히 0이 아니어도 기록 수량의 10% 미만이면 "이미 정리됐다"고
            # 본다 - 수수료·반올림 오차 정도는 허용하되, 실제로 남아 있는
            # 보유는 건드리지 않는다.
            if pos.quantity <= 0 or real_qty >= pos.quantity * 0.1:
                continue
            try:
                exit_price = self._current_price(market)
                estimated_price = False
            except Exception:
                exit_price = pos.entry_price
                estimated_price = True
            pnl = (exit_price - pos.entry_price) * pos.quantity
            self.state.closed.append({
                "market": market, "quantity": pos.quantity, "entry_price": pos.entry_price,
                "exit_price": exit_price, "pnl": pnl,
                "reason": "계좌 대조 - 프로그램 밖에서 청산됨(체결가 추정)",
                "entry_time": pos.entry_time, "exit_time": _now_ts(),
                "entry_technique": pos.technique, "is_live": True, "estimated": True,
                "estimated_price": estimated_price,
            })
            self.state.book.remove(market)
            changed = True
            log.warning(
                "[실거래 계좌 대조] %s 는 서버에서 이미 정리된 것으로 보여 로컬 기록을 정리합니다 "
                "(내부 수량 %.8f, 실제 잔고 %.8f).", market, pos.quantity, real_qty,
            )
        if changed:
            self.state.save()

    def _notify(self, text: str) -> None:
        # ★★ 모의매매에서는 절대 보내지 않는다 - 가짜 체결 알림이 실제
        # 수익과 구분 안 되는 문제는 국내주식과 똑같이 겪는다.
        if not self.is_live or self.notifier is None:
            return
        try:
            self.notifier.send(text, force=True)
        except Exception:
            log.warning("암호화폐 알림 전송 실패", exc_info=True)

    # ━━ 시작/정지 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="crypto-engine", daemon=True)
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
                log.exception("암호화폐 엔진 루프 오류")
            self._stop.wait(self.crypto_cfg.poll_seconds)

    # ━━ 한 바퀴 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def run_once(self) -> None:
        self.loop_count += 1
        halted = self._halted_reason()
        if halted:
            self.last_error = halted
        # ★ halted 여도 루프 자체는 계속 돈다 - 그래야 보유 중인 포지션의
        # 청산(_manage_position)이 계속 처리된다. 신규 진입만 _process_market
        # 내부에서 막는다.
        self._maybe_refresh_top_volume()
        # ★ 국내주식(engine.py)만 기법별 실적을 playbook 에 채워주고 있어서, 암호화폐는
        # technique_learning_mode 를 뭘로 둬도 실적 가산점이 항상 중립(1.0)이었다
        # (2026-09-24 확인) - self.state.closed(코인 자체 청산 이력)로 여기서도 채워 준다.
        try:
            self.playbook.set_performance(
                summarize_closed_trades(self.state.closed, technique_field="entry_technique")["by_technique"])
        except Exception as exc:
            log.warning("기법별 실적 조회 실패(순수 신호 강도로만 판단합니다): %s", exc)
        for market in self.effective_watchlist():
            try:
                self._process_market(market)
            except Exception as exc:
                self.last_error = f"{market}: {exc}"
                log.warning("암호화폐 처리 실패 %s: %s", market, exc)
        self.state.save()

    def _halted_reason(self) -> str:
        """★ 국내주식의 일일 손실 한도·연속 손절 중단을 코인에도 둔다 -
        24시간 시장이라 "하루"를 "직전 24시간 롤링"으로 대신한다. 이미
        보유 중인 포지션의 청산까지 막지는 않는다 - 신규 진입만 멈춘다.

        ★★★ "언제 매매가 재개되는지 시간을 표기해달라"는 요청에 따라,
        중단 사유에 재개 예정 시각을 함께 담는다. 예전엔 "직전 24시간
        창에서 손절 기록이 빠질 때까지"라는 암묵적 규칙뿐이라 사용자가
        언제 풀리는지 알 방법이 없었다.
        """
        info = self.halt_info()
        return info.get("reason", "")

    def halt_info(self) -> dict:
        """★ 중단 여부·사유·재개 시각을 한 번에 계산한다. 화면(snapshot)과
        엔진 루프가 같은 값을 보게 하려고 한 곳에서 만든다."""
        now = _now_ts()
        cutoff = now - 24 * 3600
        recent = [c for c in self.state.closed if c.get("exit_time", 0) >= cutoff]
        if not recent:
            return {"halted": False, "reason": "", "resume_at": None}

        # ★★★ "일 최대 거래건수는 아예 보이지도 않고" 지적으로 발견 - 국내·해외주식엔
        # 있는 과매매 방지 장치(risk.daily_max_trades)가 암호화폐엔 없었다. 다른 한도와
        # 같은 직전 24시간 롤링 창으로 센다(가장 오래된 거래가 창을 벗어나면 자동 재개).
        max_trades = getattr(self.crypto_cfg, "daily_max_trades", 50)
        if max_trades and len(recent) >= max_trades:
            oldest = min(c.get("exit_time", now) for c in recent)
            resume = oldest + 24 * 3600
            return {
                "halted": True, "resume_at": resume,
                "reason": (f"직전 24시간 매매 {len(recent)}건으로 일일 최대 거래({max_trades}회)에 도달했습니다 "
                           f"(재개 예정 {_fmt_ts(resume)})"),
            }

        realized = sum(c.get("pnl") or 0 for c in recent)
        invested = sum((c.get("entry_price") or 0) * (c.get("quantity") or 0) for c in recent)
        if invested > 0 and realized < 0 and abs(realized) / invested >= self.crypto_cfg.daily_loss_limit_pct:
            # ★ 손실 한도는 24시간 롤링이므로, 가장 오래된 거래가 창을
            # 벗어나는 시각이 곧 재개 가능 시각이다.
            oldest = min(c.get("exit_time", now) for c in recent)
            resume = oldest + 24 * 3600
            return {
                "halted": True, "resume_at": resume,
                "reason": (f"직전 24시간 손실 한도({self.crypto_cfg.daily_loss_limit_pct*100:.0f}%) 도달 - "
                           f"신규 진입 중단 (재개 예정 {_fmt_ts(resume)})"),
            }

        # ★ 최근 청산부터 거꾸로 훑어 연속 손절 횟수를 센다.
        streak = 0
        last_loss_at = None
        for c in reversed(recent):
            if (c.get("pnl") or 0) < 0:
                streak += 1
                if last_loss_at is None:
                    last_loss_at = c.get("exit_time", now)
            else:
                break
        if streak >= self.crypto_cfg.consecutive_loss_halt:
            # ★★★ 설정한 쿨다운 시간이 지나면 재개한다 - "마지막 손절
            # 시각 + 쿨다운"이 정확한 재개 시각이다.
            hours = getattr(self.crypto_cfg, "loss_halt_cooldown_hours", 6.0)
            resume = (last_loss_at or now) + hours * 3600
            if now >= resume:
                return {"halted": False, "reason": "", "resume_at": None}
            return {
                "halted": True, "resume_at": resume,
                "reason": (f"연속 손절 {streak}회 - 신규 진입 중단(청산은 계속함) "
                           f"· 재개 예정 {_fmt_ts(resume)} (설정 {hours:g}시간 뒤)"),
            }
        return {"halted": False, "reason": "", "resume_at": None}

    def _budget(self) -> float:
        """암호화폐 총 투자금액(원). 예전 설정(코인당 금액)만 남아 있고 총액이 0 이면 그 곱을 쓴다."""
        c = self.crypto_cfg
        if getattr(c, "budget", 0):
            return float(c.budget)
        return float(getattr(c, "allocation_per_coin", 0.0) or 0.0) * max(1, int(getattr(c, "max_positions", 1) or 1))

    def _conviction_of(self, market: str):
        """거래대금 순위 근거의 크기(0~1). 전날 거래대금 상위로 뽑은 목록에서 앞쪽일수록 크다(0.4~0.8), 기본 목록은 중립 0.5."""
        auto = list(getattr(self, "_auto_watchlist", []) or [])
        if getattr(self.crypto_cfg, "auto_top_volume", False) and market in auto:
            return 0.8 - 0.4 * (auto.index(market) / max(1, len(auto) - 1))
        return 0.5

    def _cap(self) -> float:
        return sizing.position_cap(self._budget(), getattr(self.crypto_cfg, "max_positions", 1))

    def _process_market(self, market: str) -> None:
        pos = self.state.book.get(market)
        if pos is not None:
            self._manage_position(market, pos, allow_add=not self._halted_reason())  # ★ 중단 중에도 청산은 막지 않는다.
        elif not self._halted_reason():
            self._try_entry(market)

    def _fetch_bars(self, market: str, count: int = 60) -> list:
        unit = max(1, self.crypto_cfg.poll_seconds // 60) or 1
        if unit not in (1, 3, 5, 10, 15, 30, 60, 240):
            unit = 1
        rows = self.client.candles(market, unit=unit, count=count)
        return [
            Bar(
                ts=str(r.get("candle_date_time_kst") or r.get("timestamp") or ""),
                open=float(r.get("opening_price") or r.get("open") or 0),
                high=float(r.get("high_price") or r.get("high") or 0),
                low=float(r.get("low_price") or r.get("low") or 0),
                close=float(r.get("trade_price") or r.get("close") or 0),
                volume=float(r.get("candle_acc_trade_volume") or r.get("volume") or 0),
            )
            # ★★★ 해외주식에서 겪은 것과 같은 버그 방지("'str' object has
            # no attribute 'get'") - 응답에 딕셔너리가 아닌 항목이 섞여
            # 오면 r.get() 에서 죽고 매매가 통째로 실패한다.
            for r in rows or [] if isinstance(r, dict)
        ]

    def _current_price(self, market: str) -> float:
        rows = self.client.ticker([market])
        if not rows:
            raise RuntimeError(f"{market} 시세를 받지 못했습니다.")
        # ★ 딕셔너리인 항목을 골라 쓴다 - 형식이 어긋나도 죽지 않게.
        r = next((x for x in rows if isinstance(x, dict)), None)
        if r is None:
            raise RuntimeError(f"{market} 시세 응답 형식이 올바르지 않습니다({type(rows[0]).__name__}).")
        return float(r.get("trade_price") or 0)

    def _make_ctx(self, market: str, now) -> SimpleNamespace:
        # ★ 테마 관련 필드는 안 채운다 - playbook.py 전체가
        # getattr(ctx, key, 기본값) 으로 안전하게 접근하도록 이미 짜여
        # 있어서(원래 국내주식 코드), 없어도 그 항목만 자연히 "조건
        # 미충족"으로 처리되고 프로그램이 죽지 않는다(해외주식과 같은 원리).
        return SimpleNamespace(
            symbol=market, name=market, theme="암호화폐", now=now,
            prev_verdict=None, prev_verdicts=[], held_minutes=0, force_close=False,
        )

    # ━━ 감시 대상: 기본 목록 + 전날 거래대금 상위 + 보유 중인 코인 ━━━━━━━━━━━━━━━━━━━━━━━━
    def effective_watchlist(self) -> list:
        """감시 대상 = (전날 거래대금 상위 N 이 선정돼 있으면 그것, 아니면 기본 목록) + 보유 중인 코인.
        ★ 보유 중인 코인은 목록이 바뀌어도 반드시 계속 관리한다(청산 관리를 놓치면 안 된다)."""
        out: list = []
        auto = list(getattr(self, "_auto_watchlist", []) or [])
        base = auto if (getattr(self.crypto_cfg, "auto_top_volume", False) and auto) else list(self.crypto_cfg.watchlist)
        for m in base + list(self.state.book.all().keys()):
            if m not in out:
                out.append(m)
        return out

    def _kst_date(self) -> str:
        from datetime import timedelta, timezone
        return datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")

    def _maybe_refresh_top_volume(self) -> None:
        """하루 한 번(KST 날짜가 바뀐 뒤 처음) 전날 거래대금 상위 마켓을 다시 뽑는다. 호출이 수십 번이라
        루프를 막지 않게 백그라운드 스레드에서 하고, 실패하면 10분 뒤 다시 시도한다."""
        if not getattr(self.crypto_cfg, "auto_top_volume", False):
            return
        today = self._kst_date()
        if getattr(self, "_auto_date", None) == today or getattr(self, "_auto_running", False):
            return
        if time.time() < getattr(self, "_auto_retry_at", 0):
            return
        self._auto_running = True

        def work() -> None:
            try:
                picked = self._fetch_top_volume_markets(today)
                if picked:
                    self._auto_watchlist, self._auto_date = picked, today
                    log.info("암호화폐 감시 대상 선정(전날 거래대금 상위 %d개): %s", len(picked), ", ".join(picked))
                    # ★ 오늘 새로 뽑힌 종목에 실험실 백테스트를 자동으로 돌려(이미 백그라운드
                    # 스레드 안이다), "이 종목엔 이 기법이 최근 더 잘 맞았다"는 결과를 진입 기법
                    # 채점에 가산점으로 반영한다(technique_prefs.py). 가짜 시세(sim/replay)는 건너뛴다.
                    if not self.cfg.uses_fake_data:
                        try:
                            from daytrader import auto_backtest
                            cand_rows = [{"symbol": m, "name": m.replace("KRW-", ""), "theme": "거래대금 상위"} for m in picked]
                            auto_backtest.maybe_trigger(self.cfg, self.client, "crypto", cand_rows)
                        except Exception as exc:
                            log.warning("실험실 자동 백테스트 트리거 실패(무시하고 계속): %s", exc)
                else:
                    self._auto_retry_at = time.time() + 600
            except Exception as exc:
                self._auto_retry_at = time.time() + 600
                log.warning("암호화폐 거래대금 상위 선정 실패(10분 뒤 재시도, 기존 목록 유지): %s", exc)
            finally:
                self._auto_running = False

        threading.Thread(target=work, daemon=True).start()

    def _fetch_top_volume_markets(self, today: str) -> list:
        return fetch_top_volume_markets(
            self.client, today, int(getattr(self.crypto_cfg, "top_volume_count", 10) or 10))

    def _in_reentry_cooldown(self, market: str) -> bool:
        """직전 청산이 손실이고 아직 쿨다운 안이면 True - 손절 직후의 재진입을 막는다."""
        minutes = float(getattr(self.crypto_cfg, "reentry_cooldown_minutes", 0) or 0)
        if minutes <= 0:
            return False
        for c in reversed(self.state.closed):
            if c.get("market") == market:
                return (c.get("pnl") or 0) < 0 and (time.time() - (c.get("exit_time") or 0)) < minutes * 60
        return False

    def _try_entry(self, market: str) -> None:
        if self._in_reentry_cooldown(market):
            return
        cap = int(getattr(self.crypto_cfg, "max_positions", 0) or 0)
        if cap and len(self.state.book.all()) >= cap:
            return
        # ★ ma_pullback/rsi_pullback 이 이제 ma_length=60(위 crypto_entry_params)을
        # 쓰므로, 필요 봉 수(ma_length+volume_window+2=82)보다 넉넉히 받아온다 -
        # 부족하면 "확보된 봉 수" 조건에 걸려 이 기법이 영원히 통과하지 못한다.
        bars = self._fetch_bars(market, count=140)
        if len(bars) < 2:
            return
        current_price = bars[-1].close

        # ★★★ "암호화폐는 거래량이 적은 경우 거래를 하지 않도록 보완해" - 유동성이 얇으면
        # 호가 간격이 넓어 슬리피지가 크고, 소량 체결에도 가격이 크게 튀어 손절·익절 판단이
        # 왜곡된다. 코인마다 단위 가치가 달라 원화 환산 거래대금(종가×거래량)으로 거르고,
        # 봉 하나만 보면 우연히 거래가 뜸했을 수 있어 최근 여러 봉의 평균을 본다.
        min_value = getattr(self.crypto_cfg, "min_trading_value_krw", 0) or 0
        if min_value:
            n = max(1, int(getattr(self.crypto_cfg, "min_trading_value_bars", 10) or 10))
            recent = bars[-n:]
            avg_value = sum((b.close or 0) * (b.volume or 0) for b in recent) / len(recent)
            if avg_value < min_value:
                return

        ctx = self._make_ctx(market, datetime.now())

        winner, _all_verdicts = self.playbook.evaluate_entry(bars, ctx)
        if winner is None:
            return

        if self.crypto_cfg.mode == "web":
            # ★ 관찰 모드 - 신호는 계산하지만 실제로 사지 않는다. 국내주식의
            # "web(관찰)" 모드와 같은 철학이다.
            log.info("[암호화폐 매수 신호·관찰 전용] %s @ %.0f원 (%s) - 실제로 사지 않습니다.",
                     market, current_price, winner.technique_label)
            return

        # ★★★ 총 투자금액을 동시 보유 수로 나눈 것이 종목당 한도, 첫 매수는 그 절반 × 신호 강도 배수(0.6~1.4).
        # ★ 사기 직전 뉴스 위험 필터(거르기 전용, 실패하면 통과)
        allowed, why = news_guard.get_guard(self.cfg).gate(market, market.split("-")[-1], "crypto")
        if not allowed:
            self.last_error = f"{market}: {why}"
            return
        strength = sizing.signal_strength(winner)
        conviction = self._conviction_of(market)
        vol = sizing.vol_ratio(bars, self.crypto_cfg.stop_loss_pct)
        krw_amount = sizing.entry_amount(self._cap(), strength, self.cfg.sizing, conviction=conviction, vol=vol)
        if krw_amount < 5000:
            self.last_error = f"{market}: 투자금액이 너무 작아 매수하지 않습니다(종목당 한도 {self._cap():,.0f}원)"
            return
        # ★★★ 해외주식에서 겪은 것과 같은 버그 방지 - 브로커가 현금을
        # None 으로 돌려주면 비교에서 TypeError 가 나고 매수가 통째로
        # 실패한다. 알 수 없으면 0 으로 본다(모르는데 있다고 가정하고
        # 사는 것보다 안 사는 쪽이 안전하다).
        try:
            available = float(self.broker.cash() or 0)
        except (TypeError, ValueError):
            available = 0.0
        if krw_amount > available:
            self.last_error = f"{market}: 현금 부족으로 매수 보류"
            return

        pos = self.broker.buy(market, krw_amount, current_price, technique=winner.technique)
        if pos:
            pos.conviction = 0.5 if conviction is None else conviction
            log.info("[암호화폐 매수] %s %.8f개 @ %.0f원 (%s · 신호 %.2f·거래대금 근거 %.2f·변동성 %.2f배 → %.0f원)", market, pos.quantity,
                     current_price, winner.technique_label, strength, pos.conviction, vol if vol is not None else 1.0, krw_amount)
            self._notify(
                f"🪙 <b>[암호화폐 매수]</b> {market}\n{pos.quantity:.8f}개 @ {current_price:,.0f}원\n{winner.technique_label}"
            )

    def _manage_position(self, market: str, pos, allow_add: bool = False) -> None:
        current_price = self._current_price(market)
        self._last_prices[market] = current_price
        self.state.book.update_peak(market, current_price)
        pos = self.state.book.get(market)  # update_peak 이후 최신 peak 반영된 것을 다시 읽는다.

        bars = self._fetch_bars(market, count=140)
        # ★★★ 진입 시각이 비어 있으면(상태 파일 손상 등) 뺄셈에서 죽어
        # 청산 관리가 멈춘다 - 못 파는 건 손실로 직결된다. 0 으로 본다
        # (시간 기반 청산만 판단을 보류하고 나머지는 정상 동작한다).
        _entry_ts = pos.entry_time
        held_minutes = ((_now_ts() - _entry_ts) / 60.0) if _entry_ts else 0.0
        ctx = self._make_ctx(market, datetime.now())
        ctx.held_minutes = held_minutes
        sz = self.cfg.sizing
        ctx.scale_out = bool(sz.scale_out)  # 분할 매도를 쓰면 고정 익절(전량)은 끄고 나눠서 판다.

        entry = pos.entry_price or 0.0
        take_pct = (sizing.dynamic_take_profit_pct(self.crypto_cfg.take_profit_pct, bars, sz)
                    if self.crypto_cfg.dynamic_take_profit else self.crypto_cfg.take_profit_pct)
        if self.crypto_cfg.technique_learning_mode == "entry_exit_pref":
            from daytrader import exit_efficiency
            widen, _why = exit_efficiency.widen_multiplier(self.cfg, "crypto", market)
            take_pct *= widen
        step = sizing.exit_step(scaled_out=pos.scaled_out, entry=entry, price=current_price, sz=sz,
                                take_pct=take_pct, conviction=pos.conviction,
                                vol=sizing.vol_ratio(bars, self.crypto_cfg.stop_loss_pct))
        if step is None and sizing.breakeven_hit(scaled_out=pos.scaled_out, entry=entry, price=current_price, sz=sz):
            self._execute_exit(market, pos, current_price, "breakeven_stop")
            return

        verdict = self.playbook.evaluate_exit(pos, bars, current_price, ctx)
        # ★★★ 실제로 겪은 버그 - Playbook.evaluate_exit() 은 모든 청산
        # 기법이 조건 미충족이면 verdict 자체가 None 이다(winner_key 가
        # 없으면 return None). 이걸 안 가리고 바로 verdict.ok 를 읽으면
        # "NoneType 객체에 ok 속성이 없다" 로 죽는다 - 국내주식 engine.py
        # 의 manage_positions() 는 처음부터 None 체크를 하고 있었는데,
        # 암호화폐가 같은 Playbook 을 재사용하게 되면서 이 체크를 빠뜨렸다.
        if verdict is not None and verdict.ok:
            self._execute_exit(market, pos, current_price, verdict.technique)  # 손절·추적·시간 청산은 전량
            return
        if step is not None:
            self._execute_exit(market, pos, current_price, step[1], fraction=step[0])
            return
        if allow_add:
            self._maybe_add(market, pos, current_price, bars, ctx)

    def _maybe_add(self, market: str, pos, price: float, bars, ctx) -> None:
        """이익 중인 코인에만 추가 매수(피라미딩) - 조건은 해외주식과 같다(sizing.py)."""
        sz = self.cfg.sizing
        if not sizing.add_due(adds=pos.adds, last_fill=pos.last_fill_price or pos.entry_price, avg_entry=pos.entry_price,
                              price=price, scaled_out=pos.scaled_out, sz=sz, stop_pct=self.crypto_cfg.stop_loss_pct):
            return
        try:
            winner, _ = self.playbook.evaluate_entry(bars, ctx)
        except Exception:
            return
        if winner is None or self.crypto_cfg.mode == "web":
            return
        invested = pos.invested or (pos.entry_price * pos.quantity)
        amount = sizing.add_amount(self._cap(), invested, pos.adds, sz, conviction=pos.conviction,
                                   vol=sizing.vol_ratio(bars, self.crypto_cfg.stop_loss_pct))
        try:
            available = float(self.broker.cash() or 0)
        except (TypeError, ValueError):
            available = 0.0
        if amount < 5000 or amount > available:
            return
        added = self.broker.add(market, amount, price)
        if added is not None:
            log.info("[암호화폐 추가 매수] %s @ %.0f원 (%d/%d회, 평균 %.0f원)", market, price, added.adds, sz.max_adds, added.entry_price)
            self._notify(f"🪙 <b>[암호화폐 추가 매수]</b> {market}\n@ {price:,.0f}원 · {added.adds}/{sz.max_adds}회 · 평균 {added.entry_price:,.0f}원")

    def _execute_exit(self, market: str, pos, current_price: float, reason: str, fraction: float = 1.0) -> bool:
        """★ _manage_position(조건부 청산)과 liquidate_all(강제 전량 청산)이
        같은 매도 절차를 공유한다 - 매도 로직을 두 곳에 따로 두면 한쪽만
        고치고 다른 쪽을 빠뜨리는 실수가 생기기 쉽다.
        """
        try:
            result = self.broker.sell(market, current_price, reason=reason, fraction=fraction)
        except NotOwnedError:
            # ★★★ 이 엔진이 안 산 코인이라 매도가 거부됐다 - 절대 조용히
            # 넘어가지 않는다. 로직 어딘가 잘못돼 여기 왔다는 뜻이다.
            log.error("★★★ %s 매도가 NotOwnedError 로 거부됨 - 이 엔진이 사지 않은 코인입니다.", market)
            self.last_error = f"{market}: NotOwnedError - 이 엔진이 사지 않은 암호화폐 매도 시도가 거부됨"
            return False

        result = result if isinstance(result, dict) else {}
        partial = bool(result.get("partial"))
        sold = result.get("quantity", pos.quantity)
        if partial:
            # 나눠 파는 중 - 기록은 거래가 끝났을 때 한 건으로 합쳐 남긴다(건수·승률이 부풀지 않게). 여기서는 누적만.
            pos.sold_qty = (pos.sold_qty or 0.0) + sold
            pos.sold_value = (pos.sold_value or 0.0) + sold * current_price
            pnl = result.get("pnl")
            log.info("[암호화폐 분할 매도] %s @ %.0f원 (%s) - 남은 %.8f개", market, current_price, reason, pos.quantity)
            self._notify(
                f"🪙 <b>[암호화폐 분할 매도]</b> {market}\n@ {current_price:,.0f}원 · 손익 {pnl:+,.0f}원 · 남은 {pos.quantity:.8f}개\n{reason}"
            )
            return True
        total_qty = (pos.sold_qty or 0.0) + sold
        avg_exit = (((pos.sold_value or 0.0) + sold * current_price) / total_qty) if total_qty else current_price
        total_pnl = result.get("total_pnl", result.get("pnl"))
        parts = (getattr(pos, "scaled_out", 0) or 0)
        record = {
            "market": market, "quantity": total_qty,
            "entry_price": pos.entry_price, "exit_price": avg_exit,
            "pnl": total_pnl,
            "reason": reason + (f" (분할 매도 {parts}회 후)" if parts else ""), "entry_time": pos.entry_time, "exit_time": _now_ts(),
            # ★★★ reason 은 청산(매도) 사유다 - 어떤 기법으로 샀는지는
            # Position 에만 있고 거래 기록엔 없어서 화면에서 볼 수 없었다.
            "entry_technique": pos.technique,
            "is_live": self.is_live,  # ★ 성과/매매일지에서 실거래·모의를 구분하려면 거래 기록 자체에 있어야 한다.
        }
        if getattr(pos, "adds", 0) or parts:
            record["adds"] = getattr(pos, "adds", 0)
            record["scaled_out"] = parts
        self.state.closed.append(record)
        if self.crypto_cfg.technique_learning_mode == "entry_exit_pref" and pos.entry_price:
            from daytrader import exit_efficiency
            mfe_pct = (pos.peak_price or pos.entry_price) / pos.entry_price - 1
            realized_pct = avg_exit / pos.entry_price - 1
            exit_efficiency.record(self.cfg, "crypto", market, realized_pct, mfe_pct)
        log.info("[암호화폐 매도] %s @ %.0f원 (%s)", market, current_price, reason)
        pnl_text = f"{total_pnl:+,.0f}원" if total_pnl is not None else "-"
        self._notify(
            f"🪙 <b>[암호화폐 매도]</b> {market}\n@ {current_price:,.0f}원 · 손익 {pnl_text}\n{reason}"
        )
        return True

    def liquidate_all(self) -> int:
        """★ "전량 청산 후 정지" - 지금 보유 중인 모든 코인을 조건과
        무관하게 즉시 시장가로 정리한다. 몇 건을 정리했는지 돌려준다.
        """
        count = 0
        for market, pos in list(self.state.book.all().items()):
            try:
                current_price = self._current_price(market)
            except Exception as exc:
                self.last_error = f"{market}: 청산 중 시세 조회 실패 - {exc}"
                continue
            if self._execute_exit(market, pos, current_price, "force_close"):
                count += 1
        self.state.save()
        return count

    # ━━ 화면용 요약 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def snapshot(self) -> dict:
        positions = {
            m: {
                "quantity": p.quantity, "entry_price": p.entry_price,
                "peak_price": p.peak_price, "technique": p.technique,
                "last_price": self._last_prices.get(m),
                "adds": p.adds, "scaled_out": p.scaled_out, "invested": p.invested,
                "held_hours": round((_now_ts() - p.entry_time) / 3600.0, 2),
            }
            for m, p in self.state.book.all().items()
        }
        return {
            "running": self.is_running(), "is_live": self.is_live, "mode": self.crypto_cfg.mode,
            "cash": self.broker.cash(), "positions": positions,
            "closed_count": len(self.state.closed), "closed": self.state.closed[-10:],
            # ★ 대시보드 지표·손익 추이는 국내주식(오늘 거래)과 같은 "오늘" 기준이어야 한다 -
            #   closed 는 최근 10건이라 그걸로 세면 며칠 전 거래가 섞이거나 오늘 거래가 잘린다.
            "closed_today": _closed_today(self.state.closed),
            # ★★★ "승률·손익비가 화면에 안 보인다" - closed 는 최근 10건만
            # 잘라 보내니 화면이 직접 집계할 수 없다. 전체 기록 기준 요약을
            # 서버에서 미리 계산해 함께 보낸다(국내주식·해외주식과 지표를
            # 맞추려면 두 엔진이 같은 계산 함수를 써야 한다 - perf_stats).
            "stats": summarize_closed_trades(self.state.closed, technique_field="entry_technique", symbol_field="market"),
            "loop_count": self.loop_count, "last_error": self.last_error,
            "watchlist": self.effective_watchlist(),
            # ★★★ "여러 기법을 켰는데 화면엔 변동성 돌파만 나온다"는 지적 -
            # 실제로는 켜진 기법을 전부 평가하는데, 화면 문구가 암호화폐
            # 초기(변동성 돌파 하나뿐이던 시절) 그대로 남아 있었다.
            # 지금 켜진 기법을 화면에 알려줘서 정확히 표시하게 한다.
            "entry_techniques": [t.label for t in self.playbook.entries],
            # ★★★ "언제 매매가 재개되는지 사용자가 알 수 있도록" - 중단
            # 여부와 재개 예정 시각을 화면에 그대로 넘긴다.
            "halt": self.halt_info(),
        }
