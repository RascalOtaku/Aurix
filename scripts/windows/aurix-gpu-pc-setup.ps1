# aurix-gpu-pc-setup.ps1 - keep the Windows GPU PC on, and its Ollama models where Aurix can find them.
#
# Two symptoms, and what usually causes them:
#   "it keeps shutting off"   Windows sleeps/hibernates on its power plan, or Windows Update restarts it; after a restart
#                             nothing starts Ollama until someone logs in, so Aurix sees the PC as off.
#   "it loses all its models" Ollama keeps models per folder. If it is started once by the tray app and once from a shell,
#                             a service or another user, with a different OLLAMA_MODELS (or none), it sees an EMPTY list and
#                             the models look gone, though they are still on disk. (Separately, Ollama unloads a model from
#                             the GPU after 5 idle minutes; the next request just reloads it - slow, not lost.)
#
# Run in an ADMIN PowerShell:
#   powershell -ExecutionPolicy Bypass -File aurix-gpu-pc-setup.ps1                 # diagnose only: changes nothing
#   powershell -ExecutionPolicy Bypass -File aurix-gpu-pc-setup.ps1 -Apply          # fix it
#   ... -Apply -RequiredModels "qwen3.5:9b,llama3.2:3b" -KeepAlive 30m
#
# -Apply does:
#   1. Power: never sleep / hibernate on AC power; wake-on-LAN stays on (Aurix wakes the PC with it).
#   2. Windows Update: restarts only between 02:00 and 08:00 (active hours 08-02, the 18 h maximum).
#   3. Ollama: pins ONE models folder machine-wide (the one that already holds the most models), listens for Aurix on the
#      network, and keeps a model loaded for -KeepAlive after use.
#   4. A scheduled task "Aurix Ollama guard" (SYSTEM, at startup and every 10 min): starts Ollama if it is not answering -
#      before anyone logs in - and re-pulls a -RequiredModels entry only if it is really missing. Log:
#      C:\ProgramData\Aurix\ollama-guard.log
# Undo: -Undo removes the task and the pinned variables and puts the power plan back to Windows' defaults.
param(
    [switch]$Apply,
    [switch]$Undo,
    [string]$RequiredModels = "",      # e.g. "qwen3.5:9b,llama3.2:3b" - pulled again only if missing
    [string]$KeepAlive = "30m",        # how long a model stays in VRAM after use; "-1" = forever (blocks VRAM for games)
    [int]$Days = 14                    # how far back the diagnosis reads the event log
)
$ErrorActionPreference = "Stop"
$TaskName = "Aurix Ollama guard"
$DataDir = Join-Path $env:ProgramData "Aurix"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-OllamaExe {
    $candidates = @()
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    $candidates += Get-ChildItem "C:\Users\*\AppData\Local\Programs\Ollama\ollama.exe" -ErrorAction SilentlyContinue | ForEach-Object FullName
    $candidates += "C:\Program Files\Ollama\ollama.exe"
    return $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

function Get-ModelDirs {
    # Every place Ollama may have put models, with how many models each holds.
    $dirs = @()
    foreach ($scope in "Process", "User", "Machine") {
        $v = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS", $scope)
        if ($v) { $dirs += $v }
    }
    $dirs += Get-ChildItem "C:\Users\*\.ollama\models" -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName
    $dirs += Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root ".ollama\models" }
    $dirs += Join-Path $env:ProgramData "Ollama\models"
    $dirs | Where-Object { $_ -and (Test-Path $_) } | Sort-Object -Unique | ForEach-Object {
        $manifests = Join-Path $_ "manifests"
        $n = 0
        if (Test-Path $manifests) { $n = @(Get-ChildItem $manifests -Recurse -File -ErrorAction SilentlyContinue).Count }
        $gb = 0
        $blobs = Join-Path $_ "blobs"
        if (Test-Path $blobs) { $gb = [math]::Round(((Get-ChildItem $blobs -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1GB, 1) }
        [pscustomobject]@{ Path = $_; Models = $n; GB = $gb }
    } | Sort-Object Models -Descending
}

function Show-Diagnosis {
    Write-Host "`n=== Why did it turn off? (last $Days days) ===" -ForegroundColor Cyan
    $since = (Get-Date).AddDays(-$Days)
    $events = Get-WinEvent -FilterHashtable @{ LogName = "System"; Id = 41, 42, 1074, 6008, 6006; StartTime = $since } -ErrorAction SilentlyContinue
    $kinds = @{ 41 = "power lost / crash (no clean shutdown)"; 6008 = "unexpected shutdown"; 1074 = "planned shutdown/restart";
                42 = "went to sleep"; 6006 = "clean shutdown" }
    if (-not $events) { Write-Host "No shutdown, sleep or crash events found." }
    else {
        $events | Group-Object Id | ForEach-Object { "{0,4} x {1}" -f $_.Count, $kinds[[int]$_.Name] } | Write-Host
        Write-Host "`nMost recent:"
        $events | Select-Object -First 12 | ForEach-Object {
            $why = $kinds[$_.Id]
            if ($_.Id -eq 1074) { $why += " by " + (($_.Properties[0].Value -split "\\")[-1]) + " - " + $_.Properties[2].Value }
            "  {0:yyyy-MM-dd HH:mm}  {1}" -f $_.TimeCreated, $why
        } | Write-Host
        if ($events | Where-Object Id -eq 41) {
            Write-Host "`nKernel-Power 41 means it lost power or crashed: check the PSU/power strip, GPU temperatures and drivers." -ForegroundColor Yellow
        }
    }
    Write-Host "`n=== Power plan (AC) ===" -ForegroundColor Cyan
    powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE | Select-String "Current AC" | ForEach-Object { "sleep after (s): " + ($_ -split ":")[-1].Trim() } | Write-Host
    powercfg /query SCHEME_CURRENT SUB_SLEEP HIBERNATEIDLE | Select-String "Current AC" | ForEach-Object { "hibernate after (s): " + ($_ -split ":")[-1].Trim() } | Write-Host

    Write-Host "`n=== Where are the Ollama models? ===" -ForegroundColor Cyan
    $dirs = @(Get-ModelDirs)
    if (-not $dirs) { Write-Host "No Ollama models folder found." }
    $dirs | Format-Table -AutoSize | Out-String | Write-Host
    foreach ($scope in "User", "Machine") {
        Write-Host ("OLLAMA_MODELS ({0}): {1}" -f $scope, ([Environment]::GetEnvironmentVariable("OLLAMA_MODELS", $scope)))
    }
    if (@($dirs | Where-Object Models -gt 0).Count -gt 1) {
        Write-Host "More than one folder holds models: Ollama shows whichever one it was started with. -Apply pins the biggest." -ForegroundColor Yellow
    }
    $procs = Get-Process -Name "ollama", "ollama app" -ErrorAction SilentlyContinue
    Write-Host ("Ollama processes: " + (($procs | ForEach-Object { $_.ProcessName }) -join ", "))
    try {
        $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 5
        Write-Host ("Ollama answers with {0} model(s): {1}" -f $tags.models.Count, (($tags.models | ForEach-Object name) -join ", "))
    } catch { Write-Host "Ollama is not answering on 127.0.0.1:11434." -ForegroundColor Yellow }
}

function Write-Guard([string]$ollamaExe, [string]$modelsDir) {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    $guard = Join-Path $DataDir "ollama-guard.ps1"
    $required = ($RequiredModels -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ }) -join "','"
    @"
# Written by aurix-gpu-pc-setup.ps1. Runs as SYSTEM at startup and every 10 minutes.
`$ErrorActionPreference = "Continue"
`$log = "$DataDir\ollama-guard.log"
function Log(`$m) { Add-Content -Path `$log -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), `$m) }
`$env:OLLAMA_MODELS = "$modelsDir"
`$env:OLLAMA_HOST = "0.0.0.0:11434"
`$env:OLLAMA_KEEP_ALIVE = "$KeepAlive"
function Up { try { Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 5 | Out-Null; return `$true } catch { return `$false } }
if (-not (Up)) {
    Log "Ollama not answering: starting it"
    Start-Process -FilePath "$ollamaExe" -ArgumentList "serve" -WindowStyle Hidden
    for (`$i = 0; `$i -lt 30 -and -not (Up); `$i++) { Start-Sleep -Seconds 2 }
    if (Up) { Log "Ollama is up" } else { Log "Ollama did not come up"; exit 1 }
}
`$have = @((Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 10).models | ForEach-Object name)
foreach (`$m in @('$required')) {
    if (-not `$m) { continue }
    if (`$have -notcontains `$m -and `$have -notcontains "`$(`$m):latest") {
        Log "required model `$m is missing: pulling it"
        & "$ollamaExe" pull `$m 2>&1 | Select-Object -Last 1 | ForEach-Object { Log "  `$_" }
    }
}
if ((Get-Item `$log -ErrorAction SilentlyContinue).Length -gt 1MB) { Move-Item `$log "`$log.old" -Force }
"@ | Set-Content -Path $guard -Encoding UTF8
    return $guard
}

if ($Undo) {
    if (-not (Test-Admin)) { throw "Run this in an ADMIN PowerShell." }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    foreach ($v in "OLLAMA_MODELS", "OLLAMA_HOST", "OLLAMA_KEEP_ALIVE") { [Environment]::SetEnvironmentVariable($v, $null, "Machine") }
    powercfg /change standby-timeout-ac 30; powercfg /change hibernate-timeout-ac 180
    Remove-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate" -Name SetActiveHours, ActiveHoursStart, ActiveHoursEnd -ErrorAction SilentlyContinue
    Write-Host "Undone: task removed, Ollama variables cleared, power plan back to Windows defaults."
    exit 0
}

Show-Diagnosis
if (-not $Apply) {
    Write-Host "`nDiagnosis only - nothing changed. Run again with -Apply to fix it." -ForegroundColor Green
    exit 0
}
if (-not (Test-Admin)) { throw "Run this in an ADMIN PowerShell (it changes power, Windows Update and machine variables)." }

Write-Host "`n=== Applying ===" -ForegroundColor Cyan
# 1. Power
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
foreach ($nic in Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object Status -eq "Up") {
    try { Set-NetAdapterPowerManagement -Name $nic.Name -WakeOnMagicPacket Enabled -ErrorAction Stop; Write-Host "wake-on-LAN on: $($nic.Name)" }
    catch { Write-Host "wake-on-LAN setting not available on $($nic.Name) (check the BIOS too)" }
}
Write-Host "Power: never sleeps or hibernates on AC."

# 2. Windows Update restarts only 02:00-08:00
$wu = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate"
New-Item -Path $wu -Force | Out-Null
Set-ItemProperty -Path $wu -Name SetActiveHours -Value 1 -Type DWord
Set-ItemProperty -Path $wu -Name ActiveHoursStart -Value 8 -Type DWord
Set-ItemProperty -Path $wu -Name ActiveHoursEnd -Value 2 -Type DWord
Write-Host "Windows Update: restarts only between 02:00 and 08:00 (and the guard brings Ollama back after one)."

# 3. One models folder for every way Ollama is started
$ollamaExe = Get-OllamaExe
if (-not $ollamaExe) { throw "Ollama is not installed (no ollama.exe found)." }
$best = @(Get-ModelDirs) | Select-Object -First 1
$modelsDir = if ($best -and $best.Models -gt 0) { $best.Path } else { Join-Path $env:ProgramData "Ollama\models" }
New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null
[Environment]::SetEnvironmentVariable("OLLAMA_MODELS", $modelsDir, "Machine")
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0:11434", "Machine")
[Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", $KeepAlive, "Machine")
foreach ($u in "OLLAMA_MODELS", "OLLAMA_HOST", "OLLAMA_KEEP_ALIVE") {
    if ([Environment]::GetEnvironmentVariable($u, "User")) {
        Write-Host "Removing a conflicting per-user $u (it would override the machine-wide one)."
        [Environment]::SetEnvironmentVariable($u, $null, "User")
    }
}
Write-Host "Ollama: models folder pinned to $modelsDir ($($best.Models) model(s)); keep-alive $KeepAlive."

# 4. The guard task (SYSTEM: runs before anyone logs in)
$guard = Write-Guard $ollamaExe $modelsDir
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$guard`""
$triggers = @((New-ScheduledTaskTrigger -AtStartup),
              (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Write-Host "Scheduled task '$TaskName' installed (startup + every 10 min). Log: $DataDir\ollama-guard.log"

# Restart Ollama so it picks up the pinned folder; the guard starts it headless.
Get-Process -Name "ollama app", "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 15
try {
    $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 10
    Write-Host ("Done. Ollama answers with {0} model(s): {1}" -f $tags.models.Count, (($tags.models | ForEach-Object name) -join ", ")) -ForegroundColor Green
} catch { Write-Host "Ollama is not answering yet; check $DataDir\ollama-guard.log in a minute." -ForegroundColor Yellow }
