import { createRouter, createWebHashHistory, type Router, type RouteRecordRaw } from 'vue-router';
import { usePortalAuthStore } from '@/stores/portalAuth';

// Routes for the end-user (multi-user) portal SPA. Components live under
// dashboard/src/components/portal/** (owned by a different Phase 1 workstream
// per the plan — imported here by path, same convention as the backend's
// multi-agent build referencing not-yet-landed sibling modules).
//
// Route names are prefixed with `portal-` to avoid any accidental collision if
// this router config were ever imported alongside the operator dashboard's
// MainRoutes/AuthRoutes (it never is at runtime — portal-main.ts is a fully
// separate entry/bundle — but keeping the names disjoint costs nothing).
const PUBLIC_ROUTE_NAMES = new Set(['portal-login', 'portal-register']);

// Phase 5: two routes that must be reachable regardless of auth state, and
// must NEVER be bounced by the "already authenticated -> /chat" rule
// PUBLIC_ROUTE_NAMES gets below. `portal-invite-accept` is reached by an
// anonymous invitee following an operator-delivered one-time link.
// `portal-oauth-callback` is reached by BOTH an anonymous login-purpose SSO
// flow AND an authenticated link-purpose SSO flow (started from inside an
// already-logged-in session, see astrbot/portal/services/sso_service.py's
// module docstring) — bouncing an authenticated caller away from it here
// would make account-linking impossible to complete. Each page decides for
// itself what to render for either case; the router's only job is to never
// gate them on auth status at all.
const ALWAYS_ACCESSIBLE_ROUTE_NAMES = new Set(['portal-invite-accept', 'portal-oauth-callback']);

const PORTAL_BASE_PATH = '/portal';

/**
 * Bridges a real (non-hash) server/IdP-issued browser navigation into this
 * SPA's own hash-history route space, before the router is even created.
 *
 * Two Phase 5 entry points are, by protocol necessity, plain paths with
 * query params attached by something that knows nothing about this SPA's
 * `createWebHashHistory` routing:
 *
 * - The invite-acceptance link an operator delivers out-of-band is built
 *   literally as `/portal/invite/accept?token=<...>` by
 *   `astrbot/portal/api/admin_routes.py`'s `create_invite`.
 * - Every SSO provider's `redirect_uri` is built as
 *   `{sso_redirect_base_url}/oauth/callback/{provider}` by
 *   `astrbot/portal/services/sso_service.py`'s `_redirect_uri`, and an
 *   OAuth2 authorization server always appends `code`/`state` (or
 *   `error`/`error_description`) to that exact URL's query string on
 *   redirect — it has no notion of a `#` fragment to preserve.
 *
 * Both land on `static_router.py`'s SPA-fallback response (`portal.html`,
 * since neither path's last segment looks like a static asset filename)
 * with an empty `location.hash` and the real path + query sitting in
 * `location.pathname`/`location.search` instead — exactly where
 * `createWebHashHistory` never looks. Without this bridge the hash router
 * would see a blank hash on first load, match the default `/` route
 * (redirect to `/chat`), and silently swallow the invite token / OAuth
 * code+state, leaving the user on the chat shell (or bounced to `/login`)
 * with no explanation at all — precisely the "blank screen" failure mode
 * this phase's assignment calls out.
 *
 * Deliberately narrow: only rewrites when `location.hash` is already empty
 * (a real top-level navigation, not something already inside this SPA's own
 * hash routing) AND the path is one of exactly these two well-known
 * non-SPA entry shapes. Any other hash-less deep link (e.g. someone
 * bookmarking `/portal/chat/123` without the hash) is left alone, matching
 * this hash router's pre-existing behavior — this function only ever
 * fixes the two new cases Phase 5 introduces, it does not change what
 * happens for anything else.
 */
function bridgeServerRedirectIntoHash(): void {
  if (typeof window === 'undefined') return;
  if (window.location.hash) return;

  const path = window.location.pathname;
  if (!path.startsWith(`${PORTAL_BASE_PATH}/`)) return;
  const relativePath = path.slice(PORTAL_BASE_PATH.length); // e.g. "/invite/accept"

  const isInviteAccept = relativePath === '/invite/accept';
  const isOAuthCallback = /^\/oauth\/callback\/[^/]+$/.test(relativePath);
  if (!isInviteAccept && !isOAuthCallback) return;

  window.location.hash = `${relativePath}${window.location.search}`;
}

export const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: { name: 'portal-chat' },
  },
  {
    name: 'portal-login',
    path: '/login',
    component: () => import('@/components/portal/LoginPage.vue'),
  },
  {
    name: 'portal-register',
    path: '/register',
    component: () => import('@/components/portal/RegisterPage.vue'),
  },
  {
    name: 'portal-chat',
    path: '/chat',
    component: () => import('@/components/portal/PortalChatShell.vue'),
  },
  {
    name: 'portal-chat-session',
    path: '/chat/:sessionId',
    component: () => import('@/components/portal/PortalChatShell.vue'),
    props: true,
  },
  {
    // Reached only via bridgeServerRedirectIntoHash() rewriting the real
    // `/portal/invite/accept?token=...` navigation an operator-delivered
    // invite link performs (Phase 5a) — see that function's own docstring.
    name: 'portal-invite-accept',
    path: '/invite/accept',
    component: () => import('@/components/portal/InviteAcceptPage.vue'),
  },
  {
    // Reached only via bridgeServerRedirectIntoHash() rewriting the real
    // `/portal/oauth/callback/{provider}?code=...&state=...` navigation an
    // SSO provider redirects the browser to (Phase 5b) — see that
    // function's own docstring.
    name: 'portal-oauth-callback',
    path: '/oauth/callback/:provider',
    component: () => import('@/components/portal/OAuthCallbackPage.vue'),
    props: true,
  },
  {
    // Anything unrecognized falls back to the chat shell (which itself
    // redirects to /login via the guard below if unauthenticated) rather than
    // a blank screen.
    path: '/:pathMatch(.*)*',
    redirect: { name: 'portal-chat' },
  },
];

/**
 * Builds the standalone router for the end-user portal SPA, including its
 * authentication navigation guard.
 *
 * Guard contract: any route other than /login, /register, /invite/accept or
 * /oauth/callback/:provider requires an authenticated session. On first
 * navigation after a page load, the guard resolves the session exactly once
 * via GET /api/v1/portal/auth/me (through usePortalAuthStore().fetchCurrentUser());
 * subsequent navigations reuse the cached result. Unauthenticated users are
 * redirected to /login with the originally-requested path preserved in
 * `?redirect=`. Already-authenticated users hitting /login or /register are
 * bounced to /chat instead. /invite/accept and /oauth/callback/:provider are
 * never gated AND never bounced regardless of auth state (see
 * ALWAYS_ACCESSIBLE_ROUTE_NAMES's own docstring above for why both
 * directions matter for those two specifically).
 */
export function createEndUserRouter(): Router {
  // Must run BEFORE createRouter() below: createWebHashHistory reads
  // window.location.hash synchronously while resolving its initial route,
  // so the rewrite has to already be in place by the time that happens.
  bridgeServerRedirectIntoHash();

  const router = createRouter({
    // Base must align with astrbot/portal/api/static_router.py serving this
    // SPA at /portal — with hash history that means the served URL looks like
    // /portal/#/chat.
    history: createWebHashHistory('/portal/'),
    routes,
  });

  router.beforeEach(async (to) => {
    const auth = usePortalAuthStore();
    const routeName = typeof to.name === 'string' ? to.name : undefined;

    if (routeName && ALWAYS_ACCESSIBLE_ROUTE_NAMES.has(routeName)) {
      // Neither gated on auth state nor ever bounced elsewhere — see this
      // set's own docstring above for why both directions matter here.
      return true;
    }

    if (routeName && PUBLIC_ROUTE_NAMES.has(routeName)) {
      if (!auth.initialized) {
        await auth.fetchCurrentUser();
      }
      if (auth.isAuthenticated) {
        return { name: 'portal-chat' };
      }
      return true;
    }

    if (!auth.initialized) {
      await auth.fetchCurrentUser();
    }

    if (!auth.isAuthenticated) {
      return {
        name: 'portal-login',
        query: to.fullPath && to.fullPath !== '/' ? { redirect: to.fullPath } : undefined,
      };
    }

    return true;
  });

  return router;
}
