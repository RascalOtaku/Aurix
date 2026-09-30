# aurix-ssh-setup.ps1 - one-time setup on the Windows PC: an SSH key, and a Ctrl+Alt+A hotkey that opens an SSH
# session to the home server with port 7070 forwarded (http://localhost:7070 on the PC -> port 7070 on the server).
#
#   powershell -ExecutionPolicy Bypass -File aurix-ssh-setup.ps1
#   powershell -ExecutionPolicy Bypass -File aurix-ssh-setup.ps1 -Server 100.112.82.10 -User rascal_otaku -Port 7070 -Hotkey "CTRL+ALT+A"
#
# It never touches the server. It prints (and copies) your PUBLIC key; that line has to be added to
# ~/.ssh/authorized_keys on the server once - see the note printed at the end.
param(
    [string]$Server = "100.112.82.10",
    [string]$User   = "rascal_otaku",
    [int]   $Port   = 7070,
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
if (-not (Test-Path $config) -or -not (Select-String -Path $config -Pattern "^Host aurix$" -Quiet)) {
    Add-Content -Path $config -Encoding ascii -Value @"

Host aurix
    HostName $Server
    User $User
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    LocalForward $Port localhost:$Port
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
$s.Description = "SSH to the Aurix server ($User@$Server), forwarding port $Port"
$s.Save()
Write-Host "Shortcut: $lnk  (hotkey $Hotkey)"

$pub = Get-Content "$key.pub"
Set-Clipboard -Value $pub
Write-Host ""
Write-Host "Your public key (copied to the clipboard):"
Write-Host $pub
Write-Host ""
Write-Host "The server has to trust it ONCE. Pick whichever way in you still have:"
Write-Host "  * Tailscale SSH (no keys needed): on the server run  sudo tailscale set --ssh"
Write-Host "      then from here:  tailscale ssh $User@$Server"
Write-Host "  * At the server's own keyboard, or any session that still works, run:"
Write-Host "      mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '<paste key>' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
Write-Host "Then test:  ssh aurix   (or press $Hotkey)"
