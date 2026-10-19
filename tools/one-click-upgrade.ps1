# 一键升级：自己找程序目录 → 备份旧文件 → 只换程序文件 → 数据一个字不碰
param([string]$Target = "", [string[]]$Roots = @())
$ErrorActionPreference = 'Continue'
function Say($t) { Write-Host $t }

# ---------- 1. 找程序目录 ----------
$installs = @()
if ($Target -ne "" -and (Test-Path -LiteralPath (Join-Path $Target 'launcher.py'))) {
    $installs = @((Resolve-Path -LiteralPath $Target).Path)
} else {
    Say '  [1/3] 正在找你装的程序（桌面 / 下载 / 文档，往下找三层）...'
    $roots = @()
    if ($Roots.Count -gt 0) {
        foreach ($p in $Roots) { if (Test-Path -LiteralPath $p) { $roots += $p } }
    } else {
        foreach ($p in @("$env:USERPROFILE\Desktop", "$env:USERPROFILE\桌面", "$env:USERPROFILE\Downloads", "$env:USERPROFILE\Documents", "$env:USERPROFILE\OneDrive\Desktop", "$env:USERPROFILE\OneDrive\桌面")) {
            if (Test-Path -LiteralPath $p) { $roots += $p }
        }
    }
    $hits = @()
    foreach ($r in $roots) {
        $hits += Get-ChildItem -LiteralPath $r -Recurse -Depth 3 -Filter launcher.py -File -ErrorAction SilentlyContinue |
            Where-Object { Test-Path -LiteralPath (Join-Path $_.DirectoryName 'data\applications.db') } |
            ForEach-Object { $_.DirectoryName }
    }
    $installs = @($hits | Sort-Object -Unique)
}

if ($installs.Count -eq 0) {
    Say ''
    Say '  没有自动找到。把那个文件夹（里面有 launcher.py 和 data）拖进本窗口，再回车：'
    $p = Read-Host '  程序目录'
    $p = ($p -replace '"', '').Trim()
    if ($p -ne '' -and (Test-Path -LiteralPath (Join-Path $p 'launcher.py'))) { $installs = @($p) }
}
if ($installs.Count -eq 0) { Say '  [X] 没找到程序目录，什么都没改。'; exit 1 }
if ($installs.Count -gt 1) {
    Say ''
    Say '  找到不止一个，输入编号后回车：'
    for ($i = 0; $i -lt $installs.Count; $i++) { Say ("    {0}) {1}" -f ($i + 1), $installs[$i]) }
    $pick = Read-Host '  编号'
    $idx = 0
    if (-not [int]::TryParse($pick, [ref]$idx) -or $idx -lt 1 -or $idx -gt $installs.Count) { Say '  [X] 输入不对，什么都没改。'; exit 1 }
    $installs = @($installs[$idx - 1])
}
$prog = $installs[0]
Say ''
Say ("  程序目录：{0}" -f $prog)

# ---------- 2. 备份 + 替换（data/logs/config 永不触碰）----------
$skip = @('config.json', 'startup-log.txt', 'one-click-upgrade.ps1', 'data', 'logs', 'webview_profile', '个人画像.json')
$bak = Join-Path $prog ("_before_upgrade_" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$payload = $PSScriptRoot
Say '  [2/3] 备份旧文件，然后换上新版本 ...'
New-Item -ItemType Directory -Force -Path $bak | Out-Null
$n = 0
foreach ($item in Get-ChildItem -LiteralPath $payload -Force) {
    if ($skip -contains $item.Name) { continue }
    $dest = Join-Path $prog $item.Name
    $old = Join-Path $bak $item.Name
    if ($item.PSIsContainer) {
        if (Test-Path -LiteralPath $dest) {
            New-Item -ItemType Directory -Force -Path $old | Out-Null
            Copy-Item -LiteralPath $dest -Destination $old -Recurse -Force -ErrorAction SilentlyContinue
        }
        Copy-Item -LiteralPath $item.FullName -Destination $prog -Recurse -Force
    } else {
        if (Test-Path -LiteralPath $dest) { Copy-Item -LiteralPath $dest -Destination $old -Force }
        Copy-Item -LiteralPath $item.FullName -Destination $dest -Force
    }
    $n++
}

# ---------- 3. 检查 ----------
# 光看 "launcher.py 在不在" 是不够的：2026-09-24 那次升级包里少了整个 services\ 目录，
# 而 launcher.py 和 core\excel.py 都在，检查照样通过 —— 用户看到「升级完成」，
# 打开却是起不来。所以这里逐个点名新版本真正依赖的文件，缺一个就报失败。
Say '  [3/3] 检查 ...'
$need = @('launcher.py', 'server.py', 'store.py', 'core\excel.py', 'core\__init__.py',
          'api\__init__.py', 'services\__init__.py', 'services\apps.py', 'services\talks.py',
          'services\state.py', 'web\index.html')
$lost = @()
foreach ($f in $need) {
    if (-not (Test-Path -LiteralPath (Join-Path $prog $f))) { $lost += $f }
}
$ok = ($lost.Count -eq 0)
Say ''
if ($ok) {
    Say '  [OK] 升级完成 —— 只换了程序文件，数据一个字没动。'
    Say ("       换了 {0} 项；旧文件备份：{1}" -f $n, $bak)
    $db = Join-Path $prog 'data\applications.db'
    if (Test-Path -LiteralPath $db) { Say '       data\applications.db 保持原样（启动时会照常自动备份）' }
    Say ''
    $ans = Read-Host '  现在打开程序吗？(Y/N)'
    if ($ans -match '^[Yy]') {
        $starter = Join-Path $prog '启动.cmd'
        if (Test-Path -LiteralPath $starter) { Start-Process -FilePath $starter -WorkingDirectory $prog }
        else { Say '  没找到 启动.cmd，请手动双击它。' }
    }
    exit 0
}
Say '  [X] 检查没通过，这些文件没到位：'
Say ("      {0}" -f ($lost -join '、'))
Say '      旧文件都在备份目录里，把那个文件夹整个复制回来就能回到升级前：'
Say ("      {0}" -f $bak)
exit 1
