# Build distributable artifacts for phone-video-sync.
#
# Usage:
#   .\scripts\build.ps1              # wheel + sdist in dist/
#   .\scripts\build.ps1 -Binary       # also build standalone phone-sync.exe
#   .\scripts\build.ps1 -BinaryOnly  # standalone exe only

param(
    [switch]$Binary,
    [switch]$BinaryOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Ensure-DevDeps {
    param([string[]]$Extras)
    $extraArg = ($Extras | ForEach-Object { ".[$_]" }) -join ""
    python -m pip install -e "$extraArg" | Out-Null
}

if (-not $BinaryOnly) {
    Write-Host "Building wheel and sdist..." -ForegroundColor Cyan
    Ensure-DevDeps @("build")
    python -m build
    Get-ChildItem dist | Format-Table Name, Length, LastWriteTime
}

if ($Binary -or $BinaryOnly) {
    Write-Host "Building standalone phone-sync.exe..." -ForegroundColor Cyan
    Ensure-DevDeps @("build")
    python -m pip install pyinstaller | Out-Null
    python -m PyInstaller --noconfirm --clean phone-sync.spec
    $exe = Join-Path $Root "dist\phone-sync.exe"
    if (Test-Path $exe) {
        Write-Host "Standalone binary: $exe" -ForegroundColor Green
    } else {
        throw "Expected binary not found at $exe"
    }
}

if (-not $Binary -and -not $BinaryOnly) {
    Write-Host ""
    Write-Host "Install the CLI from the wheel:" -ForegroundColor Yellow
    Write-Host "  python -m pip install dist\phone_video_sync-*.whl"
    Write-Host ""
    Write-Host "Or editable for development:" -ForegroundColor Yellow
    Write-Host "  python -m pip install -e `".[dev]`""
    Write-Host ""
    Write-Host "For a standalone .exe (no Python required on target machine):" -ForegroundColor Yellow
    Write-Host "  .\scripts\build.ps1 -Binary"
}
