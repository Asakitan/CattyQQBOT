# A/B 缓存命中测试 harness v3 (openai-claude-95 §五) — 放远端 D:\CattyQQAI\_ab3.ps1
# 用法:
#   群聊:  .\_ab3.ps1 -gid 731010 -label gpt55_grp -provider gpt55 -coldRounds 2 -warmRounds 10
#   私聊:  .\_ab3.ps1 -gid 0 -uid 731011 -label gpt55_priv -provider gpt55
#   deepseek 回归对照: 省略 -provider (走生产默认端点)
# 读结果: python scripts/ab_hit_report.py --scope group:731010 --since "yyyy-MM-dd HH:mm"
param(
    [int]$gid = 0,
    [int]$uid = 0,
    [string]$label = "ab3",
    [string]$provider = "",
    [int]$coldRounds = 2,
    [int]$warmRounds = 10,
    [int]$sleepMs = 11000
)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Continue"

if ($uid -eq 0) { $uid = if ($gid -gt 0) { $gid + 900 } else { 731900 } }
$scope = if ($gid -gt 0) { "group:$gid" } else { "private:$uid" }
$total = $coldRounds + $warmRounds
$out = "ab_$label.txt"
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] scope=$scope provider='$provider' rounds=$total" | Tee-Object $out

# 中性文案池 (不触 nsfw/preg/imagegen), 轮转
$msgs = @(
    "今天群里有什么好玩的吗",
    "你最近在忙什么呀",
    "推荐一首歌听听呗",
    "周末打算干嘛",
    "刚看完一部电影感觉还不错",
    "今天天气怎么样",
    "有没有什么好吃的推荐",
    "最近学了个新东西",
    "晚上吃什么好纠结",
    "讲个冷笑话吧",
    "你觉得猫和狗哪个可爱",
    "最近睡眠不太好有建议吗"
)

for ($i = 0; $i -lt $total; $i++) {
    $text = $msgs[$i % $msgs.Count]
    if ($gid -gt 0) { $text = "[CQ:at,qq=0] $text" }
    $body = @{
        text = $text; user_id = $uid; live = $true; persist = $true
        with_tools = $true; include_messages = $false
    }
    if ($gid -gt 0) { $body.group_id = $gid }
    if ($provider -ne "") { $body.provider_override = $provider }
    $json = $body | ConvertTo-Json -Compress
    $phase = if ($i -lt $coldRounds) { "cold" } else { "warm" }
    try {
        $resp = Invoke-RestMethod -Uri "http://localhost:8080/dev/sim_chat" -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 180
        $reply = "$($resp.reply)"
        $line = "[r$($i+1)/$total $phase] ok=$($resp.ok) model='$($resp.override_model)' replyLen=$($reply.Length) head=$($reply.Substring(0, [Math]::Min(60, $reply.Length)))"
    } catch {
        $line = "[r$($i+1)/$total $phase] REQUEST FAILED: $($_.Exception.Message)"
    }
    $line | Tee-Object $out -Append
    if ($i -lt $total - 1) { Start-Sleep -Milliseconds $sleepMs }
}
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] done. 分析: python scripts/ab_hit_report.py --scope $scope" | Tee-Object $out -Append
