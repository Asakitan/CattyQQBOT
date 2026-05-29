@echo off
REM S5.6j (主人 2026-05-29): 独立启动 Ollama, 不通过 bot 自动拉 (避免父子进程互相饿死).
REM 设 affinity = 0xF (核 0-3, 4 核) 留 2 核给 bot Python.
REM 必须从 D:\CattyQQAI 根目录运行 (相对路径模型目录).

setlocal

cd /d "%~dp0\.."
echo Working dir: %CD%

REM Kill 旧 ollama 进程 (避免端口冲突)
taskkill /F /IM ollama.exe >NUL 2>&1
timeout /t 2 /nobreak >NUL

REM 关键环境变量
set "OLLAMA_HOST=127.0.0.1:11434"
set "OLLAMA_MODELS=%CD%\models\ollama"
set "OLLAMA_NUM_PARALLEL=1"
set "OLLAMA_MAX_LOADED_MODELS=1"
set "OLLAMA_KEEP_ALIVE=30m"
set "OLLAMA_NUM_THREAD=4"

set "OLLAMA_EXE=%CD%\tools\ollama\ollama.exe"
if not exist "%OLLAMA_EXE%" (
  echo ERROR: %OLLAMA_EXE% not found
  pause
  exit /b 1
)

echo Starting Ollama serve with affinity=0xF (cores 0-3)...
REM /AFFINITY 是 cmd start 内置开关, 直接限 affinity 不用事后改
start "ollama serve" /B /AFFINITY 0xF /BELOWNORMAL "%OLLAMA_EXE%" serve

echo Waiting for Ollama to be ready...
timeout /t 5 /nobreak >NUL

REM 健康检查
curl -s http://127.0.0.1:11434/api/tags >NUL 2>&1
if errorlevel 1 (
  echo WARN: Ollama API not responding yet, give it more time
) else (
  echo Ollama is up.
  "%OLLAMA_EXE%" list
)

echo.
echo Ollama started. Use stopollama.bat to stop.
echo To load qwen2.5:3b in memory:
echo   curl http://127.0.0.1:11434/api/generate -d "{\"model\":\"qwen2.5:3b\",\"prompt\":\"hi\",\"keep_alive\":\"30m\"}"
echo.
pause
