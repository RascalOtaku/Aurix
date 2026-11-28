// Loads the real game scripts into a Node vm context with the software canvas, so tests, the
// balance bot and the screenshot tool all run the exact code the browser runs.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { Canvas, loadPNG } = require('./canvas-shim');

const GAME = path.resolve(__dirname, '../../../static/games/sundown');
const FILES = ['00-core', '01-assets', '02-render', '03-input', '04-audio', '10-data', '11-world', '12-run', '13-game', '14-ui'];

async function create(opts) {
  opts = opts || {};
  const store = {};
  const sandbox = {
    console, Math, Date, JSON, Promise, Object, Array, Uint8Array, Uint8ClampedArray, Int32Array, Float32Array, Buffer,
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {}, addEventListener() {}, removeEventListener() {},
    performance: { now: () => Date.now() },
    localStorage: { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
    document: { createElement: () => new Canvas(1, 1), getElementById: () => null, addEventListener() {}, hidden: false },
  };
  sandbox.window = sandbox; sandbox.innerWidth = 640; sandbox.innerHeight = 360;
  vm.createContext(sandbox);
  const run = file => vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: file });
  const manifest = path.join(GAME, 'assets/manifest.js');
  const haveAssets = fs.existsSync(manifest) && opts.assets !== false;
  if (haveAssets) run(manifest);
  for (const f of FILES) run(path.join(GAME, 'js', f + '.js'));
  const SD = sandbox.SD;
  SD.assets.makeCanvas = (w, h) => new Canvas(w, h);
  SD.assets.loadImage = src => Promise.resolve().then(() => { try { return loadPNG(path.join(GAME, src)); } catch (e) { return null; } });
  if (opts.load !== false) await SD.assets.load(haveAssets ? sandbox.SD_ASSETS : null);
  const canvas = new Canvas(SD.W, SD.H);
  SD.R.init(canvas);
  SD.input.canvas = canvas;
  return { SD, canvas, sandbox, haveAssets, store };
}
module.exports = { create, GAME };
