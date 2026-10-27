# Claude kullanimini 5 dk'da bir Pi Panel'e yollayan gorevi ekler (\PiPanel\ altina).
# Gereken: Python (pythonw.exe), Claude Code ile oturum acilmis olmasi, ssh'ta "pi" adli anahtarli Host.
#   powershell -ExecutionPolicy Bypass -File windows\claude-kullanim-gorevi.ps1
$pythonw = (Get-Command pythonw.exe).Source
$script = Join-Path $PSScriptRoot "claude_kullanim.py"
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`""
$every = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "Pi Panel - Claude kullanimi" -TaskPath "\PiPanel\" -Trigger @($every, $logon) `
    -Action $action -Settings $settings -Principal $principal -Force | Out-Null
Get-ScheduledTask -TaskPath "\PiPanel\" | Select-Object TaskName, State
