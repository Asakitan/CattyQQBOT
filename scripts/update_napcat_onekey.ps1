$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $PSScriptRoot
$toolsDir = Join-Path $root "tools"
$zipPath = Join-Path $toolsDir "NapCat.Shell.Windows.OneKey.zip"
$extractDir = Join-Path $toolsDir "napcat-onekey"

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

$release = Invoke-RestMethod `
  -Uri "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest" `
  -Headers @{ "User-Agent" = "CattyQQAI" }

$asset = $release.assets | Where-Object { $_.name -eq "NapCat.Shell.Windows.OneKey.zip" } | Select-Object -First 1
if (-not $asset) {
  throw "NapCat.Shell.Windows.OneKey.zip was not found in release $($release.tag_name)."
}

Invoke-WebRequest `
  -Uri $asset.browser_download_url `
  -OutFile $zipPath `
  -Headers @{ "User-Agent" = "CattyQQAI" }

if (Test-Path -LiteralPath $extractDir) {
  Remove-Item -LiteralPath $extractDir -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force

Write-Host "Downloaded NapCat $($release.tag_name) to $extractDir"
