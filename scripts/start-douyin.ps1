$ErrorActionPreference = "Stop"

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

if (-not (Test-PortListening -Port 8000)) {
    Start-HiddenProcess `
        -FilePath "python" `
        -ArgumentList @("-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8000") `
        -WorkingDirectory $ProjectRoot `
        -StdOut $BackendLog `
        -StdErr $BackendErr
}

if (-not (Test-PortListening -Port 5175)) {
    Start-HiddenProcess `
        -FilePath "npm.cmd" `
        -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "5175", "--strictPort") `
        -WorkingDirectory $ProjectRoot `
        -StdOut $FrontendLog `
        -StdErr $FrontendErr
}

Start-Sleep -Seconds 2
Start-Process $Url | Out-Null

