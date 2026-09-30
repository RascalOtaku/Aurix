# aurix-ssh-setup.ps1 - one-time setup on the Windows PC: an SSH key, an `ssh aurix` alias (plus -Alias), and a
# Ctrl+Alt+A hotkey that opens an SSH session to the home server. -Port N also forwards localhost:N to the server.
#
#   powershell -ExecutionPolicy Bypass -File aurix-ssh-setup.ps1 -Server <tailscale-ip-or-name> -User <linux-user>
#   powershell -ExecutionPolicy Bypass -File aurix-ssh-setup.ps1 -Server 100.x.y.z -User me -Alias myserver -Port 7000
#
# It never touches the server. It prints (and copies) your PUBLIC key; that line has to be added to
# ~/.ssh/authorized_keys on the server once - see the note printed at the end.
param(
    [Parameter(Mandatory = $true)][string]$Server,   # no defaults: this repo is public, your address stays on your PC
    [Parameter(Mandatory = $true)][string]$User,
    [string]$Alias  = "",
    [int]   $Port   = 0,
    [string]$Hotkey = "CTRL+ALT+A"
)
$ErrorActionPreference = "Stop"
$sshDir = Join-Path $env:USERPROFILE ".ssh"
$key    = Join-Path $sshDir "id_ed25519"
New-Item -ItemType Directory -Force -Path $sshDir | Out-Null

if (-not (Test-Path $key)) {
    Write-Host "No SSH key yet - creating $key (press Enter twice for no passphrase, or type one)."
    ssh-keygen -t ed25519 -f $key -C "$env:USERNAME@$env:COMPUTERNAME"
}

# ~/.ssh/config entry, so plain `ssh aurix` works too and always uses this key.
$config = Join-Path $sshDir "config"
if (-not (Test-Path $config) -or -not (Select-String -Path $config -Pattern "^Host aurix" -Quiet)) {
    $fwd = if ($Port -gt 0) { "`n    LocalForward $Port localhost:$Port" } else { "" }
    Add-Content -Path $config -Encoding ascii -Value @"

Host aurix $Alias
    HostName $Server
    User $User
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes$fwd
    ServerAliveInterval 30
"@
    Write-Host "Added 'Host aurix' to $config"
}

# Desktop shortcut with a global hotkey (Windows only honours hotkeys on Desktop / Start Menu shortcuts).
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Aurix SSH.lnk"
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$s.Arguments  = "-NoExit -Command `"ssh aurix`""
$s.Hotkey     = $Hotkey
$s.Description = "SSH to the Aurix server ($User@$Server)"
$s.Save()
Write-Host "Shortcut: $lnk  (hotkey $Hotkey)"

$pub = Get-Content "$key.pub"
Set-Clipboard -Value $pub
Write-Host ""
Write-Host "Your public key (copied to the clipboard):"
Write-Host $pub
Write-Host ""
Write-Host "The server has to trust it ONCE. Pick whichever way in you still have:"
Write-Host "  * Tailscale SSH, if it is on for the server - one line from THIS PowerShell window:"
Write-Host "      Get-Content `"$key.pub`" | tailscale ssh $User@$Server `"mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys`""
Write-Host "  * Otherwise IN A SERVER SHELL (its keyboard, or a session that still works - not this PowerShell) run:"
Write-Host "      mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '<paste key>' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
Write-Host "Then test:  ssh aurix   (or press $Hotkey)"
