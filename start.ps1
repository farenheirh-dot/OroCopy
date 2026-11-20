$ErrorActionPreference = "Stop"
$appFile = Join-Path $PSScriptRoot "app.py"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $venvPython) {
    & $venvPython $appFile
    exit $LASTEXITCODE
}

Write-Host "No está preparado el entorno de OroControl." -ForegroundColor Yellow
Write-Host "Instala Python 3.12 de 64 bits y ejecuta .\setup.ps1 una vez."
Read-Host "Pulsa Enter para cerrar"
