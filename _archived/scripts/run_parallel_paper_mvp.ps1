param(
    [ValidateSet("Replay", "Live")]
    [string]$Mode = "Live",

    [ValidateSet("CTP", "CTPTEST")]
    [string]$Gateway = "CTP",

    [string]$WaitUntilLocalTime = ""
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

$exitCode = 0
$logName = "launcher_{0}_{1}_{2}.log" -f $Mode.ToLower(), $Gateway.ToLower(), (Get-Date -Format "yyyyMMdd_HHmmss")
$logPath = Join-Path $launcherLogDir $logName

Push-Location $parentDir
try {
    Start-Transcript -Path $logPath -Force | Out-Null
    $transcriptStarted = $true

    Write-Host "Launcher log: $logPath"
    Write-Host "Mode: $Mode | Gateway: $Gateway"

    if ($WaitUntilLocalTime) {
        $launchTime = Resolve-LaunchTime -TimeText $WaitUntilLocalTime
        Write-Host ("Delayed launch enabled; target local time is {0:yyyy-MM-dd HH:mm:ss}" -f $launchTime)
        Wait-UntilLaunchTime -TargetTime $launchTime
    }

    if ($Mode -eq "Replay") {
        & $pythonExe "$repoRoot\examples\paper_trading_mvp\run_replay_demo.py"
        $exitCode = $LASTEXITCODE
        return
    }

    if ($Gateway -eq "CTP") {
        $configPath = "$repoRoot\examples\paper_trading_mvp\live_ctp_paper_config.json"
    }
    else {
        $configPath = "$repoRoot\examples\paper_trading_mvp\live_ctptest_paper_config.json"
    }

    & $pythonExe -m paper_trading_mvp.live_runner $configPath
    $exitCode = $LASTEXITCODE
}
finally {
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
    Pop-Location
}

return $exitCode