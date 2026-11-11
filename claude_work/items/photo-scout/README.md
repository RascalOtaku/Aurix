# AURIX photo scout: phone photos to stock

Goal: pull the ~10 to 30 best of Rascal's ~3,000 phone photos, tag them, and get them
ready to sell on Adobe Stock. Nothing gets uploaded, and no account gets opened, until Rascal says so.

## Site pick: Adobe Stock

Read on the sites' own contributor pages, 2026-11-10.

| Site | You get per sale | Payout | Notes |
|---|---|---|---|
| **Adobe Stock** (pick) | 33% of the license price, min $0.33 a photo on subscriptions (about $0.99 on a 10-image monthly plan) | $25 minimum; PayPal, Payoneer or Skrill | Non-exclusive. JPEG, sRGB, 4 to 100 MP. Takes a CSV of titles and keywords |
| Shutterstock | 15% at level 1 (levels go up to 40% and reset every January 1) | could not confirm | New accounts start at the bottom rate |
| Alamy | 15% (Bronze, under $250 a year in sales) since the 1 Sep 2026 contract | $75 minimum | Good for editorial shots (people, logos), but the low rate and high threshold hurt a small portfolio |
| Wirestock | stopped distributing to other stock sites in April 2026 | n/a | Out |

Why Adobe: the best rate for a beginner, the lowest payout threshold, non-exclusive (so Alamy
can be added later for the editorial-only shots), and it takes a metadata CSV, which this script writes.

What to expect: microstock pays cents to about a dollar per download. A 10 to 30 photo portfolio
earns occasional sales, not $1 to $5 every week by itself. Treat it as a small add-on to the itch.io pack.

Sources:
- https://helpx.adobe.com/stock/contributor/payments-earnings/royalties-pricing/royalty-rates-assets.html
- https://helpx.adobe.com/stock/contributor/payments-earnings/payment-taxes/payment-requirements.html
- https://helpx.adobe.com/stock/contributor/submit-your-content/submit-photos/technical-legal-requirements-photo-submission.html
- https://submit.shutterstock.com/help/en/articles/10594604-how-much-will-i-be-paid-as-a-contributor-to-shutterstock
- https://www.alamy.com/help/contributor-contract/
- https://wirestock.io/blog/whats-changing-on-wirestock-a-guide-for-creators

## Step 1 (recommended for iPhone): Immich on the tower

Immich is an open-source, self-hosted Google Photos replacement. The iPhone app backs up the whole
library to the tower over Wi-Fi or Tailscale and never sees the Hidden album. Nothing goes on the internet.

```bash
bash immich/install_immich.sh     # checks Docker, disk, Funnel; asks before starting
```

Then follow `immich/PHONE_SETUP.md` on the iPhone (it includes a go/no-go check that Hidden stays off),
and score the library read-only with:

```bash
bash immich/scout_immich.sh
```

## Step 1 (alternative): copy the photos by hand (copies; the phone keeps its originals)

Set your tower login once (the Tailscale name of the 3431):

```bash
TOWER=YOUR_USER@rascal-precision-tower-3431
ssh $TOWER 'mkdir -p ~/photos/phone'
```

**iPhone, through the MacBook (simplest):** plug the iPhone into the Mac, open **Image Capture**,
pick the iPhone, select all, import to `~/Desktop/phone-photos`. Then:

```bash
rsync -av --progress ~/Desktop/phone-photos/ $TOWER:~/photos/phone/
```

(Or in the Photos app: select all, then File > Export > Export Unmodified Originals.)

**Android, over USB to a Linux box or the Mac (needs adb):**

```bash
adb pull /sdcard/DCIM/Camera ~/phone-photos
rsync -av --progress ~/phone-photos/ $TOWER:~/photos/phone/
```

**Ongoing, no cable:** Syncthing already runs on the tower. On Android install Syncthing-Fork,
on iPhone install Mobius Sync, share the camera folder to the tower as **Send Only**, and point
the tower side at `~/photos/phone`. Send Only means the tower can never change or delete the phone's photos.

## Step 2: score them on the tower

```bash
cd ~/ai/brain/photo_scout            # or wherever you copy this folder
python3 -m pip install --user pillow numpy pillow-heif   # pillow-heif reads iPhone .HEIC
ollama pull qwen2.5vl:3b              # ~3 GB vision model, fits the GTX 1660 Super
python3 photo_scout.py --src ~/photos/phone --out ~/photos/scout --export
```

Runs the CPU pass on all 3,000 (a few minutes on the i9), then the vision model on the best 80.
Rerunning is quick: results are cached in `~/photos/scout/cache.json`.

Fast dry run with no model: add `--no-vision`.

### What it writes to `~/photos/scout`

- `contact_sheet.jpg`: the top 36 with rank and verdict. Green = SUBMIT, amber = needs a release.
- `shortlist.csv`: ranked, with title, keywords, category, release flags and the reason for each score.
- `upload/`: upload-ready copies of the SUBMIT shots (sRGB JPEG, location and camera data stripped,
  a light polish: auto-levels and gentle sharpening). `--no-enhance` skips the polish.
- `adobe_stock_metadata.csv`: titles and keywords in Adobe's CSV format, matching the files in `upload/`.
- `all_scored.csv`, `duplicates.csv`, `summary.txt`: everything else and why it was dropped.

### What it filters

- under 4 MP (Adobe's minimum), blurry, too dark or blown out, unreadable
- near-duplicates (burst shots and retakes): keeps the sharpest one
- low stock appeal per the vision model
- flags anything with recognisable people, a private home, logos or brands, licence plates or artwork:
  those need a release or are editorial-only, so they are not in `upload/`

The source folder is only read. The script refuses to run if `--out` is the same folder, and the tests
check every source file's hash before and after.

About "hasty" shots: the polish helps a flat or slightly soft photo, but it cannot add detail that
isn't there. Adobe reviewers reject over-sharpened, noisy or upscaled images, so the export stays mild
and never enlarges anything.

## Step 3: upload (Rascal's step, after a yes)

1. Open the contact sheet and the shortlist. Delete from `upload/` (and the CSV) anything you don't want sold.
2. Create a free Adobe Stock contributor account with your Adobe ID and fill in the tax form there,
   so sales aren't held back at the 30% withholding rate.
3. In the contributor portal, upload the files in `upload/`.
4. Upload `adobe_stock_metadata.csv` in the portal to fill the titles and keywords, check each one,
   and tick the release box only where a release really exists.
5. Submit. Adobe reviews them; payouts start once the balance reaches $25.
6. Later, optional: send the amber (people or logo) shots to Alamy as editorial.

AURIX does not upload, sign in or accept terms on its own. The contributor account and every submit are yours.

## Tests

```bash
python3 test_photo_scout.py     # or: python3 -m pytest -q test_photo_scout.py
```

Builds synthetic photos (sharp, blurry, tiny, dark, broken, a near-duplicate, one with GPS),
runs against a fake ollama, and checks the filters, the outputs, the cache and that the source is untouched.
