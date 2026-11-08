# scripts/restart.ps1 — 只重启后端 (kill 8000 → 拉起 uvicorn), 不开浏览器不动前端。
# 2026-11-08 用户定论: 改完代码重启服务绝不弹界面; start-douyin.ps1 的开浏览器逻辑已删,
# 但它带 CDP Chrome/前端复用等完整逻辑, 日常改后端代码用这个轻量脚本更快。
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendLog = Join-Path $ProjectRoot "backend.log"
$BackendErr = Join-Path $ProjectRoot "backend.err.log"

function Test-Port {
    param([int]$Port)
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

# 终止旧后端
$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction Stop
    $deadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $deadline -and (Test-Port 8000)) { Start-Sleep -Milliseconds 300 }
    Write-Host "[ok] 旧后端已终止"
} else {
    Write-Host "[info] 8000 无监听进程, 直接启动"
}

# 拉起新后端
foreach ($logFile in @($BackendLog, $BackendErr)) {
    if (Test-Path -LiteralPath $logFile) {
        try { Set-Content -LiteralPath $logFile -Value "" -Encoding UTF8 -ErrorAction Stop } catch {}
    }
}
Start-Process `
    -FilePath "python" `
    -ArgumentList @("-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8000") `
    -WorkingDirectory $ProjectRoot `
    -RedirectStandardOutput $BackendLog `
    -RedirectStandardError $BackendErr `
    -WindowStyle Hidden | Out-Null

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    try {
        if ((Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 2).StatusCode -eq 200) {
            Write-Host "[ok] 后端已就绪 (http://127.0.0.1:8000)"
            exit 0
        }
    } catch {}
    Start-Sleep -Milliseconds 500
}
Write-Host "[error] 后端 30s 未就绪, 查 $BackendErr"
exit 1
