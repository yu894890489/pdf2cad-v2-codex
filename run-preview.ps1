param(
    [string]$Out = 'runs/new-drawing-decomposed',
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
    & $pythonExecutable -m tower_rebuild audit --out $Out
    if ($LASTEXITCODE -notin @(0, 2)) { throw "Dimension audit failed. See $Out/failure.json" }
    & $pythonExecutable -m tower_rebuild preview --out $Out
    if ($LASTEXITCODE -notin @(0, 2)) { throw "3D preview failed. See $Out/failure.json" }
    Write-Host "Drawing and 3D preview: http://127.0.0.1:$Port/topology/"
    Write-Host "Dimension audit: http://127.0.0.1:$Port/audit/"
    Write-Host "Model dimension audit: http://127.0.0.1:$Port/model-audit/"
    Write-Host "Final check: http://127.0.0.1:$Port/final/"
    if (-not $NoServe) {
        & $pythonExecutable -m tower_rebuild serve --out $Out --port $Port
        if ($LASTEXITCODE -ne 0) { throw "Local server failed (exit $LASTEXITCODE)." }
    }
    $global:LASTEXITCODE = 0
} finally {
    Pop-Location
}
