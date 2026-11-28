/* Audio: every sound and all the music are synthesised with WebAudio at runtime. No audio files.
   Music is a small step sequencer with four moods: frontier (spaghetti-western, A minor),
   saloon (honky-tonk), rift (synthwave after midnight) and boss. */
'use strict';
SD.audio = {
  ctx: null, master: null, sfx: null, mus: null, echo: null, noise: null, on: false,
  vol: { master: 0.8, sfx: 0.9, music: 0.55 },
  unlock() {
    if (this.ctx) { if (this.ctx.state === 'suspended') this.ctx.resume(); return; }
    const AC = (typeof window !== 'undefined') && (window.AudioContext || window.webkitAudioContext);
    if (!AC) return;
    const c = this.ctx = new AC();
    this.master = c.createGain(); this.master.gain.value = this.vol.master;
    const comp = c.createDynamicsCompressor(); comp.threshold.value = -14; comp.ratio.value = 4;
    this.master.connect(comp); comp.connect(c.destination);
    this.sfx = c.createGain(); this.sfx.gain.value = this.vol.sfx; this.sfx.connect(this.master);
    this.mus = c.createGain(); this.mus.gain.value = this.vol.music; this.mus.connect(this.master);
    // feedback echo for the canyon feel / synth delay
    const d = c.createDelay(1.5); d.delayTime.value = 0.34; const fb = c.createGain(); fb.gain.value = 0.32;
    const lp = c.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 2400;
    d.connect(lp); lp.connect(fb); fb.connect(d); lp.connect(this.master);
    this.echo = c.createGain(); this.echo.gain.value = 0.5; this.echo.connect(d);
    const len = c.sampleRate * 1.5, buf = c.createBuffer(1, len, c.sampleRate), ch = buf.getChannelData(0);
    for (let i = 0; i < len; i++) ch[i] = Math.random() * 2 - 1;
    this.noise = buf; this.on = true;
    const saved = SD.store.get('vol', null); if (saved) this.setVol(saved);
    if (this.pending) { this.music(this.pending); }
  },
  setVol(v) {
    Object.assign(this.vol, v); SD.store.set('vol', this.vol);
    if (!this.on) return;
    this.master.gain.value = this.vol.master; this.sfx.gain.value = this.vol.sfx; this.mus.gain.value = this.vol.music;
  },

  /* ---------- building blocks ---------- */
  env(g, t, a, peak, dec, sus) { g.gain.cancelScheduledValues(t); g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(peak, t + a); g.gain.exponentialRampToValueAtTime(Math.max(0.0001, sus || 0.0001), t + a + dec); },
  osc(type, f, t, dur, peak, dest, o) {
    o = o || {};
    const c = this.ctx, s = c.createOscillator(), g = c.createGain();
    s.type = type; s.frequency.setValueAtTime(f, t);
    if (o.to) s.frequency.exponentialRampToValueAtTime(Math.max(20, o.to), t + (o.glide || dur));
    if (o.vib) { const l = c.createOscillator(), lg = c.createGain(); l.frequency.value = o.vib; lg.gain.value = f * 0.012; l.connect(lg); lg.connect(s.frequency); l.start(t); l.stop(t + dur + 0.1); }
    this.env(g, t, o.a || 0.004, peak, dur);
    let node = g;
    if (o.lp) { const f2 = c.createBiquadFilter(); f2.type = 'lowpass'; f2.frequency.value = o.lp; f2.Q.value = o.q || 1; g.connect(f2); node = f2; }
    s.connect(g); node.connect(dest || this.sfx); if (o.echo) node.connect(this.echo);
    s.start(t); s.stop(t + dur + 0.05);
  },
  burst(t, dur, peak, type, freq, dest, o) {
    o = o || {};
    const c = this.ctx, s = c.createBufferSource(), f = c.createBiquadFilter(), g = c.createGain();
    s.buffer = this.noise; s.loop = true; s.playbackRate.value = o.rate || 1;
    f.type = type || 'lowpass'; f.frequency.setValueAtTime(freq, t); if (o.to) f.frequency.exponentialRampToValueAtTime(o.to, t + dur);
    f.Q.value = o.q || 0.7;
    this.env(g, t, o.a || 0.002, peak, dur);
    s.connect(f); f.connect(g); g.connect(dest || this.sfx); if (o.echo) g.connect(this.echo);
    s.start(t, SD.fx() * 1.0); s.stop(t + dur + 0.05);
  },

  /* ---------- sound effects ---------- */
  play(name, vol) {
    if (!this.on) return;
    const t = this.ctx.currentTime + 0.002, v = vol == null ? 1 : vol, r = SD.fx;
    switch (name) {
      case 'revolver': this.burst(t, 0.22, 0.9 * v, 'lowpass', 3800, null, { to: 500, echo: true }); this.osc('sine', 140, t, 0.12, 0.7 * v, null, { to: 45 }); break;
      case 'fan': this.burst(t, 0.16, 0.7 * v, 'lowpass', 4200, null, { to: 700, echo: true }); this.osc('sine', 160, t, 0.08, 0.5 * v, null, { to: 50 }); break;
      case 'shotgun': this.burst(t, 0.42, 1.0 * v, 'lowpass', 2600, null, { to: 220, echo: true }); this.osc('sine', 90, t, 0.25, 0.9 * v, null, { to: 32 }); break;
      case 'rifle': this.burst(t, 0.3, 0.95 * v, 'bandpass', 2400, null, { to: 300, q: 0.6, echo: true }); this.osc('square', 220, t, 0.05, 0.25 * v, null, { to: 60 }); this.osc('sine', 110, t, 0.2, 0.7 * v, null, { to: 40 }); break;
      case 'lever': this.burst(t + 0.18, 0.04, 0.4 * v, 'highpass', 2500); this.burst(t + 0.28, 0.05, 0.5 * v, 'highpass', 1800); break;
      case 'arc': this.osc('sawtooth', 900 + r() * 200, t, 0.12, 0.35 * v, null, { to: 180, lp: 3000, echo: true }); this.burst(t, 0.08, 0.3 * v, 'highpass', 4000); break;
      case 'rail': this.osc('sawtooth', 1800, t, 0.4, 0.4 * v, null, { to: 90, lp: 4000, echo: true }); this.osc('sine', 60, t, 0.35, 0.8 * v, null, { to: 30 }); this.burst(t, 0.3, 0.5 * v, 'bandpass', 1200, null, { to: 200 }); break;
      case 'enemyShot': this.burst(t, 0.18, 0.45 * v, 'lowpass', 2400, null, { to: 400 }); this.osc('sine', 120, t, 0.08, 0.3 * v, null, { to: 50 }); break;
      case 'droneShot': this.osc('square', 1400, t, 0.1, 0.18 * v, null, { to: 500, lp: 2500 }); break;
      case 'click': this.burst(t, 0.03, 0.4 * v, 'highpass', 3000); break;
      case 'reload': for (let i = 0; i < 3; i++) this.burst(t + i * 0.09, 0.03, 0.35 * v, 'highpass', 2600 - i * 300); this.burst(t + 0.38, 0.06, 0.5 * v, 'bandpass', 1400); break;
      case 'empty': this.osc('square', 1800, t, 0.03, 0.15 * v, null, { lp: 3000 }); break;
      case 'hit': this.burst(t, 0.07, 0.5 * v, 'bandpass', 900 + r() * 400, null, { q: 2 }); this.osc('sine', 300, t, 0.05, 0.25 * v, null, { to: 120 }); break;
      case 'crit': this.osc('triangle', 1600, t, 0.12, 0.3 * v, null, { to: 2400 }); this.burst(t, 0.06, 0.5 * v, 'highpass', 3000); break;
      case 'metal': this.osc('triangle', 1200 + r() * 600, t, 0.18, 0.2 * v, null, { to: 900 }); this.burst(t, 0.05, 0.3 * v, 'highpass', 5000); break;
      case 'wood': this.burst(t, 0.09, 0.45 * v, 'bandpass', 600 + r() * 300, null, { q: 3 }); break;
      case 'ricochet': this.osc('sine', 2400 + r() * 1600, t, 0.25, 0.12 * v, null, { to: 900, echo: true }); break;
      case 'die': this.osc('sawtooth', 220, t, 0.35, 0.25 * v, null, { to: 60, lp: 900 }); this.burst(t, 0.2, 0.3 * v, 'lowpass', 800); break;
      case 'hurt': this.osc('square', 180, t, 0.16, 0.3 * v, null, { to: 90, lp: 1200 }); this.burst(t, 0.1, 0.4 * v, 'lowpass', 1500); break;
      case 'explode': this.burst(t, 1.1, 1.2 * v, 'lowpass', 1600, null, { to: 60, echo: true }); this.osc('sine', 70, t, 0.8, 1.0 * v, null, { to: 25 }); break;
      case 'fuse': this.burst(t, 0.5, 0.18 * v, 'highpass', 5000, null, { rate: 2 }); break;
      case 'throw': this.burst(t, 0.15, 0.25 * v, 'bandpass', 700, null, { to: 1800, q: 1.5 }); break;
      case 'dash': this.burst(t, 0.2, 0.4 * v, 'bandpass', 500, null, { to: 1600, q: 0.8 }); break;
      case 'coin': this.osc('square', 1318, t, 0.06, 0.15 * v, null, { lp: 5000 }); this.osc('square', 1975, t + 0.06, 0.12, 0.15 * v, null, { lp: 5000 }); break;
      case 'heal': [523, 659, 784].forEach((f, i) => this.osc('triangle', f, t + i * 0.06, 0.2, 0.2 * v)); break;
      case 'buy': [784, 988, 1318].forEach((f, i) => this.osc('square', f, t + i * 0.05, 0.12, 0.12 * v, null, { lp: 4000 })); break;
      case 'deny': this.osc('square', 160, t, 0.18, 0.2 * v, null, { lp: 900 }); break;
      case 'blip': this.osc('square', 880, t, 0.04, 0.1 * v, null, { lp: 3000 }); break;
      case 'select': this.osc('square', 660, t, 0.05, 0.12 * v, null, { lp: 3000 }); this.osc('square', 990, t + 0.05, 0.08, 0.12 * v, null, { lp: 3000 }); break;
      case 'bell': [0, 0.9].forEach(d => { this.osc('sine', 392, t + d, 2.2, 0.35 * v, null, { echo: true }); this.osc('sine', 392 * 2.76, t + d, 1.2, 0.12 * v); this.osc('sine', 392 * 5.4, t + d, 0.6, 0.05 * v); }); break;
      case 'wave': this.osc('triangle', 440, t, 0.25, 0.25 * v, null, { echo: true }); this.osc('triangle', 660, t + 0.2, 0.5, 0.25 * v, null, { echo: true }); break;
      case 'clear': [523, 659, 784, 1046].forEach((f, i) => this.osc('triangle', f, t + i * 0.09, 0.35, 0.22 * v, null, { echo: true })); break;
      case 'glitch': for (let i = 0; i < 6; i++) this.osc('square', 80 + r() * 2400, t + i * 0.035, 0.04, 0.15 * v, null, { lp: 6000 }); this.burst(t, 0.25, 0.3 * v, 'bandpass', 3000, null, { to: 200, q: 4 }); break;
      case 'rift': this.osc('sawtooth', 40, t, 3.5, 0.6 * v, null, { to: 220, glide: 3, lp: 1200, echo: true }); this.burst(t, 3.5, 0.5 * v, 'bandpass', 200, null, { to: 4000, q: 6, a: 1.5 }); break;
      case 'teleport': this.osc('sine', 300, t, 0.2, 0.25 * v, null, { to: 2200 }); this.burst(t, 0.15, 0.2 * v, 'highpass', 6000); break;
      case 'boss': this.osc('sawtooth', 55, t, 1.6, 0.5 * v, null, { lp: 500, echo: true }); this.osc('sawtooth', 58, t, 1.6, 0.4 * v, null, { lp: 500 }); break;
      case 'focusOn': this.osc('sine', 700, t, 0.5, 0.2 * v, null, { to: 180 }); break;
      case 'focusOff': this.osc('sine', 180, t, 0.3, 0.2 * v, null, { to: 700 }); break;
      case 'pickup': this.osc('triangle', 880, t, 0.08, 0.18 * v); this.osc('triangle', 1320, t + 0.07, 0.1, 0.18 * v); break;
      case 'whiskey': this.burst(t, 0.3, 0.25 * v, 'bandpass', 900, null, { to: 400, q: 3 }); break;
    }
  },

  /* ---------- music: step sequencer ---------- */
  song: null, step: 0, nextT: 0, timer: null, pending: null,
  NOTE: n => 440 * Math.pow(2, (n - 69) / 12),
  music(name) {
    if (!this.on) { this.pending = name; return; }
    if (this.song && this.song.name === name) return;
    this.song = this.SONGS[name] ? Object.assign({ name }, this.SONGS[name]) : null;
    this.step = 0; this.nextT = this.ctx.currentTime + 0.08;
    if (!this.timer) this.timer = setInterval(() => this.tick(), 25);
  },
  stopMusic() { this.song = null; },
  tick() {
    if (!this.song || !this.on) return;
    const c = this.ctx, s = this.song, sp = 60 / s.bpm / 4; // 16th notes
    while (this.nextT < c.currentTime + 0.12) { s.play(this, this.step, this.nextT, sp); this.step++; this.nextT += sp; }
  },
  SONGS: {
    // A minor, 6/8-ish lope: bass on 1 and 4, strummed chords, a whistle that answers itself
    frontier: { bpm: 92, play(A, st, t, sp) {
      const bar = Math.floor(st / 16) % 8, i = st % 16, M = A.mus;
      const prog = [[57, 60, 64], [57, 60, 64], [55, 59, 62], [57, 60, 64], [53, 57, 60], [55, 59, 62], [52, 56, 59], [57, 60, 64]][bar];
      if (i === 0 || i === 10) A.osc('triangle', A.NOTE(prog[0] - 12), t, sp * 5, 0.32, M, { lp: 700 });
      if (i === 6) A.osc('triangle', A.NOTE(prog[0] - 5), t, sp * 3, 0.22, M, { lp: 700 });
      if (i === 4 || i === 12 || i === 14) prog.forEach((n, k) => A.osc('sawtooth', A.NOTE(n), t + k * 0.012, sp * 2.5, 0.05, M, { lp: 1800 }));
      if (i % 4 === 2) A.burst(t, 0.04, 0.06, 'highpass', 6000, M);
      const mel = [[69, 0, 72, 0, 0, 0, 74, 0, 76, 0, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 74, 0, 72, 0, 69, 0, 0, 0, 0, 0, 0, 0],
        [67, 0, 0, 0, 71, 0, 74, 0, 0, 0, 72, 0, 71, 0, 0, 0], [69, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [65, 0, 69, 0, 72, 0, 0, 0, 77, 0, 76, 0, 74, 0, 0, 0], [74, 0, 0, 0, 71, 0, 0, 0, 67, 0, 0, 0, 0, 0, 0, 0],
        [68, 0, 71, 0, 76, 0, 0, 0, 74, 0, 0, 0, 71, 0, 68, 0], [69, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]];
      const n = mel[bar][i]; if (n && (Math.floor(st / 128) % 2 === 1 || bar < 4)) A.osc('sine', A.NOTE(n + 12), t, sp * 5, 0.11, M, { a: 0.05, vib: 5.5, echo: true });
    } },
    // honky-tonk: oom-pah left hand, bright right hand
    saloon: { bpm: 128, play(A, st, t, sp) {
      const bar = Math.floor(st / 16) % 4, i = st % 16, M = A.mus;
      const roots = [48, 53, 55, 48][bar], ch = [[64, 67, 72], [65, 69, 72], [62, 67, 71], [64, 67, 72]][bar];
      if (i % 8 === 0) A.osc('triangle', A.NOTE(roots - 12), t, sp * 3, 0.3, M);
      if (i % 8 === 4) ch.forEach(n => A.osc('square', A.NOTE(n - 12), t, sp * 2, 0.035, M, { lp: 1600 }));
      const rh = [72, 0, 76, 79, 0, 76, 0, 72, 74, 0, 77, 0, 76, 0, 72, 0];
      if (rh[i] && SD.fx() > 0.15) A.osc('square', A.NOTE(rh[i] + (bar === 1 ? 1 : bar === 2 ? -1 : 0)), t, sp * 1.5, 0.045, M, { lp: 2800 });
    } },
    // synthwave: driving 16th bass, gated pad, square arp through the echo
    rift: { bpm: 112, play(A, st, t, sp) {
      const bar = Math.floor(st / 16) % 4, i = st % 16, M = A.mus;
      const root = [45, 41, 43, 40][bar];
      A.osc('sawtooth', A.NOTE(root - 12 + (i % 4 === 3 ? 12 : 0)), t, sp * 0.9, 0.12, M, { lp: 600 + (i % 8) * 80, q: 4 });
      if (i % 4 === 0) { A.osc('sine', 120, t, 0.18, 0.6, M, { to: 40 }); }
      if (i === 4 || i === 12) A.burst(t, 0.16, 0.22, 'bandpass', 1800, M, { q: 0.8 });
      if (i % 2 === 1) A.burst(t, 0.03, 0.05, 'highpass', 7000, M);
      const arp = [0, 7, 12, 15, 19, 15, 12, 7]; A.osc('square', A.NOTE(root + 24 + arp[i % 8]), t, sp * 0.8, 0.035, M, { lp: 3200, echo: true });
      if (i === 0) [0, 3, 7].forEach(k => A.osc('sawtooth', A.NOTE(root + 12 + k), t, sp * 14, 0.025, M, { a: 0.2, lp: 1400 }));
    } },
    boss: { bpm: 140, play(A, st, t, sp) {
      const bar = Math.floor(st / 16) % 4, i = st % 16, M = A.mus;
      const root = [45, 45, 46, 44][bar];
      if (i % 2 === 0) A.osc('sawtooth', A.NOTE(root - 12), t, sp * 1.6, 0.14, M, { lp: 900, q: 3 });
      if (i % 4 === 0) A.osc('sine', 110, t, 0.2, 0.7, M, { to: 38 });
      if (i === 4 || i === 12) A.burst(t, 0.2, 0.3, 'bandpass', 1500, M);
      if (i === 0 && bar % 2 === 0) A.osc('triangle', A.NOTE(root + 24), t, sp * 6, 0.12, M, { vib: 6, echo: true });
      if (i === 8) A.osc('triangle', A.NOTE(root + 27), t, sp * 6, 0.1, M, { vib: 6, echo: true });
    } },
    title: { bpm: 72, play(A, st, t, sp) {
      const i = st % 32, M = A.mus;
      if (i === 0) A.osc('triangle', A.NOTE(33), t, sp * 30, 0.25, M, { a: 0.4, lp: 500 });
      const mel = { 0: 69, 6: 72, 8: 74, 12: 76, 20: 74, 24: 69 };
      if (mel[i] && Math.floor(st / 32) % 2 === 0) A.osc('sine', A.NOTE(mel[i] + 12), t, sp * 6, 0.1, M, { a: 0.08, vib: 5, echo: true });
      if (i === 16 && Math.floor(st / 32) % 4 === 3) A.play('bell', 0.4);
    } },
  },
};
