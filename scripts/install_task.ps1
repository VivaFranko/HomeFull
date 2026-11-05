param([Parameter(Mandatory=$true)][string]$PythonExe)
$ErrorActionPreference = 'Stop'
$TaskName = 'HomeFull'
$Runner = Join-Path $PSScriptRoot 'ensure_running.ps1'
$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask -and $ExistingTask.Actions.Arguments -notlike "*$Runner*") {
    throw 'An unrelated scheduled task already uses this name.'
}
$TaskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$ActionArgs = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -PythonExe "{1}"' -f $Runner,$PythonExe
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $ActionArgs
$Triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $TaskUser),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$Principal = New-ScheduledTaskPrincipal -UserId $TaskUser -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$null = Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers `
    -Principal $Principal -Settings $Settings -Description 'HomeFull rental search assistant watchdog.' -Force
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
