$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendLog = Join-Path $ProjectRoot "backend.log"
$BackendErr = Join-Path $ProjectRoot "backend.err.log"
$FrontendLog = Join-Path $ProjectRoot "frontend.log"
$FrontendErr = Join-Path $ProjectRoot "frontend.err.log"
$Url = "http://127.0.0.1:5175"

# ── Headless Chrome with login state (for downloader video source) ──
# downloader reuses this Chrome via CDP (http://127.0.0.1:9222) to fetch the
# /aweme/v1/play/?...&a_bogus=... direct link that only a logged-in session gets.
# The profile is maintained by douyin-user-search (valid sessionid/sid_guard etc).
$CdpPort = 9222
$CdpUrl = "http://127.0.0.1:$CdpPort"
$LoginProfile = Join-Path $ProjectRoot "external\douyin-user-search\douyin_profile"
$ChromeExe = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path -LiteralPath $ChromeExe)) {
    $ChromeExe = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
}

function Test-PortListening {
    param([int]$Port)
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-PortOwner {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $conn) { return $null }
    return $conn.OwningProcess
}

function Stop-PortOwner {
    param([int]$Port)
    $pidToStop = Get-PortOwner -Port $Port
    if (-not $pidToStop) { return $true }
    try {
        Stop-Process -Id $pidToStop -Force -ErrorAction Stop
    } catch {}
    Start-Sleep -Milliseconds 700
    if (Test-PortListening -Port $Port) {
        try {
            Start-Process `
                -FilePath "taskkill.exe" `
                -ArgumentList @("/PID", "$pidToStop", "/T", "/F") `
                -WindowStyle Hidden `
                -Wait | Out-Null
        } catch {}
    }

    $deadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-PortListening -Port $Port)) { break }
        Start-Sleep -Milliseconds 300
    }
    # 端口释放后额外等待 500ms，避免端口归还到 LISTENING 的竞态导致新进程绑定失败
    Start-Sleep -Milliseconds 500
    return -not (Test-PortListening -Port $Port)
}

function Start-HiddenProcess {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [string]$StdOut,
        [string]$StdErr
    )
    Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $StdOut `
        -RedirectStandardError $StdErr `
        -WindowStyle Hidden | Out-Null
}

function Wait-PortReady {
    param([int]$Port, [int]$TimeoutSeconds = 25, [scriptblock]$HealthCheck = $null)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-PortListening -Port $Port) {
            if (-not $HealthCheck) { return $true }
            try {
                & $HealthCheck | Out-Null
                return $true
            } catch {}
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Test-CdpAlive {
    param([string]$VersionUrl)
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri $VersionUrl -TimeoutSec 3
        return $null -ne $r -and $r.StatusCode -eq 200
    } catch { return $false }
}

function Clear-ProfileLock {
    param([string]$Profile)
    if (-not (Test-Path -LiteralPath $Profile)) { return }
    Get-ChildItem -LiteralPath $Profile -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'Singleton*' } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
}

function Ensure-CdpChrome {
    # Make sure a logged-in Chrome keeps listening on the CDP port so the
    # downloader (downloader.py connect_over_cdp) can fetch video direct links.
    # 已健康监听则直接复用，绝不重复拉起。
    if (Test-CdpAlive -VersionUrl $CdpUrl) {
        Write-Host "[ok] CDP Chrome already up at $CdpUrl"
        return $true
    }
    if (-not (Test-Path -LiteralPath $ChromeExe)) {
        Write-Host "[warn] Chrome not found at $ChromeExe. downloader will fall back to a bare launch."
        return $false
    }
    if (-not (Test-Path -LiteralPath $LoginProfile)) {
        Write-Host "[warn] Login profile not found: $LoginProfile. downloader will start without login state."
        return $false
    }
    Clear-ProfileLock -Profile $LoginProfile
    # 完全静默：用新 headless 引擎(--headless=new)，绝不创建可见窗口。
    # 旧方案用 --window-position=-32000 + WindowStyle Minimized 想藏窗口，
    # 在多屏/DPI 变化/重启后会窜到前台变成"无法点击的可见 Chrome"，是 user 反馈的 bug 源。
    # headless=new 下若抖音不给登录态直链，downloader 自己会 cdp_alive 判否回退裸启动，
    # 不会影响下载功能；故这里宁可静默、宁可回退，也不要可见窗口。
    Start-Process -FilePath $ChromeExe `
        -ArgumentList @(
            "--headless=new",
            "--remote-debugging-port=$CdpPort",
            "--user-data-dir=$LoginProfile",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-features=AutomationControlled",
            "--window-position=-32000,-32000",
            "--window-size=1280,800",
            "--restore-last-session=false",
            "https://www.douyin.com"
        ) `
        -WindowStyle Hidden | Out-Null
    $ready = Wait-PortReady -Port $CdpPort -TimeoutSeconds 15 -HealthCheck { Test-CdpAlive -VersionUrl $CdpUrl }
    if ($ready) {
        Write-Host "[ok] CDP Chrome started (headless=new) at $CdpUrl"
        return $true
    }
    Write-Host "[warn] CDP Chrome did not become ready in 15s. downloader will fall back. (no impact on services)"
    return $false
}

function Restart-PortOwner {
    param([int]$Port, [string]$Name, [bool]$Required = $true)
    if (Stop-PortOwner -Port $Port) { return }
    $owner = Get-PortOwner -Port $Port
    if (-not $Required) {
        return
    }
    [System.Windows.Forms.MessageBox]::Show(
        "无法停止占用 $Port 端口的旧 $Name 进程 PID=$owner。请手动结束该进程后重新启动。",
        "Douyin 启动失败",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
    exit 1
}

# ── First ensure the logged-in CDP Chrome is up so the downloader can reach it ──
# CDP Chrome 仅 downloader 抓登录态直链用。注意：双击 exe 不应让用户看到/点到这个 Chrome。
# Ensure-CdpChrome 已改为 headless=new 真静默；若起不来也只打日志、绝不阻断服务。
Ensure-CdpChrome | Out-Null

# ── 幂等启动：服务已在监听且健康则直接复用，不杀旧不重启、不清日志、不 exit 1 ──
# (旧版"强制重启跑最新代码"会反复杀服务、弹错框，是 user 反馈的启动不稳来源。)
# 后端 ──
$backendAlready = Wait-PortReady -Port 8000 -TimeoutSeconds 1 -HealthCheck {
    (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 2).StatusCode -eq 200
}
if ($backendAlready) {
    Write-Host "[ok] 后端已在 8000 监听，复用，不重启"
} else {
    # 端口被旧/僵死后端占着却没通过 health：才清掉重启
    Restart-PortOwner -Port 8000 -Name "后端" -Required $true
    foreach ($logFile in @($BackendLog, $BackendErr)) {
        if (Test-Path -LiteralPath $logFile) {
            try { Set-Content -LiteralPath $logFile -Value "" -Encoding UTF8 -ErrorAction Stop } catch {}
        }
    }
    Start-HiddenProcess `
        -FilePath "python" `
        -ArgumentList @("-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $ProjectRoot `
        -StdOut $BackendLog `
        -StdErr $BackendErr
    $backendReady = Wait-PortReady -Port 8000 -TimeoutSeconds 30 -HealthCheck {
        (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 2).StatusCode -eq 200
    }
    if (-not $backendReady) {
        [System.Windows.Forms.MessageBox]::Show(
            "后端服务未能在 30 秒内就绪。请查看 $BackendErr 了解详情。",
            "Douyin 启动失败",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
    }
}

# 前端 ──
$frontendAlready = Wait-PortReady -Port 5175 -TimeoutSeconds 1 -HealthCheck {
    (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:5175/" -TimeoutSec 2).StatusCode -eq 200
}
if ($frontendAlready) {
    Write-Host "[ok] 前端已在 5175 监听，复用，不重启"
} else {
    Restart-PortOwner -Port 5175 -Name "前端" -Required $false
    foreach ($logFile in @($FrontendLog, $FrontendErr)) {
        if (Test-Path -LiteralPath $logFile) {
            try { Set-Content -LiteralPath $logFile -Value "" -Encoding UTF8 -ErrorAction Stop } catch {}
        }
    }
    $ViteCmd = Join-Path $ProjectRoot "node_modules\.bin\vite.cmd"
    if (-not (Test-Path -LiteralPath $ViteCmd)) {
        Start-Process `
            -FilePath "npm.cmd" `
            -ArgumentList @("install") `
            -WorkingDirectory $ProjectRoot `
            -Wait `
            -WindowStyle Hidden
    }
    Start-HiddenProcess `
        -FilePath "npm.cmd" `
        -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "5175", "--strictPort") `
        -WorkingDirectory $ProjectRoot `
        -StdOut $FrontendLog `
        -StdErr $FrontendErr
    $frontendReady = Wait-PortReady -Port 5175 -TimeoutSeconds 40 -HealthCheck {
        (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:5175/" -TimeoutSec 2).StatusCode -eq 200
    }
    if (-not $frontendReady) {
        [System.Windows.Forms.MessageBox]::Show(
            "前端服务未能在 40 秒内就绪。请查看 $FrontendErr 了解详情。",
            "Douyin 启动失败",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
    }
}

Start-Sleep -Seconds 1
Start-Process $Url | Out-Null
