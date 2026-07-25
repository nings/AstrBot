import { createApp, defineComponent, h } from 'vue';
import { createPinia } from 'pinia';
import { RouterView } from 'vue-router';
import vuetify from './plugins/vuetify';
import { setupI18n } from './i18n/composables';
import { createEndUserRouter } from './router/EndUserRoutes';
import '@/scss/style.scss';

// Bootstrap for the end-user (multi-user) portal SPA — a physically separate
// entry from dashboard/src/main.ts (see portal.html), mirroring its overall
// init order (i18n -> app -> pinia -> router -> vuetify -> mount) but
// intentionally slimmed down: no Monaco/ApexCharts/print/confirm-dialog
// plugins and no operator-only theme-sync wiring, since none of those are
// reachable from the end-user chat surface.
//
// This file owns its own Pinia instance and its own vue-router instance
// (via createEndUserRouter(), see EndUserRoutes.ts) — it must never import
// dashboard/src/router or dashboard/src/stores/auth.ts (the operator
// dashboard's own JWT-based auth store), since the two SPAs have entirely
// separate auth/session domains (astrbot_portal_* cookies vs. the dashboard's
// Bearer token in localStorage).

// The portal SPA has no shared "App shell" component of its own in this
// workstream's file ownership (that lives under
// dashboard/src/components/portal/**, owned by a different Phase 1 task) — a
// trivial inline root that just renders the active route is sufficient and
// keeps this bootstrap fully self-contained.
const PortalRoot = defineComponent({
  name: 'PortalRoot',
  setup() {
    return () => h(RouterView);
  },
});

async function bootstrap() {
  try {
    await setupI18n();
  } catch (error) {
    // Never block mounting the app on i18n failing to load — fall back to
    // whatever default translations setupI18n already installed.
    console.error('[portal] i18n initialization failed:', error);
  }

  const app = createApp(PortalRoot);

  const pinia = createPinia();
  app.use(pinia);

  const router = createEndUserRouter();
  app.use(router);

  app.use(vuetify);

  await router.isReady();
  app.mount('#app');
}

void bootstrap();
