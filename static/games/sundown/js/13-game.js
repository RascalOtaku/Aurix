/* Game: the state machine around a run (title, play, pause, saloon, game over, dawn), the fixed-step
   loop, the wiring from sim events to sound / shake / banners, and drawing the town each frame. */
'use strict';

SD.Game = {
  state: 'boot', run: null, acc: 0, t: 0, frameN: 0,
  banner: null, later: [], pending: null, quiet: false,
  cx: 0, cy: 0, hurtV: 0, focusV: 0, riftV: 0, whiteout: 0,
  mode: 'story', daily: null, lastResult: null,
  _throttle: {},

  init() {
    SD.bus.clear();
    this.wire();
    this.toTitle();
  },

  /* ------------------------------------------------------------------ runs */
  start(mode) {
    this.mode = mode;
    let seed = (Date.now() & 0x7fffffff) >>> 0;
    if (mode === 'daily') { this.daily = SD.dailySeed(); seed = this.daily.seed; }
    this.quiet = false;
    this.banner = null; this.later = []; this.pending = null; this.acc = 0;
    this.run = new SD.Run({ mode, seed });
    const p = this.run.p; this.cx = p.x; this.cy = p.y;
    this.hurtV = 0; this.whiteout = 0; this.lastResult = null;
    this.state = 'play';
    SD.UI.onStart && SD.UI.onStart(this);
  },
  toTitle() {
    this.quiet = true;
    this.run = new SD.Run({ mode: 'story', seed: 1864 }); // a backdrop town; never stepped
    this.quiet = false;
    this.state = 'title'; this.banner = null; this.pending = null; this.later = [];
    this.cx = 400; this.cy = 420;
    SD.UI.openTitle && SD.UI.openTitle(this);
    if (SD.audio.ctx) SD.audio.music('title');
  },
  finish(won) {
    const R = this.run, key = this.mode === 'daily' ? 'daily.' + (this.daily ? this.daily.key : 0) : 'best.' + this.mode;
    const prev = SD.store.get(key, 0);
    const res = { won, score: R.score, wave: R.wave, kills: R.kills, bestCombo: R.bestCombo, bounty: R.stats.bounty,
      acc: R.shots ? Math.round(100 * R.hits / R.shots) : 0, time: R.t, best: Math.max(prev, R.score), newBest: R.score > prev, mode: this.mode, clock: R.clock };
    if (res.newBest) SD.store.set(key, R.score);
    const runs = SD.store.get('runs', 0); SD.store.set('runs', runs + 1);
    if (won && this.mode === 'story') SD.store.set('beatStory', true);
    this.lastResult = res;
    this.state = won ? 'victory' : 'over';
    SD.UI.openEnd && SD.UI.openEnd(this, res);
    SD.audio.music(won ? 'title' : null);
  },

  /* ------------------------------------------------------------------ loop */
  frame(realDt) {
    const dt = Math.min(0.1, Math.max(0, realDt));
    this.t += dt; this.frameN++;
    const inp = SD.input.sample();
    this.stepFrame(dt, inp);
    this.draw();
  },
  // separated so the harness can drive frames with synthetic input
  stepFrame(dt, inp) {
    const G = this;
    if (G.state === 'play') {
      if (inp.pause) { G.state = 'pause'; SD.UI.openPause(G); SD.audio.play('select'); }
      else {
        G.acc += dt; let first = true, n = 0;
        while (G.acc >= SD.DT && n < 6) { G.run.step(first ? inp : G.held(inp)); G.updateCamera(SD.DT); first = false; G.acc -= SD.DT; n++; }
        if (n >= 6) G.acc = 0;
      }
    } else if (G.state !== 'boot') {
      SD.UI.update(G, inp, dt);
      if (G.run && G.state !== 'shop') G.updateCamera(dt);
    }
    // timers
    for (const L of G.later) { L.t -= dt; if (L.t <= 0) { L.done = true; L.fn(); } }
    G.later = G.later.filter(L => !L.done);
    if (G.pending) { G.pending.t -= dt; if (G.pending.t <= 0) { const p = G.pending; G.pending = null; G.finish(p.won); } }
    if (G.banner) { G.banner.t += dt; if (G.banner.t > G.banner.dur) G.banner = null; }
    G.hurtV = Math.max(0, G.hurtV - dt * 1.6);
    G.whiteout = Math.max(0, G.whiteout - dt * 1.2);
    const R = G.run;
    G.focusV = SD.approach(G.focusV, R && R.p.focusing ? 1 : 0, dt * 5);
    if (SD.audio.tick) SD.audio.tick();
  },
  held(s) { // the same snapshot with one-shot presses removed, for extra sim steps in one frame
    const h = Object.assign({}, s);
    h.reload = h.dyn = h.dash = h.pause = h.confirm = h.back = h.up = h.down = h.left = h.right = h.any = h.interact = false;
    h.slot = 0; h.swap = 0; h.tap = null;
    return h;
  },
  after(t, fn) { this.later.push({ t, fn }); },
  say(title, sub, o) {
    o = o || {};
    this.banner = { title, sub: sub || '', t: 0, dur: o.dur || 2.4, color: o.color || 'y', subColor: o.subColor || 'w', big: o.big !== false };
  },
  throttle(key, gap) { const now = this.t; if ((this._throttle[key] || -1) > now) return false; this._throttle[key] = now + gap; return true; },

  /* ------------------------------------------------------------------ sim events -> sound, shake, banners */
  wire() {
    const G = this, A = SD.audio, on = (ev, fn) => SD.bus.on(ev, (a, b, c) => { if (!G.quiet) fn(a, b, c); });
    const shake = a => SD.R.shake(a);
    on('phase', (ph, R) => {
      if (R !== G.run) return;
      const d = R.waveDef;
      if (ph === 'intro') {
        const last = R.mode !== 'endless' && R.wave === 10;
        G.say(R.mode === 'endless' && R.wave > 10 ? 'WAVE ' + R.wave : (last ? 'LAST WAVE' : 'WAVE ' + R.wave), SD.fmtClock(d.hour * 60) + (R.isRift() ? '  //  2089 BLEEDS THROUGH' : '  //  SUNDOWN, COLORADO TERRITORY'), { color: R.isRift() ? 'm' : 'y' });
        A.play('wave'); A.music(R.isRift() ? 'rift' : 'frontier');
      } else if (ph === 'bossIntro') {
        const b = SD.ENEMIES[R.bossPending]; G.say(b.title, b.sub, { color: 'r', dur: 3.2 }); A.play('boss'); A.music('boss'); shake(6);
      } else if (ph === 'clear') {
        G.say(R.mode !== 'endless' && R.wave >= 10 ? 'DAWN' : 'WAVE CLEARED', R.mode !== 'endless' && R.wave >= 10 ? 'THE SUN COMES UP OVER SUNDOWN' : 'HEAD TO THE LAST CHANCE SALOON', { color: 'g', dur: 2.4 });
        A.play('clear');
      } else if (ph === 'shop') {
        G.state = 'shop'; SD.UI.openShop(G); A.music('saloon');
      } else if (ph === 'victory') {
        G.pending = { won: true, t: 2.2 }; G.whiteout = 1;
      } else if (ph === 'dead') {
        G.pending = { won: false, t: 2.6 }; A.stopMusic();
        G.say('SHOT DEAD', SD.fmtClock(R.clock) + '  //  WAVE ' + R.wave, { color: 'r', dur: 3 });
      } else if (ph === 'rift') {
        A.music(null); A.stopMusic();
      }
    });
    on('bell', n => { A.play('bell'); if (n === 1) G.say('MIDNIGHT', 'THE CHURCH BELL RINGS TWELVE', { color: 'w', dur: 3.4 }); });
    on('riftOpen', () => { A.play('rift'); shake(14); G.whiteout = 0.8; G.riftV = 1; G.say('ZERO HOUR', 'SOMETHING OPENS IN THE MINE', { color: 'm', subColor: 'c', dur: 3.6 }); SD.input.rumble(1, 600); });
    on('riftSwap', () => { if (G.throttle('swap', 0.09)) A.play('glitch', 0.35); });
    on('shot', (k, w) => {
      A.play(w.sfx, k === 'arc' ? 0.55 : 1); shake(w.kick * 0.55);
      if (w.after) G.after(0.22, () => A.play(w.after, 0.6));
      if (k !== 'arc') SD.input.rumble(Math.min(1, w.kick / 7), 60);
    });
    on('fan', () => A.play('fan'));
    on('reload', () => A.play('reload'));
    on('reloaded', () => A.play('click', 0.7));
    on('empty', () => A.play('empty'));
    on('swap', () => A.play('select', 0.7));
    on('dash', () => A.play('dash'));
    on('hurt', dmg => { A.play('hurt'); shake(4 + dmg * 0.25); G.hurtV = Math.min(1, G.hurtV + 0.35 + dmg / 40); SD.input.rumble(0.8, 140); });
    on('playerDie', () => { A.play('die'); shake(12); SD.input.rumble(1, 400); });
    on('hitEnemy', (e, b) => {
      if (!G.throttle('hit', 0.03)) return;
      A.play(b && b.crit ? 'crit' : (e.d.fly || e.d.static ? 'metal' : 'hit'), 0.55);
    });
    on('kill', e => {
      A.play(e.d.fly || e.d.static ? 'explode' : 'die', e.boss ? 1 : 0.5);
      if (e.boss) { shake(14); G.whiteout = 0.7; G.say(e.d.title + ' IS DOWN', '+' + e.d.bounty + ' BOUNTY', { color: 'y', dur: 2.6 }); A.music(G.run && G.run.isRift() ? 'rift' : 'frontier'); }
      G.gore(e);
    });
    on('enemyShot', () => { if (G.throttle('es', 0.04)) A.play('enemyShot', 0.45); });
    on('enemyShotgun', () => A.play('shotgun', 0.55));
    on('droneShot', () => { if (G.throttle('ds', 0.06)) A.play('droneShot', 0.4); });
    on('swing', () => A.play('dash', 0.4));
    on('teleport', () => A.play('teleport', 0.5));
    on('bossPhase', (e, ph) => { A.play('boss', 0.8); shake(8); G.say(e.d.title, ph === 2 ? 'IS GETTING ANGRY' : 'HAS NOTHING LEFT TO LOSE', { color: 'r', dur: 2 }); });
    on('bossYell', () => { if (G.throttle('yell', 1)) A.play('boss', 0.5); });
    on('lanceWarn', () => A.play('focusOn', 0.6));
    on('lance', () => { A.play('rail', 0.9); shake(9); });
    on('hitProp', (p, mat) => { if (G.throttle('prop', 0.05)) A.play(mat === 'metal' ? 'ricochet' : mat === 'rock' ? 'ricochet' : 'wood', 0.4); });
    on('breakProp', p => { A.play(p.def.mat === 'metal' ? 'metal' : 'wood', 0.9); shake(3); });
    on('throw', o => A.play(o === 'p' ? 'throw' : 'fuse', 0.6));
    on('explode', (x, y, r, small) => { A.play('explode', small ? 0.5 : 1); const pl = G.run && G.run.p, d = pl ? SD.dist(x, y, pl.x, pl.y) : 0; shake(Math.max(2, (small ? 5 : 13) * (1 - Math.min(1, d / 500)))); SD.input.rumble(small ? 0.4 : 0.9, 200); });
    on('pickup', k => A.play(k.kind === 'coin' ? 'coin' : k.kind === 'health' ? 'whiskey' : 'pickup', k.kind === 'coin' ? 0.35 : 0.8));
    on('buy', () => A.play('buy'));
    on('deny', () => A.play('deny'));
    on('focusOn', () => A.play('focusOn'));
    on('focusOff', () => A.play('focusOff'));
    on('bossSpawn', () => shake(8));
  },
  gore(e) { // cosmetic only: SD.fx, never SD.rng
    const R = this.run; if (!R) return;
    const metal = e.d.fly || e.d.static, rift = e.d.rift;
    for (let i = 0; i < (e.boss ? 40 : 12); i++) {
      const a = (e.deathAng || 0) + SD.fx.range(-0.9, 0.9), s = SD.fx.range(40, 170);
      R.parts.push({ x: e.x, y: e.y, z: 18 + (e.d.lift || 0), vx: Math.cos(a) * s, vy: Math.sin(a) * s * 0.6, vz: SD.fx.range(20, 140), t: SD.fx.range(0.4, 1.0),
        col: metal ? SD.fx.pick(['#5a5f72', '#3ff0ff', '#ff3ec8']) : rift ? SD.fx.pick(['#3ff0ff', '#ff3ec8', '#7a2a8a']) : SD.fx.pick(['#7a1a14', '#a8241a', '#5a120e']), w: 2, h: 2, g: 380, floor: e.y + SD.fx.range(-4, 4), glow: metal || rift });
    }
    if (!metal) R.decals.push({ x: e.x + Math.cos(e.deathAng || 0) * 8, y: e.y, r: e.boss ? 16 : 7, t: 40, blood: !rift, rift });
    if (R.decals.length > 60) R.decals.shift();
  },

  /* ------------------------------------------------------------------ drawing */
  draw() {
    const G = this, Rr = SD.R;
    if (G.state === 'boot') return;
    if (G.state === 'shop') { SD.UI.drawShop(G); SD.UI.drawOverlay(G); return; }
    if (G.run) G.drawWorld(G.run);
    if (G.state === 'play' || G.state === 'pause') SD.UI.hud(G, G.run);
    if (G.state === 'title') SD.UI.drawTitle(G);
    else if (G.state === 'pause') SD.UI.drawPause(G);
    else if (G.state === 'over' || G.state === 'victory') SD.UI.drawEnd(G);
    SD.UI.drawOverlay(G);
    void Rr;
  },
  updateCamera(dt) { // follow the player, lead toward the aim, lean toward a boss
    const G = this, R = G.run, p = R.p, t = G.t;
    let tx = p.x + Math.cos(p.aim) * 48, ty = p.y - 18 + Math.sin(p.aim) * 30;
    if (G.state === 'title') { tx = 560 + Math.sin(t * 0.07) * 380; ty = 470 + Math.sin(t * 0.05) * 160; }
    if (R.boss && R.phase === 'wave') { tx = SD.lerp(tx, R.boss.x, 0.18); ty = SD.lerp(ty, R.boss.y, 0.18); }
    const k = G.state === 'title' ? 1 : 1 - Math.pow(0.0008, dt);
    G.cx = SD.lerp(G.cx, tx, k); G.cy = SD.lerp(G.cy, ty, k);
  },
  /* how dark it is: dusk at 6 PM to full night by ten; after midnight the rift lights the sky */
  nightLevel(R) {
    const h = R.clock / 60;
    if (R.isRift()) return 0.66;
    return SD.clamp(0.12 + h * 0.14, 0.12, 0.72);
  },
  drawWorld(R) {
    const G = this, Rr = SD.R, W = R.world, p = R.p, t = G.t;
    Rr.setCamera(G.cx, G.cy, W.w, W.h);

    const rift = W.rift.on;
    Rr.clear(rift ? '#100818' : '#1a120c');
    Rr.tiles(W);
    const g = Rr.g, ox = Rr.ox(), oy = Rr.oy();

    // decals: scorch marks and blood soak into the ground
    for (const d of R.decals) {
      if (!Rr.onScreen(d.x, d.y, 40)) continue;
      const a = Math.min(1, d.t / 6);
      if (d.blood) { Rr.circle(d.x, d.y, d.r, '#4a0e0a', 0.55 * a); Rr.circle(d.x + 3, d.y + 1, d.r * 0.6, '#5e120c', 0.5 * a); }
      else if (d.rift) { Rr.circle(d.x, d.y, d.r, '#ff3ec8', 0.18 * a); }
      else { g.globalAlpha = 0.55 * a; g.fillStyle = '#16100a'; g.beginPath(); g.ellipse(d.x + ox, d.y + oy, d.r, d.r * 0.55, 0, 0, Math.PI * 2); g.fill(); g.globalAlpha = 1; }
    }
    // flat props (boardwalks) are part of the floor
    for (const f of W.flats) if (Rr.onScreen(f.x, f.y, 60)) Rr.drawSprite(f.name, f.row, 0, f.x, f.y, {});

    // depth-sorted world
    for (const pr of W.props) {
      if (pr.dead || !Rr.onScreen(pr.x, pr.y, 140)) continue;
      const s = SD.assets.sheet(pr.name);
      const col = s && s.cols > 1 ? Math.floor(t * (pr.name === 'Campfire' ? 10 : 6) + pr.id) % s.cols : 0;
      const o = { flash: pr.flash > 0 ? 0.8 : 0 };
      if (pr.glitch > 0) o.tint = [0, 0.7 * pr.glitch / 0.6, (SD.fx() * 8 - 4) | 0];
      Rr.sprite(pr.name, pr.row, col, pr.x, pr.y, o);
      if (pr.maxHp && pr.hp < pr.maxHp && pr.hp > 0 && pr.def.explode) { const x = pr.x, y = pr.y; Rr.push(pr.y + 1, () => { if (((t * 8) | 0) % 2) Rr.glow('red', x, y - 14, 0.4, 0.7); }); }
    }
    for (const e of R.enemies) G.queueEnemy(R, e);
    G.queuePlayer(R);
    for (const k of R.pickups) {
      const x = k.x, y = k.y, bob = Math.sin((k.t + x) * 6) * 2, blink = k.life < 3 && ((k.life * 8) | 0) % 2;
      if (blink) continue;
      if (k.kind === 'coin') Rr.push(y, () => { Rr.shadowAt(x, y, 3); Rr.circle(x, y - 5 + bob, 3.2, '#7a5418'); Rr.circle(x, y - 5.5 + bob, 2.4, '#ffd166'); Rr.rect(x - 0.5 + ox, y - 7 + bob + oy, 1, 2, '#fff6c8'); });
      else Rr.sprite(k.kind === 'health' ? 'WhiskeyCrate' : 'DynamiteCrate', 0, 0, x, y, { scale: 0.42, lift: -bob + 2, shadow: 8 });
      if (k.kind !== 'coin') Rr.push(y + 1, () => Rr.glow(k.kind === 'health' ? 'warm' : 'fire', x, y - 8, 0.3, 0.5 + 0.2 * Math.sin(t * 6)));
    }
    for (const b of R.bombs) {
      const x = b.x, y = b.y, z = b.z;
      Rr.push(y, () => {
        Rr.shadowAt(x, y, 4);
        const sx = Math.round(x + ox), sy = Math.round(y - z - 4 + oy), c = Math.cos(b.spin), s = Math.sin(b.spin);
        g.fillStyle = '#b8261a'; g.strokeStyle = '#b8261a'; g.lineWidth = 3; g.beginPath(); g.moveTo(sx - c * 4, sy - s * 4); g.lineTo(sx + c * 4, sy + s * 4); g.stroke();
        Rr.glow('muzzle', x + c * 5, y - z - 4 + s * 5, 0.5 + SD.fx() * 0.3, 1);
        if (b.fuse < 0.5 && ((t * 20) | 0) % 2) Rr.glow('red', x, y - z, 0.6, 0.8);
      });
    }
    Rr.flush();

    // particles
    for (const q of R.parts) {
      if (!Rr.onScreen(q.x, q.y, 20)) continue;
      const a = Math.min(1, q.t * 3);
      if (q.smoke) { Rr.circle(q.x, q.y - q.z, q.w / 2, q.col, 0.35 * a); continue; }
      g.globalAlpha = a; g.fillStyle = q.col; g.fillRect(Math.round(q.x - q.w / 2 + ox), Math.round(q.y - q.z - q.h / 2 + oy), q.w, q.h); g.globalAlpha = 1;
      if (q.glow && q.z > 1) Rr.glow('muzzle', q.x, q.y - q.z, 0.25, 0.5 * a);
    }
    // bullets
    for (const b of R.bullets) {
      const lift = b.owner === 'p' ? 20 : (b.lift || 20);
      const x1 = b.x, y1 = b.y - lift, tl = b.owner === 'p' ? 0.022 : 0.03;
      if (!Rr.onScreen(x1, y1, 30)) continue;
      if (b.big) { Rr.glow(b.col === '#3ff0ff' ? 'cyan' : 'pink', x1, y1, 0.3, 0.8); Rr.circle(x1, y1, 2.6, b.col); Rr.circle(x1, y1, 1.2, '#ffffff'); continue; }
      Rr.line(x1 - b.vx * tl, y1 - b.vy * tl, x1, y1, b.col, b.owner === 'p' ? (b.crit ? 3 : 2) : 2, 0.95);
      if (b.owner === 'e') Rr.circle(x1, y1, 1.6, '#fff2d0');
    }
    // beams: rail shots, arc chains, the Warden's lance
    for (const bm of R.beams) {
      const a = Math.min(1, bm.t * 6);
      if (bm.zig) {
        let px = bm.x0, py = bm.y0; const n = 5;
        for (let i = 1; i <= n; i++) { const qx = SD.lerp(bm.x0, bm.x1, i / n) + (i < n ? SD.fx.range(-6, 6) : 0), qy = SD.lerp(bm.y0, bm.y1, i / n) + (i < n ? SD.fx.range(-6, 6) : 0); Rr.line(px, py, qx, qy, bm.col, 2, a); px = qx; py = qy; }
      } else {
        const off = bm.fat ? 0 : 20;
        Rr.line(bm.x0, bm.y0 - off, bm.x1, bm.y1 - off, bm.col, bm.fat ? 9 * a : 5 * a, 0.5 * a);
        Rr.line(bm.x0, bm.y0 - off, bm.x1, bm.y1 - off, '#ffffff', bm.fat ? 3 : 1.5, a);
      }
    }
    // Warden lance warning: a thin flickering sight line before it fires
    for (const e of R.enemies) if (e.lance > 0 && !e.lanceFired && ((t * 24) | 0) % 2) Rr.line(e.x, e.y - 30, e.x + Math.cos(e.lanceA) * 900, e.y - 30 + Math.sin(e.lanceA) * 900, '#ff3ec8', 1, 0.8);

    // night
    const night = G.nightLevel(R);
    const lights = [];
    lights.push({ x: p.x, y: p.y - 14, r: 120, a: 0.9 });
    for (const L of W.lights) {
      const lx = L.prop ? L.prop.x : L.x, ly = L.prop ? L.prop.y - 26 : L.y;
      if (L.prop && L.prop.dead) continue;
      const fl = L.flicker || L.glow === 'fire' ? 0.85 + 0.15 * Math.sin(t * 13 + lx) * Math.sin(t * 7.3 + ly) : 1;
      lights.push({ x: lx, y: ly, r: (L.r || (L.glow === 'fire' ? 100 : 70)) * fl, a: 1 });
    }
    for (const f of R.flashes) lights.push({ x: f.x, y: f.y, r: 40 * f.s, a: 1 });
    for (const b of R.bombs) lights.push({ x: b.x, y: b.y - b.z, r: 26, a: 0.8 });
    for (const bm of R.beams) lights.push({ x: (bm.x0 + bm.x1) / 2, y: (bm.y0 + bm.y1) / 2, r: 30, a: 0.4 });
    for (const e of R.enemies) if (!e.dead && e.d.rift) lights.push({ x: e.x, y: e.y - 20 - (e.d.lift || 0), r: e.boss ? 160 : 40, a: 0.6 });
    if (R.clock < 90 && !rift) Rr.rect(0, 0, SD.W, SD.H, '#ff6a2a', 0.1 * (1 - R.clock / 90)); // last of the sunset
    Rr.darkness(rift ? '#12051f' : '#080a1c', night, lights);
    // light sources glow on top of the dark
    for (const L of W.lights) {
      if (L.prop && L.prop.dead) continue;
      const lx = L.prop ? L.prop.x : L.x, ly = L.prop ? L.prop.y - 26 : L.y;
      if (!Rr.onScreen(lx, ly, 80)) continue;
      Rr.glow(L.glow === 'fire' ? 'fire' : L.glow || 'warm', lx, ly, L.glow === 'fire' ? 1.6 : 0.8, 0.35 * night + 0.1);
    }
    for (const f of R.flashes) Rr.glow(f.glow, f.x, f.y, f.s, Math.min(1, f.t * 14));
    for (const e of R.enemies) {
      if (e.dead) continue;
      if (e.warn > 0) Rr.glow('red', e.x, e.y - 24 - (e.d.lift || 0), e.boss ? 0.8 : 0.35, 0.9);
      if (e.d.rift) Rr.glow(e.d.ai === 'glitch' ? 'cyan' : 'pink', e.x, e.y - 18 - (e.d.lift || 0), e.boss ? 2.4 : 0.5, 0.25);
    }
    // damage numbers
    for (const f of R.floaters) {
      const sx = f.x - Rr.cam.x, sy = f.y - Rr.cam.y;
      if (sx < -20 || sx > SD.W + 20 || sy < -20 || sy > SD.H + 20) continue;
      Rr.text(f.crit ? f.v + '!' : f.v, sx, sy, { align: 'center', color: f.crit ? 'y' : 'w', alpha: Math.min(1, f.t * 3) });
    }
    // the rift wavefront: scanline bands while it spreads
    if (rift && W.rift.r < W.rift.max) {
      const k2 = 1 - W.rift.r / W.rift.max;
      for (let i = 0; i < 6; i++) { const y = SD.fx.int(0, SD.H - 6); Rr.rect(0, y, SD.W, SD.fx.int(1, 5), SD.fx() < 0.5 ? '#ff3ec8' : '#3ff0ff', 0.18 * k2); }
    }
    if (rift) G.riftV = Math.max(0.12, G.riftV - SD.DT * 0.4);
    if (rift && SD.fx() < 0.02 * G.riftV * 6) { const y = SD.fx.int(0, SD.H); Rr.rect(0, y, SD.W, 1, '#3ff0ff', 0.3); }
  },
  queueEnemy(R, e) {
    const Rr = SD.R, d = e.d, t = this.t;
    if (!Rr.onScreen(e.x, e.y, 120)) return;
    const o = { shadow: d.fly ? 0 : e.r * 0.9, scale: d.scale || 1, lift: d.lift ? d.lift + Math.sin(t * 3 + e.id) * 3 : 0 };
    if (e.flash > 0) o.flash = 1;
    if (d.tint) o.tint = [0, 0.18, 0];
    if (e.spawnT > 0) { o.alpha = SD.clamp(1 - e.spawnT / 0.6, 0, 1); if (d.rift) o.tint = [0, 0.8, (SD.fx() * 10 - 5) | 0]; }
    if (e.dead) { o.alpha = SD.clamp(1 - e.deadT / 0.9, 0, 1); o.flash = e.deadT < 0.08 ? 1 : 0; if (d.fly) return; }
    if (d.ai === 'glitch' && SD.fx() < 0.06) o.tint = [0, 0.7, (SD.fx() * 12 - 6) | 0];
    let name = d.sheet, col = 0;
    const s = SD.assets.sheet(name);
    if (d.fly || d.static) col = Math.floor(t * 10 + e.id) % ((s && s.cols) || 1);
    else if (!e.dead && Math.hypot(e.vx, e.vy) > 8) col = Math.floor(e.walk) % 8;
    else { const idle = SD.assets.sheet(name + 'Idle'); if (idle) { name = name + 'Idle'; col = Math.floor(t * 5 + e.id) % idle.cols; } }
    if (d.fly && o.lift) Rr.push(e.y - 1, () => Rr.shadowAt(e.x, e.y, e.r * 0.8));
    Rr.sprite(name, e.dir, col, e.x, e.y, o);
  },
  queuePlayer(R) {
    const Rr = SD.R, p = R.p, t = this.t;
    if (this.state === 'title') return;
    const moving = p.moving && !R.dead;
    let name = moving ? 'Prospector' : 'ProspectorIdle', col = moving ? Math.floor(p.walk) % 8 : Math.floor(t * 5) % 4;
    const o = { shadow: 9 };
    if (p.hurtFlash > 0) o.flash = 1;
    if (p.inv > 0 && p.dashT <= 0 && ((t * 20) | 0) % 2) o.alpha = 0.5;
    if (R.dead) { o.alpha = 0.85; o.flash = 0; name = 'ProspectorIdle'; col = 0; }
    if (p.dashT > 0) for (let i = 1; i <= 3; i++) Rr.sprite(name, p.dir, col, p.x - p.dvx * 0.018 * i, p.y - p.dvy * 0.018 * i, { alpha: 0.35 - i * 0.09, sortBias: -1 });
    Rr.sprite(name, p.dir, col, p.x, p.y, o);
    // a little laser-sight dot for the long guns after midnight
    if (R.isRift() && (p.cur === 'rail' || p.cur === 'arc')) { const x = p.x + Math.cos(p.aim) * 60, y = p.y - 20 + Math.sin(p.aim) * 60; Rr.push(p.y + 2, () => Rr.circle(x, y, 1, p.cur === 'rail' ? '#ff3ec8' : '#3ff0ff', 0.8)); }
  },
};
