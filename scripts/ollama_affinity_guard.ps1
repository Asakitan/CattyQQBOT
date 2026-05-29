# Ollama CPU Affinity Guard
# 主人 2026-05-30: 限 ollama.exe 只用 4 个核 (CPU 0-3, bitmask 0x0F = 0b00001111 = 15)
# 后台每 5 秒扫一次, 新启动的 ollama 进程自动设 affinity. 不侵入 bot 代码.
# 用法: powershell -ExecutionPolicy Bypass -File ollama_affinity_guard.ps1 (建议任务计划开机自启)

$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 4 核硬限: bitmask 0x0F = CPU 0,1,2,3
$TARGET_AFFINITY = [IntPtr]15

# 标记已设过的 PID, 避免重复 setter 浪费 syscall
$seenPids = @{}

Write-Host "[ollama-affinity-guard] start, target=0x0F (4 cores: CPU 0-3)"

while ($true) {
    # 关注 ollama.exe / ollama_llama_server.exe (推理子进程)
    $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -match '^(ollama|ollama_llama_server)$'
    }

    foreach ($p in $procs) {
        $pid = $p.Id
        $current = [int64]$p.ProcessorAffinity
        if ($current -ne 15) {
            try {
                $p.ProcessorAffinity = $TARGET_AFFINITY
                Write-Host "[ollama-affinity-guard] $(Get-Date -Format 'HH:mm:ss') set $($p.ProcessName) pid=$pid affinity 0x$([Convert]::ToString($current,16)) -> 0x0F"
                $seenPids[$pid] = $true
            } catch {
                Write-Host "[ollama-affinity-guard] WARN pid=$pid set affinity failed: $_"
            }
        } elseif (-not $seenPids.ContainsKey($pid)) {
            # 第一次见且已 0x0F, 标记一下
            $seenPids[$pid] = $true
            Write-Host "[ollama-affinity-guard] $(Get-Date -Format 'HH:mm:ss') $($p.ProcessName) pid=$pid already 0x0F"
        }
    }

    # 清理已死的 pid 记录 (防内存涨)
    if ($seenPids.Count -gt 200) {
        $aliveIds = @($procs | ForEach-Object { $_.Id })
        $deadKeys = @($seenPids.Keys | Where-Object { $aliveIds -notcontains $_ })
        foreach ($k in $deadKeys) { $seenPids.Remove($k) }
    }

    Start-Sleep -Seconds 5
}
