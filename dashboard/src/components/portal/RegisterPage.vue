<script setup lang="ts">
/**
 * End-user registration page for the AstrBot Portal (consumer product surface).
 *
 * Mirrors the server's password policy client-side
 * (astrbot/core/utils/auth_password.py::validate_dashboard_password): at
 * least 8 characters, one uppercase letter, one lowercase letter, one digit.
 * This is presentation-layer convenience only -- the server re-validates
 * independently and is the sole source of truth; see the `invalidInput`
 * fallback branch in `onSubmit` for what happens if the two ever disagree.
 *
 * See LoginPage.vue's module docstring for the exact store/router contract
 * this component depends on (`usePortalAuthStore` from '@/stores/portalAuth',
 * the portal SPA's own hash-history router with named routes `portal-login`/
 * `portal-register`/`portal-chat`).
 */
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useModuleI18n } from '@/i18n/composables';
import { usePortalAuthStore } from '@/stores/portalAuth';
import LanguageSwitcher from '@/components/shared/LanguageSwitcher.vue';

const { tm: t } = useModuleI18n('features/auth');
const router = useRouter();
const route = useRoute();
const authStore = usePortalAuthStore();

// See LoginPage.vue for why this points at the upstream repo rather than
// `/api/v1/portal/source` (that endpoint does not exist in this Phase 1
// build -- verified, see this task's `gaps`).
const SOURCE_LINK_URL = 'https://github.com/AstrBotDevs/AstrBot';

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

const email = ref('');
const password = ref('');
const confirmPassword = ref('');
const displayName = ref('');
const showPassword = ref(false);
const showConfirmPassword = ref(false);
const loading = ref(false);
const attemptedSubmit = ref(false);

type FormErrorKind = 'email_taken' | 'registration_closed' | 'rate_limited' | 'invalid_input' | 'network' | 'unknown';
const formError = ref<{ kind: FormErrorKind; message: string } | null>(null);

// --- Password policy, mirrored from validate_dashboard_password() --------
const passwordRules = computed(() => ({
  length: password.value.length >= 8,
  uppercase: /[A-Z]/.test(password.value),
  lowercase: /[a-z]/.test(password.value),
  digit: /\d/.test(password.value),
}));
const passwordMeetsPolicy = computed(() => Object.values(passwordRules.value).every(Boolean));
const showPasswordChecklist = computed(() => password.value.length > 0 || attemptedSubmit.value);

const emailError = computed(() => {
  if (!attemptedSubmit.value) return '';
  const value = email.value.trim();
  if (!value) return t('portal.register.validation.emailRequired');
  if (!EMAIL_RE.test(value)) return t('portal.register.validation.emailInvalid');
  return '';
});

const passwordError = computed(() => {
  if (!attemptedSubmit.value) return '';
  if (!password.value) return t('portal.register.validation.passwordRequired');
  if (!passwordMeetsPolicy.value) return t('portal.register.validation.passwordPolicy');
  return '';
});

const confirmPasswordError = computed(() => {
  if (!attemptedSubmit.value) return '';
  if (!confirmPassword.value) return t('portal.register.validation.confirmPasswordRequired');
  if (confirmPassword.value !== password.value) return t('portal.register.validation.passwordMatch');
  return '';
});

const isFormValid = computed(
  () =>
    EMAIL_RE.test(email.value.trim()) &&
    passwordMeetsPolicy.value &&
    confirmPassword.value === password.value &&
    confirmPassword.value.length > 0,
);

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
  if (!isFormValid.value || loading.value) {
    return;
  }

  loading.value = true;
  try {
    await authStore.register(email.value.trim(), password.value, displayName.value.trim() || undefined);
    // Same redirect-honoring behavior as LoginPage.vue -- a user who landed
    // on /register via a deep-link bounce should end up back where they were
    // headed, not always at the chat shell's default route.
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : null;
    router.push(redirect || { name: 'portal-chat' });
  } catch (err) {
    const status = extractHttpStatus(err);
    if (status === 409) {
      formError.value = { kind: 'email_taken', message: t('portal.register.emailTaken') };
    } else if (status === 403) {
      formError.value = { kind: 'registration_closed', message: t('portal.register.registrationClosed') };
    } else if (status === 429) {
      formError.value = { kind: 'rate_limited', message: t('portal.register.rateLimitedGeneric') };
    } else if (status === 400) {
      // Client-side validation above already mirrors the server's email
      // format + password policy checks, so reaching this branch means the
      // two disagreed (or were bypassed) -- a generic message is shown
      // rather than the server's raw (English-only, unlocalized) detail
      // string, to keep the UI consistent in the user's chosen language.
      formError.value = { kind: 'invalid_input', message: t('portal.register.invalidInput') };
    } else if (status === null) {
      formError.value = { kind: 'network', message: t('portal.register.networkError') };
    } else {
      formError.value = { kind: 'unknown', message: t('portal.register.unknownError') };
    }
  } finally {
    loading.value = false;
  }
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

        <h1 class="portal-auth-title">{{ t('portal.register.title') }}</h1>
        <p class="portal-auth-subtitle">{{ t('portal.register.subtitle') }}</p>

        <form class="portal-auth-form" novalidate @submit.prevent="onSubmit">
          <v-text-field
            v-model="email"
            :label="t('portal.register.email')"
            :placeholder="t('portal.register.emailPlaceholder')"
            type="email"
            autocomplete="email"
            inputmode="email"
            variant="outlined"
            class="mb-1"
            hide-details="auto"
            prepend-inner-icon="mdi-email-outline"
            :error-messages="emailError ? [emailError] : []"
            :disabled="loading"
            autofocus
          ></v-text-field>

          <v-text-field
            v-model="displayName"
            :label="t('portal.register.displayNameOptional')"
            autocomplete="nickname"
            variant="outlined"
            class="mt-4"
            hide-details="auto"
            prepend-inner-icon="mdi-account-outline"
            :disabled="loading"
          ></v-text-field>

          <v-text-field
            v-model="password"
            :label="t('portal.register.password')"
            :type="showPassword ? 'text' : 'password'"
            autocomplete="new-password"
            variant="outlined"
            class="mt-4"
            hide-details="auto"
            prepend-inner-icon="mdi-lock-outline"
            :append-inner-icon="showPassword ? 'mdi-eye-off' : 'mdi-eye'"
            :error-messages="passwordError ? [passwordError] : []"
            :disabled="loading"
            @click:append-inner="showPassword = !showPassword"
          ></v-text-field>

          <ul v-if="showPasswordChecklist" class="portal-password-checklist">
            <li :class="{ met: passwordRules.length }">
              <v-icon size="14">{{ passwordRules.length ? 'mdi-check-circle' : 'mdi-circle-outline' }}</v-icon>
              {{ t('portal.register.passwordRuleLength') }}
            </li>
            <li :class="{ met: passwordRules.uppercase }">
              <v-icon size="14">{{ passwordRules.uppercase ? 'mdi-check-circle' : 'mdi-circle-outline' }}</v-icon>
              {{ t('portal.register.passwordRuleUppercase') }}
            </li>
            <li :class="{ met: passwordRules.lowercase }">
              <v-icon size="14">{{ passwordRules.lowercase ? 'mdi-check-circle' : 'mdi-circle-outline' }}</v-icon>
              {{ t('portal.register.passwordRuleLowercase') }}
            </li>
            <li :class="{ met: passwordRules.digit }">
              <v-icon size="14">{{ passwordRules.digit ? 'mdi-check-circle' : 'mdi-circle-outline' }}</v-icon>
              {{ t('portal.register.passwordRuleDigit') }}
            </li>
          </ul>

          <v-text-field
            v-model="confirmPassword"
            :label="t('portal.register.confirmPassword')"
            :type="showConfirmPassword ? 'text' : 'password'"
            autocomplete="new-password"
            variant="outlined"
            class="mt-4"
            hide-details="auto"
            prepend-inner-icon="mdi-lock-check-outline"
            :append-inner-icon="showConfirmPassword ? 'mdi-eye-off' : 'mdi-eye'"
            :error-messages="confirmPasswordError ? [confirmPasswordError] : []"
            :disabled="loading"
            @click:append-inner="showConfirmPassword = !showConfirmPassword"
            @keyup.enter="onSubmit"
          ></v-text-field>

          <v-alert
            v-if="formError"
            class="mt-4"
            :color="formError.kind === 'rate_limited' ? 'warning' : 'error'"
            variant="tonal"
            :icon="formError.kind === 'rate_limited' ? 'mdi-timer-lock-outline' : 'mdi-alert-circle'"
            border="start"
            density="comfortable"
          >
            {{ formError.message }}
          </v-alert>

          <v-btn
            type="submit"
            color="primary"
            block
            size="large"
            class="portal-auth-submit mt-6"
            variant="flat"
            :loading="loading"
            :disabled="loading || (attemptedSubmit && !isFormValid)"
          >
            {{ t('portal.register.submit') }}
          </v-btn>
        </form>

        <div class="portal-auth-switch">
          <span>{{ t('portal.register.haveAccount') }}</span>
          <router-link :to="{ name: 'portal-login' }" class="portal-auth-switch-link">
            {{ t('portal.register.loginLink') }}
          </router-link>
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
  max-width: 420px;
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

.portal-password-checklist {
  list-style: none;
  margin: 10px 2px 0;
  padding: 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 12px;

  li {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.75rem;
    color: rgba(var(--v-theme-on-surface), 0.55);

    .v-icon {
      color: rgba(var(--v-theme-on-surface), 0.35);
    }

    &.met {
      color: rgb(var(--v-theme-success));

      .v-icon {
        color: rgb(var(--v-theme-success));
      }
    }
  }
}

.portal-auth-submit {
  height: 48px;
  border-radius: 8px !important;
  letter-spacing: 0.3px;
  font-weight: 600;
}

.portal-auth-switch {
  display: flex;
  justify-content: center;
  gap: 6px;
  margin-top: 24px;
  font-size: 0.875rem;
  color: rgba(var(--v-theme-on-surface), 0.65);
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
