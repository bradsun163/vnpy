$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$parentDir = Split-Path -Parent $repoRoot

$parallelRepo = Join-Path $parentDir "vnpy-4.3.0"
$packageRoot = Join-Path $parentDir "vnpy-4.3.0-packages"
$pythonExe = "C:\Users\bradsun\AppData\Local\Programs\Python\Python312\python.exe"

if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found: $pythonExe"
}

Push-Location $repoRoot
try {
    git fetch upstream --tags

    $tagExists = git rev-parse --verify refs/tags/4.3.0 2>$null
    if (-not $tagExists) {
        throw "Official tag 4.3.0 not found after fetch."
    }

    if (-not (Test-Path $parallelRepo)) {
        git worktree add $parallelRepo 4.3.0
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path $packageRoot)) {
    New-Item -ItemType Directory -Path $packageRoot | Out-Null
}

& $pythonExe -m pip install --upgrade --target $packageRoot `
    "vnpy==4.3.0" `
    "vnpy_ctastrategy==1.4.1" `
    "vnpy_paperaccount==1.0.6" `
    "vnpy_ctp==6.7.11.4" `
    "vnpy_riskmanager==2.0.0" `
    "vnpy_datamanager==1.2.0" `
    "vnpy_datarecorder==1.1.1" `
    "vnpy_spreadtrading==1.3.1"

Write-Host "Parallel repo: $parallelRepo"
Write-Host "Parallel packages: $packageRoot"
Write-Host "To run with the parallel package line:"
Write-Host "`$env:PYTHONPATH = '$packageRoot;$parallelRepo'"
Write-Host "Push-Location '$parentDir'"
Write-Host "& '$pythonExe' -c 'import vnpy; print(vnpy.__version__)'"
Write-Host "Pop-Location"
