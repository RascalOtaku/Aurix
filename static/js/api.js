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
//   - per-call timeout via AbortController (default 30s)
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

// ── Core request ───────────────────────────────────────────────────────────
export async function api(path, options = {}) {
  const {
    method = 'GET',
    body,
    headers = {},
    redirectOnAuth = true,
    timeoutMs = 30000,
    // any other fetch init fields (signal, cache, ...) pass straight through
    ...init
  } = options;

  const url = apiUrl(path);
  const controller = new AbortController();
  const timer = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;

  const finalHeaders = { ...headers };
  let payload = body;
  if (body !== undefined && body !== null && typeof body === 'object' && !(body instanceof FormData) && !(body instanceof Blob) && !(body instanceof ArrayBuffer)) {
    payload = JSON.stringify(body);
    if (!finalHeaders['Content-Type'] && !finalHeaders['content-type']) {
      finalHeaders['Content-Type'] = 'application/json';
    }
  }

  let res;
  try {
    res = await fetch(url, {
      method,
      credentials: 'same-origin',
      headers: finalHeaders,
      body: payload,
      signal: controller.signal,
      ...init,
    });
  } catch (err) {
    // Network failure, CORS block, or our own timeout abort.
    if (err && err.name === 'AbortError' && timeoutMs > 0) {
      throw new ApiError(0, `Request timed out after ${timeoutMs}ms`, url);
    }
    throw new ApiError(0, (err && err.message) || 'Network request failed', url);
  } finally {
    if (timer) clearTimeout(timer);
  }

  if (res.status === 401 && redirectOnAuth && typeof window !== 'undefined') {
    window.location.href = apiUrl('/login');
    // Return a never-resolving promise so callers don't continue on the old page.
    return new Promise(() => {});
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

// Raw variant for non-JSON responses (downloads, SSE setup, etc.).
export async function apiRaw(path, options = {}) {
  const url = apiUrl(path);
  const { timeoutMs = 30000, ...init } = options;
  const controller = new AbortController();
  const timer = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const res = await fetch(url, { credentials: 'same-origin', signal: controller.signal, ...init });
    if (res.status === 401 && options.redirectOnAuth !== false && typeof window !== 'undefined') {
      window.location.href = apiUrl('/login');
      return new Promise(() => {});
    }
    if (!res.ok) throw new ApiError(res.status, null, url);
    return res;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// ── Convenience methods ────────────────────────────────────────────────────
export const get = (path, opts) => api(path, { ...opts, method: 'GET' });
export const post = (path, body, opts) => api(path, { ...opts, method: 'POST', body });
export const put = (path, body, opts) => api(path, { ...opts, method: 'PUT', body });
export const patch = (path, body, opts) => api(path, { ...opts, method: 'PATCH', body });
// `delete` is reserved — exported as `del`.
export const del = (path, opts) => api(path, { ...opts, method: 'DELETE' });

export default { api, apiRaw, get, post, put, patch, del, apiUrl, basePath, ApiError };
