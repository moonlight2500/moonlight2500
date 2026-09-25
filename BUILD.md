# AutoDayTrading — 빌드 및 실행 방법

## 1. 파일 구조 (전체 44개 파일)

```
AutoDayTrading/
├── README.md                 사용자 안내 (원칙·모드·안전장치·알려진 한계)
├── BUILD.md                  이 문서
├── requirements.txt           의존 패키지 목록
├── config.yaml                 설정 (모드·자금·위험관리·기법·비용 등)
├── themes.yaml                 테마 사전 (11개 테마, 57종목)
├── run.py                      CLI (개발·진단용: net/check/select/run/report 등)
├── tray.py                     트레이 실행기 (exe 빌드 시 진입점)
├── build_exe.bat                PyInstaller 빌드 스크립트 (cp949+CRLF)
│
├── daytrader/                    백엔드 패키지
│   ├── __init__.py
│   ├── paths.py                실행 경로 판정 (exe/소스 구분)
│   ├── timeutil.py               KST 시각 유틸
│   ├── ticks.py                   호가단위·비용 계산
│   ├── config.py                  설정 로드·검증 (dataclass)
│   ├── signals.py                 지표 (sma/ema/vwap/atr 등)
│   ├── playbook.py                 매매 기법 12종 (진입 7 + 청산 5)
│   ├── netutil.py                  프록시/인증서 대응 세션
│   ├── clock.py                    시계 (실시간/시뮬레이션/멈춤)
│   ├── tossapi.py                   토스증권 Open API 클라이언트
│   ├── orders.py                    주문 원장 (멱등성)
│   ├── broker.py                    PaperBroker/LiveBroker
│   ├── simulator.py                  가짜 시장(SimClient)
│   ├── screener.py                   종목 선정
│   ├── journal.py                    매매일지
│   ├── ledger.py                     거래 원장·집계
│   ├── engine.py                     매매 엔진 (메인 루프)
│   ├── safety.py                     실거래 안전장치 (사전점검·계좌대조)
│   ├── runner.py                     엔진 백그라운드 실행기
│   ├── server.py                     FastAPI 웹 서버 (전체 API)
│   ├── router.py                     시세 라우터 (토스 우선, 인터넷 폴백)
│   ├── webquote.py                    인터넷 공개 시세 (네이버)
│   ├── news.py                        속보 수집
│   ├── principles.py                   매매원칙 문서 (설정에서 동적 생성)
│   ├── session.py                      장 국면 판정 (개장전/매매중/마감 등)
│   ├── market.py                       시장 탭 (지수·환율·코인 등)
│   ├── notify.py                       텔레그램 알림
│   ├── review.py                       월간 리뷰·최적화 제안
│   ├── lab.py                          실험실 (설정 비교)
│   └── selftest.py                     토스 API 연계 테스트
│
├── web/                            프론트엔드
│   ├── index.html
│   └── static/
│       ├── style.css                디자인 시스템
│       ├── ui.js                     RealtimeChart/DataGrid 등 공통 위젯
│       ├── chart.js                   분봉 캔들 차트
│       ├── forms.js                   설정 폼·기법 배지
│       └── app.js                     화면 전체 (13개 탭)
│
└── tests/                          오프라인 테스트 (네트워크 불필요)
    ├── test_offline.py               63건 - 계산·설정·지표 기본 검증
    ├── test_live_safety.py            31건 - 실거래 사고 시나리오 (가장 중요)
    └── test_engine_loop.py             22건 - 하루 전체 엔진 흐름
```

## 2. 개발 환경에서 바로 실행하기 (exe 빌드 없이)

```bash
# 1) 패키지 설치
pip install -r requirements.txt

# 2) 오프라인 테스트로 정상 여부 확인 (네트워크 불필요, 30초 내외)
python tests/test_offline.py
python tests/test_live_safety.py
python tests/test_engine_loop.py

# 3) 웹 서버 실행
python run.py ui
# → http://127.0.0.1:8000 접속
```

기본 `config.yaml`의 `mode: web` 상태로는 실시간 시세만 관찰하며 주문을 내지 않습니다.
연습해보려면 `run.py run --sim` 으로 가짜 시장을 돌려볼 수 있습니다:

```bash
python run.py run --sim
```

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
3. 대시보드가 뜨고 `/static/*` 가 서빙되는지
4. 실제 시세로 종목 선정이 되는지 ([준비·연결]에서 토스 API 키 등록 후)
5. 트레이 아이콘이 뜨는지 (윈도우 11은 작업표시줄의 `^` 숨김 아이콘 확인)
6. 부모(트레이)를 끄면 자식(서버) 프로세스도 같이 내려가는지 (작업 관리자로 확인)

## 4. 실거래를 시작하기 전에

1. [준비·연결] → 바깥 연동에서 토스증권 API 키를 등록합니다.
2. 같은 화면의 **연계 테스트**를 눌러 15개 항목이 통과하는지 확인합니다
   (읽기 전용이라 계좌에 아무 변화도 없습니다).
3. `config.yaml` 의 `mode` 를 `paper`(모의매매)로 먼저 돌려보고,
   [매매원칙] 화면에서 지금 설정이 뭘 하는지 다시 읽어보세요.
4. 실거래(`mode: live`)로 바꾸면, 시작할 때 `실매매` 라는 문구를 정확히
   입력해야 진행됩니다 (CLI는 `DAYTRADER_NO_CONFIRM=1` 로만 우회 가능).

## 5. 참고

- 텔레그램 알림은 실거래(`live`) 모드에서만 자동으로 나갑니다. 연습 모드는
  기록만 남습니다.
- 토스 API 키·텔레그램 봇 토큰이 없어도 `web`/`sim` 모드는 정상 동작합니다
  (인터넷 공개 시세로 대체됩니다).
- 자세한 사용법과 원칙은 `README.md` 를 참고하세요.
