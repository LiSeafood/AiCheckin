@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  WorkBuddy/Trae/Qoder 每日自动签到
rem  注册 Windows 任务计划程序(无需管理员权限)
rem  脚本路径自动取本文件所在目录,无需修改
rem ============================================================

rem --- Python 路径: 默认自动探测 PATH 里的 python;如有 conda 等独立
rem --- 安装,请把下一行的注释打开并改成完整路径
set "PYTHON="
rem set "PYTHON=C:\Path\To\Your\python.exe"
for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PYTHON set "PYTHON=%%i"
)
set "SCRIPT=%~dp0checkin.py"
set "WORKDIR=%~dp0"
set "LOGDIR=%WORKDIR%logs"
set "LOGFILE=%LOGDIR%\checkin.log"
set "TASKNAME=WorkBuddy-DailyCheckin"

rem --- 运行时间: 可用参数指定一个或多个(install_task.bat 09:30 或 install_task.bat 10:00 22:00),
rem --- 未给参数则交互询问,直接回车默认 10:00
set "TIMES=%*"
if not defined TIMES set /p "TIMES=每天几点运行?(HH:MM,多个用空格分隔,直接回车 = 10:00): "
if not defined TIMES set "TIMES=10:00"

echo ============================================================
echo   WorkBuddy 每日自动签到 - 任务注册
echo ============================================================
echo.
echo   Python : %PYTHON%
echo   脚本   : %SCRIPT%
echo   日志   : %LOGFILE%
echo   任务名 : %TASKNAME%
echo   时间   : 每天 %TIMES%
echo.

if not exist "%PYTHON%" (
    echo [错误] 找不到 Python: %PYTHON%
    echo        请把本文件顶部的 PYTHON 变量改成你的 Python 完整路径。
    echo.
    pause
    exit /b 1
)
if not exist "%SCRIPT%" (
    echo [错误] 找不到脚本: %SCRIPT%
    echo.
    pause
    exit /b 1
)
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

rem --- 用 PowerShell 注册(比 schtasks 更可靠, 且无需管理员) ---
echo [信息] 正在注册任务...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%PYTHON%' -Argument '\"%SCRIPT%\"' -WorkingDirectory '%WORKDIR%';" ^
  "$t = ('%TIMES%' -split '\s+') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ };" ^
  "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 60);" ^
  "Register-ScheduledTask -TaskName '%TASKNAME%' -Action $a -Trigger $t -Settings $s -Description 'AI IDE 每日自动签到' -Force | Out-Null;" ^
  "if ($?) { exit 0 } else { exit 1 }"

if %errorlevel% neq 0 (
    echo.
    echo [失败] 任务注册失败。
    echo        可尝试右键本文件 -^> 以管理员身份运行。
    echo.
    pause
    exit /b 1
)

echo [成功] 任务已注册!
echo.
echo   ------------------------------------------------------------
echo   每天 %RUNTIME% 自动运行,后台静默执行,无需打开任何应用。
echo   ------------------------------------------------------------
echo.
echo   常用操作:
echo     立即测试 : schtasks /run /tn "%TASKNAME%"
echo     查看任务 : schtasks /query /tn "%TASKNAME%" /v /fo list
echo     删除任务 : schtasks /delete /tn "%TASKNAME%" /f
echo     查看日志 : type "%LOGFILE%"
echo.
echo   提示: 任务运行时不弹窗口,结果请查看日志文件。
echo         预览最近一次结果:
echo         powershell -Command "Get-Content '%LOGFILE%' -Tail 12"
echo.

set /p TESTNOW="是否立即运行一次做测试? (Y/N): "
if /i "%TESTNOW%"=="Y" (
    echo.
    echo [信息] 正在触发任务...
    schtasks /run /tn "%TASKNAME%" >nul 2>&1
    echo [信息] 已触发。约 30 秒后可查看日志:
    echo        powershell -Command "Get-Content '%LOGFILE%' -Tail 20"
)

echo.
pause
