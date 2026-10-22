# Bilgisayar Win+L ile kilitlenince Pi Panel gece ekranina gecer, kilit acilinca (ya da oturum acilinca) geri doner.
# Gorev Zamanlayicisi'nda \PiPanel\ altina iki gorev ekler. ssh'ta "pi" adli, sifresiz (anahtarli) bir Host tanimli olmali.
#   powershell -ExecutionPolicy Bypass -File windows\pc-kilit-gorevleri.ps1
$ssh = (Get-Command ssh.exe).Source
$conhost = "$env:WINDIR\System32\conhost.exe"
$user = "$env:USERDOMAIN\$env:USERNAME"
$class = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger

function New-PiTask($name, $cmd, $triggers) {
    # conhost --headless: konsol penceresi acilmasin
    $action = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$ssh`" -o BatchMode=yes -o ConnectTimeout=6 pi `"$cmd`""
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $name -TaskPath "\PiPanel\" -Trigger $triggers -Action $action `
        -Settings $settings -Principal $principal -Force | Out-Null
}

$lock = New-CimInstance -CimClass $class -ClientOnly -Property @{ StateChange = 7; UserId = $user; Enabled = $true }
$unlock = New-CimInstance -CimClass $class -ClientOnly -Property @{ StateChange = 8; UserId = $user; Enabled = $true }
New-PiTask "Pi Panel - kilitlenince gece" "touch /tmp/pc-locked" @($lock)
New-PiTask "Pi Panel - acilinca normal" "rm -f /tmp/pc-locked" @($unlock, (New-ScheduledTaskTrigger -AtLogOn -User $user))
Get-ScheduledTask -TaskPath "\PiPanel\" | Select-Object TaskName, State
