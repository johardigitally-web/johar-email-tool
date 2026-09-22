# Pull the newest mailer database snapshot from the server to this PC.
#
# The server takes its own daily snapshot, but it sits on the same disk as the
# database, so one dead disk loses both. This is the second physical copy. It
# only runs while this PC is on, so it supplements the server timer rather than
# replacing it.
#
# Tell it where to look first:
#   $env:MAILER_HOST       = "you@your-server"
#   $env:MAILER_SSH_KEY    = "$env:USERPROFILE\.ssh\id_ed25519"
#   $env:MAILER_BACKUP_DIR = "D:\backups\mailer"      (optional)
#
# Two gotchas here that cost time: the backup directory on the server is 700
# root, so a login shell cannot even expand a glob inside it, and the PowerShell
# pipeline corrupts binary output.

$ErrorActionPreference = "Stop"

function Need($name) {
    # Nothing falls back to a value that happens to work. A default host in a
    # script that fetches a file full of real people's email addresses is a
    # copy of that file arriving from, or landing on, the wrong machine.
    $value = [Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "$name is not set. See the header of this script."
    }
    return $value
}

$Remote    = Need "MAILER_HOST"
$Key       = Need "MAILER_SSH_KEY"
$RemoteDir = "/var/backups/mailer"
if ($env:MAILER_BACKUP_DIR) { $Dest = $env:MAILER_BACKUP_DIR }
else { $Dest = Join-Path $env:USERPROFILE "mailer-backups" }
$Keep = 30
$Log  = Join-Path $Dest "pull_backup.log"

function Note($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Output $line
    if (Test-Path $Dest) { Add-Content -Path $Log -Value $line -Encoding utf8 }
}

if (-not (Test-Path $Dest)) { New-Item -ItemType Directory -Path $Dest -Force | Out-Null }

try {
    $newest = & ssh -i $Key -o BatchMode=yes -o ConnectTimeout=20 $Remote `
        "sudo sh -c 'ls -1t $RemoteDir/mailer-*.sqlite3.gz 2>/dev/null | head -1'"
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($newest)) {
        throw "could not list remote snapshots (ssh exit $LASTEXITCODE)"
    }
    $newest = $newest.Trim()
    $name   = Split-Path $newest -Leaf
    $out    = Join-Path $Dest $name

    if (Test-Path $out) {
        Note "already have $name, nothing to do"
    } else {
        $tmp = "$out.part"
        # Through cmd, not the PowerShell pipeline: PS 5.1 decodes native output
        # as text, which silently corrupts a compressed file.
        & cmd /c "ssh -i ""$Key"" -o BatchMode=yes $Remote ""sudo cat '$newest'"" > ""$tmp"""
        if ($LASTEXITCODE -ne 0) { throw "transfer failed (ssh exit $LASTEXITCODE)" }

        $size = (Get-Item $tmp).Length
        if ($size -lt 2000) { throw "transferred file is only $size bytes, refusing to keep it" }

        # gzip files start 1f 8b. Anything else means we caught an error message
        # or a mangled stream rather than a snapshot.
        $magic = [byte[]](Get-Content -Path $tmp -Encoding Byte -TotalCount 2)
        if ($magic[0] -ne 0x1f -or $magic[1] -ne 0x8b) {
            throw ("transferred file is not gzip (header {0:x2}{1:x2})" -f $magic[0], $magic[1])
        }

        Move-Item -Path $tmp -Destination $out -Force
        Note ("pulled {0} ({1:N0} KB)" -f $name, ($size / 1KB))
    }

    Get-ChildItem -Path $Dest -Filter "mailer-*.sqlite3.gz" |
        Sort-Object Name -Descending | Select-Object -Skip $Keep |
        ForEach-Object { Remove-Item $_.FullName -Force; Note "removed old $($_.Name)" }
}
catch {
    Note "FAILED: $($_.Exception.Message)"
    exit 1
}
