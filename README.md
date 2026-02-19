# Kiwoom STG/Simul Demand

키움 REST API 기반 수급 분석 + 자동매수/자동매도(확장) 프로젝트입니다.  
메인 웹 UI는 `stgdemand.py`(포트 `7791`)이며, 분석 엔진은 `simuldemand.py`를 재사용합니다.

## 핵심 파일

- `stgdemand.py`: 스케줄(자동 07:00), MD 수동 실행, 실시간 진행 UI
- `simuldemand.py`: 수급 신호/시뮬레이션 계산 로직
- `demand.py`: 키움 토큰/TR 호출 및 종목명-코드 해석
- `bsdemand.py`: 자동매수/자동매도 UI 및 실행 훅(`stgdemand` 확장)
- `buysell.py`: 주문/호가/계좌 관련 실거래 로직
- `start_stgdemand.bat`: Windows 부팅 자동실행용 배치 파일
- `static/styles.css`: 웹 UI 스타일

## 동작 요약

1. MD 파일 수동 실행으로 테마 종목을 갱신
2. 스케줄 시각(예: 07:00)에 자동 분석 실행
3. 신호 종목을 자동매수 후보로 계획
4. 매수 시작시각(기본 09:00) 이후 자동매수 시도
5. 보유 후 자동매도 규칙 적용
   - 트레일링 익절: 최고가 대비 -10% 하락 트리거(저가 기준), 다음 거래일 시가 매도
   - 손절: 매수가 대비 -7% 하락 트리거(종가 기준), 다음 거래일 시가 매도
   - 동시 발생 시 트레일링 우선

## 환경 설정

`.env.example`를 복사해 `.env` 생성 후 값 입력:

```bash
cp .env.example .env
```

주요 변수:

- `APP_KEY`, `APP_SECRET`, `KIWOOM_ACCOUNT_NO`
- `KIWOOM_DRY_RUN` (`1` 권장, 실거래는 `0`)
- `KIWOOM_ENABLE_LIVE_TRADING` (`YES`일 때만 실거래 허용)
- `KIWOOM_DMST_STEX_TP=KRX` (KRX 기준)
- `KIWOOM_MARKET_OPEN=09:00`
- `KIWOOM_LIVE_ORDER_START=09:00`
- `KIWOOM_POLL_SEC=2.0`

## 실행 방법

### 수동 실행

```bash
python -B stgdemand.py
```

브라우저:

- `http://127.0.0.1:7791`

### Windows 부팅 자동실행

- 시작프로그램에서 `start_stgdemand_hidden.vbs` -> `start_stgdemand.bat` 호출
- `start_stgdemand.bat`는 중복 실행 방지(7791 포트 확인) + 로그 롤링 포함

## 데이터/로그 파일

실행 중 아래 파일이 생성/갱신됩니다.

- `data/stgdemand_schedule.json`
- `data/stgdemand_auto_history.json`
- `data/stgdemand_cache/*`
- `data/bsdemand_config.json`
- `data/bsdemand_buy_plan.json`
- `data/bsdemand_buy_log.jsonl`
- `data/logs/*.log`

## Git 업로드 가이드

- 커밋 권장: 코드 + 설정 템플릿(`.env.example`)
- 커밋 제외: 실제 `.env`, 로그, 캐시, 런타임 상태 파일
- `.gitignore`에 기본 제외 규칙 포함됨

## 주의

- `KIWOOM_DRY_RUN=0` + `KIWOOM_ENABLE_LIVE_TRADING=YES`는 실제 주문이 나갑니다.
- 운영 전 모의 환경/소액 검증을 먼저 권장합니다.
