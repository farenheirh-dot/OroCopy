$ErrorActionPreference = "Stop"
$observerFile = Join-Path $PSScriptRoot "screen_observer.py"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $venvPython) {
    & $venvPython $observerFile
    exit $LASTEXITCODE
}

Write-Host "No está preparado el entorno. Ejecuta .\setup.ps1 primero." -ForegroundColor Yellow
Read-Host "Pulsa Enter para cerrar"
