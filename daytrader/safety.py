"""사전 점검(preflight)과 계좌 대조(reconcile)를 한곳에 둔다.
목적이 같아서 한곳에 둔다: 돈이 나간 뒤에 알면 늦는 것을 미리 막는다.
preflight() 는 시작 전 관문 - 하나라도 막히면 실매매를 시작할 수 없다.
reconcile() 은 도는 중 계좌와 상태를 맞춘다 - 계좌가 진실이다.

막는 사고:
  ghost           서버 OCO 로 이미 팔렸는데 보유 중으로 안다
  unknown_holding 사용자가 직접 산 종목을 건드린다
  qty_mismatch    부분 체결로 수량이 어긋난다
  orphan_order    크래시 당시 미체결 주문이 다음날 체결된다

불일치를 조용히 고치지 않는다. 원장·일지·화면에 남긴다.
사용자가 자기 계좌에 무슨 일이 있었는지 영영 모르는 것이 가장 나쁘다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from daytrader.orders import is_ours
from daytrader.ticks import breakeven_pct, trade_pnl
from daytrader.timeutil import iso, now_kst, won


@dataclass
class Check:
    key: str
    label: str
    ok: bool
    level: str  # "block"|"warn"|"info"
    detail: str
    value: object = None
    api: str = ""  # ★ 이 항목이 어떤 API 호출과 관련있는지 - _finish() 에서 key 로 채운다.


# ★ key -> 실제 호출하는 API 경로. 화면에서 "이 항목이 뭘 확인하는지"를
# 추상적인 설명 대신 실제 엔드포인트로 보여준다 - 실패했을 때 어디를
# 찾아봐야 할지 바로 알 수 있게 하는 게 목적이다.
_CHECK_API = {
    "credentials": "POST /oauth2/token",
    "account": "GET /api/v1/accounts",
    "market_open": "GET /api/v1/market-calendar/KR",
    "session_time": "(API 아님 - 지금 시각과 설정값 비교)",
    "buying_power": "GET /api/v1/buying-power",
    "allocation_sanity": "(API 아님 - buying_power 결과와 설정값 비교)",
    "existing_holdings": "GET /api/v1/holdings",
    "open_orders": "GET /api/v1/orders?status=OPEN",
    "conditional_orders": "GET /api/v1/conditional-orders",
    "themes_verified": "GET /api/v1/stocks",
    "cost_config": "(API 아님 - 설정값끼리 비교)",
    "oco_supported": "(API 아님 - 설정값 확인)",
    "paper_experience": "(API 아님 - 과거 모의매매 기록 확인)",
}


# ━━ 사전 점검 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def preflight(cfg, client, ledger=None, positions=None) -> dict:
    """실매매 시작 전 항목을 순서대로 점검한다. 하나라도 block 이면 시작할 수 없다.
    ★★★ 실제로 겪은 버그 - 8)·9) 는 미체결 주문이 "하나라도" 있으면 무조건
    막았다. 그런데 재시작은 정상적인 사용법이다 - 이미 포지션을 보유한 채로
    재시작하면 그 포지션의 서버 OCO(조건부 주문)가 미체결 상태로 남아 있는 게
    당연한데(특히 allow_overnight=True 기본값), 그 정상 상태 때문에 실거래를
    아예 시작할 수 없었다. positions(엔진이 들고 있는 종목별 Position, 이미
    __init__ 에서 daily_state.json 을 읽어 둔 것)를 받아 "지금 보유 중인
    종목과 연결된" 주문은 통과시키고, 그 종목과 무관한(고아) 주문만 막는다.
    """
    checks: list[Check] = []
    account_seq = None
    tracked_symbols = set((positions or {}).keys())
    tracked_oco_ids = {p.oco_id for p in (positions or {}).values() if getattr(p, "oco_id", None)}

    # 1) credentials - 토큰 발급 성공
    try:
        client.accounts()
        checks.append(Check("credentials", "인증", True, "block", "토큰 발급에 성공했습니다."))
    except Exception as exc:
        checks.append(Check(
            "credentials", "인증", False, "block",
            f"토큰 발급에 실패했습니다: {exc}. TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 환경변수를 확인하세요.",
        ))
        return _finish(checks, None)

    # 2) account - BROKERAGE 계좌
    try:
        account_seq = client.resolve_account()
        checks.append(Check("account", "계좌", True, "block", f"위탁계좌를 확인했습니다 (accountSeq={account_seq})."))
    except Exception as exc:
        checks.append(Check("account", "계좌", False, "block", f"사용 가능한 위탁계좌를 찾지 못했습니다: {exc}"))
        return _finish(checks, None)

    # 3) market_open - 오늘이 거래일인지
    try:
        cal = client.market_calendar_kr()
        is_open = bool(cal.get("open", True))
        checks.append(Check(
            "market_open", "개장 여부", is_open, "block",
            "오늘은 거래일입니다." if is_open else "오늘은 휴장일입니다. 실거래를 시작할 수 없습니다.",
        ))
    except Exception as exc:
        checks.append(Check("market_open", "개장 여부", False, "block", f"거래일 조회에 실패했습니다: {exc}"))

    # 4) session_time - 매매 시간대 밖이면 경고
    now_t = now_kst().time()
    in_session = cfg.entry.scan_start <= now_t <= cfg.exit.force_close_time
    checks.append(Check(
        "session_time", "매매 시간대", in_session, "warn",
        "매매 시간대 안입니다." if in_session
        else f"현재 시각이 매매 시간대({cfg.entry.scan_start}~{cfg.exit.force_close_time}) 밖입니다.",
    ))

    # 5) buying_power - per_trade_amount 이상인지
    # 종목당 한도(총 투자금액 / 동시 보유 수)의 절반이 첫 매수의 기본 금액이다(신호 강도 배수 0.6~1.4 적용 전).
    per_trade_amount = cfg.capital.allocation / max(1, cfg.capital.max_positions) * cfg.sizing.initial_ratio
    cash = None
    try:
        bp = client.buying_power()
        cash = float(bp.get("cash") or bp.get("buyingPower") or 0)
        ok = cash >= per_trade_amount
        checks.append(Check(
            "buying_power", "매수 가능 금액", ok, "block",
            f"매수 가능 금액 {cash:,.0f}원입니다."
            + ("" if ok else f" 1회 매매 금액 {per_trade_amount:,.0f}원보다 적어 매매를 시작할 수 없습니다."),
            value=cash,
        ))
    except Exception as exc:
        checks.append(Check("buying_power", "매수 가능 금액", False, "block", f"매수 가능 금액 조회에 실패했습니다: {exc}"))

    # 6) allocation_sanity - 배정금액이 매수가능금액의 50% 초과면 경고
    if cash is not None and cash > 0:
        ratio = cfg.capital.allocation / cash
        ok = ratio <= 0.5
        checks.append(Check(
            "allocation_sanity", "배정금액 적정성", ok, "warn",
            f"배정금액이 매수가능금액의 {ratio*100:.0f}%입니다."
            + ("" if ok else " 50%를 넘어 계좌 전체가 이 프로그램에 과도하게 노출됩니다."),
        ))

    # ★★★ "배정 자금이 실제 계좌 잔여 현금을 초과하면 안 된다"는 요청 -
    # 위 allocation_sanity 는 "50% 초과=경고"일 뿐 100% 넘게 배정해도
    # block 이 아니었다. 계좌에 있는 돈보다 많이 배정하는 건 애초에
    # 말이 안 되는 설정이니(그 이상은 살 수도 없다), 이건 경고가 아니라
    # 반드시 막아야 한다.
    if cash is not None:
        ok = cfg.capital.allocation <= cash
        checks.append(Check(
            "allocation_within_cash", "배정금액 ≤ 계좌 잔고", ok, "block",
            f"배정금액 {cfg.capital.allocation:,.0f}원이 매수 가능 금액 {cash:,.0f}원 이내입니다." if ok
            else f"배정금액 {cfg.capital.allocation:,.0f}원이 매수 가능 금액 {cash:,.0f}원을 초과합니다 - "
                 "[설정] → 자금(국내주식)에서 배정 금액을 줄이거나 계좌에 입금하세요.",
        ))

    # 7) existing_holdings - 보유 종목 안내
    try:
        # ★★★ 실제 토스 API 스펙(HoldingsOverview 모델)을 다시 확인한
        # 결과 - holdings() 는 종목 리스트가 아니라
        # {"items": [...], "totalPurchaseAmount":..., "marketValue":..., ...}
        # 형태의 객체를 반환한다. 예전엔 이걸 리스트인 것처럼 바로
        # 순회했는데(실제로 겪을 뻔한 버그), 그러면 딕셔너리의 키 문자열을
        # 순회하게 되어 h.get("symbol") 에서 AttributeError 가 난다.
        holdings = client.holdings()
        items = holdings.get("items", []) if isinstance(holdings, dict) else holdings
        names = ", ".join(h.get("symbol", "") for h in items) if items else "없음"
        checks.append(Check(
            "existing_holdings", "기존 보유 종목", True, "warn",
            f"현재 보유 종목: {names}. 이 프로그램이 만든 게 아닌 포지션은 건드리지 않습니다.",
        ))
    except Exception as exc:
        checks.append(Check("existing_holdings", "기존 보유 종목", False, "warn", f"보유 종목 조회에 실패했습니다: {exc}"))

    # 8) open_orders - 우리가 보유 중인 종목과 무관한(고아) 미체결 주문이 있으면 차단
    try:
        open_orders = client.get_orders(status="OPEN")
        rows = open_orders if isinstance(open_orders, list) else open_orders.get("orders", [])
        ours = [o for o in rows if is_ours(o.get("clientOrderId", ""))]
        orphans = [o for o in rows if o.get("symbol") not in tracked_symbols]
        ok = len(orphans) == 0
        checks.append(Check(
            "open_orders", "미체결 주문", ok, "block",
            "미체결 주문이 없습니다." if ok and not rows
            else "보유 중인 종목의 주문뿐입니다." if ok
            else f"미체결 주문 {len(rows)}건 중 보유 종목과 무관한 {len(orphans)}건"
                 f"(전체 중 우리 것 {len(ours)}건)이 있어 시작할 수 없습니다.",
        ))
    except Exception as exc:
        checks.append(Check("open_orders", "미체결 주문", False, "block", f"미체결 주문 조회에 실패했습니다: {exc}"))

    # 9) conditional_orders - 우리가 보유 중인 종목과 무관한(고아) 조건부 주문이 있으면 차단
    try:
        # ★★★ 실제로 겪은 버그 - status 없이 부르면 API 자체가 실패해서,
        # "미체결 조건부 주문 조회 실패"로 이 block 체크가 부당하게
        # 실거래를 막을 수 있었다. "미체결"이라는 목적과 status="OPEN"
        # 이 정확히 맞는다.
        cond = client.conditional_orders(status="OPEN")
        rows = cond if isinstance(cond, list) else cond.get("orders", [])
        orphans = [
            o for o in rows
            if o.get("symbol") not in tracked_symbols and o.get("conditionalOrderId") not in tracked_oco_ids
        ]
        ok = len(orphans) == 0
        checks.append(Check(
            "conditional_orders", "미체결 조건부 주문", ok, "block",
            "미체결 조건부 주문이 없습니다." if ok and not rows
            else "보유 중인 종목의 서버 손절/익절(OCO)뿐입니다." if ok
            else f"미체결 조건부 주문 {len(rows)}건 중 보유 종목과 무관한 {len(orphans)}건이 있어 시작할 수 없습니다.",
        ))
    except Exception as exc:
        checks.append(Check("conditional_orders", "미체결 조건부 주문", False, "block", f"조건부 주문 조회에 실패했습니다: {exc}"))

    # 10) themes_verified - themes.yaml 전 종목이 조회되는지
    try:
        from daytrader.screener import load_themes
        themes = load_themes(cfg.themes_file)
        all_symbols = sorted({s for codes in themes.values() for s in codes})
        rows = client.stocks(all_symbols)
        found = {r.get("symbol") for r in rows}
        missing = [s for s in all_symbols if s not in found]
        ok = not missing
        checks.append(Check(
            "themes_verified", "테마 종목 검증", ok, "block",
            "themes.yaml 의 모든 종목이 조회됩니다." if ok
            else f"조회되지 않는 종목이 있습니다: {', '.join(missing)}",
        ))
    except Exception as exc:
        checks.append(Check("themes_verified", "테마 종목 검증", False, "block", f"테마 종목 검증에 실패했습니다: {exc}"))

    # 11) cost_config - take_profit_pct > breakeven*2
    be = breakeven_pct(cfg.costs.commission_pct, cfg.costs.tax_pct)
    ok = cfg.risk.take_profit_pct > be * 2
    checks.append(Check(
        "cost_config", "비용 대비 익절폭", ok, "block",
        f"익절폭 {cfg.risk.take_profit_pct*100:.2f}%가 왕복 비용의 2배({be*200:.2f}%)를 "
        + ("넘습니다." if ok else "넘지 못합니다."),
    ))

    # 12) oco_supported - 꺼져 있으면 경고
    ok = cfg.exit.use_conditional_oco
    checks.append(Check(
        "oco_supported", "서버 OCO 사용", ok, "warn",
        "손절/익절이 서버에 등록됩니다." if ok else "프로그램이 멈추면 손절도 멈춥니다.",
    ))

    # 13) paper_experience - 연습 20건 미만이면 경고
    paper_trades = 0
    if ledger is not None:
        try:
            paper_trades = len(ledger.trades(modes=["paper"]))
        except Exception:
            paper_trades = 0
    ok = paper_trades >= 20
    checks.append(Check(
        "paper_experience", "모의매매 경험", ok, "warn",
        f"모의매매 {paper_trades}건을 완료했습니다." if ok
        else f"모의매매 {paper_trades}건으로 연습이 부족합니다 (권장 20건 이상).",
    ))

    return _finish(checks, account_seq)


def _finish(checks: list, account_seq) -> dict:
    for c in checks:
        c.api = _CHECK_API.get(c.key, "")
    blocking = [c for c in checks if c.level == "block" and not c.ok]
    warnings = [c for c in checks if c.level == "warn" and not c.ok]
    return {
        "ok": not blocking,
        "checks": [c.__dict__ for c in checks],
        "blocking": [c.__dict__ for c in blocking],
        "warnings": [c.__dict__ for c in warnings],
        "account_seq": account_seq,
    }


# ━━ 계좌 대조 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Diff:
    kind: str  # ghost|unknown_holding|qty_mismatch|orphan_order
    symbol: str
    name: str
    state_qty: int
    account_qty: int
    detail: str
    action: str


def reconcile(client, state, cfg, journal, ledger, last_prices=None, *, adopt=None) -> list:
    """도는 중 계좌와 상태를 맞춘다. 계좌가 진실이다.
    불일치를 조용히 고치지 않는다 - 원장·일지·state/reconcile.jsonl 에 남긴다.
    """
    last_prices = last_prices or {}
    diffs: list[Diff] = []

    try:
        holdings = client.holdings()
    except Exception as exc:
        journal.write("reconcile", f"계좌 대조 실패 - 보유 종목 조회 오류: {exc}")
        return diffs

    # ★★★ 실거래 안전장치의 핵심(고스트 포지션 탐지)이 이 딕셔너리에
    # 통째로 의존한다 - holdings() 가 실제로는 리스트가 아니라
    # {"items": [...], ...} 객체를 반환하는데, 이걸 놓치면
    # account_by_symbol 자체가 잘못 만들어져서 "계좌에 없는데 상태엔
    # 있는 종목"(ghost) 탐지가 통째로 오작동할 수 있다.
    items = holdings.get("items", []) if isinstance(holdings, dict) else holdings
    account_by_symbol = {h.get("symbol"): h for h in items}

    # ghost: 상태엔 있는데 계좌엔 없다(수량 0) - 서버 OCO 등으로 프로그램 밖에서 팔렸다.
    # ★ 계좌를 믿는다. 청산 처리한다.
    for symbol, pos in list(state.positions.items()):
        acc = account_by_symbol.get(symbol)
        account_qty = int(acc.get("quantity", 0)) if acc else 0

        if account_qty == 0:
            exit_price = _find_recent_sell_price(client, symbol)
            estimated = exit_price is None
            if exit_price is None:
                exit_price = last_prices.get(symbol, pos.entry_price)

            pnl = won(trade_pnl(pos.entry_price, exit_price, pos.quantity, cfg.costs.commission_pct, cfg.costs.tax_pct))
            reason = "계좌 대조 — 프로그램 밖에서 청산됨(체결가 추정)" if estimated else "계좌 대조 — 프로그램 밖에서 청산됨"
            trade = {
                "date": iso(now_kst())[:10], "symbol": symbol, "name": pos.name, "theme": pos.theme,
                "qty": pos.quantity, "entry": pos.entry_price, "exit": exit_price, "pnl": pnl,
                "reason": reason, "entry_time": iso(pos.entry_time), "exit_time": iso(now_kst()),
                "technique": pos.technique, "verdict_id": pos.verdict_id, "estimated": estimated,
            }
            ledger.append_trade(cfg.mode, trade)
            state.positions.pop(symbol, None)

            diff = Diff(
                kind="ghost", symbol=symbol, name=pos.name, state_qty=pos.quantity, account_qty=0,
                detail="서버에서 이미 청산되었는데 프로그램은 보유 중으로 알고 있었습니다.",
                action="청산 처리(계좌 기준)",
            )
            diffs.append(diff)
            journal.write("reconcile", f"{symbol} 유령 포지션 정리: {diff.detail}", symbol=symbol, name=pos.name)

        elif account_qty != pos.quantity:
            diff = Diff(
                kind="qty_mismatch", symbol=symbol, name=pos.name, state_qty=pos.quantity,
                account_qty=account_qty, detail=f"보유 수량이 어긋났습니다 (내부 {pos.quantity} vs 계좌 {account_qty}).",
                action="계좌 수량으로 맞춤",
            )
            pos.quantity = account_qty
            diffs.append(diff)
            journal.write("reconcile", diff.detail, symbol=symbol, name=pos.name)

    # unknown_holding: 계좌엔 있는데 상태엔 없다 - 사용자가 직접 산 것일 수 있다.
    # ★ 건드리지 않는다. adopt=True 일 때만 입양(평단가를 진입가로).
    for symbol, acc in account_by_symbol.items():
        qty = int(acc.get("quantity", 0))
        if qty <= 0 or symbol in state.positions:
            continue
        diff = Diff(
            kind="unknown_holding", symbol=symbol, name=acc.get("name", symbol),
            state_qty=0, account_qty=qty,
            detail="프로그램이 만들지 않은 보유 종목입니다.",
            action="입양됨" if adopt else "건드리지 않음",
        )
        if adopt:
            from daytrader.broker import Position
            # ★★★ 실제 API 필드명은 averagePurchasePrice 다(HoldingsItem
            # 모델 확인) - averagePrice 는 존재하지 않는 필드라 이 값이
            # 항상 0으로 기록되고 있었다(입양된 종목의 진입가가 0원으로
            # 잘못 남는 버그).
            state.positions[symbol] = Position(
                symbol=symbol, name=acc.get("name", symbol), theme="", quantity=qty,
                entry_price=float(acc.get("averagePurchasePrice", 0)), entry_time=now_kst(),
                peak_price=float(acc.get("averagePurchasePrice", 0)), oco_id=None, entry_volume=0,
                verdict_id=None, why="사용자 계좌에서 입양됨", technique="",
            )
        diffs.append(diff)
        journal.write("reconcile", f"{symbol} {diff.detail} ({diff.action})", symbol=symbol, name=diff.name)

    _append_reconcile_log(cfg.state_dir, diffs)
    return diffs


def _find_recent_sell_price(client, symbol) -> float | None:
    """get_orders() 의 최근 SELL 체결에서 체결가를 찾는다. 없으면 None."""
    try:
        orders = client.get_orders(symbol=symbol, side="SELL", status="CLOSED")
    except Exception:
        return None
    rows = orders if isinstance(orders, list) else orders.get("orders", [])
    rows = [r for r in rows if r.get("status") in ("FILLED", "EXECUTED", "COMPLETED", "FULLY_EXECUTED", "DONE")]
    if not rows:
        return None
    rows.sort(key=lambda r: r.get("filledAt") or r.get("updatedAt") or "", reverse=True)
    row = rows[0]
    return row.get("averageFilledPrice") or row.get("averagePrice")


def _append_reconcile_log(state_dir: str, diffs: list) -> None:
    if not diffs:
        return
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "reconcile.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for d in diffs:
            row = {"at": iso(now_kst()), **d.__dict__}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def cleanup_orphans(broker, state, *, only_ours: bool = True) -> list:
    """크래시 당시 남아있던 미체결 주문을 정리한다.
    ★ is_ours("dt-") 인 것만. 남의 주문은 절대 건드리지 않는다.
    """
    diffs: list[Diff] = []
    for o in broker.open_orders():
        coid = o.get("clientOrderId", "")
        if only_ours and not is_ours(coid):
            continue
        symbol = o.get("symbol", "")
        if symbol in state.positions:
            continue  # 살아있는 포지션에 딸린 주문이면 그냥 둔다.
        order_id = o.get("orderId") or o.get("id")
        try:
            # ★ 이건 일반 주문이지 조건부(OCO) 주문이 아니다 - client.cancel_order 를 써야 한다.
            client = getattr(broker, "client", None)
            if client is not None:
                client.cancel_order(order_id)
        except Exception:
            pass
        diffs.append(Diff(
            kind="orphan_order", symbol=symbol, name=symbol, state_qty=0, account_qty=0,
            detail="크래시 당시 남아있던 미체결 주문을 정리했습니다.", action="취소",
        ))
    return diffs
