$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$AppDir = Join-Path $ProjectRoot "app"
$CompatIcon = Join-Path $AppDir "tiktok-compatible.ico"
$OutputExe = Join-Path $AppDir "douyin.exe"
$ProgramCs = Join-Path $ProjectRoot "launcher\Program.cs"
$IconSource = if ($env:DOUYIN_LAUNCHER_ICON) { $env:DOUYIN_LAUNCHER_ICON } else { $CompatIcon }
$CscCommand = Get-Command csc.exe -ErrorAction SilentlyContinue
if (-not $CscCommand) {
    throw "csc.exe was not found. Install Visual Studio Build Tools or .NET Framework developer tools, then rerun this script."
}
$Csc = $CscCommand.Source

if (-not (Test-Path -LiteralPath $AppDir)) {
    New-Item -ItemType Directory -Path $AppDir | Out-Null
}

if (-not (Test-Path -LiteralPath $IconSource)) {
    throw "Launcher icon was not found. Set DOUYIN_LAUNCHER_ICON or add app\tiktok-compatible.ico."
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
