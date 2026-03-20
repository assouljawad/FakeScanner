param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

Write-Host "==> Creating build virtual environment"
if (-not (Test-Path ".build-venv")) {
    py -3 -m venv .build-venv
}

$Python = Join-Path $RepoRoot ".build-venv\\Scripts\\python.exe"
$Pip = Join-Path $RepoRoot ".build-venv\\Scripts\\pip.exe"

Write-Host "==> Installing build dependencies"
& $Python -m pip install --upgrade pip
& $Pip install pyinstaller

Write-Host "==> Building FakeScanner.exe with PyInstaller"
& $Python -m PyInstaller --noconfirm packaging\\fake_scanner_ui.spec

if (-not $SkipInstaller) {
    $InnoSetup = "${env:ProgramFiles(x86)}\\Inno Setup 6\\ISCC.exe"
    if (-not (Test-Path $InnoSetup)) {
        throw "Inno Setup 6 was not found at '$InnoSetup'. Install it or rerun with -SkipInstaller."
    }

    Write-Host "==> Building installer with Inno Setup"
    & $InnoSetup packaging\\FakeScannerInstaller.iss
}

Write-Host "Build complete."
Write-Host "PyInstaller output: dist\\FakeScanner\\FakeScanner.exe"
if (-not $SkipInstaller) {
    Write-Host "Installer output: dist\\installer\\FakeScannerSetup.exe"
}
