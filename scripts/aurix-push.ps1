<#
.SYNOPSIS
    One-command Aurix push routine for the Windows PC.
.DESCRIPTION
    Runs the verified push sequence: fresh clone, transfer-bundle fetch,
    rebase onto latest main, compile + content checks, the secret guard
    (same rules as the pre-commit hook and CI, with your private patterns),
    push, remote verification, and cleanup. Stops loudly at the first failed stage.
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
.PARAMETER GitKey
    SSH key for GitHub. Default: $env:AURIX_GIT_KEY, else ~/.ssh/id_ed25519_aurix_claude.
.PARAMETER PythonExe
    Python used for the compile check. Default: $env:AURIX_PYTHON, else `py -3` / `python` on PATH.
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
    [string]$GitKey = $(if ($env:AURIX_GIT_KEY) { $env:AURIX_GIT_KEY } else { Join-Path $HOME ".ssh/id_ed25519_aurix_claude" }),
    [string]$PythonExe = $(if ($env:AURIX_PYTHON) { $env:AURIX_PYTHON } else { (Get-Command python -ErrorAction SilentlyContinue).Source }),
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# Paths come from parameters / the environment, never from this file: the repository is public.
# Forward slashes: Git for Windows runs ssh through sh.exe, which eats backslashes.
$GitKey = $GitKey -replace '\\', '/'
$RepoUrl = "git@github.com:RascalOtaku/Aurix.git"
# Git for Windows' own bash runs the secret guard (scripts/git-hooks/secret_guard.sh).
# (git --exec-path is <Git>/mingw64/libexec/git-core on Windows, so <Git>/bin/bash.exe is three levels up.)
$GitBash = $null
$gitRoot = Split-Path (Split-Path (Split-Path (git --exec-path) -Parent) -Parent) -Parent
if ($gitRoot -and (Test-Path (Join-Path $gitRoot "bin/bash.exe"))) { $GitBash = Join-Path $gitRoot "bin/bash.exe" }
if (-not $GitBash) { $GitBash = (Get-Command bash -ErrorAction SilentlyContinue).Source }

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
if (-not $PythonExe) { throw "Precondition failed: no Python found (set AURIX_PYTHON or pass -PythonExe)" }
Assert-File $PythonExe "Python"
if (-not $GitBash) { throw "Precondition failed: Git for Windows' bash not found (needed for the secret guard)" }
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

    Invoke-Step "Secret guard (tokens, key files, your private patterns)" {
        # A fresh clone has no hook and no private patterns: copy yours in, then scan what would be published.
        $patterns = Join-Path (git -C $SourceDir rev-parse --absolute-git-dir) "info/aurix-private-patterns"
        if (-not (Test-Path $patterns)) { throw "no private patterns at $patterns - create it first (see docs/OPERATIONS.md)" }
        Copy-Item $patterns (Join-Path $cloneDir ".git/info/aurix-private-patterns")
        git -C $cloneDir checkout --quiet $Branch
        Push-Location $cloneDir
        try { & $GitBash scripts/git-hooks/secret_guard.sh --tree } finally { Pop-Location }
    }

    Invoke-Step "Push $Branch to origin/$PushTarget" {
        git -C $cloneDir push origin "$Branch`:$PushTarget"
    }

    Invoke-Step "Remote verification (ls-remote tip matches)" {
        $localSha = (git -C $cloneDir rev-parse $Branch).Trim()
        if ($LASTEXITCODE -ne 0) { throw "git rev-parse failed" }
        $remoteLine = (git -C $cloneDir ls-remote origin "refs/heads/$PushTarget").Trim()
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
