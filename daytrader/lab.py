"""실험실 - 여러 설정을 동시에 비교한다.

두 가지 실험:
① 섀도 비교(장중) — 실제 매매는 지금 돌고 있는 엔진 하나가 한다. 실험실은
   그 옆에서 같은 실제 시세를 보며 다른 기법으로 가상 매매를 하고,
   "내가 그 기법을 썼다면 어땠을까"를 같은 표에 놓는다.
② 대조 실험(장외) — 전부 가상. 시뮬레이션 시드를 하나로 고정한다.

★★ 실제 주문을 내는 엔진은 언제나 하나뿐이다. 세 겹으로 막는다:
   1차 _virtual_mode() - live 가 오면 paper 로 바꾼다.
   2차 변환 후에도 live 이거나 VIRTUAL_ONLY 밖이면 예외를 던진다.
   3차 엔진을 만든 뒤 실제 broker 타입이 PaperBroker 인지 확인한다.
   같은 계좌에 두 엔진이 동시에 주문을 내면 수량·한도·OCO 가 전부 어긋나고
   사후에 복구할 방법이 없다. 한 곳만 막으면 언젠가 뚫린다.
"""

from __future__ import annotations

import copy
import os
import random
import shutil
import threading
import time

from daytrader.broker import PaperBroker

VIRTUAL_ONLY = ("sim", "replay", "web", "paper")
MAX_VARIANTS = 3


def _virtual_mode(mode: str) -> str:
    """1차 방어 - live 가 오면 paper 로 바꿔서라도 절대 실주문을 내지 않는다."""
    return "paper" if mode == "live" else mode


def _apply_override(cfg, path: str, value) -> None:
    parts = path.split(".")
    obj = cfg
    for p in parts[:-1]:
        obj = getattr(obj, p)
    setattr(obj, parts[-1], value)


# ━━ 참가자 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Variant:
    """가상 참가자 하나. ★ 참가자마다 state_dir 을 따로 준다 - 같은 원장에
    쓰면 결과가 섞인다.
    """

    def __init__(self, vid, name, overrides, base, root, seed, speed, start, note, shared_client=None):
        self.vid = vid
        self.name = name
        self.note = note
        self.overrides = overrides or {}
        self.shared_client = shared_client
        self.engine = None
        self.thread = None
        self.error = None

        for key in self.overrides:
            # ★ overrides 에 mode 나 notify.* 가 오면 거부한다.
            if key == "mode" or key.startswith("notify."):
                raise RuntimeError(f"실험실에서는 바꿀 수 없는 설정입니다: {key}")

        cfg = copy.deepcopy(base)
        mode = _virtual_mode(cfg.mode)
        # ★★ 2차 방어 - 변환 후에도 live 이거나 VIRTUAL_ONLY 밖이면 거부한다.
        if mode == "live" or mode not in VIRTUAL_ONLY:
            raise RuntimeError(f"실험실은 실거래 모드를 만들 수 없습니다: {mode}")
        cfg.mode = mode

        variant_dir = os.path.join(root, vid)
        os.makedirs(variant_dir, exist_ok=True)
        cfg.state_dir = variant_dir
        cfg.log_dir = variant_dir

        # ★ style 오버라이드는 먼저 적용하고 apply_style() 로 손절/익절/트레일링 등 파생값을
        # 다시 계산한다 - 안 그러면 style 문자열만 바뀌고 실제 매매 폭은 base(보통 normal)에
        # 멈춰 있는다. 그 다음에 나머지 오버라이드(예: risk.stop_loss_pct 직접 지정)를 적용해야
        # 사용자가 명시한 값이 style 파생값보다 항상 우선한다.
        # ★★★ "단타 매매 모드를 시장별로 분리해" 이후 style 은 더 이상 cfg 전체의 값이 아니라
        # risk/overseas/crypto 가 각자 갖는다. 실험실의 "style" 오버라이드(프리셋 style_fast 등)는
        # "전부 이 속도로 맞춰서 비교해 본다"는 의도이므로, 세 시장 모두에 같은 값을 넣는다 -
        # 개별 시장만 바꿔 보고 싶으면 "risk.style"/"overseas.style"/"crypto.style" 을 직접 넘기면 된다.
        remaining = dict(self.overrides)
        style_override = remaining.pop("style", None)
        if style_override is not None:
            from daytrader.config import apply_style
            cfg.risk.style = cfg.overseas.style = cfg.crypto.style = style_override
            apply_style(cfg)
        for key, value in remaining.items():
            _apply_override(cfg, key, value)

        # ★ notify 를 강제로 끈다 - 실험 한 번에 알림 수십 통이 갈 이유가 없다.
        cfg.notify.enabled = False

        cfg.simulation.seed = seed
        cfg.simulation.speed = speed
        cfg.simulation.start_time = start

        self.cfg = cfg
        self.seed = seed

    def run(self, stop_flag=None) -> None:
        from daytrader.clock import make_clock
        from daytrader.engine import Engine
        from daytrader.simulator import SimClient

        try:
            client = self.shared_client
            if client is None:
                clock = make_clock(self.cfg)
                client = SimClient(self.cfg, clock=clock)

            engine = Engine(self.cfg, client)
            # ★★ 3차 방어 - 엔진을 만든 뒤 실제 broker 타입을 확인한다.
            if not isinstance(engine.broker, PaperBroker):
                raise RuntimeError("실험실 참가자의 broker 가 PaperBroker 가 아닙니다 - 실행을 거부합니다.")
            self.engine = engine
            engine.run()
        except Exception as exc:
            self.error = str(exc)

    def progress(self) -> dict:
        if self.engine is None:
            return {"vid": self.vid, "name": self.name, "note": self.note, "status": "대기 중",
                    "trades": 0, "realized_pnl": 0, "positions": 0, "error": self.error}
        snap = self.engine.snapshot()
        status = "완료" if self.engine.ended_at is not None else "진행 중"
        return {
            "vid": self.vid, "name": self.name, "note": self.note, "status": status,
            "trades": snap.get("trades", 0), "realized_pnl": snap.get("realized_pnl", 0),
            "positions": len(snap.get("positions", {})), "error": self.error,
        }

    def result(self) -> dict:
        from daytrader import review
        from daytrader.ledger import Ledger

        trades = []
        if self.engine is not None:
            trades = Ledger(self.cfg.state_dir).trades(modes=[self.cfg.mode])
        stat = review._stat(trades)
        stat.update({"vid": self.vid, "name": self.name, "note": self.note, "real": False})
        return stat


class RealParticipant:
    """실거래 엔진을 읽기만 하는 껍데기. ★ 실험실은 이 엔진을 만들지도
    멈추지도 고치지도 않는다 - 읽기만 한다.
    """

    vid = "real"

    def __init__(self, engine):
        self.engine = engine
        self.name = "실제 매매(지금 쓰는 설정)"
        self.note = "지금 실제로 주문을 내고 있는 엔진입니다."

    def progress(self) -> dict:
        snap = self.engine.snapshot()
        return {
            "vid": self.vid, "name": self.name, "note": self.note, "status": "진행 중",
            "trades": snap.get("trades", 0), "realized_pnl": snap.get("realized_pnl", 0),
            "positions": len(snap.get("positions", {})), "error": None,
        }

    def result(self) -> dict:
        """오늘치 원장만 집계한다."""
        from daytrader import review
        from daytrader.ledger import Ledger
        from daytrader.timeutil import day_str

        today = day_str(self.engine.clock.now())
        trades = [t for t in Ledger(self.engine.cfg.state_dir).trades(modes=[self.engine.cfg.mode])
                  if t.get("date") == today]
        stat = review._stat(trades)
        stat.update({"vid": self.vid, "name": self.name, "note": self.note, "real": True})
        return stat


# ━━ 판정 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BIAS_NOTE = "가상 참가자의 체결은 원하는 가격에 전량 즉시 채워진다고 가정합니다."


def _verdict(ranked: list, real_id: str | None = None) -> tuple:
    """★ 순위는 조건을 걸고 낸다 (A-22). (문장, 주의사항 목록) 을 돌려준다."""
    cautions = [BIAS_NOTE]

    if len(ranked) < 2:
        return "비교할 대상이 부족합니다.", cautions

    top, second = ranked[0], ranked[1]

    if top["trades"] < 10 or second["trades"] < 10:
        return "거래 수가 적어 순위를 믿을 수 없습니다.", cautions

    diff = abs(top["expectancy"] - second["expectancy"])
    scale = abs(top["avg_loss"]) or abs(second["avg_loss"])
    if scale and diff < scale * 0.3:
        return "차이가 잡음보다 작아 우열을 가릴 수 없습니다.", cautions

    if real_id is not None and top["vid"] == real_id:
        return "지금 쓰는 설정이 앞섰습니다 — 바꿀 이유가 없습니다.", cautions

    return (
        "가상 체결은 미끄러짐이 없어 유리하게 나옵니다. 여러 날 반복해 같은 결과가 "
        "나오는지 먼저 확인하십시오.",
        cautions,
    )


# ━━ 실험실 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Lab:
    MAX_VARIANTS = MAX_VARIANTS

    def __init__(self, state_dir: str):
        self.root = os.path.join(state_dir, "lab")
        os.makedirs(self.root, exist_ok=True)
        self.variants: list = []
        self.real = None
        self.spec = None
        self._threads: list = []
        self.run_id = None

    def start(self, base, variants: list, speed: int = 600, start: str = "09:00",
              seed: int | None = None, live_engine=None) -> dict:
        if self.is_running():
            raise RuntimeError("이미 실험이 돌고 있습니다. 먼저 멈추세요.")
        if len(variants) > self.MAX_VARIANTS:
            raise RuntimeError(f"참가자는 최대 {self.MAX_VARIANTS}명까지입니다.")
        if live_engine is None and len(variants) < 2:
            # ★ 비교 대상이 없다 - 실거래가 없으면 가상 참가자가 둘 이상이어야 한다.
            raise RuntimeError("비교 대상이 없습니다 - 실거래 엔진이 없으면 가상 참가자가 둘 이상이어야 합니다.")

        seed = seed if seed is not None else random.randint(0, 999_999)
        self.run_id = f"run-{int(time.time())}"
        run_root = os.path.join(self.root, self.run_id)
        os.makedirs(run_root, exist_ok=True)

        # ★ shadow 참가자는 실제 시세를 실거래 엔진과 공유한다 - 참가자마다
        # 따로 붙으면 토스 호출이 참가자 수만큼 늘어난다.
        shared_client = live_engine.client if live_engine is not None else None

        self.variants = []
        for i, v in enumerate(variants):
            vid = f"v{i + 1}"
            self.variants.append(Variant(
                vid=vid, name=v["name"], overrides=v.get("overrides", {}),
                base=base, root=run_root, seed=seed, speed=speed, start=start,
                note=v.get("note", ""), shared_client=shared_client,
            ))

        self.real = RealParticipant(live_engine) if live_engine is not None else None

        fairness = f"모든 참가자에게 시뮬레이션 시드 {seed} 를 똑같이 줬습니다."
        if live_engine is not None:
            fairness += " 실거래 엔진과 같은 시세 클라이언트를 공유해 실제 시세를 함께 봅니다."

        self.spec = {
            "run_id": self.run_id, "seed": seed, "speed": speed, "start": start,
            "kind": "shadow" if live_engine is not None else "contrast",
            "fairness": fairness,
        }

        self._threads = []
        for variant in self.variants:
            t = threading.Thread(target=variant.run, name=f"lab-{variant.vid}", daemon=True)
            t.start()
            self._threads.append(t)

        return self.spec

    def stop(self) -> None:
        """★ '중지' 버튼은 가상 참가자만 멈춘다 - 실거래 엔진은 절대 건드리지 않는다."""
        for v in self.variants:
            if v.engine is not None:
                v.engine.request_stop(close_positions=False)

    def is_running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def status(self) -> dict:
        participants = [v.progress() for v in self.variants]
        if self.real is not None:
            participants.insert(0, self.real.progress())
        return {"running": self.is_running(), "spec": self.spec, "participants": participants}

    def result(self) -> dict:
        rows = [v.result() for v in self.variants]
        real_id = None
        if self.real is not None:
            real_row = self.real.result()
            rows.insert(0, real_row)
            real_id = real_row["vid"]

        ranked = sorted(rows, key=lambda r: r["expectancy"], reverse=True)
        verdict, cautions = _verdict(ranked, real_id=real_id)

        return {
            "spec": self.spec, "participants": rows, "ranked": [r["vid"] for r in ranked],
            "winner": ranked[0]["vid"] if ranked else None,
            "verdict": verdict, "caution": cautions,
        }

    def cleanup(self, keep: int = 5) -> dict:
        """오래된 실험 폴더를 정리한다. 최근 keep 개만 남긴다."""
        if not os.path.isdir(self.root):
            return {"removed": [], "kept": []}
        runs = sorted(d for d in os.listdir(self.root) if os.path.isdir(os.path.join(self.root, d)))
        to_remove = runs[:-keep] if len(runs) > keep else []
        for r in to_remove:
            shutil.rmtree(os.path.join(self.root, r), ignore_errors=True)
        return {"removed": to_remove, "kept": runs[len(to_remove):]}


# ━━ 프리셋 12개 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_ALL_ENTRY = ["breakout", "theme_leader", "vwap_pullback", "orb", "volume_dry_pop", "bull_flag", "ma_pullback"]

PRESETS = [
    {"id": "only_breakout", "name": "돌파 추종만",
     "overrides": {"strategy.entry_order": ["breakout"]},
     "note": "돌파 추종 기법 하나만 켭니다. 다른 기법과 비교할 기준선입니다."},
    {"id": "only_theme_leader", "name": "테마 대장주만",
     "overrides": {"strategy.entry_order": ["theme_leader"]},
     "note": "테마가 인정된 날에만 작동합니다. 조용한 날엔 거래가 없습니다."},
    {"id": "only_vwap_pullback", "name": "VWAP 눌림목만",
     "overrides": {"strategy.entry_order": ["vwap_pullback"]},
     "note": "거래량이 실린 종목이 VWAP 까지 되돌리는 순간만 잡습니다."},
    {"id": "only_vwap_reclaim", "name": "VWAP 재탈환만",
     "overrides": {"strategy.entry_order": ["vwap_reclaim"]},
     "note": "VWAP 아래로 밀렸다가 거래량을 실어 다시 올라서는 순간만 잡습니다. 새 기법이라 기록이 쌓일 때까지 결과를 가볍게 보세요."},
    {"id": "only_orb", "name": "개장 레인지 돌파만",
     "overrides": {"strategy.entry_order": ["orb"]},
     "note": "ORB: 오전에만 작동합니다. 스캔 시작이 09:30 이후면 한 건도 안 잡힙니다."},
    {"id": "only_volume_dry_pop", "name": "거래량 마름 후 분출만",
     "overrides": {"strategy.entry_order": ["volume_dry_pop"]},
     "note": "거래량이 바짝 마른 뒤 튀어오르는 순간만 잡습니다. 신호가 드뭅니다."},
    {"id": "only_bull_flag", "name": "강세 깃발형만",
     "overrides": {"strategy.entry_order": ["bull_flag"]},
     "note": "급등 후 짧은 횡보(깃발)를 기다립니다. 변동성 큰 날에 유리합니다."},
    {"id": "only_ma_pullback", "name": "이동평균 눌림목만",
     "overrides": {"strategy.entry_order": ["ma_pullback"]},
     "note": "추세 중 이동평균까지 눌린 자리만 잡습니다. 추세장에 유리합니다."},
    {"id": "all_techniques", "name": "전 기법 켜기",
     "overrides": {"strategy.entry_order": list(_ALL_ENTRY)},
     "note": "7개 기법을 모두 켜서 순서대로 확인합니다. 기회는 많아지지만 서로 다른 성격이 섞입니다."},
    {"id": "tight_stop", "name": "짧은 손절(1.5/3)",
     "overrides": {"risk.stop_loss_pct": 0.015, "risk.take_profit_pct": 0.03},
     "note": "손절 1.5%, 익절 3%. 손실을 작게 자르는 대신 자주 털릴 수 있습니다."},
    {"id": "wide_stop", "name": "넓은 손절(3.5/7)",
     "overrides": {"risk.stop_loss_pct": 0.035, "risk.take_profit_pct": 0.07},
     "note": "손절 3.5%, 익절 7%. 흔들림에 덜 털리는 대신 한 번의 손실이 커집니다."},
    {"id": "no_trailing", "name": "트레일링 없음",
     "overrides": {"strategy.exit_enabled": ["fixed", "time_stop"]},
     "note": "추적 손절을 빼고 고정 손절·익절과 시간 손절만 씁니다. 수익을 끝까지 안고 가는 대신 반납 위험이 있습니다."},
    {"id": "atr_stop", "name": "ATR 손절",
     "overrides": {"strategy.exit_enabled": ["fixed", "atr_stop", "time_stop"]},
     "note": "변동성(ATR) 기준 손절을 추가합니다. 변동성 큰 종목에서 손절선이 더 넓게 잡힙니다."},
    # ★ "이 시뮬레이션 로직을 실험실에 추가해" 요청 - 매매 모델(technique_learning_mode) 3단계와
    # 단타 속도(style) 를 실험실에서 서로 비교할 수 있는 프리셋. 국내주식(risk.*) 기준이며,
    # 실험실은 언제나 국내주식 엔진(daytrader.engine.Engine)으로만 돈다 - 다른 시장을 비교하고
    # 싶으면 지금은 각 시장 설정 화면에서 직접 technique_learning_mode 를 바꿔 며칠 지켜봐야 한다.
    {"id": "model_none", "name": "매매 모델: 기본 룰만",
     "overrides": {"risk.technique_learning_mode": "none"},
     "note": "과거 실적·백테스트 가산점을 전부 끄고 순수 신호 강도로만 진입 기법을 고릅니다."},
    {"id": "model_entry_pref", "name": "매매 모델: 진입 가산점(기본값)",
     "overrides": {"risk.technique_learning_mode": "entry_pref"},
     "note": "실전 승률·[실험실] 백테스트 결과를 진입 기법 채점에 가산점으로 반영합니다."},
    {"id": "model_entry_exit_pref", "name": "매매 모델: 진입+청산 가산점",
     "overrides": {"risk.technique_learning_mode": "entry_exit_pref"},
     "note": "진입 가산점에 더해, 종목별 청산 효율(exit_efficiency.py) 기록으로 재진입 시 익절 목표를 넓힙니다. 표본이 쌓일 시간이 필요해 짧은 실험에선 entry_pref 와 차이가 안 보일 수 있습니다."},
    {"id": "style_fast", "name": "속도: 빠른 단타(fast)",
     "overrides": {"style": "fast"},
     "note": "짧게 자주 먹는 좁은 손절/익절 폭으로 바꿉니다(손절 2.5%/익절 5%)."},
    {"id": "style_scalp", "name": "속도: 스캘핑(scalp)",
     "overrides": {"style": "scalp"},
     "note": "가장 짧고 좁은 스캘핑 폭으로 바꿉니다."},
]
