$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$AppDir = Join-Path $ProjectRoot "app"
$CompatIcon = Join-Path $AppDir "tiktok-compatible.ico"
$OutputExe = Join-Path $AppDir "douyin.exe"
$ProgramCs = Join-Path $ProjectRoot "launcher\Program.cs"
$IconSource = $CompatIcon

$CscCommand = Get-Command csc.exe -ErrorAction SilentlyContinue
$Csc = if ($CscCommand) { $CscCommand.Source } else { "" }
if (-not $Csc) {
    $FrameworkCsc = @(
        "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($FrameworkCsc) {
        $Csc = $FrameworkCsc
    }
}
if (-not $Csc) {
    throw "csc.exe was not found. Install Visual Studio Build Tools or .NET Framework developer tools, then rerun this script."
}

if (-not (Test-Path -LiteralPath $AppDir)) {
    New-Item -ItemType Directory -Path $AppDir | Out-Null
}

if (-not (Test-Path -LiteralPath $IconSource)) {
    throw "Launcher icon was not found. Add app\tiktok-compatible.ico."
}

@"
from PIL import Image
img = Image.open(r"$IconSource").convert("RGBA")
img.save(r"$CompatIcon", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
"@ | python -

& $Csc `
    /nologo `
    /target:winexe `
    /out:$OutputExe `
    /win32icon:$CompatIcon `
    /reference:System.Windows.Forms.dll `
    /reference:System.dll `
    $ProgramCs

Get-Item -LiteralPath $OutputExe
