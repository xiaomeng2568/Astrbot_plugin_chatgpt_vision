@echo off
chcp 65001 >nul
setlocal
pushd "%~dp0"
set "VISION_PY="
if exist "%~dp0..\..\..\..\venv\Scripts\python.exe" set "VISION_PY=%~dp0..\..\..\..\venv\Scripts\python.exe"
if not defined VISION_PY if exist "%~dp0..\..\..\.venv\Scripts\python.exe" set "VISION_PY=%~dp0..\..\..\.venv\Scripts\python.exe"
if not defined VISION_PY (
  echo 找不到AstrBot的Python。请在原CMD使用README中的self-test命令。
  pause
  popd
  exit /b 2
)
echo 使用已有授权，依次检查文本、校验图和模型选择。不安装依赖。
"%VISION_PY%" manage.py --proxy "" --timeout 180 --diagnostic self-test gpt-6.1-sol
set "VISION_EXIT=%ERRORLEVEL%"
echo.
if "%VISION_EXIT%"=="0" (echo 完整自检通过。现在可以用QQ的 /OAuth识图 测试。) else (echo 自检未通过。请反馈安全诊断区块，不要发送Token或凭据文件。)
pause
popd
exit /b %VISION_EXIT%
