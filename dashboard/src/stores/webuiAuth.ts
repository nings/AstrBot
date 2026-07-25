import { defineStore } from 'pinia';
import { webuiApi } from '@/api/webui';

// CSRF cookie set by astrbot/webui/auth/security.py (non-httpOnly, Path=/).
// api/webui.ts's request interceptor is the one that actually attaches the
// X-Webui-CSRF-Token header on unsafe methods for axios calls made through
// `webuiApi`; this store additionally exposes the raw token (via the
// `csrfToken` getter) for any non-axios caller (e.g. a raw `fetch`/EventSource
// used by the SSE chat stream) that needs to read/attach it manually.
const CSRF_COOKIE_NAME = 'astrbot_webui_csrf';

function readCsrfCookie(): string | null {
  if (typeof document === 'undefined') return null;
  const escaped = CSRF_COOKIE_NAME.replace(/([.$?*|{}()[\]\\/+^])/g, '\\$1');
  const match = document.cookie.match(new RegExp(`(?:^|; )${escaped}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

function extractErrorMessage(err: unknown): string {
  const anyErr = err as { response?: { data?: { message?: string } }; message?: string } | undefined;
  return anyErr?.response?.data?.message || anyErr?.message || '请求失败，请稍后重试';
}

// Shape of astrbot/webui/db/models.py's WebUiUser, as returned by GET /auth/me
// (and echoed by /auth/register and /auth/login). password_hash is never sent
// to the client — it isn't part of this contract.
export interface WebuiUser {
  user_id: string;
  email: string;
  display_name: string | null;
  status: string;
  role: string;
  created_at?: string;
}

interface WebuiAuthState {
  user: WebuiUser | null;
  // Whether we've ever resolved the current-session state (successfully or
  // not) since page load — lets the router guard avoid re-fetching /auth/me
  // on every navigation.
  initialized: boolean;
  loading: boolean;
  error: string | null;
}

function unwrapUser(payload: unknown): WebuiUser | null {
  if (!payload || typeof payload !== 'object') return null;
  const data = payload as Record<string, unknown>;
  const candidate = (data.user ?? data.data ?? data) as Record<string, unknown> | undefined;
  if (candidate && typeof candidate === 'object' && 'user_id' in candidate) {
    return candidate as unknown as WebuiUser;
  }
  return null;
}

export const useWebuiAuthStore = defineStore('webuiAuth', {
  state: (): WebuiAuthState => ({
    user: null,
    initialized: false,
    loading: false,
    error: null,
  }),

  getters: {
    isAuthenticated: (state): boolean => !!state.user,
    csrfToken: (): string | null => readCsrfCookie(),
  },

  actions: {
    async register(email: string, password: string, displayName?: string): Promise<WebuiUser | null> {
      this.loading = true;
      this.error = null;
      try {
        const res = await webuiApi.post('/auth/register', {
          email,
          password,
          display_name: displayName ?? null,
        });
        this.user = unwrapUser(res.data);
        this.initialized = true;
        return this.user;
      } catch (err) {
        this.error = extractErrorMessage(err);
        throw err;
      } finally {
        this.loading = false;
      }
    },

    async login(email: string, password: string): Promise<WebuiUser | null> {
      this.loading = true;
      this.error = null;
      try {
        const res = await webuiApi.post('/auth/login', { email, password });
        this.user = unwrapUser(res.data);
        this.initialized = true;
        return this.user;
      } catch (err) {
        this.error = extractErrorMessage(err);
        throw err;
      } finally {
        this.loading = false;
      }
    },

    async logout(): Promise<void> {
      try {
        await webuiApi.post('/auth/logout');
      } catch {
        // Logout is permissive/idempotent server-side (per session_service.py's
        // contract) — always clear local state even if the request itself
        // failed (e.g. already-expired session).
      } finally {
        this.user = null;
        this.initialized = true;
        this.error = null;
      }
    },

    /** Attempts a refresh-token rotation. Returns false (and clears the local
     * user) if the refresh token is missing, expired, or was replayed. */
    async refresh(): Promise<boolean> {
      try {
        await webuiApi.post('/auth/refresh');
        return true;
      } catch {
        this.user = null;
        return false;
      } finally {
        this.initialized = true;
      }
    },

    /** Resolves the current session by calling GET /auth/me. Used by the
     * router guard on first navigation after a page load/reload. */
    async fetchCurrentUser(): Promise<WebuiUser | null> {
      this.loading = true;
      try {
        const res = await webuiApi.get('/auth/me');
        this.user = unwrapUser(res.data);
        return this.user;
      } catch {
        this.user = null;
        return null;
      } finally {
        this.loading = false;
        this.initialized = true;
      }
    },
  },
});
