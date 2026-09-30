#!/usr/bin/env bash
# secret_guard.sh - the one secret/PII check used by the pre-commit hook, scripts/aurix_git_sync.sh and CI.
#
#   bash scripts/git-hooks/secret_guard.sh            check what is staged (pre-commit / sync)
#   bash scripts/git-hooks/secret_guard.sh --tree     check every tracked file (CI)
#
# Blocks: secret-looking FILES (.env, keys, certificates, credential dumps, databases), secret-looking CONTENT
# (private keys, GitHub/OpenAI/Anthropic/OpenRouter/Slack/AWS/Google tokens), and anything matching the owner's
# private patterns - one extended regex per line in .git/info/aurix-private-patterns (your real IPs, hostnames,
# email...). That file lives inside .git, so the patterns themselves are never committed or published.
# Test fixtures that deliberately contain fake tokens are exempt (tests/test_foundation_teacher.py etc.).
set -uo pipefail
MODE="${1:---staged}"
cd "$(git rev-parse --show-toplevel)" || exit 2

FILE_RX='(^|/)(\.env($|\.)|id_(rsa|dsa|ecdsa|ed25519)($|\.)|[^/]*\.(pem|key|p12|pfx|jks|keystore|kdbx|sqlite3?|db)$|credentials[^/]*\.json$|api_keys\.json$|auth\.json$|\.netrc$|\.pypirc$|service[-_]account[^/]*\.json$)'
ALLOW_FILE_RX='(^|/)\.env\.example$'
TOKEN_RX='-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,}|sk-(ant-api[0-9]{2}-|or-v1-|proj-)[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9]{48}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|[0-9]{8,10}:AA[A-Za-z0-9_-]{33}'
FIXTURES_RX='^tests/(test_foundation_teacher|test_starred_round2|test_starred_integrations)\.py$'
PRIVATE="$(git rev-parse --git-dir)/info/aurix-private-patterns"

if [ "$MODE" = "--tree" ]; then
    files="$(git ls-files)"
    content() { git ls-files -z | grep -zvE "$FIXTURES_RX" | xargs -0 -r grep -IHnE -- "$1" 2>/dev/null; }
else
    files="$(git diff --cached --name-only --diff-filter=ACMR)"
    content() { git diff --cached -U0 --diff-filter=ACMR -- . $(git diff --cached --name-only | grep -E "$FIXTURES_RX" | sed 's/^/:!/') \
                | grep -E '^\+[^+]' | grep -nE -- "$1"; }
fi

fail=0
bad_files="$(printf '%s\n' "$files" | grep -E "$FILE_RX" | grep -vE "$ALLOW_FILE_RX" || true)"
if [ -n "$bad_files" ]; then
    echo "secret-guard: secret-looking files - add them to .gitignore instead:"; printf '  %s\n' $bad_files; fail=1
fi
hits="$(content "$TOKEN_RX" | cut -c1-120 | sed -E 's/(-----BEGIN|ghp_|gho_|github_pat_|sk-|xox|AKIA|AIza)[A-Za-z0-9_:-]*/\1…REDACTED/g')"
if [ -n "$hits" ]; then
    echo "secret-guard: a private key or API token is in the changes - move it to .env:"; printf '%s\n' "$hits" | sed 's/^/  /'; fail=1
fi
if [ -s "$PRIVATE" ]; then
    rx="$(grep -vE '^\s*(#|$)' "$PRIVATE" | paste -sd'|' -)"
    if [ -n "$rx" ]; then
        phits="$(content "$rx" | cut -d: -f1-2 | sort -u)"
        if [ -n "$phits" ]; then
            echo "secret-guard: matches your private patterns ($PRIVATE) at:"; printf '%s\n' "$phits" | sed 's/^/  /'; fail=1
        fi
    fi
fi
[ "$fail" = 0 ] || { echo "secret-guard: blocked. (Bypass only if you are sure: git commit --no-verify)"; exit 1; }
exit 0
