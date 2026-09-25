"""종목별 청산 효율 추적 - "청산 시 어떤 시점에 정리했어야 가장 이익을 많이 냈을지 기록했다가
다음에 같은 종목에 들어갔을 때 반영해달라"는 요청(매매 모델 3번)의 구현.

★★★ 설계 - 여러 청산 기법을 매 순간 되감아 "그때 팔았다면 손익이 얼마였을지"까지 전부 재현하는
완전한 시뮬레이션은 아니다(수수료·슬리피지까지 매 순간 재현해야 해서 비용이 크고, 이미 있는
peak_price 추적만으로도 실질적으로 같은 목적을 달성할 수 있다). 대신 포지션이 보유 중 실제로
도달한 최고가(peak_price - MFE, Maximum Favorable Excursion)를 "그 순간 팔았다면 챙길 수
있었던 최대 이익"의 근사로 쓰고, 실제 청산에서 챙긴 이익과 비교해 "효율"을 잰다. 이 효율이
계속 낮게 나오는 종목(추적 손절·모멘텀 소멸 등으로 늘 너무 일찍 정리해 온 종목이라는 뜻)은
다음에 그 종목에 다시 들어갈 때 익절 목표를 추가로 넓혀 준다.

learning_mode == "entry_exit_pref" 인 시장에서만 기록·반영된다(daytrader/playbook.py,
각 엔진의 진입/청산 코드 참고). 표본이 적을 때 과적합되지 않도록 최소 표본(MIN_SAMPLES) 미만이면
반영하지 않는다 - technique_prefs.py 의 min_trades_for_weight 와 같은 원칙이다.
"""

from __future__ import annotations

import json
import os
import threading

_lock = threading.Lock()

MIN_SAMPLES = 3         # 표본이 이 미만이면 아직 반영하지 않는다(우연에 좌우되지 않게)
MAX_WIDEN = 1.5          # 효율이 아무리 나빠도 익절 목표를 이 배수 이상은 안 넓힌다
EFFICIENCY_FLOOR = 0.3   # 이보다 효율이 낮아야(도달 가능했던 이익의 30% 미만만 챙겼어야) 넓히기 시작한다


def _path(cfg) -> str:
    return os.path.join(cfg.state_dir, "exit_efficiency.json")


def _load(cfg) -> dict:
    p = _path(cfg)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(cfg, data: dict) -> None:
    p = _path(cfg)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = f"{p}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def record(cfg, market: str, symbol: str, realized_pct: float, mfe_pct: float) -> None:
    """포지션이 완전히 청산될 때마다 호출한다.
    realized_pct = 실제 청산 손익률(진입가 대비, 평균 청산가 기준).
    mfe_pct = 보유 중 도달한 최고가 기준 손익률. 0 이하(한 번도 이익 구간에 못 갔던 거래)면
    "청산 효율"을 잴 의미가 없어 기록하지 않는다.

    ★★★ 실제로 겪은 버그 - 처음엔 mfe_pct>0 이기만 하면(손실로 끝났어도) 기록해서 realized 를
    0으로 취급했다. 그런데 "잠깐 올랐다가 반전해 손실로 끝난 거래"(휩쏘)와 "이익권에서 너무
    일찍 정리한 거래"를 구분 못 해, 휩쏘가 잦은 종목까지 "효율이 낮다"고 잘못 배워 다음에
    익절 목표를 넓혀 버렸다 - 그러면 다음번 같은 휩쏘에서 더 오래 들고 있다가 더 크게 잃는
    악순환이 된다(비교 시뮬레이션에서 10일 연속 성과가 급격히 나빠지는 것으로 발견됨).
    이제는 실제로 이익으로 끝난 거래(realized_pct > 0)만 기록한다 - "이익을 놓쳤다"는
    교훈은 최종적으로 이익이었던 거래에서만 유효하다."""
    if symbol is None or mfe_pct is None or mfe_pct <= 0 or realized_pct is None or realized_pct <= 0:
        return
    key = f"{market}:{symbol}"
    with _lock:
        data = _load(cfg)
        row = data.get(key) or {"n": 0, "sum_realized": 0.0, "sum_mfe": 0.0}
        row["n"] = row.get("n", 0) + 1
        row["sum_realized"] = row.get("sum_realized", 0.0) + realized_pct
        row["sum_mfe"] = row.get("sum_mfe", 0.0) + mfe_pct
        data[key] = row
        _save(cfg, data)


def widen_multiplier(cfg, market: str, symbol: str) -> tuple:
    """이 종목의 과거 청산 효율을 보고, 익절 목표에 곱할 추가 배수와 설명을 돌려준다.
    표본이 적거나(MIN_SAMPLES 미만) 효율이 이미 충분하면(EFFICIENCY_FLOOR 이상) (1.0, "")."""
    if not symbol:
        return 1.0, ""
    key = f"{market}:{symbol}"
    data = _load(cfg)
    row = data.get(key)
    if not row or row.get("n", 0) < MIN_SAMPLES or row.get("sum_mfe", 0) <= 0:
        return 1.0, ""
    efficiency = row["sum_realized"] / row["sum_mfe"]
    if efficiency >= EFFICIENCY_FLOOR:
        return 1.0, ""
    # 효율 0(전혀 못 챙김)이면 MAX_WIDEN, EFFICIENCY_FLOOR 에 가까울수록 1.0 에 가깝게 - 선형 보간.
    mult = 1.0 + (MAX_WIDEN - 1.0) * (1.0 - efficiency / EFFICIENCY_FLOOR)
    return mult, f"이 종목은 최근 보유 중 도달 가능했던 이익의 {efficiency * 100:.0f}%만 챙겨왔습니다 → 익절 목표 {mult:.2f}배 확대"


def summary(cfg) -> dict:
    """[실험실]/설정 화면 표시용 - 시장:종목별 효율 표. 표본 1건 이상만 보여준다."""
    data = _load(cfg)
    out = {}
    for key, row in data.items():
        if row.get("n", 0) < 1 or row.get("sum_mfe", 0) <= 0:
            continue
        out[key] = {"n": row["n"], "efficiency": row["sum_realized"] / row["sum_mfe"]}
    return out


def clear(cfg) -> None:
    with _lock:
        _save(cfg, {})
