# Klasik Outlook'a (acik olmali) yeni mail gelince gonderen + konuyu Pi Panel'e yollar. Govde gonderilmez.
#   powershell -WindowStyle Hidden -File windows\mail_bildirim.ps1
$ol = $null
$seen = Get-Date
$info = @{ time = 0; from = ""; subject = "" }; $lastUnread = -1; $lastSent = Get-Date "2000-01-01"
while ($true) {
    try {
        if (-not $ol) { $ol = [Runtime.InteropServices.Marshal]::GetActiveObject("Outlook.Application") }
        $inbox = $ol.GetNamespace("MAPI").GetDefaultFolder(6)
        $items = $inbox.Items; $items.Sort("[ReceivedTime]", $true)
        $new = @()
        foreach ($m in $items) {
            if ($m.ReceivedTime -le $seen) { break }
            $new += $m
        }
        if ($new.Count) {
            $seen = $new[0].ReceivedTime
            $last = $new[0]
            $subj = $last.Subject; if ($new.Count -gt 1) { $subj = "$($new.Count) yeni mail · $subj" }
            $info = @{ time = [double]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000); from = $last.SenderName; subject = $subj }
        }
        $unread = $inbox.UnReadItemCount
        if ($new.Count -or $unread -ne $lastUnread -or ((Get-Date) - $lastSent).TotalSeconds -gt 60) {
            $info.unread = $unread; $info.ts = [double]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000)
            ($info | ConvertTo-Json -Compress) | ssh -o BatchMode=yes -o ConnectTimeout=6 pi "cat > ~/dashboard/cache/mail.json"
            $lastUnread = $unread; $lastSent = Get-Date
        }
    } catch { $ol = $null }
    Start-Sleep -Seconds 10
}
