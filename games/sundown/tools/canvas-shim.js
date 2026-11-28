// A small software Canvas2D for Node, so the game's real draw code can render frames headlessly
// (tests, screenshots, the demo GIF) with no browser. It covers exactly the subset the game uses:
//   fillRect, clearRect, drawImage (3/5/9 args, nearest sampling), beginPath/arc/rect/moveTo/lineTo,
//   fill, stroke (lineWidth), save/restore, translate, scale, setTransform, globalAlpha,
//   globalCompositeOperation: source-over | lighter | source-in | destination-out, fillStyle/strokeStyle
//   as #rgb/#rrggbb/rgb()/rgba().  No rotation, no text (the game draws its own bitmap font).
'use strict';
const fs = require('fs');
const { PNG } = require('pngjs');

function parseColor(s) {
  if (typeof s !== 'string') return [255, 0, 255, 1];
  s = s.trim();
  if (s[0] === '#') {
    if (s.length === 4) return [parseInt(s[1] + s[1], 16), parseInt(s[2] + s[2], 16), parseInt(s[3] + s[3], 16), 1];
    if (s.length === 7) return [parseInt(s.slice(1, 3), 16), parseInt(s.slice(3, 5), 16), parseInt(s.slice(5, 7), 16), 1];
    if (s.length === 9) return [parseInt(s.slice(1, 3), 16), parseInt(s.slice(3, 5), 16), parseInt(s.slice(5, 7), 16), parseInt(s.slice(7, 9), 16) / 255];
  }
  const m = s.match(/rgba?\(([^)]+)\)/);
  if (m) {
    const p = m[1].split(',').map(x => parseFloat(x));
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  }
  return [255, 0, 255, 1];
}

class Ctx {
  constructor(canvas) {
    this.canvas = canvas;
    this._s = [];
    this.globalAlpha = 1;
    this.globalCompositeOperation = 'source-over';
    this.fillStyle = '#000';
    this.strokeStyle = '#000';
    this.lineWidth = 1;
    this.imageSmoothingEnabled = false;
    this._t = [1, 1, 0, 0]; // sx, sy, tx, ty
    this._path = [];
  }
  save() { this._s.push([this.globalAlpha, this.globalCompositeOperation, this.fillStyle, this.strokeStyle, this.lineWidth, this._t.slice()]); }
  restore() { const s = this._s.pop(); if (!s) return; [this.globalAlpha, this.globalCompositeOperation, this.fillStyle, this.strokeStyle, this.lineWidth, this._t] = s; }
  translate(x, y) { this._t[2] += x * this._t[0]; this._t[3] += y * this._t[1]; }
  scale(x, y) { this._t[0] *= x; this._t[1] *= y; }
  setTransform(a, b, c, d, e, f) { this._t = [a, d, e, f]; }
  resetTransform() { this._t = [1, 1, 0, 0]; }
  _px(x, y, r, g, b, a) {
    const W = this.canvas.width, H = this.canvas.height;
    if (x < 0 || y < 0 || x >= W || y >= H) return;
    const d = this.canvas.data, i = (y * W + x) * 4;
    const op = this.globalCompositeOperation;
    if (op === 'lighter') {
      d[i] = Math.min(255, d[i] + r * a); d[i + 1] = Math.min(255, d[i + 1] + g * a); d[i + 2] = Math.min(255, d[i + 2] + b * a);
      d[i + 3] = Math.min(255, d[i + 3] + 255 * a);
      return;
    }
    if (op === 'destination-out') { d[i + 3] = d[i + 3] * (1 - a); return; }
    if (op === 'source-in') {
      const da = d[i + 3] / 255; d[i] = r; d[i + 1] = g; d[i + 2] = b; d[i + 3] = 255 * a * da; return;
    }
    const da = d[i + 3] / 255, oa = a + da * (1 - a);
    if (oa <= 0) return;
    d[i] = (r * a + d[i] * da * (1 - a)) / oa;
    d[i + 1] = (g * a + d[i + 1] * da * (1 - a)) / oa;
    d[i + 2] = (b * a + d[i + 2] * da * (1 - a)) / oa;
    d[i + 3] = oa * 255;
  }
  _rect(x, y, w, h, col) {
    const [sx, sy, tx, ty] = this._t;
    let x0 = Math.round(x * sx + tx), y0 = Math.round(y * sy + ty), x1 = Math.round((x + w) * sx + tx), y1 = Math.round((y + h) * sy + ty);
    if (x0 > x1) [x0, x1] = [x1, x0]; if (y0 > y1) [y0, y1] = [y1, y0];
    x0 = Math.max(0, x0); y0 = Math.max(0, y0); x1 = Math.min(this.canvas.width, x1); y1 = Math.min(this.canvas.height, y1);
    const [r, g, b, a] = col; const A = a * this.globalAlpha;
    if (A <= 0 && this.globalCompositeOperation !== 'source-in') return;
    for (let yy = y0; yy < y1; yy++) for (let xx = x0; xx < x1; xx++) this._px(xx, yy, r, g, b, A);
  }
  fillRect(x, y, w, h) { this._rect(x, y, w, h, parseColor(this.fillStyle)); }
  clearRect(x, y, w, h) {
    const W = this.canvas.width, d = this.canvas.data;
    const [sx, sy, tx, ty] = this._t;
    const x0 = Math.max(0, Math.round(x * sx + tx)), y0 = Math.max(0, Math.round(y * sy + ty));
    const x1 = Math.min(W, Math.round((x + w) * sx + tx)), y1 = Math.min(this.canvas.height, Math.round((y + h) * sy + ty));
    for (let yy = y0; yy < y1; yy++) d.fill(0, (yy * W + x0) * 4, (yy * W + x1) * 4);
  }
  drawImage(img, a, b, c, d, e, f, g, h) {
    if (!img || !img.width) return;
    let sx = 0, sy = 0, sw = img.width, sh = img.height, dx, dy, dw, dh;
    if (arguments.length === 3) { dx = a; dy = b; dw = sw; dh = sh; }
    else if (arguments.length === 5) { dx = a; dy = b; dw = c; dh = d; }
    else { sx = a; sy = b; sw = c; sh = d; dx = e; dy = f; dw = g; dh = h; }
    const src = img.data || (img._canvas && img._canvas.data);
    if (!src || sw <= 0 || sh <= 0) return;
    const [tsx, tsy, ttx, tty] = this._t;
    let X0 = dx * tsx + ttx, Y0 = dy * tsy + tty, X1 = (dx + dw) * tsx + ttx, Y1 = (dy + dh) * tsy + tty;
    const flipX = X1 < X0; if (flipX) [X0, X1] = [X1, X0];
    const flipY = Y1 < Y0; if (flipY) [Y0, Y1] = [Y1, Y0];
    const px0 = Math.max(0, Math.round(X0)), py0 = Math.max(0, Math.round(Y0));
    const px1 = Math.min(this.canvas.width, Math.round(X1)), py1 = Math.min(this.canvas.height, Math.round(Y1));
    const iw = img.width, GA = this.globalAlpha;
    for (let py = py0; py < py1; py++) {
      let v = (py + 0.5 - Y0) / (Y1 - Y0); if (flipY) v = 1 - v;
      const syy = Math.min(img.height - 1, Math.floor(sy + v * sh));
      for (let px = px0; px < px1; px++) {
        let u = (px + 0.5 - X0) / (X1 - X0); if (flipX) u = 1 - u;
        const sxx = Math.min(iw - 1, Math.floor(sx + u * sw));
        const i = (syy * iw + sxx) * 4;
        const A = src[i + 3] / 255 * GA;
        if (A <= 0 && this.globalCompositeOperation !== 'source-in') continue;
        this._px(px, py, src[i], src[i + 1], src[i + 2], A);
      }
    }
  }
  beginPath() { this._path = []; }
  closePath() {}
  moveTo(x, y) { this._path.push(['m', x, y]); }
  lineTo(x, y) { this._path.push(['l', x, y]); }
  rect(x, y, w, h) { this._path.push(['r', x, y, w, h]); }
  arc(x, y, r) { this._path.push(['c', x, y, r]); }
  ellipse(x, y, rx, ry) { this._path.push(['e', x, y, rx, ry]); }
  fill() {
    const col = parseColor(this.fillStyle);
    const [sx, sy, tx, ty] = this._t;
    for (const p of this._path) {
      if (p[0] === 'r') this._rect(p[1], p[2], p[3], p[4], col);
      else if (p[0] === 'c' || p[0] === 'e') {
        const cx = p[1] * sx + tx, cy = p[2] * sy + ty, rx = Math.abs(p[3] * sx), ry = Math.abs((p[0] === 'e' ? p[4] : p[3]) * sy);
        const A = col[3] * this.globalAlpha;
        for (let y = Math.floor(cy - ry); y <= Math.ceil(cy + ry); y++) {
          for (let x = Math.floor(cx - rx); x <= Math.ceil(cx + rx); x++) {
            const dx = (x + 0.5 - cx) / rx, dy = (y + 0.5 - cy) / ry;
            if (dx * dx + dy * dy <= 1) this._px(x, y, col[0], col[1], col[2], A);
          }
        }
      }
    }
  }
  stroke() {
    const col = parseColor(this.strokeStyle);
    const [sx, sy, tx, ty] = this._t;
    const A = col[3] * this.globalAlpha, lw = Math.max(1, this.lineWidth * Math.abs(sx));
    let cx = 0, cy = 0;
    for (const p of this._path) {
      if (p[0] === 'm') { cx = p[1] * sx + tx; cy = p[2] * sy + ty; }
      else if (p[0] === 'l') {
        const nx = p[1] * sx + tx, ny = p[2] * sy + ty;
        const n = Math.max(1, Math.ceil(Math.hypot(nx - cx, ny - cy)));
        const seen = new Set();
        for (let k = 0; k <= n; k++) {
          const x = cx + (nx - cx) * k / n, y = cy + (ny - cy) * k / n;
          const h = lw / 2;
          for (let yy = Math.round(y - h); yy < Math.round(y + h) || yy === Math.round(y - h); yy++)
            for (let xx = Math.round(x - h); xx < Math.round(x + h) || xx === Math.round(x - h); xx++) {
              const key = yy * 100000 + xx; if (seen.has(key)) continue; seen.add(key);
              this._px(xx, yy, col[0], col[1], col[2], A);
            }
        }
        cx = nx; cy = ny;
      } else if (p[0] === 'c') {
        const r = p[3] * Math.abs(sx), ox = p[1] * sx + tx, oy = p[2] * sy + ty;
        const n = Math.max(8, Math.ceil(r * 6));
        for (let k = 0; k < n; k++) {
          const t = k / n * Math.PI * 2;
          this._px(Math.round(ox + Math.cos(t) * r), Math.round(oy + Math.sin(t) * r), col[0], col[1], col[2], A);
        }
      }
    }
  }
  // gradients are not supported: the game bakes glow sprites instead
  createImageData(w, h) { return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }; }
}

class Canvas {
  constructor(w = 300, h = 150) { this._w = w; this._h = h; this.data = new Uint8ClampedArray(w * h * 4); this._ctx = null; this.style = {}; }
  get width() { return this._w; }
  set width(v) { this._w = v | 0; this.data = new Uint8ClampedArray(this._w * this._h * 4); }
  get height() { return this._h; }
  set height(v) { this._h = v | 0; this.data = new Uint8ClampedArray(this._w * this._h * 4); }
  getContext() { return this._ctx || (this._ctx = new Ctx(this)); }
  addEventListener() {}
  getBoundingClientRect() { return { left: 0, top: 0, width: this._w, height: this._h }; }
  toPNG(file, scale = 1) {
    const W = this._w * scale, H = this._h * scale;
    const png = new PNG({ width: W, height: H });
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const si = ((y / scale | 0) * this._w + (x / scale | 0)) * 4, di = (y * W + x) * 4;
      png.data[di] = this.data[si]; png.data[di + 1] = this.data[si + 1]; png.data[di + 2] = this.data[si + 2]; png.data[di + 3] = 255;
    }
    fs.writeFileSync(file, PNG.sync.write(png));
  }
  toRGB() { // raw rgb24 for ffmpeg
    const out = Buffer.alloc(this._w * this._h * 3);
    for (let i = 0, j = 0; i < this.data.length; i += 4, j += 3) { out[j] = this.data[i]; out[j + 1] = this.data[i + 1]; out[j + 2] = this.data[i + 2]; }
    return out;
  }
}

function loadPNG(file) {
  const png = PNG.sync.read(fs.readFileSync(file));
  return { width: png.width, height: png.height, data: png.data, complete: true, naturalWidth: png.width };
}

module.exports = { Canvas, Ctx, loadPNG, parseColor };
