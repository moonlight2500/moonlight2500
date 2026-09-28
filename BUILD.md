# AutoDayTrading — 빌드 및 실행 방법

## 1. 파일 구조 (daytrader/ 52개 파일 · tests/ 20개 파일)

```
AutoDayTrading/
├── README.md                    사용자 안내 (원칙·모드·시장별 안내·안전장치·알려진 한계)
├── BUILD.md                     이 문서
├── requirements.txt              실행에 필요한 패키지 목록 (버전 고정)
├── requirements-dev.txt          테스트에만 필요한 패키지 (httpx2·playwright)
├── config.yaml                    설정 (모드·자금·위험관리·기법·비용·시장별 설정 등)
├── themes.yaml                    테마 사전
├── run.py                         CLI (개발·진단용: net/check/select/run/report/ui/db-backup 등)
├── tray.py                        트레이 실행기 (exe 빌드 시 진입점)
├── build_exe.bat                   PyInstaller 빌드 스크립트 (cp949+CRLF)
│
├── daytrader/                       백엔드 패키지
│   ├── __init__.py
│   ├── paths.py                   실행 경로 판정 (exe/소스 구분, 사용자 파일 위치)
│   ├── timeutil.py                  KST 시각 유틸
│   ├── ticks.py                      호가단위·비용(본전 상승률) 계산
│   ├── config.py                     설정 로드·검증 (dataclass)
│   ├── signals.py                    지표 (sma/ema/vwap/atr 등)
│   ├── playbook.py                   매매 기법 전체(진입·청산) - 국내·해외·스윙·암호화폐가 공유
│   ├── crypto_playbook.py             (예전) 암호화폐 전용 기법 - 지금은 playbook.py 로 대체됐지만
│   │                                  자체 테스트(test_crypto_playbook.py)가 남아 있어 그대로 둠
│   ├── netutil.py                    프록시/인증서 대응 세션
│   ├── clock.py                      시계 (실시간/시뮬레이션/멈춤)
│   ├── tossapi.py                     토스증권 Open API 클라이언트 (국내·해외 공용)
│   ├── bithumb_api.py                 빗썸 Open API 2.0 클라이언트 (암호화폐 시세·주문)
│   ├── orders.py                      주문 원장 (멱등성, daytrader.db 의 order_intents 표)
│   ├── broker.py                      PaperBroker/LiveBroker (국내주식)
│   ├── overseas_broker.py             해외주식 브로커
│   ├── bithumb_broker.py              빗썸 코인 매매 브로커
│   ├── swing_broker.py                스윙 매매 브로커
│   ├── simulator.py                   가짜 시장(SimClient, 국내주식)
│   ├── sim_feed.py                    해외주식·암호화폐 sim 모드용 합성 시세 생성기
│   ├── screener.py                    국내주식 종목 선정(테마 스크리닝)
│   ├── us_themes.py                   미국 주식 테마 분류·자동 선정
│   ├── sizing.py                      투자금액 배분·분할 매수/매도 (국내·해외·암호화폐 공통)
│   ├── db.py                          매매기록·로그 저장소(SQLite, state\daytrader.db) - 연결 관리·스키마·유지보수
│   ├── db_import.py                   기존 JSONL/JSON 기록을 daytrader.db 로 1회성 가져오기(멱등·재개 가능)
│   ├── applog.py                      WARNING 이상 애플리케이션 로그를 daytrader.db 의 app_log 표에도 남김
│   ├── journal.py                     매매일지 (daytrader.db 의 journal 표)
│   ├── ledger.py                      거래 원장·집계 (daytrader.db 의 trades/equity 표)
│   ├── perf_stats.py                  해외주식·암호화폐 성과 통계(승률·손익비·기법별)
│   ├── exit_efficiency.py             청산 효율 추적("더 기다렸으면 어땠을지")
│   ├── engine.py                      국내주식 매매 엔진 (메인 루프)
│   ├── overseas_engine.py             해외주식(미국) 매매 엔진
│   ├── crypto_engine.py               암호화폐(빗썸) 매매 엔진
│   ├── swing_engine.py                스윙(며칠~몇 주 보유) 매매 엔진 - 세 시장 공통
│   ├── technique_backtest.py          종목·테마별 기법 백테스트 ([실험실] 화면)
│   ├── auto_backtest.py               재선정마다 기법 백테스트를 자동으로 돌리는 백그라운드 스레드
│   ├── technique_prefs.py             종목·테마별 "이 기법이 잘 맞았다" 선호도 저장소
│   ├── lab.py                         실험실 - 섀도 비교·가상 실험(설정 비교)
│   ├── safety.py                      실거래 안전장치 (사전점검·계좌대조)
│   ├── runner.py                      엔진 백그라운드 실행기 (로그·이벤트를 웹으로 전달)
│   ├── devices.py                     접속 기기 승인 (새 기기 저장·신뢰 목록)
│   ├── secrets.py                     API 키·로그인 비밀번호 저장소 (secrets.yaml)
│   ├── server.py                      FastAPI 웹 서버 (전체 API·인증·보안 미들웨어)
│   ├── router.py                      국내주식 시세 라우터 (토스 우선, 인터넷 폴백)
│   ├── webquote.py                    인터넷 공개 시세 (네이버) - 토스 없을 때 폴백
│   ├── market.py                      [시장] 탭 시세 (국내외 지수·환율·코인 등, 매매와 무관)
│   ├── market_commentary.py           본장 마감 후 외부 시장 평가 요약
│   ├── news.py                        속보 수집 (헤드라인·출처·링크·시각만)
│   ├── news_guard.py                  뉴스·공시 위험 필터 (LLM 기반)
│   ├── llm.py                         AI(LLM) 호출 한 곳 - 뉴스 위험 필터 전용
│   ├── principles.py                  매매원칙 문서 (설정에서 동적 생성)
│   ├── session.py                     장 국면 판정 (개장전/매매중/마감 등)
│   ├── daily_review.py                하루 매매 복기 (장 마감 후 텔레그램 전송)
│   ├── review.py                      월간 리뷰·최적화 제안·반영 이력
│   ├── notify.py                      텔레그램 알림
│   └── selftest.py                    토스 API 연계 테스트 (연결·기능 확인)
│
├── web/                             프론트엔드
│   ├── index.html                   로그인·재확인 모달, 앱 셸(사이드바/하단 탭바)
│   └── static/
│       ├── style.css                 디자인 시스템(사이드바·하단 탭바·카드·표 등 공통 컴포넌트)
│       ├── ui.js                      아이콘(UI.icon)·RealtimeChart/DataGrid 등 공통 위젯
│       ├── chart.js                   분봉 캔들 차트
│       ├── forms.js                   설정 폼·기법 배지
│       ├── admin.js                   /admin (새 기기 승인) 페이지
│       └── app.js                     화면 전체 (16개 탭, 4개 그룹)
│
└── tests/                          오프라인 테스트(대부분 네트워크 불필요) - 20개 파일
    ├── test_offline.py               계산·설정·지표 등 기본 검증
    ├── test_live_safety.py           실거래 사고 시나리오 (가장 중요)
    ├── test_engine_loop.py           가짜 시장으로 국내주식 하루 전체 완주 검증
    ├── test_db.py                    매매기록·로그 저장소(SQLite, daytrader/db.py) 검증
    ├── test_overseas_engine.py       해외주식 엔진 검증
    ├── test_crypto_engine.py         암호화폐(빗썸) 엔진 검증
    ├── test_crypto_playbook.py       (예전) 암호화폐 전용 기법(crypto_playbook.py) 검증
    ├── test_swing.py                 스윙 엔진 검증
    ├── test_bithumb_broker.py        빗썸 브로커 검증
    ├── test_scaling.py               분할 매수/매도(sizing) 검증
    ├── test_sizing.py                투자금액 배분 검증
    ├── test_exit_efficiency.py       청산 효율 추적 검증
    ├── test_technique_backtest.py    기법 백테스트 검증
    ├── test_screener.py              국내주식 종목 선정 검증
    ├── test_news_guard.py            뉴스 위험 필터 검증
    ├── test_market_priority.py       [시장] 탭 시세 우선순위·폴백 검증
    ├── test_security.py              로그인·세션·Host/Origin 검증 등 보안 검증
    ├── test_devices.py               새 기기 승인 검증
    ├── test_session.py               장 국면 판정 검증
    ├── test_config_save.py           설정 저장(ruamel.yaml 라운드트립, 주석 보존) 검증
    └── ui_smoke.py                   Playwright 로 16개 화면을 실제로 열어보는 스모크 테스트
                                       (playwright 설치·브라우저가 없으면 건너뜀)
```

## 2. 개발 환경에서 바로 실행하기 (exe 빌드 없이)

```bash
# 1) 패키지 설치
pip install -r requirements.txt
# 테스트까지 돌리려면(httpx2·playwright 포함):
pip install -r requirements-dev.txt

# 2) 오프라인 테스트로 정상 여부 확인 (네트워크 불필요, 대부분 수 초 내외)
python tests/test_offline.py
python tests/test_live_safety.py
python tests/test_engine_loop.py
# tests/ 안의 나머지 test_*.py 도 같은 방식으로 개별 실행할 수 있습니다.

# 3) 웹 서버 실행
python run.py ui
# → http://127.0.0.1:8000 접속 (기본 비밀번호 123456, 로그인 필요)
```

기본 `config.yaml` 의 `mode` 는 `sim`(가짜 시장) 입니다 — 그대로 실행해도
실제 계좌와 무관하게 안전하게 화면을 둘러볼 수 있습니다. 실시간 시세만
관찰하고 싶다면 `mode: web` 으로, 가짜 시장으로 매매 흐름을 보고 싶다면
`run.py run --sim` 을 씁니다:

```bash
python run.py run --sim
```

국내주식 외에 해외주식(`overseas.enabled`)·암호화폐(`crypto.enabled`)·
스윙(`swing.enabled`)은 `config.yaml` 에서 각자 따로 켜고 끕니다 - 자세한
내용은 `README.md` 의 해당 절을 보세요.

## 3. Windows exe로 배포하기

```bat
build_exe.bat
```

- Python이 설치되어 있어야 합니다 (`where python` 으로 확인).
- PyInstaller를 자동 설치하고, `requirements.txt`를 설치한 뒤,
  `dist\AutoDayTrading\AutoDayTrading.exe` 를 만듭니다.
- 결과 폴더 크기는 대략 47MB 입니다.
- 빌드가 끝나면 `config.yaml`, `themes.yaml` 을 배포 폴더에 복사합니다.

### exe 검증 순서 (build_exe.bat 실행 후 화면에도 안내됩니다)

**반드시 `dist\AutoDayTrading` 폴더를 다른 경로로 복사한 뒤** 아래를 확인하세요
(같은 자리에서 확인하면 소스 폴더와 섞여 결과를 믿을 수 없습니다):

1. `AutoDayTrading.exe --check` — 파일·패키지·포트·설정·연결이 전부 정상인지
2. 실행한 뒤 exe 옆에 `config.yaml`, `themes.yaml`, `logs\`, `state\` 가 생겼는지
   (임시 폴더가 아니라 exe 옆이어야 합니다)
3. 대시보드가 뜨고(비밀번호 로그인 통과 후) `/static/*` 가 서빙되는지
4. 실제 시세로 종목 선정이 되는지 ([준비·연결]에서 토스 API 키 등록 후)
5. 트레이 아이콘이 뜨는지 (윈도우 11은 작업표시줄의 `^` 숨김 아이콘 확인)
6. 부모(트레이)를 끄면 자식(서버) 프로세스도 같이 내려가는지 (작업 관리자로 확인)

## 4. 실거래를 시작하기 전에

1. [준비·연결] → 바깥 연동에서 토스증권 API 키를 등록합니다(암호화폐는
   빗썸 API 키, 텔레그램 알림은 봇 토큰을 같은 화면에서 등록합니다).
2. 같은 화면의 **연계 테스트**를 눌러 항목이 통과하는지 확인합니다
   (읽기 전용이라 계좌에 아무 변화도 없습니다).
3. `config.yaml` 의 해당 시장 `mode` 를 `paper`(모의매매)로 먼저 돌려보고,
   [매매원칙] 화면에서 지금 설정이 뭘 하는지 다시 읽어보세요.
4. 실거래(`mode: live`)로 바꾸면, 시작할 때 `실매매` 라는 문구를 정확히
   입력해야 진행됩니다 (CLI는 `DAYTRADER_NO_CONFIRM=1` 로만 우회 가능).
5. **로그인 비밀번호를 기본값(123456)에서 반드시 바꾸세요** — 실거래
   계좌를 움직일 수 있는 유일한 잠금입니다. 자세한 내용은 `README.md`
   의 "로그인과 접속 보안" 절을 보세요.

## 5. 참고

- 텔레그램 알림은 실거래(`live`) 모드에서만 자동으로 나갑니다. 연습 모드는
  기록만 남습니다.
- 토스 API 키·빗썸 API 키·텔레그램 봇 토큰이 없어도 `web`/`sim`/`paper`
  모드는 정상 동작합니다(인터넷 공개 시세로 대체됩니다).
- 자세한 사용법과 원칙, 시장별(국내·해외·암호화폐·스윙) 안내, 화면 구성
  (사이드바/하단 탭바)은 `README.md` 를 참고하세요.
