// static/js/storage.js
// Centralized localStorage access with key constants and JSON parse safety

// ── Key constants ──
export const KEYS = {
  THEME: 'aurix-theme',
  TOGGLES: 'aurix-toggles',
  SIDEBAR_COLLAPSED: 'sidebar-collapsed',
  SIDEBAR_WIDTH: 'sidebar-width',
  SIDEBAR_SIDE: 'sidebar-side',
  CURRENT_SESSION: 'currentSessionId',
  COMPARE_SAVE: 'compare-save-results',
  COMPARE_CHAT: 'compare-continue-chat',
  COMPARE_BLIND: 'compare-blind',
  COMPARE_RANDOM: 'compare-randomize',
  MODELS_EXPANDED: 'aurix-model-expanded',
  MODEL_ENDPOINTS: 'aurix-model-endpoints',
  MODEL_SELECTED: 'aurix-selected-model',
  SORT_ORDER: 'aurix-sessions-sort',
  CHAT_SEARCH_SCOPE: 'aurix-search-scope',
  INCOGNITO: 'aurix-incognito',
  RAG_ACTIVE: 'aurix-rag-active',
  MCP_ACTIVE: 'aurix-mcp-active',
  SECTION_ORDER: 'sidebar-section-order',
  ADMIN_LAST_TAB: 'admin-last-tab',
  DENSITY: 'aurix-density'
};

// ── Legacy storage-key migration (Odysseus → Aurix rename) ──
// Copy any legacy `odysseus…` storage keys to their `aurix…` equivalents
// so user settings (theme, favourites, UI state…) survive the app
// rename. Runs on every load: it only copies when the new key is
// absent, and the legacy key is left in place (harmless) so rolling
// back to the old build still finds its settings. Keep this
// permanently — legacy keys can resurface from old backups at any
// time.
(function migrateLegacyKeys() {
  const stores = [];
  try { stores.push(localStorage); } catch (_) {}
  try { stores.push(sessionStorage); } catch (_) {}
  for (const store of stores) {
    try {
      const legacy = [];
      for (let i = 0; i < store.length; i++) {
        const k = store.key(i);
        if (k && k.startsWith('odysseus')) legacy.push(k);
      }
      for (const oldKey of legacy) {
        const newKey = 'aurix' + oldKey.slice('odysseus'.length);
        if (store.getItem(newKey) === null) {
          const v = store.getItem(oldKey);
          if (v !== null) store.setItem(newKey, v);
        }
      }
    } catch (_) {}
  }
})();

/**
 * Safely get and parse a JSON value from localStorage.
 * Returns fallback on any error.
 */
export function getJSON(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback !== undefined ? fallback : null;
    return JSON.parse(raw);
  } catch (e) {
    console.warn('[Storage] Failed to parse key "' + key + '":', e.message);
    return fallback !== undefined ? fallback : null;
  }
}

/**
 * Set a JSON-serialized value in localStorage.
 */
export function setJSON(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (e) {
    console.warn('[Storage] Failed to set key "' + key + '":', e.message);
  }
}

/**
 * Get a raw string value from localStorage.
 */
export function get(key, fallback) {
  try {
    const val = localStorage.getItem(key);
    return val !== null ? val : (fallback !== undefined ? fallback : null);
  } catch (e) {
    return fallback !== undefined ? fallback : null;
  }
}

/**
 * Set a raw string value in localStorage.
 */
export function set(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (e) {
    console.warn('[Storage] Failed to set key "' + key + '":', e.message);
  }
}

/**
 * Remove a key from localStorage.
 */
export function remove(key) {
  try {
    localStorage.removeItem(key);
  } catch (e) {
    // Ignore removal errors
  }
}

// ── Toggle state helpers ──

export function loadToggleState() {
  return getJSON(KEYS.TOGGLES, {});
}

export function saveToggleState(state) {
  setJSON(KEYS.TOGGLES, state);
}

export function getToggle(name, fallback) {
  const state = loadToggleState();
  return state[name] !== undefined ? state[name] : (fallback !== undefined ? fallback : false);
}

export function setToggle(name, value) {
  const state = loadToggleState();
  state[name] = value;
  saveToggleState(state);
}

const Storage = {
  KEYS,
  getJSON,
  setJSON,
  get,
  set,
  remove,
  loadToggleState,
  saveToggleState,
  getToggle,
  setToggle
};

export default Storage;
