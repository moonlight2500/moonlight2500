@echo off
setlocal
cd /d "%~dp0"

echo AutoDayTrading ���带 �����մϴ�.
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [����] python �� ã�� �� �����ϴ�. https://www.python.org ���� ��ġ�� �� �ٽ� �õ��ϼ���.
    goto :err
)

echo [1/5] PyInstaller ��ġ Ȯ�� ��...
python -m pip install --upgrade --quiet pyinstaller
if errorlevel 1 goto :err

echo [2/5] ���� ��Ű�� ��ġ ��...
python -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :err

echo �� �� �ִ� AutoDayTrading.exe �� ����ϴ�...
REM �� ������ �߰��� ���� - Ʈ����(�θ�)�� ����(�ڽ�) ���μ����� exe �̸���
REM   ������ ���尡 "�׼����� �źεǾ����ϴ�" �� �����Ѵ�.
REM   ���� ������ �Ź� �����ϸ� �۾� ��ȭ���ڿ��� �� �׵��� �̸� �����Ѵ�.
taskkill /F /IM AutoDayTrading.exe >nul 2>nul
timeout /t 1 /nobreak >nul

echo [3/5] ���� ����� ������ �����մϴ�(API Ű, ����, �Ÿ� ���)...
REM �ٷ� �Ʒ� PyInstaller �� --noconfirm �ɼ����� dist\AutoDayTrading ������
REM ��°�� ����� ���� �����(Ȯ�� ���� ����� ���� �ݵ�� �ʿ��� �ɼ��̴� -
REM ������ �̹� �ִ� ������ ������ ����� â�� ���� �ڵ� ���尡 �����).
REM �� �ȿ� �ִ� config.yaml/themes.yaml/secrets.yaml(API Ű)�� state\(�Ÿ�
REM ���)/logs\ �� PyInstaller �� �������� �ʴ� �����̶� �״�� ������� -
REM �̰� ������ ������� ������ API Ű�� �ٽ� �ְ� ���� ���Ǽ���/�ǰŷ�
REM ����� �ʱ�ȭ�Ǵ� ������ ���´�. ���� ���� �ӽ� ������ ���� �ΰ�,
REM ���尡 ���� �� �ٽ� ���ڸ��� �ǵ������´�.
set "BACKUP_DIR=%TEMP%\AutoDayTrading_build_backup"
REM ★★★ 실제로 겪은 사고(2026-09-24) - 예전엔 여기서 무조건 옛 백업부터 지우고 시작했다.
REM 그런데 직전 빌드가 중간에 실패해서 dist\AutoDayTrading 이 이미 비어 있는 채로 이 스크립트를
REM 다시 돌리면, "지울 옛 백업"(API 키·거래 기록이 살아있는 좋은 백업)은 멀쩡한데 "새로 담을
REM 내용"은 하나도 없어서, 결국 사용자의 secrets.yaml·state\ 가 통째로 사라졌다(빈 백업으로
REM 덮어씀). 이제는 지금 dist\AutoDayTrading 에 secrets.yaml 이나 state\ 가 실제로 있을 때만
REM 백업을 새로 만들고, 없으면(=직전 빌드가 이미 비워놓은 상태) 기존 백업을 절대 건드리지
REM 않는다 - 5단계 복원도 그 기존(직전의 좋았던) 백업을 그대로 쓴다.
REM ★ 괄호가 든 echo 를 if(...) 블록 안에 중첩시키면 cmd.exe 파서가 괄호 개수를 잘못 세어
REM 블록이 중간에 깨질 수 있다(실제로 겪음) - goto 로 분기해 블록 중첩을 아예 없앤다.
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
if exist "%BACKUP_DIR%" echo   [안내] 기존 상태가 없어 백업을 새로 만들지 않았습니다 - 직전 백업을 그대로 복원에 씁니다.

:backup_step_done

echo [4/5] ���� ���Դϴ�. �� �� �ɸ� �� �ֽ��ϴ�...
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

echo [5/5] ���� ������ �غ��մϴ�...
REM PyInstaller �� ��� ���� �⺻ config.yaml/themes.yaml ����, ������
REM ����� �� ����� ������ �״�� �ǵ������´� - �־��ٸ� �װ���
REM �׻� �̱��(����ڰ� ��ģ ���� �켱�̴�). ó�� ����� ����� ������
REM �ƹ��͵� �� �ϰ� PyInstaller �� ���� �⺻�� �״�� ���´�.
if exist "%BACKUP_DIR%" (
    if exist "%BACKUP_DIR%\config.yaml" copy /y "%BACKUP_DIR%\config.yaml" dist\AutoDayTrading\ >nul
    if exist "%BACKUP_DIR%\themes.yaml" copy /y "%BACKUP_DIR%\themes.yaml" dist\AutoDayTrading\ >nul
    if exist "%BACKUP_DIR%\secrets.yaml" copy /y "%BACKUP_DIR%\secrets.yaml" dist\AutoDayTrading\ >nul
    if exist "%BACKUP_DIR%\state" robocopy "%BACKUP_DIR%\state" dist\AutoDayTrading\state /E >nul
    if exist "%BACKUP_DIR%\logs" robocopy "%BACKUP_DIR%\logs" dist\AutoDayTrading\logs /E >nul
    rmdir /s /q "%BACKUP_DIR%"
)
if exist ��Ģ.txt copy /y ��Ģ.txt dist\AutoDayTrading\ >nul

echo.
echo ���尡 �������ϴ�: dist\AutoDayTrading\AutoDayTrading.exe
echo (���� ���� ũ��� �뷫 47MB �Դϴ�.)
echo.
echo ================================================================
echo  ����� �ݵ�� dist\AutoDayTrading ������ �ٸ� ��η� �ű�
echo  �ڿ� �ϼ��ϼ��� (�� �ڸ������� �ϸ� �ҽ� ������ �ٷ� �ݿ��Ǵ� ȥ����
echo  ����ϴ�).
echo.
echo   1^) AutoDayTrading.exe --check
echo      ��ġ �� ��Ű�� ���� Ʈ�� �� ���� ���� ���� ���ռ��� Ȯ���մϴ�.
echo   2^) ����� �� exe ���� config.yaml, themes.yaml, secrets.yaml, logs\, state\
echo      �� �״������ Ȯ���մϴ�(������ص� API Ű�� �Ÿ� ����� ������� �ʾƾ� �մϴ�).
echo   3^) ��ú��尡 �߰� /static/* �� �������� Ȯ���մϴ�.
echo   4^) ��� ��ī�̰� ���� �缺���� Ȯ���մϴ�.
echo   5^) Ʈ���� �������� �������� Ȯ���մϴ�(������ 11�� �۾�ǥ���ٿ���
echo      ^^ �Ʒ� ȭ��ǥ �������� ������ Ȯ���ϼ���).
echo   6^) �θ�(Ʈ����^)�� �ڽ�(����^) ���μ����� �̸��� ���� �޶����ִ���
echo      �۾�ǥ���ٿ��� Ȯ���մϴ�.
echo ================================================================
echo.
pause
goto :eof

:err
echo.
echo ���忡 �����߽��ϴ�. �� ���� �޽����� Ȯ���ϼ���.
pause
exit /b 1
