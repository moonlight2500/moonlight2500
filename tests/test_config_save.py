"""설정 저장 경로(ruamel.yaml 라운드트립)가 주석·키 순서를 보존하는지,
그리고 저장 직전 config.yaml.bak 을 남기는지에 대한 오프라인 테스트.
`python tests/test_config_save.py` 로 실행한다."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import daytrader.server as server  # noqa: E402

_total = 0
_failures: list = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _total
    _total += 1
    mark = "OK  " if cond else "FAIL"
    line = f"[{mark}] {name}"
    if extra:
        line += f" - {extra}"
    print(line)
    if not cond:
        _failures.append(name)


def test_save_keeps_comments_order_and_backup() -> None:
    print("\n== 설정 저장: 주석·키 순서 보존 + config.yaml.bak 백업 ==")
    tmpdir = tempfile.mkdtemp()
    orig_config_path = server.CONFIG_PATH
    try:
        cfg_path = os.path.join(tmpdir, "config.yaml")
        shutil.copy(os.path.join(ROOT, "config.yaml"), cfg_path)
        server.CONFIG_PATH = cfg_path

        with open(cfg_path, "r", encoding="utf-8") as f:
            before_text = f.read()

        # ★ 실제 저장 경로와 같은 순서: 라운드트립으로 읽고 → 값 하나만
        # 바꾸고 → 검증하고 → 쓴다.
        raw = server._read_config_raw(round_trip=True)
        assert raw.get("mode") != "paper"  # 이번 값이 바뀌는지 보려는 것 - 원래 다른 값이어야 한다.
        raw["mode"] = "paper"
        raw["sizing"]["initial_ratio"] = 0.5
        server._validate_raw(raw)
        server._write_config_raw(raw)

        with open(cfg_path, "r", encoding="utf-8") as f:
            after_text = f.read()

        check("바꾼 값(mode)이 저장됨", "mode: paper" in after_text)
        check("바꾼 값(sizing.initial_ratio)이 저장됨", "initial_ratio: 0.5" in after_text)

        # ★ 원본에 있던 주석들이 저장 후에도 그대로 있는지 - 문서(README 등)가
        # 이 주석에 기대고 있으므로 사라지면 안 된다.
        sample_comments = [
            "# mode 는 5가지 중 하나:",
            "#   live   - 실제 계좌로 실주문 (원칙 11: 이 엔진은 항상 하나뿐)",
            "# 투자금액 배분·분할 매수/매도(세 시장 공통)."
            " 시장별 총 투자금액은 capital.allocation(국내, 원)·overseas.budget_usd(해외, 달러)·crypto.budget(암호화폐, 원).",
        ]
        for comment in sample_comments:
            check(f"주석 보존: {comment[:24]}...", comment in after_text)

        # ★ 키 순서 보존 - 예전 safe_dump 는 sort_keys 여부와 무관하게
        # 원본 순서를 기억하지 못했다. 최상위 섹션들이 원래 순서 그대로인지 본다.
        top_keys_before = [
            line.split(":", 1)[0] for line in before_text.splitlines()
            if line and not line.startswith(("#", " ")) and ":" in line
        ]
        top_keys_after = [
            line.split(":", 1)[0] for line in after_text.splitlines()
            if line and not line.startswith(("#", " ")) and ":" in line
        ]
        check("최상위 키 순서 보존", top_keys_before == top_keys_after,
              f"before={top_keys_before} after={top_keys_after}")

        # ★ [4-3] 덮어쓰기 직전의 파일이 .bak 으로 남아 있어야 한다.
        bak_path = cfg_path + ".bak"
        check("config.yaml.bak 이 생성됨", os.path.exists(bak_path))
        if os.path.exists(bak_path):
            with open(bak_path, "r", encoding="utf-8") as f:
                bak_text = f.read()
            check("config.yaml.bak 은 저장 전(변경 전) 내용", bak_text == before_text)
    finally:
        server.CONFIG_PATH = orig_config_path
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> None:
    tests = [test_save_keeps_comments_order_and_backup]
    for t in tests:
        t()

    print(f"\n총 {_total}건 중 실패 {len(_failures)}건")
    if _failures:
        print("실패한 검증:")
        for name in _failures:
            print(f"  - {name}")
        sys.exit(1)
    print("모두 통과했습니다.")


if __name__ == "__main__":
    main()
