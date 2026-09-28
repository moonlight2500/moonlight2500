"""숫자를 문서에 적어두면 설정을 바꾼 순간부터 거짓말이 된다. 그래서
하드코딩하지 않고 살아있는 설정과 켜진 기법에서 뽑아 만든다. 손절 폭을
2.5%→3% 로 바꾸면 원칙 문서의 문장도 같이 바뀐다.
"""

from __future__ import annotations

from daytrader.ticks import breakeven_pct, round_trip_cost_pct

_MODE_LEAD = {
    "sim": "지금은 가짜 시장(시뮬레이션)을 보고 있습니다. 실제 계좌·실제 시세와는 무관합니다.",
    "replay": "지금은 과거 데이터를 재생하며 검증하고 있습니다. 실제 주문은 나가지 않습니다.",
    "web": "지금은 실시간 시세를 관찰만 하고 있습니다. 매수·매도 주문은 나가지 않습니다.",
    "paper": "지금은 실시간 시세로 모의 주문을 내고 있습니다. 실제 체결은 없습니다.",
    "live": "지금은 실제 계좌로 실주문을 내고 있습니다. 여기서 나가는 주문은 진짜 돈을 움직입니다.",
}


def _section_data(cfg) -> dict:
    lead = _MODE_LEAD.get(cfg.mode, f"현재 모드: {cfg.mode}")
    return {
        "id": "data", "title": "지금 무엇을 보고 있나", "lead": lead,
        "items": [
            {"head": "모드", "body": cfg.mode},
            {"head": "배정 자본", "body": f"{cfg.capital.allocation:,.0f}원"},
        ],
    }


def _section_philosophy() -> dict:
    items = [
        {"head": "1. 수익을 최대한 추구한다",
         "body": "익절폭을 미리 좁게 고정하지 않고, 추세가 살아있는 한 트레일링 스탑으로 크게 먹는 것을 우선합니다. "
                 "최소한의 손절·손실한도만 남기고, 그 안에서는 공격적으로 베팅합니다."},
        {"head": "2. 비용을 정면으로 다룬다",
         "body": "왕복 비용을 계산에 넣고, 익절폭이 비용의 2배 미만인 설정은 실행을 거부합니다."},
        {"head": "3. 감정이 개입할 자리를 없앤다", "body": "기준은 미리 숫자로 정하고 기계가 실행합니다."},
        {"head": "4. 안 하는 날이 정상이다",
         "body": "거래 0건은 고장이 아닙니다. 지금 왜 안 사는지를 항상 사람이 읽을 수 있게 남깁니다."},
        {"head": "5. 판단은 재현 가능해야 한다",
         "body": "어떤 기법의 어떤 항목이 어떤 값이었고, 무엇이 바뀌어 결정이 뒤집혔는지 구조화해 남깁니다."},
    ]
    return {"id": "philosophy", "title": "원칙", "lead": "이 프로그램이 지키는 다섯 가지 원칙입니다.", "items": items}


def _section_universe() -> dict:
    items = [
        {"head": "1단계 - 테마",
         "body": "themes.yaml 에 적힌 테마 중, 구성 종목 여럿이 함께 오른 것만 '테마'로 인정합니다."},
        {"head": "2단계 - 주도주", "body": "인정된 테마 안에서 거래대금이 가장 큰 종목(대장주)을 우선합니다."},
        {"head": "3단계 - 걸러내기", "body": "가격대·상승률·거래대금·유의종목 조건을 모두 통과한 종목만 후보가 됩니다."},
        {"head": "테마 목록은 사람이 관리한다",
         "body": "테마 화면은 종목을 고르는 곳이 아니라 자동 선정이 뒤질 범위를 정하는 곳입니다."},
        {"head": "선정 과정은 화면에 그대로 보인다",
         "body": "후보가 왜 뽑혔는지, 탈락한 종목은 왜 탈락했는지 전부 남습니다."},
    ]
    return {
        "id": "universe", "title": "무엇을 사는가",
        "lead": "종목은 사람이 고르지 않습니다. 프로그램이 다음 순서로 뽑습니다.", "items": items,
    }


def _section_techniques(cfg, playbook) -> dict:
    entries = [t for t in playbook.describe() if t["phase"] == "entry"]
    items = [{"head": t["label"], "body": f"{t['description']} (출처: {t['origin']})"} for t in entries]
    items.append({
        "head": "주문 방식",
        "body": "지정가로 매수하며, 20초 안에 체결되지 않으면 취소합니다(시장가로 추격하지 않습니다).",
    })

    if entries:
        order = " → ".join(t["label"] for t in entries)
        lead = f"현재 켜진 진입 기법은 {len(entries)}개이며, 다음 순서로 확인합니다: {order}."
    else:
        lead = "현재 켜진 진입 기법이 없습니다."

    return {"id": "techniques", "title": "어떻게 사는가", "lead": lead, "items": items}


def _section_exit(cfg, playbook) -> dict:
    exits = [t for t in playbook.describe() if t["phase"] == "exit"]
    items = [{"head": t["label"], "body": f"{t['description']} (출처: {t['origin']})"} for t in exits]

    priority = "force_close > fixed.stop_loss > atr_stop > fixed.take_profit > trailing > momentum_fade > time_stop"
    items.append({"head": "우선순위", "body": f"손실을 막는 규칙이 항상 먼저입니다: {priority}"})

    if cfg.exit.use_conditional_oco:
        items.append({
            "head": "서버 OCO",
            "body": "손절·익절 주문을 증권사 서버에도 등록합니다. 프로그램이 꺼져도 손절이 살아있습니다.",
        })
    else:
        items.append({
            "head": "서버 OCO",
            "body": "서버 OCO 가 꺼져 있습니다. 프로그램 내부에서만 손절을 감시하므로, 프로그램이 멈추면 손절도 멈춥니다.",
        })

    # ★★★ [9-1] "조건부 오버나이트" - allow_overnight 는 더 이상 "무조건
    # 넘긴다"가 아니라 "이익 중인 포지션만, 최대 며칠까지" 넘기는 규칙이다.
    # 숫자(기준 수익률 등)는 설정에서 그대로 읽어 문서가 설정과 어긋나지 않게 한다.
    if cfg.exit.allow_overnight:
        ex = cfg.exit
        holiday_body = (
            "주말·공휴일 앞 마지막 거래일에는 이익이 충분해도 넘기지 않습니다."
            if ex.overnight_skip_before_holiday
            else "휴장 전날에도(설정에서 꺼둠) 조건만 맞으면 넘깁니다."
        )
        stop_body = (
            "넘기는 포지션은 손절선을 본전(평균 매수가+비용)으로 올리고 서버 OCO를 다시 겁니다. "
            "재설정에 실패하면 안전을 위해 넘기지 않고 그 자리에서 청산합니다."
            if ex.overnight_breakeven_stop
            else "손절선은 원래 값 그대로 두고 수량만 넘깁니다(설정에서 본전 상향을 꺼둠)."
        )
        items.append({
            "head": "조건부 오버나이트",
            "body": (
                f"장 마감 시점 평가손익이 비용을 뺀 뒤에도 {ex.overnight_min_profit_pct*100:.1f}% 이상인 "
                f"포지션만 다음 거래일로 최대 {ex.overnight_max_days}일 넘깁니다. 기준에 못 미치는 포지션과 "
                f"손실 중인 포지션은 그대로 당일 청산됩니다. {holiday_body} {stop_body} 넘긴 포지션은 다음 "
                "장마감에 이익이 나도 무조건 정리합니다(다시 넘기지 않음)."
            ),
        })
    else:
        items.append({
            "head": "오버나이트",
            "body": "장 마감 전에 반드시 정리합니다(설정에서 꺼둠) - 포지션을 다음날로 넘기지 않습니다.",
        })

    lead = f"현재 켜진 청산 기법은 {len(exits)}개(+장 마감 강제청산)입니다."
    return {"id": "exit", "title": "어떻게 파는가", "lead": lead, "items": items}


def _section_defense(cfg) -> dict:
    r = cfg.risk
    size_after_loss = f"{r.reduced_size_pct*100:.0f}%로 축소" if r.reduce_after_loss else "축소하지 않음"
    table = [
        {"k": "종목당 한도", "v": f"총 투자금액의 1/{cfg.capital.max_positions} (첫 매수는 그 절반×신호 배수, 나머지는 이익 날 때 추가)", "why": "한 종목에 너무 크게 들어가지 않기 위해서입니다."},
        {"k": "동시 보유", "v": f"최대 {cfg.capital.max_positions}종목", "why": "종목 수가 많아지면 감시가 허술해집니다."},
        {"k": "손실 뒤 크기", "v": size_after_loss, "why": "잃고 있을 때 키우는 것은 물타기이고, 계좌가 가장 빨리 망가지는 길입니다."},
        {"k": "연속 손절", "v": f"{r.max_consecutive_losses}회", "why": "오늘은 시장과 안 맞는다는 신호로 봅니다."},
        {"k": "일일 한도", "v": f"{r.daily_loss_limit_pct*100:.0f}%", "why": "손실이 난 날 더 하려는 충동을 막습니다."},
        {"k": "주간 한도", "v": f"{r.weekly_loss_limit_pct*100:.0f}%", "why": "이번 주는 더 하지 않고 다음 주에 다시 시작합니다."},
        {"k": "일일 최대 거래", "v": f"{r.daily_max_trades}회", "why": "과매매는 비용으로 계좌를 갉아먹습니다."},
        {"k": "시세 끊김", "v": f"{cfg.live.degrade_after_failures}회 연속 실패 시 저하 모드", "why": "시세를 못 보는 상태에서 사는 것은 눈 감고 사는 것입니다."},
    ]
    items = [
        {"head": "손실 한도는 늘리지 않는다", "body": "설정을 실시간으로 완화해서 손실을 정당화하지 않습니다."},
        {"head": "계좌가 진실이다", "body": "내부 상태와 실제 계좌가 다르면 계좌를 믿고 고칩니다."},
        {"head": "주문은 두 번 나가지 않는다", "body": "응답을 못 받으면 재전송 대신 조회로 확인합니다."},
    ]
    return {
        "id": "defense", "title": "손실을 막는 장치",
        "lead": "여덟 가지 안전장치가 항상 켜져 있습니다.", "items": items, "table": table,
    }


def _section_cost(cfg, be: float, rt: float) -> dict:
    items = [
        {"head": "왕복 비용", "body": f"{rt*100:.3f}% (수수료 {cfg.costs.commission_pct*100:.3f}%×2 + 거래세 {cfg.costs.tax_pct*100:.2f}%)"},
        {"head": "본전 상승률", "body": f"{be*100:.3f}% - 이보다 적게 오르면 팔아도 손해입니다."},
        {"head": "익절폭", "body": f"{cfg.risk.take_profit_pct*100:.2f}% (본전의 {cfg.risk.take_profit_pct/be:.1f}배)"},
        {"head": "손절폭", "body": f"{cfg.risk.stop_loss_pct*100:.2f}%"},
    ]
    return {"id": "cost", "title": "비용, 매매의 출발선", "lead": "모든 매매는 비용을 이기는 것에서 시작합니다.", "items": items}


def _section_never(cfg) -> dict:
    items = [
        {"head": "물타기", "body": "손실 중인 포지션에 추가로 매수하지 않습니다."},
    ]
    # ★★★ [9-1] "조건부 오버나이트" - cfg.exit.allow_overnight 에 따라 실제 동작이
    # 다르므로(숫자를 문서에 적어두면 설정을 바꾼 순간부터 거짓말이 된다는 이 파일의
    # 원칙과 같다) "절대 안 함" 목록에는 지금 설정대로만 넣는다.
    if not getattr(cfg.exit, "allow_overnight", False):
        items.append({"head": "오버나이트", "body": "장 마감 전에 반드시 정리합니다. 포지션을 다음날로 넘기지 않습니다."})
    else:
        items.append({
            "head": "손실 중 오버나이트",
            "body": f"손실 중이거나 이익이 {cfg.exit.overnight_min_profit_pct*100:.1f}% 미만인 포지션은 "
                    "다음날로 넘기지 않습니다 - 이익 기준을 넘긴 포지션만 넘깁니다.",
        })
    items += [
        {"head": "추격매수", "body": "이미 너무 오른 종목은 사지 않습니다."},
        {"head": "유의종목", "body": "정리매매·투자경고·투자위험·단기과열 종목은 애초에 후보에서 제외합니다."},
        {"head": "신용·미수", "body": "빌린 돈으로 매매하지 않습니다."},
        {"head": "남의 포지션", "body": "사용자가 직접 산 종목은 건드리지 않습니다."},
    ]
    lead = "다음은 어떤 상황에서도 하지 않습니다."
    if getattr(cfg.exit, "allow_overnight", False):
        lead += (
            " (조건부 오버나이트: 이익 중인 포지션만, 최대 "
            f"{cfg.exit.overnight_max_days}일까지 다음 거래일로 넘깁니다 - 그 밖에는 그대로 당일 청산합니다.)"
        )
    return {"id": "never", "title": "하지 않는 것", "lead": lead, "items": items}


def _section_limits(cfg) -> dict:
    items = [
        {"head": "뉴스를 읽지 못한다", "body": "제목의 문자열만 볼 뿐, 기사 내용을 이해하지 못합니다."},
        {"head": "시장 전체를 보지 못한다", "body": "themes.yaml 에 적힌 종목만 봅니다. 그 밖의 급등주는 잡지 못합니다."},
        {"head": "호가 잔량을 보지 못한다", "body": "실제 체결 우선순위나 매물대를 알 수 없습니다."},
        {"head": "인터넷 시세 분봉에 O/H/L 이 없다",
         "body": "토스 없이 인터넷 시세만 쓸 때는 봉 안의 진폭을 알 수 없어 판정이 보수적입니다."},
        {"head": "시뮬레이션 성적은 아무것도 보장하지 않는다",
         "body": "호가 잔량, VI, 체결 우선순위, 뉴스 반응, 군집 행동을 재현하지 않습니다."},
        {"head": "수익을 만들어주지 않는다", "body": "이 프로그램은 규칙을 지키는 도구일 뿐, 수익을 보장하지 않습니다."},
    ]
    return {"id": "limits", "title": "못 하는 것", "lead": "원칙 7: 못 하는 것을 숨기지 않습니다.", "items": items}


def build(cfg, playbook=None) -> dict:
    if playbook is None:
        from daytrader.playbook import Playbook
        playbook = Playbook(cfg)

    be = breakeven_pct(cfg.costs.commission_pct, cfg.costs.tax_pct)
    rt = round_trip_cost_pct(cfg.costs.commission_pct, cfg.costs.tax_pct)

    sections = [
        _section_data(cfg),
        _section_philosophy(),
        _section_universe(),
        _section_techniques(cfg, playbook),
        _section_exit(cfg, playbook),
        _section_defense(cfg),
        _section_cost(cfg, be, rt),
        _section_never(cfg),
        _section_limits(cfg),
    ]

    return {"mode": cfg.mode, "breakeven_pct": be, "allocation": cfg.capital.allocation, "sections": sections}


def as_text(cfg, playbook=None) -> str:
    """CLI 출력용."""
    doc = build(cfg, playbook=playbook)
    lines = [f"[{doc['mode']}] 배정 {doc['allocation']:,.0f}원 · 본전 상승률 {doc['breakeven_pct']*100:.3f}%", ""]
    for sec in doc["sections"]:
        lines.append(f"## {sec['title']}")
        if sec.get("lead"):
            lines.append(sec["lead"])
        for it in sec["items"]:
            lines.append(f"- {it['head']}: {it['body']}")
        if sec.get("table"):
            lines.append("")
            for row in sec["table"]:
                lines.append(f"  {row['k']}: {row['v']} ({row['why']})")
        lines.append("")
    return "\n".join(lines)
