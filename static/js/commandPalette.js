/**
 * Aurix command palette (Cmd+K / Ctrl+K).
 *
 * Fully self-contained ES module: builds its own overlay DOM, injects its
 * own <style> (every class/id prefixed `cmdk-`), and self-initializes on
 * DOMContentLoaded. No changes to chat.js, slashCommands.js, index.html
 * (beyond the <script> tag) or style.css are required.
 *
 * Data sources:
 *  - `listSlashCommands()` from ./slashCommands.js — every command and
 *    subcommand becomes a palette entry (usage string shown as the hint).
 *    `hidden: true` commands are included but deprioritized in a trailing
 *    "More" group (easter eggs like /8ball and /matrix stay discoverable).
 *  - Built-in navigation actions for the app's real pages (/, /command,
 *    /systems), verified against routes/command_routes.py and
 *    routes/sysview_routes.py.
 *
 * Running a slash command reuses the app's own programmatic-submit pattern
 * (see the skill-invocation flow in slashCommands.js): set #message's value,
 * then form.requestSubmit() on #chat-form, falling back to a dispatched
 * submit event and finally a synthetic Enter keydown.
 *
 * Edge cases handled:
 *  - Never opens while the first-run tour is active
 *    (sessionStorage 'aurix-tour-active').
 *  - Ignores Cmd/Ctrl+K while an IME composition is in progress.
 *  - If #message is absent, only navigation actions are offered.
 */
import { listSlashCommands } from './slashCommands.js';

if (!window.__cmdk_init) {
window.__cmdk_init = true;

/* ------------------------------------------------------------------ */
/* styles                                                              */
/* ------------------------------------------------------------------ */
const CMDK_CSS = `
#cmdk-overlay {
  position: fixed; inset: 0; z-index: 99990;
  display: none; align-items: flex-start; justify-content: center;
  padding: 12vh 16px 16px;
  background: rgba(8, 10, 14, 0.55);
  -webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px);
  opacity: 0; transition: opacity 140ms ease;
}
#cmdk-overlay.cmdk-open { display: flex; opacity: 1; }
#cmdk-panel {
  width: 640px; max-width: 100%;
  max-height: min(60vh, 560px);
  display: flex; flex-direction: column; overflow: hidden;
  background: var(--cmdk-panel, rgba(22, 25, 33, 0.92));
  background: color-mix(in srgb, var(--bg, #161920) 88%, transparent);
  border: 1px solid var(--border, rgba(140, 160, 190, 0.22));
  border-radius: 14px;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.55), 0 2px 12px rgba(0, 0, 0, 0.4);
  color: var(--fg, #e8ecf3);
  font-family: inherit;
  animation: cmdk-pop 160ms cubic-bezier(0.2, 0.9, 0.3, 1.2);
}
@keyframes cmdk-pop {
  from { opacity: 0; transform: translateY(-10px) scale(0.985); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}
#cmdk-input {
  flex: 0 0 auto; width: 100%; box-sizing: border-box;
  padding: 14px 18px; font-size: 16px;
  background: transparent; border: none; outline: none;
  border-bottom: 1px solid var(--border, rgba(140, 160, 190, 0.18));
  color: var(--fg, #e8ecf3);
  font-family: inherit;
}
#cmdk-input::placeholder { color: var(--dim, rgba(160, 175, 195, 0.55)); }
#cmdk-list {
  overflow-y: auto; padding: 8px;
  scrollbar-width: thin; scrollbar-color: rgba(140,160,190,.35) transparent;
}
#cmdk-list::-webkit-scrollbar { width: 8px; }
#cmdk-list::-webkit-scrollbar-thumb { background: rgba(140,160,190,.3); border-radius: 4px; }
.cmdk-group {
  padding: 10px 12px 4px;
  font-size: 11px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--dim, rgba(160, 175, 195, 0.6));
}
.cmdk-item {
  display: flex; align-items: baseline; gap: 12px;
  padding: 9px 12px; border-radius: 8px; cursor: pointer;
  border-left: 2px solid transparent;
}
.cmdk-item:hover { background: rgba(130, 150, 185, 0.10); }
.cmdk-item.cmdk-selected {
  background: rgba(130, 150, 185, 0.18);
  border-left-color: var(--accent, #7aa2f7);
}
.cmdk-label {
  flex: 0 0 auto; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13.5px; color: var(--fg, #e8ecf3); white-space: nowrap;
}
.cmdk-item.cmdk-selected .cmdk-label { color: #fff; }
.cmdk-hint {
  flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 12.5px; color: var(--dim, rgba(160, 175, 195, 0.62));
}
.cmdk-empty {
  padding: 28px 12px; text-align: center; font-size: 13.5px;
  color: var(--dim, rgba(160, 175, 195, 0.6));
}
.cmdk-footer {
  flex: 0 0 auto; display: flex; gap: 14px; align-items: center;
  padding: 8px 16px; font-size: 11.5px;
  color: var(--dim, rgba(160, 175, 195, 0.55));
  border-top: 1px solid var(--border, rgba(140, 160, 190, 0.14));
}
.cmdk-footer b {
  display: inline-block; min-width: 18px; text-align: center;
  padding: 1px 5px; margin-right: 4px; border-radius: 4px;
  background: rgba(130, 150, 185, 0.16); font-weight: 600;
}
@media (max-width: 640px) {
  #cmdk-overlay { padding: 0; align-items: flex-end; }
  #cmdk-panel {
    width: 100%; max-height: 82vh;
    border-radius: 16px 16px 0 0;
    border-left: none; border-right: none; border-bottom: none;
    animation: cmdk-sheet 180ms cubic-bezier(0.2, 0.9, 0.3, 1);
  }
  @keyframes cmdk-sheet {
    from { opacity: 0; transform: translateY(24px); }
    to   { opacity: 1; transform: translateY(0); }
  }
}
`;

/* ------------------------------------------------------------------ */
/* data                                                                */
/* ------------------------------------------------------------------ */
const NAV_ACTIONS = [
  { label: 'Go to Chat',            path: '/',        hint: 'Main chat',      keywords: 'chat home main index' },
  { label: 'Go to Command Center',  path: '/command',  hint: 'Command Center', keywords: 'command center dashboard control' },
  { label: 'Go to Systems',         path: '/systems',  hint: 'Systems view',   keywords: 'systems sysview status health services' },
];

let allEntries = [];
let visibleEntries = [];   // flat list of currently rendered (non-header) entries
let isOpen = false;
let activeIndex = 0;
let overlayEl = null, inputEl = null, listEl = null;

function buildEntries() {
  const out = [];
  let cmds = [];
  try { cmds = listSlashCommands() || []; }
  catch (e) { console.warn('cmdk: listSlashCommands failed', e); }

  for (const c of cmds) {
    const base = {
      kind: 'cmd',
      category: c.category || 'General',
      hidden: !!c.hidden,
    };
    // Top-level command entry (e.g. `/session`; runs its default sub).
    out.push({
      ...base,
      text: '/' + c.name,
      label: '/' + c.name,
      hint: c.usage || ('/' + c.name),
      help: c.help || '',
      search: ('/' + c.name + ' ' + (c.aliases || []).join(' ') + ' ' + (c.help || '')).toLowerCase(),
    });
    // One entry per subcommand (e.g. `/session new`), usage as the hint.
    for (const s of (c.subs || [])) {
      out.push({
        ...base,
        text: '/' + c.name + ' ' + s.name,
        label: '/' + c.name + ' ' + s.name,
        hint: s.usage || ('/' + c.name + ' ' + s.name),
        help: s.help || '',
        search: ('/' + c.name + ' ' + s.name + ' ' + (s.aliases || []).join(' ') + ' ' + (s.help || '')).toLowerCase(),
      });
    }
  }
  for (const n of NAV_ACTIONS) {
    out.push({
      kind: 'nav',
      category: 'Go to',
      hidden: false,
      text: n.path,
      label: n.label,
      hint: n.hint,
      help: '',
      path: n.path,
      search: (n.label + ' ' + n.path + ' ' + n.keywords).toLowerCase(),
    });
  }
  return out;
}

/* ------------------------------------------------------------------ */
/* fuzzy matching — in-order subsequence, case-insensitive; lower is  */
/* better. -1 means no match.                                         */
/* ------------------------------------------------------------------ */
function fuzzyScore(query, target) {
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  let qi = 0, score = 0, last = -1;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      score += (last === -1) ? ti * 2 : (ti - last - 1);
      last = ti;
      qi++;
    }
  }
  return qi === q.length ? score : -1;
}

function filterEntries(query) {
  const q = (query || '').trim();
  const hasChat = !!document.getElementById('message');
  let pool = allEntries;
  // Without a chat input there is nothing to run commands into — nav only.
  if (!hasChat) pool = pool.filter(e => e.kind === 'nav');

  if (!q) {
    // Browsed view: category groups, curated first, hidden trailing.
    const groups = new Map();
    for (const e of pool) {
      const g = e.hidden ? 'More' : e.category;
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(e);
    }
    // "Go to" first, then registry order, "More" last.
    const ordered = [];
    if (groups.has('Go to')) { ordered.push(['Go to', groups.get('Go to')]); groups.delete('Go to'); }
    const more = groups.has('More') ? groups.get('More') : null;
    groups.delete('More');
    for (const [g, items] of groups) ordered.push([g, items]);
    if (more) ordered.push(['More', more]);
    return ordered;
  }

  // Filtered view: score, drop non-matches, hidden entries penalized.
  const scored = [];
  for (const e of pool) {
    const s = fuzzyScore(q, e.search);
    if (s === -1) continue;
    scored.push([s + (e.hidden ? 10000 : 0), e.search.length, e]);
  }
  scored.sort((a, b) => (a[0] - b[0]) || (a[1] - b[1]));
  // Regroup by category, groups ordered by their best score.
  const groups = new Map();
  for (const [, , e] of scored) {
    const g = e.hidden ? 'More' : e.category;
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(e);
  }
  return [...groups.entries()];
}

/* ------------------------------------------------------------------ */
/* rendering                                                           */
/* ------------------------------------------------------------------ */
function escHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function render() {
  const groups = filterEntries(inputEl.value);
  visibleEntries = [];
  let html = '';
  for (const [gname, items] of groups) {
    const shown = items.slice(0, 60);
    html += `<div class="cmdk-group">${escHtml(gname)}</div>`;
    for (const e of shown) {
      const idx = visibleEntries.length;
      visibleEntries.push(e);
      html += `<div class="cmdk-item${idx === activeIndex ? ' cmdk-selected' : ''}" data-cmdk-idx="${idx}" title="${escHtml(e.help || e.hint)}">` +
        `<span class="cmdk-label">${escHtml(e.label)}</span>` +
        `<span class="cmdk-hint">${escHtml(e.hint)}</span></div>`;
    }
  }
  if (!visibleEntries.length) {
    html = `<div class="cmdk-empty">No matching commands. Try "session", "memory", or "toggle".</div>`;
  }
  listEl.innerHTML = html;
  if (activeIndex >= visibleEntries.length) activeIndex = Math.max(0, visibleEntries.length - 1);
  updateSelection();
}

function updateSelection() {
  const items = listEl.querySelectorAll('.cmdk-item');
  items.forEach(el => el.classList.toggle('cmdk-selected', Number(el.dataset.cmdkIdx) === activeIndex));
  const sel = listEl.querySelector('.cmdk-item.cmdk-selected');
  if (sel) sel.scrollIntoView({ block: 'nearest' });
}

function moveSelection(delta) {
  if (!visibleEntries.length) return;
  activeIndex = (activeIndex + delta + visibleEntries.length) % visibleEntries.length;
  updateSelection();
}

/* ------------------------------------------------------------------ */
/* running entries                                                     */
/* ------------------------------------------------------------------ */
function runSlashCommand(text) {
  const input = document.getElementById('message');
  if (!input) return;
  input.value = text;
  try { input.focus({ preventScroll: true }); } catch (_) { input.focus(); }
  // Same programmatic-submit pattern the app itself uses for skill
  // invocation (slashCommands.js): requestSubmit, then dispatched
  // submit event, then a synthetic Enter keydown as last resort.
  const form = document.getElementById('chat-form');
  if (form && typeof form.requestSubmit === 'function') {
    form.requestSubmit();
  } else if (form) {
    form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
  } else {
    input.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
      bubbles: true, cancelable: true,
    }));
  }
}

function runEntry(entry) {
  if (!entry) return;
  close();
  if (entry.kind === 'nav') {
    location.href = entry.path;
    return;
  }
  runSlashCommand(entry.text);
}

/* ------------------------------------------------------------------ */
/* open / close / toggle                                               */
/* ------------------------------------------------------------------ */
function open() {
  // Never interrupt the first-run tour.
  try {
    if (sessionStorage.getItem('aurix-tour-active')) return;
  } catch (_) { /* sessionStorage unavailable — carry on */ }
  if (isOpen) return;
  isOpen = true;
  activeIndex = 0;
  inputEl.value = '';
  render();
  overlayEl.classList.add('cmdk-open');
  // Focus after the open transition starts so mobile keyboards behave.
  requestAnimationFrame(() => { try { inputEl.focus(); } catch (_) {} });
}

function close() {
  if (!isOpen) return;
  isOpen = false;
  overlayEl.classList.remove('cmdk-open');
  const msg = document.getElementById('message');
  if (msg) { try { msg.focus({ preventScroll: true }); } catch (_) {} }
}

function toggle() {
  if (isOpen) close();
  else open();
}

/* ------------------------------------------------------------------ */
/* init                                                                */
/* ------------------------------------------------------------------ */
function init() {
  // Styles
  const style = document.createElement('style');
  style.id = 'cmdk-styles';
  style.textContent = CMDK_CSS;
  document.head.appendChild(style);

  // DOM
  overlayEl = document.createElement('div');
  overlayEl.id = 'cmdk-overlay';
  overlayEl.setAttribute('role', 'dialog');
  overlayEl.setAttribute('aria-label', 'Command palette');
  overlayEl.innerHTML =
    `<div id="cmdk-panel" role="listbox" aria-label="Commands">` +
      `<input id="cmdk-input" type="text" placeholder="Type a command or search pages…" autocomplete="off" spellcheck="false" aria-label="Search commands">` +
      `<div id="cmdk-list"></div>` +
      `<div class="cmdk-footer"><span><b>↑↓</b> navigate</span><span><b>↵</b> run</span><span><b>esc</b> close</span></div>` +
    `</div>`;
  document.body.appendChild(overlayEl);
  inputEl = overlayEl.querySelector('#cmdk-input');
  listEl = overlayEl.querySelector('#cmdk-list');

  allEntries = buildEntries();

  // Backdrop click closes; clicks inside the panel do not.
  overlayEl.addEventListener('mousedown', (e) => { if (e.target === overlayEl) close(); });
  listEl.addEventListener('click', (e) => {
    const row = e.target.closest('.cmdk-item');
    if (!row) return;
    runEntry(visibleEntries[Number(row.dataset.cmdkIdx)]);
  });
  // Keep palette keystrokes out of the app's own handlers.
  overlayEl.addEventListener('keydown', (e) => e.stopPropagation(), true);

  inputEl.addEventListener('input', () => { activeIndex = 0; render(); });
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); moveSelection(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); moveSelection(-1); }
    else if (e.key === 'Enter') { e.preventDefault(); runEntry(visibleEntries[activeIndex]); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
  });

  document.addEventListener('keydown', (e) => {
    // Never hijack keystrokes mid-IME-composition.
    if (e.isComposing) return;
    const k = (e.key || '').toLowerCase();
    if ((e.metaKey || e.ctrlKey) && k === 'k') {
      e.preventDefault();
      toggle();
      return;
    }
    if (e.key === 'Escape' && isOpen) {
      e.preventDefault();
      close();
    }
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

}
