<#
    Registers a Windows Task Scheduler job that runs main.py once a day.

    Run this once, from an ordinary (non-admin) PowerShell prompt, from
    inside this project folder:

        .\register_daily_task.ps1

    Optional: pass -Time to change the run time (default 07:00).

        .\register_daily_task.ps1 -Time "06:30"

    To remove the task later:

        Unregister-ScheduledTask -TaskName "TTX Lit Reviewer" -Confirm:$false
#>

param(
    [string]$Time = "07:00"
)

$ProjectDir = $PSScriptRoot
$PythonExe = (Get-Command python).Source
$MainScript = Join-Path $ProjectDir "main.py"

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$MainScript`"" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd

Register-ScheduledTask -TaskName "TTX Lit Reviewer" `
    -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Daily scan for test-taker experience literature (TTX lit reviewer project)" `
    -Force

Write-Host "Registered daily task 'TTX Lit Reviewer' to run at $Time using $PythonExe"
Write-Host "Note: this only runs while your machine is on. Use Task Scheduler's GUI (taskschd.msc) if you want to adjust wake/battery settings."
