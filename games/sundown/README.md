# Zero Hour: Sundown

A twin-stick survival shooter set in the Zero Hour world. Colorado Territory, 1864: you hold the
mining town of Sundown from 6 PM to dawn, one wave an hour. At midnight the church bell rings twelve,
something opens in the mine, and the year 2089 bleeds through. The barrels turn into trash piles,
the wagons into dumpsters, the outlaws start teleporting, and drones come down out of the dark.

Every sprite comes from the three packs (Frontier 1864, Frontier Saloon, Rainwire 2089).
Every sound and all the music are synthesised live with WebAudio. No audio files, no network
requests, no build step: it is plain HTML and JavaScript that runs from a folder.

Screenshots and a demo video come from `node tools/shot.js --gif` (they land in `tools/out/`, which is not committed because it shows the paid art).

## Play it

- **In AURIX:** open `/static/games/sundown/index.html` on your AURIX server (the `/static` path needs no login).
- **On its own:** open `static/games/sundown/index.html` in a browser. It works from `file://`.

The art is built from the packs (it is git-ignored, because the packs are paid assets). Build it once:

```
python3 games/sundown/tools/build_assets.py \
  --pack path/to/Frontier1864.zip --pack path/to/FrontierSaloon.zip --pack path/to/Rainwire2089.zip
```

This writes `static/games/sundown/assets/` (sprites, tiles, the Rainwire Mono font and `manifest.js`).
It needs Python 3 and Pillow. Without it the game still runs and draws coloured blocks.

## How it plays

- **Ten waves, 6 PM to 4 AM.** The clock in the HUD is the wave timer: it moves with your kills.
- **Wave 6 (11 PM): Jim Reynolds**, Confederate raider, three phases: fanned bursts, a dynamite volley with backup, then he charges.
- **Midnight (wave 7): Zero Hour.** The rift spreads from the mine across the whole town. 2089 enemies join: Glitch gunmen who teleport, hover drones that shoot over cover, security turrets.
- **Wave 10: the Rainwire Warden**, a security daemon with bullet spirals, rings and a telegraphed lance.
- **The Last Chance Saloon** between waves: whiskey, dynamite, two frontier guns, six upgrades, and after midnight two guns from 2089.
- **Modes:** Story (survive to dawn), Endless (waves keep coming and scaling), Daily Ride (everyone gets the same town and waves for the day; your best is saved per day).

| | Keyboard and mouse | Controller (PlayStation or Xbox) |
|---|---|---|
| Move / aim | WASD, mouse | left stick, right stick |
| Fire / fan the hammer | left / right click | RT / LB |
| Dash (dodges bullets) | Space or Shift | A |
| Dynamite | Q or G | RB |
| Focus (slow time) | hold F or C | hold LT |
| Reload / switch gun | R / 1-5, wheel, Tab | X / Y |
| Pause | Esc or P | Start |

Touch works too: left thumb moves, right thumb aims and fires, buttons on the right.

Things worth knowing: dashing makes you untouchable for a moment, kills and crits fill the focus
meter, wooden cover breaks and rock does not, dynamite crates go off when shot, and the rail driver
goes through everything.

## How it is built

```
static/games/sundown/
  index.html, style.css
  js/00-core.js     namespace, math, seeded RNG (mulberry32), event bus, storage
  js/01-assets.js   loads the manifest, tints the bitmap font, bakes glow sprites
  js/02-render.js   camera, depth-sorted sprites, tiles, night lighting, bitmap text
  js/03-input.js    keyboard, mouse, gamepad and touch merged into one snapshot per step
  js/04-audio.js    synthesised sound effects and a step-sequencer for five songs
  js/10-data.js     weapons, enemies, waves, the saloon's stock, props (all tuning lives here)
  js/11-world.js    the town map, collision, flow-field pathfinding, spawn points, the rift
  js/12-run.js      the simulation: player, enemies and their AI, bullets, dynamite, bosses, shop
  js/13-game.js     state machine, fixed 60 Hz loop, sim events to sound and shake, world drawing
  js/14-ui.js       HUD, title, pause, the saloon, end screens
  js/99-main.js     boot
games/sundown/tools/
  build_assets.py   pack zips -> scaled sprite sheets, recoloured outlaws, tiles, font, manifest
  harness.js        loads the real game scripts into Node with a software canvas
  test.js           16 headless tests
  bot.js            balance report from an autoplayer
  shot.js           screenshots, demo GIF and MP4 through the real draw code
```

The simulation is deterministic: a fixed 60 Hz step, a seeded RNG for gameplay and a separate one
for cosmetics, and input arrives as one snapshot per step. The same code runs in the browser and in
Node, so the tests, the balance bot and the screenshots all exercise the game exactly as shipped.

The tools need only Node 18 or newer (no npm install):

```
cd games/sundown/tools
node test.js              # 16 tests
node bot.js 16 story 0.6  # balance report: 16 runs at skill 0.6
node shot.js --gif        # out/*.png, out/demo.gif, out/demo.mp4 (the video needs ffmpeg)
```

## Balance

Tuned with the autoplayer, which aims with lead and dodges what it can see, so it is better than
most people. At skill 0.6 it reaches dawn in 16 of 16 story runs; the Warden is the wall (about 80
damage taken on wave 10) and Reynolds is the mid-night spike. In Endless it lasts about 17 waves.
It needs a human playtest; the numbers to change are all in `js/10-data.js`.
