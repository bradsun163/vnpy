param(
    [ValidateSet("CTP", "CTPTEST")]
    [string]$Gateway = "CTP",

    [string]$WaitUntilLocalTime = "",

    [int]$DiagnosticTimeout = 15,

    [switch]$AllDiagnosticCandidates
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$parentDir = Split-Path -Parent $repoRoot

$packageRoot = Join-Path $parentDir "vnpy-4.3.0-packages"
$pythonExe = "C:\Users\bradsun\AppData\Local\Programs\Python\Python312\python.exe"
$launcherLogDir = Join-Path $HOME ".vntrader\paper_mvp\launcher_logs"
$transcriptStarted = $false

function Resolve-LaunchTime {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TimeText
    )

    $match = [System.Text.RegularExpressions.Regex]::Match($TimeText, '^(?<hour>\d{1,2}):(?<minute>\d{2})(:(?<second>\d{2}))?$')
    if (-not $match.Success) {
        throw "Invalid -WaitUntilLocalTime value '$TimeText'. Use HH:mm or HH:mm:ss, for example 08:55 or 20:55:00."
    }

    $hour = [int]$match.Groups['hour'].Value
    $minute = [int]$match.Groups['minute'].Value
    $second = 0
    if ($match.Groups['second'].Success) {
        $second = [int]$match.Groups['second'].Value
    }

    if ($hour -gt 23 -or $minute -gt 59 -or $second -gt 59) {
        throw "Invalid -WaitUntilLocalTime value '$TimeText'. Use a valid 24-hour local time such as 08:55 or 20:55:00."
    }

    $target = Get-Date -Hour $hour -Minute $minute -Second $second
    if ($target -le (Get-Date)) {
        $target = $target.AddDays(1)
    }

    return $target
}

function Wait-UntilLaunchTime {
    param(
        [Parameter(Mandatory = $true)]
        [datetime]$TargetTime
    )

    while ($true) {
        $now = Get-Date
        if ($now -ge $TargetTime) {
            break
        }

        $remaining = $TargetTime - $now
        Write-Host ("Waiting until {0:yyyy-MM-dd HH:mm:ss} (remaining {1:hh\:mm\:ss})" -f $TargetTime, $remaining)

        $sleepSeconds = [Math]::Min([Math]::Max([int][Math]::Floor($remaining.TotalSeconds), 1), 60)
        Start-Sleep -Seconds $sleepSeconds
    }
}

function Get-LatestDiagnosticSummaryPath {
    $diagnosticRoot = Join-Path $HOME ".vntrader\diagnostics"
    if (-not (Test-Path $diagnosticRoot)) {
        throw "Diagnostic root not found: $diagnosticRoot"
    }

    $latestDir = Get-ChildItem -Path $diagnosticRoot -Directory |
        Where-Object { $_.Name -like 'official_ctp_diag_*' } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    if (-not $latestDir) {
        throw "No official diagnostic directory found under $diagnosticRoot"
    }

    $summaryPath = Join-Path $latestDir.FullName "summary.json"
    if (-not (Test-Path $summaryPath)) {
        throw "Diagnostic summary not found: $summaryPath"
    }

    return $summaryPath
}

if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found: $pythonExe"
}

if (-not (Test-Path $packageRoot)) {
    throw "Parallel package root not found: $packageRoot"
}

if (-not (Test-Path $launcherLogDir)) {
    New-Item -ItemType Directory -Path $launcherLogDir -Force | Out-Null
}

$env:PYTHONPATH = "$packageRoot;$repoRoot"

$diagnosticArgs = @(
    "$repoRoot\examples\veighna_trader\diagnose_ctp_connect.py",
    "--connect-file", "C:\Users\bradsun\.vntrader\connect_ctp.json",
    "--timeout", "$DiagnosticTimeout"
)
if ($AllDiagnosticCandidates) {
    $diagnosticArgs += "--all-candidates"
}

$launcherArgs = @("-Mode", "Live", "-Gateway", $Gateway)

$logName = "retest_{0}_{1}.log" -f $Gateway.ToLower(), (Get-Date -Format "yyyyMMdd_HHmmss")
$logPath = Join-Path $launcherLogDir $logName

Push-Location $parentDir
try {
    Start-Transcript -Path $logPath -Force | Out-Null
    $transcriptStarted = $true

    Write-Host "Retest log: $logPath"
    Write-Host "Gateway: $Gateway | DiagnosticTimeout: $DiagnosticTimeout"

    if ($WaitUntilLocalTime) {
        $launchTime = Resolve-LaunchTime -TimeText $WaitUntilLocalTime
        Write-Host ("Delayed retest enabled; target local time is {0:yyyy-MM-dd HH:mm:ss}" -f $launchTime)
        Wait-UntilLaunchTime -TargetTime $launchTime
    }

    Write-Host "Running official diagnostic first..."
    & $pythonExe $diagnosticArgs
    $diagnosticExitCode = $LASTEXITCODE

    $summaryPath = Get-LatestDiagnosticSummaryPath
    $summary = Get-Content -Path $summaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host "Diagnostic summary: $summaryPath"
    Write-Host ("Diagnostic status={0} contract_count={1}" -f $summary.status, $summary.contract_count)

    if ($diagnosticExitCode -ne 0 -or $summary.status -ne "connected" -or [int]$summary.contract_count -le 0) {
        Write-Host "Diagnostic did not reach usable contract metadata. Live runner will not start."
        return 1
    }

    Write-Host "Diagnostic passed. Starting live runner..."
    & "$repoRoot\scripts\run_parallel_paper_mvp.ps1" @launcherArgs
    return $LASTEXITCODE
}
finally {
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
    Pop-Location
}