<#
.SYNOPSIS
    One-command Aurix push routine for Steammachine.
.DESCRIPTION
    Runs the verified push sequence: fresh clone, transfer-bundle fetch,
    rebase onto latest main, compile + content checks, push, remote
    verification, and cleanup. Stops loudly at the first failed stage.
    Manual trigger only — nothing here runs on a schedule.
.PARAMETER Branch
    Local branch holding the commits to push.
.PARAMETER OldBase
    Commit the branch was originally based on (rebase --onto origin/main <OldBase> <Branch>).
.PARAMETER SourceDir
    Working repo checkout containing the branch. Defaults to the current directory.
.PARAMETER PushTarget
    Remote ref to update. Default "main" (pushes Branch to main). Use the
    branch name to push a branch as-is.
.PARAMETER Markers
    Optional content markers; each must appear in the rebased diff (grep check).
.PARAMETER DryRun
    Print the planned commands without executing them.
.EXAMPLE
    .\aurix-push.ps1 -Branch my-fix -OldBase abc1234
.EXAMPLE
    .\aurix-push.ps1 -Branch scheduler-failure-logging -OldBase 03e167b -PushTarget scheduler-failure-logging
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Branch,
    [Parameter(Mandatory = $true)][string]$OldBase,
    [string]$SourceDir = (Get-Location).Path,
    [string]$PushTarget = "main",
    [string[]]$Markers = @(),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# Forward slashes: Git for Windows runs ssh through sh.exe, which eats backslashes.
$GitKey = "C:/Users/winte/.ssh/id_ed25519_aurix_claude"
$PythonExe = "C:\Users\winte\AppData\Local\Programs\Python\Python312\python.exe"
$RepoUrl = "git@github.com:RascalOtaku/Aurix.git"

function Invoke-Step {
    param([string]$Name, [scriptblock]$Action)
    Write-Host "==> $Name" -ForegroundColor Cyan
    if ($DryRun) { Write-Host "    [dry-run] skipped" -ForegroundColor DarkGray; return }
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "Stage failed: $Name (exit code $LASTEXITCODE)" }
}

function Assert-File([string]$Path, [string]$Label) {
    if (-not (Test-Path $Path)) { throw "Precondition failed: $Label not found at $Path" }
}

# ---- Preconditions (always run, even in DryRun) ----
Assert-File $GitKey "GitHub SSH key"
Assert-File (Join-Path $SourceDir ".git") "git repo at SourceDir"
Assert-File $PythonExe "Python 3.12"
$env:GIT_SSH_COMMAND = "ssh -i $GitKey -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tmpRoot = Join-Path ([IO.Path]::GetTempPath()) "aurix-push-$stamp"
$cloneDir = Join-Path $tmpRoot "repo"
$bundlePath = Join-Path $tmpRoot "commits.bundle"

try {
    if (-not $DryRun) { New-Item -ItemType Directory -Path $tmpRoot | Out-Null }

    Invoke-Step "Fresh clone of origin/main" {
        git clone --quiet $RepoUrl $cloneDir
    }

    Invoke-Step "Transfer bundle fetch ($Branch from $SourceDir)" {
        git -C $SourceDir bundle create $bundlePath "$OldBase..$Branch"
        if ($LASTEXITCODE -ne 0) { throw "git bundle create failed" }
        git -C $cloneDir fetch --quiet $bundlePath "$Branch`:$Branch"
    }

    Invoke-Step "Rebase onto latest origin/main" {
        git -C $cloneDir rebase --onto origin/main $OldBase $Branch
    }

    Invoke-Step "Compile checks (py_compile on changed .py files)" {
        $changed = git -C $cloneDir diff --name-only "origin/main..$Branch" -- '*.py'
        if ($LASTEXITCODE -ne 0) { throw "git diff --name-only failed" }
        foreach ($f in $changed) {
            if ([string]::IsNullOrWhiteSpace($f)) { continue }
            $full = Join-Path $cloneDir $f
            if (Test-Path $full) {
                & $PythonExe -m py_compile $full
                if ($LASTEXITCODE -ne 0) { throw "py_compile failed for $f" }
            }
        }
        if (-not $changed) { Write-Host "    (no .py files changed)" -ForegroundColor DarkGray }
    }

    Invoke-Step "Content marker checks" {
        if ($Markers.Count -eq 0) {
            Write-Host "    (no markers supplied, skipping)" -ForegroundColor DarkGray
            return
        }
        $diff = git -C $cloneDir diff "origin/main..$Branch"
        if ($LASTEXITCODE -ne 0) { throw "git diff failed" }
        foreach ($m in $Markers) {
            if ($diff -notmatch [regex]::Escape($m)) { throw "marker not found in diff: $m" }
            Write-Host "    found: $m" -ForegroundColor DarkGray
        }
    }

    Invoke-Step "Push $Branch to origin/$PushTarget" {
        git -C $cloneDir push origin "$Branch`:$PushTarget"
    }

    Invoke-Step "Remote verification (ls-remote tip matches)" {
        $localSha = (git -C $cloneDir rev-parse $Branch).Trim()
        if ($LASTEXITCODE -ne 0) { throw "git rev-parse failed" }
        $remoteLine = (git ls-remote origin $PushTarget).Trim()
        if ($LASTEXITCODE -ne 0) { throw "git ls-remote failed" }
        $remoteSha = ($remoteLine -split "\s+")[0]
        if ([string]::IsNullOrWhiteSpace($remoteSha)) { throw "empty ls-remote result for $PushTarget" }
        if ($remoteSha -ne $localSha) { throw "remote tip $remoteSha != pushed $localSha" }
        Write-Host "    origin/$PushTarget = $remoteSha" -ForegroundColor Green
    }

    Write-Host "PUSH-OK: $Branch -> origin/$PushTarget" -ForegroundColor Green
}
finally {
    if (-not $DryRun -and (Test-Path $tmpRoot)) {
        Write-Host "==> Cleanup temp dir + bundle" -ForegroundColor Cyan
        Remove-Item -Recurse -Force $tmpRoot
    }
}
