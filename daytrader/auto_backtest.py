"""종목이 새로 선정되면(재선정) 실험실 백테스트(technique_backtest.py)를 자동으로 돌려,
결과를 technique_prefs.py 에 저장한다 - 그러면 다음 진입 판정부터 이 종목·테마에 잘 맞았던
기법이 가산점을 받는다(daytrader/playbook.py 의 _preference_multiplier).

★★★ 엔진의 주 루프를 절대 막지 않는다 - 종목 하나를 재생하는 데도 실제 API 를 여러 번 부르고,
후보가 여러 개면 수 초~수십 초가 걸릴 수 있다. 그래서 무거운 부분(technique_backtest.run())은
항상 별도 데몬 스레드에서 돌리고, 실패해도 예외를 삼켜 매매에 영향이 없게 한다(다음 재선정 때
다시 시도한다).

★ 같은 날 같은 종목 조합이면 다시 안 돈다(technique_prefs.already_ran_today) - 국내는 30분,
해외는 60분, 암호화폐는 하루 한 번 재선정이 도는데, 매번 다시 돌리면 API 호출만 낭비된다.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_running: set = set()  # 지금 백테스트가 돌고 있는 시장 - 같은 시장을 동시에 두 번 안 돌린다.
_lock = threading.Lock()


def maybe_trigger(cfg, client, market: str, candidates: list, *, days: int = 7, symbol_limit: int = 10) -> bool:
    """조건이 맞으면(후보가 있고, 오늘 이 조합으로 아직 안 돌렸고, 지금 같은 시장이 돌고 있지
    않으면) 백그라운드 스레드로 백테스트를 시작한다. 시작했으면 True(참고용 - 끝날 때까지
    기다리지 않는다).

    ★ candidates 는 호출하는 엔진이 재선정 때 이미 뽑아 둔 목록을 그대로 받는다([{symbol,name,
    theme}, ...]) - 여기서 다시 뽑으면(랭킹·테마 API 재호출) 재선정마다(국내 10~30분, 해외
    60분마다) 매번 헛돈이 나간다. 실제로 무거운 일(종목별 시세 재생)만 백그라운드에서 돈다."""
    from daytrader import technique_backtest, technique_prefs

    candidates = (candidates or [])[:symbol_limit]
    sig = technique_prefs.candidate_signature(candidates)
    if not sig or technique_prefs.already_ran_today(cfg, market, sig):
        return False

    with _lock:
        if market in _running:
            return False
        _running.add(market)

    def _run() -> None:
        try:
            technique_backtest.run(
                cfg, client, market, days=days, symbol_limit=symbol_limit,
                save=True, candidates=candidates, trigger="auto",
            )
            log.info("실험실 자동 백테스트 완료(%s, %d종목) - 진입 기법 채점에 반영됩니다.", market, len(candidates))
        except Exception as exc:
            log.warning("실험실 자동 백테스트 실패(%s) - 다음 재선정 때 다시 시도합니다: %s", market, exc)
        finally:
            with _lock:
                _running.discard(market)

    threading.Thread(target=_run, name=f"auto-backtest-{market}", daemon=True).start()
    return True
