# Operating Aurix

One page for the routine jobs: getting in, keeping GitHub and the server in step, deploying, rolling back,
and what guards the public repository. Commands run on the home server in the Aurix folder unless noted.

## Getting in

- **SSH with a key** from the PC: `ssh <user>@<server>`. One-time setup on Windows:
  `scripts/windows/aurix-ssh-setup.ps1 -Server <tailscale-ip> -User <linux-user>` (key, `ssh aurix` alias,
  Ctrl+Alt+A hotkey).
- **Tailscale SSH** as the backup way in, so a broken key or a hardened `sshd_config` can never lock you out:
  `sudo tailscale set --ssh` once on the server, then `tailscale ssh <user>@<server>` from any tailnet device.
- Keep both. Before changing SSH settings on the server, open a second session and keep it open until a new
  login works.

## Keeping GitHub and the server in step

`scripts/aurix_git_sync.sh` (full notes at the top of the script):

| Command | What it does |
|---|---|
| `status` | Shows what differs. Changes nothing. |
| `sync --dry-run` | Previews a sync. First run on a plain folder or an upstream clone adopts it (see below). |
| `sync` | Commits edits to files GitHub already has, merges GitHub's changes, pushes. |
| `sync --add-new` | Also publishes NEW files, only after you have reviewed the list `sync` prints. |
| `sync --to-branch=NAME` | Pushes this machine's state to its own branch and merges nothing, for a big merge done elsewhere. |

- The GitHub remote is found by URL; `origin` may be the upstream Odysseus project.
- A folder whose history GitHub doesn't share is adopted on a local branch `aurix`, starting at the closest
  GitHub commit. Other branches and all files are left as they are.
- Conflicts stop the sync with both versions marked in the file; nothing is pushed until you fix them,
  `git add` them and run `sync` again. It never rebases or force-pushes.

## Deploying and rolling back

- `bash scripts/aurix_deploy.sh`: saves the running build, builds and starts the new code, and puts the saved
  build back by itself if the new one is not healthy within about 3 minutes. It also clears a leftover
  container from an interrupted restart and retries once.
- `bash scripts/aurix_deploy.sh list`: builds you can go back to.
- `bash scripts/aurix_deploy.sh rollback [tag]`: go back. Only the code image changes; `data/` is never touched.
- Optional containers (FileBrowser, ReClip) start only with `COMPOSE_PROFILES=extras` in `.env`.

## What keeps the public repository clean

The repository is public. The same rules run in four places:

1. `.gitignore`: `data/`, `logs/`, `.env*`, keys, certificates, credential files, `personal_docs/`.
2. The pre-commit hook `scripts/git-hooks/secret_guard.sh`, switched on by the sync script: blocks tokens,
   private keys, secret-looking files, and anything matching your own patterns in
   `.git/info/aurix-private-patterns` (one regex per line: real IPs, hostnames, email). That file lives inside
   `.git`, so the patterns are never published.
3. The sync script runs the same guard before committing, and only publishes new files with `--add-new`.
4. CI on every push and pull request: gitleaks over the full history, the guard, the full test suite, and
   CodeQL.

Files to keep local without touching `.gitignore` go in `.git/info/exclude`.

### Every machine that pushes

Three things push to GitHub: the server (`scripts/aurix_git_sync.sh`), the Windows PC (`scripts/aurix-push.ps1`), and
cloud sessions. The rules only hold if each one has them:

| Where | Needs | Check |
|---|---|---|
| Server | `git config core.hooksPath scripts/git-hooks` (the sync sets it) and `.git/info/aurix-private-patterns` | `git config core.hooksPath` prints `scripts/git-hooks` |
| Windows PC | the same two, in the checkout `aurix-push.ps1` pushes from; it copies your patterns into its temporary clone and runs the guard before pushing | `git config core.hooksPath` in that folder |
| GitHub CI | the `AURIX_PRIVATE_PATTERNS` repository secret (Settings → Secrets and variables → Actions) | the CI log says `private patterns: N loaded` |

On Windows, write the patterns file with Unix line endings; with Windows line endings the patterns never match:

```powershell
$p = "first-pattern`nsecond-pattern`n"
[IO.File]::WriteAllText((Join-Path (git rev-parse --absolute-git-dir) 'info/aurix-private-patterns'), $p)
```

The sync script also scans the whole tree before any push, so a commit made on the server by hand, or by an agent,
without the hook is caught before it leaves the machine.

## Dependency updates

Dependabot opens weekly pull requests (pip, npm, Docker, GitHub Actions); CI tests each one. Major versions
need a real run as well as a green build. For example, MCP SDK 2.x passed CI but crashed every built-in server
until `mcp_servers/_common.mcp_server()` made them work on both versions; `tests/test_mcp_e2e.py` now starts a
real server so that kind of break fails CI.

## Where to look when something is wrong

| Symptom | Look at |
|---|---|
| App not answering | `docker compose ps`, `docker compose logs --tail 80 odysseus` |
| Last deploy result | `data/deploy_status.json`, `logs/deploy.log` |
| Last sync result | `logs/git_sync.log` |
| CI or CodeQL failing | The repository's Actions tab and Security → Code scanning |
