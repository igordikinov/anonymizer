$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Create .venv and install the dev dependencies first.' }
Push-Location $repoRoot
try {
    & $taskPython -m PyInstaller --noconfirm --clean --onefile --windowed --name SNP-Anonymizer --icon packaging/app-icon.ico --paths src --collect-data anonymizer packaging/launcher.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
} finally {
    Pop-Location
}
