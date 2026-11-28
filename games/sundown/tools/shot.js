// Screenshots and a demo GIF, rendered headlessly through the real draw code.
//   node shot.js              -> out/*.png (title, street fight, saloon, midnight rift, boss)
//   node shot.js --gif        -> also out/demo.gif and out/demo.mp4 (needs ffmpeg on PATH)
'use strict';
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const H = require('./harness');
const { makeBrain } = require('./brain');

const OUT = path.join(__dirname, 'out');
const args = process.argv.slice(2);

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const { SD, canvas, haveAssets } = await H.create();
  if (!haveAssets) console.warn('no built assets: screenshots will show placeholder blocks');
  const G = SD.Game;
  const save = (name, scale) => { canvas.toPNG(path.join(OUT, name + '.png'), scale || 2); console.log('wrote out/' + name + '.png'); };
  const brain = makeBrain(SD, { seed: 7, skill: 1 });
  // a run the bot plays; the god flag keeps it alive so every phase can be shown
  const play = (secs, o) => {
    o = o || {};
    for (let i = 0; i < secs * 60; i++) {
      const R = G.run;
      if (G.state === 'shop') { if (o.stopAtShop) return true; brain.shop(R); G.state = 'play'; R.leaveShop(); }
      if (o.god) R.p.hp = Math.max(R.p.hp, 60);
      const inp = brain.think(R);
      G.stepFrame(SD.DT, inp);
      if (o.until && o.until(R)) return true;
    }
    return false;
  };
  G.init();
  SD.input.mouse.x = 400; SD.input.mouse.y = 200;
  G.t = 40; G.draw(); save('01-title');

  G.start('story'); G.run.seed = 7;
  play(14, { god: true, until: R => R.phase === 'wave' && R.enemies.filter(e => !e.dead).length >= 4 && R.bullets.length > 4 });
  SD.input.mode = 'kb'; G.draw(); save('02-street-fight');
  // jump through the early night to the saloon
  play(400, { god: true, stopAtShop: true });
  G.draw(); save('03-saloon');
  brain.shop(G.run); G.state = 'play'; G.run.leaveShop();
  // fast-forward: give the bot guns and skip to wave 6 (Jim Reynolds)
  const R = G.run, p = R.p; p.weapons.winchester = p.weapons.coach = true; p.up.hollow = 2; p.maxHp = 150;
  const skipTo = n => { R.enemies.length = 0; R.queue.length = 0; R.bullets.length = 0; R.bossPending = null; R.boss = null; R.wave = n - 1; R.nextWave(); };
  skipTo(6); play(9, { god: true, until: r => r.boss && r.phase === 'wave' && r.t > 0 && r.phaseT > 2.5 });
  G.draw(); save('04-reynolds');
  skipTo(7); play(5, { god: true, until: r => r.world.rift.on && r.world.rift.r > 500 });
  G.draw(); save('05-zero-hour');
  play(12, { god: true, until: r => r.phase === 'wave' && r.enemies.filter(e => !e.dead).length >= 5 });
  G.draw(); save('06-rift-fight');
  p.weapons.arc = p.weapons.rail = true; skipTo(10);
  play(12, { god: true, until: r => r.boss && r.phaseT > 4 && r.bullets.length > 20 });
  G.draw(); save('07-warden');

  if (args.includes('--gif')) {
    const frames = path.join(OUT, 'frames.rgb'); const fd = fs.openSync(frames, 'w'); let n = 0;
    G.start('story'); const b2 = makeBrain(SD, { seed: 3 }); SD.input.mode = 'pad'; // no mouse crosshair: the bot aims with a stick
    G.run.p.weapons.winchester = true;
    for (let i = 0; i < 60 * 14; i++) {
      const Rn = G.run; if (G.state === 'shop') { b2.shop(Rn); G.state = 'play'; Rn.leaveShop(); }
      Rn.p.hp = Math.max(Rn.p.hp, 50);
      G.stepFrame(SD.DT, b2.think(Rn));
      if (i % 3 === 0 && i > 60 * 2) { G.draw(); fs.writeSync(fd, canvas.toRGB()); n++; }
    }
    fs.closeSync(fd);
    const raw = ['-y', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', SD.W + 'x' + SD.H, '-r', '20', '-i', frames];
    // a small GIF for READMEs and a crisp MP4 for everything else
    const ff = spawnSync('ffmpeg', raw.concat(['-t', '9', '-vf', 'fps=12,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle', path.join(OUT, 'demo.gif')]), { stdio: 'inherit' });
    const mp4 = spawnSync('ffmpeg', raw.concat(['-vf', 'scale=1280:-2:flags=neighbor', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '20', '-preset', 'slow', path.join(OUT, 'demo.mp4')]), { stdio: 'inherit' });
    fs.unlinkSync(frames);
    console.log(ff.status === 0 && mp4.status === 0 ? 'wrote out/demo.gif and out/demo.mp4 (' + n + ' frames)' : 'ffmpeg failed');
  }
}
main().catch(e => { console.error(e); process.exit(1); });
