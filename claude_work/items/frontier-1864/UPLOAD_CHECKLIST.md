# Frontier 1864: tonight's upload (about 10 minutes)

Everything to paste is in LISTING.md (same folder). Everything to upload:

| What | File |
|---|---|
| Paid zip | `aurix/economic/pack/dist/Frontier1864.zip` |
| Free zip | `aurix/economic/pack/dist/Frontier1864_Starter.zip` |
| Cover (630x500) | `aurix/economic/pack/cover_630x500.png` |
| Screenshots, in this order | `listing/01_town_3d.png`, `02_mining_camp_3d.png`, `03_all_models_3d.png`, `04_sprites_catalog.png`, `06_prospector_walk.gif` |
| Spare screenshot | `listing/05_prospector_walk.png` (still version of the GIF) |

itch.io's own guide says: cover 315:250 ratio (630x500 is their example), 3 to 5 screenshots, up to 10 tags,
and new pages start private so you can check them before going public.

## 1. Account and payouts (once)
- [ ] Sign up at itch.io and confirm the email.
- [ ] Settings > Payouts (seller settings): pick how you're paid and finish the tax form. The payouts mode has a $3 one-time ID check.

## 2. Paid page
- [ ] Dashboard > Create new project.
- [ ] Title: `Frontier 1864: Low-Poly Western Props + Pixel Sprites`
- [ ] Project URL: `frontier-1864`
- [ ] Short description: from LISTING.md.
- [ ] Classification: Game assets. Kind of project: Downloadable.
- [ ] Pricing: Paid, minimum $4.00, and tick the box that lets people pay more.
- [ ] Uploads: Frontier1864.zip.
- [ ] Description: paste the body from LISTING.md.
- [ ] Tags: the 10 in LISTING.md.
- [ ] Cover image and the 5 screenshots above.
- [ ] Save as Draft, open "View page", look it over.
- [ ] Visibility: Public. Save.

## 3. Free starter page
- [ ] Create new project: `Frontier 1864 Starter (Free)`, URL `frontier-1864-starter`.
- [ ] Classification: Game assets. Downloadable. Pricing: No payments (or $0 or donate).
- [ ] Upload Frontier1864_Starter.zip.
- [ ] Paste its description from LISTING.md, then replace `PAID_PAGE_LINK` with the paid page's address.
- [ ] Same cover; screenshots 04 and 01 are enough.
- [ ] Public. Save.

## 4. Sales tracker (after both pages are live)
- [ ] itch.io > Settings > API keys > Generate new API key.
- [ ] Copy `aurix/economic/tracker/install_itch_ledger.sh` to the Tower and run `bash install_itch_ledger.sh`.
      First run creates `~/ai/brain/config/itch.env` (mode 600). Paste the key after `ITCH_API_KEY=` and run it again.
      It checks the key, takes the first snapshot, shows the 2 cron lines and asks before adding them.
