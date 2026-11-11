# Claude's work (AURIX One library)

Everything Claude makes for you, in one folder the AURIX One dash can show.

- `manifest.json`: the list the dash reads. Each item has `id`, `title`, `kind`
  (pack, art, spec, checklist, script), `date`, `note`, `path`, `files`, an optional `thumb`,
  and `external` for big zips that stay outside the library.
- `items/<id>/`: the files for each item (covers, listings, checklists, specs, scripts, art).
- `index.html`: a standalone panel that reads `manifest.json` and shows the cards, in the
  Rainwire/Capri colors. Open it as an iframe tab in AURIX One, or serve the folder:

```
cd claude_work && python3 -m http.server 8790
```

## Dropping in new work

```
python3 add_item.py --id rainwire-2 --title "Rainwire 2" --kind pack \
  --note "One line on what it is." cover.png LISTING.md --thumb cover.png --external path/to/Rainwire2.zip
```

Same `--id` again replaces the entry. Paid pack zips are listed under `external`, never copied in,
so this folder is safe to keep in a repo without giving the packs away.

## Wiring it into AURIX One

Copy this folder into the dash's static files (for the Aurix app that's `static/claude_work/`, served
at `/static/claude_work/index.html`) and add a tab whose iframe points there, the same way
`static/aurix_panels.html` adds Cortex, Agents and Jellyfin.

## What's kept out of this repo

This repo is public, so paid pack zips and the full Capri status art set (it ships inside
Rainwire 2089) are listed under `external` and never committed. Nothing personal goes in here:
no account keys, mail, benefits or personal photos.
