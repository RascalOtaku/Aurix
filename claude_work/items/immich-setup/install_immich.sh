#!/usr/bin/env bash
# Install Immich (self-hosted photo library) on the AURIX tower with Docker Compose.
#
# - Reachable on your LAN and tailnet at port 2283. Does NOT touch Tailscale Funnel or cloudflared,
#   and stops if Funnel is serving anything, so nothing is put on the public internet.
# - Machine learning (search/faces) runs on the CPU so it doesn't fight ollama for the GTX 1660.
# - Never overwrites an existing install. Asks before starting anything.
#
# Usage:   bash install_immich.sh
# Options (env): IMMICH_DIR=/srv/immich  UPLOAD_LOCATION=/srv/immich/library  DB_DATA_LOCATION=/srv/immich/postgres
set -euo pipefail

IMMICH_DIR="${IMMICH_DIR:-/srv/immich}"
UPLOAD_LOCATION="${UPLOAD_LOCATION:-$IMMICH_DIR/library}"
DB_DATA_LOCATION="${DB_DATA_LOCATION:-$IMMICH_DIR/postgres}"   # must be a local disk, not a network share
TZ_NAME="${TZ_NAME:-America/Denver}"
MIN_FREE_GB="${MIN_FREE_GB:-50}"
REL="https://github.com/immich-app/immich/releases/latest/download"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# ---- preflight (read-only)
command -v docker >/dev/null || die "Docker not installed. Install it first: curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker \$USER (then log out and back in)"
docker compose version >/dev/null 2>&1 || die "'docker compose' plugin missing (sudo apt install docker-compose-plugin)"
docker info >/dev/null 2>&1 || die "Can't talk to Docker. Add yourself to the docker group or run with sudo."

if command -v tailscale >/dev/null; then
  fs="$(tailscale funnel status 2>&1 || true)"
  if ! grep -qiE "no serve config|not enabled|^$" <<<"$fs"; then
    echo "$fs"
    die "Tailscale Funnel is serving something. Turn it off (sudo tailscale funnel off) before installing, so Immich stays private."
  fi
fi
if systemctl is-active --quiet cloudflared-aurix.service 2>/dev/null; then
  die "cloudflared-aurix tunnel is running. Stop it first (sudo systemctl stop cloudflared-aurix.service) so Immich stays private."
fi

if [[ -e "$IMMICH_DIR/docker-compose.yml" ]]; then
  die "$IMMICH_DIR/docker-compose.yml already exists. Not touching an existing install. (cd $IMMICH_DIR && docker compose ps)"
fi

parent="$(dirname "$UPLOAD_LOCATION")"; while [[ ! -d "$parent" ]]; do parent="$(dirname "$parent")"; done
free_gb=$(( $(df -Pk "$parent" | awk 'NR==2{print $4}') / 1024 / 1024 ))
fs_type="$(df -PT "$parent" | awk 'NR==2{print $2}')"
ram_gb=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 / 1024 ))
ts_ip="$(tailscale ip -4 2>/dev/null | head -1 || true)"
lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"

cat <<EOF

Immich install plan
  install folder : $IMMICH_DIR
  photos stored  : $UPLOAD_LOCATION   ($fs_type, ${free_gb} GB free)
  database       : $DB_DATA_LOCATION
  timezone       : $TZ_NAME
  RAM            : ${ram_gb} GB (Immich wants 6+)
  will be at     : http://${ts_ip:-<tailscale-ip>}:2283 (tailnet)  http://${lan_ip:-<lan-ip>}:2283 (LAN)
  internet       : not exposed (Funnel/cloudflared checked above)

EOF
(( free_gb >= MIN_FREE_GB )) || warn "Only ${free_gb} GB free there. 3,000 iPhone photos plus videos can pass 30-60 GB. Set UPLOAD_LOCATION to a bigger disk if needed."
case "$fs_type" in nfs*|cifs|smb*|fuse.sshfs) warn "Photos folder is on a network share; fine for photos, but keep DB_DATA_LOCATION on a local disk.";; esac
(( ram_gb >= 6 )) || warn "Less than 6 GB RAM."

read -r -p "Go? This creates $IMMICH_DIR and starts 4 containers. [y/N] " go
[[ "$go" =~ ^[Yy]$ ]] || { echo "Stopped. Nothing changed."; exit 0; }

# ---- install
sudo mkdir -p "$IMMICH_DIR" "$UPLOAD_LOCATION" "$DB_DATA_LOCATION"
sudo chown "$(id -u):$(id -g)" "$IMMICH_DIR"
cd "$IMMICH_DIR"
say "Downloading docker-compose.yml and example.env from the latest Immich release"
curl -fsSL -o docker-compose.yml "$REL/docker-compose.yml"
curl -fsSL -o .env "$REL/example.env"

pw="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
sed -i \
  -e "s|^UPLOAD_LOCATION=.*|UPLOAD_LOCATION=$UPLOAD_LOCATION|" \
  -e "s|^DB_DATA_LOCATION=.*|DB_DATA_LOCATION=$DB_DATA_LOCATION|" \
  -e "s|^# *TZ=.*|TZ=$TZ_NAME|" \
  -e "s|^DB_PASSWORD=.*|DB_PASSWORD=$pw|" .env
chmod 600 .env
grep -q "^TZ=" .env || echo "TZ=$TZ_NAME" >> .env

say "Pulling images and starting Immich"
docker compose pull
docker compose up -d

say "Waiting for the server to answer"
for _ in $(seq 1 60); do
  if curl -fs "http://127.0.0.1:2283/api/server/ping" >/dev/null 2>&1; then ok=1; break; fi
  sleep 5
done
[[ "${ok:-}" == 1 ]] || { docker compose ps; die "Immich didn't come up in 5 minutes. Check: cd $IMMICH_DIR && docker compose logs immich-server | tail -50"; }

cat <<EOF

Immich is up.
  1. On this machine or any tailnet device, open http://${ts_ip:-<tailscale-ip>}:2283 and create the admin account
     (local to the tower; no cloud account involved).
  2. Then follow PHONE_SETUP.md on the iPhone.
  3. After the first backup finishes, score the library:  bash $(dirname "$(readlink -f "$0")")/scout_immich.sh

Originals live under $UPLOAD_LOCATION (upload/ and library/). Don't edit or move files there by hand.
Back up $IMMICH_DIR/.env and $UPLOAD_LOCATION with the rest of your backups.
EOF
