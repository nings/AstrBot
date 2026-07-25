<script setup lang="ts">
/**
 * SSO/OAuth callback landing page for the AstrBot Portal (Phase 5b).
 *
 * This is the SPA page every enabled provider's ``redirect_uri`` actually
 * points at -- ``astrbot/portal/services/sso_service.py``'s ``_redirect_uri``
 * builds it as ``f"{sso_redirect_base_url}/oauth/callback/{provider_id}"``,
 * a REAL (non-hash) browser navigation the identity provider issues after
 * the user authenticates/declines, with ``code``/``state`` (success) or
 * ``error``/``error_description`` (decline/failure at the IdP itself)
 * appended as query params. ``EndUserRoutes.ts`` bridges that one-time
 * real-path navigation into this SPA's own ``portal-oauth-callback`` hash
 * route (``bridgeServerRedirectIntoHash``) before this component ever
 * mounts -- by the time it runs, ``route.params.provider`` and
 * ``route.query`` are populated exactly like any other hash-routed
 * navigation.
 *
 * This page's own job is narrow and entirely a client of
 * ``GET /api/v1/portal/auth/sso/{provider}/callback`` (see
 * ``astrbot/portal/api/auth_routes.py``'s ``sso_callback`` -- it deliberately
 * returns JSON, not a redirect, precisely so this page can decide what to
 * render/where to navigate next): call it once with whatever ``code``/
 * ``state`` arrived, then branch on the JSON shape it returns:
 *
 * - ``{result: "login", user, redirect_to}``: a LOGIN-purpose flow
 *   completed. The response already set the same three auth cookies a
 *   normal ``/auth/login`` would (``sso_callback`` calls
 *   ``_set_auth_cookies`` itself) -- this page hydrates the store via
 *   ``fetchCurrentUser()`` (the same single source of truth
 *   ``InviteAcceptPage.vue`` uses) rather than trusting the embedded
 *   ``user`` object directly, then navigates to ``redirect_to`` (re-
 *   validated client-side too -- see ``normalizeRedirectTarget`` -- even
 *   though the server already validated it against the same ``/portal``
 *   allowlist; defense in depth costs nothing here).
 * - ``{result: "link_pending", provider, email, display_name,
 *   confirmation_token, expires_at}``: a LINK-purpose flow (started from an
 *   already-authenticated session) reached the callback but wrote nothing
 *   yet -- no ``PortalOAuthIdentity`` row exists until
 *   ``POST /auth/sso/{provider}/link/confirm`` is called, which THIS page
 *   is the only place that does, and only after an explicit, un-skippable
 *   confirmation click that spells out the actual consequence ("this third
 *   party identity will be able to sign in as your account") -- never
 *   auto-confirmed on arrival. See ``sso_service.py``'s own module
 *   docstring: this two-step shape (opaque token from the callback, a
 *   separate confirmed-by-the-same-authenticated-user call to act on it)
 *   is what makes the confirmation unforgeable.
 * - anything else (thrown error, or a response shape that matches neither
 *   of the above): a *visible* explanation, never a blank screen -- a
 *   rejected/expired ``state`` is exactly what a real CSRF attempt against
 *   this exact endpoint looks like from the outside, and a confused
 *   legitimate user (who just took too long, or double-clicked an old
 *   emailed... well, there is no email here, but double-opened a stale
 *   link) needs to be told plainly what happened and what to try next,
 *   not left staring at nothing.
 *
 * A provider-side decline (``?error=...``, no ``code``/``state`` at all --
 * e.g. the user clicked "Cancel" at the IdP's consent screen) is detected
 * before ever calling the backend at all: there is nothing genuine to
 * verify in that case, and showing a distinct "you cancelled" message
 * (rather than routing it through the generic "invalid state" branch,
 * which would be a technically-true but needlessly alarming way to
 * describe a user's own cancel click) is the more honest response.
 */
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useModuleI18n } from '@/i18n/composables';
import { usePortalAuthStore } from '@/stores/portalAuth';
import portalApi from '@/api/portal';

const { tm: t } = useModuleI18n('features/auth');
const route = useRoute();
const router = useRouter();
const authStore = usePortalAuthStore();

const PORTAL_BASE_PATH = '/portal';

type PageState = 'processing' | 'cancelled' | 'error' | 'linkConfirm' | 'linkConfirming' | 'linkSuccess' | 'redirecting';

type ErrorKind =
  | 'invalidState'
  | 'unavailable'
  | 'notProvisioned'
  | 'alreadyLinked'
  | 'notLinked'
  | 'linkConfirmationInvalid'
  | 'cannotRemoveLast'
  | 'temporary'
  | 'verificationFailed'
  | 'rateLimited'
  | 'network'
  | 'unknown';

interface PendingLink {
  provider: string;
  email: string;
  displayName: string | null;
  confirmationToken: string;
}

const state = ref<PageState>('processing');
const errorKind = ref<ErrorKind>('unknown');
const pendingLink = ref<PendingLink | null>(null);

const provider = computed(() => (typeof route.params.provider === 'string' ? route.params.provider : ''));

const providerLabel = computed(() => {
  const known = ['oidc', 'wecom', 'feishu'];
  return known.includes(provider.value) ? t(`portal.oauth.providerNames.${provider.value}`) : provider.value;
});

function extractHttpStatus(err: unknown): number | null {
  const anyErr = err as Record<string, any> | null | undefined;
  if (!anyErr) return null;
  if (typeof anyErr.status === 'number') return anyErr.status;
  if (typeof anyErr?.response?.status === 'number') return anyErr.response.status;
  return null;
}

function extractDetail(err: unknown): string {
  const anyErr = err as { response?: { data?: { detail?: unknown } } } | undefined;
  const detail = anyErr?.response?.data?.detail;
  return typeof detail === 'string' ? detail : '';
}

/**
 * Classifies a failed callback/confirm/unlink call per
 * ``auth_routes.py::_raise_for_sso_error``'s own recommended mapping
 * (mirrored here verbatim, not re-derived -- that function is the single
 * source of truth for this vocabulary). Status 400 is intentionally
 * ambiguous server-side (both an invalid/expired ``state`` and a generic
 * adapter verification failure map to it, with different hardcoded detail
 * text) -- the two possible Chinese detail strings are matched here to
 * split them into two distinct, more actionable messages; an unrecognized
 * 400 body still falls back to the generic "invalid state" message rather
 * than inventing a new claim the response does not support.
 */
function classifyError(err: unknown): ErrorKind {
  const status = extractHttpStatus(err);
  const detail = extractDetail(err);

  if (status === 400) {
    // Three distinct SsoServiceError subtypes share HTTP 400
    // (InvalidSsoStateError / SsoLinkConfirmationError / a generic
    // SsoProviderError-derived verification failure) -- split on the exact
    // hardcoded detail text ``_raise_for_sso_error`` emits for each.
    if (detail.includes('绑定确认无效或已过期')) return 'linkConfirmationInvalid';
    if (detail.includes('验证失败')) return 'verificationFailed';
    return 'invalidState';
  }
  // 404 covers both "该登录方式当前不可用" (provider disabled/unknown, from
  // the callback route) and, defensively, "尚未绑定该登录方式"
  // (SsoIdentityNotLinkedError -- not reachable from this page today since
  // it never calls unlink, but sharing the same status if that ever
  // changes) -- split on the exact hardcoded detail text.
  if (status === 404) {
    return detail.includes('尚未绑定') ? 'notLinked' : 'unavailable';
  }
  if (status === 403) return 'notProvisioned';
  if (status === 409) {
    // Both SsoIdentityAlreadyLinkedError (login/link callback) and
    // SsoCannotRemoveLastCredentialError (unlink, not reachable from this
    // page today but sharing the same status) map to 409 -- split on text.
    if (detail.includes('绑定到另一个账号')) return 'alreadyLinked';
    return 'cannotRemoveLast';
  }
  if (status === 503) return 'temporary';
  if (status === 429) return 'rateLimited';
  if (status === null) return 'network';
  return 'unknown';
}

/**
 * Re-validates ``redirect_to`` client-side even though
 * ``auth_routes.py::_validate_redirect_target`` already guarantees it is
 * either the SPA's own root or a path under it -- defense in depth, and
 * translates the server's ``/portal``-prefixed absolute path back into
 * this hash router's own path space (which never includes that prefix --
 * see ``EndUserRoutes.ts``'s ``routes`` array). Anything that does not
 * cleanly match that prefix is never trusted, just like the server-side
 * function it mirrors -- falls back to the chat shell's default route.
 */
function normalizeRedirectTarget(candidate: unknown): string {
  if (typeof candidate !== 'string' || !candidate) return '/chat';
  if (candidate === PORTAL_BASE_PATH) return '/chat';
  if (candidate.startsWith(`${PORTAL_BASE_PATH}/`)) {
    const rest = candidate.slice(PORTAL_BASE_PATH.length);
    return rest || '/chat';
  }
  return '/chat';
}

async function runCallback() {
  const idpError = typeof route.query.error === 'string' ? route.query.error : null;
  if (idpError) {
    state.value = 'cancelled';
    return;
  }

  const code = typeof route.query.code === 'string' ? route.query.code : undefined;
  const oauthState = typeof route.query.state === 'string' ? route.query.state : undefined;

  try {
    const res = await portalApi.get(`/auth/sso/${encodeURIComponent(provider.value)}/callback`, {
      params: { code, state: oauthState },
    });
    const data = res.data as Record<string, unknown>;

    if (data?.result === 'login') {
      await authStore.fetchCurrentUser();
      state.value = 'redirecting';
      const target = normalizeRedirectTarget(data.redirect_to);
      router.replace(target);
      return;
    }

    if (data?.result === 'link_pending') {
      pendingLink.value = {
        provider: typeof data.provider === 'string' ? data.provider : provider.value,
        email: typeof data.email === 'string' ? data.email : '',
        displayName: typeof data.display_name === 'string' ? data.display_name : null,
        confirmationToken: typeof data.confirmation_token === 'string' ? data.confirmation_token : '',
      };
      state.value = 'linkConfirm';
      return;
    }

    // Unrecognized response shape -- fail closed rather than silently
    // treating it as success.
    errorKind.value = 'unknown';
    state.value = 'error';
  } catch (err) {
    errorKind.value = classifyError(err);
    state.value = 'error';
  }
}

async function confirmLink() {
  if (!pendingLink.value) return;
  state.value = 'linkConfirming';
  try {
    await portalApi.post(`/auth/sso/${encodeURIComponent(pendingLink.value.provider)}/link/confirm`, {
      confirmation_token: pendingLink.value.confirmationToken,
    });
    state.value = 'linkSuccess';
  } catch (err) {
    errorKind.value = classifyError(err);
    state.value = 'error';
  }
}

function cancelLink() {
  // No PortalOAuthIdentity row was ever written for a link_pending result
  // (confirm_link is the ONLY call that writes one) -- declining here
  // needs no server-side undo, just navigate away.
  router.replace({ name: 'portal-chat' });
}

function goToLogin() {
  router.replace({ name: 'portal-login' });
}

function continueToChat() {
  router.replace({ name: 'portal-chat' });
}

onMounted(() => {
  void runCallback();
});
</script>

<template>
  <div class="portal-auth-page">
    <div class="portal-auth-backdrop" aria-hidden="true"></div>

    <main class="portal-auth-main">
      <v-card class="portal-auth-card" elevation="4" rounded="lg">
        <div class="portal-auth-brand">
          <img width="44" src="@/assets/images/icon-no-shadow.svg" alt="AstrBot" />
        </div>

        <!-- Transitional: the whole point of this page while a request is
             in flight is to visibly signal "something is happening", not a
             blank/frozen screen. -->
        <template v-if="state === 'processing' || state === 'redirecting'">
          <div class="portal-oauth-spinner">
            <v-progress-circular indeterminate size="40" width="3" color="primary" />
          </div>
          <h1 class="portal-auth-title">{{ t('portal.oauth.connecting.title') }}</h1>
          <p class="portal-auth-subtitle">{{ t('portal.oauth.connecting.subtitle', { provider: providerLabel }) }}</p>
        </template>

        <template v-else-if="state === 'cancelled'">
          <h1 class="portal-auth-title">{{ t('portal.oauth.cancelled.title') }}</h1>
          <p class="portal-invite-terminal-message">{{ t('portal.oauth.cancelled.message') }}</p>
          <v-btn color="primary" block size="large" variant="flat" class="portal-auth-submit mt-4" @click="goToLogin">
            {{ t('portal.oauth.backToLogin') }}
          </v-btn>
        </template>

        <template v-else-if="state === 'error'">
          <v-icon size="40" color="error" class="portal-oauth-error-icon">mdi-alert-circle-outline</v-icon>
          <h1 class="portal-auth-title">{{ t(`portal.oauth.errors.${errorKind}.title`) }}</h1>
          <p class="portal-invite-terminal-message">{{ t(`portal.oauth.errors.${errorKind}.message`) }}</p>
          <v-btn color="primary" block size="large" variant="flat" class="portal-auth-submit mt-4" @click="goToLogin">
            {{ t('portal.oauth.backToLogin') }}
          </v-btn>
        </template>

        <template v-else-if="state === 'linkConfirm' && pendingLink">
          <v-icon size="40" color="warning" class="portal-oauth-error-icon">mdi-shield-alert-outline</v-icon>
          <h1 class="portal-auth-title">{{ t('portal.oauth.linkConfirm.title') }}</h1>
          <p class="portal-invite-terminal-message">
            {{
              t('portal.oauth.linkConfirm.message', {
                provider: t(`portal.oauth.providerNames.${pendingLink.provider}`) || pendingLink.provider,
                identity: pendingLink.displayName || pendingLink.email || pendingLink.provider,
              })
            }}
          </p>
          <v-alert color="warning" variant="tonal" density="comfortable" icon="mdi-information-outline" class="mt-2">
            {{ t('portal.oauth.linkConfirm.consequence') }}
          </v-alert>
          <v-btn
            color="primary"
            block
            size="large"
            variant="flat"
            class="portal-auth-submit mt-6"
            @click="confirmLink"
          >
            {{ t('portal.oauth.linkConfirm.confirm') }}
          </v-btn>
          <v-btn color="default" block size="large" variant="text" class="mt-2" @click="cancelLink">
            {{ t('portal.oauth.linkConfirm.cancel') }}
          </v-btn>
        </template>

        <template v-else-if="state === 'linkConfirming'">
          <div class="portal-oauth-spinner">
            <v-progress-circular indeterminate size="40" width="3" color="primary" />
          </div>
          <h1 class="portal-auth-title">{{ t('portal.oauth.linkConfirm.confirming') }}</h1>
        </template>

        <template v-else-if="state === 'linkSuccess'">
          <v-icon size="40" color="success" class="portal-oauth-error-icon">mdi-check-circle-outline</v-icon>
          <h1 class="portal-auth-title">{{ t('portal.oauth.linkConfirm.success.title') }}</h1>
          <p class="portal-invite-terminal-message">{{ t('portal.oauth.linkConfirm.success.message') }}</p>
          <v-btn
            color="primary"
            block
            size="large"
            variant="flat"
            class="portal-auth-submit mt-4"
            @click="continueToChat"
          >
            {{ t('portal.oauth.continueToChat') }}
          </v-btn>
        </template>
      </v-card>
    </main>
  </div>
</template>

<style scoped lang="scss">
.portal-auth-page {
  position: relative;
  width: 100vw;
  min-height: 100vh;
  overflow: hidden auto;
  background: rgb(var(--v-theme-containerBg));
}

.portal-auth-backdrop {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background:
    radial-gradient(560px circle at 12% 8%, rgba(var(--v-theme-primary), 0.16), transparent 60%),
    radial-gradient(620px circle at 88% 92%, rgba(var(--v-theme-secondary), 0.14), transparent 60%);
}

.portal-auth-main {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 24px 16px 40px;
}

.portal-auth-card {
  width: 100%;
  max-width: 440px;
  padding: 36px 32px 28px;
  text-align: center;
}

.portal-auth-brand {
  display: flex;
  justify-content: center;
  margin-bottom: 12px;
}

.portal-oauth-spinner {
  display: flex;
  justify-content: center;
  margin-bottom: 8px;
}

.portal-oauth-error-icon {
  display: block;
  margin: 0 auto 8px;
}

.portal-auth-title {
  font-size: 1.35rem;
  font-weight: 700;
  text-align: center;
  margin: 0;
  color: rgba(var(--v-theme-on-surface), 0.95);
}

.portal-auth-subtitle {
  text-align: center;
  font-size: 0.9rem;
  color: rgba(var(--v-theme-on-surface), 0.6);
  margin: 6px 0 0;
}

.portal-invite-terminal-message {
  text-align: center;
  font-size: 0.9rem;
  line-height: 1.6;
  color: rgba(var(--v-theme-on-surface), 0.7);
  margin: 14px 0 6px;
}

.portal-auth-submit {
  height: 48px;
  border-radius: 8px !important;
  letter-spacing: 0.3px;
  font-weight: 600;
}
</style>
