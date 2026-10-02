// static/js/api.js
// Centralized API client for the Aurix frontend.
//
// Concept ported from upstream odysseus PR #4379 ("feat(axios)"), reimplemented
// dependency-free: no Axios, no CDN script tags — just a thin wrapper over
// fetch() that every UI module should use instead of calling fetch() directly.
//
// What it gives every call site for free:
//   - base-path-aware URLs (see basePath())
//   - credentials: 'same-origin' (the repo-wide convention)
//   - JSON request bodies serialized automatically
//   - consistent error parsing (FastAPI-style `detail` field)
//   - 401 responses redirect to the login page (opt out per call)
//   - per-call timeout via AbortController (default 30s). A caller-supplied
//     `signal` is COMBINED with the timeout (whichever fires first wins) —
//     it no longer silently disables the timeout.
//   - automatic retry with backoff on 429/503 for idempotent methods
//     (GET/HEAD/OPTIONS/DELETE, 2 retries by default; opt out with
//     `retries: 0`, opt in for other methods with `retries: N`).
//
// Usage:
//   import { get, post, ApiError } from './api.js';
//   const data = await get('/api/sessions');
//   await post('/api/chat', { message: 'hi' });
//   try { ... } catch (e) { if (e instanceof ApiError) ... }

// ── Base path ──────────────────────────────────────────────────────────────
// For reverse-proxy subpath deployments. If a backend ever injects
// window.__AURIX_BASE_PATH (e.g. "/aurix"), all API URLs and the login
// redirect are prefixed with it. Defaults to '' (app served from root),
// so this is a no-op until such support exists server-side.
export function basePath() {
  const bp = (typeof window !== 'undefined' && window.__AURIX_BASE_PATH) || '';
  return bp.endsWith('/') && bp.length > 1 ? bp.slice(0, -1) : bp;
}

// Join the base path with an API path without producing double slashes.
export function apiUrl(path) {
  const bp = basePath();
  const p = String(path || '');
  if (!bp) return p;
  return bp + (p.startsWith('/') ? p : '/' + p);
}

// ── Errors ─────────────────────────────────────────────────────────────────
export class ApiError extends Error {
  constructor(status, detail, url) {
    super(detail || `Request failed with status ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail || null;
    this.url = url || null;
  }
}

// ── Signals ────────────────────────────────────────────────────────────────
// Combine the per-attempt timeout signal with an optional caller signal so
// that a caller-passed `signal` can never silently disable the timeout.
// Whichever fires first wins.
function combineSignals(timeoutSignal, callerSignal) {
  if (!callerSignal) return timeoutSignal;
  if (typeof AbortSignal.any === 'function') {
    return AbortSignal.any([timeoutSignal, callerSignal]);
  }
  // Fallback for runtimes without AbortSignal.any: forward either abort.
  const ctrl = new AbortController();
  const onAbort = () => ctrl.abort();
  if (timeoutSignal.aborted || callerSignal.aborted) {
    ctrl.abort();
  } else {
    timeoutSignal.addEventListener('abort', onAbort, { once: true });
    callerSignal.addEventListener('abort', onAbort, { once: true });
  }
  return ctrl.signal;
}

// ── Retry ──────────────────────────────────────────────────────────────────
const IDEMPOTENT_METHODS = new Set(['GET', 'HEAD', 'OPTIONS', 'DELETE']);
const RETRYABLE_STATUS = new Set([429, 503]);

// Backoff for attempt N (0-based): exponential on retryDelayMs with jitter,
// honoring the server's Retry-After header when present (capped at 30s).
function retryDelayMsFor(res, attempt, baseMs) {
  const raw = res.headers.get('retry-after');
  if (raw != null && raw !== '') {
    const secs = Number(raw);
    if (Number.isFinite(secs) && secs >= 0) return Math.min(secs * 1000, 30000);
    const dateMs = Date.parse(raw);
    if (!Number.isNaN(dateMs)) return Math.min(Math.max(dateMs - Date.now(), 0), 30000);
  }
  const exp = baseMs * 2 ** attempt;
  return Math.floor(exp * (0.75 + Math.random() * 0.5));
}

function sleepWithAbort(ms, signal) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      if (signal) signal.removeEventListener('abort', onAbort);
    };
    const onAbort = () => {
      clearTimeout(t);
      cleanup();
      reject(new DOMException('Aborted', 'AbortError'));
    };
    if (signal && signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const t = setTimeout(() => {
      cleanup();
      resolve();
    }, ms);
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
  });
}

// ── Core request ───────────────────────────────────────────────────────────
export async function api(path, options = {}) {
  const {
    method = 'GET',
    body,
    headers = {},
    redirectOnAuth = true,
    timeoutMs = 30000,
    signal: callerSignal,
    retries,
    retryDelayMs = 500,
    // any other fetch init fields (cache, ...) pass straight through.
    // NOTE: `signal` is destructured above and combined with the timeout —
    // it is intentionally NOT part of ...init anymore.
    ...init
  } = options;

  const upperMethod = String(method).toUpperCase();
  const maxRetries =
    retries !== undefined ? Math.max(0, retries | 0)
    : IDEMPOTENT_METHODS.has(upperMethod) ? 2
    : 0;

  const url = apiUrl(path);

  const finalHeaders = { ...headers };
  let payload = body;
  if (body !== undefined && body !== null && typeof body === 'object' && !(body instanceof FormData) && !(body instanceof Blob) && !(body instanceof ArrayBuffer)) {
    payload = JSON.stringify(body);
    if (!finalHeaders['Content-Type'] && !finalHeaders['content-type']) {
      finalHeaders['Content-Type'] = 'application/json';
    }
  }

  let attempt = 0;
  for (;;) {
    const ctrl = new AbortController();
    let timedOut = false;
    const timer = timeoutMs > 0 ? setTimeout(() => { timedOut = true; ctrl.abort(); }, timeoutMs) : null;
    const signal = combineSignals(ctrl.signal, callerSignal);

    let res;
    try {
      res = await fetch(url, {
        method,
        credentials: 'same-origin',
        headers: finalHeaders,
        body: payload,
        signal,
        ...init,
      });
    } catch (err) {
      if (timer) clearTimeout(timer);
      // Network failure, CORS block, caller abort, or our own timeout.
      if (err && err.name === 'AbortError') {
        throw timedOut
          ? new ApiError(0, `Request timed out after ${timeoutMs}ms`, url)
          : new ApiError(0, 'Request aborted', url);
      }
      throw new ApiError(0, (err && err.message) || 'Network request failed', url);
    }
    if (timer) clearTimeout(timer);

    if (res.status === 401 && redirectOnAuth && typeof window !== 'undefined') {
      window.location.href = apiUrl('/login');
      // Return a never-resolving promise so callers don't continue on the old page.
      return new Promise(() => {});
    }

    if (RETRYABLE_STATUS.has(res.status) && attempt < maxRetries) {
      // Drain the body so the connection can be reused, then back off.
      await res.arrayBuffer().catch(() => null);
      const waitMs = retryDelayMsFor(res, attempt, retryDelayMs);
      attempt += 1;
      try {
        await sleepWithAbort(waitMs, signal);
      } catch {
        throw new ApiError(0, 'Request aborted', url);
      }
      continue;
    }

    const contentType = res.headers.get('content-type') || '';
    let data = null;
    if (contentType.includes('application/json')) {
      data = await res.json().catch(() => null);
    } else {
      // Non-JSON success bodies (blobs, text) are returned raw via apiRaw().
      data = await res.text().catch(() => null);
    }

    if (!res.ok) {
      const detail =
        (data && typeof data === 'object' && (data.detail || data.message)) ||
        (typeof data === 'string' && data) ||
        null;
      throw new ApiError(res.status, detail, url);
    }
    return data;
  }
}

// Raw variant for non-JSON responses (downloads, SSE setup, etc.).
export async function apiRaw(path, options = {}) {
  const url = apiUrl(path);
  const { timeoutMs = 30000, signal: callerSignal, ...init } = options;
  const ctrl = new AbortController();
  let timedOut = false;
  const timer = timeoutMs > 0 ? setTimeout(() => { timedOut = true; ctrl.abort(); }, timeoutMs) : null;
  const signal = combineSignals(ctrl.signal, callerSignal);
  try {
    const res = await fetch(url, { credentials: 'same-origin', signal, ...init });
    if (res.status === 401 && options.redirectOnAuth !== false && typeof window !== 'undefined') {
      window.location.href = apiUrl('/login');
      return new Promise(() => {});
    }
    if (!res.ok) throw new ApiError(res.status, null, url);
    return res;
  } catch (err) {
    if (err && err.name === 'AbortError') {
      throw timedOut
        ? new ApiError(0, `Request timed out after ${timeoutMs}ms`, url)
        : new ApiError(0, 'Request aborted', url);
    }
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// ── Convenience methods ────────────────────────────────────────────────────
export const get = (path, opts) => api(path, { ...opts, method: 'GET' });
export const post = (path, body, opts) => api(path, { ...opts, method: 'POST', body });
export const put = (path, body, opts) => api(path, { ...opts, method: 'PUT', body });
export const patch = (path, body, opts) => api(path, { ...opts, method: 'PATCH', body });
// `delete` is reserved — exported as `del`. Carries an optional JSON body:
// del('/api/items/1') or del('/api/items', { ids: [1, 2] }).
export const del = (path, body, opts) => api(path, { ...opts, method: 'DELETE', body });

export default { api, apiRaw, get, post, put, patch, del, apiUrl, basePath, ApiError };
