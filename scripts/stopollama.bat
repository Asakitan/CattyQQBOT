@echo off
REM 停掉 Ollama 进程 + 释放模型内存
taskkill /F /IM ollama.exe >NUL 2>&1
echo Ollama stopped.
pause
