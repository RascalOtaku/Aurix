# Security Policy

Aurix is a self-hosted AI workspace with privileged local capabilities. Please do not run it as a public, unauthenticated service.

## Supported Versions

Security fixes are handled on the default branch until formal releases are cut.

## Deployment Guidance

- Keep `AUTH_ENABLED=true`.
- Use HTTPS when exposing the app beyond localhost.
- Put the app behind a trusted reverse proxy or private network.
- Protect `.env`, `data/`, logs, uploaded files, generated media, and database files.
- Disable open signup unless you intentionally want new accounts.
- Keep demo/test users non-admin, and remove them entirely on serious deployments.
- Give admin accounts strong passwords and enable 2FA where possible.
- Leave high-risk agent tools restricted to admins: shell, Python, file read/write, email send/read, MCP, app API, task/skill/memory management, settings, tokens, and model serving.
- Rotate API keys, webhook secrets, and Aurix API tokens if they appear in logs, screenshots, demos, or shared chats.
- Treat shell, model-serving, MCP, email, calendar, and vault features as privileged admin functionality.

## Publishing A Fork

Before pushing a public fork, run:

```bash
git status --short
git check-ignore -v .env data/auth.json data/app.db logs/compound.log aurix.db
git grep -n -I -E "(sk-[A-Za-z0-9_-]{20,}|xox[baprs]-|AIza[0-9A-Za-z_-]{20,}|Bearer [A-Za-z0-9._~+/-]{20,})" -- . ':!static/lib/**' ':!package-lock.json'
```

Only `.env.example`, docs, source, tests, and static assets should be committed. Never commit live `data/` contents, local databases, uploaded files, generated media, logs, backups, API keys, password hashes, or personal documents.

## How this repository keeps secrets out

The repository is public, so nothing personal or secret may reach it. Four layers, all using the same rules:

1. **`.gitignore`**: `data/`, `logs/`, `.env*`, keys, certificates, credential files and databases never get staged.
2. **Pre-commit hook** `scripts/git-hooks/secret_guard.sh` (turned on by `scripts/aurix_git_sync.sh`, or by
   `git config core.hooksPath scripts/git-hooks`): blocks secret-looking files, tokens and private keys, and
   anything matching your own private patterns in `.git/info/aurix-private-patterns` (one regex per line: your
   real IPs, hostnames, email). That file lives inside `.git`, so the patterns are never published.
3. **The sync script** runs the same guard before it commits and refuses to push on a hit.
4. **CI** (`.github/workflows/ci.yml`) scans the full history with gitleaks and the tree with the same guard on
   every push and pull request, with a read-only token and actions pinned to commit SHAs.

Code in this repo uses placeholders (`100.64.0.x`, `example.com`) for addresses; real ones go in `.env`.

## Reporting

Report vulnerabilities privately: **Security → Report a vulnerability** on GitHub (private vulnerability
reporting). Please do not open a public issue with exploit details.
