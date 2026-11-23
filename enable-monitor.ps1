param([switch]$FlightDetailsOnly)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1')
Import-Module (Join-Path $PSHOME 'Modules/ScheduledTasks/ScheduledTasks.psd1')
$root = $PSScriptRoot
$taskName = 'Shanghai Holiday Fare Monitor'
$python = (Get-Command python.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$pythonWindowless = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) {
    throw 'Python was found, but pythonw.exe is missing.'
}

$data = Join-Path $root 'data'
New-Item -ItemType Directory -Path $data -Force | Out-Null
$staged = Join-Path $data ("serpapi-" + [Guid]::NewGuid().ToString("N") + ".tmp")
$keyFile = Join-Path $data 'serpapi.dpapi'
$monitor = Join-Path $root 'monitor.py'
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited

function Enable-FlightDetails {
    $name = 'Shanghai Holiday Fare Details'
    $server = Join-Path $root 'flight_server.py'
    $action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument ('-X utf8 "' + $server + '"') -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $name
}

if ($FlightDetailsOnly) {
    Enable-FlightDetails
    return
}

Write-Host 'This enables a Windows scheduled task at 09:00 each day under your signed-in account.'
Write-Host 'Get your free SerpApi key at https://serpapi.com/manage-api-key'
try {
    $key = Read-Host 'Paste your SerpApi API key here' -AsSecureString
    if ($key.Length -eq 0) {
        throw 'No API key was entered.'
    }
    $key | ConvertFrom-SecureString | Set-Content -LiteralPath $staged -Encoding Ascii
    $key.Dispose()

    & $python -X utf8 $monitor check-key --key-file $staged
    if ($LASTEXITCODE -ne 0) {
        throw 'The API key could not be verified.'
    }

    Move-Item -LiteralPath $staged -Destination $keyFile -Force
    & $python -X utf8 $monitor collect
    $initialResult = $LASTEXITCODE

    $action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument ('-X utf8 "' + $monitor + '" collect') -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Daily -At (Get-Date -Hour 9 -Minute 0 -Second 0)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Enable-FlightDetails
    Get-ScheduledTaskInfo -TaskName $taskName | Select-Object NextRunTime
    if ($initialResult -ne 0) {
        Write-Warning 'The schedule is enabled. Some initial searches need attention. See the report.'
    }
} finally {
    if (Test-Path -LiteralPath $staged) {
        Remove-Item -LiteralPath $staged
    }
}
