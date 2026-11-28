// Headless tests for Zero Hour: Sundown. Runs the real game scripts in Node.
//   node test.js            (exit code 0 when everything passes)
'use strict';
const H = require('./harness');
const { makeBrain } = require('./brain');

const results = []; let failed = 0;
async function test(name, fn) {
  try { await fn(); results.push('  ok   ' + name); }
  catch (e) { failed++; results.push('  FAIL ' + name + '\n       ' + (e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n       ') : e)); }
}
const assert = (c, msg) => { if (!c) throw new Error(msg || 'assertion failed'); };
const eq = (a, b, msg) => { if (a !== b) throw new Error((msg || 'expected equal') + ': ' + JSON.stringify(a) + ' vs ' + JSON.stringify(b)); };

// play a run with the autoplayer for a number of sim seconds; returns the run
function autoplay(SD, R, secs, o) {
  o = o || {}; const B = makeBrain(SD, { seed: o.brainSeed || 1, skill: o.skill == null ? 1 : o.skill });
  for (let i = 0; i < secs * 60 && !R.dead && !R.won; i++) {
    if (R.phase === 'shop') { B.shop(R); R.leaveShop(); }
    if (o.god) R.p.hp = Math.max(R.p.hp, 50);
    R.step(B.think(R));
  }
  return R;
}
function stateHash(R) {
  const parts = [R.t.toFixed(4), R.p.x.toFixed(3), R.p.y.toFixed(3), R.p.hp, R.score, R.kills, R.wave, R.phase, R.enemies.length, R.bullets.length];
  for (const e of R.enemies) parts.push(e.kind, e.x.toFixed(2), e.y.toFixed(2), e.hp.toFixed(2));
  return parts.join('|');
}

(async () => {
  const env = await H.create();
  const { SD } = env;

  await test('scripts load and the manifest covers every sprite the game names', () => {
    assert(env.haveAssets, 'assets not built (run build_assets.py)');
    const need = new Set(['Prospector', 'ProspectorIdle', 'BartenderWipe', 'FloorSection', 'WallSection', 'BackBar', 'BarCounter', 'BarStool', 'Piano', 'CardTable', 'Chair', 'OverturnedChair', 'WhiskeyCrate', 'HangingLamp', 'KegStand', 'WallSconce', 'NeonBarSign']);
    for (const e of Object.values(SD.ENEMIES)) { need.add(e.sheet); if (!e.fly && !e.static) need.add(e.sheet + 'Idle'); }
    for (const k of Object.keys(SD.RIFT_SWAP)) { need.add(k); need.add(SD.RIFT_SWAP[k]); }
    const W = new SD.World(1); for (const p of W.props.concat(W.flats)) need.add(p.name);
    const missing = [...need].filter(n => !SD.assets.sheet(n) || !SD.assets.img[n]);
    eq(missing.length, 0, 'missing sheets ' + missing.join(','));
    eq(SD.assets.missing.length, 0, 'images failed to load');
  });

  await test('helpers: clock, 8-way facing, seeded rng', () => {
    eq(SD.fmtClock(0), '6:00 PM'); eq(SD.fmtClock(6 * 60), '12:00 AM'); eq(SD.fmtClock(12 * 60), '6:00 AM'); eq(SD.fmtClock(5 * 60 + 59), '11:59 PM');
    eq(SD.dir8(Math.PI / 2), 0, 'south is row 0'); eq(SD.dir8(Math.PI), 2, 'west is row 2'); eq(SD.dir8(-Math.PI / 2), 4, 'north is row 4'); eq(SD.dir8(0), 6, 'east is row 6');
    const a = SD.makeRng(42), b = SD.makeRng(42); for (let i = 0; i < 50; i++) eq(a(), b());
  });

  await test('every story wave and the first endless waves are well formed', () => {
    eq(SD.WAVES.length, 10);
    SD.WAVES.forEach((w, i) => { assert(w.cap > 0 && Object.keys(w.mix).every(k => SD.ENEMIES[k]), 'wave ' + (i + 1)); if (w.boss) assert(SD.ENEMIES[w.boss].boss, 'boss'); });
    eq(SD.WAVES[5].boss, 'reynolds'); eq(SD.WAVES[9].boss, 'warden'); assert(SD.WAVES[6].rift && !SD.WAVES[5].rift, 'rift opens at wave 7 (midnight)');
    SD.rng = SD.makeRng(3); for (let n = 11; n < 40; n++) { const w = SD.endlessWave(n); assert(Object.keys(w.mix).length && w.cap >= 10 && w.scale > 1); }
  });

  await test('a run is deterministic from its seed and inputs', () => {
    const a = autoplay(SD, new SD.Run({ mode: 'story', seed: 777 }), 50);
    const ha = stateHash(a);
    const b = autoplay(SD, new SD.Run({ mode: 'story', seed: 777 }), 50);
    eq(stateHash(b), ha, 'same seed, same inputs');
    const c = autoplay(SD, new SD.Run({ mode: 'story', seed: 778 }), 50);
    assert(stateHash(c) !== ha, 'different seed should differ');
  });

  await test('the autoplayer clears wave 1 without errors', () => {
    const R = autoplay(SD, new SD.Run({ mode: 'story', seed: 11 }), 60, { god: true });
    assert(R.wave >= 2 || R.phase === 'shop' || R.phase === 'clear', 'stuck on wave ' + R.wave + ' phase ' + R.phase);
    assert(R.kills >= 9, 'kills ' + R.kills);
    assert(R.p.money > 30, 'earned bounty');
  });

  await test('a full story run reaches dawn (god mode, all ten waves and both bosses)', () => {
    const R = new SD.Run({ mode: 'story', seed: 2024 }); let sawRift = false, sawReynolds = false, sawWarden = false;
    const B = makeBrain(SD, { seed: 5 });
    for (let i = 0; i < 60 * 60 * 20 && !R.won; i++) {
      if (R.phase === 'shop') { B.shop(R); R.leaveShop(); }
      R.p.hp = Math.max(R.p.hp, 50);
      R.step(B.think(R));
      if (R.world.rift.on) sawRift = true;
      if (R.boss && R.boss.kind === 'reynolds') sawReynolds = true;
      if (R.boss && R.boss.kind === 'warden') sawWarden = true;
    }
    assert(R.won && R.phase === 'victory', 'did not reach dawn: wave ' + R.wave + ' ' + R.phase);
    assert(sawRift && sawReynolds && sawWarden, 'rift ' + sawRift + ' reynolds ' + sawReynolds + ' warden ' + sawWarden);
  });

  await test('midnight: bells, the rift spreads, props turn into 2089', () => {
    const R = new SD.Run({ mode: 'story', seed: 9 });
    const bells = []; SD.bus.on('bell', n => bells.push(n));
    R.wave = 6; R.nextWave();
    eq(R.phase, 'rift');
    const before = R.world.props.filter(p => SD.RIFT_SWAP[p.name]).length;
    for (let i = 0; i < 60 * 9; i++) R.step(SD.blankInput());
    eq(bells.length, 12, 'twelve bells');
    assert(R.world.rift.on && R.world.rift.r >= R.world.rift.max, 'rift covers the map');
    const after = R.world.props.filter(p => SD.RIFT_SWAP[p.name]).length;
    assert(before > 20 && after === 0, 'props swapped: ' + before + ' -> ' + after);
    let rt = 0; for (let k = 0; k < R.world.tile.length; k++) if (R.world.riftTile[k]) rt++;
    eq(rt, R.world.tile.length, 'every tile is rift ground');
    eq(R.phase, 'intro'); SD.bus.clear(); SD.Game.wire();
  });

  await test('saloon: prices, reasons, upgrades and guns', () => {
    const R = new SD.Run({ mode: 'story', seed: 1 }), it = id => SD.SHOP.find(s => s.id === id);
    R.p.money = 1000;
    eq(R.canBuy(it('whiskey')), 'FULL HEALTH');
    eq(R.canBuy(it('arc')), 'AFTER MIDNIGHT');
    assert(R.buy(it('coach')) && R.p.weapons.coach && R.p.cur === 'coach', 'gun bought and equipped');
    eq(R.canBuy(it('coach')), 'SOLD OUT');
    const p0 = R.shopPrice(it('hollow')); R.buy(it('hollow')); assert(R.shopPrice(it('hollow')) > p0, 'upgrades get pricier');
    const mag = R.magSize('revolver'); R.buy(it('cylinder')); eq(R.magSize('revolver'), mag + 2);
    R.buy(it('duster')); eq(R.p.maxHp, 125); eq(R.p.hp, 125);
    R.p.money = 0; eq(R.canBuy(it('dynamite')), 'NOT ENOUGH'); assert(!R.buy(it('dynamite')));
  });

  await test('cover stops bullets; dynamite crates explode; rail rounds go through', () => {
    const R = new SD.Run({ mode: 'story', seed: 1 }), W = R.world;
    const crate = W.props.find(p => p.name === 'DynamiteCrate');
    R.bullets.push({ x: crate.x - 60, y: crate.y - 4, px: 0, py: 0, vx: 600, vy: 0, life: 1, owner: 'p', dmg: 40, hit: [] });
    let booms = 0; SD.bus.on('explode', () => booms++);
    for (let i = 0; i < 30; i++) R.updateBullets(SD.DT);
    assert(crate.dead && booms >= 1, 'crate exploded');
    const rock = W.props.find(p => p.name === 'RockSet');
    R.bullets.push({ x: rock.x - 60, y: rock.y - 4, px: 0, py: 0, vx: 600, vy: 0, life: 1, owner: 'p', dmg: 40, hit: [] });
    for (let i = 0; i < 30; i++) R.updateBullets(SD.DT);
    eq(R.bullets.length, 0, 'rock stopped the bullet'); assert(!rock.dead, 'rock does not break');
    R.bullets.push({ x: rock.x - 60, y: rock.y - 4, px: 0, py: 0, vx: 1800, vy: 0, life: 0.1, owner: 'p', dmg: 40, hit: [], rail: true, pierce: 99 });
    R.updateBullets(SD.DT); eq(R.bullets.length, 1, 'rail round passes cover');
    SD.bus.clear(); SD.Game.wire();
  });

  await test('dashing makes you untouchable for a moment', () => {
    const R = new SD.Run({ mode: 'story', seed: 1 }), inp = SD.blankInput();
    inp.dash = true; inp.mx = 1; R.phase = 'wave';
    R.step(inp);
    assert(R.p.inv > 0, 'invulnerable during dash');
    eq(R.hurtPlayer(50, R.p.x + 10, R.p.y), false);
    eq(R.p.hp, 100);
  });

  await test('no enemy gets stuck where the player cannot reach it', () => {
    const R = new SD.Run({ mode: 'story', seed: 3 }); R.world.nav.update(R.p.x, R.p.y);
    for (let i = 0; i < 400; i++) { const s = R.world.spawnPoint(SD.rng, R.p.x, R.p.y, i % 2 === 0); assert(R.world.reachable(s.x, s.y), 'unreachable spawn ' + s.x + ',' + s.y); }
  });

  await test('every screen draws without errors and puts real pixels on screen', () => {
    const G = SD.Game; G.init();
    const lit = () => { const d = env.canvas.data; let n = 0; for (let i = 0; i < d.length; i += 4 * 97) if (d[i] + d[i + 1] + d[i + 2] > 60) n++; return n; };
    G.t = 10; G.draw(); assert(lit() > 200, 'title');
    for (const s of ['controls', 'credits']) { SD.UI.screen = s; G.draw(); assert(lit() > 100, s); }
    SD.UI.screen = 'title';
    G.start('story'); autoplayGame(SD, G, 6); G.draw(); assert(lit() > 200, 'play');
    G.state = 'pause'; G.draw(); G.state = 'play';
    G.run.setPhase('shop'); eq(G.state, 'shop'); G.draw(); assert(lit() > 200, 'shop');
    G.finish(false); eq(G.state, 'over'); G.draw(); G.finish(true); eq(G.state, 'victory'); G.draw();
    G.start('daily'); eq(G.run.seed, SD.dailySeed().seed, 'daily seed'); G.draw();
  });

  await test('menus: keyboard confirm on the title starts a story run; pause and resume', () => {
    const G = SD.Game; G.toTitle(); SD.UI.sel = 0;
    const inp = SD.blankInput(); inp.confirm = true; G.stepFrame(SD.DT, inp); eq(G.state, 'play'); eq(G.mode, 'story');
    const p = SD.blankInput(); p.pause = true; G.stepFrame(SD.DT, p); eq(G.state, 'pause');
    G.stepFrame(SD.DT, p); eq(G.state, 'play');
  });

  await test('the saloon menu buys with confirm and leaves from the last row', () => {
    const G = SD.Game; G.start('story'); const R = G.run;
    R.p.money = 200; R.p.hp = 50; G.run.setPhase('shop'); SD.UI.shopSel = 0;
    const c = SD.blankInput(); c.confirm = true; G.stepFrame(SD.DT, c);
    assert(R.p.hp > 50 && R.p.money < 200, 'bought whiskey');
    const b = SD.blankInput(); b.back = true; G.stepFrame(SD.DT, b); G.stepFrame(SD.DT, c);
    eq(G.state, 'play'); eq(R.wave, 2);
  });

  await test('every sound and every song plays through a WebAudio graph without errors', () => {
    const { FakeAudioContext } = require('./fake-audio');
    env.sandbox.AudioContext = FakeAudioContext;
    const A = SD.audio; A.ctx = null; A.on = false; A.unlock();
    assert(A.on, 'audio unlocked');
    const src = require('fs').readFileSync(require('path').join(H.GAME, 'js/04-audio.js'), 'utf8');
    const names = [...src.matchAll(/case '([a-zA-Z]+)'/g)].map(m => m[1]);
    assert(names.length > 30, 'found sounds');
    for (const n of names) A.play(n, 1);
    // every event the game wires to a sound must name one that exists
    const used = [...require('fs').readFileSync(require('path').join(H.GAME, 'js/13-game.js'), 'utf8').matchAll(/A\.play\('([a-zA-Z]+)'/g)].map(m => m[1]);
    for (const u of used) assert(names.includes(u), 'unknown sound ' + u);
    for (const w of Object.values(SD.WEAPONS)) { assert(names.includes(w.sfx), 'weapon sound ' + w.sfx); if (w.after) assert(names.includes(w.after)); }
    for (const song of Object.keys(A.SONGS)) { A.music(song); for (let k = 0; k < 64; k++) { A.ctx.currentTime += 0.05; A.tick(); } }
    A.stopMusic(); A.on = false; A.ctx = null; delete env.sandbox.AudioContext;
  });

  await test('game over saves a best score', () => {
    const G = SD.Game; G.start('endless'); G.run.score = 4321; G.finish(false);
    eq(SD.store.get('best.endless', 0), 4321); assert(G.lastResult.newBest);
    G.start('endless'); G.run.score = 10; G.finish(false); eq(SD.store.get('best.endless', 0), 4321);
  });

  await test('wave 1 opens with its banner (the intro fires before the run is assigned)', () => {
    const G = SD.Game; G.start('story');
    assert(G.banner && /WAVE 1/.test(JSON.stringify(G.banner)), 'wave 1 banner shown');
  });

  await test('a press on a frame that runs no sim step still lands (displays faster than 60 Hz)', () => {
    const G = SD.Game; G.start('story'); const p = G.run.p; p.dashCd = 0; p.dashT = 0;
    const d = SD.blankInput(); d.dash = true; d.mx = 1;
    G.stepFrame(SD.DT * 0.4, d); G.stepFrame(SD.DT * 0.4, SD.blankInput()); G.stepFrame(SD.DT * 0.4, SD.blankInput());
    assert(p.dashT > 0, 'dash pressed between steps went through');
    eq(G.carry, null, 'nothing carried after the step');
  });

  await test('holding the touch aim stick keeps firing a semi-auto gun', () => {
    const shots = autoFire => {
      const G = SD.Game; G.start('story'); const R = G.run, p = R.p; p.cur = 'revolver'; const before = p.ammo.revolver;
      for (let i = 0; i < 60; i++) { const t = SD.blankInput(); t.fire = true; t.autoFire = autoFire; t.aimVec = { x: 1, y: 0 }; R.step(t); }
      return before - p.ammo.revolver;
    };
    eq(shots(false), 1, 'a held mouse button fires once');
    assert(shots(true) > 1, 'a held touch stick fires repeatedly');
  });

  await test('the saloon will not sell dynamite past the 9-stick cap', () => {
    const G = SD.Game; G.start('story'); const R = G.run, dyn = SD.SHOP.find(i => i.id === 'dynamite');
    R.p.dynamite = 9; R.p.money = 999; eq(R.canBuy(dyn), 'FULL');
    R.p.dynamite = 3; eq(R.canBuy(dyn), null);
  });

  await test('a mouse button already held when a menu opens is not a click', () => {
    const M = SD.input.mouse; M.down = true;
    SD.UI.openEnd(); eq(SD.UI.prevDown, true); SD.UI.openPause(); eq(SD.UI.prevDown, true);
    M.down = false;
  });

  console.log('Zero Hour: Sundown tests\n' + results.join('\n'));
  console.log(failed ? failed + ' failed' : 'all ' + results.length + ' passed');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });

function autoplayGame(SD, G, secs) {
  const B = makeBrain(SD, { seed: 2 });
  for (let i = 0; i < secs * 60; i++) { if (G.state === 'shop') { B.shop(G.run); G.state = 'play'; G.run.leaveShop(); } G.stepFrame(SD.DT, B.think(G.run)); }
}
