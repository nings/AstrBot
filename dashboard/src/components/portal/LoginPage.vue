<script setup lang="ts">
/**
 * End-user login page for the AstrBot Portal (consumer product surface).
 *
 * This is deliberately NOT styled like the operator dashboard's login form
 * (dashboard/src/views/authentication/auth/LoginPage.vue) -- it is a
 * product-facing sign-in screen for real end users, not an ops console.
 *
 * Contract this component depends on (verified against the sibling files
 * that were already on disk when this was written -- dashboard/src/stores/
 * portalAuth.ts, dashboard/src/api/portal.ts, dashboard/src/router/EndUserRoutes.ts):
 *   - Pinia store `usePortalAuthStore` from '@/stores/portalAuth', exposing
 *     `login(email, password): Promise<PortalUser | null>` that rejects with
 *     the raw AxiosError on failure (status read via `err.response.status`,
 *     see `extractHttpStatus` below).
 *   - The portal SPA's own hash-history router (`useRouter()`/`useRoute()`),
 *     with named routes `portal-login` (`/login`), `portal-register`
 *     (`/register`) and `portal-chat` (`/chat`); the navigation guard in
 *     EndUserRoutes.ts redirects unauthenticated visitors to `portal-login`
 *     with the originally-requested path in `?redirect=`.
 *
 * Identity is never read from anything the client sends except the
 * credentials the user typed -- the server derives `user_id` solely from
 * its own verified session/JWT.
 *
 * Phase 5b adds SSO login buttons, driven entirely by
 * `GET /api/v1/portal/auth/sso/providers` (see `astrbot/portal/api/
 * auth_routes.py`'s `sso_providers`, backed by `astrbot/portal/config.py`'s
 * `get_sso_provider_availability`): that endpoint returns `{provider: bool}`
 * for every provider this build knows about, already accounting for both
 * `PortalSettings.sso_enabled`/`sso_enabled_providers` AND per-provider
 * credential presence, so a provider that is merely enabled-but-half-
 * configured still renders `false` here -- this page never renders a
 * button for anything but a `true` entry, precisely so a disabled/
 * unconfigured provider never renders a button that would fail the moment
 * it is clicked. Clicking a rendered button is a real top-level navigation
 * (`window.location.assign`) to that provider's public, unauthenticated
 * `GET /sso/{provider}/start` route -- never an XHR -- since that route's
 * whole job is to 302 the browser to the identity provider itself.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useModuleI18n } from '@/i18n/composables';
import { usePortalAuthStore } from '@/stores/portalAuth';
import portalApi from '@/api/portal';
import LanguageSwitcher from '@/components/shared/LanguageSwitcher.vue';

const { tm: t } = useModuleI18n('features/auth');
const router = useRouter();
const route = useRoute();
const authStore = usePortalAuthStore();

// AGPL §13 source-offer link. `/api/v1/portal/source` is not implemented as
// of this pass (verified: no such route exists anywhere under
// astrbot/portal/api/), so this points at the upstream repository instead.
// See this task's `gaps` for the exact follow-up needed once that endpoint
// ships (swap this constant for a fetch against it, e.g. to surface the
// exact running commit SHA).
const SOURCE_LINK_URL = 'https://github.com/AstrBotDevs/AstrBot';

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
// The server does not send a Retry-After header on 429 (verified against
// astrbot/portal/api/auth_routes.py and auth/rate_limit.py), so this is a
// client-side heuristic matched to the backend's own first-lockout window
// (rate_limit.py's FAILURE_LOCKOUT_BASE_SECONDS = 30s) -- it under-estimates
// the wait for repeat offenders (server backs off exponentially up to 15
// minutes) but gives a truthful floor for the common case.
const LOCKOUT_HEURISTIC_SECONDS = 30;

const email = ref('');
const password = ref('');
const showPassword = ref(false);
const loading = ref(false);
const attemptedSubmit = ref(false);

type FormErrorKind = 'credentials' | 'rate_limited' | 'network' | 'unknown';
const formError = ref<{ kind: FormErrorKind; message: string } | null>(null);
const lockoutSecondsRemaining = ref(0);
let lockoutTimer: ReturnType<typeof setInterval> | null = null;

const emailError = computed(() => {
  if (!attemptedSubmit.value) return '';
  const value = email.value.trim();
  if (!value) return t('portal.login.validation.emailRequired');
  if (!EMAIL_RE.test(value)) return t('portal.login.validation.emailInvalid');
  return '';
});

const passwordError = computed(() => {
  if (!attemptedSubmit.value) return '';
  if (!password.value) return t('portal.login.validation.passwordRequired');
  return '';
});

const isFormValid = computed(() => EMAIL_RE.test(email.value.trim()) && password.value.length > 0);
const isLocked = computed(() => lockoutSecondsRemaining.value > 0);

function clearLockoutTimer() {
  if (lockoutTimer !== null) {
    clearInterval(lockoutTimer);
    lockoutTimer = null;
  }
}

function startLockoutCountdown(seconds: number) {
  clearLockoutTimer();
  lockoutSecondsRemaining.value = seconds;
  lockoutTimer = setInterval(() => {
    lockoutSecondsRemaining.value -= 1;
    if (lockoutSecondsRemaining.value <= 0) {
      lockoutSecondsRemaining.value = 0;
      clearLockoutTimer();
    }
  }, 1000);
}

onBeforeUnmount(() => {
  clearLockoutTimer();
});

// Defensive extraction: tolerates either the documented store contract
// (a plain object `{ status, detail }`) or a raw rejected AxiosError,
// since the store implementation is owned by a different, concurrently
// developed assignment and had not landed at the time this file was written.
function extractHttpStatus(err: unknown): number | null {
  const anyErr = err as Record<string, any> | null | undefined;
  if (!anyErr) return null;
  if (typeof anyErr.status === 'number') return anyErr.status;
  if (typeof anyErr?.response?.status === 'number') return anyErr.response.status;
  return null;
}

async function onSubmit() {
  attemptedSubmit.value = true;
  formError.value = null;
  if (isLocked.value || !isFormValid.value || loading.value) {
    return;
  }

  loading.value = true;
  try {
    await authStore.login(email.value.trim(), password.value);
    // Honor a deep-link redirect the router guard attached when it bounced
    // an unauthenticated visitor here (see dashboard/src/router/EndUserRoutes.ts
    // -- `{ name: 'portal-login', query: { redirect: to.fullPath } }`), falling
    // back to the chat shell's default route otherwise.
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : null;
    router.push(redirect || { name: 'portal-chat' });
  } catch (err) {
    const status = extractHttpStatus(err);
    if (status === 429) {
      formError.value = { kind: 'rate_limited', message: t('portal.login.rateLimitedGeneric') };
      startLockoutCountdown(LOCKOUT_HEURISTIC_SECONDS);
    } else if (status === 401) {
      // Deliberately generic: the backend already returns the same message
      // regardless of whether the email exists or the password is wrong
      // (see auth_routes.py's login()), and we never surface anything more
      // specific here either.
      formError.value = { kind: 'credentials', message: t('portal.login.genericError') };
    } else if (status === null) {
      formError.value = { kind: 'network', message: t('portal.login.networkError') };
    } else {
      formError.value = { kind: 'unknown', message: t('portal.login.unknownError') };
    }
  } finally {
    loading.value = false;
  }
}

// --- SSO (Phase 5b) -------------------------------------------------------

const SSO_PROVIDER_ORDER = ['oidc', 'wecom', 'feishu'] as const;
const SSO_PROVIDER_ICONS: Record<(typeof SSO_PROVIDER_ORDER)[number], string> = {
  oidc: 'mdi-shield-key-outline',
  wecom: 'mdi-wechat',
  feishu: 'mdi-message-processing-outline',
};

const ssoProviders = ref<Partial<Record<string, boolean>>>({});
// `registrationOpen` mirrors `PortalSettings.self_registration_open`, but no
// endpoint currently exposes that flag publicly (verified: neither
// `GET /auth/sso/providers` nor any other unauthenticated route in
// `auth_routes.py` returns it -- see this task's `gaps`). Defaulting to
// `true` (the register link renders exactly as it always has) is the only
// choice that cannot turn a working link into a dead one on a deployment
// this page has no way to ask; the read below is forward-compatible should
// that endpoint gain the field later without requiring another change here.
const registrationOpen = ref(true);

const enabledSsoProviders = computed(() =>
  SSO_PROVIDER_ORDER.filter((provider) => ssoProviders.value[provider] === true),
);
const hasSsoProviders = computed(() => enabledSsoProviders.value.length > 0);

onMounted(async () => {
  try {
    const res = await portalApi.get('/auth/sso/providers');
    ssoProviders.value = (res.data?.providers as Partial<Record<string, boolean>>) ?? {};
    if (typeof res.data?.self_registration_open === 'boolean') {
      registrationOpen.value = res.data.self_registration_open;
    }
  } catch {
    // SSO buttons are a progressive enhancement on top of an already fully
    // functional email/password form -- a failed fetch here must never
    // block or error out the rest of the login page, it just means no SSO
    // buttons render this load.
    ssoProviders.value = {};
  }
});

function startSsoLogin(provider: string) {
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : null;
  // `/auth/sso/{provider}/start`'s `next` param is validated server-side
  // against a `/portal`-prefixed allowlist (see `auth_routes.py`'s
  // `_validate_redirect_target`) -- this router's own route paths never
  // include that prefix (EndUserRoutes.ts's hash-history base already is
  // `/portal/`), so it must be added back on here.
  const target = redirect && redirect.startsWith('/') ? redirect : '/chat';
  const next = `/portal${target}`;
  const url = `/api/v1/portal/auth/sso/${encodeURIComponent(provider)}/start?next=${encodeURIComponent(next)}`;
  // A real top-level navigation, not portalApi/XHR -- this route's whole
  // purpose is to 302 the browser to the identity provider.
  window.location.assign(url);
}
</script>

<template>
  <div class="portal-auth-page">
    <div class="portal-auth-backdrop" aria-hidden="true"></div>

    <div class="portal-auth-topbar">
      <LanguageSwitcher />
    </div>

    <main class="portal-auth-main">
      <v-card class="portal-auth-card" elevation="4" rounded="lg">
        <div class="portal-auth-brand">
          <img width="44" src="@/assets/images/icon-no-shadow.svg" alt="AstrBot" />
        </div>

        <h1 class="portal-auth-title">{{ t('portal.login.title') }}</h1>
        <p class="portal-auth-subtitle">{{ t('portal.login.subtitle') }}</p>

        <form class="portal-auth-form" novalidate @submit.prevent="onSubmit">
          <v-text-field
            v-model="email"
            :label="t('portal.login.email')"
            :placeholder="t('portal.login.emailPlaceholder')"
            type="email"
            autocomplete="email"
            inputmode="email"
            variant="outlined"
            class="mb-1"
            hide-details="auto"
            prepend-inner-icon="mdi-email-outline"
            :error-messages="emailError ? [emailError] : []"
            :disabled="loading || isLocked"
            autofocus
          ></v-text-field>

          <v-text-field
            v-model="password"
            :label="t('portal.login.password')"
            :type="showPassword ? 'text' : 'password'"
            autocomplete="current-password"
            variant="outlined"
            class="mt-4"
            hide-details="auto"
            prepend-inner-icon="mdi-lock-outline"
            :append-inner-icon="showPassword ? 'mdi-eye-off' : 'mdi-eye'"
            :error-messages="passwordError ? [passwordError] : []"
            :disabled="loading || isLocked"
            @click:append-inner="showPassword = !showPassword"
            @keyup.enter="onSubmit"
          ></v-text-field>

          <v-alert
            v-if="formError && formError.kind !== 'rate_limited'"
            class="mt-4"
            color="error"
            variant="tonal"
            icon="mdi-alert-circle"
            border="start"
            density="comfortable"
          >
            {{ formError.message }}
          </v-alert>

          <v-alert
            v-if="formError && formError.kind === 'rate_limited'"
            class="mt-4"
            color="warning"
            variant="tonal"
            icon="mdi-timer-lock-outline"
            border="start"
            density="comfortable"
          >
            <div>{{ formError.message }}</div>
            <div v-if="isLocked" class="portal-auth-lockout-countdown">
              {{ t('portal.login.rateLimitedCountdown', { seconds: lockoutSecondsRemaining }) }}
            </div>
          </v-alert>

          <v-btn
            type="submit"
            color="primary"
            block
            size="large"
            class="portal-auth-submit mt-6"
            variant="flat"
            :loading="loading"
            :disabled="loading || isLocked || (attemptedSubmit && !isFormValid)"
          >
            {{ t('portal.login.submit') }}
          </v-btn>
        </form>

        <template v-if="hasSsoProviders">
          <div class="portal-auth-divider">
            <span>{{ t('portal.login.sso.divider') }}</span>
          </div>

          <div class="portal-sso-buttons">
            <v-btn
              v-for="provider in enabledSsoProviders"
              :key="provider"
              block
              size="large"
              variant="outlined"
              class="portal-sso-btn"
              :prepend-icon="SSO_PROVIDER_ICONS[provider]"
              :disabled="loading || isLocked"
              @click="startSsoLogin(provider)"
            >
              {{ t(`portal.login.sso.continueWith.${provider}`) }}
            </v-btn>
          </div>
        </template>

        <div v-if="registrationOpen" class="portal-auth-switch">
          <span>{{ t('portal.login.noAccount') }}</span>
          <router-link :to="{ name: 'portal-register' }" class="portal-auth-switch-link">
            {{ t('portal.login.registerLink') }}
          </router-link>
        </div>
        <div v-else class="portal-auth-switch portal-auth-switch-closed">
          <v-icon size="14" class="mr-1">mdi-information-outline</v-icon>
          <span>{{ t('portal.login.registrationClosedNotice') }}</span>
        </div>
      </v-card>

      <footer class="portal-auth-footer">
        <span>{{ t('portal.sourceLink') }}: </span>
        <a
          :href="SOURCE_LINK_URL"
          target="_blank"
          rel="noopener noreferrer"
          class="portal-auth-footer-link"
        >
          <v-icon size="14" class="mr-1">mdi-github</v-icon>{{ SOURCE_LINK_URL }}
          <v-tooltip activator="parent" location="top">{{ t('portal.sourceLinkTooltip') }}</v-tooltip>
        </a>
      </footer>
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

.portal-auth-topbar {
  position: relative;
  z-index: 1;
  display: flex;
  justify-content: flex-end;
  padding: 16px 20px 0;
}

.portal-auth-main {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: calc(100vh - 56px);
  padding: 24px 16px 40px;
}

.portal-auth-card {
  width: 100%;
  max-width: 400px;
  padding: 36px 32px 28px;
}

.portal-auth-brand {
  display: flex;
  justify-content: center;
  margin-bottom: 12px;
}

.portal-auth-title {
  font-size: 1.5rem;
  font-weight: 700;
  text-align: center;
  margin: 0;
  color: rgba(var(--v-theme-on-surface), 0.95);
}

.portal-auth-subtitle {
  text-align: center;
  font-size: 0.9rem;
  color: rgba(var(--v-theme-on-surface), 0.6);
  margin: 6px 0 28px;
}

.portal-auth-form {
  display: flex;
  flex-direction: column;
}

.portal-auth-submit {
  height: 48px;
  border-radius: 8px !important;
  letter-spacing: 0.3px;
  font-weight: 600;
}

.portal-auth-lockout-countdown {
  margin-top: 4px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.portal-auth-divider {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 22px 0 16px;
  font-size: 0.75rem;
  color: rgba(var(--v-theme-on-surface), 0.45);

  &::before,
  &::after {
    content: '';
    flex: 1;
    height: 1px;
    background: rgba(var(--v-theme-on-surface), 0.12);
  }
}

.portal-sso-buttons {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.portal-sso-btn {
  height: 46px;
  border-radius: 8px !important;
  text-transform: none;
  letter-spacing: normal;
  font-weight: 500;
}

.portal-auth-switch {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 6px;
  margin-top: 24px;
  font-size: 0.875rem;
  color: rgba(var(--v-theme-on-surface), 0.65);
}

.portal-auth-switch-closed {
  text-align: center;
  color: rgba(var(--v-theme-on-surface), 0.5);
}

.portal-auth-switch-link {
  color: rgb(var(--v-theme-primary));
  font-weight: 600;
  text-decoration: none;

  &:hover {
    text-decoration: underline;
  }
}

.portal-auth-footer {
  margin-top: 24px;
  text-align: center;
  font-size: 0.75rem;
  color: rgba(var(--v-theme-on-surface), 0.5);
}

.portal-auth-footer-link {
  color: inherit;
  display: inline-flex;
  align-items: center;
  text-decoration: none;

  &:hover {
    color: rgb(var(--v-theme-primary));
    text-decoration: underline;
  }
}
</style>
