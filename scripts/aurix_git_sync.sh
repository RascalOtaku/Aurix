#!/usr/bin/env bash
# aurix_git_sync.sh - make GitHub and this machine's Aurix match, both ways, without ever losing work.
#
#   bash scripts/aurix_git_sync.sh status        what differs, nothing changed (default)
#   bash scripts/aurix_git_sync.sh sync          commit local edits -> merge GitHub's -> push the result
#   bash scripts/aurix_git_sync.sh sync --dry-run   everything up to (not including) the commit, merge and push
#   bash scripts/aurix_git_sync.sh sync --add-new   also publish NEW files (only after reviewing the list it shows)
#   bash scripts/aurix_git_sync.sh sync --to-branch=NAME   commit and push this machine's state to its own branch,
#                                                   merge nothing (for a big merge done and tested elsewhere)
#
# Your PC and home server already mirror each other with Syncthing, so run this on ONE of them (the server:
# it is always on). Running it on both would have two git histories racing over the same synced files.
#
# First run in a folder that is not a git checkout yet (a Syncthing copy): it adopts the folder in place. It
# points git at GitHub and compares - every file you have stays exactly as it is on disk, and your local
# differences become the first "sync" commit. Nothing is deleted or overwritten on either side.
#
# Already a git repo with a DIFFERENT history (e.g. a clone of the upstream Odysseus project with Aurix edits
# on top): the GitHub remote is found by URL, and the folder is adopted on a separate local branch `aurix`
# starting from the closest GitHub commit. Your other branches (main, overnight-..., the upstream remote) and
# every file on disk are left exactly as they are. Undo: git symbolic-ref HEAD refs/heads/<old-branch> && git reset -q
#
# Safety rails:
#   * Only files GitHub already tracks are synced automatically. NEW files (not on GitHub yet) are listed and
#     left out until you review them and pass --add-new; a public repo must never pick up personal folders just
#     because they exist on disk.
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
DRY=0; ADD_NEW=0; TO_BRANCH=""
for a in "${@:2}"; do
    case "$a" in
        --dry-run) DRY=1 ;;
        --add-new) ADD_NEW=1 ;;
        --to-branch=*) TO_BRANCH="${a#--to-branch=}" ;;
        *) echo "unknown option: $a (use --dry-run, --add-new, --to-branch=NAME)"; exit 64 ;;
    esac
done
case "$TO_BRANCH" in ""|*[!A-Za-z0-9._/-]*|"$BRANCH") [ -z "$TO_BRANCH" ] || { echo "bad --to-branch name: $TO_BRANCH"; exit 64; } ;; esac
mkdir -p logs 2>/dev/null
[ -w logs ] || { echo "STOP: cannot write to $(pwd)/logs (owned by $(stat -c %U logs 2>/dev/null || echo '?')). Fix: sudo chown -R $(id -un): $(pwd)/logs"; exit 1; }
LOG="logs/git_sync.log"
say() { echo "$*"; echo "$(date -Is) $*" >> "$LOG"; }
die() { say "STOP: $*"; exit 1; }

LOCK="logs/.git_sync.lock"
exec 9>"$LOCK" || die "cannot create the lock file $LOCK"
if command -v flock >/dev/null 2>&1; then flock -n 9 || die "another sync is running"; fi

command -v git >/dev/null || die "git is not installed"

# Point HEAD at the GitHub commit these files are closest to, on local branch $1, without touching any file.
# Using that commit as the base (not GitHub's newest) makes the first sync a real 3-way merge: files GitHub
# gained later come down instead of looking "deleted here", and files changed on both sides are flagged.
adopt_onto() {
    local local_branch="$1" best="" best_n="" n c
    for c in $(git rev-list --max-count=300 "$REMOTE/$BRANCH"); do
        git read-tree "$c" 2>/dev/null || continue
        n="$(git diff --name-only | wc -l)"
        if [ -z "$best_n" ] || [ "$n" -lt "$best_n" ]; then best="$c"; best_n="$n"; fi
        [ "$n" -eq 0 ] && break
    done
    [ -n "$best" ] || { git read-tree HEAD 2>/dev/null; die "could not read any commit of $REMOTE/$BRANCH"; }
    git update-ref "refs/heads/$local_branch" "$best" || die "could not create branch $local_branch"
    git symbolic-ref HEAD "refs/heads/$local_branch"
    git reset -q --mixed "$best" || die "could not point at $best"       # index = that commit, files = yours
    git branch -q --set-upstream-to="$REMOTE/$BRANCH" "$local_branch" 2>/dev/null || true
    say "Adopted on branch '$local_branch' from the closest GitHub commit: $(git log -1 --format='%h %s' "$best") ($best_n tracked file(s) differ)."
}

# --- a plain folder (Syncthing copy): make it a checkout, keeping every file on disk ------------------------
if [ ! -d .git ]; then
    if [ "$MODE" != "sync" ]; then
        say "Not a git checkout yet. 'sync --dry-run' adopts it (adds .git only, files untouched) and shows the diff; 'sync' also commits and pushes."
        exit 0
    fi
    say "Adopting $(pwd) as a checkout of $REMOTE_URL ($BRANCH); files on disk are not touched."
    git init -q . || die "git init failed"
fi
git config user.name  >/dev/null 2>&1 || git config user.name  "Aurix sync ($(hostname))"
git config user.email >/dev/null 2>&1 || git config user.email "aurix-sync@$(hostname).local"

# The GitHub remote, found by URL: on a clone of the upstream project `origin` is somebody else's repository.
REMOTE="${AURIX_SYNC_REMOTE_NAME:-}"
if [ -z "$REMOTE" ]; then                   # 1) the remote with exactly this URL, 2) any RascalOtaku/Aurix URL
    REMOTE="$(git remote -v | awk -v u="$REMOTE_URL" '$2 == u {print $1; exit}')"
fi
if [ -z "$REMOTE" ]; then
    REMOTE="$(git remote -v | awk 'tolower($2) ~ /rascalotaku\/aurix(\.git)?$/ {print $1; exit}')"
fi
if [ -z "$REMOTE" ]; then
    if git remote get-url origin >/dev/null 2>&1; then REMOTE="aurix-origin"; else REMOTE="origin"; fi
    git remote add "$REMOTE" "$REMOTE_URL" || die "could not add remote $REMOTE"
fi
[ -n "${AURIX_SYNC_REMOTE:-}" ] && git remote set-url "$REMOTE" "$REMOTE_URL"
git fetch -q "$REMOTE" "$BRANCH" || die "cannot fetch $BRANCH from $(git remote get-url "$REMOTE") (check network / credentials)"
for b in $EXTRA; do git fetch -q "$REMOTE" "$b" || die "cannot fetch $b"; done

if ! git rev-parse -q --verify HEAD >/dev/null; then
    adopt_onto "$BRANCH"                                                    # fresh `git init` above
else
    current="$(git rev-parse --abbrev-ref HEAD)"
    if ! git merge-base HEAD "$REMOTE/$BRANCH" >/dev/null 2>&1; then
        # A repo whose history GitHub doesn't share. Use (or create) the dedicated `aurix` branch.
        if git rev-parse -q --verify refs/heads/aurix >/dev/null && git merge-base refs/heads/aurix "$REMOTE/$BRANCH" >/dev/null 2>&1; then
            die "on '$current', but your Aurix sync branch is 'aurix': git symbolic-ref HEAD refs/heads/aurix && git reset -q   (files stay as they are), then run again"
        fi
        if [ "$MODE" != "sync" ]; then
            say "This repo ('$current') has no history in common with GitHub. 'sync --dry-run' adopts it on a new local branch 'aurix' (other branches and all files untouched) and shows what would sync."
            exit 0
        fi
        say "This repo ('$current') has no history in common with GitHub; adopting it on a new branch 'aurix' (other branches untouched)."
        adopt_onto aurix
    else
        upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
        [ "$current" = "$BRANCH" ] || [ "$current" = "aurix" ] || [ "$upstream" = "$REMOTE/$BRANCH" ] \
            || die "on branch '$current', which does not track $REMOTE/$BRANCH (git checkout $BRANCH, or set AURIX_SYNC_BRANCH)"
    fi
fi

# Every commit on this machine goes through the secret guard, not just the ones this script makes.
[ -d scripts/git-hooks ] && [ "$(git config core.hooksPath)" != "scripts/git-hooks" ] && git config core.hooksPath scripts/git-hooks
[ -f "$(git rev-parse --git-dir)/MERGE_HEAD" ] && die "a previous merge is unfinished: resolve the conflicted files, 'git add' them, then run sync again"

tracked_changes="$(git status --porcelain --untracked-files=no)"
new_files="$(git ls-files --others --exclude-standard)"
ahead="$(git rev-list --count "$REMOTE/$BRANCH..HEAD")"
behind="$(git rev-list --count "HEAD..$REMOTE/$BRANCH")"

echo "Edited here, not on GitHub yet: $(printf '%s' "$tracked_changes" | grep -c . || true) file(s)"
printf '%s\n' "$tracked_changes" | sed -n '1,200p' | sed 's/^/    /'
n_new="$(printf '%s' "$new_files" | grep -c . || true)"
if [ "$n_new" -gt 0 ]; then
    if [ "$ADD_NEW" = 1 ]; then echo "NEW files that WILL be published (--add-new): $n_new"
    else echo "NEW files GitHub doesn't have: $n_new - left out. Review them; add the personal ones to .gitignore, then use --add-new:"
    fi
    printf '%s\n' "$new_files" | sed -n '1,200p' | sed 's/^/    + /'
    [ "$n_new" -gt 200 ] && echo "    ... ($n_new total; full list: git ls-files --others --exclude-standard)"
fi
echo "Local commits not on GitHub: $ahead    GitHub commits not here: $behind"
[ "$behind" -gt 0 ] && git log --oneline "HEAD..$REMOTE/$BRANCH" | sed -n '1,15p' | sed 's/^/    incoming  /'
for b in $EXTRA; do echo "Also merging $REMOTE/$b: $(git rev-list --count "HEAD..$REMOTE/$b") commit(s)"; done

[ "$MODE" = "sync" ] || exit 0

# --- secret guard on everything that would be committed (same rules as the pre-commit hook and CI) ----------
git add -u                                   # edits and deletions of files GitHub already has
[ "$ADD_NEW" = 1 ] && [ -n "$new_files" ] && git add -A    # new files only when asked for, after review
staged_files="$(git diff --cached --name-only)"
GUARD="scripts/git-hooks/secret_guard.sh"
if [ ! -f "$GUARD" ]; then                  # first sync of an older copy: use GitHub's guard, never skip the check
    GUARD="$(mktemp)"; trap 'rm -f "$GUARD"' EXIT
    git show "$REMOTE/$BRANCH:scripts/git-hooks/secret_guard.sh" > "$GUARD" 2>/dev/null \
        || { git reset -q; die "no secret guard available (scripts/git-hooks/secret_guard.sh); refusing to commit"; }
fi
if ! bash "$GUARD" --staged; then
    git reset -q
    die "the secret guard blocked this sync (see above); nothing was committed or pushed"
fi

if [ "$DRY" = 1 ]; then
    git reset -q
    say "Dry run: the edits above would be committed, GitHub's merged in, and the result pushed. No file or commit changed."
    exit 0
fi

if [ -n "$staged_files" ]; then
    git commit -q -m "Sync from $(hostname) $(date '+%Y-%m-%d %H:%M')" || die "commit failed"
    say "Committed $(printf '%s\n' "$staged_files" | grep -c .) local file(s)."
fi

if [ -n "$TO_BRANCH" ]; then
    # Hand the local state over for merging elsewhere: push it to its own branch, merge nothing here.
    git push -q "$REMOTE" "HEAD:refs/heads/$TO_BRANCH" || die "push to branch $TO_BRANCH failed"
    say "Pushed this machine's state to GitHub branch '$TO_BRANCH' ($(git rev-parse --short HEAD)); nothing merged here. Once it is merged into $BRANCH, run: sync"
    exit 0
fi

for ref in "$REMOTE/$BRANCH" $(for b in $EXTRA; do echo "$REMOTE/$b"; done); do
    if ! git merge -q --no-edit "$ref"; then
        conflicted="$(git diff --name-only --diff-filter=U)"
        say "CONFLICT merging $ref in: $(echo $conflicted)"
        say "Both versions are marked in those files (<<<<<<< / >>>>>>>). Edit them, 'git add' them, then run sync again."
        exit 2
    fi
done

if [ "$(git rev-list --count "$REMOTE/$BRANCH..HEAD")" -gt 0 ]; then
    git push -q "$REMOTE" "HEAD:$BRANCH" || die "push failed (credentials? someone pushed meanwhile? just run sync again)"
    say "Pushed: GitHub $BRANCH now matches this machine ($(git rev-parse --short HEAD))."
else
    say "Already in sync ($(git rev-parse --short HEAD))."
fi
