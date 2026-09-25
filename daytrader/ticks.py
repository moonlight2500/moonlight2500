from __future__ import annotations

from math import ceil, inf

# 2023-01-25 개정 KRX 호가단위표
TICK_TABLE = [
    (2_000, 1),
    (5_000, 5),
    (20_000, 10),
    (50_000, 50),
    (200_000, 100),
    (500_000, 500),
    (inf, 1_000),
]


def tick_size(price: float) -> int:
    for upper, unit in TICK_TABLE:
        if price < upper:
            return unit
    return TICK_TABLE[-1][1]


def round_to_tick(price: float, mode: str = "nearest") -> int:
    """호가단위에 맞춰 가격을 조정한다. mode 는 nearest|up|down.
    ★ 경계값에서 단위가 바뀐다. 4,998 을 5원 단위로 올리면 5,000 인데
    5,000 의 단위는 10원이라 다시 맞춰야 한다. 그래서 최대 3회 반복한다.
    """
    p = float(price)
    for _ in range(3):
        unit = tick_size(p)
        if mode == "up":
            # ★★★ 실제로 겪은 버그 - int(p) 로 소수점을 먼저 버린 뒤 올림 공식을
            # 적용해서, round_to_tick(13540.5, "up") 이 13550이 아니라 13540을
            # 돌려줬다(이미 호가단위에 딱 맞는 값처럼 취급됨 - 매수 슬리피지
            # 계산에 그대로 쓰이는 값이라 실제 주문가가 틀어진다). 소수점을
            # 버리지 않고 그대로 나눠 올림(ceil)한다. 부동소수점 오차로
            # 13540.000000000002 같은 값이 13550으로 밀려 올라가지 않도록
            # 아주 작은 허용오차(1e-9)를 미리 뺀다.
            p = ceil(p / unit - 1e-9) * unit
        elif mode == "down":
            p = (int(p) // unit) * unit
        else:
            p = round(p / unit) * unit
        if tick_size(p) == unit:
            break
    return int(p)


def buy_cost(price: float, qty: int, commission_pct: float) -> int:
    """매수 시 실제 지불 금액 (매수 수수료 포함)."""
    amount = price * qty
    return int(round(amount + amount * commission_pct))


def sell_proceeds(price: float, qty: int, commission_pct: float, tax_pct: float) -> int:
    """매도 시 실제 수령 금액 (매도 수수료·거래세 차감)."""
    amount = price * qty
    return int(round(amount - amount * commission_pct - amount * tax_pct))


def round_trip_cost_pct(commission_pct: float, tax_pct: float) -> float:
    """왕복 비용률 = 매수 수수료 + 매도 수수료 + 거래세."""
    return commission_pct * 2 + tax_pct


def breakeven_pct(commission_pct: float, tax_pct: float) -> float:
    """본전이 되려면 필요한 상승률.
    팔 때 받는 금액에서 비용이 빠지므로 왕복비용을 그대로 쓰면 안 되고,
    (1 - c - t) 로 한 번 더 나눠야 실제 필요한 상승률이 나온다.
    """
    c, t = commission_pct, tax_pct
    return (c * 2 + t) / (1 - c - t)


def trade_pnl(entry: float, exit: float, qty: int, commission_pct: float, tax_pct: float) -> float:
    """매매 손익 계산식. 엔진과 백테스트가 다르게 계산하면 두 성적을
    비교할 수 없으므로, 이 식 하나만 쓴다.
    """
    buy = buy_cost(entry, qty, commission_pct)
    sell = sell_proceeds(exit, qty, commission_pct, tax_pct)
    return float(sell - buy)
