/* Boot: size the canvas, load the art, then run the game loop. */
'use strict';
(function () {
  const canvas = document.getElementById('game');
  canvas.width = SD.W; canvas.height = SD.H;
  const fit = () => {
    const s = Math.min(innerWidth / SD.W, innerHeight / SD.H);
    const k = s >= 1 ? Math.max(1, Math.floor(s * 4) / 4) : s; // quarter steps keep pixels crisp
    canvas.style.width = Math.floor(SD.W * k) + 'px'; canvas.style.height = Math.floor(SD.H * k) + 'px';
  };
  addEventListener('resize', fit); fit();
  SD.R.init(canvas);
  SD.input.attach(canvas);
  const g = canvas.getContext('2d');
  const loading = p => {
    g.fillStyle = '#0d0806'; g.fillRect(0, 0, SD.W, SD.H);
    g.fillStyle = '#3a2416'; g.fillRect(SD.W / 2 - 100, SD.H / 2, 200, 6);
    g.fillStyle = '#ff9a3c'; g.fillRect(SD.W / 2 - 100, SD.H / 2, Math.round(200 * p), 6);
  };
  loading(0);
  // The manifest is built locally (it is git-ignored), so load it here rather than from a static script tag;
  // when it is missing the game still boots and draws placeholder blocks.
  const loadManifest = () => new Promise(done => {
    if (window.SD_ASSETS) return done(window.SD_ASSETS);
    const tag = document.createElement('script');
    tag.src = 'assets/manifest.js';
    tag.onload = () => done(window.SD_ASSETS || null);
    tag.onerror = () => done(null);
    document.head.appendChild(tag);
  });
  loadManifest().then(manifest => {
    if (!manifest) console.warn('Sundown: assets/manifest.js not found. Build it with games/sundown/tools/build_assets.py; drawing placeholder blocks.');
    return SD.assets.load(manifest, loading);
  }).then(() => {
    SD.Game.init();
    let last = performance.now();
    const tick = now => {
      SD.Game.frame((now - last) / 1000); last = now;
      canvas.style.cursor = SD.Game.state === 'play' && SD.input.mode === 'kb' ? 'none' : 'default';
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden && SD.Game.state === 'play') { SD.Game.state = 'pause'; SD.UI.openPause(SD.Game); } });
  addEventListener('keydown', e => { if (e.code === 'F11' || (e.code === 'Enter' && e.altKey)) { e.preventDefault(); if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen && document.documentElement.requestFullscreen(); } });
})();
