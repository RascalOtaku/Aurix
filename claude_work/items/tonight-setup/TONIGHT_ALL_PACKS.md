# Tonight: all three packs on itch.io in one sitting

Everything you upload is in this folder. Each pack folder has its paid zip, free zip, cover,
screenshots (upload in number order) and LISTING.md with the title, price, tags and text to paste.
Nothing has been uploaded or posted for you; every step below is yours.

Order: account and payouts first, then Frontier 1864, Saloon Interior, Rainwire 2089, then the tracker.
New itch.io pages start private, so you can check each one before making it public.

## 1. itch.io account and payouts (once)
- [ ] Sign up at itch.io and confirm the email.
- [ ] Settings > Payouts (seller settings): choose how you're paid and finish the tax form.
      The payouts mode has a $3 one-time ID check.

## 2. Frontier 1864 ($4 minimum) - folder 1_frontier
Paid page
- [ ] Dashboard > Create new project.
- [ ] Title: `Frontier 1864: Low-Poly Western Props + Pixel Sprites`   URL: `frontier-1864`
- [ ] Classification: Game assets. Kind: Downloadable.
- [ ] Pricing: Paid, minimum $4.00, tick "let people pay more".
- [ ] Upload `Frontier1864.zip`.
- [ ] Short description, body and 10 tags: from `LISTING.md`.
- [ ] Cover `cover_630x500.png`, then the 5 files in `screens/`.
- [ ] Save as draft, View page, look it over, set Public, Save. Copy the page address.

Free starter page
- [ ] Create new project: `Frontier 1864 Starter (Free)`   URL: `frontier-1864-starter`
- [ ] Game assets, Downloadable, Pricing: No payments.
- [ ] Upload `Frontier1864_Starter.zip`.
- [ ] Paste the starter text from `LISTING.md` and replace `PAID_PAGE_LINK` with the paid page's address.
- [ ] Same cover, screens 04 and 01. Public, Save.

## 3. Saloon Interior ($3 minimum) - folder 2_saloon
Paid page
- [ ] Create new project. Title: `Frontier 1864: Saloon Interior - Low-Poly Props + Pixel Sprites`
      URL: `frontier-1864-saloon`
- [ ] Game assets, Downloadable. Paid, minimum $3.00, tick "let people pay more".
- [ ] Upload `FrontierSaloon.zip`.
- [ ] Short description, body and 10 tags: from `LISTING.md`.
- [ ] Cover `cover_630x500.png`, then the 3 files in `screens/`.
- [ ] Optional: add a line at the end of the body pointing to the Frontier 1864 page.
- [ ] Draft, look it over, Public, Save. Copy the page address.

Free starter page
- [ ] Create new project: `Saloon Interior Starter (Free)`   URL: `frontier-1864-saloon-starter`
- [ ] Game assets, Downloadable, No payments. Upload `FrontierSaloon_Starter.zip`.
- [ ] Paste the starter text from `LISTING.md`, replace `PAID_PAGE_LINK` with the Saloon paid page's address.
- [ ] Same cover, screen 01. Public, Save.

## 4. Rainwire 2089 ($4 minimum) - folder 3_rainwire
Paid page
- [ ] Create new project. Title: `Rainwire 2089: Neon Street Props + Cyberpunk UI Kit`   URL: `rainwire-2089`
- [ ] Game assets, Downloadable. Paid, minimum $4.00, tick "let people pay more".
- [ ] Upload `Rainwire2089.zip` (about 24 MB).
- [ ] Short description, body and 10 tags: from `LISTING.md`.
- [ ] Cover `cover_630x500.png`, then the 5 files in `screens/`.
- [ ] Draft, look it over, Public, Save. Copy the page address.

Free starter page
- [ ] Create new project: `Rainwire 2089 Starter (Free)`   URL: `rainwire-2089-starter`
- [ ] Game assets, Downloadable, No payments. Upload `Rainwire2089_Starter.zip`.
- [ ] Paste the starter text from `LISTING.md`, replace `PAID_PAGE_LINK` with the Rainwire paid page's address.
- [ ] Same cover, screens 01 and 05. Public, Save.

## 5. Sales tracker (on the Tower, after the pages are live)
- [ ] itch.io > Settings > API keys > Generate new API key.
- [ ] Copy `install_itch_ledger.sh` (in this folder) to the Tower and run `bash install_itch_ledger.sh`.
      The first run creates `~/ai/brain/config/itch.env`. Paste the key after `ITCH_API_KEY=`, then run it again.
      It checks the key, takes the first snapshot, shows the 2 cron lines and asks before adding them.
