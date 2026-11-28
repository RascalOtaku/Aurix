// Balance report: the autoplayer plays many full story runs and prints how far it gets, where it
// dies, and how much damage each wave deals. Pure simulation, no drawing, so it runs fast.
//   node bot.js [runs=20] [mode=story] [skill=1]
'use strict';
const H = require('./harness');
const { makeBrain } = require('./brain');

async function main() {
  const runs = +(process.argv[2] || 20), mode = process.argv[3] || 'story', skill = process.argv[4] == null ? 1 : +process.argv[4];
  const { SD } = await H.create({ load: false });
  const waves = {}, deaths = {}, ends = [], stuck = [];
  const t0 = Date.now();
  for (let i = 0; i < runs; i++) {
    const R = new SD.Run({ mode, seed: 1000 + i * 7919 }), B = makeBrain(SD, { seed: i + 1, skill });
    let w = 0, hp0 = R.p.hp, steps = 0, ws = 0, counted = 0;
    const cap = 60 * 60 * (mode === 'endless' ? 40 : 25);
    while (steps++ < cap && !R.dead && !R.won) {
      if (R.phase === 'shop') { B.shop(R); R.leaveShop(); }
      if (R.wave !== w) { w = R.wave; hp0 = R.p.hp; ws = steps; }
      R.step(B.think(R));
      if (R.phase === 'clear' && counted !== w) {
        counted = w;
        const s = waves[w] = waves[w] || { n: 0, dmg: 0, secs: 0, money: 0 };
        s.n++; s.dmg += hp0 - R.p.hp; s.secs += (steps - ws) / 60; s.money += R.p.money;
      }
    }
    if (R.dead) deaths[R.wave] = (deaths[R.wave] || 0) + 1;
    else if (!R.won) { stuck.push({ seed: 1000 + i * 7919, wave: R.wave, phase: R.phase, left: R.queue.length, alive: R.enemies.filter(e => !e.dead).map(e => e.kind + '@' + (e.x | 0) + ',' + (e.y | 0)) }); }
    ends.push({ wave: R.wave, won: R.won, score: R.score, kills: R.kills, acc: R.shots ? R.hits / R.shots : 0 });
  }
  const won = ends.filter(e => e.won).length, avg = k => (ends.reduce((a, e) => a + e[k], 0) / ends.length);
  console.log(`${runs} ${mode} runs at skill ${skill} in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
  console.log(`reached dawn: ${won}/${runs}   average wave ${avg('wave').toFixed(1)}   average score ${avg('score').toFixed(0)}   accuracy ${(avg('acc') * 100).toFixed(0)}%`);
  if (stuck.length) console.log('timed out:', JSON.stringify(stuck));
  console.log('wave  cleared  avg-secs  avg-damage  avg-money-after  deaths-here');
  for (let w = 1; w <= Math.max(10, ...Object.keys(waves).map(Number)); w++) {
    const s = waves[w]; if (!s && !deaths[w]) continue;
    const n = s ? s.n : 0;
    console.log(String(w).padStart(4) + String(n).padStart(9) + (n ? (s.secs / n).toFixed(0) : '-').padStart(10) + (n ? (s.dmg / n).toFixed(0) : '-').padStart(12) + (n ? (s.money / n).toFixed(0) : '-').padStart(17) + String(deaths[w] || 0).padStart(13));
  }
}
main().catch(e => { console.error(e); process.exit(1); });
