# 显式等待：轮询一个 HTTP 地址直到就绪，**每一轮都说话**。
#
# 为什么要有这个脚本（2026-10-11 立）：
#   以前在会话里现场拼的等待长这样 ——
#       for($i=1; $i -le 15; $i++){ Start-Sleep -Seconds 2; try{ Invoke-WebRequest ... | Out-Null; break }catch{} }
#   三个毛病，全是"看起来很忙其实在干等"：
#     ① 每轮都静默：15 轮 × 2 秒 = 最多 30 秒**屏幕上一个字都不出**，看的人以为卡死了；
#     ② 分不清"还在等"和"等失败了"：循环走完既不报也不退非零，脚本照样往下跑，
#        后面每道门都撞一遍连接超时（AGENTS.md 三·五节实测：端口空着一次 30~60 秒）；
#     ③ 不报"卡在哪一步"：是端口没起、还是起了但 500，看不出来。
#   本脚本把这三点反过来：每轮打印序号/状态/耗时，成功报就绪，失败报最后一次的现场并 exit 1。
#
# 用法：
#   # 等价于原来那段（但每轮都说话，超时 30 秒）
#   .\tools\wait_for.ps1 -Url 'http://127.0.0.1:8791/api/state' -TimeoutSec 30 -IntervalSec 2
#
#   # 起服务的那一步（先清进程 → 拉起 → 等就绪 → 只把"就绪了"交给下一步）
#   .\tools\wait_for.ps1 -Url 'http://127.0.0.1:8768/api/state' -TimeoutSec 60
#   if($LASTEXITCODE -ne 0){ throw '后端没起来，后面的门别跑了' }
#
#   # 慢活丢后台作业时，让它把进度写进文件，你随时可以 tail
#   .\tools\wait_for.ps1 -Url '...' -LogFile "$env:TEMP\wait-8768.log"
#
# 约定（和 tools/ 里其它脚本一致）：就绪 exit 0，等不到 exit 1，参数写错 exit 2。
# 这个脚本**纯只读**：只发 GET，不起进程、不碰 data/、不写任何东西（-LogFile 除外，由你指定）。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [int]$TimeoutSec = 60,        # 最多等多久；超时**兜底**，正常靠提前就绪
    [double]$IntervalSec = 2,     # 每轮间隔
    [int]$PerAttemptTimeoutSec = 3,
    [int]$ExpectStatus = 200,     # 期望的状态码；命中即成功
    [ValidateSet('Body', 'None')][string]$Probe = 'Body',  # 只探通不通 → None
    [string]$Label = '',          # 打印用的名字（默认从 URL 取）
    [string]$LogFile = ''         # 可选：把每一轮追加写进这个文件（后台作业用）
)

$ErrorActionPreference = 'Stop'
if ($IntervalSec -le 0) { $IntervalSec = 0.5 }
if (-not $Label) {
    try { $Label = ([uri]$Url).Authority + ([uri]$Url).AbsolutePath } catch { $Label = $Url }
}

# 输出在 pwsh 里会被缓冲，加一个开关让每一轮立刻出现在屏幕上
function Say([string]$Text) {
    Write-Host $Text
    if ($LogFile) {
        # 用 UTF8 无 BOM 追加；日志文件只有一行一行的短句，坏了也不影响判断
        try { Add-Content -LiteralPath $LogFile -Value $Text -Encoding utf8 } catch { }
    }
}

$budget = [math]::Round($TimeoutSec, 1)
Say ("[等待] {0} —— 每 {1}s 探一次，最多等 {2}s（就绪即返回，不用等满）" -f $Label, $IntervalSec, $budget)

$started = Get-Date
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$attempt = 0
$last = '还没探过'
$lastStatus = $null

while ($true) {
    $attempt++
    $elapsed = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    $t0 = [System.Diagnostics.Stopwatch]::StartNew()
    $text = ''
    $ok = $false
    $detail = ''

    try {
        $resp = Invoke-WebRequest -Uri $Url -TimeoutSec $PerAttemptTimeoutSec -UseBasicParsing
        $code = [int]$resp.StatusCode
        if ($Probe -eq 'Body') { $text = [string]$resp.Content }
        if ($code -eq $ExpectStatus) {
            $ok = $true
            $detail = "HTTP $code" + $(if ($text) { " · $($text.Length) 字节" } else { '' })
        } else {
            $detail = "HTTP $code（期望 $ExpectStatus）"
        }
        $lastStatus = $code
    } catch {
        $detail = $_.Exception.Message
        $lastStatus = $null
    }
    $t0.Stop()

    if ($ok) {
        Say ("  [{0,3}] {1,4}s  OK   {2}（本轮 {3}s）" -f $attempt, $elapsed, $detail, [math]::Round($t0.Elapsed.TotalSeconds, 1))
        $total = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        Say ("[就绪] {0} 已可用，总耗时 {1}s（探了 {2} 轮）" -f $Label, $total, $attempt)
        Say ("       {0}" -f $Url)
        exit 0
    }

    # 没成就说清楚"现在到什么程度"：连不上是连接错，连上是状态码不对
    $kind = if ($lastStatus) { "未就绪(HTTP $lastStatus)" } else { '连不上' }
    Say ("  [{0,3}] {1,4}s  {2}  {3}（本轮 {4}s）" -f $attempt, $elapsed, $kind, $detail, [math]::Round($t0.Elapsed.TotalSeconds, 1))
    $last = $detail

    if ($sw.Elapsed.TotalSeconds -ge $TimeoutSec) { break }
    Start-Sleep -Seconds $IntervalSec
}

$total = [math]::Round($sw.Elapsed.TotalSeconds, 1)
Say ("[超时] {0} 等了 {1}s / {2} 轮仍未就绪 —— 最后一次：{3}" -f $Label, $total, $attempt, $last)
Say '       下一步别硬跑门（端口空着时每道浏览器门要白等 30~60 秒），先查：'
if ($lastStatus -eq 401 -or $lastStatus -eq 403) {
    Say ("         ① 端口是通的，是**被拒了**（HTTP {0}）—— 这个地址要登录/鉴权，别当「没起来」处理" -f $lastStatus)
    Say '         ② 确认你探的是对的地址（有的端口上跑的是需要凭据的服务，不是你要的程序）'
    Say '         ③ 要的凭据/令牌从哪来：查那个服务自己的设置页或它的 profile 配置'
} else {
    Say '         ① 服务进程起没起（pythonw 在不在、日志里有没有 ISOLATED_INSTANCE_UP）'
    Say '         ② 端口对不对（8768=在用版 / 8791=隔离副本；两个混了就是等不到）'
    Say '         ③ 起服务那一步的日志末尾（gate_instance.ps1 会自动打出来）'
}
Say '         ④ 直接打一次这个 URL 看原始报错：' + $Url
exit 1
