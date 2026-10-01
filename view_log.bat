@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  查看自动签到日志 / 任务状态
rem ============================================================

set "TASKNAME=WorkBuddy-DailyCheckin"
set "LOGFILE=%~dp0logs\checkin.log"

echo ============================================================
echo   自动签到 - 状态与日志
echo ============================================================
echo.

echo [任务状态]
schtasks /query /tn "%TASKNAME%" /fo list 2>nul | findstr /i "TaskName Status Next Last"
if %errorlevel% neq 0 (
    echo   (未找到任务,可能尚未注册。请先运行 install_task.bat)
)
echo.

echo [最近日志 - 末尾 25 行]
if exist "%LOGFILE%" (
    powershell -NoProfile -Command "Get-Content '%LOGFILE%' -Tail 25 -Encoding UTF8"
) else (
    echo   (暂无日志文件)
)

echo.
echo ============================================================
echo   其他操作:
echo     立即运行 : schtasks /run /tn "%TASKNAME%"
echo     删除任务 : schtasks /delete /tn "%TASKNAME%" /f
echo     完整日志 : notepad "%LOGFILE%"
echo ============================================================
echo.
pause
