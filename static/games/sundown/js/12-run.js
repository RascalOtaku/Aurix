/* The run: player, enemies, bullets, dynamite, pickups, waves, the clock to midnight, bosses.
   Pure simulation, stepped at a fixed 60 Hz by SD.Game. It reads one input snapshot per step and
   tells the rest of the game what happened through SD.bus (sounds, shakes, banners). */
'use strict';

SD.Run = function (opts) {
  const R = this;
  R.mode = opts.mode || 'story';            // story | endless | daily
  R.seed = opts.seed >>> 0 || 1864;
  SD.rng = SD.makeRng(R.seed);
  R.world = new SD.World(R.seed);
  R.t = 0; R.timeScale = 1; R.hitstop = 0; R.slow = 0;
  R.phase = 'intro'; R.phaseT = 0;
  R.wave = 0; R.waveDef = null; R.queue = []; R.spawnT = 0; R.waveKills = 0; R.waveTotal = 1;
  R.clock = 0;                              // minutes after 6 PM
  R.enemies = []; R.bullets = []; R.bombs = []; R.pickups = []; R.parts = []; R.floaters = []; R.decals = []; R.beams = []; R.flashes = [];
  R.score = 0; R.combo = 0; R.comboT = 0; R.kills = 0; R.shots = 0; R.hits = 0; R.bestCombo = 0; R.dmgTaken = 0;
  R.boss = null; R.banner = null; R.dead = false; R.won = false;
  R.stats = { waves: 0, bounty: 0, crits: 0, dynamite: 0, rift: false };
  const W = R.world;
  R.p = {
    x: 790, y: W.street.y0 + 40, r: 8, vx: 0, vy: 0, hp: 100, maxHp: 100, speed: 118,
    aim: -Math.PI / 2, dir: 0, walk: 0, moving: false,
    weapons: { revolver: true }, cur: 'revolver', ammo: {}, reloadT: 0, fireT: 0, fanQ: 0, fanT: 0,
    dashT: 0, dashCd: 0, dvx: 0, dvy: 0, inv: 0, hurtFlash: 0,
    dynamite: 2, focus: 60, focusing: false, money: 30,
    up: { hollow: 0, cylinder: 0, quick: 0, duster: 0, boots: 0, lucky: 0, deadeye: 0 },
  };
  for (const k of SD.WEAPON_ORDER) R.p.ammo[k] = R.magSize(k);
  R.nextWave();
};

SD.Run.prototype = {
  emit(ev, a, b, c) { SD.bus.emit(ev, a, b, c); },
  magSize(k) { return SD.WEAPONS[k].mag + (k === 'revolver' || k === 'winchester' || k === 'arc' ? this.p.up.cylinder * 2 : (k === 'coach' ? 0 : this.p.up.cylinder)); },
  isRift() { return this.world.rift.on; },

  /* ===================================================================== waves */
  nextWave() {
    const R = this;
    R.wave++;
    const story = R.mode !== 'endless' ? SD.WAVES[R.wave - 1] : null;
    R.waveDef = story || (R.mode === 'endless' && R.wave <= 10 ? SD.WAVES[R.wave - 1] : SD.endlessWave(R.wave));
    if (R.mode === 'endless' && R.wave <= 10) R.waveDef = SD.WAVES[R.wave - 1];
    const d = R.waveDef;
    R.queue = [];
    for (const [k, n] of Object.entries(d.mix)) for (let i = 0; i < n; i++) R.queue.push(k);
    for (let i = R.queue.length - 1; i > 0; i--) { const j = Math.floor(SD.rng() * (i + 1)); [R.queue[i], R.queue[j]] = [R.queue[j], R.queue[i]]; }
    // turrets come early so the field changes shape at the start of a rift wave
    R.queue.sort((a, b) => (a === 'turret' ? -1 : 0) - (b === 'turret' ? -1 : 0));
    R.waveTotal = R.queue.length + (d.boss ? 1 : 0); R.waveKills = 0;
    R.clock = d.hour * 60; R.spawnT = 1.5; R.bossPending = d.boss || null;
    if (d.rift && !R.world.rift.on) { R.setPhase('rift'); return; }
    R.setPhase('intro');
  },
  setPhase(ph) {
    this.phase = ph; this.phaseT = 0;
    this.emit('phase', ph, this);
  },
  waveScale() { return (this.waveDef && this.waveDef.scale) || 1; },

  /* ===================================================================== step */
  step(inp) {
    const R = this, dt0 = SD.DT;
    R.phaseT += dt0;
    if (R.hitstop > 0) { R.hitstop -= dt0; R.updateCosmetic(dt0 * 0.2); return; }
    // focus slows the world; the player is slowed a little less
    const p = R.p;
    if (R.phase === 'wave' || R.phase === 'boss') {
      const want = inp.focus && p.focus > 1 && !R.dead;
      if (want !== p.focusing) { p.focusing = want; R.emit(want ? 'focusOn' : 'focusOff'); }
      if (p.focusing) p.focus = Math.max(0, p.focus - dt0 * (30 - p.up.deadeye * 6));
    } else if (p.focusing) { p.focusing = false; R.emit('focusOff'); }
    const target = R.dead ? 0.25 : p.focusing ? 0.35 : 1;
    R.timeScale = SD.approach(R.timeScale, target, dt0 * 4);
    const dt = dt0 * R.timeScale, pdt = dt0 * (p.focusing ? 0.75 : R.timeScale);
    R.t += dt;

    switch (R.phase) {
      case 'intro': if (R.phaseT > 2.4) R.setPhase(R.bossPending ? 'bossIntro' : 'wave'); break;
      case 'rift': R.updateRiftPhase(dt0); break;
      case 'bossIntro': if (R.phaseT > 3.2) { R.spawnBoss(R.bossPending); R.bossPending = null; R.setPhase('wave'); } break;
      case 'wave': R.updateSpawns(dt); R.checkClear(); break;
      case 'clear': if (R.phaseT > 2.6) { if (R.mode !== 'endless' && R.wave >= 10) { R.won = true; R.setPhase('victory'); } else R.setPhase('shop'); } break;
      case 'shop': return;            // the saloon screen drives this phase
      case 'victory': case 'dead': break;
    }
    if (R.phase !== 'shop') {
      if (!R.dead) R.updatePlayer(inp, pdt);
      R.updateEnemies(dt);
      R.updateBullets(dt);
      R.updateBombs(dt);
      R.updatePickups(dt);
      R.world.updateRift(dt0, (ev, x) => R.emit(ev, x));
      if (R.world.nav.dirty || ((R.t * 5) | 0) !== R._navTick) { R._navTick = (R.t * 5) | 0; R.world.nav.update(p.x, p.y); }
    }
    R.updateCosmetic(dt);
    if (R.comboT > 0) { R.comboT -= dt; if (R.comboT <= 0) R.combo = 0; }
    if (R.dead && R.phase !== 'dead' && R.phaseT > 0) R.setPhase('dead');
  },

  updateRiftPhase(dt) {
    const R = this, t = R.phaseT;
    if (!R._bells) R._bells = 0;
    // twelve bells, fast, then the rift tears open from the mine
    if (R._bells < 12 && t > 0.3 + R._bells * 0.28) { R._bells++; R.emit('bell', R._bells); R.clock = 5 * 60 + 59; }
    if (t > 3.8 && !R.world.rift.on) { R.world.startRift(); R.stats.rift = true; R.clock = 6 * 60; R.emit('riftOpen'); }
    if (t > 8.2) { R._bells = 0; R.setPhase('intro'); }
  },
  updateSpawns(dt) {
    const R = this, d = R.waveDef;
    const alive = R.enemies.filter(e => !e.dead && !e.boss).length;
    R.spawnT -= dt;
    if (R.queue.length && R.spawnT <= 0 && alive < d.cap) {
      const kind = R.queue.shift();
      const sp = R.world.spawnPoint(SD.rng, R.p.x, R.p.y, R.isRift());
      R.spawnEnemy(kind, sp.x, sp.y);
      R.spawnT = Math.max(0.35, 1.25 - R.wave * 0.07) * (SD.rng.range(0.7, 1.3));
    }
    // the clock follows the wave's progress
    R.clock = d.hour * 60 + 55 * (R.waveKills / Math.max(1, R.waveTotal));
  },
  checkClear() {
    const R = this;
    if (R.queue.length || R.bossPending) return;
    if (R.enemies.some(e => !e.dead)) return;
    R.stats.waves = R.wave;
    R.emit('waveClear', R.wave);
    R.setPhase('clear');
  },
  leaveShop() { this.nextWave(); },

  /* ===================================================================== player */
  updatePlayer(inp, dt) {
    const R = this, p = R.p, W = R.world;
    // aim
    if (inp.aimScreen) { const wx = inp.aimScreen.x + SD.R.cam.x, wy = inp.aimScreen.y + SD.R.cam.y; p.aim = SD.ang(p.x, p.y - 20, wx, wy); }
    else if (inp.aimVec) p.aim = Math.atan2(inp.aimVec.y, inp.aimVec.x);
    else if (inp.aimAngle != null) p.aim = inp.aimAngle;
    else if (Math.hypot(inp.mx, inp.my) > 0.2 && !inp.fire) p.aim = Math.atan2(inp.my, inp.mx);
    // weapon swap
    if (inp.slot) { const k = SD.WEAPON_ORDER[inp.slot - 1]; if (k && p.weapons[k]) R.switchTo(k); }
    if (inp.swap) { const own = SD.WEAPON_ORDER.filter(k => p.weapons[k]); const i = own.indexOf(p.cur); R.switchTo(own[(i + inp.swap + own.length) % own.length]); }
    // dash
    p.dashCd -= dt; p.inv -= dt; p.hurtFlash -= dt;
    if (inp.dash && p.dashCd <= 0 && p.dashT <= 0) {
      let dx = inp.mx, dy = inp.my; if (Math.hypot(dx, dy) < 0.2) { dx = Math.cos(p.aim); dy = Math.sin(p.aim); }
      const m = Math.hypot(dx, dy); p.dvx = dx / m * 330; p.dvy = dy / m * 330;
      p.dashT = 0.2; p.dashCd = 0.85 - p.up.boots * 0.12; p.inv = Math.max(p.inv, 0.28);
      R.emit('dash', p);
    }
    const sp = p.speed * (1 + p.up.boots * 0.1) * (p.reloadT > 0 ? 0.85 : 1);
    if (p.dashT > 0) { p.dashT -= dt; p.x += p.dvx * dt; p.y += p.dvy * dt; if (SD.fx() < 0.6) R.dust(p.x, p.y, 1); }
    else { p.vx = SD.approach(p.vx, inp.mx * sp, sp * 10 * dt); p.vy = SD.approach(p.vy, inp.my * sp, sp * 10 * dt); p.x += p.vx * dt; p.y += p.vy * dt; }
    W.pushOut(p);
    p.moving = Math.hypot(p.vx, p.vy) > 12 || p.dashT > 0;
    if (p.moving) p.walk += dt * (p.dashT > 0 ? 18 : 11); else p.walk = 0;
    p.dir = SD.dir8(p.aim);
    // reload
    const wpn = SD.WEAPONS[p.cur], mag = R.magSize(p.cur);
    if (p.reloadT > 0) { p.reloadT -= dt; if (p.reloadT <= 0) { p.ammo[p.cur] = mag; R.emit('reloaded', p.cur); } }
    else if ((inp.reload && p.ammo[p.cur] < mag) || (p.ammo[p.cur] <= 0 && p.fanQ <= 0)) R.startReload();
    // fire
    p.fireT -= dt; p.fanT -= dt;
    if (p.fanQ > 0 && p.fanT <= 0 && p.reloadT <= 0) {
      if (p.ammo.revolver > 0) { R.firePlayer(p.aim + SD.rng.range(-0.22, 0.22), 0.85); p.fanQ--; p.fanT = 0.075; } else p.fanQ = 0;
    } else if (inp.alt && wpn.alt === 'fan' && p.reloadT <= 0 && p.ammo.revolver > 0 && p.fanQ <= 0 && p.fireT <= 0) {
      p.fanQ = p.ammo.revolver; p.fanT = 0; R.emit('fan');
    } else if (inp.fire && p.fireT <= 0 && p.reloadT <= 0) {
      if (p.ammo[p.cur] > 0) {
        if (wpn.auto || !p._held) R.firePlayer(p.aim, 1);
      } else if (!p._held) R.emit('empty');
    }
    p._held = inp.fire && !wpn.auto;
    // dynamite
    if (inp.dyn && p.dynamite > 0) {
      p.dynamite--; R.stats.dynamite++;
      const dist = inp.aimScreen ? Math.min(220, SD.dist(p.x, p.y, inp.aimScreen.x + SD.R.cam.x, inp.aimScreen.y + SD.R.cam.y)) : 170;
      R.throwBomb(p.x, p.y, p.aim, dist, 'p');
    }
  },
  switchTo(k) { const p = this.p; if (!k || k === p.cur || !p.weapons[k]) return; p.cur = k; p.reloadT = 0; p.fireT = 0.18; p.fanQ = 0; this.emit('swap', k); },
  startReload() {
    const p = this.p; if (p.reloadT > 0) return;
    p.reloadT = SD.WEAPONS[p.cur].reload * Math.pow(0.75, p.up.quick); p.fanQ = 0;
    this.emit('reload', p.cur);
  },
  firePlayer(ang, mult) {
    const R = this, p = R.p, w = SD.WEAPONS[p.cur];
    p.ammo[p.cur]--; p.fireT = w.rof; R.shots += w.pellets;
    const mx = p.x + Math.cos(ang) * 12, my = p.y + Math.sin(ang) * 12;
    for (let i = 0; i < w.pellets; i++) {
      const a = ang + SD.rng.range(-w.spread, w.spread) * (w.pellets > 1 ? 1 : (p.moving ? 1.6 : 1));
      const sp = w.speed * (w.pellets > 1 ? SD.rng.range(0.85, 1.1) : 1);
      const crit = SD.rng() < 0.05 + p.up.lucky * 0.1;
      R.bullets.push({ x: mx, y: my, px: mx, py: my, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp, life: w.life, owner: 'p',
        dmg: w.dmg * (1 + p.up.hollow * 0.15) * (mult || 1) * (crit ? 2 : 1), crit, pierce: w.pierce || 0, hit: [], col: w.tracer, chain: w.chain || 0, rail: !!w.rail, knock: w.knock, wpn: p.cur });
    }
    if (w.rail) R.beams.push({ x0: mx, y0: my, x1: mx + Math.cos(ang) * 900, y1: my + Math.sin(ang) * 900, t: 0.25, col: '#ff3ec8' });
    R.flashes.push({ x: mx, y: my - 20, t: 0.06, glow: p.cur === 'arc' ? 'cyan' : p.cur === 'rail' ? 'pink' : 'muzzle', s: w.pellets > 1 ? 2.2 : 1.4 });
    R.emit('shot', p.cur, w);
    // casing
    if (!w.rift) R.parts.push({ x: p.x, y: p.y - 18, z: 0, vx: SD.fx.range(-40, 40) - Math.cos(ang) * 30, vy: SD.fx.range(-20, 10), vz: SD.fx.range(60, 110), t: 0.9, col: '#d9a441', w: 2, h: 1, g: 300, floor: p.y });
  },
  hurtPlayer(dmg, fromX, fromY) {
    const R = this, p = R.p;
    if (p.inv > 0 || R.dead || R.phase === 'clear' || R.phase === 'victory') return false;
    p.hp -= dmg; R.dmgTaken += dmg; p.inv = 0.45; p.hurtFlash = 0.2;
    const a = SD.ang(fromX, fromY, p.x, p.y); p.vx += Math.cos(a) * 120; p.vy += Math.sin(a) * 120;
    R.emit('hurt', dmg, p);
    if (p.hp <= 0) { p.hp = 0; R.dead = true; R.emit('playerDie', p); R.hitstop = 0.2; }
    return true;
  },

  /* ===================================================================== enemies */
  spawnEnemy(kind, x, y) {
    const R = this, d = SD.ENEMIES[kind], s = R.waveScale();
    const e = { kind, d, x, y, r: d.r, hp: d.hp * s * (R.mode === 'endless' && !d.boss ? 1 + (R.wave - 1) * 0.03 : 1), maxHp: 0, vx: 0, vy: 0, kx: 0, ky: 0,
      dir: 0, walk: SD.rng() * 8, cd: SD.rng.range(0.8, 1.8) + (d.cd || 0) * 0.3, burst: 0, burstT: 0, flash: 0, dead: false, deadT: 0, spawnT: 0.6,
      tele: 0, strafe: SD.rng.chance(0.5) ? 1 : -1, strafeT: SD.rng.range(1, 3), boss: !!d.boss, phase: 1, aT: 0, warn: 0, id: R._eid = (R._eid || 0) + 1 };
    e.maxHp = e.hp;
    R.enemies.push(e);
    R.emit('spawn', e);
    return e;
  },
  spawnBoss(kind) {
    const R = this, W = R.world;
    const b = kind === 'warden' ? R.spawnEnemy('warden', W.mine.x - 200, W.mine.y + 140) : R.spawnEnemy('reynolds', W.w - 80, (W.street.y0 + W.street.y1) / 2);
    R.boss = b; R.emit('bossSpawn', b);
  },
  updateEnemies(dt) {
    const R = this, p = R.p, W = R.world;
    for (const e of R.enemies) {
      if (e.dead) { e.deadT += dt; continue; }
      e.flash -= dt; e.spawnT -= dt; e.warn -= dt;
      const d = e.d, dx = p.x - e.x, dy = p.y - e.y, dist = Math.hypot(dx, dy), toP = Math.atan2(dy, dx);
      const los = d.fly || W.lineClear(e.x, e.y - 4, p.x, p.y - 4);
      let mvx = 0, mvy = 0, spd = d.speed * (R.mode === 'endless' ? Math.min(1.4, 1 + R.wave * 0.015) : 1);
      if (e.spawnT > 0) spd = 0;
      const ai = SD.AI[d.ai];
      if (ai && e.spawnT <= 0 && !R.dead) { const m = ai(R, e, dt, { dist, toP, los, dx, dy }); if (m) { mvx = m[0]; mvy = m[1]; if (m[2] != null) spd = m[2]; } }
      // separation from other enemies
      for (const o of R.enemies) { if (o === e || o.dead || o.d.fly !== d.fly) continue; const sx = e.x - o.x, sy = e.y - o.y, s2 = sx * sx + sy * sy, rr = (e.r + o.r) * (e.r + o.r); if (s2 < rr && s2 > 0.01) { const s = Math.sqrt(s2), k = (Math.sqrt(rr) - s) / s * 0.5; e.x += sx * k; e.y += sy * k; } }
      e.vx = SD.approach(e.vx, mvx * spd, spd * 8 * dt); e.vy = SD.approach(e.vy, mvy * spd, spd * 8 * dt);
      e.x += (e.vx + e.kx) * dt; e.y += (e.vy + e.ky) * dt;
      e.kx *= Math.pow(0.0015, dt); e.ky *= Math.pow(0.0015, dt);
      if (!d.fly && !d.static) W.pushOut(e); else { e.x = SD.clamp(e.x, 20, W.w - 20); e.y = SD.clamp(e.y, 60, W.h - 20); }
      if (Math.hypot(e.vx, e.vy) > 8) e.walk += dt * spd / 9;
      if (!d.static) e.dir = SD.dir8(e.face != null ? e.face : toP);
      // anti-stuck: anything that has not seen the player for a long while comes back in from a fresh spot
      if (!los && !e.boss && e.spawnT <= 0) { e.lostT = (e.lostT || 0) + dt; if (e.lostT > (d.static ? 10 : 16)) { const sp = W.spawnPoint(SD.rng, p.x, p.y, R.isRift()); e.x = sp.x; e.y = sp.y; e.lostT = 0; e.spawnT = 0.6; R.emit('teleport', e, e.x, e.y); } }
      else e.lostT = 0;
      // contact damage
      if (dist < e.r + p.r + 2 && d.ai !== 'rusher' && !d.fly && e.spawnT <= 0) R.hurtPlayer(6, e.x, e.y);
    }
    R.enemies = R.enemies.filter(e => !e.dead || e.deadT < 1.2);
  },
  enemyShoot(e, ang, o) {
    o = o || {};
    const R = this, d = e.d, sp = (o.speed || d.bspeed || 240) * (R.mode === 'endless' ? 1 + Math.min(0.3, R.wave * 0.01) : 1);
    const lift = d.lift || 0;
    R.bullets.push({ x: e.x + Math.cos(ang) * 10, y: e.y + Math.sin(ang) * 6, px: e.x, py: e.y, vx: Math.cos(ang) * sp, vy: Math.sin(ang) * sp, life: o.life || 2.4, owner: 'e',
      dmg: (o.dmg || d.dmg || 8) * R.waveScale(), col: o.col || (d.rift ? (d.ai === 'glitch' ? '#3ff0ff' : '#ff3ec8') : '#ffb070'), big: o.big || d.fly, lift: lift + 20, hit: [] });
  },
  damageEnemy(e, dmg, b, ang) {
    const R = this;
    if (e.dead || e.spawnT > 0.3) return;
    e.hp -= dmg; e.flash = 0.09;
    const k = (b && b.knock) || 60;
    if (!e.boss) { e.kx += Math.cos(ang) * k; e.ky += Math.sin(ang) * k; }
    R.floaters.push({ x: e.x + SD.fx.range(-6, 6), y: e.y - 44 - (e.d.lift || 0), v: Math.round(dmg), t: 0.7, crit: b && b.crit });
    R.emit('hitEnemy', e, b);
    if (b && b.crit) { R.stats.crits++; R.p.focus = Math.min(100, R.p.focus + 2 + R.p.up.deadeye); }
    if (e.hp <= 0) R.killEnemy(e, ang);
  },
  killEnemy(e, ang) {
    const R = this, p = R.p;
    e.dead = true; e.deadT = 0; e.deathAng = ang || 0;
    R.kills++; R.waveKills++;
    R.combo++; R.comboT = 2.6; R.bestCombo = Math.max(R.bestCombo, R.combo);
    const mult = 1 + Math.min(4, Math.floor(R.combo / 5) * 0.5);
    R.score += Math.round(e.d.score * mult);
    p.focus = Math.min(100, p.focus + 7 * (1 + p.up.deadeye * 0.4));
    // bounty: coins that fly out and get pulled to you
    let v = e.d.bounty; const n = Math.min(8, Math.ceil(v / 4));
    for (let i = 0; i < n; i++) { const c = Math.ceil(v / (n - i)); v -= c; R.pickups.push({ kind: 'coin', v: c, x: e.x, y: e.y, vx: SD.fx.range(-70, 70), vy: SD.fx.range(-70, 70), t: 0, life: 14 }); }
    if (SD.rng() < (e.boss ? 1 : 0.07)) R.pickups.push({ kind: 'health', v: e.boss ? 60 : 20, x: e.x, y: e.y, vx: 0, vy: 0, t: 0, life: 14 });
    if (SD.rng() < 0.05) R.pickups.push({ kind: 'dynamite', v: 1, x: e.x + 6, y: e.y, vx: 0, vy: 0, t: 0, life: 14 });
    if (e.d.fly) R.explode(e.x, e.y, e.boss ? 140 : 38, e.boss ? 0 : 18, 'e', true);
    if (e.boss) { R.boss = null; R.hitstop = 0.3; R.slow = 1.2; }
    else R.hitstop = Math.max(R.hitstop, 0.03);
    R.emit('kill', e, R.combo);
  },

  /* ===================================================================== bullets */
  updateBullets(dt) {
    const R = this, W = R.world, p = R.p;
    for (const b of R.bullets) {
      if (b.dead) continue;
      b.life -= dt; if (b.life <= 0) { b.dead = true; continue; }
      const steps = Math.max(1, Math.ceil(Math.hypot(b.vx, b.vy) * dt / 7));
      for (let s = 0; s < steps && !b.dead; s++) {
        b.px = b.x; b.py = b.y;
        b.x += b.vx * dt / steps; b.y += b.vy * dt / steps;
        // props (cover). Rail rounds ignore cover; flying drones' shots arc over it.
        if (!b.rail && !(b.owner === 'e' && b.big)) {
          const h = W.hitProp(b.px, b.py, b.x, b.y);
          if (h) { b.dead = true; R.hitPropWith(h.p, b, h.x, h.y); break; }
        }
        if (b.owner === 'p') {
          for (const e of R.enemies) {
            if (e.dead || b.hit.includes(e.id)) continue;
            const ey = e.y - (e.d.fly ? (e.d.lift || 0) * 0.5 : 0);
            if (SD.dist2(b.x, b.y, e.x, ey) < (e.r + 4) * (e.r + 4)) {
              b.hit.push(e.id); if (b.hit.length === 1) R.hits++;
              R.damageEnemy(e, b.dmg, b, Math.atan2(b.vy, b.vx));
              if (b.chain > 0) R.chainArc(e, b);
              if (b.pierce > 0) { b.pierce--; b.dmg *= 0.8; } else { b.dead = true; break; }
            }
          }
          // shooting your own dynamite in the air sets it off
          for (const bomb of R.bombs) if (!bomb.dead && bomb.owner === 'e' && SD.dist2(b.x, b.y, bomb.x, bomb.y) < 100) { bomb.fuse = 0; b.dead = true; }
        } else if (SD.dist2(b.x, b.y, p.x, p.y) < (p.r + 3) * (p.r + 3)) {
          if (R.hurtPlayer(b.dmg, b.x - b.vx, b.y - b.vy)) b.dead = true;
        }
        if (b.x < -20 || b.y < -20 || b.x > W.w + 20 || b.y > W.h + 20) b.dead = true;
      }
    }
    R.bullets = R.bullets.filter(b => !b.dead);
  },
  chainArc(from, b) {
    const R = this; let src = from, n = b.chain;
    const seen = [from.id];
    while (n-- > 0) {
      let best = null, bd = 120 * 120;
      for (const e of R.enemies) { if (e.dead || seen.includes(e.id)) continue; const d2 = SD.dist2(src.x, src.y, e.x, e.y); if (d2 < bd) { bd = d2; best = e; } }
      if (!best) break;
      seen.push(best.id);
      R.beams.push({ x0: src.x, y0: src.y - 20, x1: best.x, y1: best.y - 20, t: 0.12, col: '#3ff0ff', zig: true, world: true });
      R.damageEnemy(best, b.dmg * 0.6, { knock: 10 }, SD.ang(src.x, src.y, best.x, best.y));
      src = best;
    }
  },
  hitPropWith(prop, b, x, y) {
    const R = this, d = prop.def;
    R.emit('hitProp', prop, d.mat, x, y);
    R.sparks(x, y, d.mat);
    if (!d.hp || prop.dead) return;
    prop.hp -= b.owner === 'p' ? b.dmg : b.dmg * 2; prop.flash = 0.08;
    if (prop.hp <= 0) R.breakProp(prop);
  },
  breakProp(prop) {
    const R = this, d = prop.def;
    prop.dead = true; R.world.nav.dirty = true;
    R.emit('breakProp', prop);
    for (let i = 0; i < 14; i++) R.parts.push({ x: prop.x + SD.fx.range(-10, 10), y: prop.y - 10, z: SD.fx.range(0, 16), vx: SD.fx.range(-120, 120), vy: SD.fx.range(-70, 50), vz: SD.fx.range(60, 180), t: SD.fx.range(0.6, 1.2), col: d.mat === 'metal' ? '#5a5f72' : '#8a5a32', w: 3, h: 2, g: 420, floor: prop.y + SD.fx.range(-6, 6) });
    if (d.explode) R.explode(prop.x, prop.y - 8, 90, 120, 'world');
    else if (d.drop && SD.rng() < d.drop) R.pickups.push({ kind: SD.rng() < 0.6 ? 'coin' : (SD.rng() < 0.5 ? 'health' : 'dynamite'), v: SD.rng() < 0.6 ? 6 : 20, x: prop.x, y: prop.y, vx: 0, vy: 0, t: 0, life: 16 });
  },

  /* ===================================================================== dynamite */
  throwBomb(x, y, ang, dist, owner, o) {
    o = o || {};
    const R = this, T = o.time || 0.75;
    R.bombs.push({ x, y, z: 14, vx: Math.cos(ang) * dist / T, vy: Math.sin(ang) * dist / T, vz: 150, fuse: o.fuse || SD.DYNAMITE.fuse, owner, t: 0, spin: 0,
      dmg: o.dmg || (owner === 'p' ? SD.DYNAMITE.dmg : 32), radius: o.radius || (owner === 'p' ? SD.DYNAMITE.radius : 62) });
    R.emit('throw', owner);
  },
  updateBombs(dt) {
    const R = this;
    for (const b of R.bombs) {
      if (b.dead) continue;
      b.t += dt; b.fuse -= dt; b.spin += dt * 14;
      if (b.z > 0 || b.vz > 0) { b.vz -= 420 * dt; b.z += b.vz * dt; if (b.z <= 0) { b.z = 0; b.vz = Math.abs(b.vz) > 60 ? -b.vz * 0.3 : 0; b.vx *= 0.45; b.vy *= 0.45; } }
      else { b.vx *= Math.pow(0.02, dt); b.vy *= Math.pow(0.02, dt); }
      const nx = b.x + b.vx * dt, ny = b.y + b.vy * dt;
      const h = R.world.hitProp(b.x, b.y, nx, ny);
      if (h && b.z < 18) { b.vx *= -0.4; b.vy *= -0.4; } else { b.x = nx; b.y = ny; }
      if (b.fuse <= 0) { b.dead = true; R.explode(b.x, b.y, b.radius, b.dmg, b.owner); }
    }
    R.bombs = R.bombs.filter(b => !b.dead);
  },
  explode(x, y, radius, dmg, owner, small) {
    const R = this, p = R.p;
    R.emit('explode', x, y, radius, small);
    R.decals.push({ x, y, r: radius * 0.45, t: 30 });
    if (R.decals.length > 40) R.decals.shift();
    R.flashes.push({ x, y: y - 10, t: 0.25, glow: 'fire', s: radius / 22 });
    for (let i = 0; i < (small ? 10 : 34); i++) {
      const a = SD.fx() * Math.PI * 2, s = SD.fx.range(40, 220) * radius / 70;
      R.parts.push({ x, y, z: 4, vx: Math.cos(a) * s, vy: Math.sin(a) * s * 0.6, vz: SD.fx.range(40, 200), t: SD.fx.range(0.3, 0.8), col: SD.fx.pick(['#ffd166', '#ff7a1a', '#ff4d1a', '#fff2a8']), w: 3, h: 3, g: 200, floor: y + SD.fx.range(-10, 10), glow: true });
      if (i % 3 === 0) R.parts.push({ x: x + SD.fx.range(-radius * 0.4, radius * 0.4), y, z: SD.fx.range(0, 20), vx: SD.fx.range(-20, 20), vy: 0, vz: SD.fx.range(20, 50), t: SD.fx.range(0.8, 1.6), col: '#3a3028', w: 6, h: 6, g: -10, floor: y, smoke: true });
    }
    if (dmg <= 0) return;
    for (const e of R.enemies) {
      if (e.dead) continue;
      const d = SD.dist(x, y, e.x, e.y);
      if (d < radius + e.r) R.damageEnemy(e, dmg * (owner === 'e' ? 0.5 : 1) * (1 - 0.5 * d / (radius + e.r)), { knock: SD.DYNAMITE.knock }, SD.ang(x, y, e.x, e.y));
    }
    const dp = SD.dist(x, y, p.x, p.y);
    if (dp < radius + p.r) R.hurtPlayer(Math.round((owner === 'p' ? 26 : owner === 'world' ? 38 : dmg) * (1 - 0.5 * dp / (radius + p.r))), x, y);
    for (const pr of R.world.props) {
      if (pr.dead || !pr.def.hp) continue;
      if (SD.dist(x, y, pr.x, pr.y) < radius + 16) { pr.hp -= dmg; pr.flash = 0.1; if (pr.hp <= 0) R.breakProp(pr); }
    }
    for (const b of R.bombs) if (!b.dead && SD.dist(x, y, b.x, b.y) < radius) b.fuse = Math.min(b.fuse, 0.12);
  },

  /* ===================================================================== pickups */
  updatePickups(dt) {
    const R = this, p = R.p;
    for (const k of R.pickups) {
      k.t += dt; k.life -= dt;
      k.vx *= Math.pow(0.03, dt); k.vy *= Math.pow(0.03, dt);
      const d = SD.dist(k.x, k.y, p.x, p.y);
      const pull = k.kind === 'coin' ? 90 : 34;
      if (k.t > 0.35 && d < pull && !R.dead) { const a = SD.ang(k.x, k.y, p.x, p.y), s = 260 + (pull - d) * 4; k.vx = Math.cos(a) * s; k.vy = Math.sin(a) * s; }
      k.x += k.vx * dt; k.y += k.vy * dt;
      if (d < 10 && k.t > 0.2 && !R.dead) {
        k.dead = true;
        if (k.kind === 'coin') { p.money += k.v; R.stats.bounty += k.v; }
        else if (k.kind === 'health') p.hp = Math.min(p.maxHp, p.hp + k.v);
        else if (k.kind === 'dynamite') p.dynamite = Math.min(9, p.dynamite + k.v);
        R.emit('pickup', k);
      }
      if (k.life <= 0) k.dead = true;
    }
    R.pickups = R.pickups.filter(k => !k.dead);
  },

  /* ===================================================================== cosmetics */
  dust(x, y, n) { for (let i = 0; i < n; i++) this.parts.push({ x: x + SD.fx.range(-4, 4), y, z: 1, vx: SD.fx.range(-25, 25), vy: SD.fx.range(-10, 4), vz: SD.fx.range(10, 30), t: SD.fx.range(0.3, 0.6), col: '#b89470', w: 3, h: 2, g: 20, floor: y, smoke: true }); },
  sparks(x, y, mat) {
    const col = mat === 'metal' ? '#fff2a8' : mat === 'rock' ? '#c8c0b0' : mat === 'hay' ? '#e8d27a' : '#a8743f';
    for (let i = 0; i < 5; i++) this.parts.push({ x, y, z: 16, vx: SD.fx.range(-80, 80), vy: SD.fx.range(-50, 30), vz: SD.fx.range(30, 120), t: SD.fx.range(0.2, 0.45), col, w: 2, h: 2, g: 400, floor: y + 2, glow: mat === 'metal' });
  },
  updateCosmetic(dt) {
    const R = this;
    for (const q of R.parts) {
      q.t -= dt; q.x += q.vx * dt; q.y += q.vy * dt;
      if (q.smoke) { q.z += q.vz * dt; q.vx *= 0.96; q.vy *= 0.96; q.w += dt * 6; q.h += dt * 6; }
      else { q.vz -= q.g * dt; q.z += q.vz * dt; if (q.z <= 0) { q.z = 0; q.vz = -q.vz * 0.3; q.vx *= 0.6; q.vy *= 0.6; } }
    }
    R.parts = R.parts.filter(q => q.t > 0);
    if (R.parts.length > 700) R.parts.splice(0, R.parts.length - 700);
    for (const f of R.floaters) { f.t -= dt; f.y -= dt * 28; }
    R.floaters = R.floaters.filter(f => f.t > 0);
    for (const b of R.beams) b.t -= dt; R.beams = R.beams.filter(b => b.t > 0);
    for (const f of R.flashes) f.t -= dt; R.flashes = R.flashes.filter(f => f.t > 0);
    for (const d of R.decals) d.t -= dt;
    for (const pr of R.world.props) { pr.flash -= dt; pr.glitch -= dt; }
  },

  /* ===================================================================== shop */
  // in endless the upgrades go twice as deep, so late-night bounty still has somewhere to go
  maxOf(item) { return this.mode === 'endless' && !item.weapon && item.max < 50 ? item.max * 2 : item.max; },
  shopPrice(item) { const n = this.owned(item.id); return Math.round(item.cost * (item.max > 1 && item.max < 50 ? 1 + n * 0.5 : 1)); },
  owned(id) {
    const p = this.p;
    if (SD.WEAPONS[id]) return p.weapons[id] ? 1 : 0;
    if (id in p.up) return p.up[id];
    return 0;
  },
  canBuy(item) {
    if (item.rift && !this.isRift()) return 'AFTER MIDNIGHT';
    if (this.owned(item.id) >= this.maxOf(item)) return 'SOLD OUT';
    if (item.id === 'whiskey' && this.p.hp >= this.p.maxHp) return 'FULL HEALTH';
    if (this.p.money < this.shopPrice(item)) return 'NOT ENOUGH';
    return null;
  },
  buy(item) {
    const R = this, p = R.p, why = R.canBuy(item);
    if (why) { R.emit('deny', why); return false; }
    p.money -= R.shopPrice(item);
    switch (item.id) {
      case 'whiskey': p.hp = Math.min(p.maxHp, p.hp + 45); break;
      case 'dynamite': p.dynamite = Math.min(9, p.dynamite + 2); break;
      case 'duster': p.up.duster++; p.maxHp += 25; p.hp = p.maxHp; break;
      case 'cylinder': p.up.cylinder++; for (const k of SD.WEAPON_ORDER) p.ammo[k] = R.magSize(k); break;
      default:
        if (SD.WEAPONS[item.id]) { p.weapons[item.id] = true; p.ammo[item.id] = R.magSize(item.id); R.switchTo(item.id); }
        else if (item.id in p.up) p.up[item.id]++;
    }
    R.emit('buy', item);
    return true;
  },
};

/* ===================================================================== AI
   Each returns [moveX, moveY, speed?] for this step and may shoot. */
SD.AI = {
  _approach(R, e, c, keep) {
    const W = R.world;
    if (!c.los || c.dist > keep[1]) { const v = W.nav.dir(e.x, e.y); return [v[0], v[1]]; }
    e.strafeT -= SD.DT; if (e.strafeT <= 0) { e.strafe *= -1; e.strafeT = SD.rng.range(1.2, 2.6); }
    const px = -Math.sin(c.toP) * e.strafe, py = Math.cos(c.toP) * e.strafe;
    if (c.dist < keep[0]) return [-Math.cos(c.toP) * 0.8 + px * 0.5, -Math.sin(c.toP) * 0.8 + py * 0.5];
    return [px * 0.7, py * 0.7, e.d.speed * 0.6];
  },
  gunner(R, e, dt, c) {
    const d = e.d; e.cd -= dt;
    if (e.burst > 0) { e.burstT -= dt; if (e.burstT <= 0) { e.burst--; e.burstT = d.burstGap; R.enemyShoot(e, c.toP + SD.rng.range(-d.spread, d.spread)); R.emit('enemyShot', e); } }
    else if (e.cd <= 0 && c.los && c.dist < 300) { e.warn = 0.35; e.cd = d.cd * SD.rng.range(0.8, 1.25) + 0.35; e.burst = d.burst; e.burstT = 0.35; }
    return SD.AI._approach(R, e, c, d.keep);
  },
  rusher(R, e, dt, c) {
    const d = e.d; e.cd -= dt;
    if (e.aT > 0) { e.aT -= dt; if (e.aT <= 0 && c.dist < 26) R.hurtPlayer(d.melee * R.waveScale(), e.x, e.y); return [0, 0]; }
    if (c.dist < 20 && e.cd <= 0) { e.aT = 0.22; e.cd = d.meleeCd; e.warn = 0.22; R.emit('swing', e); return [0, 0]; }
    if (c.los && c.dist < 160) return [Math.cos(c.toP), Math.sin(c.toP), d.speed * 1.12];
    const v = R.world.nav.dir(e.x, e.y); return [v[0], v[1]];
  },
  shotgunner(R, e, dt, c) {
    const d = e.d; e.cd -= dt;
    if (e.cd <= 0 && c.los && c.dist < d.keep[1] + 30) {
      e.warn = 0.4; e.cd = d.cd * SD.rng.range(0.85, 1.2);
      e.pending = 0.4;
    }
    if (e.pending > 0) { e.pending -= dt; if (e.pending <= 0) { for (let i = 0; i < d.pellets; i++) R.enemyShoot(e, c.toP + SD.rng.range(-d.spread, d.spread), { speed: d.bspeed * SD.rng.range(0.8, 1.1), life: 0.9 }); R.emit('enemyShotgun', e); } return [0, 0]; }
    return SD.AI._approach(R, e, c, d.keep);
  },
  bomber(R, e, dt, c) {
    const d = e.d; e.cd -= dt;
    if (e.cd <= 0 && c.dist < 260 && c.dist > 70) {
      e.cd = d.cd * SD.rng.range(0.8, 1.2);
      const lead = 0.5, tx = R.p.x + R.p.vx * lead, ty = R.p.y + R.p.vy * lead;
      R.throwBomb(e.x, e.y, SD.ang(e.x, e.y, tx, ty), Math.min(240, SD.dist(e.x, e.y, tx, ty)), 'e', { fuse: 1.6 });
    }
    return SD.AI._approach(R, e, c, d.keep);
  },
  glitch(R, e, dt, c) {
    e.tele -= dt;
    if (e.tele <= 0 && e.spawnT <= 0) {
      e.tele = SD.rng.range(2.6, 4.2);
      if (c.dist > 90) {
        const a = SD.rng() * Math.PI * 2, r = SD.rng.range(110, 180);
        const nx = R.p.x + Math.cos(a) * r, ny = R.p.y + Math.sin(a) * r;
        R.emit('teleport', e, e.x, e.y); e.x = nx; e.y = ny; R.world.pushOut(e); e.spawnT = 0.15; e.flash = 0.2; e.cd = Math.max(e.cd, 0.6);
      }
    }
    return SD.AI.gunner(R, e, dt, c);
  },
  drone(R, e, dt, c) {
    const d = e.d; e.cd -= dt;
    if (e.cd <= 0 && c.dist < 320) { e.cd = d.cd * SD.rng.range(0.8, 1.3); R.enemyShoot(e, c.toP, { col: '#ff3ec8' }); R.emit('droneShot', e); }
    // orbit at ~150 px, drifting in and out
    const want = 150 + Math.sin(R.t * 0.7 + e.id) * 40;
    const radial = (c.dist - want) / 60, tang = e.strafe;
    const vx = Math.cos(c.toP) * radial - Math.sin(c.toP) * tang, vy = Math.sin(c.toP) * radial + Math.cos(c.toP) * tang;
    const m = Math.hypot(vx, vy) || 1; return [vx / m, vy / m];
  },
  turret(R, e, dt, c) {
    const d = e.d; e.cd -= dt; e.face = c.toP;
    if (e.cd <= 0 && c.los && c.dist < 340) { e.cd = d.cd * SD.rng.range(0.85, 1.15); e.warn = 0.3; e.pending = 0.3; }
    if (e.pending > 0) { e.pending -= dt; if (e.pending <= 0) { for (let i = -1; i <= 1; i++) R.enemyShoot(e, c.toP + i * 0.18, { col: '#ff3ec8' }); R.emit('droneShot', e); } }
    return [0, 0];
  },
  reynolds(R, e, dt, c) {
    const d = e.d, f = e.hp / e.maxHp;
    const ph = f > 0.66 ? 1 : f > 0.33 ? 2 : 3;
    if (ph !== e.phase) { e.phase = ph; R.emit('bossPhase', e, ph); if (ph === 2) for (let i = 0; i < 3; i++) { const sp = R.world.spawnPoint(SD.rng, R.p.x, R.p.y); R.spawnEnemy(i ? 'outlaw' : 'bomber', sp.x, sp.y); R.waveTotal++; } if (ph === 3) for (let i = 0; i < 4; i++) { const sp = R.world.spawnPoint(SD.rng, R.p.x, R.p.y); R.spawnEnemy('rustler', sp.x, sp.y); R.waveTotal++; } }
    e.cd -= dt; e.aT -= dt;
    if (e.charge > 0) { e.charge -= dt; if (c.dist < 22) R.hurtPlayer(18, e.x, e.y); return [Math.cos(e.chargeA), Math.sin(e.chargeA), 300]; }
    if (e.burst > 0) { e.burstT -= dt; if (e.burstT <= 0) { e.burst--; e.burstT = 0.07; R.enemyShoot(e, c.toP + (e.burst - 3) * 0.12, { speed: d.bspeed }); R.emit('enemyShot', e); } return [0, 0]; }
    if (e.cd <= 0) {
      const roll = SD.rng();
      if (ph >= 3 && roll < 0.35) { e.charge = 0.55; e.chargeA = c.toP; e.warn = 0.4; e.cd = 1.4; R.emit('bossYell', e); }
      else if (ph >= 2 && roll < 0.65) { for (let i = -1; i <= 1; i++) R.throwBomb(e.x, e.y, c.toP + i * 0.5, Math.min(220, c.dist), 'e', { fuse: 1.4, dmg: 26, radius: 60 }); e.cd = 2.2 - ph * 0.3; }
      else { e.warn = 0.45; e.burst = 6; e.burstT = 0.45; e.cd = 2.4 - ph * 0.35; }
    }
    return SD.AI._approach(R, e, c, d.keep);
  },
  warden(R, e, dt, c) {
    const d = e.d, f = e.hp / e.maxHp;
    const ph = f > 0.66 ? 1 : f > 0.33 ? 2 : 3;
    if (ph !== e.phase) { e.phase = ph; R.emit('bossPhase', e, ph); for (let i = 0; i < 2 + ph; i++) { R.spawnEnemy('drone', e.x + SD.rng.range(-80, 80), e.y + SD.rng.range(-40, 40)); R.waveTotal++; } }
    e.cd -= dt; e.aT += dt;
    // patterns: spiral, rings, and an aimed lance that is telegraphed before it fires
    if (e.lance > 0) {
      e.lance -= dt;
      if (e.lance <= 0.25 && !e.lanceFired) {
        e.lanceFired = true; R.beams.push({ x0: e.x, y0: e.y - 30, x1: e.x + Math.cos(e.lanceA) * 1000, y1: e.y - 30 + Math.sin(e.lanceA) * 1000, t: 0.25, col: '#ff3ec8', fat: true });
        // damage anything on the line
        const px = R.p.x - e.x, py = R.p.y - e.y, along = px * Math.cos(e.lanceA) + py * Math.sin(e.lanceA), off = Math.abs(-px * Math.sin(e.lanceA) + py * Math.cos(e.lanceA));
        if (along > 0 && off < 14) R.hurtPlayer(28, e.x, e.y);
        R.emit('lance', e);
      }
      return [0, 0];
    }
    if (e.pattern) {
      e.pt -= dt; e.pT -= dt;
      if (e.pT <= 0) {
        if (e.pattern === 'spiral') { e.pT = 0.07 - ph * 0.01; for (let k = 0; k < (ph >= 2 ? 3 : 2); k++) R.enemyShoot(e, e.spin + k * Math.PI * 2 / (ph >= 2 ? 3 : 2), { speed: 150 + ph * 15, life: 4, col: '#ff3ec8', big: true }); e.spin += 0.23; }
        else if (e.pattern === 'ring') { e.pT = 0.55; const n = 16 + ph * 4, off = SD.rng() * 0.4; for (let k = 0; k < n; k++) R.enemyShoot(e, off + k * Math.PI * 2 / n, { speed: 140, life: 5, col: '#3ff0ff', big: true }); }
        R.emit('droneShot', e);
      }
      if (e.pt <= 0) { e.pattern = null; e.cd = 1.2 - ph * 0.15; }
      return [Math.cos(c.toP) * 0.2, Math.sin(c.toP) * 0.2];
    }
    if (e.cd <= 0) {
      const roll = SD.rng();
      if (roll < 0.4) { e.pattern = 'spiral'; e.pt = 2.6 + ph * 0.6; e.pT = 0; e.spin = SD.rng() * 6; }
      else if (roll < 0.7) { e.pattern = 'ring'; e.pt = 1.7 + ph * 0.5; e.pT = 0; }
      else { e.lance = 1.1; e.lanceFired = false; e.lanceA = c.toP; e.warn = 0.9; R.emit('lanceWarn', e); }
    }
    const want = 190, radial = (c.dist - want) / 80;
    return [Math.cos(c.toP) * radial - Math.sin(c.toP) * 0.6, Math.sin(c.toP) * radial + Math.cos(c.toP) * 0.6];
  },
};
