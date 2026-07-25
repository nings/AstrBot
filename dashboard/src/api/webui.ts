import axios, {
  type AxiosError,
  type AxiosInstance,
  type InternalAxiosRequestConfig,
} from 'axios';

// Dedicated axios instance for the end-user (multi-user) webui SPA.
//
// This intentionally does NOT reuse dashboard/src/api/http.ts's `httpClient`/
// `apiV1Client` or its Bearer-token/localStorage-based auth handling — the
// webui auth model is cookie + CSRF based (see astrbot/webui/auth/*), a
// completely separate session domain from the operator dashboard's JWT.

const WEBUI_API_BASE_URL = '/api/v1/webui';
const CSRF_COOKIE_NAME = 'astrbot_webui_csrf';
const CSRF_HEADER_NAME = 'X-Webui-CSRF-Token';
const REFRESH_PATH = '/auth/refresh';
const UNSAFE_METHODS = new Set(['post', 'put', 'patch', 'delete']);

/** Reads the non-httpOnly CSRF cookie set by astrbot/webui/auth/security.py. */
function readCsrfCookie(): string | null {
  if (typeof document === 'undefined') return null;
  const escaped = CSRF_COOKIE_NAME.replace(/([.$?*|{}()[\]\\/+^])/g, '\\$1');
  const match = document.cookie.match(new RegExp(`(?:^|; )${escaped}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

function setHeader(config: InternalAxiosRequestConfig, key: string, value: string) {
  const headers = config.headers;
  if (headers && typeof (headers as any).set === 'function') {
    (headers as any).set(key, value);
  } else {
    config.headers = { ...(config.headers as any), [key]: value } as any;
  }
}

export const webuiApi: AxiosInstance = axios.create({
  baseURL: WEBUI_API_BASE_URL,
  withCredentials: true,
});

// Attach the CSRF header (double-submit pattern) on every state-changing
// request. GETs are exempt — there is nothing to protect against CSRF-wise
// for a read-only request, and it keeps the header list minimal.
webuiApi.interceptors.request.use((config) => {
  const method = (config.method || 'get').toLowerCase();
  if (UNSAFE_METHODS.has(method)) {
    const token = readCsrfCookie();
    if (token) {
      setHeader(config, CSRF_HEADER_NAME, token);
    }
  }
  return config;
});

type RetriableConfig = InternalAxiosRequestConfig & { _webuiRetried?: boolean };

let pendingRefresh: Promise<boolean> | null = null;

/** Ensures only one refresh request is ever in flight at a time. */
function refreshOnce(): Promise<boolean> {
  if (!pendingRefresh) {
    pendingRefresh = webuiApi
      .post(REFRESH_PATH)
      .then(() => true)
      .catch(() => false)
      .finally(() => {
        pendingRefresh = null;
      });
  }
  return pendingRefresh;
}

function redirectToLogin() {
  if (typeof window === 'undefined') return;
  // The webui SPA is hash-routed (createWebHashHistory, base '/webui/') so a
  // hard reload isn't required — but if the app hasn't mounted yet (e.g. this
  // 401 happened during the very first boot request) forcing the hash is
  // still the correct, router-agnostic way to land on the login screen.
  if (!window.location.hash.startsWith('#/login')) {
    window.location.hash = '/login';
  }
}

webuiApi.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as RetriableConfig | undefined;
    const status = error.response?.status;
    const requestUrl = originalRequest?.url || '';
    const isAuthEndpoint =
      requestUrl.includes('/auth/refresh') ||
      requestUrl.includes('/auth/login') ||
      requestUrl.includes('/auth/register');

    if (status === 401 && originalRequest && !originalRequest._webuiRetried && !isAuthEndpoint) {
      originalRequest._webuiRetried = true;
      const refreshed = await refreshOnce();
      if (refreshed) {
        return webuiApi(originalRequest);
      }
      redirectToLogin();
    }

    return Promise.reject(error);
  },
);

export default webuiApi;
