"""텔레그램 알림.

★★ 실거래(live)에서만 보낸다. 시뮬레이션·연습·모의매매는 기록만 남기고
알림도 일·월·연 결산도 내지 않는다. 가짜 체결로 "매도 +23,100원"이
휴대폰에 오면 실제 수익과 구분이 안 되고, 그 혼동이 실제 돈을 잘못
판단하게 만든다.

★ 토큰이나 채팅 ID 가 없으면 아무것도 하지 않는다. 오류도 내지 않는다.
"""

from __future__ import annotations

import queue
import threading
import time

from daytrader import netutil

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3500  # 텔레그램 제한은 4096 이지만 여유를 둔다.
QUEUE_MAX = 100
SEND_INTERVAL = 0.35

MODE_LABELS = {"sim": "시뮬레이션", "replay": "리플레이", "web": "관찰", "paper": "모의매매", "live": "실거래"}


def _truncate(text: str) -> str:
    if len(text) <= MAX_LEN:
        return text
    return text[: MAX_LEN - 20] + "\n... (이하 생략)"


def _post_raw_sync(token: str, chat_id: str, text: str) -> tuple:
    """POST 한 통을 동기로 보낸다. ★ 계좌번호·API 키·토큰은 메시지에 절대
    넣지 않는다 - 텔레그램 메시지는 서버를 거치고 휴대폰에 남는다.
    """
    if not (token and chat_id):
        return False, "토큰 또는 채팅 ID 가 없습니다."
    try:
        sess = netutil.make_session()
        resp = sess.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": _truncate(text), "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=8,
        )
        if resp.status_code == 200:
            return True, ""
        # ★★★ "등록했는데 테스트 전송이 안 된다" - 텔레그램이 주는 원문
        # ("Bad Request: chat not found")만으로는 무엇을 고쳐야 할지 알 수
        # 없다. 흔한 원인을 코드별로 짚어 준다.
        detail = resp.text[:200]
        hint = ""
        if resp.status_code == 401:
            hint = " — 봇 토큰이 틀렸습니다. @BotFather 에서 받은 값을 다시 복사해 넣으세요."
        elif resp.status_code == 400 and "chat not found" in detail:
            hint = (" — 채팅 ID 가 틀렸거나, 봇과 대화를 시작한 적이 없습니다. "
                    "텔레그램에서 내 봇을 찾아 /start 를 한 번 보낸 뒤 다시 시도하세요.")
        elif resp.status_code == 400 and "chat_id" in detail:
            hint = " — 채팅 ID 형식이 올바르지 않습니다(숫자여야 하며, 그룹은 앞에 -가 붙습니다)."
        elif resp.status_code == 403:
            hint = " — 봇이 차단됐거나 그룹에서 내보내졌습니다. 봇과의 대화를 다시 여세요."
        elif resp.status_code == 429:
            hint = " — too many requests(요청이 너무 잦음). 잠시 후 다시 시도하세요."
        return False, f"HTTP {resp.status_code}: {detail}{hint}"
    except Exception as exc:
        return False, str(exc)


def configured_cfg(cfg) -> bool:
    return bool(cfg and cfg.notify.telegram_token and cfg.notify.telegram_chat_id)


def enabled_cfg(cfg) -> bool:
    """'실거래에서 자동 알림을 보낼지'의 순수 판정. Telegram 인스턴스(스레드) 없이도 쓸 수 있다.

    ★★★ "텔레그램 메시지를 못 보낸다"의 흔한 원인이 여기였다 - 기본은
    실거래(live)에서만 보낸다. 연습 매매 알림이 실제 주문과 섞이면
    위험해서 둔 안전장치다. notify.notify_in_practice 를 켜면 연습
    모드에서도 보낸다(메시지에 [연습] 표시가 붙는다).
    """
    if cfg is None or not configured_cfg(cfg) or not cfg.notify.enabled:
        return False
    if cfg.is_live:
        return True
    return bool(getattr(cfg.notify, "notify_in_practice", False))


def why_off_cfg(cfg) -> str:
    if cfg is None:
        return "설정을 불러오지 못했습니다."
    if not configured_cfg(cfg):
        return "텔레그램 토큰·채팅 ID 가 없습니다. [준비·연결] → 텔레그램 알림에서 등록하세요."
    if not cfg.notify.enabled:
        return "알림이 꺼져 있습니다. [설정] → 정보·알림에서 켜세요."
    if not cfg.is_live:
        # ★ 해결 방법까지 알려준다 - 예전엔 "실거래가 아닙니다"에서 끝나서
        #   연습 모드에서 알림을 받는 방법이 있는지 알 수 없었다.
        return ("지금은 연습 모드(모의매매·시뮬레이션)라 알림을 보내지 않습니다 - "
                "연습 중에도 받으려면 [설정] → 정보·알림에서 '연습 모드에서도 알림 보내기'를 켜세요.")
    return ""


def status_cfg(cfg, sent: int = 0, failed: int = 0, last_error: str = "") -> dict:
    enabled = enabled_cfg(cfg)
    return {
        "configured": configured_cfg(cfg), "enabled": enabled,
        "why_off": "" if enabled else why_off_cfg(cfg),
        "mode": cfg.mode if cfg else None, "live_only": True,
        "events": list(cfg.notify.events) if cfg else [],
        "sent": sent, "failed": failed, "last_error": last_error,
        "hint": "실거래 모드에서만 매매·마감 알림이 자동으로 나갑니다. "
                "다른 모드에서는 기록만 남고 알림은 나가지 않습니다.",
    }


class Telegram:
    """★ 전역 싱글턴을 만들지 않는다 (A-18). 엔진마다 자기 인스턴스를 갖는다.
    실험실이 엔진을 동시에 여러 개 띄우면 싱글턴은 앞 엔진 설정을 덮는다.
    """

    def __init__(self, cfg=None):
        self.cfg = cfg
        self._queue: queue.Queue = queue.Queue()
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        self._stop = False
        self._thread = threading.Thread(target=self._worker, name="telegram-notify", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    # ━━ 판정 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def configured(self) -> bool:
        return configured_cfg(self.cfg)

    def enabled(self) -> bool:
        """'실거래에서 자동 알림을 보낼지'의 판정이다. 연습 모드에서는 언제나 False.
        ★★ 이 함수를 테스트 전송 판정에 쓰면 연습 모드에서 연결 확인 자체가
        영구히 불가능해진다 (A-26). 자동 알림과 수동 전송은 다른 규칙이다.
        """
        return enabled_cfg(self.cfg)

    def why_off(self) -> str:
        return why_off_cfg(self.cfg)

    def wants(self, event: str) -> bool:
        return self.cfg is not None and event in (self.cfg.notify.events or [])

    def status(self) -> dict:
        return status_cfg(self.cfg, sent=self.sent, failed=self.failed, last_error=self.last_error)

    # ━━ 전송 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _record(self, ok: bool, error: str = "") -> None:
        if ok:
            self.sent += 1
        else:
            self.failed += 1
            self.last_error = error

    def _worker(self) -> None:
        while not self._stop:
            try:
                text = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            if self.cfg is not None:
                ok, err = _post_raw_sync(self.cfg.notify.telegram_token, self.cfg.notify.telegram_chat_id, text)
                self._record(ok, err)
            time.sleep(SEND_INTERVAL)

    def send(self, text: str, event: str | None = None, force: bool = False) -> None:
        """★ 큐에 넣고 즉시 반환한다 - 엔진을 붙잡지 않는다.
        force=True 면 wants()/enabled() 를 건너뛰고 동기로 즉시 보낸다
        (사람이 버튼을 눌렀는데 갔는지 안 갔는지 모르면 버튼이 고장난 것과 같다).
        """
        if not force:
            if not self.enabled():
                return
            if event is not None and not self.wants(event):
                return
            # ★★★ 연습 모드에서 보내는 알림에는 반드시 표시를 붙인다 -
            # 실거래 알림과 똑같이 생기면 "실제로 샀다"고 오해할 수 있고,
            # 그건 돈이 걸린 오해다.
            if self.cfg is not None and not self.cfg.is_live:
                text = "🧪 [연습] " + text
            if self._queue.qsize() >= QUEUE_MAX:
                try:
                    self._queue.get_nowait()  # 큐가 차면 오래된 것부터 버린다.
                except queue.Empty:
                    pass
            self._queue.put(text)
            return

        token = self.cfg.notify.telegram_token if self.cfg else ""
        chat_id = self.cfg.notify.telegram_chat_id if self.cfg else ""
        ok, err = _post_raw_sync(token, chat_id, text)
        self._record(ok, err)

    def send_now(self, text: str, token: str | None = None, chat_id: str | None = None) -> tuple:
        """설정 화면의 '테스트 전송'용. 동기 전송이고 실거래 제한을 우회한다
        (연결 확인은 모드와 무관하게 되어야 하므로). token/chat_id 를 직접
        받을 수 있어 저장하지 않은 값도 바로 테스트할 수 있다.
        """
        token = token if token is not None else (self.cfg.notify.telegram_token if self.cfg else "")
        chat_id = chat_id if chat_id is not None else (self.cfg.notify.telegram_chat_id if self.cfg else "")
        if not (token and chat_id):
            return False, "토큰과 채팅 ID 를 모두 넣어야 보낼 수 있습니다."
        ok, err = _post_raw_sync(token, chat_id, text)
        self._record(ok, err)
        return ok, err

    # ━━ 메시지 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _mode_tag(self) -> str:
        mode_label = MODE_LABELS.get(self.cfg.mode, self.cfg.mode) if self.cfg else "-"
        if self.cfg and not self.cfg.is_live:
            return f"{mode_label} · 가상 성적입니다"
        return mode_label

    def trade(self, kind: str, t: dict, force: bool = False) -> None:
        tag = self._mode_tag()
        if kind == "entry":
            text = (
                f"🟢 매수 [{tag}]\n"
                f"{t.get('name', '')}({t.get('symbol', '')}) / {t.get('theme', '')}\n"
                f"{t.get('qty', 0)}주 × {t.get('entry', 0):,.0f}원\n"
                f"기법: {t.get('technique', '')}\n"
                f"{t.get('why', '')}"
            )
        else:
            pnl = t.get("pnl", 0)
            entry = t.get("entry", 0) or 1
            pct = (pnl / (entry * t.get("qty", 1))) if entry and t.get("qty") else 0.0
            text = (
                f"🔴 매도 [{tag}]\n"
                f"{t.get('name', '')}({t.get('symbol', '')})\n"
                f"{t.get('qty', 0)}주 {entry:,.0f}원 → {t.get('exit', 0):,.0f}원\n"
                f"손익 {pnl:+,.0f}원 ({pct * 100:+.2f}%)\n"
                f"사유: {t.get('reason', '')}\n"
                f"기법: {t.get('technique', '')}"
            )
        self.send(text, event="trade", force=force)

    def daily(self, day: str, s: dict, force: bool = False) -> None:
        text = (
            f"📈 일마감 {day} [{self._mode_tag()}]\n"
            f"손익 {s.get('pnl', 0):+,.0f}원 · 거래 {s.get('trades', 0)}건 · "
            f"승률 {s.get('win_rate', 0) * 100:.0f}%\n"
            f"잔고 {s.get('balance', 0):,.0f}원\n"
        )
        if s.get("best_symbol"):
            text += f"최고 {s['best_symbol']} {s.get('best_pnl', 0):+,.0f}원\n"
        if s.get("worst_symbol"):
            text += f"최저 {s['worst_symbol']} {s.get('worst_pnl', 0):+,.0f}원\n"
        if s.get("open_positions"):
            text += f"⚠️ 미청산 종목: {', '.join(s['open_positions'])}\n"
        self.send(text, event="daily", force=force)

    def monthly(self, month: str, s: dict, force: bool = False) -> None:
        text = (
            f"📈 월마감 {month} [{self._mode_tag()}]\n"
            f"손익 {s.get('pnl', 0):+,.0f}원 · 수익률 {s.get('return_pct', 0) * 100:+.2f}% · "
            f"승률 {s.get('win_rate', 0) * 100:.0f}%\n"
            f"매매일 {s.get('trading_days', 0)}일 (수익일 {s.get('up_days', 0)}일)\n"
        )
        if s.get("best_day"):
            text += f"최고일 {s['best_day']} {s.get('best_day_pnl', 0):+,.0f}원\n"
        if s.get("worst_day"):
            text += f"최저일 {s['worst_day']} {s.get('worst_day_pnl', 0):+,.0f}원\n"
        if s.get("best_technique"):
            text += f"최고 기법: {s['best_technique']}\n"
        self.send(text, event="monthly", force=force)

    def yearly(self, year: str, s: dict, force: bool = False) -> None:
        text = (
            f"🎯 연마감 {year} [{self._mode_tag()}]\n"
            f"손익 {s.get('pnl', 0):+,.0f}원 · 수익률 {s.get('return_pct', 0) * 100:+.2f}% · "
            f"승률 {s.get('win_rate', 0) * 100:.0f}%\n"
            f"수익월 {s.get('up_months', 0)}개월\n"
        )
        if s.get("best_month"):
            text += f"최고월 {s['best_month']} {s.get('best_month_pnl', 0):+,.0f}원\n"
        if s.get("worst_month"):
            text += f"최저월 {s['worst_month']} {s.get('worst_month_pnl', 0):+,.0f}원\n"
        self.send(text, event="yearly", force=force)
