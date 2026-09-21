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

  /** Which door this session was opened at: 'tenant' or 'staff'.
   *
   * Never stored separately. It is a claim inside the token, and every
   * response that hands us a token tells us what it is — including the refresh
   * that rehydrates a reloaded tab — so the server stays the only authority on
   * which surface we are in. */
  scope: 'tenant',

  /** The door the last session was opened at, kept deliberately across
   *  clear() so that a sign-out — or a token quietly expiring — returns to the
   *  page it came from instead of dropping a support engineer onto the
   *  customer login. */
  lastDoor: 'tenant',

  get isAuthenticated() {
    return Boolean(this.accessToken);
  },
  get canWrite() {
    return ['owner', 'admin'].includes(this.user?.role);
  },

  get isStaffSession() {
    return this.scope === 'staff';
  },

  /** Platform staff, not a role: it crosses accounts, roles never do.
   *
   * Both halves matter. The flag says this person may use the console; the
   * scope says the session in hand is the one the server will accept there. A
   * dual-role user signed in at the customer door has the flag and no console,
   * and showing them the nav would offer a screen that answers 403.
   *
   * Hiding it is still convenience only — the server checks every request. */
  get isPlatformAdmin() {
    return this.user?.is_platform_admin === true && this.isStaffSession;
  },

  persist(tokens) {
    this.accessToken = tokens.access_token;
    if (tokens.scope) this.scope = tokens.scope;
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
    // Only when there was something to clear: an expiry clears once in api.js
    // and again in the signed-out handler, and the second pass must not
    // overwrite the remembered door with the reset default.
    if (this.accessToken || this.refreshToken) this.lastDoor = this.scope;
    this.accessToken = null;
    this.refreshToken = null;
    this.user = null;
    this.tenant = null;
    this.scope = 'tenant';
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
  // /auth/me is the one route that takes either kind of token, which is why it
  // is safe to call before we know which shell to build.
  auth.user = await api.get('/auth/me');

  // A console session has no workspace to load. /tenant refuses a staff-scoped
  // token by design, and for someone who is both a customer and staff it would
  // quietly pull their own account into the console shell — the exact mixing of
  // hats the two doors exist to prevent.
  auth.tenant = auth.isStaffSession ? null : await api.get('/tenant');
}
