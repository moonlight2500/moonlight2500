# AutoDayTrading

실제 돈으로 매매하는 자동매매 프로그램이다. 국내주식은 토스증권, 해외주식은 토스 해외, 코인은 빗썸 API로 실주문을 낸다.
대시보드는 FastAPI 서버와 순수 JS 프론트엔드로 되어 있다. 윈도우에서 `tray.py` 가 서버를 띄우고, 배포는 PyInstaller exe로 한다.
분석·수정 이력과 남은 할 일은 `docs/WORK_LOG.md` 에 있다. 필요할 때만 읽는다.

## 명령
- 테스트: `for f in tests/test_*.py; do python $f || echo FAIL $f; done`
  - 테스트 파일은 단독 실행 스크립트이고, 실패하면 종료코드 1을 낸다. pytest는 쓰지 않는다.
  - CI(`.github/workflows/tests.yml`)가 윈도우와 우분투에서 같은 방식으로 돈다.
- 설치: `pip install -r requirements.txt -r requirements-dev.txt` (버전 고정됨)
- 서버: `python run.py ui` → http://127.0.0.1:8000. 기본 모드는 `sim` 이다.
- UI 점검: `python tests/ui_smoke.py` (Playwright. 없으면 건너뛴다)
- 백테스트 연구: `python -m research.run --source {synthetic,csv,yahoo,toss} --days 30`. 설명은 `research/README.md`.

## 구조 (daytrader/)
- `engine.py`: 국내 매매 메인 루프(`try_entries`, `manage_positions`, `_close_position`, 강제청산). `runner.py` 가 백그라운드로 돌린다.
- `playbook.py`: 진입·청산 기법. `signals.py`: 지표(ATR은 Wilder 방식). `sizing.py`: 사이징과 분할매수·매도. `screener.py`: 테마·종목 선정.
- `broker.py`(국내 Paper/Live), `orders.py`(주문 원장·멱등성), `safety.py`(사전점검 `preflight`, 계좌대조 `reconcile`), `tossapi.py`
- `overseas_engine.py`/`overseas_broker.py`, `crypto_engine.py`/`bithumb_broker.py`/`bithumb_api.py`, `swing_engine.py`/`swing_broker.py`
- `server.py`: API 전체. 인증(비밀번호, 세션, 기기승인, 재확인 토큰), Origin/Host 검사, CSP가 여기 있다. `session.py`: 장 국면·휴장일.
- `config.py`: 설정 dataclass와 검증. `config.yaml`: 사용자 설정(주석 보존 저장). `secrets.py`: `secrets.yaml`(윈도우 DPAPI 암호화).
- `llm.py`(Groq, 키 2개 폴백), `market_commentary.py`(AI 시황), `news_guard.py`(AI 뉴스 위험 필터, 실패하면 통과시킴 = 의도된 설계)
- 프론트(web/): `index.html` 셸, `static/app.js`(탭별 `registerPanel`), `ui.js`(`UI.icon`, 위젯), `style.css`(디자인 토큰과 공통 컴포넌트)

## 반드시 지킬 규칙
- 시간은 항상 KST-aware(`timeutil.now_kst()`, `day_str()`)로 다룬다. `datetime.now()` 를 새로 쓰지 않는다.
- 실주문은 주문 원장에 먼저 기록하고 client order id를 붙인다. 응답이 불확실하면 재전송하지 말고 조회로 확정하고, 확정하지 못하면 멈춘다. 국내·해외·빗썸 모두 이 방식이다.
- 포지션 수량을 바꾸면 서버 OCO(손절·익절)도 취소하고 같은 수량으로 다시 건다.
- 서버의 async 라우트에서 네트워크·IO를 동기로 호출하지 않는다(이벤트 루프가 막힌다). 그런 라우트는 `def` 로 만들거나 스레드로 돌린다. `tests/test_async_routes.py` 가 이를 검사한다.
- 프론트에서 서버·외부 데이터를 `html:` 로 넣을 때는 반드시 `esc()` 를 거친다. `tests/test_security.py` 가 정적으로 검사한다. CSP 때문에 인라인 스크립트는 금지다.
- 새 CSS는 `style.css` 의 토큰(`--ink`, `--accent`, `--rise`(빨강=상승), `--fall`(파랑=하락))과 공통 클래스(`.page-head`, `.kpi-grid`, `details.acc`, `.scroll-box`, `.chips`)를 쓴다.
- 커밋하지 않는 파일: `secrets.yaml`, `state/`, `logs/`, `config.yaml.bak`, `research/cache/`, `research/out/`
- 수정할 때는 재현 테스트를 먼저 추가한다. 실거래 경로를 바꾸면 paper/sim 경로도 같은 의미로 맞춘다.
