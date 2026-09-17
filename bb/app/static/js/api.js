/* HTTP layer and session state.
 *
 * The access token lives in memory only. The refresh token goes to
 * sessionStorage so a reload survives, without leaving a long-lived credential
 * in localStorage where any XSS would find it and where it outlives the tab.
 */

const BASE = '/api/v1';
const REFRESH_KEY = 'bb.refresh';

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

function detailToMessage(payload, fallback) {
  const detail = payload && payload.detail;
  if (typeof detail === 'string') return detail;
  // FastAPI validation errors arrive as a list of {loc, msg}.
  if (Array.isArray(detail)) {
    return detail
      .map((e) => `${(e.loc || []).slice(1).join('.') || 'field'}: ${e.msg}`)
      .join('; ');
  }
  return fallback;
}

export const auth = {
  accessToken: null,
  refreshToken: null,
  user: null,
  tenant: null,

  get isAuthenticated() {
    return Boolean(this.accessToken);
  },
  get canWrite() {
    return ['owner', 'admin'].includes(this.user?.role);
  },

  persist(tokens) {
    this.accessToken = tokens.access_token;
    if (tokens.refresh_token) {
      this.refreshToken = tokens.refresh_token;
      try {
        sessionStorage.setItem(REFRESH_KEY, tokens.refresh_token);
      } catch { /* private mode: the session simply will not survive a reload */ }
    }
  },
  restore() {
    try {
      this.refreshToken = sessionStorage.getItem(REFRESH_KEY);
    } catch {
      this.refreshToken = null;
    }
    return this.refreshToken;
  },
  clear() {
    this.accessToken = null;
    this.refreshToken = null;
    this.user = null;
    this.tenant = null;
    try {
      sessionStorage.removeItem(REFRESH_KEY);
    } catch { /* nothing to clean up */ }
  },
};

let refreshing = null;

async function refreshAccessToken() {
  // Deliberately a bare fetch rather than raw(), to avoid recursing on 401.
  const response = await fetch(
    `${BASE}/auth/refresh?refresh_token=${encodeURIComponent(auth.refreshToken)}`,
    { method: 'POST' }
  );
  if (!response.ok) return false;
  auth.persist(await response.json());
  return true;
}

async function raw(path, options = {}, retry = true) {
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
  }

  const authenticated = Boolean(auth.accessToken);
  if (authenticated) headers.Authorization = `Bearer ${auth.accessToken}`;

  const response = await fetch(BASE + path, { ...options, headers });

  // A 401 on a request that carried no token is a credential failure — a wrong
  // password, say — so it falls through and the caller shows the server's own
  // message. A 401 on an authenticated request means the token expired.
  if (response.status === 401 && authenticated) {
    if (retry && auth.refreshToken) {
      refreshing = refreshing || refreshAccessToken();
      const ok = await refreshing;
      refreshing = null;
      if (ok) return raw(path, options, false);
    }
    auth.clear();
    window.dispatchEvent(new CustomEvent('bb:signed-out'));
    throw new ApiError('Your session has expired. Please sign in again.', 401);
  }

  return response;
}

async function request(path, options) {
  const response = await raw(path, options);
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!response.ok) {
    throw new ApiError(
      detailToMessage(payload, `Request failed (HTTP ${response.status})`),
      response.status,
      payload
    );
  }
  return payload;
}

export const api = {
  get: (path) => request(path, { method: 'GET' }),
  post: (path, body) =>
    request(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: (path, body) => request(path, { method: 'PATCH', body: JSON.stringify(body) }),
  del: (path) => request(path, { method: 'DELETE' }),
};

export async function loadSession() {
  const [user, tenant] = await Promise.all([api.get('/auth/me'), api.get('/tenant')]);
  auth.user = user;
  auth.tenant = tenant;
}
