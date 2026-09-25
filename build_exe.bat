@echo off
setlocal
chcp 949 >nul
cd /d "%~dp0"

echo AutoDayTrading 실행 파일을 만듭니다.
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [오류] python 을 찾을 수 없습니다. https://www.python.org 에서 설치한 뒤 다시 실행하세요.
    goto :err
)

echo [1/5] PyInstaller 설치/업데이트 중...
python -m pip install --upgrade --quiet pyinstaller
if errorlevel 1 goto :err

echo [2/5] 필요한 패키지 설치 중...
python -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :err

echo 실행 중인 AutoDayTrading.exe 를 종료합니다...
REM 실행 중인 exe 가 파일을 잡고 있으면 빌드가 "액세스 거부"로 실패한다.
REM   트레이와 서버를 함께 내린 뒤 잠깐 기다린다.
taskkill /F /IM AutoDayTrading.exe >nul 2>nul
timeout /t 1 /nobreak >nul

echo [3/5] 사용자 데이터 백업 중(API 키, 설정, 거래 기록)...
REM PyInstaller --noconfirm 은 dist\AutoDayTrading 폴더를 통째로 지우고 다시 만든다.
REM 그 안에 있던 config.yaml/themes.yaml/secrets.yaml(API 키)와 state\(거래 기록),
REM logs\ 가 빌드 때마다 사라지지 않도록 임시 폴더에 옮겨 두었다가 5단계에서 되돌린다.
set "BACKUP_DIR=%TEMP%\AutoDayTrading_build_backup"
REM 2026-09-24 실제 사고: 빌드가 중간에 실패해 dist 가 빈 상태로 다시 돌리면
REM 멀쩡한 옛 백업이 빈 백업으로 덮어써졌다. 그래서 secrets.yaml 이나 state\ 가
REM 실제로 있을 때만 백업을 새로 만들고, 없으면 기존 백업을 그대로 둔다.
REM (if 괄호 블록 안에서 변수를 바로 쓰면 cmd 가 미리 펼쳐 버려 goto 로 나눴다.)
set "HAVE_LIVE_DATA="
if exist dist\AutoDayTrading\secrets.yaml set "HAVE_LIVE_DATA=1"
if exist dist\AutoDayTrading\state set "HAVE_LIVE_DATA=1"
if not defined HAVE_LIVE_DATA goto :skip_backup_refresh

if exist "%BACKUP_DIR%" rmdir /s /q "%BACKUP_DIR%"
mkdir "%BACKUP_DIR%"
if exist dist\AutoDayTrading\config.yaml copy /y dist\AutoDayTrading\config.yaml "%BACKUP_DIR%\" >nul
if exist dist\AutoDayTrading\themes.yaml copy /y dist\AutoDayTrading\themes.yaml "%BACKUP_DIR%\" >nul
if exist dist\AutoDayTrading\secrets.yaml copy /y dist\AutoDayTrading\secrets.yaml "%BACKUP_DIR%\" >nul
if exist dist\AutoDayTrading\state robocopy dist\AutoDayTrading\state "%BACKUP_DIR%\state" /E >nul
if exist dist\AutoDayTrading\logs robocopy dist\AutoDayTrading\logs "%BACKUP_DIR%\logs" /E >nul
goto :backup_step_done

:skip_backup_refresh
if exist "%BACKUP_DIR%" echo   [안내] 배포 폴더에 데이터가 없어 이전 백업을 그대로 사용합니다.

:backup_step_done

echo [4/5] 빌드 중입니다. 몇 분 걸릴 수 있습니다...
python -m PyInstaller --noconfirm --clean --name AutoDayTrading --windowed ^
    --add-data "web;web" --add-data "config.yaml;." --add-data "themes.yaml;." --add-data "CHANGELOG.md;." ^
    --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.protocols.http.h11_impl ^
    --hidden-import uvicorn.protocols.websockets.auto ^
    --hidden-import uvicorn.lifespan.on --hidden-import truststore ^
    --hidden-import pystray._win32 --hidden-import pystray._base ^
    --collect-submodules pystray --collect-submodules PIL ^
    --collect-submodules daytrader tray.py
if errorlevel 1 goto :err

echo [5/5] 사용자 데이터 되돌리는 중...
REM PyInstaller 가 새로 넣은 기본 config.yaml/themes.yaml 대신 사용자가 쓰던 파일을
REM 되돌린다. 백업이 없으면(첫 빌드) 기본 파일이 그대로 남는다.
if exist "%BACKUP_DIR%" (
    if exist "%BACKUP_DIR%\config.yaml" copy /y "%BACKUP_DIR%\config.yaml" dist\AutoDayTrading\ >nul
    if exist "%BACKUP_DIR%\themes.yaml" copy /y "%BACKUP_DIR%\themes.yaml" dist\AutoDayTrading\ >nul
    if exist "%BACKUP_DIR%\secrets.yaml" copy /y "%BACKUP_DIR%\secrets.yaml" dist\AutoDayTrading\ >nul
    if exist "%BACKUP_DIR%\state" robocopy "%BACKUP_DIR%\state" dist\AutoDayTrading\state /E >nul
    if exist "%BACKUP_DIR%\logs" robocopy "%BACKUP_DIR%\logs" dist\AutoDayTrading\logs /E >nul
    rmdir /s /q "%BACKUP_DIR%"
)
if exist README.md copy /y README.md dist\AutoDayTrading\ >nul

echo.
echo 빌드 완료: dist\AutoDayTrading\AutoDayTrading.exe
echo (폴더 크기는 약 47MB 입니다.)
echo.
echo ================================================================
echo  확인 방법: dist\AutoDayTrading 폴더를 다른 경로로 복사한 뒤 확인하세요
echo  (같은 자리에서 확인하면 소스 폴더와 섞여 결과를 믿을 수 없습니다).
echo.
echo   1^) AutoDayTrading.exe --check
echo      파일, 패키지, 포트, 설정, 연결 상태가 모두 정상인지 확인합니다.
echo   2^) 실행 후 exe 옆에 config.yaml, themes.yaml, secrets.yaml, logs\, state\
echo      가 생기는지 확인합니다(임시 폴더가 아니라 exe 옆이어야 합니다).
echo   3^) 대시보드가 뜨고 /static/* 가 서빙되는지 확인합니다.
echo   4^) 트레이 아이콘이 뜨는지 확인합니다(윈도우 11 은
echo      ^^ 숨김 아이콘 안에 있을 수 있습니다).
echo   5^) 트레이(부모^)를 끄면 서버(자식^) 프로세스도 같이 내려가는지
echo      작업 관리자에서 확인합니다.
echo ================================================================
echo.
pause
goto :eof

:err
echo.
echo 빌드에 실패했습니다. 위의 오류 메시지를 확인하세요.
pause
exit /b 1
