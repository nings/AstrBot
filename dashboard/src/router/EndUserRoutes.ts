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
 * Guard contract: any route other than /login or /register requires an
 * authenticated session. On first navigation after a page load, the guard
 * resolves the session exactly once via GET /api/v1/portal/auth/me (through
 * usePortalAuthStore().fetchCurrentUser()); subsequent navigations reuse the
 * cached result. Unauthenticated users are redirected to /login with the
 * originally-requested path preserved in `?redirect=`. Already-authenticated
 * users hitting /login or /register are bounced to /chat instead.
 */
export function createEndUserRouter(): Router {
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
