#!/usr/bin/env python3
"""AutoDayTrading CLI - 개발·진단용.

명령: net / check / verify-themes / discover / select / run / journal / rules / report / ui
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from daytrader.config import load_config
from daytrader.paths import app_path, ensure_user_files


def setup_logging(verbose: bool) -> None:
    """로그는 stdout + RotatingFileHandler 로 남긴다."""
    ensure_user_files()
    log_dir = app_path("logs")
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "run.log"), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file_handler)


def make_client(cfg, advancing: bool = False):
    """모드별로 다른 클라이언트를 만든다.
    ★ advancing(실제로 매매를 진행하는 경우)이고 live 면 인터넷 폴백을 쓰지
    않는다 - 실거래는 대체 데이터로 조용히 넘어가면 안 된다.
    """
    if cfg.mode in ("sim", "replay"):
        from daytrader.clock import make_clock
        from daytrader.simulator import SimClient
        return SimClient(cfg, clock=make_clock(cfg))

    from daytrader.router import build_router
    allow_web = not (advancing and cfg.mode == "live")
    return build_router(cfg, allow_web_fallback=allow_web)


# ━━ 명령 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_net(cfg, args) -> None:
    from daytrader import netutil
    result = netutil.diagnose()
    print(f"[{result['mode_label']}] 접속 방식 (프록시: {result['proxies'] or '없음'})")
    for t in result["targets"]:
        mark = "OK  " if t["ok"] else "FAIL"
        print(f"  [{mark}] {t['label']:12s} {t['status']} {t['ms']}ms - {t['detail']}")


def cmd_check(cfg, args) -> None:
    from daytrader.ticks import breakeven_pct
    be = breakeven_pct(cfg.costs.commission_pct, cfg.costs.tax_pct)
    print(f"모드: {cfg.mode}")
    print(f"본전 상승률: {be * 100:.3f}% (익절폭 {cfg.risk.take_profit_pct * 100:.2f}%)")
    if cfg.risk.take_profit_pct <= be * 2:
        print("경고: 익절폭이 왕복 비용의 2배에 못 미칩니다.")

    if cfg.mode in ("sim", "replay"):
        print("(가짜 시장 모드라 계좌 조회는 생략합니다.)")
        return
    try:
        client = make_client(cfg)
        print(f"계좌: {client.accounts()}")
    except Exception as exc:
        print(f"계좌 조회 실패: {exc}")


def cmd_verify_themes(cfg, args) -> None:
    from daytrader.screener import load_themes
    themes = load_themes(cfg.themes_file)
    all_symbols = sorted({s for codes in themes.values() for s in codes})
    client = make_client(cfg)
    rows = client.stocks(all_symbols)
    found = {r.get("symbol"): r.get("name") for r in rows}
    missing = [s for s in all_symbols if s not in found]

    print(f"테마 {len(themes)}개, 종목 {len(all_symbols)}개 확인")
    if missing:
        print(f"조회되지 않는 종목: {', '.join(missing)}")
    else:
        print("모든 종목이 정상 조회됩니다.")


def _load_names(cfg, client) -> dict:
    """SimClient 는 이미 theme_names 를 들고 있고, 그 밖은 themes.yaml 주석에서 읽는다."""
    if hasattr(client, "theme_names"):
        return client.theme_names
    try:
        from daytrader.simulator import load_theme_names
        return load_theme_names(cfg.themes_file)
    except Exception:
        return {}


def cmd_discover(cfg, args) -> None:
    from daytrader.screener import Screener
    client = make_client(cfg)
    screener = Screener(client, cfg, names=_load_names(cfg, client))
    rows = screener.discover(count=args.count)
    if not rows:
        print("미분류 급등 종목이 없습니다.")
        return
    for r in rows:
        print(f"{r['symbol']} {r['name']:10s} {r['change_rate'] * 100:+6.2f}%  대금 {r['trading_amount'] / 1e8:>8,.0f}억")


def cmd_select(cfg, args) -> None:
    """선정 기준 → 테마 평가(구성 종목 전부) → 매매 대상(why) 을 출력한다."""
    from daytrader.screener import Screener
    client = make_client(cfg)
    screener = Screener(client, cfg, names=_load_names(cfg, client))
    report = screener.build_report()

    print("== 선정 기준 ==")
    for t in report.criteria["text"]:
        print(f"  - {t}")

    print()
    print("== 테마 평가 ==")
    for theme in report.themes:
        tag = "선정" if theme.picked else ("자격" if theme.qualified else "탈락")
        print(f"[{tag}] {theme.name} (점수 {theme.score:.3f})")
        if theme.formula:
            print(f"    공식: {theme.formula}")
        if theme.reason:
            print(f"    사유: {theme.reason}")
        for m in theme.members:
            mark = "O" if m.is_up else " "
            tail = " [선정]" if m.selected else (f" (탈락: {m.reject})" if m.reject else "")
            print(f"    [{mark}] {m.symbol} {m.name:10s} {m.change_rate * 100:+6.2f}%{tail}")

    print()
    print("== 매매 대상 ==")
    if not report.candidates:
        print(f"  {report.summary}")
    for c in report.candidates:
        print(f"- {c.symbol} {c.name} ({c.theme})")
        print(f"    {c.why}")


def cmd_run(cfg, args) -> None:
    from daytrader.engine import Engine

    # ★ --live 는 "실매매" 를 정확히 타이핑해야 진행한다. DAYTRADER_NO_CONFIRM=1
    # 로만 우회할 수 있다 (자동화 스크립트에서 의도적으로 켤 때만 쓴다).
    if cfg.mode == "live" and os.environ.get("DAYTRADER_NO_CONFIRM") != "1":
        typed = input("실거래를 시작합니다. 계속하려면 '실매매' 를 입력하세요: ")
        if typed.strip() != "실매매":
            print("확인 문구가 일치하지 않아 중단합니다.")
            return

    client = make_client(cfg, advancing=True)
    engine = Engine(cfg, client)
    engine.run()


def cmd_journal(cfg, args) -> None:
    from daytrader.journal import Journal
    journal = Journal(cfg.state_dir, mode=cfg.mode)
    rows = journal.read(limit=args.limit)
    if not rows:
        print("일지가 비어 있습니다.")
        return
    for r in rows:
        symbol = f" {r.get('symbol')}" if r.get("symbol") else ""
        print(f"[{r['date']} {r['time']}] ({r['kind']}){symbol} {r['explain']}")


def cmd_rules(cfg, args) -> None:
    from daytrader import principles
    print(principles.as_text(cfg))


def cmd_report(cfg, args) -> None:
    from daytrader.ledger import Ledger, modes_in
    ledger = Ledger(cfg.state_dir)
    modes = modes_in("virtual")
    totals = ledger.totals(modes=modes)
    print(f"거래 {totals['trades']}건 · 승률 {totals['win_rate'] * 100:.1f}% · 손익 {totals['pnl']:,.0f}원")
    print(f"손익비 {totals['profit_factor']:.2f} · MDD {totals['mdd']:,.0f}원")
    print()
    for d in ledger.daily(modes=modes):
        print(f"  {d['date']}: {d['pnl']:+,.0f}원 ({d['trades']}건, 승률 {d['win_rate'] * 100:.0f}%)")


def cmd_ui(cfg, args) -> None:
    from daytrader import server
    server.main()


def cmd_db_backup(cfg, args) -> None:
    """daytrader.db 의 안전한 복사본을 만든다(sqlite Online Backup API).
    ★★★ 프로그램이 켜져 있는 동안 daytrader.db 파일을 그냥 복사(탐색기 복사·robocopy 등)하면
    WAL 에 아직 합쳐지지 않은 최근 기록이 빠지거나 파일이 중간 상태로 찍힐 수 있다 - 그래서
    이 명령을 따로 둔다(db.backup_to() 가 실행 중에도 안전하게 복사한다)."""
    from daytrader import db
    from daytrader.timeutil import now_kst
    out = args.out or os.path.join(cfg.state_dir, f"daytrader-backup-{now_kst().strftime('%Y%m%d-%H%M%S')}.db")
    db.backup_to(cfg.state_dir, out)
    print(f"daytrader.db 를 안전하게 복사했습니다: {out}")


# ━━ 진입점 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description="AutoDayTrading 개발·진단용 CLI")
    parser.add_argument("-c", "--config", default=None, help="config.yaml 경로 (기본: paths 가 정한다)")
    parser.add_argument("-v", "--verbose", action="store_true", help="디버그 로그 출력")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("net", help="연결 진단 (netutil.diagnose)")
    sub.add_parser("check", help="설정·비용·계좌 점검")
    sub.add_parser("verify-themes", help="themes.yaml 종목코드 검증")

    p_discover = sub.add_parser("discover", help="테마 밖 급등 종목 탐색")
    p_discover.add_argument("--count", type=int, default=40)

    sub.add_parser("select", help="선정 과정 출력 (기준 → 테마 평가 → 매매 대상)")

    p_run = sub.add_parser("run", help="엔진 실행")
    mode_group = p_run.add_mutually_exclusive_group()
    mode_group.add_argument("--sim", action="store_const", dest="mode", const="sim")
    mode_group.add_argument("--web", action="store_const", dest="mode", const="web")
    mode_group.add_argument("--paper", action="store_const", dest="mode", const="paper")
    mode_group.add_argument("--live", action="store_const", dest="mode", const="live")
    p_run.set_defaults(mode=None)

    p_journal = sub.add_parser("journal", help="최근 일지 출력")
    p_journal.add_argument("--limit", type=int, default=100)

    sub.add_parser("rules", help="매매원칙 문서 출력")
    sub.add_parser("report", help="성과 요약 출력")
    sub.add_parser("ui", help="웹 서버 실행")

    p_dbbackup = sub.add_parser("db-backup", help="daytrader.db 를 실행 중에도 안전하게 복사")
    p_dbbackup.add_argument("--out", default=None, help="복사본 경로 (기본: state\\daytrader-backup-<시각>.db)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    cfg = load_config(args.config)
    if args.command == "run" and getattr(args, "mode", None):
        cfg.mode = args.mode

    # ★ setup_logging() 은 cfg 를 읽기 전에 불린다(state_dir 을 몰라서) - cfg 가 준비된
    # 지금에서야 WARNING 이상을 SQLite(daytrader.db 의 app_log 표)에도 남기는 핸들러를 붙인다.
    from daytrader import applog
    applog.attach(cfg.state_dir)

    handlers = {
        "net": cmd_net, "check": cmd_check, "verify-themes": cmd_verify_themes,
        "discover": cmd_discover, "select": cmd_select, "run": cmd_run,
        "journal": cmd_journal, "rules": cmd_rules, "report": cmd_report, "ui": cmd_ui,
        "db-backup": cmd_db_backup,
    }
    handlers[args.command](cfg, args)


if __name__ == "__main__":
    main()
