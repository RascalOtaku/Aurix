# Tonight, in one sitting (Nov 11)

Unzip this anywhere. Do the sections in order. Nothing has been uploaded, published or signed up for you;
every step here is yours. Each itch.io page starts private, so you can check it before it goes public.

## 1. itch.io account and payouts (once)
- [ ] Sign up at itch.io and confirm the email.
- [ ] Settings > Payouts (seller settings): choose how you're paid and finish the tax form.
      The payouts mode has a $3 one-time ID check.

## 2. Three packs on itch.io (folder itch)
Each pack folder has its paid zip, free zip, cover, screenshots in `screens/` (upload in number order)
and `LISTING.md` with the short description, body, 10 tags and starter text to paste.
For every page: Create new project > Classification: Game assets > Kind: Downloadable >
save as draft > View page > look it over > Public > Save.

**Frontier 1864** (`1_frontier`)
- [ ] Paid: `Frontier 1864: Low-Poly Western Props + Pixel Sprites`, URL `frontier-1864`.
      Paid, minimum $4.00, allow paying more. Upload `Frontier1864.zip`. Cover + 5 screens. Copy the page address.
- [ ] Free: `Frontier 1864 Starter (Free)`, URL `frontier-1864-starter`. No payments.
      Upload `Frontier1864_Starter.zip`. Starter text, with `PAID_PAGE_LINK` replaced by the paid page's address.
      Cover + screens 04 and 01.

**Saloon Interior** (`2_saloon`)
- [ ] Paid: `Frontier 1864: Saloon Interior - Low-Poly Props + Pixel Sprites`, URL `frontier-1864-saloon`.
      Paid, minimum $3.00, allow paying more. Upload `FrontierSaloon.zip`. Cover + 3 screens. Copy the page address.
- [ ] Free: `Saloon Interior Starter (Free)`, URL `frontier-1864-saloon-starter`. No payments.
      Upload `FrontierSaloon_Starter.zip`. Starter text with `PAID_PAGE_LINK` replaced. Cover + screen 01.

**Rainwire 2089** (`3_rainwire`, the higher-poly version)
- [ ] Paid: `Rainwire 2089: Neon Street Props + Cyberpunk UI Kit`, URL `rainwire-2089`.
      Paid, minimum $4.00, allow paying more. Upload `Rainwire2089.zip` (about 24 MB). Cover + 5 screens. Copy the page address.
- [ ] Free: `Rainwire 2089 Starter (Free)`, URL `rainwire-2089-starter`. No payments.
      Upload `Rainwire2089_Starter.zip`. Starter text with `PAID_PAGE_LINK` replaced. Cover + screens 01 and 05.

## 3. Sales tracker (on the Tower, folder tracker)
- [ ] itch.io > Settings > API keys > Generate new API key.
- [ ] Copy `install_itch_ledger.sh` to the Tower and run:
      ```
      bash install_itch_ledger.sh
      ```
      The first run creates `~/ai/brain/config/itch.env`. Paste the key after `ITCH_API_KEY=`, run it again.
      It checks the key, takes the first snapshot, shows the 2 cron lines and asks y/N before adding them.

## 4. AURIX 7070 sync
- [ ] Where those two scripts live, run in this order:
      ```
      aurix_git_sync.sh sync
      aurix_deploy.sh
      ```
      Go/no-go: if the sync reports a conflict or anything other than a clean sync, stop there and send me the output
      before running the deploy.

## 5. Capri BUSY Bar spec (a 5-minute read, folder capri)
- [ ] Read `BUSY_BAR_BACKEND_SPEC.md`. Capri ships with the on-screen display either way; the Bar plugs into
      the same interface later.
- [ ] Tell me one thing: keep Capri on-screen only for now, or plan for a BUSY Bar on the desk.
      A review put the Bar at about $200, and buying one is your call.

## 6. Photo scout (on the steammachine, folder photos)
Immich is already installed there, so there's no install step.
- [ ] If the iPhone isn't backed up to it yet: follow `PHONE_SETUP.md`, including the hidden-screenshot
      go/no-go check before any backup. (Server URL uses Immich's default port 2283; use yours if you changed it.)
- [ ] After the backup finishes: follow `WINDOWS_RUN.md`. It puts `photo_scout.py` in
      `%USERPROFILE%\aurix\photo_scout\`, finds the Immich library folder and runs the scout read-only.
      Results land in `%USERPROFILE%\photos\scout`. Nothing gets uploaded.
