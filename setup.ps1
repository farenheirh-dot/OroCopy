$ErrorActionPreference = "Stop"
$pythonExe = $null
$candidatePaths = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:ProgramFiles "Python312\python.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Python312\python.exe")
)
foreach ($candidate in $candidatePaths) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) {
        $pythonExe = $candidate
        break
    }
}

if (-not $pythonExe -and (Get-Command py -ErrorAction SilentlyContinue)) {
    try {
        & py -3.12 --version *> $null
        if ($LASTEXITCODE -eq 0) { $pythonExe = "py" }
    } catch { }
}

if (-not $pythonExe) {
    Write-Host "No encuentro Python 3.12. Instala Python 3.12 de 64 bits y vuelve a ejecutar este archivo." -ForegroundColor Yellow
    Read-Host "Pulsa Enter para cerrar"
    exit 1
}

$venvPath = Join-Path $PSScriptRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    if ($pythonExe -eq "py") {
        & py -3.12 -m venv $venvPath
    } else {
        & $pythonExe -m venv $venvPath
    }
    if ($LASTEXITCODE -ne 0) { throw "No se pudo crear el entorno virtual." }
} else {
    Write-Host "Entorno virtual existente; se reutilizará." -ForegroundColor Cyan
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "No se pudo actualizar pip." }
& $venvPython -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "No se pudieron instalar las dependencias de OroControl." }

Write-Host "OroCopy quedó preparado. Inicia con .\start.ps1; el lector TikTok requiere Tesseract OCR para Windows." -ForegroundColor Green
