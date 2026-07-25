<script setup lang="ts">
/**
 * Invite-acceptance page for the AstrBot Portal (Phase 5a: admin-provisioned
 * accounts).
 *
 * The link an operator delivers out-of-band (chat, ticket -- there is no
 * mail infrastructure in this repo) is built server-side, literally, as
 * ``/portal/invite/accept?token=<...>`` (see
 * ``astrbot/portal/api/admin_routes.py``'s ``create_invite``). That is a
 * REAL (non-hash) browser navigation with the token sitting in
 * ``location.search`` -- it knows nothing about this SPA's hash-history
 * routing. ``EndUserRoutes.ts`` bridges that one-time real-path navigation
 * into this SPA's own ``portal-invite-accept`` hash route before this
 * component ever mounts (see that file's ``bridgeServerRedirectIntoHash``)
 * -- by the time this component runs, ``route.query.token`` is populated
 * exactly like any other hash-routed query param.
 *
 * Redemption goes straight through ``POST /api/v1/portal/auth/invite/redeem``
 * (``astrbot/portal/api/auth_routes.py``'s ``redeem_invite``, backed by
 * ``astrbot/portal/services/invite_service.py``'s ``redeem_invite``) via the
 * shared ``portalApi`` axios instance -- there is no dedicated Pinia store
 * action for this (invite redemption is a Phase 5 addition to a Phase 1
 * store this task does not own), so this component calls the endpoint
 * directly and, on success, defers to the store's own
 * ``fetchCurrentUser()`` to hydrate session state from the cookies the
 * response already set, rather than constructing a ``PortalUser`` object by
 * hand here (see ``notes_for_integrator`` in this task's handoff for why
 * that split is deliberate).
 *
 * Password policy mirrors ``RegisterPage.vue``'s own client-side checklist
 * (in turn mirroring ``astrbot/core/utils/auth_password.py::validate_dashboard_password``):
 * at least 8 characters, one uppercase letter, one lowercase letter, one
 * digit. Presentation-layer convenience only -- the server
 * (``invite_service.validate_password_policy``) is the sole source of
 * truth.
 *
 * Failure handling is the point of this page: ``invite_service.py``'s own
 * module docstring is explicit that an invalid/unrecognized token (401), an
 * expired one, and one that has already been used (both 410, but distinct
 * exception types with distinct message text -- see that module's "Once the
 * secret verifies, distinguishing consumed vs. revoked vs. expired *is*
 * safe" section) describe three different situations with three different
 * correct next actions for the invitee: get a new link from the
 * administrator, get a new link from the administrator, or -- notably
 * different -- just sign in, since "already used" usually means the
 * invitee (or someone at their organization) already finished setup. HTTP
 * status code alone cannot distinguish the 410 sub-cases (all three map to
 * the same status by design, so a legitimate holder of a dead link gets a
 * single simple client-side branch: "show the message, offer to contact
 * the operator" -- see that module's own reasoning) -- this component reads
 * the exact ``detail`` text the server returns to tell them apart. This is
 * matching-not-guessing: every string matched below was read verbatim out
 * of ``invite_service.py`` in this same task, not reverse-engineered from
 * behavior.
 */
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useModuleI18n } from '@/i18n/composables';
import { usePortalAuthStore } from '@/stores/portalAuth';
import portalApi from '@/api/portal';
import LanguageSwitcher from '@/components/shared/LanguageSwitcher.vue';

const { tm: t } = useModuleI18n('features/auth');
const router = useRouter();
const route = useRoute();
const authStore = usePortalAuthStore();

const token = computed(() => (typeof route.query.token === 'string' ? route.query.token : ''));

const displayName = ref('');
const password = ref('');
const confirmPassword = ref('');
const showPassword = ref(false);
const showConfirmPassword = ref(false);
const loading = ref(false);
const attemptedSubmit = ref(false);

// --- Password policy, mirrored from validate_dashboard_password() (same
// four rules as RegisterPage.vue's own checklist) -----------------------
const passwordRules = computed(() => ({
  length: password.value.length >= 8,
  uppercase: /[A-Z]/.test(password.value),
  lowercase: /[a-z]/.test(password.value),
  digit: /\d/.test(password.value),
}));
const passwordMeetsPolicy = computed(() => Object.values(passwordRules.value).every(Boolean));
const showPasswordChecklist = computed(() => password.value.length > 0 || attemptedSubmit.value);

const passwordError = computed(() => {
  if (!attemptedSubmit.value) return '';
  if (!password.value) return t('portal.invite.validation.passwordRequired');
  if (!passwordMeetsPolicy.value) return t('portal.invite.validation.passwordPolicy');
  return '';
});

const confirmPasswordError = computed(() => {
  if (!attemptedSubmit.value) return '';
  if (!confirmPassword.value) return t('portal.invite.validation.confirmPasswordRequired');
  if (confirmPassword.value !== password.value) return t('portal.invite.validation.passwordMatch');
  return '';
});

const isFormValid = computed(
  () => passwordMeetsPolicy.value && confirmPassword.value === password.value && confirmPassword.value.length > 0,
);

// Terminal failure kinds replace the whole form -- retrying the same
// request against a dead token can never succeed, so there is nothing
// productive left for a form to do (unlike the inline-alert kinds below,
// where the token itself is still potentially good and a retry makes
// sense).
type TerminalErrorKind = 'missingToken' | 'invalidToken' | 'expired' | 'used' | 'revoked';
// Non-terminal: shown as an inline alert above a form that remains usable.
type InlineErrorKind = 'passwordPolicy' | 'emailMismatch' | 'conflict' | 'rateLimited' | 'network' | 'unknown';

const terminalError = ref<TerminalErrorKind | null>(token.value ? null : 'missingToken');
const inlineError = ref<{ kind: InlineErrorKind; message: string } | null>(null);

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
 * Classifies a failed redemption per ``invite_service.py``'s own exception
 * vocabulary. The 410 branch below is the one place status code alone is
 * not enough: ``InviteAlreadyConsumedError`` / ``InviteRevokedError`` /
 * ``InviteExpiredError`` all map to HTTP 410, so the exact English
 * ``detail`` text (hardcoded server-side, not localized -- verified by
 * reading ``invite_service.py``'s ``redeem_invite``) is what tells them
 * apart here. If the server ever adds a fourth 410 case whose text matches
 * none of these, this deliberately falls back to the generic "invalid"
 * terminal state rather than guessing -- fail closed on an unrecognized
 * shape, never invent a more specific claim than the response supports.
 */
function classifyError(err: unknown): void {
  const status = extractHttpStatus(err);
  const detail = extractDetail(err).toLowerCase();

  if (status === 401) {
    terminalError.value = 'invalidToken';
    return;
  }
  if (status === 410) {
    if (detail.includes('expired')) {
      terminalError.value = 'expired';
    } else if (detail.includes('revoked')) {
      terminalError.value = 'revoked';
    } else {
      // Covers "already been used" and, defensively, any other 410 text
      // this module might return -- "used" is the most actionable of the
      // three (it points the invitee at signing in instead), so it is the
      // safer default for an unrecognized-but-still-410 message.
      terminalError.value = 'used';
    }
    return;
  }
  if (status === 409) {
    inlineError.value = {
      kind: detail.includes('retry') ? 'conflict' : 'emailMismatch',
      message: detail.includes('retry') ? t('portal.invite.errors.conflict') : t('portal.invite.errors.emailMismatch'),
    };
    return;
  }
  if (status === 400) {
    inlineError.value = { kind: 'passwordPolicy', message: t('portal.invite.errors.invalidInput') };
    return;
  }
  if (status === 429) {
    inlineError.value = { kind: 'rateLimited', message: t('portal.invite.errors.rateLimited') };
    return;
  }
  if (status === null) {
    inlineError.value = { kind: 'network', message: t('portal.invite.errors.network') };
    return;
  }
  inlineError.value = { kind: 'unknown', message: t('portal.invite.errors.unknown') };
}

async function onSubmit() {
  attemptedSubmit.value = true;
  inlineError.value = null;
  if (!token.value || !isFormValid.value || loading.value) {
    return;
  }

  loading.value = true;
  try {
    await portalApi.post('/auth/invite/redeem', {
      token: token.value,
      password: password.value,
      display_name: displayName.value.trim() || undefined,
    });
    // The redeem response already set the same three auth cookies a normal
    // login would -- resolve the session through the store's own
    // fetchCurrentUser() (single source of truth for hydrating `user`)
    // rather than hand-building a PortalUser object from this response.
    await authStore.fetchCurrentUser();
    router.push({ name: 'portal-chat' });
  } catch (err) {
    classifyError(err);
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

        <template v-if="terminalError">
          <h1 class="portal-auth-title">{{ t(`portal.invite.errors.${terminalError}.title`) }}</h1>
          <p class="portal-invite-terminal-message">{{ t(`portal.invite.errors.${terminalError}.message`) }}</p>

          <v-btn
            v-if="terminalError === 'used'"
            color="primary"
            block
            size="large"
            variant="flat"
            class="portal-auth-submit mt-4"
            @click="router.push({ name: 'portal-login' })"
          >
            {{ t('portal.invite.goToLogin') }}
          </v-btn>
          <v-btn
            v-else
            color="primary"
            block
            size="large"
            variant="tonal"
            class="portal-auth-submit mt-4"
            @click="router.push({ name: 'portal-login' })"
          >
            {{ t('portal.invite.backToLogin') }}
          </v-btn>
        </template>

        <template v-else>
          <h1 class="portal-auth-title">{{ t('portal.invite.title') }}</h1>
          <p class="portal-auth-subtitle">{{ t('portal.invite.subtitle') }}</p>

          <form class="portal-auth-form" novalidate @submit.prevent="onSubmit">
            <v-text-field
              v-model="displayName"
              :label="t('portal.invite.displayNameOptional')"
              autocomplete="nickname"
              variant="outlined"
              hide-details="auto"
              prepend-inner-icon="mdi-account-outline"
              :disabled="loading"
              autofocus
            ></v-text-field>

            <v-text-field
              v-model="password"
              :label="t('portal.invite.password')"
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
                {{ t('portal.invite.passwordRuleLength') }}
              </li>
              <li :class="{ met: passwordRules.uppercase }">
                <v-icon size="14">{{ passwordRules.uppercase ? 'mdi-check-circle' : 'mdi-circle-outline' }}</v-icon>
                {{ t('portal.invite.passwordRuleUppercase') }}
              </li>
              <li :class="{ met: passwordRules.lowercase }">
                <v-icon size="14">{{ passwordRules.lowercase ? 'mdi-check-circle' : 'mdi-circle-outline' }}</v-icon>
                {{ t('portal.invite.passwordRuleLowercase') }}
              </li>
              <li :class="{ met: passwordRules.digit }">
                <v-icon size="14">{{ passwordRules.digit ? 'mdi-check-circle' : 'mdi-circle-outline' }}</v-icon>
                {{ t('portal.invite.passwordRuleDigit') }}
              </li>
            </ul>

            <v-text-field
              v-model="confirmPassword"
              :label="t('portal.invite.confirmPassword')"
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
              v-if="inlineError"
              class="mt-4"
              :color="inlineError.kind === 'rateLimited' ? 'warning' : 'error'"
              variant="tonal"
              :icon="inlineError.kind === 'rateLimited' ? 'mdi-timer-lock-outline' : 'mdi-alert-circle'"
              border="start"
              density="comfortable"
            >
              {{ inlineError.message }}
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
              {{ t('portal.invite.submit') }}
            </v-btn>
          </form>
        </template>
      </v-card>

      <footer class="portal-auth-footer">
        <span>{{ t('portal.sourceLink') }}: </span>
        <a
          href="https://github.com/AstrBotDevs/AstrBot"
          target="_blank"
          rel="noopener noreferrer"
          class="portal-auth-footer-link"
        >
          <v-icon size="14" class="mr-1">mdi-github</v-icon>https://github.com/AstrBotDevs/AstrBot
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

.portal-invite-terminal-message {
  text-align: center;
  font-size: 0.9rem;
  line-height: 1.6;
  color: rgba(var(--v-theme-on-surface), 0.7);
  margin: 14px 0 6px;
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
