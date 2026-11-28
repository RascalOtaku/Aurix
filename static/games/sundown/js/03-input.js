/* Input: keyboard + mouse, gamepad (PlayStation / Xbox via the Gamepad API) and touch twin-sticks,
   merged into one snapshot per simulation step. The sim only ever reads SD.input.sample(), so the
   bot and tests drive the game by handing it the same snapshot shape. */
'use strict';
SD.input = {
  keys: {}, pressed: {}, mouse: { x: SD.W / 2, y: SD.H / 2, down: false, right: false, moved: false },
  mode: 'kb', // 'kb' | 'pad' | 'touch'
  pad: null, padPrev: {},
  touch: { move: null, aim: null, buttons: {} },
  canvas: null, scale: 1, offX: 0, offY: 0,

  attach(canvas) {
    this.canvas = canvas;
    const K = this;
    addEventListener('keydown', e => {
      if (['Space', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Tab'].includes(e.code)) e.preventDefault();
      if (!K.keys[e.code]) K.pressed[e.code] = true;
      K.keys[e.code] = true; K.mode = 'kb';
      SD.audio && SD.audio.unlock();
    });
    addEventListener('keyup', e => { K.keys[e.code] = false; });
    addEventListener('blur', () => { K.keys = {}; K.mouse.down = K.mouse.right = false; });
    canvas.addEventListener('mousemove', e => { K.toLogical(e.clientX, e.clientY, K.mouse); K.mouse.moved = true; if (K.mode !== 'kb') K.mode = 'kb'; });
    canvas.addEventListener('mousedown', e => {
      K.toLogical(e.clientX, e.clientY, K.mouse);
      if (e.button === 0) { K.mouse.down = true; K.pressed.Mouse0 = true; }
      if (e.button === 2) { K.mouse.right = true; K.pressed.Mouse2 = true; }
      K.mode = 'kb'; SD.audio && SD.audio.unlock();
    });
    addEventListener('mouseup', e => { if (e.button === 0) K.mouse.down = false; if (e.button === 2) K.mouse.right = false; });
    canvas.addEventListener('contextmenu', e => e.preventDefault());
    canvas.addEventListener('wheel', e => { e.preventDefault(); K.pressed[e.deltaY < 0 ? 'WheelUp' : 'WheelDown'] = true; }, { passive: false });
    const tstart = e => { e.preventDefault(); K.mode = 'touch'; SD.audio && SD.audio.unlock(); for (const t of e.changedTouches) K.touchDown(t); };
    const tmove = e => { e.preventDefault(); for (const t of e.changedTouches) K.touchMove(t); };
    const tend = e => { e.preventDefault(); for (const t of e.changedTouches) K.touchUp(t); };
    canvas.addEventListener('touchstart', tstart, { passive: false });
    canvas.addEventListener('touchmove', tmove, { passive: false });
    canvas.addEventListener('touchend', tend, { passive: false });
    canvas.addEventListener('touchcancel', tend, { passive: false });
    addEventListener('gamepadconnected', () => { K.mode = 'pad'; });
  },
  toLogical(cx, cy, out) {
    const r = this.canvas.getBoundingClientRect();
    out.x = (cx - r.left) / r.width * SD.W; out.y = (cy - r.top) / r.height * SD.H; return out;
  },
  /* ---- touch: left half = move stick, right half = aim stick (auto-fire while held) ---- */
  TOUCH_BUTTONS: { dash: [SD.W - 46, SD.H - 120, 22], dyn: [SD.W - 100, SD.H - 70, 18], reload: [SD.W - 100, SD.H - 140, 16], swap: [SD.W - 150, SD.H - 110, 15], pause: [SD.W - 20, 20, 14] },
  touchDown(t) {
    const p = this.toLogical(t.clientX, t.clientY, {});
    for (const [k, b] of Object.entries(this.TOUCH_BUTTONS)) {
      if (SD.dist(p.x, p.y, b[0], b[1]) < b[2] + 6) { this.touch.buttons[t.identifier] = k; this.pressed['T_' + k] = true; return; }
    }
    const stick = { id: t.identifier, ox: p.x, oy: p.y, x: p.x, y: p.y };
    if (p.x < SD.W / 2) this.touch.move = stick; else this.touch.aim = stick;
    this.pressed.Tap = true; this.tapAt = p;
  },
  touchMove(t) {
    const p = this.toLogical(t.clientX, t.clientY, {});
    for (const s of [this.touch.move, this.touch.aim]) if (s && s.id === t.identifier) { s.x = p.x; s.y = p.y; }
  },
  touchUp(t) {
    if (this.touch.move && this.touch.move.id === t.identifier) this.touch.move = null;
    if (this.touch.aim && this.touch.aim.id === t.identifier) this.touch.aim = null;
    delete this.touch.buttons[t.identifier];
  },
  /* ---- gamepad ---- */
  readPad() {
    if (typeof navigator === 'undefined' || !navigator.getGamepads) return null;
    const pads = navigator.getGamepads(); let p = null;
    for (const q of pads) if (q && q.connected) { p = q; break; }
    if (!p) return null;
    const dz = (x, y, d) => { const m = Math.hypot(x, y); if (m < d) return [0, 0]; const k = Math.min(1, (m - d) / (1 - d)) / m; return [x * k, y * k]; };
    const b = i => !!(p.buttons[i] && (p.buttons[i].pressed || p.buttons[i].value > 0.5));
    const cur = { a: b(0), b: b(1), x: b(2), y: b(3), lb: b(4), rb: b(5), lt: b(6), rt: b(7), back: b(8), start: b(9), up: b(12), down: b(13), left: b(14), right: b(15) };
    const ls = dz(p.axes[0] || 0, p.axes[1] || 0, 0.18), rs = dz(p.axes[2] || 0, p.axes[3] || 0, 0.22);
    const edge = {}; for (const k in cur) edge[k] = cur[k] && !this.padPrev[k];
    this.padPrev = cur;
    if (Object.values(cur).some(v => v) || Math.hypot(ls[0], ls[1]) > 0 || Math.hypot(rs[0], rs[1]) > 0) this.mode = 'pad';
    return { cur, edge, ls, rs, id: p.id || '' };
  },
  rumble(strong, ms) {
    try {
      const pads = navigator.getGamepads && navigator.getGamepads(); const p = pads && [...pads].find(q => q && q.connected);
      if (p && p.vibrationActuator) p.vibrationActuator.playEffect('dual-rumble', { duration: ms || 120, strongMagnitude: strong, weakMagnitude: strong * 0.6 });
    } catch (e) { /* not supported */ }
  },

  /* One snapshot per sim step. aimScreen is the mouse position (game turns it into world space);
     aimVec is a stick direction. Edge flags are consumed here. */
  sample() {
    const K = this.keys, P = this.pressed, s = {
      mx: 0, my: 0, aimVec: null, aimScreen: null, fire: false, alt: false, focus: false,
      reload: false, dyn: false, dash: false, pause: false, interact: false,
      up: false, down: false, left: false, right: false, confirm: false, back: false, any: false, mode: this.mode, slot: 0, swap: 0,
    };
    // keyboard + mouse
    s.mx = (K.KeyD || K.ArrowRight ? 1 : 0) - (K.KeyA || K.ArrowLeft ? 1 : 0);
    s.my = (K.KeyS || K.ArrowDown ? 1 : 0) - (K.KeyW || K.ArrowUp ? 1 : 0);
    if (this.mode === 'kb') { s.aimScreen = { x: this.mouse.x, y: this.mouse.y }; s.fire = this.mouse.down; s.alt = this.mouse.right; }
    s.focus = !!(K.KeyF || K.KeyC);
    s.reload = !!P.KeyR; s.dyn = !!(P.KeyQ || P.KeyG); s.dash = !!(P.Space || P.ShiftLeft || P.ShiftRight);
    s.pause = !!(P.Escape || P.KeyP); s.interact = !!P.KeyE;
    for (let d = 1; d <= 5; d++) if (P['Digit' + d]) s.slot = d;
    if (P.WheelUp) s.swap = -1; if (P.WheelDown || P.Tab) s.swap = 1;
    s.up = !!(P.ArrowUp || P.KeyW); s.down = !!(P.ArrowDown || P.KeyS); s.left = !!(P.ArrowLeft || P.KeyA); s.right = !!(P.ArrowRight || P.KeyD);
    s.confirm = !!(P.Enter || P.Space || P.Mouse0 || P.KeyE); s.back = !!(P.Escape || P.Backspace);
    s.any = Object.keys(P).length > 0;
    // gamepad
    const g = this.readPad();
    if (g) {
      if (Math.hypot(g.ls[0], g.ls[1]) > 0) { s.mx = g.ls[0]; s.my = g.ls[1]; }
      if (this.mode === 'pad') {
        if (Math.hypot(g.rs[0], g.rs[1]) > 0.25) s.aimVec = { x: g.rs[0], y: g.rs[1] };
        s.fire = s.fire || g.cur.rt; s.alt = s.alt || g.cur.lb; s.focus = s.focus || g.cur.lt;
      }
      s.reload = s.reload || g.edge.x; s.dyn = s.dyn || g.edge.rb; s.dash = s.dash || g.edge.a;
      s.pause = s.pause || g.edge.start; if (g.edge.y) s.swap = 1;
      s.up = s.up || g.edge.up; s.down = s.down || g.edge.down; s.left = s.left || g.edge.left; s.right = s.right || g.edge.right;
      s.confirm = s.confirm || g.edge.a; s.back = s.back || g.edge.b;
      s.any = s.any || Object.values(g.edge).some(v => v);
      // stick-as-dpad for menus
      const ly = g.ls[1], lx = g.ls[0];
      if (!this._stickHeld && Math.abs(ly) > 0.6) { if (ly < 0) s.up = true; else s.down = true; this._stickHeld = true; }
      else if (!this._stickHeld && Math.abs(lx) > 0.6) { if (lx < 0) s.left = true; else s.right = true; this._stickHeld = true; }
      else if (Math.abs(ly) < 0.3 && Math.abs(lx) < 0.3) this._stickHeld = false;
    }
    // touch
    if (this.mode === 'touch') {
      const T = this.touch;
      if (T.move) { const dx = T.move.x - T.move.ox, dy = T.move.y - T.move.oy, m = Math.hypot(dx, dy); if (m > 4) { const k = Math.min(1, m / 34) / m; s.mx = dx * k; s.my = dy * k; } }
      if (T.aim) { const dx = T.aim.x - T.aim.ox, dy = T.aim.y - T.aim.oy; if (Math.hypot(dx, dy) > 6) { s.aimVec = { x: dx, y: dy }; s.fire = true; } }
      s.dash = s.dash || !!P.T_dash; s.dyn = s.dyn || !!P.T_dyn; s.reload = s.reload || !!P.T_reload; s.pause = s.pause || !!P.T_pause; if (P.T_swap) s.swap = 1;
      if (P.Tap) { s.confirm = true; s.tap = this.tapAt; }
    }
    const m = Math.hypot(s.mx, s.my); if (m > 1) { s.mx /= m; s.my /= m; }
    this.pressed = {};
    return s;
  },
};

/* An empty snapshot (bots and tests start from this). */
SD.blankInput = function () {
  return { mx: 0, my: 0, aimVec: null, aimScreen: null, fire: false, alt: false, focus: false, reload: false, dyn: false, dash: false,
    pause: false, interact: false, up: false, down: false, left: false, right: false, confirm: false, back: false, any: false, mode: 'bot', slot: 0, swap: 0 };
};
