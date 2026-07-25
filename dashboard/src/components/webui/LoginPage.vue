<script setup lang="ts">
/**
 * End-user login page for the AstrBot WebUI (consumer product surface).
 *
 * This is deliberately NOT styled like the operator dashboard's login form
 * (dashboard/src/views/authentication/auth/LoginPage.vue) -- it is a
 * product-facing sign-in screen for real end users, not an ops console.
 *
 * Contract this component depends on (verified against the sibling files
 * that were already on disk when this was written -- dashboard/src/stores/
 * webuiAuth.ts, dashboard/src/api/webui.ts, dashboard/src/router/EndUserRoutes.ts):
 *   - Pinia store `useWebuiAuthStore` from '@/stores/webuiAuth', exposing
 *     `login(email, password): Promise<WebuiUser | null>` that rejects with
 *     the raw AxiosError on failure (status read via `err.response.status`,
 *     see `extractHttpStatus` below).
 *   - The webui SPA's own hash-history router (`useRouter()`/`useRoute()`),
 *     with named routes `webui-login` (`/login`), `webui-register`
 *     (`/register`) and `webui-chat` (`/chat`); the navigation guard in
 *     EndUserRoutes.ts redirects unauthenticated visitors to `webui-login`
 *     with the originally-requested path in `?redirect=`.
 *
 * Identity is never read from anything the client sends except the
 * credentials the user typed -- the server derives `user_id` solely from
 * its own verified session/JWT.
 */
import { computed, onBeforeUnmount, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useModuleI18n } from '@/i18n/composables';
import { useWebuiAuthStore } from '@/stores/webuiAuth';
import LanguageSwitcher from '@/components/shared/LanguageSwitcher.vue';

const { tm: t } = useModuleI18n('features/auth');
const router = useRouter();
const route = useRoute();
const authStore = useWebuiAuthStore();

// AGPL §13 source-offer link. `/api/v1/webui/source` is not implemented as
// of this pass (verified: no such route exists anywhere under
// astrbot/webui/api/), so this points at the upstream repository instead.
// See this task's `gaps` for the exact follow-up needed once that endpoint
// ships (swap this constant for a fetch against it, e.g. to surface the
// exact running commit SHA).
const SOURCE_LINK_URL = 'https://github.com/AstrBotDevs/AstrBot';

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
// The server does not send a Retry-After header on 429 (verified against
// astrbot/webui/api/auth_routes.py and auth/rate_limit.py), so this is a
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
  if (!value) return t('webui.login.validation.emailRequired');
  if (!EMAIL_RE.test(value)) return t('webui.login.validation.emailInvalid');
  return '';
});

const passwordError = computed(() => {
  if (!attemptedSubmit.value) return '';
  if (!password.value) return t('webui.login.validation.passwordRequired');
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
    // -- `{ name: 'webui-login', query: { redirect: to.fullPath } }`), falling
    // back to the chat shell's default route otherwise.
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : null;
    router.push(redirect || { name: 'webui-chat' });
  } catch (err) {
    const status = extractHttpStatus(err);
    if (status === 429) {
      formError.value = { kind: 'rate_limited', message: t('webui.login.rateLimitedGeneric') };
      startLockoutCountdown(LOCKOUT_HEURISTIC_SECONDS);
    } else if (status === 401) {
      // Deliberately generic: the backend already returns the same message
      // regardless of whether the email exists or the password is wrong
      // (see auth_routes.py's login()), and we never surface anything more
      // specific here either.
      formError.value = { kind: 'credentials', message: t('webui.login.genericError') };
    } else if (status === null) {
      formError.value = { kind: 'network', message: t('webui.login.networkError') };
    } else {
      formError.value = { kind: 'unknown', message: t('webui.login.unknownError') };
    }
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="webui-auth-page">
    <div class="webui-auth-backdrop" aria-hidden="true"></div>

    <div class="webui-auth-topbar">
      <LanguageSwitcher />
    </div>

    <main class="webui-auth-main">
      <v-card class="webui-auth-card" elevation="4" rounded="lg">
        <div class="webui-auth-brand">
          <img width="44" src="@/assets/images/icon-no-shadow.svg" alt="AstrBot" />
        </div>

        <h1 class="webui-auth-title">{{ t('webui.login.title') }}</h1>
        <p class="webui-auth-subtitle">{{ t('webui.login.subtitle') }}</p>

        <form class="webui-auth-form" novalidate @submit.prevent="onSubmit">
          <v-text-field
            v-model="email"
            :label="t('webui.login.email')"
            :placeholder="t('webui.login.emailPlaceholder')"
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
            :label="t('webui.login.password')"
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
            <div v-if="isLocked" class="webui-auth-lockout-countdown">
              {{ t('webui.login.rateLimitedCountdown', { seconds: lockoutSecondsRemaining }) }}
            </div>
          </v-alert>

          <v-btn
            type="submit"
            color="primary"
            block
            size="large"
            class="webui-auth-submit mt-6"
            variant="flat"
            :loading="loading"
            :disabled="loading || isLocked || (attemptedSubmit && !isFormValid)"
          >
            {{ t('webui.login.submit') }}
          </v-btn>
        </form>

        <div class="webui-auth-switch">
          <span>{{ t('webui.login.noAccount') }}</span>
          <router-link :to="{ name: 'webui-register' }" class="webui-auth-switch-link">
            {{ t('webui.login.registerLink') }}
          </router-link>
        </div>
      </v-card>

      <footer class="webui-auth-footer">
        <span>{{ t('webui.sourceLink') }}: </span>
        <a
          :href="SOURCE_LINK_URL"
          target="_blank"
          rel="noopener noreferrer"
          class="webui-auth-footer-link"
        >
          <v-icon size="14" class="mr-1">mdi-github</v-icon>{{ SOURCE_LINK_URL }}
          <v-tooltip activator="parent" location="top">{{ t('webui.sourceLinkTooltip') }}</v-tooltip>
        </a>
      </footer>
    </main>
  </div>
</template>

<style scoped lang="scss">
.webui-auth-page {
  position: relative;
  width: 100vw;
  min-height: 100vh;
  overflow: hidden auto;
  background: rgb(var(--v-theme-containerBg));
}

.webui-auth-backdrop {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background:
    radial-gradient(560px circle at 12% 8%, rgba(var(--v-theme-primary), 0.16), transparent 60%),
    radial-gradient(620px circle at 88% 92%, rgba(var(--v-theme-secondary), 0.14), transparent 60%);
}

.webui-auth-topbar {
  position: relative;
  z-index: 1;
  display: flex;
  justify-content: flex-end;
  padding: 16px 20px 0;
}

.webui-auth-main {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: calc(100vh - 56px);
  padding: 24px 16px 40px;
}

.webui-auth-card {
  width: 100%;
  max-width: 400px;
  padding: 36px 32px 28px;
}

.webui-auth-brand {
  display: flex;
  justify-content: center;
  margin-bottom: 12px;
}

.webui-auth-title {
  font-size: 1.5rem;
  font-weight: 700;
  text-align: center;
  margin: 0;
  color: rgba(var(--v-theme-on-surface), 0.95);
}

.webui-auth-subtitle {
  text-align: center;
  font-size: 0.9rem;
  color: rgba(var(--v-theme-on-surface), 0.6);
  margin: 6px 0 28px;
}

.webui-auth-form {
  display: flex;
  flex-direction: column;
}

.webui-auth-submit {
  height: 48px;
  border-radius: 8px !important;
  letter-spacing: 0.3px;
  font-weight: 600;
}

.webui-auth-lockout-countdown {
  margin-top: 4px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.webui-auth-switch {
  display: flex;
  justify-content: center;
  gap: 6px;
  margin-top: 24px;
  font-size: 0.875rem;
  color: rgba(var(--v-theme-on-surface), 0.65);
}

.webui-auth-switch-link {
  color: rgb(var(--v-theme-primary));
  font-weight: 600;
  text-decoration: none;

  &:hover {
    text-decoration: underline;
  }
}

.webui-auth-footer {
  margin-top: 24px;
  text-align: center;
  font-size: 0.75rem;
  color: rgba(var(--v-theme-on-surface), 0.5);
}

.webui-auth-footer-link {
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
