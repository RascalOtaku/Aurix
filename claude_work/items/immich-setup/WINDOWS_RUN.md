# Run the photo scout against Immich on the Windows steammachine

Read-only on the Immich library. Output goes outside it. Nothing is uploaded anywhere.

## 1. Get the script onto the steammachine

Put `photo_scout.py` (from this folder's parent) in `%USERPROFILE%\aurix\photo_scout\`.

## 2. Find where Immich keeps the originals

```powershell
docker inspect immich_server --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
```

The line ending in `-> /data` is the library folder (older Immich: `-> /usr/src/app/upload`).
With Docker Desktop it can show as `/run/desktop/mnt/host/d/immich/library`, which is `D:\immich\library`.
Or read `UPLOAD_LOCATION=` in the `.env` next to Immich's `docker-compose.yml` (relative paths are relative to that folder).

Originals are in its `upload` subfolder, and in `library` if the storage template is on.
Skip `thumbs`, `encoded-video`, `profile` and `backups`: those are Immich's own files.

## 3. Install and run

```powershell
py -m pip install --user pillow numpy pillow-heif
ollama pull qwen2.5vl:3b        # only if ollama is installed on this PC; otherwise add --no-vision below
$LIB = "D:\immich\library"      # from step 2
cd $env:USERPROFILE\aurix\photo_scout
py photo_scout.py --src "$LIB\upload" "$LIB\library" --out "$env:USERPROFILE\photos\scout" --export
```

Drop `"$LIB\library"` if that folder doesn't exist. Rerunning is quick (cached).

## 4. Results in `%USERPROFILE%\photos\scout`

`contact_sheet.jpg`, `shortlist.csv`, `upload\` (ready-to-sell copies, location data stripped),
`adobe_stock_metadata.csv`, `summary.txt`.

Rules: never write, move, rename or delete anything inside the Immich library; the script refuses an
`--out` inside a source folder. Don't open any website accounts or upload anything.
