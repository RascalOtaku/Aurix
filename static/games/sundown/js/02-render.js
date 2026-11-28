/* Renderer: camera, depth-sorted sprites, ground tiles, bitmap text, night lighting.
   Uses only the Canvas2D subset the Node shim implements (see games/sundown/tools/canvas-shim.js). */
'use strict';
SD.R = {
  g: null, canvas: null, light: null, lg: null,
  cam: { x: 0, y: 0, shake: 0, sx: 0, sy: 0, zoomPunch: 0 },
  queue: [],
  init(canvas) {
    this.canvas = canvas; this.g = canvas.getContext('2d'); this.g.imageSmoothingEnabled = false;
    this.light = SD.assets.makeCanvas(SD.W, SD.H); this.lg = this.light.getContext('2d');
  },
  /* ---------- camera ---------- */
  setCamera(x, y, worldW, worldH) {
    const c = this.cam;
    c.x = SD.clamp(x - SD.W / 2, 0, Math.max(0, worldW - SD.W));
    c.y = SD.clamp(y - SD.H / 2, 0, Math.max(0, worldH - SD.H));
    if (c.shake > 0) { c.sx = (SD.fx() * 2 - 1) * c.shake; c.sy = (SD.fx() * 2 - 1) * c.shake; c.shake = Math.max(0, c.shake * 0.88 - 0.05); }
    else { c.sx = c.sy = 0; }
  },
  shake(a) { this.cam.shake = Math.min(14, Math.max(this.cam.shake, a)); },
  ox() { return Math.round(-this.cam.x + this.cam.sx); },
  oy() { return Math.round(-this.cam.y + this.cam.sy); },
  onScreen(x, y, pad) { pad = pad || 96; return x > this.cam.x - pad && x < this.cam.x + SD.W + pad && y > this.cam.y - pad && y < this.cam.y + SD.H + pad * 2; },

  clear(col) { const g = this.g; g.globalAlpha = 1; g.globalCompositeOperation = 'source-over'; g.fillStyle = col || '#000'; g.fillRect(0, 0, SD.W, SD.H); },

  /* ---------- ground ---------- */
  tiles(map) { // map: {cols, rows, T, at(i,j)->tile index}
    const g = this.g, A = SD.assets, im = A.img.__tiles, T = map.T;
    const ox = this.ox(), oy = this.oy();
    const i0 = Math.max(0, Math.floor(this.cam.x / T) - 1), j0 = Math.max(0, Math.floor(this.cam.y / T) - 1);
    const i1 = Math.min(map.cols - 1, i0 + Math.ceil(SD.W / T) + 2), j1 = Math.min(map.rows - 1, j0 + Math.ceil(SD.H / T) + 2);
    for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) {
      const t = map.at(i, j);
      const x = i * T + ox, y = j * T + oy;
      if (im) g.drawImage(im, t * T, 0, T, T, x, y, T, T);
      else { g.fillStyle = t >= 10 && t <= 12 ? '#16141f' : ((i + j) & 1 ? '#96704a' : '#90694a'); g.fillRect(x, y, T, T); }
    }
  },

  /* ---------- sprites ---------- */
  // o: {alpha, flash (0..1 white), scale, lift (px above ground), shadow (radius) }
  push(y, fn) { this.queue.push({ y, fn }); },
  sprite(name, row, col, x, y, o) { this.queue.push({ y: y + ((o && o.sortBias) || 0), name, row, col, x, sy: y, o }); },
  flush() {
    const q = this.queue; q.sort((a, b) => a.y - b.y);
    for (let i = 0; i < q.length; i++) { const e = q[i]; if (e.fn) e.fn(this.g, this.ox(), this.oy()); else this.drawSprite(e.name, e.row, e.col, e.x, e.sy, e.o); }
    this.queue = [];
  },
  drawSprite(name, row, col, x, y, o) {
    o = o || {};
    const g = this.g, s = SD.assets.sheet(name), im = SD.assets.img[name];
    const sc = o.scale || 1, ox = this.ox(), oy = this.oy();
    const lift = o.lift || 0;
    if (!s || !im) { // fallback block so a missing sheet is visible, not fatal
      const w = (s ? s.bw : 20) * sc, h = (s ? s.bh : 40) * sc;
      g.globalAlpha = o.alpha == null ? 1 : o.alpha; g.fillStyle = o.fallback || '#c0399a';
      g.fillRect(Math.round(x - w / 2 + ox), Math.round(y - h - lift + oy), w, h); g.globalAlpha = 1; return;
    }
    row = ((row % s.rows) + s.rows) % s.rows; col = ((col % s.cols) + s.cols) % s.cols;
    const dw = Math.round(s.fw * sc), dh = Math.round(s.fh * sc);
    const dx = Math.round(x - s.ax * sc + ox), dy = Math.round(y - s.ay * sc - lift + oy);
    if (o.shadow) this.shadowAt(x, y, o.shadow * sc);
    g.globalAlpha = o.alpha == null ? 1 : o.alpha;
    g.drawImage(im, col * s.fw, row * s.fh, s.fw, s.fh, dx, dy, dw, dh);
    if (o.flash > 0) {
      const w = SD.assets.white[name];
      if (w) { g.globalAlpha = Math.min(1, o.flash) * (o.alpha == null ? 1 : o.alpha); g.drawImage(w, col * s.fw, row * s.fh, s.fw, s.fh, dx, dy, dw, dh); }
    }
    if (o.tint) { // additive colour pass (rift glitch, burning)
      g.globalCompositeOperation = 'lighter'; g.globalAlpha = o.tint[1];
      const w = SD.assets.white[name] || im;
      g.drawImage(w, col * s.fw, row * s.fh, s.fw, s.fh, dx + (o.tint[2] || 0), dy, dw, dh);
      g.globalCompositeOperation = 'source-over';
    }
    g.globalAlpha = 1;
  },
  shadowAt(x, y, r) {
    const g = this.g; g.globalAlpha = 0.28; g.fillStyle = '#1a0f08';
    g.beginPath(); g.ellipse(Math.round(x + this.ox()), Math.round(y + this.oy()), r, r * 0.42, 0, 0, Math.PI * 2); g.fill(); g.globalAlpha = 1;
  },
  circle(x, y, r, col, alpha, world) {
    const g = this.g; const ox = world === false ? 0 : this.ox(), oy = world === false ? 0 : this.oy();
    g.globalAlpha = alpha == null ? 1 : alpha; g.fillStyle = col; g.beginPath(); g.arc(x + ox, y + oy, Math.max(0.5, r), 0, Math.PI * 2); g.fill(); g.globalAlpha = 1;
  },
  line(x0, y0, x1, y1, col, w, alpha) {
    const g = this.g, ox = this.ox(), oy = this.oy();
    g.globalAlpha = alpha == null ? 1 : alpha; g.strokeStyle = col; g.lineWidth = w || 1;
    g.beginPath(); g.moveTo(x0 + ox, y0 + oy); g.lineTo(x1 + ox, y1 + oy); g.stroke(); g.globalAlpha = 1;
  },
  rect(x, y, w, h, col, alpha) { const g = this.g; g.globalAlpha = alpha == null ? 1 : alpha; g.fillStyle = col; g.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h)); g.globalAlpha = 1; },
  glow(name, x, y, scale, alpha, world) {
    const im = SD.assets.glows[name]; if (!im) return;
    const g = this.g, s = scale || 1, w = im.width * s, h = im.height * s;
    const ox = world === false ? 0 : this.ox(), oy = world === false ? 0 : this.oy();
    g.globalCompositeOperation = 'lighter'; g.globalAlpha = alpha == null ? 1 : alpha;
    g.drawImage(im, Math.round(x - w / 2 + ox), Math.round(y - h / 2 + oy), Math.round(w), Math.round(h));
    g.globalCompositeOperation = 'source-over'; g.globalAlpha = 1;
  },

  /* ---------- night: a darkness layer with holes cut by lights ---------- */
  darkness(col, alpha, lights) {
    if (alpha <= 0.01) return;
    const lg = this.lg, ox = this.ox(), oy = this.oy();
    lg.globalCompositeOperation = 'source-over'; lg.globalAlpha = 1;
    lg.clearRect(0, 0, SD.W, SD.H);
    lg.fillStyle = col; lg.globalAlpha = alpha; lg.fillRect(0, 0, SD.W, SD.H);
    lg.globalCompositeOperation = 'destination-out';
    for (const L of lights) {
      const im = SD.assets.glows[L.glow || 'white']; if (!im) continue;
      const s = L.r / (im.width / 2); const w = im.width * s;
      if (L.x + ox < -w || L.x + ox > SD.W + w || L.y + oy < -w || L.y + oy > SD.H + w) continue;
      lg.globalAlpha = Math.min(1, L.a == null ? 1 : L.a);
      lg.drawImage(im, Math.round(L.x - w / 2 + ox), Math.round(L.y - w / 2 + oy), Math.round(w), Math.round(w));
    }
    lg.globalCompositeOperation = 'source-over'; lg.globalAlpha = 1;
    this.g.drawImage(this.light, 0, 0);
  },

  /* ---------- text (Rainwire Mono bitmap font) ---------- */
  textWidth(str, scale) {
    const F = SD.assets.fonts[scale === 2 ? '2' : '1'];
    const adv = F && F.def ? (F.def.glyphs[65] ? F.def.glyphs[65][6] : 6) : 6 * (scale === 2 ? 2 : 1);
    return String(str).length * adv;
  },
  text(str, x, y, o) {
    o = o || {}; str = String(str);
    const scale = o.scale === 2 ? '2' : '1';
    const F = SD.assets.fonts[scale]; const g = this.g;
    const w = this.textWidth(str, o.scale);
    if (o.align === 'center') x -= w / 2; else if (o.align === 'right') x -= w;
    x = Math.round(x); y = Math.round(y);
    if (!F || !F.def || !F.pages.w) { // no font built: plain rects so layout is still visible
      g.fillStyle = '#f4ecd8'; for (let i = 0; i < str.length; i++) if (str[i] !== ' ') g.fillRect(x + i * 6, y + 2, 4, 5); return w;
    }
    const draw = (page, dx, dy) => {
      let cx = dx;
      for (let i = 0; i < str.length; i++) {
        const gl = F.def.glyphs[str.charCodeAt(i)] || F.def.glyphs[63];
        if (gl) { if (gl[2] > 0) g.drawImage(page, gl[0], gl[1], gl[2], gl[3], cx + gl[4], dy + gl[5], gl[2], gl[3]); cx += gl[6]; }
      }
    };
    g.globalAlpha = o.alpha == null ? 1 : o.alpha;
    if (o.shadow !== false) draw(F.pages.k, x + 1, y + 1);
    draw(F.pages[o.color || 'w'] || F.pages.w, x, y);
    g.globalAlpha = 1;
    return w;
  },
  panel(x, y, w, h, o) {
    o = o || {}; const g = this.g;
    g.globalAlpha = o.alpha == null ? 0.82 : o.alpha; g.fillStyle = o.bg || '#140d09'; g.fillRect(x, y, w, h);
    g.globalAlpha = 1; g.fillStyle = o.edge || '#7a5530';
    g.fillRect(x, y, w, 1); g.fillRect(x, y + h - 1, w, 1); g.fillRect(x, y, 1, h); g.fillRect(x + w - 1, y, 1, h);
    if (o.corner) { g.fillStyle = o.corner; g.fillRect(x, y, 3, 3); g.fillRect(x + w - 3, y, 3, 3); g.fillRect(x, y + h - 3, 3, 3); g.fillRect(x + w - 3, y + h - 3, 3, 3); }
  },
  bar(x, y, w, h, frac, col, back) {
    const g = this.g; g.fillStyle = back || '#2a1a12'; g.fillRect(x, y, w, h);
    g.fillStyle = col; g.fillRect(x, y, Math.round(w * SD.clamp(frac, 0, 1)), h);
  },
};
