// A do-nothing WebAudio stand-in that records calls, so tests can run every sound and song.
'use strict';
function param(v) { return { value: v || 0, setValueAtTime() { return this; }, linearRampToValueAtTime() { return this; }, exponentialRampToValueAtTime(x) { if (!(x > 0)) throw new Error('exponential ramp to ' + x); return this; }, setTargetAtTime() { return this; }, cancelScheduledValues() { return this; } }; }
function node(extra) {
  return Object.assign({ connect(n) { return n; }, disconnect() {}, start() {}, stop() {}, gain: param(1), frequency: param(440), detune: param(0), Q: param(1), delayTime: param(0), threshold: param(0), ratio: param(1), pan: param(0), playbackRate: param(1), type: 'sine', buffer: null }, extra || {});
}
class FakeAudioContext {
  constructor() { this.currentTime = 0; this.sampleRate = 8000; this.state = 'running'; this.destination = node(); this.nodes = 0; }
  resume() {}
  createGain() { this.nodes++; return node(); } createOscillator() { this.nodes++; return node(); } createBiquadFilter() { this.nodes++; return node(); }
  createDelay() { this.nodes++; return node(); } createDynamicsCompressor() { this.nodes++; return node(); } createStereoPanner() { this.nodes++; return node(); }
  createBufferSource() { this.nodes++; return node(); } createWaveShaper() { this.nodes++; return node({ curve: null }); }
  createBuffer(ch, len) { return { getChannelData: () => new Float32Array(len), length: len }; }
}
module.exports = { FakeAudioContext };
