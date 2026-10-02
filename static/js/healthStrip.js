// static/js/healthStrip.js — minimal degraded-state status strip.
//
// Polls GET /api/health/aggregate and renders a single status dot in the
// chat top bar: green = every probed service ok, amber = something degraded,
// red = something down, grey = the endpoint itself unreachable.
// The dot's tooltip lists per-service state; clicking it re-polls immediately.

import { get } from './api.js';

const ENDPOINT = '/api/health/aggregate';
const POLL_MS = 60_000;

const DOT_COLORS = { ok: '#34c77b', degraded: '#e8a13c', down: '#e05252', unknown: '#8a8a8a' };

function worstStatus(services) {
  let worst = 'ok';
  for (const s of Object.values(services || {})) {
    if (s.status === 'down') return 'down';
    if (s.status === 'degraded') worst = 'degraded';
  }
  return worst;
}

async function fetchHealth() {
  return get(ENDPOINT, { redirectOnAuth: false });
}

function ensureEl() {
  let el = document.getElementById('health-strip');
  if (el) return el;
  el = document.createElement('span');
  el.id = 'health-strip';
  el.setAttribute('role', 'status');
  el.setAttribute('aria-label', 'Service health: checking');
  Object.assign(el.style, {
    display: 'inline-block',
    width: '10px',
    height: '10px',
    borderRadius: '50%',
    background: DOT_COLORS.unknown,
    marginLeft: '8px',
    cursor: 'pointer',
    flexShrink: '0',
  });
  const anchor = document.querySelector('.chat-top-bar .chat-meta-overlay')
    || document.querySelector('.chat-top-bar');
  if (anchor) anchor.appendChild(el);
  else document.body.appendChild(el);
  return el;
}

function render(el, data) {
  const services = data.services || {};
  const worst = worstStatus(services);
  el.style.background = DOT_COLORS[worst] || DOT_COLORS.unknown;
  const lines = Object.entries(services).map(([name, s]) => {
    const extra = s.error ? ` \u2014 ${s.error}` : '';
    return `${name}: ${s.status} (${s.latency_ms}ms)${extra}`;
  });
  el.title = `Service health: ${worst}\n` + lines.join('\n');
  el.setAttribute('aria-label', `Service health: ${worst}`);
}

async function poll() {
  const el = ensureEl();
  try {
    render(el, await fetchHealth());
  } catch (e) {
    el.style.background = DOT_COLORS.unknown;
    el.title = 'Service health: unreachable (' + e.message + ')';
    el.setAttribute('aria-label', 'Service health: unreachable');
  }
}

const strip = ensureEl();
strip.addEventListener('click', poll);

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => { poll(); setInterval(poll, POLL_MS); });
} else {
  poll();
  setInterval(poll, POLL_MS);
}
