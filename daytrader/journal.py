"""로그는 흘러가지만 일지는 남는다. 모든 판단에 사람이 읽을 수 있는
설명을 같이 적어서 나중에 '왜 저걸 샀지?'를 되짚을 수 있게 한다.
SQLite(daytrader.db)에 담는다 - decisions.jsonl 이 1.8MB 까지 자라 매번 파일 전체를
다시 읽고 파싱하던 것을, 인덱스가 걸린 표 조회로 바꿔 데이터가 쌓여도 빠르게 유지한다
(db.py 상단의 "왜 SQLite 인가" 참고).
"""

from __future__ import annotations

import os

from daytrader import db
from daytrader.timeutil import iso, now_kst, with_josa

KIND_LABELS = {
    "theme_scan": "테마 선정", "pick": "후보 선정", "reject": "후보 제외",
    "watch": "관찰 중", "evaluate": "판정", "buy": "매수", "sell": "매도",
    "halt": "매매 중단", "session": "세션", "reconcile": "계좌 대조",
    "overnight": "오버나이트 판단",
}


class Journal:
    def __init__(self, state_dir, mode: str = "sim", clock=None):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        db.get_connection(state_dir)  # 스키마 준비 + 기존 decisions.jsonl 1회성 가져오기
        self.mode = mode
        self.clock = clock
        self._last_watch: dict[str, str] = {}  # symbol -> 직전 reason(괄호 앞부분)
        self._last_blocked: dict[tuple, tuple] = {}  # (symbol, technique) -> 직전 blocked_by

    def _now(self):
        # ★ 시뮬레이션은 실제 시계가 아니라 가상 장중 시각을 써야 일지가 앞뒤로 맞는다.
        return self.clock.now() if self.clock is not None else now_kst()

    def write(self, kind, explain, *, at=None, symbol="", name="", theme="", detail=None) -> None:
        at = at or iso(self._now())
        row = {
            "at": at, "date": at[:10], "time": at[11:19],
            "mode": self.mode, "kind": kind,
            "symbol": symbol, "name": name, "theme": theme,
            "explain": explain, "detail": detail or {},
        }
        db.insert_json_row(self.state_dir, "journal", {
            "at": row["at"], "date": row["date"], "mode": row["mode"],
            "kind": row["kind"], "symbol": row["symbol"],
        }, row)

    def log(self, kind, **detail) -> None:
        """스크리너 등 다른 모듈이 짧게 남길 때 쓰는 편의 메서드."""
        explain = detail.pop("explain", KIND_LABELS.get(kind, kind))
        symbol = detail.pop("symbol", "")
        name = detail.pop("name", "")
        theme = detail.pop("theme", "")
        self.write(kind, explain, symbol=symbol, name=name, theme=theme, detail=detail)

    def watch(self, symbol, name, reason, at=None, detail=None) -> None:
        """★ reason.split(" (")[0] 이 직전과 같으면 기록하지 않는다.
        괄호 안 숫자는 매번 달라진다. 같은 이유가 30초마다 쌓이면 일지가 쓸모없어진다.
        """
        head = reason.split(" (")[0]
        if self._last_watch.get(symbol) == head:
            return
        self._last_watch[symbol] = head
        self.write("watch", reason, at=at, symbol=symbol, name=name, detail=detail)

    def clear_watch(self, symbol) -> None:
        self._last_watch.pop(symbol, None)

    def evaluate(self, verdict, at=None) -> bool:
        """★ 막힌 항목 집합(blocked_by)이 직전과 같으면 기록하지 않는다.
        판정이 바뀐 순간은 반드시 남는다 - 그게 사용자가 알고 싶은 것이다.
        """
        key = (verdict.symbol, verdict.technique)
        blocked = tuple(sorted(verdict.blocked_by))
        if self._last_blocked.get(key) == blocked:
            return False
        self._last_blocked[key] = blocked
        self.write(
            "evaluate", verdict.narrative or verdict.headline, at=at,
            symbol=verdict.symbol, name=verdict.name, theme=verdict.theme,
            detail=verdict.to_dict(),
        )
        return True

    def _where(self, date=None, modes=None, kinds=None) -> tuple[str, list]:
        clauses = []
        params: list = []
        if date is not None:
            clauses.append("date = ?")
            params.append(date)
        if modes is not None:
            modes = list(modes)
            clauses.append(f"mode IN ({', '.join('?' for _ in modes)})")
            params.extend(modes)
        if kinds is not None:
            kinds = list(kinds)
            clauses.append(f"kind IN ({', '.join('?' for _ in kinds)})")
            params.extend(kinds)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_sql, params

    def read(self, date=None, modes=None, kinds=None, limit=None) -> list:
        conn = db.get_connection(self.state_dir)
        where_sql, params = self._where(date, modes, kinds)
        if limit:
            # ★ "끝에서부터 limit 개"를 id 내림차순으로 빠르게 찾은 뒤(인덱스 활용),
            # 화면에 보여줄 때는 다시 시간순으로 뒤집는다(keyset 방식 - 전체를 안 읽는다).
            sql = f"SELECT data FROM (SELECT id, data FROM journal {where_sql} ORDER BY id DESC LIMIT ?) ORDER BY id ASC"  # noqa: S608
            cur = conn.execute(sql, (*params, limit))
        else:
            sql = f"SELECT data FROM journal {where_sql} ORDER BY id"  # noqa: S608
            cur = conn.execute(sql, tuple(params))
        return db.load_data_rows(cur.fetchall())

    def dates(self, modes=None, kinds=None) -> list:
        conn = db.get_connection(self.state_dir)
        where_sql, params = self._where(None, modes, kinds)
        cur = conn.execute(f"SELECT DISTINCT date FROM journal {where_sql} ORDER BY date", tuple(params))  # noqa: S608
        return [r["date"] for r in cur.fetchall() if r["date"]]

    def reset(self, modes=None) -> None:
        """일지를 지운다. modes 를 주면 그 모드에 해당하는 줄만 지운다."""
        conn = db.get_connection(self.state_dir)
        if modes is None:
            conn.execute("DELETE FROM journal")
        else:
            modes = list(modes)
            placeholders = ", ".join("?" for _ in modes)
            conn.execute(f"DELETE FROM journal WHERE mode IN ({placeholders})", tuple(modes))  # noqa: S608
        self._last_watch.clear()
        self._last_blocked.clear()


def explain_buy(cand, verdict, qty, price, stop, target, oco) -> str:
    """"왜 이 종목인가"(선정)와 "왜 지금인가"(신호)는 다른 질문이다. 둘 다 답한다."""
    parts = [f"{with_josa(cand.name)} {price:,.0f}원에 {qty}주 샀습니다."]
    parts.append("[왜 이 종목인가] " + cand.why)
    parts.append("[왜 지금인가] " + verdict.narrative)
    tail = f"손절 {stop:,.0f}원 / 목표 {target:,.0f}원을 함께 걸었습니다"
    if oco:
        tail += "(증권사 서버에 OCO로 등록해서 프로그램이 꺼져도 손절이 살아있습니다)."
    else:
        tail += "(프로그램 내부에서만 감시합니다)."
    parts.append(tail)
    return " ".join(parts)


def explain_sell(pos, price, verdict, pnl, held) -> str:
    result = "이익" if pnl >= 0 else "손실"
    parts = [
        f"{with_josa(pos.name)} {price:,.0f}원에 매도해 {abs(pnl):,.0f}원 {result}으로 "
        f"{held:.0f}분 보유 후 정리했습니다."
    ]
    if verdict is not None:
        parts.append(f"[청산 사유] {verdict.narrative or verdict.headline}")
    return " ".join(parts)
