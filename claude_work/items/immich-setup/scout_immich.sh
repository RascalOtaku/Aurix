#!/usr/bin/env bash
# Run photo_scout over the Immich library, read-only. Output goes OUTSIDE the library.
#
# Usage:   bash scout_immich.sh            (extra args go to photo_scout.py, e.g. --no-vision)
set -euo pipefail
IMMICH_DIR="${IMMICH_DIR:-/srv/immich}"
OUT="${OUT:-$HOME/photos/scout}"
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SCOUT="${SCOUT:-$HERE/../photo_scout.py}"

[[ -f "$IMMICH_DIR/.env" ]] || { echo "No Immich install at $IMMICH_DIR (set IMMICH_DIR)"; exit 1; }
UPLOAD_LOCATION="$(sed -n 's/^UPLOAD_LOCATION=//p' "$IMMICH_DIR/.env")"
[[ "$UPLOAD_LOCATION" = /* ]] || UPLOAD_LOCATION="$IMMICH_DIR/${UPLOAD_LOCATION#./}"

# Originals only: upload/ (default) and library/ (if the storage template is on).
# thumbs/, encoded-video/, profile/ and backups/ are Immich's own files and are skipped.
srcs=()
for d in upload library; do [[ -d "$UPLOAD_LOCATION/$d" ]] && srcs+=("$UPLOAD_LOCATION/$d"); done
(( ${#srcs[@]} )) || { echo "No originals yet under $UPLOAD_LOCATION. Has the iPhone backup run?"; exit 1; }

# Files written by the container can be root-owned; check we can read them without changing anything.
first="$(find "${srcs[@]}" -type f -print -quit)"
if [[ -n "$first" && ! -r "$first" ]]; then
  echo "Can't read Immich's files as $(id -un). Rerun as: sudo -E bash $0 $*"
  exit 1
fi

python3 -c "import pillow_heif" 2>/dev/null || echo "Tip: python3 -m pip install --user pillow-heif  (needed for iPhone HEIC photos)"
exec python3 "$SCOUT" --src "${srcs[@]}" --out "$OUT" --export "$@"
