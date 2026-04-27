param(
  [string]$Model = "qwen2.5:1.5b",
  [string]$InstallDir = "tools\ollama",
  [string]$ModelsDir = "models\ollama",
  [string]$DownloadUrl = "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip",
  [switch]$SkipPull
)

$ErrorActionPreference = "Stop"
$root = Get-Location
$ollamaExe = Join-Path $InstallDir "ollama.exe"

function Assert-InCurrentFolder {
  param([string]$PathValue, [string]$Name)
  if ([System.IO.Path]::IsPathRooted($PathValue)) {
    throw "$Name must be a relative path inside current folder: $PathValue"
  }
  $resolvedRoot = (Resolve-Path $root).Path
  $fullPath = [System.IO.Path]::GetFullPath((Join-Path $root $PathValue))
  if (-not $fullPath.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "$Name must stay inside current folder: $PathValue"
  }
}

Assert-InCurrentFolder $InstallDir "InstallDir"
Assert-InCurrentFolder $ModelsDir "ModelsDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null

if (-not (Test-Path $ollamaExe)) {
  $archivePath = Join-Path $InstallDir "ollama-windows-amd64.zip"
  Write-Host "Downloading project-local Ollama: $DownloadUrl"
  Invoke-WebRequest -Uri $DownloadUrl -OutFile $archivePath
  Expand-Archive -Path $archivePath -DestinationPath $InstallDir -Force
  Remove-Item -LiteralPath $archivePath -Force
}

if (-not (Test-Path $ollamaExe)) {
  $found = Get-ChildItem -Path $InstallDir -Recurse -Filter "ollama.exe" -File | Select-Object -First 1
  if ($found) {
    $ollamaExe = $found.FullName
  }
}

if (-not (Test-Path $ollamaExe)) {
  throw "Ollama executable not found in $InstallDir"
}

$env:OLLAMA_MODELS = $ModelsDir
Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WorkingDirectory $root -WindowStyle Hidden | Out-Null
Start-Sleep -Seconds 5

if (-not $SkipPull) {
  & $ollamaExe pull $Model
}

& $ollamaExe list
