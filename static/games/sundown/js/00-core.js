/* Zero Hour: Sundown — core: namespace, math, seeded RNG, small helpers.
   Every file is a classic script on window.SD so the game runs from file:// with no build step,
   and the same files load in Node (tools/harness.js) for tests, the bot and screenshots. */
'use strict';
var SD = (typeof window !== 'undefined' ? (window.SD = window.SD || {}) : {});

SD.VERSION = '1.0.0';
SD.W = 640;            // logical screen size in world pixels (scaled up with nearest-neighbour)
SD.H = 360;
SD.DT = 1 / 60;        // fixed simulation step

SD.clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
SD.lerp = (a, b, t) => a + (b - a) * t;
SD.dist = (ax, ay, bx, by) => Math.hypot(bx - ax, by - ay);
SD.dist2 = (ax, ay, bx, by) => (bx - ax) * (bx - ax) + (by - ay) * (by - ay);
SD.ang = (ax, ay, bx, by) => Math.atan2(by - ay, bx - ax);
SD.approach = (v, t, d) => (v < t ? Math.min(v + d, t) : Math.max(v - d, t));
SD.wrapAng = a => { while (a > Math.PI) a -= Math.PI * 2; while (a < -Math.PI) a += Math.PI * 2; return a; };
SD.ease = { out: t => 1 - (1 - t) * (1 - t), inOut: t => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2), outBack: t => 1 + 2.7 * Math.pow(t - 1, 3) + 1.7 * Math.pow(t - 1, 2) };

/* The packs render 8 directions, row 0 facing the camera (south) and turning clockwise seen from
   above: S, SW, W, NW, N, NE, E, SE.  Screen y points down, so south is +90 degrees. */
SD.dir8 = a => ((Math.round((a - Math.PI / 2) / (Math.PI / 4)) % 8) + 8) % 8;
SD.dirVec = d => { const a = Math.PI / 2 + d * Math.PI / 4; return [Math.cos(a), Math.sin(a)]; };

/* mulberry32: small, fast, seedable. Gameplay uses SD.rng (seeded per run, so a run is
   reproducible from seed + inputs); cosmetics use SD.fx (never affects the simulation). */
SD.makeRng = function (seed) {
  let s = seed >>> 0;
  const r = function () {
    s = (s + 0x6D2B79F5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  r.range = (a, b) => a + r() * (b - a);
  r.int = (a, b) => a + Math.floor(r() * (b - a + 1));
  r.pick = arr => arr[Math.floor(r() * arr.length)];
  r.chance = p => r() < p;
  r.seed = () => s;
  return r;
};
SD.rng = SD.makeRng(1864);
SD.fx = SD.makeRng(2089);

SD.hashStr = function (str) { let h = 2166136261; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; };
SD.dailySeed = function (date) {
  const d = date || new Date();
  const key = d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate();
  return { key, seed: SD.hashStr('sundown-' + key) };
};

/* storage that never throws (private mode, file://, node) */
SD.store = {
  get(k, def) { try { const v = (typeof localStorage !== 'undefined') && localStorage.getItem('sundown.' + k); return v ? JSON.parse(v) : def; } catch (e) { return def; } },
  set(k, v) { try { if (typeof localStorage !== 'undefined') localStorage.setItem('sundown.' + k, JSON.stringify(v)); } catch (e) { /* ignore */ } },
};

/* tiny event bus: the sim emits, audio/ui/fx listen. Keeps the sim free of side effects. */
SD.bus = {
  h: {},
  on(ev, fn) { (this.h[ev] = this.h[ev] || []).push(fn); },
  emit(ev, a, b, c) { const l = this.h[ev]; if (l) for (let i = 0; i < l.length; i++) l[i](a, b, c); },
  clear() { this.h = {}; },
};

SD.fmtClock = function (minutes) { // minutes after 18:00 -> "6:00 PM"
  const t = (18 * 60 + Math.floor(minutes)) % (24 * 60);
  let h = Math.floor(t / 60); const m = t % 60;
  const pm = h >= 12; h = h % 12; if (h === 0) h = 12;
  return h + ':' + (m < 10 ? '0' : '') + m + (pm ? ' PM' : ' AM');
};
SD.fmtMoney = v => '$' + Math.floor(v);

if (typeof module !== 'undefined') module.exports = SD;
