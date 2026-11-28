/* World: the town of Sundown (Colorado Territory, 1864). Ground tiles, props with collision
   footprints, a navigation flow field, spawn points, and the rift that spreads from the mine. */
'use strict';
SD.World = function (seed) {
  const W = this;
  W.T = 32; W.cols = 52; W.rows = 34; W.w = W.cols * W.T; W.h = W.rows * W.T;
  W.rng = SD.makeRng(seed ^ 0x5eed);
  W.props = []; W.flats = []; W.lights = [];
  W.street = { y0: 500, y1: 580 };
  W.mine = { x: 1290, y: 210 };
  W.rift = { on: false, t: 0, r: 0, max: 0 };
  W.tile = new Uint8Array(W.cols * W.rows);
  W.riftTile = new Uint8Array(W.cols * W.rows);
  W.buildGround();
  W.buildTown();
  W.nav = new SD.Nav(W);
};
SD.World.prototype = {
  at(i, j) { const k = j * this.cols + i; return this.riftTile[k] ? this.riftTile[k] : this.tile[k]; },
  buildGround() {
    const r = this.rng, T = this.T;
    for (let j = 0; j < this.rows; j++) for (let i = 0; i < this.cols; i++) {
      const y = j * T + 16, x = i * T + 16;
      let t = r.int(0, 3);
      const n = Math.sin(i * 0.37 + j * 0.21) + Math.sin(i * 0.11 - j * 0.53) * 0.8;
      if (n > 1.1 && r.chance(0.6)) t = r.chance(0.5) ? 5 : 6;           // grass patches
      else if (n < -1.3 && r.chance(0.5)) t = 9;                          // sandy wash
      else if (r.chance(0.05)) t = 4;                                     // pebbles
      else if (r.chance(0.02)) t = 14;                                    // cracked
      if (y > this.street.y0 && y < this.street.y1) t = (j % 2 ? 7 : r.int(0, 3));   // the main street, rutted
      if (Math.abs(x - this.mine.x) < 90 && y > this.mine.y && y < this.street.y0 && (i % 2 === 0)) t = 8; // track down from the mine
      this.tile[j * this.cols + i] = t;
    }
  },
  add(name, x, y, o) {
    const d = SD.PROPS[name] || { w: 20, h: 12, mat: 'wood' };
    const p = Object.assign({ name, base: name, x, y, row: 0, hp: d.hp || 0, maxHp: d.hp || 0, def: d, dead: false, flash: 0, glitch: 0, id: this.props.length }, o || {});
    if (d.flat) this.flats.push(p); else this.props.push(p);
    if (d.light) this.lights.push({ prop: p, glow: d.light });
    return p;
  },
  buildTown() {
    const r = this.rng, A = this.add.bind(this), S = this.street;
    // the main street: telegraph line along the north edge, boardwalks along both sides
    for (let x = 90; x < this.w - 60; x += 230) A('TelegraphPole', x + r.int(-10, 10), S.y0 - 18, { row: 1 });
    for (let x = 520; x <= 1060; x += 90) A('Boardwalk', x, S.y0 - 30, { row: 0 });
    for (let x = 260; x <= 520; x += 90) A('Boardwalk', x, S.y1 + 46, { row: 0 });
    // the Last Chance Saloon frontage (north side, centre): wall sections, doors, sconces, hitching posts
    for (let k = -3; k <= 3; k++) if (k !== 0) A('WallSection', 790 + k * 58, S.y0 - 66, { row: 0, facade: true });
    A('SaloonDoors', 790, S.y0 - 60, { row: 0, saloon: true });
    A('WallSconce', 760, S.y0 - 60, { row: 0, lamp: true }); A('WallSconce', 820, S.y0 - 60, { row: 0, lamp: true });
    this.lights.push({ x: 760, y: S.y0 - 78, glow: 'warm', r: 70 }, { x: 820, y: S.y0 - 78, glow: 'warm', r: 70 });
    A('HitchingPost', 690, S.y0 + 4, { row: 0 }); A('HitchingPost', 890, S.y0 + 4, { row: 0 });
    A('WaterTrough', 610, S.y0 + 12, { row: 1 }); A('WantedBoard', 990, S.y0 - 10, { row: 1, wanted: true });
    A('RainBarrel', 560, S.y0 - 40, { row: 1 }); A('RainBarrel', 1030, S.y0 - 40, { row: 7 });
    // the mine (north-east): entrance, carts, ore, sluice, dynamite storage
    A('MineEntrance', this.mine.x, this.mine.y, { row: 0 });
    A('MineCart', this.mine.x - 70, this.mine.y + 70, { row: 2 }); A('MineCart', this.mine.x + 80, this.mine.y + 110, { row: 3 });
    A('OrePile', this.mine.x + 150, this.mine.y + 40, { row: 1 }); A('OrePile', this.mine.x - 170, this.mine.y + 30, { row: 5 });
    A('SluiceBox', this.mine.x - 260, this.mine.y + 150, { row: 1 });
    A('DynamiteCrate', this.mine.x + 40, this.mine.y + 190, { row: 1 }); A('DynamiteCrate', this.mine.x - 130, this.mine.y + 210, { row: 2 });
    A('Crate', this.mine.x + 120, this.mine.y + 200, { row: 0 }); A('Barrel', this.mine.x + 160, this.mine.y + 180, { row: 0 });
    // the camp (north-west): tents round a fire
    const cx = 330, cy = 250;
    A('Campfire', cx, cy, { row: 0, anim: true });
    this.lights.push({ x: cx, y: cy - 8, glow: 'fire', r: 110, flicker: true });
    A('MinerTent', cx - 120, cy - 50, { row: 1 }); A('MinerTent', cx + 110, cy - 70, { row: 7 }); A('MinerTent', cx - 20, cy - 130, { row: 0 });
    A('WagonWheel', cx + 70, cy + 40, { row: 2 }); A('Crate', cx - 90, cy + 50, { row: 1 }); A('Barrel', cx - 60, cy + 64, { row: 0 });
    A('RockSet', 120, 120, { row: 3 }); A('RockSet', 560, 130, { row: 6 });
    // wagons circled south of the street
    A('CoveredWagon', 980, 800, { row: 1 }); A('CoveredWagon', 1240, 760, { row: 3 }); A('CoveredWagon', 720, 860, { row: 7 });
    A('HayBales', 860, 700, { row: 0 }); A('HayBales', 1130, 680, { row: 2 }); A('HayBales', 400, 760, { row: 1 });
    A('Crate', 1080, 870, { row: 2 }); A('Barrel', 1110, 880, { row: 0 }); A('DynamiteCrate', 600, 700, { row: 3 });
    A('Campfire', 1080, 770, { row: 0, anim: true }); this.lights.push({ x: 1080, y: 762, glow: 'fire', r: 90, flicker: true });
    // cover scattered along the street
    const cover = [[300, 620, 'Crate'], [450, 470, 'Barrel'], [1180, 470, 'Crate'], [1400, 610, 'Barrel'], [1500, 470, 'HayBales'], [200, 470, 'HayBales'],
      [1350, 430, 'DynamiteCrate'], [700, 640, 'Barrel'], [930, 630, 'Crate'], [1600, 640, 'Crate'], [160, 880, 'RockSet'], [1500, 900, 'RockSet'],
      [1420, 820, 'Barrel'], [260, 980, 'HayBales'], [880, 980, 'Crate'], [1260, 980, 'Barrel']];
    for (const [x, y, n] of cover) A(n, x + r.int(-12, 12), y + r.int(-8, 8), { row: r.int(0, 7) });
    // fences along the south and west edges
    for (let x = 80; x < this.w - 80; x += 64) if (r.chance(0.7)) A('SplitRailFence', x, this.h - 18, { row: 2 });
    for (let y = 680; y < this.h - 60; y += 70) if (r.chance(0.6)) A('SplitRailFence', 28, y, { row: 0 });
  },

  /* ---------- collision ---------- */
  box(p) { const d = p.def; return [p.x - d.w / 2, p.y - d.h, p.x + d.w / 2, p.y]; },
  solid(p) { return !p.dead && p.def.w > 0 && p.def.walk !== true && !p.def.flat; },
  pushOut(e) { // circle (e.x, e.y, e.r) out of every solid footprint and the map edge
    for (const p of this.props) {
      if (!this.solid(p)) continue;
      const [x0, y0, x1, y1] = this.box(p);
      const cx = SD.clamp(e.x, x0, x1), cy = SD.clamp(e.y, y0, y1);
      const dx = e.x - cx, dy = e.y - cy, d2 = dx * dx + dy * dy;
      if (d2 < e.r * e.r) {
        if (d2 > 0.0001) { const d = Math.sqrt(d2), k = (e.r - d) / d; e.x += dx * k; e.y += dy * k; }
        else { // centre inside: leave by the nearest side
          const l = e.x - x0, rr = x1 - e.x, t = e.y - y0, b = y1 - e.y, m = Math.min(l, rr, t, b);
          if (m === l) e.x = x0 - e.r; else if (m === rr) e.x = x1 + e.r; else if (m === t) e.y = y0 - e.r; else e.y = y1 + e.r;
        }
      }
    }
    e.x = SD.clamp(e.x, 14, this.w - 14); e.y = SD.clamp(e.y, 40, this.h - 10);
  },
  /* first solid prop a short segment passes through (bullets). Thin props (fences) only stop some. */
  hitProp(x0, y0, x1, y1) {
    for (const p of this.props) {
      if (!this.solid(p) || p.def.soft) continue;
      const [a, b, c, d] = this.box(p);
      if (Math.max(x0, x1) < a || Math.min(x0, x1) > c || Math.max(y0, y1) < b - 6 || Math.min(y0, y1) > d) continue;
      // sample along the segment
      const n = Math.max(1, Math.ceil(Math.hypot(x1 - x0, y1 - y0) / 4));
      for (let k = 0; k <= n; k++) {
        const x = x0 + (x1 - x0) * k / n, y = y0 + (y1 - y0) * k / n;
        if (x >= a && x <= c && y >= b - 6 && y <= d) return { p, x, y };
      }
    }
    return null;
  },
  lineClear(x0, y0, x1, y1) { return !this.hitProp(x0, y0, x1, y1); },

  /* ---------- spawns ---------- */
  spawnPoint(rng, px, py, rift) {
    const pts = [[30, (this.street.y0 + this.street.y1) / 2], [this.w - 30, (this.street.y0 + this.street.y1) / 2], [this.mine.x, this.mine.y + 40],
      [90, 900], [this.w - 90, 900], [700, 70], [this.w / 2, this.h - 70], [70, 300], [this.w - 70, 320]];
    let best = null;
    for (let tries = 0; tries < 24; tries++) {
      const c = rift && rng.chance(0.35) ? [this.mine.x + rng.range(-60, 60), this.mine.y + rng.range(30, 70)] : rng.pick(pts);
      const q = { x: c[0] + rng.range(-20, 20), y: c[1] + rng.range(-16, 16), r: 10 };
      this.pushOut(q);
      if (!this.reachable(q.x, q.y)) continue;       // never behind a fence or inside a prop
      best = q;
      if (SD.dist(q.x, q.y, px, py) > 260) break;
    }
    return best || { x: this.w - 30, y: (this.street.y0 + this.street.y1) / 2 };
  },
  reachable(x, y) { // can something standing here walk to the player? (uses the last flow field)
    const N = this.nav; if (N.tx < 0) return true;
    const i = SD.clamp(Math.floor(x / 16), 0, N.cols - 1), j = SD.clamp(Math.floor(y / 16), 0, N.rows - 1);
    return N.dist[j * N.cols + i] >= 0;
  },

  /* ---------- the rift ---------- */
  startRift() { this.rift.on = true; this.rift.t = 0; this.rift.max = Math.hypot(this.w, this.h); },
  updateRift(dt, emit) {
    const R = this.rift; if (!R.on || R.r >= R.max) return;
    R.t += dt; const prev = R.r; R.r = Math.min(R.max, R.t * 520);
    const T = this.T;
    for (let j = 0; j < this.rows; j++) for (let i = 0; i < this.cols; i++) {
      const d = SD.dist(i * T + 16, j * T + 16, this.mine.x, this.mine.y);
      if (d <= R.r && d > prev - 40) {
        const k = j * this.cols + i;
        if (!this.riftTile[k]) this.riftTile[k] = (this.tile[k] === 7 || this.tile[k] === 8) ? 12 : (((i * 13 + j * 7) % 5 === 0) ? 11 : 10);
      }
    }
    for (const p of this.props.concat(this.flats)) {
      if (p.swapped || p.dead) continue;
      const d = SD.dist(p.x, p.y, this.mine.x, this.mine.y);
      if (d <= R.r) {
        p.swapped = true; p.glitch = 0.6;
        const to = SD.RIFT_SWAP[p.name];
        if (to) {
          p.name = to; p.def = Object.assign({}, SD.PROPS[to] || p.def);
          if (p.def.hp) { p.hp = p.maxHp = p.def.hp; }
          if (p.def.light && !this.lights.some(L => L.prop === p)) this.lights.push({ prop: p, glow: p.def.light });
        }
        emit && emit('riftSwap', p);
      }
    }
    if (R.r >= R.max) this.nav.dirty = true;
  },
};

/* ---------- navigation: a flow field toward the player on a 16 px grid ---------- */
SD.Nav = function (world) {
  this.w = world; this.C = 16; this.cols = Math.ceil(world.w / 16); this.rows = Math.ceil(world.h / 16);
  this.block = new Uint8Array(this.cols * this.rows);
  this.dist = new Int32Array(this.cols * this.rows);
  this.q = new Int32Array(this.cols * this.rows);
  this.dirty = true; this.tx = -1; this.ty = -1;
};
SD.Nav.prototype = {
  rebuildBlocks() {
    this.block.fill(0);
    for (const p of this.w.props) {
      if (!this.w.solid(p)) continue;
      const [x0, y0, x1, y1] = this.w.box(p);
      const i0 = Math.max(0, Math.floor((x0 - 6) / 16)), i1 = Math.min(this.cols - 1, Math.floor((x1 + 6) / 16));
      const j0 = Math.max(0, Math.floor((y0 - 6) / 16)), j1 = Math.min(this.rows - 1, Math.floor((y1 + 6) / 16));
      for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) this.block[j * this.cols + i] = 1;
    }
    this.dirty = false;
  },
  update(tx, ty) {
    if (this.dirty) this.rebuildBlocks();
    const ci = SD.clamp(Math.floor(tx / 16), 0, this.cols - 1), cj = SD.clamp(Math.floor(ty / 16), 0, this.rows - 1);
    this.tx = tx; this.ty = ty;
    const D = this.dist, B = this.block, Q = this.q, C = this.cols, R = this.rows;
    D.fill(-1);
    let h = 0, t = 0; const s = cj * C + ci; D[s] = 0; Q[t++] = s;
    while (h < t) {
      const k = Q[h++], i = k % C, j = (k / C) | 0, nd = D[k] + 1;
      if (i > 0) { const n = k - 1; if (D[n] < 0 && !B[n]) { D[n] = nd; Q[t++] = n; } }
      if (i < C - 1) { const n = k + 1; if (D[n] < 0 && !B[n]) { D[n] = nd; Q[t++] = n; } }
      if (j > 0) { const n = k - C; if (D[n] < 0 && !B[n]) { D[n] = nd; Q[t++] = n; } }
      if (j < R - 1) { const n = k + C; if (D[n] < 0 && !B[n]) { D[n] = nd; Q[t++] = n; } }
    }
  },
  /* unit vector an enemy at (x, y) should walk to follow the field */
  dir(x, y) {
    const C = this.cols, R = this.rows;
    const i = SD.clamp(Math.floor(x / 16), 0, C - 1), j = SD.clamp(Math.floor(y / 16), 0, R - 1);
    const here = this.dist[j * C + i];
    let best = here < 0 ? 1e9 : here, bx = 0, by = 0;
    for (let dj = -1; dj <= 1; dj++) for (let di = -1; di <= 1; di++) {
      if (!di && !dj) continue;
      const ni = i + di, nj = j + dj; if (ni < 0 || nj < 0 || ni >= C || nj >= R) continue;
      const d = this.dist[nj * C + ni]; if (d < 0) continue;
      if (di && dj && (this.dist[j * C + ni] < 0 || this.dist[nj * C + i] < 0)) continue; // no corner cutting
      const v = d + (di && dj ? 0.41 : 0);
      if (v < best) { best = v; bx = di; by = dj; }
    }
    if (!bx && !by) { const a = SD.ang(x, y, this.tx, this.ty); return [Math.cos(a), Math.sin(a)]; }
    const m = Math.hypot(bx, by); return [bx / m, by / m];
  },
};
