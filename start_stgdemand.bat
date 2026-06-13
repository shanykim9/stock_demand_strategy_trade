@echo off
setlocal

set "BASE_DIR=%~dp0"
set "PY_LAUNCHER=C:\WINDOWS\py.exe"
set "APP_FILE=%BASE_DIR%stgdemand.py"
set "LOG_DIR=%BASE_DIR%data\logs"
set "LOG_FILE=%LOG_DIR%\stgdemand_stdout.log"
set "ERR_FILE=%LOG_DIR%\stgdemand_stderr.log"
set "TRACE_FILE=%LOG_DIR%\startup_trace.log"
set "MAX_LOG_MB=10"
set /a MAX_LOG_BYTES=%MAX_LOG_MB%*1024*1024

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
echo [%date% %time%] startup invoked. APP_FILE="%APP_FILE%" >> "%TRACE_FILE%"

rem 로그 용량 자동 정리: 최대 용량 초과 시 .1로 롤링(1세대 보관)
call :rotate_log "%LOG_FILE%"
call :rotate_log "%ERR_FILE%"

rem 중복 실행 방지: 이미 7791 포트가 LISTENING이면 종료
for /f %%L in ('netstat -ano ^| findstr /R /C:":7791 .*LISTENING"') do (
	echo [%date% %time%] port 7791 already listening. skip start. >> "%TRACE_FILE%"
	exit /b 0
)

cd /d "%BASE_DIR%"
if exist "%PY_LAUNCHER%" (
	echo [%date% %time%] launch with py launcher: "%PY_LAUNCHER%" -3 -B >> "%TRACE_FILE%"
	"%PY_LAUNCHER%" -3 -B "%APP_FILE%" 1>>"%LOG_FILE%" 2>>"%ERR_FILE%"
) else (
	echo [%date% %time%] py launcher not found. fallback to python -B >> "%TRACE_FILE%"
	python -B "%APP_FILE%" 1>>"%LOG_FILE%" 2>>"%ERR_FILE%"
)
echo [%date% %time%] process exited with code %ERRORLEVEL%. >> "%TRACE_FILE%"

endlocal
exit /b 0

:rotate_log
set "TARGET=%~1"
if not exist "%TARGET%" goto :eof
for %%F in ("%TARGET%") do set "FILE_SIZE=%%~zF"
if %FILE_SIZE% LEQ %MAX_LOG_BYTES% goto :eof
if exist "%TARGET%.1" del /f /q "%TARGET%.1"
move /y "%TARGET%" "%TARGET%.1" >nul
type nul > "%TARGET%"
goto :eof
