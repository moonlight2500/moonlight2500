# 작업 기록 (2026-09-25, PR #1)

원본 `c3cc073` 에 대해 분석한 결과와 수정 내역을 정리한 문서다. 코드는 PR #1(`b26a238`)로 `main` 에 합쳐졌다.
커밋 메시지 앞의 `[x-y]` 가 아래 항목 번호다. 특정 수정을 자세히 보려면 `git log --grep "\[1-2\]"` 로 커밋을 찾고 `git show <hash>` 로 본다.

## 1. 분석 요약

| 영역 | 핵심 발견 |
|---|---|
| 실거래 주문 | 최대 보유 종목 초과 매수, 재시작 시 보유 OCO 때문에 실거래 시작이 막힘, 해외·빗썸 이중 주문 위험, 유령 포지션, 호가 올림 오류 |
| 보안 | 기본 비밀번호 123456, 로그인 잠금 우회(XFF), 설정 경로가 앱 폴더 밖으로 나감, HTML 인젝션 약 15곳, 로그아웃해도 세션이 남음, Origin 검사가 포트를 무시 |
| 매매 기법 | ATR이 Wilder 방식이 아님, VWAP·당일고가가 최근 80봉 기준, 백테스트 체결 가정이 낙관적, 거래 1건짜리 결과가 실매매 가중치에 반영됨 |
| 진입 타이밍 (미수정, 4절) | 돌파류에 추격 상한 없음, 신호부터 체결까지 최대 30초 지연, 거래량 평균에 개장 봉이 섞임, 스프레드 확인 없음, 세션 구분 없음 |
| UI | 탭이 두 단계로 나뉘고 긴 스크롤, 모바일 탭 대상이 작음, 시장 탭에 예외 원문 노출, 첫 로딩 때 빈 화면 |
| 운영 | async 라우트가 네트워크 대기로 서버 전체를 멈춤, 문서와 코드 불일치, 의존성 버전 미고정, CI 없음 |

외부 공격 테스트(서버를 띄우고 직접 공격)에서 잘 막힌 것: 로그인 전 API 약 90개의 데이터 노출, 쿠키 위조, 경로 조작, 기기 자가 승인, 보안 헤더. `/docs` 와 `/openapi.json` 은 꺼져 있다.

## 2. 수정 내역

| 번호 | 내용 | 주요 파일 |
|---|---|---|
| UI | PC 사이드바(접기 가능), 모바일 하단 탭바와 칩 메뉴, 새 디자인 토큰, 선 아이콘, 화면별 요약 카드와 탭·접기 구조, 설정은 "섹션 목록 + 한 섹션" | web/* |
| 1-1 | 매수할 때마다 max_positions 재확인 | engine.py |
| 1-2 | preflight가 추적 중인 포지션의 주문·OCO는 허용하고 고아 주문만 차단 | safety.py, engine.py |
| 1-3 | reconcile이 수량을 고치면 OCO도 다시 등록, 실패하면 기록하고 알림 | safety.py |
| 1-4 | OCO 취소 경합 때 체결을 확인해 내부에서 청산(유령 포지션 방지) | engine.py |
| 1-5 | 해외·빗썸 주문에 의도 기록과 client id 적용, 불확실하면 조회로 확정하고 확정 못 하면 halt | overseas_broker.py, bithumb_broker.py, bithumb_api.py, orders.py |
| 1-6 | round_to_tick "up" 소수부 처리 | ticks.py |
| 1-7 | cash 0을 값으로 인정 | broker.py |
| 1-8 | 강제청산 마감이 15:19를 넘는 설정 거부(동시호가 회피) | config.py |
| 1-9 | 일일 손실 한도에 평가손익 포함 | engine.py |
| 1-10 | 해외·코인·스윙의 "오늘"을 KST로 통일 | overseas_engine.py, crypto_engine.py, swing_engine.py |
| 1-11 | 스윙 해외 종목 환율 반영 | swing_broker.py |
| 3-1 | ATR을 Wilder 방식으로. 손절폭이 약 19% 넓어져 atr_multiple 재조정 검토 필요 | signals.py |
| 3-2 | 세션 누적 VWAP·당일고가(엔진이 09:00부터 누적) | engine.py, playbook.py |
| 3-3 | 변동성 돌파에 실제 전일 고저 사용(코인은 기존 근사 유지) | playbook.py, engine.py, overseas_engine.py |
| 3-4 | 백테스트 체결을 다음 봉 시가 + 슬리피지로, best 선정에 최소 거래 수 적용 | technique_backtest.py |
| 3-5 | 코인·해외·스윙 익절폭과 비용 검증 | config.py |
| 2-1 | 첫 실행 때 무작위 비밀번호 생성, 기본 비밀번호 상태에서는 원격 로그인 금지 | server.py, secrets.py |
| 2-2 | 전역 로그인 실패 한도와 파일 영속화. XFF는 `DAYTRADER_TRUSTED_PROXY=1` 일 때만 신뢰 | server.py |
| 2-3 | 경로 설정은 앱 폴더 안의 상대경로만, /api/config는 알려진 키만 받음 | config.py, server.py |
| 2-4 | /api/notify/test에 임의 토큰을 주면 설정 재확인 요구 | server.py |
| 2-5 | Origin을 scheme+host+port로 비교, 상태 변경 요청에 신호가 없으면 거부(트레이 토큰은 예외) | server.py |
| 2-6 | 로그아웃이 서버에서 세션 무효화, "모든 기기 로그아웃" 추가 | server.py |
| 2-7 | app.js html 삽입 지점 이스케이프와 정적 검사 테스트 | app.js, tests/test_security.py |
| 2-8 | defusedxml, 오류 원문 숨김, /admin 토큰을 1회용 쿠키로 | news.py, webquote.py, market.py, server.py |
| 6-1~5 | Groq: 하루 호출 수 영속화, 입력 해시 20분 캐시, 프롬프트 인젝션 방어 문구, 발행·작성 시각(KST) 표시, 실패 원인 구분 표시 | news_guard.py, market_commentary.py, server.py |
| 4-1~4 | 시장 탭 오류를 코드·배너로 표시, 공통 로딩 스켈레톤, config.yaml.bak 백업, tests/ui_smoke.py | market.py, app.js, server.py |
| 5-1~3 | README·BUILD 문서 동기화, 의존성 고정(requirements-dev 분리), CI 워크플로 | 문서, requirements*, .github |
| 8-1 | async 라우트 7개가 이벤트 루프를 막던 문제(def로 전환) | server.py, tests/test_async_routes.py |
| 7-1~3 | 세션별 백테스트 연구 도구(research/), 스크리너 게이팅 근사 | research/* |

테스트는 파일 31개가 모두 통과하고, CI는 윈도우와 우분투 모두 초록이다.

## 3. 백테스트 결과 (야후 1분봉, 56종목, 2026-08-27~09-23, 21거래일)

스크리너 게이팅을 적용한 기준 설정의 성적: 51건, 승률 18%, 비용 차감 후 기대값 −0.80%, PF 0.34.

| 기법 | 거래 | 승률 | 기대값 |
|---|---:|---:|---:|
| theme_leader | 7 | 57% | +0.91% |
| breakout | 14 | 14% | −0.90% |
| ma_pullback | 28 | 11% | −1.13% |

- 청산 사유: ATR 손절 71%(중간 보유 21분), 장마감 청산 18%, 모멘텀 소멸 10%.
- 70/30 워크포워드에서 점심 돌파 제외, volume_surge 1.5, lookback 30이 기준보다 나았지만, 검증 표본이 9건뿐이라 채택할 근거가 없다(가설로만 남김).
- 한계: 도구가 분할매도(scale_out)를 재현하지 못해 기준 성적이 실제보다 나쁘게 나온다. 한 달은 하나의 장세일 뿐이다. 야후 데이터는 15:00에서 끝난다.

## 4. 결정 사항

- AI 뉴스 필터(news_guard)가 실패하면 매수를 통과시키는 현재 방식을 유지한다(사용자 결정).
- 오버나잇 보유는 조건부 허용(C)으로 한다: 비용 차감 후 수익 +2% 이상인 종목만 1일 보유, 손절선을 본전으로 올림, 금요일·휴장 전날은 넘기지 않음. 후속 PR에서 구현한다(`[9-1]`).

## 5. 남은 할 일

사용자가 나중에 하기로 한 후속 검증:
1. ATR 손절 배수 비교(4.5 / 6 / 8 / 끄기). 청산의 71%가 ATR 손절이라 가장 먼저 볼 가설이다.
2. research 도구에 분할매도(scale_out) 반영.
3. 몇 달치 데이터로 재검증: `python -m research.run --source toss --days 90`(토스 키가 있는 로컬). 세션별 설정(`sessions:` 구조, 5절)은 이 결과를 보고 결정한다.

진입 타이밍 개선 후보(미수정, 검증 뒤 적용):
- 돌파·깃발형·ORB에 추격 상한(직전 고가 대비 +x%, VWAP+k·ATR) 추가
- 거래량 평균 창에서 09:00 개장 봉 제외, 스프레드·호가 잔량 확인
- 후보를 모두 평가한 뒤 사는 구조라 생기는 신호→주문 지연 줄이기
- 기법 선택 점수(verdict.score)와 사이징 신호강도(signal_strength) 통일
- 세션별 설정 설계안: `config.yaml` 의 `sessions:` 항목(시간대별 enabled 기법과 파라미터 덮어쓰기)을 해외·코인에서 쓰는 playbook 덮어쓰기 방식으로 적용

알려진 한계와 작은 개선 후보:
- 해외·빗썸의 halt 상태가 메모리에만 있다(재시작하면 풀림). `bithumb_api.get_order()` 는 실제 계정으로 검증하지 않았다.
- technique_backtest의 청산 체결에는 슬리피지가 없다. close_squeeze의 VWAP 추세는 여전히 가져온 창 기준이다.
- 패널 폴링(setInterval)을 탭을 벗어나도 정리하지 않는다. 패널 onHide 생명주기가 필요하다.
- Groq 5xx 쿨다운, AI 시황 출력 길이 강제는 제안만 하고 구현하지 않았다.
- `daytrader/crypto_playbook.py` 는 테스트만 참조하는 사실상 죽은 코드다.
