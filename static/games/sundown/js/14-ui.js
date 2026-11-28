/* UI: the HUD, the title / controls / credits screens, pause, the Last Chance Saloon, and the
   end screens. Menus work with keyboard, mouse, gamepad and touch through the same hit list. */
'use strict';

SD.UI = {
  screen: 'title', sel: 0, hits: [], prevDown: false, shopSel: 0, shopMsg: null, quip: 0, endSel: 0, pauseSel: 0, big: {},

  /* ------------------------------------------------------------------ shared */
  openTitle() { this.screen = 'title'; this.sel = 0; },
  onStart() { this.shopSel = 0; this.pauseSel = 0; },
  openPause() { this.pauseSel = 0; },
  openShop(G) { this.shopSel = 0; this.shopMsg = null; this.quip = (G.run.wave * 7 + 3) % this.QUIPS.length; },
  openEnd() { this.endSel = 0; },

  // menu navigation shared by every screen. items: [{label, act, disabled?}]
  nav(inp, count, sel) {
    if (inp.up) { sel = (sel - 1 + count) % count; SD.audio.play('blip', 0.5); }
    if (inp.down) { sel = (sel + 1) % count; SD.audio.play('blip', 0.5); }
    return sel;
  },
  pointer(inp) { // returns {hover, click} index into this.hits, or -1
    const M = SD.input.mouse, down = M.down, click = down && !this.prevDown; this.prevDown = down;
    let hover = -1, hit = -1;
    const at = (x, y) => this.hits.findIndex(h => x >= h.x && x < h.x + h.w && y >= h.y && y < h.y + h.h);
    if (SD.input.mode === 'kb' && M.moved) { hover = at(M.x, M.y); M.moved = false; }
    if (click) hit = at(M.x, M.y);
    if (inp.tap) hit = at(inp.tap.x, inp.tap.y);
    return { hover, hit, click: click || !!inp.tap };
  },
  hitbox(x, y, w, h, i) { this.hits.push({ x, y, w, h, i }); },

  update(G, inp) {
    const st = G.state;
    if (st === 'title') this.updateTitle(G, inp);
    else if (st === 'pause') this.updatePause(G, inp);
    else if (st === 'shop') this.updateShop(G, inp);
    else if (st === 'over' || st === 'victory') this.updateEnd(G, inp);
  },
  bigText(str, x, y, color, mult, align) { // scale-2 font drawn again at 2x (or more) through a cached canvas
    const key = str + '|' + color + '|' + mult; let c = this.big[key];
    if (!c) {
      const w = SD.R.textWidth(str, 2) + 2, h = 20; c = SD.assets.makeCanvas(w, h);
      const R = SD.R, g0 = R.g; R.g = c.getContext('2d'); R.text(str, 0, 0, { scale: 2, color }); R.g = g0;
      this.big[key] = c;
    }
    const w = c.width * mult, h = c.height * mult;
    const dx = align === 'center' ? x - w / 2 : x;
    SD.R.g.drawImage(c, 0, 0, c.width, c.height, Math.round(dx), Math.round(y), w, h);
  },
  shade(a) { SD.R.rect(0, 0, SD.W, SD.H, '#07050a', a); },
  list(items, sel, x, y, o) { // draws a vertical menu and records hit boxes
    o = o || {}; const R = SD.R, lh = o.lh || 16, w = o.w || 200;
    items.forEach((it, i) => {
      const yy = y + i * lh, on = i === sel;
      if (on) { R.rect(x - 8, yy - 3, w, lh - 1, '#ffd166', 0.14); R.text('>', x - 6, yy, { color: 'y' }); }
      R.text(it.label, x + 4, yy, { color: it.disabled ? 'd' : on ? 'y' : 'w' });
      if (it.right) R.text(it.right, x + w - 14, yy, { color: it.rightColor || 'd', align: 'right' });
      this.hitbox(x - 8, yy - 3, w, lh - 1, i);
    });
  },

  /* ------------------------------------------------------------------ title, controls, credits */
  titleItems() {
    const d = SD.dailySeed(), best = k => SD.store.get(k, 0);
    return [
      { label: 'STORY: SURVIVE TO DAWN', act: 'story', right: best('best.story') ? 'BEST ' + best('best.story') : '' },
      { label: 'ENDLESS NIGHT', act: 'endless', right: best('best.endless') ? 'BEST ' + best('best.endless') : '' },
      { label: 'DAILY RIDE', act: 'daily', right: best('daily.' + d.key) ? 'TODAY ' + best('daily.' + d.key) : String(d.key).replace(/(\d{4})(\d\d)(\d\d)/, '$1-$2-$3') },
      { label: 'CONTROLS', act: 'controls' },
      { label: 'CREDITS', act: 'credits' },
    ];
  },
  updateTitle(G, inp) {
    if (this.screen !== 'title') {
      const pt = this.pointer(inp);
      if (inp.back || inp.confirm || pt.click) { this.screen = 'title'; SD.audio.play('select'); }
      return;
    }
    const items = this.titleItems();
    this.sel = this.nav(inp, items.length, this.sel);
    const pt = this.pointer(inp);
    if (pt.hover >= 0) this.sel = this.hits[pt.hover].i;
    let go = -1;
    if (pt.click) { if (pt.hit >= 0) go = this.hits[pt.hit].i; }
    else if (inp.confirm) go = this.sel;
    if (go >= 0) {
      SD.audio.unlock();
      const a = items[go].act; SD.audio.play('select');
      if (a === 'controls' || a === 'credits') { this.screen = a; return; }
      G.start(a);
    }
    if (SD.audio.on && !SD.audio.song) SD.audio.music('title');
  },
  drawTitle(G) {
    const R = SD.R; this.hits = [];
    this.shade(0.45);
    // letterbox bars
    R.rect(0, 0, SD.W, 26, '#000', 0.7); R.rect(0, SD.H - 26, SD.W, 26, '#000', 0.7);
    if (this.screen === 'controls') return this.drawControls();
    if (this.screen === 'credits') return this.drawCredits();
    const t = G.t;
    R.text('A  ZERO HOUR  STORY', SD.W / 2, 40, { align: 'center', color: 'm' });
    this.bigText('SUNDOWN', SD.W / 2 + 2, 58, 'k', 3, 'center');
    this.bigText('SUNDOWN', SD.W / 2, 56, 'o', 3, 'center');
    R.text('COLORADO TERRITORY, 1864.  THE MINE OPENS AT MIDNIGHT.', SD.W / 2, 122, { align: 'center', color: 'd' });
    R.panel(SD.W / 2 - 150, 146, 300, 98, { corner: '#ffd166' });
    this.list(this.titleItems(), this.sel, SD.W / 2 - 132, 156, { w: 276, lh: 17 });
    const tip = SD.TIPS[Math.floor(t / 6) % SD.TIPS.length];
    R.text(tip, SD.W / 2, 262, { align: 'center', color: 'd' });
    const how = SD.input.mode === 'pad' ? 'A: SELECT   D-PAD: MOVE' : SD.input.mode === 'touch' ? 'TAP TO CHOOSE' : 'ENTER / CLICK: SELECT   ARROWS: MOVE';
    R.text(how, SD.W / 2, SD.H - 18, { align: 'center', color: 'd' });
    R.text('v' + SD.VERSION, SD.W - 6, SD.H - 18, { align: 'right', color: 'd' });
    if (SD.store.get('beatStory', false)) R.text('DAWN REACHED', 6, SD.H - 18, { color: 'g' });
    if (((t * 2) | 0) % 2 && !SD.audio.on) R.text('PRESS ANY KEY FOR SOUND', SD.W / 2, 278, { align: 'center', color: 'y' });
  },
  drawControls() {
    const R = SD.R;
    R.text('CONTROLS', SD.W / 2, 36, { align: 'center', scale: 2, color: 'y' });
    const rows = [
      ['MOVE', 'WASD / ARROWS', 'LEFT STICK'], ['AIM', 'MOUSE', 'RIGHT STICK'], ['FIRE', 'LEFT CLICK', 'RT'],
      ['FAN THE HAMMER', 'RIGHT CLICK', 'LB'], ['DASH (DODGES BULLETS)', 'SPACE / SHIFT', 'A'], ['DYNAMITE', 'Q / G', 'RB'],
      ['FOCUS (SLOW TIME)', 'HOLD F / C', 'HOLD LT'], ['RELOAD', 'R', 'X'], ['SWITCH GUN', '1-5 / WHEEL / TAB', 'Y'], ['PAUSE', 'ESC / P', 'START'],
    ];
    R.text('KEYBOARD + MOUSE', 330, 70, { color: 'o' }); R.text('CONTROLLER', 490, 70, { color: 'o' });
    rows.forEach((r, i) => { const y = 88 + i * 17; R.text(r[0], 90, y); R.text(r[1], 330, y, { color: 'd' }); R.text(r[2], 490, y, { color: 'd' }); });
    R.text('TOUCH: LEFT THUMB MOVES, RIGHT THUMB AIMS AND FIRES. BUTTONS ON THE RIGHT.', SD.W / 2, 268, { align: 'center', color: 'd' });
    R.text('PLAYSTATION AND XBOX PADS BOTH WORK. PLUG ONE IN AND PRESS A BUTTON.', SD.W / 2, 282, { align: 'center', color: 'd' });
    R.text('BACK', SD.W / 2, SD.H - 18, { align: 'center', color: 'y' }); this.hitbox(0, 0, SD.W, SD.H, 0);
  },
  drawCredits() {
    const R = SD.R;
    R.text('CREDITS', SD.W / 2, 36, { align: 'center', scale: 2, color: 'y' });
    const lines = [
      ['ZERO HOUR: SUNDOWN', 'o'], ['DESIGN, CODE, SOUND AND MUSIC: CLAUDE, BUILT FOR AURIX ONE', 'w'], ['', 'w'],
      ['ART: THE FRONTIER 1864, FRONTIER SALOON AND RAINWIRE 2089 PACKS', 'w'], ['FONT: RAINWIRE MONO', 'w'], ['', 'w'],
      ['EVERY SOUND IS SYNTHESISED LIVE IN YOUR BROWSER. NO AUDIO FILES.', 'd'],
      ['NO NETWORK REQUESTS. NO TRACKING. RUNS FROM A FOLDER.', 'd'], ['', 'w'],
      ['RUNS PLAYED: ' + SD.store.get('runs', 0), 'c'],
    ];
    lines.forEach((l, i) => R.text(l[0], SD.W / 2, 80 + i * 16, { align: 'center', color: l[1] }));
    R.text('BACK', SD.W / 2, SD.H - 18, { align: 'center', color: 'y' }); this.hitbox(0, 0, SD.W, SD.H, 0);
  },

  /* ------------------------------------------------------------------ pause */
  pauseItems() {
    const v = SD.audio.vol, pct = x => Math.round(x * 100) + '%';
    return [{ label: 'RESUME', act: 'resume' }, { label: 'MUSIC', act: 'music', right: '< ' + pct(v.music) + ' >' }, { label: 'SOUND', act: 'sfx', right: '< ' + pct(v.sfx) + ' >' },
      { label: 'RESTART RUN', act: 'restart' }, { label: 'QUIT TO TITLE', act: 'quit' }];
  },
  updatePause(G, inp) {
    const items = this.pauseItems();
    if (inp.pause || inp.back) { G.state = 'play'; SD.audio.play('select'); return; }
    this.pauseSel = this.nav(inp, items.length, this.pauseSel);
    const pt = this.pointer(inp); if (pt.hover >= 0) this.pauseSel = this.hits[pt.hover].i;
    const it = items[this.pauseSel];
    const vol = (k, d) => { const v = {}; v[k] = SD.clamp(Math.round((SD.audio.vol[k] + d) * 10) / 10, 0, 1); SD.audio.setVol(v); SD.audio.play('blip'); };
    if (it.act === 'music' || it.act === 'sfx') { const k = it.act === 'music' ? 'music' : 'sfx'; if (inp.left) vol(k, -0.1); if (inp.right) vol(k, 0.1); }
    let go = -1; if (pt.click) { if (pt.hit >= 0) go = this.hits[pt.hit].i; } else if (inp.confirm) go = this.pauseSel;
    if (go < 0) return;
    const a = items[go].act; SD.audio.play('select');
    if (a === 'resume') G.state = 'play';
    else if (a === 'music' || a === 'sfx') vol(a === 'music' ? 'music' : 'sfx', SD.audio.vol[a === 'music' ? 'music' : 'sfx'] >= 1 ? -1 : 0.1);
    else if (a === 'restart') G.start(G.mode);
    else if (a === 'quit') G.toTitle();
  },
  drawPause() {
    const R = SD.R; this.hits = [];
    this.shade(0.6);
    R.text('PAUSED', SD.W / 2, 92, { align: 'center', scale: 2, color: 'y' });
    R.panel(SD.W / 2 - 110, 124, 220, 96, { corner: '#ffd166' });
    this.list(this.pauseItems(), this.pauseSel, SD.W / 2 - 94, 134, { w: 196, lh: 17 });
    R.text(SD.TIPS[(SD.Game.run.wave + 2) % SD.TIPS.length], SD.W / 2, 240, { align: 'center', color: 'd' });
  },

  /* ------------------------------------------------------------------ the Last Chance Saloon */
  QUIPS: [
    '"Drink up. Nobody pays a tab after midnight."',
    '"That Reynolds fella owes me six dollars and a window."',
    '"Mine\'s been humming since sundown. Never hums."',
    '"Lead\'s cheap. Coffins ain\'t."',
    '"Saw a man walk out of the mine glowing. Didn\'t like it."',
    '"Whatever comes out of that hole, shoot it twice."',
    '"Don\'t touch the piano. It bites."',
    '"Dawn\'s a long way off, partner."',
  ],
  shopRows(R) {
    const rows = SD.SHOP.map(it => ({ it, why: R.canBuy(it), price: R.shopPrice(it), own: R.owned(it.id) }));
    rows.push({ leave: true });
    return rows;
  },
  updateShop(G, inp) {
    const R = G.run, rows = this.shopRows(R);
    this.shopSel = this.nav(inp, rows.length, this.shopSel);
    if (inp.back) { this.shopSel = rows.length - 1; SD.audio.play('blip'); }
    const pt = this.pointer(inp); if (pt.hover >= 0) this.shopSel = this.hits[pt.hover].i;
    let go = -1; if (pt.click) { if (pt.hit >= 0) go = this.hits[pt.hit].i; } else if (inp.confirm) go = this.shopSel;
    if (go < 0) return;
    this.shopSel = go;
    const row = rows[go];
    if (row.leave) { SD.audio.play('select'); G.state = 'play'; R.leaveShop(); return; }
    if (R.buy(row.it)) this.shopMsg = { text: 'BOUGHT ' + row.it.name, color: 'g', t: G.t };
    else this.shopMsg = { text: row.why || 'CAN\'T', color: 'r', t: G.t };
  },
  drawSaloon(G) {
    const R = SD.R, c = R.cam, sv = { x: c.x, y: c.y, sx: c.sx, sy: c.sy };
    c.x = c.y = c.sx = c.sy = 0;
    const t = G.t, rift = G.run.isRift();
    R.clear('#1c100a');
    // floor planks and back wall
    for (let y = 150; y < SD.H + 60; y += 46) for (let x = 0; x < 340; x += 92) R.drawSprite('FloorSection', 0, 0, x, y, {});
    for (let x = 0; x < 340; x += 58) R.drawSprite('WallSection', 0, 0, x, 130, {});
    R.drawSprite('WallSconce', 0, 0, 40, 92, {}); R.drawSprite('WallSconce', 0, 0, 250, 92, {});
    R.drawSprite('BackBar', 0, 0, 120, 150, {});
    R.drawSprite('KegStand', 1, 0, 214, 160, {});
    const wipe = SD.assets.sheet('BartenderWipe');
    R.drawSprite('BartenderWipe', 0, wipe ? Math.floor(t * 8) % wipe.cols : 0, 118, 186, {});
    R.drawSprite('BarCounter', 0, 0, 120, 222, {});
    for (const x of [60, 112, 164]) R.drawSprite('BarStool', 4, 0, x, 252, {});
    R.drawSprite('Piano', 1, 0, 34, 318, {});
    R.drawSprite('CardTable', 0, 0, 226, 300, {}); R.drawSprite('Chair', 2, 0, 196, 304, {}); R.drawSprite('OverturnedChair', 3, 0, 262, 318, {});
    R.drawSprite('WhiskeyCrate', 1, 0, 290, 220, {});
    // warm light, and after midnight a neon sign someone hung that nobody remembers hanging
    R.darkness('#0a0612', 0.35, [{ x: 120, y: 120, r: 140, a: 1 }, { x: 40, y: 80, r: 60 }, { x: 250, y: 80, r: 60 }, { x: 226, y: 270, r: 90, a: 0.8 }]);
    for (const x of [70, 180]) { R.drawSprite('HangingLamp', 0, 0, x, 70, {}); R.glow('warm', x, 66, 0.9, 0.5 + 0.08 * Math.sin(t * 3 + x)); }
    R.glow('warm', 40, 82, 0.4, 0.6); R.glow('warm', 250, 82, 0.4, 0.6);
    if (rift) { R.drawSprite('NeonBarSign', 0, 0, 120, 70, {}); R.glow('pink', 120, 50, 1.1, 0.35 + 0.15 * Math.sin(t * 9)); }
    Object.assign(c, sv);
  },
  drawShop(G) {
    const R = SD.R, Run = G.run, p = Run.p, t = G.t; this.hits = [];
    this.drawSaloon(G);
    R.text('THE LAST CHANCE SALOON', 160, 16, { align: 'center', scale: 2, color: 'o' });
    R.text(this.QUIPS[this.quip], 160, SD.H - 22, { align: 'center', color: 'd' });
    // stock list
    const X = 334, Y = 10, Wd = 298;
    R.panel(X, Y, Wd, SD.H - 20, { corner: '#ffd166', alpha: 0.9 });
    R.text(SD.fmtMoney(p.money), X + 10, Y + 8, { scale: 2, color: 'y' });
    R.text('HP ' + Math.ceil(p.hp) + '/' + p.maxHp + '   TNT ' + p.dynamite, X + Wd - 10, Y + 8, { align: 'right', color: 'w' });
    R.text('NEXT: ' + SD.fmtClock(((Run.mode !== 'endless' || Run.wave < 10) ? (SD.WAVES[Run.wave] ? SD.WAVES[Run.wave].hour : Run.wave) : Run.wave) * 60) + '  //  WAVE ' + (Run.wave + 1), X + Wd - 10, Y + 20, { align: 'right', color: 'd' });
    const rows = this.shopRows(Run);
    rows.forEach((r, i) => {
      const y = Y + 40 + i * 16, on = i === this.shopSel;
      if (on) R.rect(X + 4, y - 3, Wd - 8, 15, '#ffd166', 0.14);
      this.hitbox(X + 4, y - 3, Wd - 8, 15, i);
      if (r.leave) { R.text((on ? '> ' : '  ') + 'HEAD BACK OUT', X + 10, y, { color: on ? 'g' : 'w' }); return; }
      const col = r.why === 'SOLD OUT' ? 'd' : r.why === 'AFTER MIDNIGHT' ? 'm' : r.why ? (on ? 'w' : 'd') : on ? 'y' : 'w';
      let name = (on ? '> ' : '  ') + r.it.name;
      if (r.it.max > 1 && r.it.max < 50 && r.own) name += ' ' + 'I'.repeat(r.own);
      R.text(name, X + 10, y, { color: col });
      const right = r.why === 'SOLD OUT' ? 'OWNED' : r.why === 'AFTER MIDNIGHT' ? 'AFTER 12' : SD.fmtMoney(r.price);
      R.text(right, X + Wd - 10, y, { align: 'right', color: r.why === 'NOT ENOUGH' ? 'r' : r.why ? 'd' : 'y' });
    });
    const cur = rows[this.shopSel];
    const by = Y + 40 + rows.length * 16 + 4;
    R.rect(X + 8, by, Wd - 16, 1, '#7a5530');
    if (cur && !cur.leave) {
      R.text(cur.it.desc, X + 10, by + 8, { color: 'w' });
      if (cur.why && cur.why !== 'SOLD OUT') R.text(cur.why === 'AFTER MIDNIGHT' ? 'ONLY SOLD AFTER THE RIFT OPENS' : cur.why, X + 10, by + 22, { color: 'r' });
    } else R.text('THE NIGHT AIN\'T OVER.', X + 10, by + 8, { color: 'w' });
    if (this.shopMsg && t - this.shopMsg.t < 1.6) R.text(this.shopMsg.text, X + Wd - 10, by + 22, { align: 'right', color: this.shopMsg.color });
    const how = SD.input.mode === 'pad' ? 'A: BUY   B: LEAVE ROW' : SD.input.mode === 'touch' ? 'TAP TO BUY' : 'ENTER / CLICK: BUY   ESC: LEAVE ROW';
    R.text(how, X + Wd / 2, SD.H - 26, { align: 'center', color: 'd' });
  },

  /* ------------------------------------------------------------------ end screens */
  updateEnd(G, inp) {
    const items = [{ act: 'again' }, { act: 'title' }];
    if (inp.left || inp.up) this.endSel = 0; if (inp.right || inp.down) this.endSel = 1;
    const pt = this.pointer(inp); if (pt.hover >= 0) this.endSel = this.hits[pt.hover].i;
    let go = -1; if (pt.click) { if (pt.hit >= 0) go = this.hits[pt.hit].i; } else if (inp.confirm) go = this.endSel;
    if (go < 0) return;
    SD.audio.play('select');
    if (items[go].act === 'again') G.start(G.mode); else G.toTitle();
  },
  drawEnd(G) {
    const R = SD.R, r = G.lastResult; this.hits = [];
    if (!r) return;
    this.shade(r.won ? 0.4 : 0.62);
    if (r.won) { R.rect(0, 0, SD.W, SD.H, '#ffb35a', 0.12); }
    if (r.won) { this.bigText('DAWN', SD.W / 2, 34, 'y', 3, 'center'); R.text(r.mode === 'endless' ? 'THE NIGHT FINALLY ENDS' : 'YOU HELD SUNDOWN UNTIL SUNRISE', SD.W / 2, 96, { align: 'center', color: 'w' }); }
    else { this.bigText('SHOT DEAD', SD.W / 2, 34, 'r', 3, 'center'); R.text('AT ' + SD.fmtClock(r.clock) + ', WAVE ' + r.wave + (r.mode === 'daily' ? '  //  DAILY RIDE' : ''), SD.W / 2, 96, { align: 'center', color: 'w' }); }
    R.panel(SD.W / 2 - 140, 116, 280, 132, { corner: r.won ? '#ffd166' : '#ff4d4d' });
    const st = [['SCORE', r.score + (r.newBest ? '  NEW BEST!' : ''), r.newBest ? 'y' : 'w'], ['BEST', r.best, 'd'], ['WAVES', r.wave, 'w'], ['OUTLAWS DOWN', r.kills, 'w'],
      ['BEST COMBO', 'x' + r.bestCombo, 'w'], ['ACCURACY', r.acc + '%', 'w'], ['BOUNTY EARNED', SD.fmtMoney(r.bounty), 'y']];
    st.forEach((s, i) => { const y = 126 + i * 16; R.text(s[0], SD.W / 2 - 124, y, { color: 'd' }); R.text(String(s[1]), SD.W / 2 + 124, y, { align: 'right', color: s[2] }); });
    const opts = ['RIDE AGAIN', 'TITLE'];
    opts.forEach((o, i) => {
      const x = SD.W / 2 + (i ? 20 : -150), y = 268, on = i === this.endSel;
      R.panel(x, y, 130, 22, { edge: on ? '#ffd166' : '#7a5530' });
      R.text(o, x + 65, y + 7, { align: 'center', color: on ? 'y' : 'w' });
      this.hitbox(x, y, 130, 22, i);
    });
  },

  /* ------------------------------------------------------------------ HUD */
  hud(G, Run) {
    const R = SD.R, p = Run.p, t = G.t, w = SD.WEAPONS[p.cur];
    // health
    R.panel(6, 6, 124, 34, { alpha: 0.6 });
    R.text('HP', 11, 10, { color: p.hp < 30 && ((t * 4) | 0) % 2 ? 'r' : 'w' });
    R.bar(28, 11, 96, 7, p.hp / p.maxHp, p.hp < 30 ? '#ff4d4d' : '#e8483a', '#2a1210');
    R.rect(28, 11, Math.round(96 * p.hp / p.maxHp), 2, '#ff9a8a', 0.6);
    R.text('FO', 11, 23, { color: p.focusing ? 'c' : 'd' });
    R.bar(28, 25, 96, 4, p.focus / 100, '#3ff0ff', '#0e2228');
    for (let i = 0; i < p.dynamite; i++) R.rect(28 + i * 8, 32, 6, 4, '#b8261a');
    // ammo and gun
    const mag = Run.magSize(p.cur), ay = SD.H - 34;
    R.panel(6, ay, 150, 28, { alpha: 0.6 });
    R.text(w.name, 12, ay + 5, { color: w.rift ? 'c' : 'o' });
    if (p.reloadT > 0) { const full = w.reload * Math.pow(0.75, p.up.quick); R.bar(12, ay + 18, 138, 4, 1 - p.reloadT / full, '#ffd166', '#2a1a12'); R.text('RELOADING', 150, ay + 5, { align: 'right', color: 'd' }); }
    else {
      const pw = Math.max(2, Math.min(6, Math.floor(138 / Math.max(1, mag)) - 2));
      for (let i = 0; i < mag; i++) R.rect(12 + i * (pw + 2), ay + 17, pw, 6, i < p.ammo[p.cur] ? (w.rift ? '#3ff0ff' : '#ffd166') : '#3a2a1a');
      if (p.ammo[p.cur] === 0) R.text('R: RELOAD', 150, ay + 5, { align: 'right', color: 'r' });
    }
    const own = SD.WEAPON_ORDER.filter(k => p.weapons[k]);
    if (own.length > 1) own.forEach((k, i) => R.text(String(SD.WEAPONS[k].slot), 164 + i * 10, ay + 17, { color: k === p.cur ? 'y' : 'd' }));
    // clock and wave
    const clk = SD.fmtClock(Run.clock);
    R.text(clk, SD.W / 2, 6, { align: 'center', scale: 2, color: Run.isRift() ? 'm' : 'y' });
    const left = Run.queue.length + Run.enemies.filter(e => !e.dead).length + (Run.bossPending ? 1 : 0);
    const wl = Run.mode === 'endless' ? 'WAVE ' + Run.wave : 'WAVE ' + Run.wave + '/10';
    R.text(wl + (Run.phase === 'wave' ? '  //  ' + left + ' LEFT' : ''), SD.W / 2, 27, { align: 'center', color: 'd' });
    // money, score, combo
    R.text(SD.fmtMoney(p.money), SD.W - 8, 6, { align: 'right', scale: 2, color: 'y' });
    R.text(String(Run.score), SD.W - 8, 27, { align: 'right', color: 'w' });
    if (Run.combo >= 2) { const m = 1 + Math.min(4, Math.floor(Run.combo / 5) * 0.5); R.text('x' + Run.combo + ' COMBO' + (m > 1 ? '  ' + m + 'X' : ''), SD.W - 8, 39, { align: 'right', color: Run.combo >= 10 ? 'm' : 'o', alpha: Math.min(1, Run.comboT) }); }
    // boss bar
    const b = Run.boss;
    if (b && !b.dead) {
      const bw = 260, bx = SD.W / 2 - bw / 2, by = 52;
      R.text(b.d.title, SD.W / 2, by - 12, { align: 'center', color: 'r' });
      R.bar(bx, by, bw, 6, b.hp / b.maxHp, b.d.rift ? '#ff3ec8' : '#c0392b', '#1a0a0a');
      for (const f of [1 / 3, 2 / 3]) R.rect(bx + Math.round(bw * f), by, 1, 6, '#000', 0.7);
    }
    // off-screen markers
    for (const e of Run.enemies) {
      if (e.dead || e.spawnT > 0) continue;
      const sx = e.x - R.cam.x, sy = e.y - 20 - R.cam.y;
      if (sx >= 0 && sx < SD.W && sy >= 0 && sy < SD.H) continue;
      const cx = SD.clamp(sx, 8, SD.W - 8), cy = SD.clamp(sy, 50, SD.H - 44), s = e.boss ? 5 : 3;
      R.rect(cx - s / 2, cy - s / 2, s, s, e.boss ? '#ff4d4d' : e.d.rift ? '#ff3ec8' : '#ff9a3c', 0.85);
    }
    // touch buttons
    if (SD.input.mode === 'touch') {
      const lab = { dash: 'DASH', dyn: 'TNT', reload: 'R', swap: 'GUN', pause: 'II' };
      for (const [k, v] of Object.entries(SD.input.TOUCH_BUTTONS)) { R.circle(v[0], v[1], v[2], '#000', 0.35, false); R.circle(v[0], v[1], v[2] - 2, '#ffd166', 0.12, false); R.text(lab[k], v[0], v[1] - 4, { align: 'center', color: 'w', alpha: 0.85 }); }
      const T = SD.input.touch;
      for (const s of [T.move, T.aim]) if (s) { R.circle(s.ox, s.oy, 26, '#fff', 0.1, false); R.circle(s.x, s.y, 10, '#fff', 0.25, false); }
    }
  },

  /* ------------------------------------------------------------------ overlay: banners, vignettes, crosshair */
  drawOverlay(G) {
    const R = SD.R, t = G.t;
    if (G.hurtV > 0) { const a = G.hurtV * 0.5; R.rect(0, 0, SD.W, 14, '#ff1a1a', a); R.rect(0, SD.H - 14, SD.W, 14, '#ff1a1a', a); R.rect(0, 0, 14, SD.H, '#ff1a1a', a); R.rect(SD.W - 14, 0, 14, SD.H, '#ff1a1a', a); }
    if (G.focusV > 0) { const a = G.focusV * 0.25; R.rect(0, 0, SD.W, 22, '#3ff0ff', a * 0.6); R.rect(0, SD.H - 22, SD.W, 22, '#3ff0ff', a * 0.6); R.rect(0, 0, SD.W, SD.H, '#103040', a * 0.5); }
    const B = G.banner;
    if (B && (G.state === 'play' || G.state === 'pause')) {
      const a = Math.min(1, B.t * 4, (B.dur - B.t) * 3);
      const slide = Math.round((1 - SD.ease.out(Math.min(1, B.t * 3))) * 30);
      R.rect(0, 112, SD.W, 52, '#000', 0.45 * a);
      R.text(B.title, SD.W / 2 - slide, 120, { align: 'center', scale: 2, color: B.color, alpha: a });
      R.text(B.sub, SD.W / 2 + slide, 146, { align: 'center', color: B.subColor, alpha: a });
    }
    if (G.whiteout > 0) R.rect(0, 0, SD.W, SD.H, '#fff6e0', Math.min(0.85, G.whiteout));
    if (G.state === 'play' && SD.input.mode === 'kb') {
      const m = SD.input.mouse, x = Math.round(m.x), y = Math.round(m.y), gap = 3 + Math.round(G.run.p.fireT > 0 ? 2 : 0);
      for (const [dx, dy, w, h] of [[-gap - 4, 0, 4, 1], [gap + 1, 0, 4, 1], [0, -gap - 4, 1, 4], [0, gap + 1, 1, 4]]) { R.rect(x + dx + 1, y + dy + 1, w, h, '#000', 0.6); R.rect(x + dx, y + dy, w, h, '#fff6d8'); }
      R.rect(x, y, 1, 1, '#ff4d4d');
    }
    void t;
  },
};
