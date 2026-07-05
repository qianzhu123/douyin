$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendLog = Join-Path $ProjectRoot "backend.log"
$BackendErr = Join-Path $ProjectRoot "backend.err.log"
$FrontendLog = Join-Path $ProjectRoot "frontend.log"
$FrontendErr = Join-Path $ProjectRoot "frontend.err.log"
$Url = "http://127.0.0.1:5175"

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
    if (-not $pidToStop) { return }
    try {
        Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue
    } catch {}
    Start-Sleep -Milliseconds 500
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

# ── 后端：强制重启（杀掉旧进程，确保跑的是项目最新代码） ──
Stop-PortOwner -Port 8000
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

# ── 前端：保留已运行的 vite（热更新），不在则拉起 ──
if (-not (Test-PortListening -Port 5175)) {
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
}

$frontendReady = Wait-PortReady -Port 5175 -TimeoutSeconds 25 -HealthCheck {
    (Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:5175/" -TimeoutSec 2).StatusCode -eq 200
}

Start-Sleep -Seconds 1
Start-Process $Url | Out-Null
