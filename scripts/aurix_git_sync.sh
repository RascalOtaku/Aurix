#!/usr/bin/env bash
# aurix_git_sync.sh - make GitHub and this machine's Aurix match, both ways, without ever losing work.
#
#   bash scripts/aurix_git_sync.sh status        what differs, nothing changed (default)
#   bash scripts/aurix_git_sync.sh sync          commit local edits -> merge GitHub's -> push the result
#   bash scripts/aurix_git_sync.sh sync --dry-run   everything up to (not including) the commit, merge and push
#
# Your PC and home server already mirror each other with Syncthing, so run this on ONE of them (the server:
# it is always on). Running it on both would have two git histories racing over the same synced files.
#
# First run in a folder that is not a git checkout yet (a Syncthing copy): it adopts the folder in place. It
# points git at GitHub and compares - every file you have stays exactly as it is on disk, and your local
# differences become the first "sync" commit. Nothing is deleted or overwritten on either side.
#
# Safety rails:
#   * .gitignore keeps data/, logs/, .env and other secrets out; on top of that scripts/git-hooks/secret_guard.sh
#     scans what would be committed and the sync STOPS on a private key, an API token, a secret-looking file, or
#     a match for your own private patterns (.git/info/aurix-private-patterns: real IPs, hostnames, email).
#     It also turns that guard on as a pre-commit hook for every commit made on this machine.
#   * merges, never rebases or force-pushes; on a conflict it stops, leaves both versions marked in the files,
#     and tells you which ones - nothing is pushed until you resolve them and run `sync` again.
#   * one run at a time (lock file), and a log in logs/git_sync.log.
#
# Env knobs: AURIX_SYNC_REMOTE (default https://github.com/RascalOtaku/Aurix.git), AURIX_SYNC_BRANCH (default main),
#            AURIX_SYNC_MERGE="branch1 branch2" (also merge these GitHub branches in, e.g. a reviewed claude/* branch).
set -uo pipefail

# Work on the Aurix checkout this script lives in; if it was run from elsewhere (e.g. downloaded to /tmp),
# on the Aurix folder you are standing in.
here="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)"
if [ -n "$here" ] && [ -f "$here/app.py" ]; then cd "$here"
elif [ -f ./app.py ]; then :
else echo "STOP: run this from inside your Aurix folder (the one with app.py)"; exit 1
fi
REMOTE_URL="${AURIX_SYNC_REMOTE:-https://github.com/RascalOtaku/Aurix.git}"
BRANCH="${AURIX_SYNC_BRANCH:-main}"
EXTRA="${AURIX_SYNC_MERGE:-}"
MODE="${1:-status}"
DRY=0; [ "${2:-}" = "--dry-run" ] && DRY=1
mkdir -p logs 2>/dev/null
[ -w logs ] || { echo "STOP: cannot write to $(pwd)/logs (owned by $(stat -c %U logs 2>/dev/null || echo '?')). Fix: sudo chown -R $(id -un): $(pwd)/logs"; exit 1; }
LOG="logs/git_sync.log"
say() { echo "$*"; echo "$(date -Is) $*" >> "$LOG"; }
die() { say "STOP: $*"; exit 1; }

LOCK="logs/.git_sync.lock"
exec 9>"$LOCK" || die "cannot create the lock file $LOCK"
if command -v flock >/dev/null 2>&1; then flock -n 9 || die "another sync is running"; fi

command -v git >/dev/null || die "git is not installed"
git config user.name  >/dev/null 2>&1 || git config user.name  "Aurix sync ($(hostname))"
git config user.email >/dev/null 2>&1 || git config user.email "aurix-sync@$(hostname).local"

# --- adopt a plain folder (Syncthing copy) as a checkout, keeping every file on disk ------------------------
if [ ! -d .git ]; then
    if [ "$MODE" != "sync" ]; then
        say "Not a git checkout yet. 'sync --dry-run' adopts it (adds .git only, files untouched) and shows the diff; 'sync' also commits and pushes."
        exit 0
    fi
    say "Adopting $(pwd) as a checkout of $REMOTE_URL ($BRANCH); files on disk are not touched."
    git init -q . && git remote add origin "$REMOTE_URL" || die "git init failed"
    git fetch -q origin "$BRANCH" || die "cannot fetch $BRANCH from $REMOTE_URL (check network / credentials)"
    git checkout -q -b "$BRANCH" 2>/dev/null || true
    # Which GitHub commit was this folder copied from? The one the files on disk differ from least. Using it as the
    # base (instead of GitHub's newest) makes the first sync a real 3-way merge: files GitHub gained later come
    # down instead of looking "deleted here", and files changed on both sides are flagged, not overwritten.
    best=""; best_n=""
    for c in $(git rev-list --max-count=300 "origin/$BRANCH"); do
        git read-tree "$c" 2>/dev/null || continue
        n="$(git diff --name-only | wc -l)"
        if [ -z "$best_n" ] || [ "$n" -lt "$best_n" ]; then best="$c"; best_n="$n"; fi
        [ "$n" -eq 0 ] && break
    done
    [ -n "$best" ] || die "could not read any commit of origin/$BRANCH"
    git reset -q --mixed "$best" || die "could not point at $best"   # index = that commit, working tree = yours
    say "Closest GitHub commit to these files: $(git log -1 --format='%h %s' "$best") ($best_n file(s) differ)."
    git branch -q --set-upstream-to="origin/$BRANCH" "$BRANCH" 2>/dev/null || true
fi

git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REMOTE_URL"
# Every commit on this machine goes through the secret guard, not just the ones this script makes.
[ -d scripts/git-hooks ] && [ "$(git config core.hooksPath)" != "scripts/git-hooks" ] && git config core.hooksPath scripts/git-hooks
git fetch -q origin "$BRANCH" || die "cannot fetch $BRANCH (check network / credentials)"
for b in $EXTRA; do git fetch -q origin "$b" || die "cannot fetch $b"; done

current="$(git rev-parse --abbrev-ref HEAD)"
[ "$current" = "$BRANCH" ] || die "on branch '$current', expected '$BRANCH' (git checkout $BRANCH, or set AURIX_SYNC_BRANCH)"
[ -f .git/MERGE_HEAD ] && die "a previous merge is unfinished: resolve the conflicted files, 'git add' them, then run sync again"

changes="$(git status --porcelain)"
ahead="$(git rev-list --count "origin/$BRANCH..HEAD")"
behind="$(git rev-list --count "HEAD..origin/$BRANCH")"

echo "Local edits not on GitHub yet: $(printf '%s' "$changes" | grep -c . || true) file(s)"
printf '%s\n' "$changes" | sed -n '1,40p' | sed 's/^/    /'
echo "Local commits not on GitHub: $ahead    GitHub commits not here: $behind"
[ "$behind" -gt 0 ] && git log --oneline "HEAD..origin/$BRANCH" | sed -n '1,15p' | sed 's/^/    incoming  /'
for b in $EXTRA; do echo "Also merging origin/$b: $(git rev-list --count "HEAD..origin/$b") commit(s)"; done

[ "$MODE" = "sync" ] || exit 0

# --- secret guard on everything that would be committed (same rules as the pre-commit hook and CI) ----------
git add -A
staged_files="$(git diff --cached --name-only)"
GUARD="scripts/git-hooks/secret_guard.sh"
if [ ! -f "$GUARD" ]; then                  # first sync of an older copy: use GitHub's guard, never skip the check
    GUARD="$(mktemp)"; trap 'rm -f "$GUARD"' EXIT
    git show "origin/$BRANCH:scripts/git-hooks/secret_guard.sh" > "$GUARD" 2>/dev/null \
        || { git reset -q; die "no secret guard available (scripts/git-hooks/secret_guard.sh); refusing to commit"; }
fi
if ! bash "$GUARD" --staged; then
    git reset -q
    die "the secret guard blocked this sync (see above); nothing was committed or pushed"
fi

if [ "$DRY" = 1 ]; then
    git reset -q
    say "Dry run: the changes above would be committed, GitHub's merged in, and the result pushed. Nothing changed."
    exit 0
fi

if [ -n "$staged_files" ]; then
    git commit -q -m "Sync from $(hostname) $(date '+%Y-%m-%d %H:%M')" || die "commit failed"
    say "Committed $(printf '%s\n' "$staged_files" | grep -c .) local file(s)."
fi

for ref in "origin/$BRANCH" $(for b in $EXTRA; do echo "origin/$b"; done); do
    if ! git merge -q --no-edit "$ref"; then
        conflicted="$(git diff --name-only --diff-filter=U)"
        say "CONFLICT merging $ref in: $(echo $conflicted)"
        say "Both versions are marked in those files (<<<<<<< / >>>>>>>). Edit them, 'git add' them, then run sync again."
        exit 2
    fi
done

if [ "$(git rev-list --count "origin/$BRANCH..HEAD")" -gt 0 ]; then
    git push -q origin "HEAD:$BRANCH" || die "push failed (credentials? someone pushed meanwhile? just run sync again)"
    say "Pushed: GitHub $BRANCH now matches this machine ($(git rev-parse --short HEAD))."
else
    say "Already in sync ($(git rev-parse --short HEAD))."
fi
