/* Assets: loads the sheets listed in assets/manifest.js (window.SD_ASSETS), builds tinted font
   pages and glow sprites. Missing art never breaks the game: a sheet that failed to load draws
   as a coloured block, so the code runs even with no packs built. */
'use strict';
SD.assets = {
  M: null, img: {}, white: {}, ready: false, missing: [],
  makeCanvas(w, h) { // overridden in Node by the harness
    const c = document.createElement('canvas'); c.width = w; c.height = h; return c;
  },
  loadImage(src) { // overridden in Node by the harness
    return new Promise(res => { const im = new Image(); im.onload = () => res(im); im.onerror = () => res(null); im.src = src; });
  },
  async load(manifest, onProgress) {
    this.M = manifest || { sheets: {}, tiles: null, font: {} };
    const jobs = [];
    const add = (key, src, bucket) => jobs.push(this.loadImage(src).then(im => { bucket[key] = im; if (!im) this.missing.push(key); }));
    for (const [k, s] of Object.entries(this.M.sheets || {})) { add(k, s.src, this.img); if (s.white) add(k, s.white, this.white); }
    if (this.M.tiles) add('__tiles', this.M.tiles.src, this.img);
    for (const [k, f] of Object.entries(this.M.font || {})) add('__font' + k, f.src, this.img);
    let done = 0;
    await Promise.all(jobs.map(p => p.then(() => { done++; if (onProgress) onProgress(done / jobs.length); })));
    this.buildFont();
    this.buildGlows();
    this.ready = true;
  },
  sheet(name) { return this.M && this.M.sheets ? this.M.sheets[name] : null; },

  /* ---- font: Rainwire Mono, pre-tinted into a few colours (source-in) ---- */
  FONT_COLORS: { w: '#f4ecd8', y: '#ffd166', r: '#ff4d4d', c: '#3ff0ff', m: '#ff3ec8', g: '#7dff8a', k: '#14100c', d: '#9a8f7a', o: '#ff9a3c' },
  fonts: {},
  buildFont() {
    for (const scale of ['1', '2']) {
      const f = this.M.font && this.M.font[scale]; const im = this.img['__font' + scale];
      const pages = {};
      if (f && im) {
        for (const [ck, col] of Object.entries(this.FONT_COLORS)) {
          const c = this.makeCanvas(im.width, im.height); const x = c.getContext('2d');
          x.drawImage(im, 0, 0); x.globalCompositeOperation = 'source-in'; x.fillStyle = col; x.fillRect(0, 0, im.width, im.height);
          pages[ck] = c;
        }
      }
      this.fonts[scale] = { def: f, pages };
    }
  },
  /* ---- glow sprites: soft discs drawn as stacked translucent circles (no gradients needed) ---- */
  glows: {},
  buildGlows() {
    const mk = (name, r, col) => {
      const c = this.makeCanvas(r * 2, r * 2); const x = c.getContext('2d');
      const steps = 10;
      for (let i = steps; i >= 1; i--) {
        x.globalAlpha = 0.11; x.fillStyle = col; x.beginPath(); x.arc(r, r, r * i / steps, 0, Math.PI * 2); x.fill();
      }
      this.glows[name] = c;
    };
    mk('warm', 64, '#ffb347'); mk('fire', 32, '#ff7a1a'); mk('cyan', 48, '#3ff0ff'); mk('pink', 48, '#ff3ec8');
    mk('white', 24, '#ffffff'); mk('muzzle', 16, '#fff2a8'); mk('red', 40, '#ff3030');
  },
};
