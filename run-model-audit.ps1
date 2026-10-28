param(
    [string]$Out = 'runs/new-drawing-decomposed',
    [double]$ToleranceMm = 3.0,
    [int]$Port = 8768,
    [switch]$NoServe
)
$ErrorActionPreference = 'Stop'
$pythonExecutable = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw 'Create .venv and install the project first. See README.md.'
}
Push-Location $PSScriptRoot
try {
    & $pythonExecutable -m tower_rebuild model-audit --out $Out --tolerance-mm $ToleranceMm
    if ($LASTEXITCODE -notin @(0, 2)) { throw "Model dimension audit failed. See $Out/failure.json" }
    Write-Host "Model dimension audit: http://127.0.0.1:$Port/model-audit/"
    if (-not $NoServe) {
        & $pythonExecutable -m tower_rebuild serve --out $Out --port $Port
        if ($LASTEXITCODE -ne 0) { throw "Local server failed (exit $LASTEXITCODE)." }
    }
    $global:LASTEXITCODE = 0
} finally {
    Pop-Location
}
