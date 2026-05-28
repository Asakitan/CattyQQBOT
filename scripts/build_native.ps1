# 编译 cpu_engine/native/*.pyx 到 inplace .pyd (Windows).
# 主人 2026-05-28 plan-cpu-alicebot-nlu-ai:
#   编译 normalize_native / cosine_topk / keyword_scan 三个热点.
#   需 MSVC Build Tools (Visual Studio Build Tools 2022).
#   失败时 Python 代码自动 fallback 到纯 Python 实现, 不阻塞业务.

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host ">> Compiling Cython extensions for cpu_engine..." -ForegroundColor Cyan
Write-Host "   repo root: $repoRoot" -ForegroundColor DarkGray

# 用项目的 venv (.venv) 优先
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $pythonExe = $venvPython
    Write-Host "   python   : $pythonExe (venv)" -ForegroundColor DarkGray
} else {
    $pythonExe = "python"
    Write-Host "   python   : $pythonExe (system)" -ForegroundColor DarkGray
}

& $pythonExe -c "import Cython; print(f'Cython {Cython.__version__} OK')" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "!! Cython not installed. Run: pip install -e .[cpu-engine]" -ForegroundColor Red
    exit 1
}

& $pythonExe setup.py build_ext --inplace
if ($LASTEXITCODE -ne 0) {
    Write-Host "!! build_ext failed. CPU engine will fallback to pure Python." -ForegroundColor Yellow
    exit $LASTEXITCODE
}

Write-Host ">> Build complete. .pyd files in src/catty_qq_ai/cpu_engine/native/" -ForegroundColor Green
Get-ChildItem -Path "src/catty_qq_ai/cpu_engine/native" -Filter "*.pyd" -ErrorAction SilentlyContinue |
    ForEach-Object { Write-Host "   - $($_.Name)" -ForegroundColor DarkGreen }
