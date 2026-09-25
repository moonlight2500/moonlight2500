"""config.py 검증 - 국내주식뿐 아니라 암호화폐·해외주식·스윙도 각자의 비용 가정(3-5)으로
익절폭(take_profit_pct)이 왕복 비용의 2배는 넘는지 확인하는지 검증한다.
`python tests/test_config_cost_validation.py` 로 실행한다.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CONFIG_PATH = os.path.join(ROOT, "config.yaml")

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f" - {extra}" if extra else ""))
    if not cond:
        _failures.append(name)


def main() -> int:
    import yaml
    from daytrader.config import load_config
    from daytrader.ticks import breakeven_pct

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        base_raw = yaml.safe_load(f)

    def try_load(mutate):
        raw = yaml.safe_load(yaml.safe_dump(base_raw))  # 간단한 깊은 복사
        mutate(raw)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True)
            path = f.name
        try:
            load_config(path)
            return True, None
        except ValueError as exc:
            return False, str(exc)
        finally:
            os.remove(path)

    check("정상 설정은 통과", try_load(lambda r: None)[0])

    # ── 암호화폐 ──────────────────────────────────────────────────────
    ok_c, err_c = try_load(lambda r: r["crypto"].__setitem__("take_profit_pct", 0.0005))
    check("crypto.take_profit_pct 가 암호화폐 왕복 비용*2 보다 좁으면 거부", not ok_c, err_c)
    check("crypto 오류 메시지에 crypto 를 명시", err_c is not None and "crypto" in err_c)

    be_crypto = breakeven_pct(0.0004, 0.0)  # 기본 crypto.commission_pct
    ok_c2, _ = try_load(lambda r: r["crypto"].__setitem__("take_profit_pct", be_crypto * 2 + 0.01))
    check("crypto.take_profit_pct 가 충분히 넓으면 통과", ok_c2)

    # ── 해외주식 ──────────────────────────────────────────────────────
    ok_o, err_o = try_load(lambda r: r["overseas"].__setitem__("take_profit_pct", 0.00001))
    check("overseas.take_profit_pct 가 해외주식 왕복 비용*2 보다 좁으면 거부", not ok_o, err_o)
    check("overseas 오류 메시지에 overseas 를 명시", err_o is not None and "overseas" in err_o)

    # ── 스윙 ──────────────────────────────────────────────────────────
    ok_s, err_s = try_load(lambda r: r["swing"].__setitem__("take_profit_pct", 0.0005))
    check("swing.take_profit_pct 가 스윙 왕복 비용*2 보다 좁으면 거부", not ok_s, err_s)
    check("swing 오류 메시지에 swing 을 명시", err_s is not None and "swing" in err_s)

    # ── 국내주식 검증은 그대로 유지되는지(회귀 방지) ────────────────────
    ok_r, _ = try_load(lambda r: r["risk"].__setitem__("take_profit_pct", 0.001))
    check("기존 국내주식(risk.take_profit_pct) 검증은 그대로 유지됨", not ok_r)

    print(f"\n{_total - len(_failures)}/{_total} 통과")
    if _failures:
        print("실패:", ", ".join(_failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
