/**
 * Slash-command autocomplete for the chat input.
 *
 * Type `/` in the message box to get a live dropdown of commands with
 * descriptions; keep typing to filter. `/session ` + partial filters that
 * group's subcommands. Arrow keys navigate, Tab/Enter completes, Esc
 * dismisses. Mirrors the `/help` visibility rules (hidden commands and
 * `_`-prefixed subs are not suggested).
 *
 * Wired via initSlashAutocomplete() from chat.js init(). The keydown
 * listener uses capture phase so Enter/Tab/Esc are swallowed before the
 * chat send handler ever sees them.
 */
import { listSlashCommands } from './slashCommands.js';

let COMMANDS = null;
function getCommands() {
  if (!COMMANDS) COMMANDS = listSlashCommands().filter(c => !c.hidden);
  return COMMANDS;
}

function findCommand(name) {
  const n = name.toLowerCase();
  return getCommands().find(c => c.name.toLowerCase() === n ||
    (c.aliases || []).some(a => a.toLowerCase() === n));
}

/**
 * Work out what the user is typing before the cursor.
 * Returns { mode: 'cmd'|'sub', cmd?, partial, tokenStart } or null.
 */
function tokenContext(ta) {
  const pos = ta.selectionStart;
  if (pos == null) return null;
  const before = ta.value.slice(0, pos);
  // Subcommand mode: "/session par"
  let m = before.match(/\/([A-Za-z0-9-]+)\s+([A-Za-z0-9-]*)$/);
  if (m && findCommand(m[1])) {
    return { mode: 'sub', cmd: m[1], partial: m[2], tokenStart: pos - m[2].length };
  }
  // Command mode: "/par" at start or after whitespace
  m = before.match(/(^|\s)\/([A-Za-z0-9-]*)$/);
  if (m) {
    return { mode: 'cmd', partial: m[2], tokenStart: pos - m[2].length - 1 };
  }
  return null;
}

function rankEntries(entries, partial) {
  const p = (partial || '').toLowerCase();
  const out = [];
  for (const e of entries) {
    const name = e.name.toLowerCase();
    let score = -1;
    if (p === '' || name.startsWith(p)) score = 0;
    else if (e.aliases.some(a => a.toLowerCase().startsWith(p))) score = 1;
    else if (name.includes(p)) score = 2;
    else if (e.match.indexOf(p) !== -1) score = 3;
    if (score >= 0) out.push([score, e]);
  }
  out.sort((a, b) => a[0] - b[0] || a[1].name.localeCompare(b[1].name));
  return out.map(x => x[1]);
}

function buildEntries(ctx) {
  if (ctx.mode === 'sub') {
    const cmd = findCommand(ctx.cmd);
    if (!cmd || !cmd.subs) return [];
    return cmd.subs
      .filter(s => !s.name.startsWith('_'))
      .map(s => ({
        name: s.name,
        insert: s.name + ' ',
        label: '/' + cmd.name + ' ' + s.name,
        help: s.help || '',
        usage: s.usage || ('/' + cmd.name + ' ' + s.name),
        aliases: s.aliases || [],
        match: (s.name + ' ' + (s.aliases || []).join(' ') + ' ' + (s.help || '')).toLowerCase(),
      }));
  }
  return getCommands().map(c => {
    const hasSubs = c.subs && c.subs.length;
    return {
      name: c.name,
      insert: '/' + c.name + ' ',
      label: '/' + c.name + (hasSubs ? ' …' : ''),
      help: c.help || (hasSubs ? c.subs.length + ' subcommands' : ''),
      usage: c.usage || ('/' + c.name),
      aliases: c.aliases || [],
      match: (c.name + ' ' + (c.aliases || []).join(' ') + ' ' + (c.help || '') + ' ' + (c.category || '')).toLowerCase(),
    };
  });
}

export function initSlashAutocomplete() {
  const ta = document.getElementById('message');
  if (!ta) {
    // Module scripts run at end of body, but be safe if init races the DOM.
    document.addEventListener('DOMContentLoaded', initSlashAutocomplete, { once: true });
    return;
  }
  if (ta.__slacInit) return;
  ta.__slacInit = true;

  let drop = null;
  let items = [];
  let selIdx = 0;
  let ctx = null;
  let composing = false;

  function close() {
    if (drop) { drop.remove(); drop = null; }
    items = [];
  }

  function position() {
    const r = ta.getBoundingClientRect();
    const maxH = Math.min(320, window.innerHeight * 0.45);
    drop.style.maxHeight = maxH + 'px';
    let top = r.bottom + 6;
    if (top + Math.min(maxH, items.length * 56) > window.innerHeight - 8) {
      top = Math.max(8, r.top - 6 - Math.min(maxH, items.length * 56));
    }
    drop.style.top = top + 'px';
    drop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 360)) + 'px';
    drop.style.width = Math.min(360, Math.max(280, r.width)) + 'px';
  }

  function render() {
    if (!items.length) { close(); return; }
    if (!drop) {
      drop = document.createElement('div');
      drop.className = 'slac-dropdown';
      drop.setAttribute('role', 'listbox');
      document.body.appendChild(drop);
      // mousedown (not click) so selection lands before textarea blur closes us
      drop.addEventListener('mousedown', (e) => {
        const el = e.target.closest('[data-slac-idx]');
        if (el) { e.preventDefault(); complete(parseInt(el.dataset.slacIdx, 10)); }
      });
    }
    drop.innerHTML = items.slice(0, 10).map((e, i) =>
      '<div class="slac-item' + (i === selIdx ? ' slac-sel' : '') + '" role="option" data-slac-idx="' + i + '">' +
        '<div class="slac-label">' + escapeHtml(e.label) + '</div>' +
        '<div class="slac-help">' + escapeHtml(e.help || e.usage || '') + '</div>' +
      '</div>'
    ).join('');
    position();
    const sel = drop.querySelector('.slac-sel');
    if (sel) sel.scrollIntoView({ block: 'nearest' });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function update() {
    if (composing) { close(); return; }
    ctx = tokenContext(ta);
    if (!ctx) { close(); return; }
    const ranked = rankEntries(buildEntries(ctx), ctx.partial).slice(0, 10);
    const changed = ranked.length !== items.length ||
      ranked.some((e, i) => !items[i] || items[i].label !== e.label);
    items = ranked;
    if (selIdx >= items.length) selIdx = 0;
    if (changed || !drop) render();
    else position();
  }

  function complete(i) {
    const e = items[i];
    if (!e || !ctx) return;
    const pos = ta.selectionStart;
    const before = ta.value.slice(0, ctx.tokenStart);
    const after = ta.value.slice(pos);
    ta.value = before + e.insert + after;
    const newPos = ctx.tokenStart + e.insert.length;
    ta.setSelectionRange(newPos, newPos);
    ta.focus();
    // Re-run so "/session " immediately offers subcommands
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  ta.addEventListener('compositionstart', () => { composing = true; close(); });
  ta.addEventListener('compositionend', () => { composing = false; });

  ta.addEventListener('input', update);

  ta.addEventListener('keydown', (e) => {
    if (!drop || !items.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault(); e.stopPropagation();
      selIdx = (selIdx + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      render();
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault(); e.stopPropagation();
      complete(selIdx);
    } else if (e.key === 'Escape') {
      e.preventDefault(); e.stopPropagation();
      close();
    }
  }, true); // capture: beat the chat send handler to Enter/Tab/Esc

  ta.addEventListener('blur', () => setTimeout(close, 150));
  window.addEventListener('resize', () => { if (drop) position(); });
  document.addEventListener('scroll', () => { if (drop) position(); }, true);
}
