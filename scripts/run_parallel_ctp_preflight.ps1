param(
    [int]$DiagnosticTimeout = 15
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$parentDir = Split-Path -Parent $repoRoot

$packageRoot = Join-Path $parentDir "vnpy-4.3.0-packages"
$pythonExe = "C:\Users\bradsun\AppData\Local\Programs\Python\Python312\python.exe"
$connectFile = "C:\Users\bradsun\.vntrader\connect_ctp.json"
$diagnosticScript = Join-Path $repoRoot "examples\veighna_trader\diagnose_ctp_connect.py"
$retestScript = Join-Path $repoRoot "scripts\run_parallel_ctp_retest.ps1"
$launcherLogDir = Join-Path $HOME ".vntrader\paper_mvp\launcher_logs"
$transcriptStarted = $false

$script:failed = $false
$results = New-Object System.Collections.Generic.List[object]

function Add-CheckResult {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [ValidateSet("PASS", "WARN", "FAIL")]
        [string]$Status,

        [Parameter(Mandatory = $true)]
        [string]$Detail
    )

    if ($Status -eq "FAIL") {
        $script:failed = $true
    }

    $results.Add([PSCustomObject]@{
        Status = $Status
        Name = $Name
        Detail = $Detail
    }) | Out-Null
}

function Test-RequiredPath {
    param(
        [string]$Name,
        [string]$Path
    )

    if (Test-Path $Path) {
        Add-CheckResult -Name $Name -Status PASS -Detail $Path
        return $true
    }

    Add-CheckResult -Name $Name -Status FAIL -Detail "Missing: $Path"
    return $false
}

function Get-NextBeijingTarget {
    param(
        [int]$Hour,
        [int]$Minute
    )

    $beijingTz = [System.TimeZoneInfo]::FindSystemTimeZoneById("China Standard Time")
    $localTz = [System.TimeZoneInfo]::Local
    $beijingNow = [System.TimeZoneInfo]::ConvertTime((Get-Date), $beijingTz)

    $targetBeijing = [datetime]::new(
        $beijingNow.Year,
        $beijingNow.Month,
        $beijingNow.Day,
        $Hour,
        $Minute,
        0
    )
    if ($targetBeijing -le $beijingNow) {
        $targetBeijing = $targetBeijing.AddDays(1)
    }

    $targetUtc = [System.TimeZoneInfo]::ConvertTimeToUtc($targetBeijing, $beijingTz)
    $targetLocal = [System.TimeZoneInfo]::ConvertTimeFromUtc($targetUtc, $localTz)

    return [PSCustomObject]@{
        Beijing = $targetBeijing
        Local = $targetLocal
    }
}

function Get-ConfigValue {
    param(
        [Parameter(Mandatory = $true)]
        $Config,

        [Parameter(Mandatory = $true)]
        [string]$Key
    )

    $property = $Config.PSObject.Properties[$Key]
    if ($null -eq $property) {
        return $null
    }

    return $property.Value
}

if (-not (Test-Path $launcherLogDir)) {
    New-Item -ItemType Directory -Path $launcherLogDir -Force | Out-Null
}

$logName = "preflight_ctp_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss")
$logPath = Join-Path $launcherLogDir $logName

Push-Location $parentDir
try {
    Start-Transcript -Path $logPath -Force | Out-Null
    $transcriptStarted = $true

    Write-Host "Preflight log: $logPath"

    $localNow = Get-Date
    $beijingTz = [System.TimeZoneInfo]::FindSystemTimeZoneById("China Standard Time")
    $beijingNow = [System.TimeZoneInfo]::ConvertTime($localNow, $beijingTz)
    $morningTarget = Get-NextBeijingTarget -Hour 8 -Minute 59
    $afternoonTarget = Get-NextBeijingTarget -Hour 13 -Minute 29

    Add-CheckResult -Name "Current local time" -Status PASS -Detail ($localNow.ToString("yyyy-MM-dd HH:mm:ss zzz"))
    Add-CheckResult -Name "Current Beijing time" -Status PASS -Detail ($beijingNow.ToString("yyyy-MM-dd HH:mm:ss"))
    Add-CheckResult -Name "Next Beijing morning pre-open" -Status PASS -Detail (("Beijing {0:yyyy-MM-dd HH:mm:ss} -> local {1:yyyy-MM-dd HH:mm:ss zzz} -> -WaitUntilLocalTime {2}" -f $morningTarget.Beijing, $morningTarget.Local, $morningTarget.Local.ToString("HH:mm")))
    Add-CheckResult -Name "Next Beijing afternoon pre-open" -Status PASS -Detail (("Beijing {0:yyyy-MM-dd HH:mm:ss} -> local {1:yyyy-MM-dd HH:mm:ss zzz} -> -WaitUntilLocalTime {2}" -f $afternoonTarget.Beijing, $afternoonTarget.Local, $afternoonTarget.Local.ToString("HH:mm")))

    $pythonOk = Test-RequiredPath -Name "Python executable" -Path $pythonExe
    $packageOk = Test-RequiredPath -Name "Parallel package root" -Path $packageRoot
    $connectOk = Test-RequiredPath -Name "CTP connect config" -Path $connectFile
    $diagnosticOk = Test-RequiredPath -Name "Official diagnostic script" -Path $diagnosticScript
    $retestOk = Test-RequiredPath -Name "One-click retest script" -Path $retestScript

    if ($connectOk -and $pythonOk) {
        try {
            $configCheckCode = "import json,sys; data=json.load(open(sys.argv[1], encoding='utf-8')); required={'\u7528\u6237\u540d':'username','\u5bc6\u7801':'password','\u7ecf\u7eaa\u5546\u4ee3\u7801':'broker_id','\u4ea4\u6613\u670d\u52a1\u5668':'td_address','\u884c\u60c5\u670d\u52a1\u5668':'md_address','\u4ea7\u54c1\u540d\u79f0':'app_id','\u6388\u6743\u7f16\u7801':'auth_code','\u67dc\u53f0\u73af\u5883':'environment'}; missing=[alias for key,alias in required.items() if not str(data.get(key,'')).strip()]; candidate_rows=data.get('\u524d\u7f6e\u7ec4\u5408\u5019\u9009',[]) or []; summary={'missing':missing,'primary_td':str(data.get('\u4ea4\u6613\u670d\u52a1\u5668','')).strip(),'primary_md':str(data.get('\u884c\u60c5\u670d\u52a1\u5668','')).strip(),'primary_env':str(data.get('\u67dc\u53f0\u73af\u5883','')).strip(),'candidate_count':len(candidate_rows)}; summary['preferred_primary']=(summary['primary_td']=='182.254.243.31:30001' and summary['primary_md']=='182.254.243.31:30011' and summary['primary_env']=='\u5b9e\u76d8'); print(json.dumps(summary))"
            $configCheckJson = & $pythonExe -c $configCheckCode $connectFile
            if ($LASTEXITCODE -ne 0) {
                throw "Python config check failed with exit code $LASTEXITCODE"
            }
            $configCheck = ($configCheckJson -join "") | ConvertFrom-Json

            if ($configCheck.missing.Count) {
                Add-CheckResult -Name "CTP config required fields" -Status FAIL -Detail ("Missing or blank: " + ($configCheck.missing -join ", "))
            }
            else {
                Add-CheckResult -Name "CTP config required fields" -Status PASS -Detail "All required fields are populated"
            }

            Add-CheckResult -Name "CTP config front summary" -Status PASS -Detail (("Primary td/md={0}/{1}, env={2}, candidate_count={3}" -f $configCheck.primary_td, $configCheck.primary_md, $configCheck.primary_env, $configCheck.candidate_count))

            if ($configCheck.preferred_primary) {
                Add-CheckResult -Name "Preferred primary SimNow route" -Status PASS -Detail "Primary route is 30001/30011 in production mode"
            }
            else {
                Add-CheckResult -Name "Preferred primary SimNow route" -Status WARN -Detail "Primary route is not the currently preferred 30001/30011 production path"
            }
        }
        catch {
            Add-CheckResult -Name "CTP config parse" -Status FAIL -Detail $_.Exception.Message
        }
    }

    if ($pythonOk -and $packageOk -and $diagnosticOk) {
        $env:PYTHONPATH = "$packageRoot;$repoRoot"

        & $pythonExe -m py_compile $diagnosticScript
        if ($LASTEXITCODE -eq 0) {
            Add-CheckResult -Name "Diagnostic script syntax" -Status PASS -Detail "py_compile succeeded under launcher runtime"
        }
        else {
            Add-CheckResult -Name "Diagnostic script syntax" -Status FAIL -Detail "py_compile failed under launcher runtime"
        }

        & $pythonExe $diagnosticScript --help | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Add-CheckResult -Name "Diagnostic script startup" -Status PASS -Detail "--help succeeded with imports resolved under launcher runtime"
        }
        else {
            Add-CheckResult -Name "Diagnostic script startup" -Status FAIL -Detail "--help failed under launcher runtime"
        }
    }

    Write-Host ""
    Write-Host "Preflight results"
    Write-Host "-----------------"
    foreach ($result in $results) {
        Write-Host (("[{0}] {1}: {2}" -f $result.Status, $result.Name, $result.Detail))
    }

    Write-Host ""
    Write-Host "Recommended next commands"
    Write-Host "-------------------------"
    Write-Host ".\scripts\run_parallel_ctp_preflight.ps1"
    Write-Host ((".\scripts\run_parallel_ctp_retest.ps1 -Gateway CTP -DiagnosticTimeout {0}" -f $DiagnosticTimeout))
    Write-Host ((".\scripts\run_parallel_ctp_retest.ps1 -Gateway CTP -DiagnosticTimeout {0} -WaitUntilLocalTime {1}" -f $DiagnosticTimeout, $morningTarget.Local.ToString("HH:mm")))
    Write-Host ((".\scripts\run_parallel_ctp_retest.ps1 -Gateway CTP -DiagnosticTimeout {0} -WaitUntilLocalTime {1}" -f $DiagnosticTimeout, $afternoonTarget.Local.ToString("HH:mm")))

    if ($script:failed) {
        Write-Host ""
        Write-Host "Preflight failed. Fix the FAIL items before running the retest wrapper."
        return 1
    }

    Write-Host ""
    Write-Host "Preflight passed. Local checks are ready for tomorrow's retest."
    return 0
}
finally {
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
    Pop-Location
}