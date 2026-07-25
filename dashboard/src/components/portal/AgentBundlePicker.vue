<template>
  <!--
    End-user AI-assistant preset picker (Phase 4). Deliberately built as
    KbSelector.vue's sibling -- same trigger-chip + dropdown-panel shape,
    same three list states (loading/ready/error), same "no dead affordance"
    principle -- rather than a differently-styled control, per this
    component's own assignment ("this should read as its sibling, not as a
    different app").

    One structural difference from KbSelector, on purpose: a bundle
    selection is a single scalar choice (persona + provider + KB, resolved
    entirely server-side), not a multi-select working set, so there is no
    staged "selectedX vs boundX" + explicit Save button here -- clicking an
    option applies it immediately (POST/DELETE), exactly like choosing a
    radio option is expected to just take effect. `agent_routes.py`'s own
    module docstring makes the same "single bundle_id, not a set" framing
    for why unbind is its own DELETE route rather than a special bind value.

    Disabled-vs-empty-curated ambiguity (documented gap, not silently
    swallowed): the assignment asks for two different empty treatments --
    "show nothing at all if the operator disabled the feature" vs "explain
    the empty case (no bundles curated yet) rather than an empty dropdown".
    `GET /agent-bundles` (agent_bundle_service.list_available) returns an
    identical `{"agent_bundles": []}` for BOTH
    `agent_bundle_selection_enabled=False` AND "enabled but nothing curated
    yet / nothing currently selectable" -- there is no separate flag in the
    response to tell those apart, and this file may not invent one (it does
    not own agent_routes.py/agent_bundle_service.py). This component
    therefore renders the same small, non-interactive explanatory hint (see
    `.bundle-empty-hint` below) for the empty case in both scenarios --
    honoring the "no empty dropdown" requirement always, at the documented
    cost of not being able to fully honor "show nothing at all" when the
    real cause is the feature being off. See this phase's
    `notes_for_integrator` for the concrete fix (an `enabled` boolean on the
    list response, mirroring how `bind_session` already distinguishes the
    two cases with `AgentBundleSelectionDisabledError` vs
    `ResourceNotFoundError`).
  -->
  <div class="bundle-picker">
    <v-menu
      v-if="listState === 'ready' && availableBundles.length > 0"
      v-model="menuOpen"
      :close-on-content-click="false"
      location="bottom start"
      @update:model-value="onMenuToggle"
    >
      <template #activator="{ props: menuProps }">
        <v-btn
          v-bind="menuProps"
          class="bundle-trigger"
          size="small"
          variant="tonal"
          :color="boundBundle ? 'primary' : undefined"
          prepend-icon="mdi-account-switch-outline"
          :disabled="disabled"
          :title="triggerTooltip"
        >
          {{ triggerLabel }}
        </v-btn>
      </template>

      <v-card class="bundle-panel" elevation="6">
        <div class="bundle-panel-header">
          <span class="bundle-panel-title">{{ t("portal.agentBundle.title") }}</span>
          <p class="bundle-panel-subtitle">{{ t("portal.agentBundle.subtitle") }}</p>
        </div>

        <div v-if="bindingState === 'loading'" class="bundle-panel-center">
          <v-progress-circular indeterminate size="22" width="2" />
        </div>

        <div v-else-if="bindingState === 'error'" class="bundle-panel-center bundle-panel-error">
          <span>{{ t("portal.agentBundle.loadBindingErrorTitle") }}</span>
          <span v-if="bindingErrorDetail" class="bundle-error-detail">{{ bindingErrorDetail }}</span>
          <v-btn size="x-small" variant="text" color="primary" @click="loadBinding">
            {{ t("portal.agentBundle.retry") }}
          </v-btn>
        </div>

        <template v-else>
          <button
            type="button"
            class="bundle-option bundle-default-option"
            :class="{ active: !boundBundle }"
            :disabled="applying"
            @click="selectDefault"
          >
            <v-icon size="18">
              {{ !boundBundle ? "mdi-radiobox-marked" : "mdi-radiobox-blank" }}
            </v-icon>
            <span class="bundle-option-copy">
              <span class="bundle-option-name">{{ t("portal.agentBundle.defaultOption") }}</span>
              <span class="bundle-option-hint">{{ t("portal.agentBundle.defaultOptionHint") }}</span>
            </span>
          </button>

          <v-divider class="my-2" />

          <div class="bundle-list">
            <button
              v-for="bundle in availableBundles"
              :key="bundle.bundle_id"
              type="button"
              class="bundle-option"
              :class="{ active: boundBundle?.bundle_id === bundle.bundle_id }"
              :disabled="applying"
              @click="selectBundle(bundle)"
            >
              <v-icon size="18">
                {{
                  boundBundle?.bundle_id === bundle.bundle_id
                    ? "mdi-radiobox-marked"
                    : "mdi-radiobox-blank"
                }}
              </v-icon>
              <span class="bundle-option-copy">
                <span class="bundle-option-name">
                  {{ bundle.name }}
                  <v-chip
                    v-if="bundle.is_default"
                    size="x-small"
                    variant="tonal"
                    color="primary"
                    class="bundle-recommended-chip"
                  >
                    {{ t("portal.agentBundle.recommendedTag") }}
                  </v-chip>
                </span>
                <span class="bundle-option-desc">
                  {{ bundle.description || t("portal.agentBundle.noDescription") }}
                </span>
              </span>
            </button>
          </div>

          <v-alert
            v-if="applyError"
            type="error"
            density="compact"
            variant="tonal"
            class="bundle-alert"
          >
            <div>{{ applyError }}</div>
            <div v-if="applyErrorDetail" class="bundle-error-detail">{{ applyErrorDetail }}</div>
          </v-alert>

          <div v-if="applying" class="bundle-panel-applying">
            <v-progress-circular indeterminate size="16" width="2" />
            <span>{{ t("portal.agentBundle.applying") }}</span>
          </div>
        </template>
      </v-card>
    </v-menu>

    <button
      v-else-if="listState === 'error'"
      type="button"
      class="bundle-list-error-chip"
      :title="listErrorDetail || t('portal.agentBundle.loadListErrorTitle')"
      @click="loadAvailableBundles"
    >
      <v-icon size="14">mdi-alert-circle-outline</v-icon>
      <span>{{ t("portal.agentBundle.loadListErrorTitle") }}</span>
    </button>

    <!--
      Empty-curated (or feature-disabled -- see the ambiguity note above)
      state: a small, entirely non-interactive label, never a clickable
      no-op. This is the one branch that deliberately differs from
      KbSelector.vue's own choice to render nothing at all for its
      equivalent empty case -- see this file's own top-of-template
      docstring for why.
    -->
    <span
      v-else-if="listState === 'ready' && availableBundles.length === 0"
      class="bundle-empty-hint"
      :title="t('portal.agentBundle.emptyTooltip')"
    >
      <v-icon size="14">mdi-account-off-outline</v-icon>
      <span>{{ t("portal.agentBundle.empty") }}</span>
    </span>
  </div>
</template>

<script setup lang="ts">
// End-user AI-assistant ("Agent Bundle") preset selector, scoped to one
// active conversation (Phase 4). Mounted by `PortalChatShell.vue` only
// while a session is active, keyed on that session's id so switching
// sessions gets a fresh component instance -- same reason as
// `KbSelector.vue`'s own instance (see that file's own comment).
//
// Talks to exactly three portal endpoints, all behind the operator-curated
// `PortalAgentBundle` whitelist (`astrbot/portal/api/agent_routes.py` /
// `astrbot/portal/services/agent_bundle_service.py`):
//   - `GET    /api/v1/portal/agent-bundles` -- the enabled, currently-
//     selectable presets this user may choose from. Never leaks
//     `persona_id`/`chat_provider_id`/`kb_ids` -- a bundle is an opaque
//     choice by design (see that route's own module docstring, "Bundle
//     metadata exposed to end users").
//   - `GET    /api/v1/portal/sessions/{id}/agent-bundle` -- this session's
//     current selection, or `{"agent_bundle": null}` if nothing is
//     selected (the operator's own default is then in effect -- rule 7,
//     "default closed").
//   - `POST   /api/v1/portal/sessions/{id}/agent-bundle` -- select one
//     bundle by `bundle_id`. THE ONLY FIELD THIS FILE EVER SENDS. There is
//     no provider/model field anywhere in this component, on purpose: the
//     client picks an opaque bundle id, the server resolves what persona/
//     provider/KB that means (rule 6). Do not add one.
//   - `DELETE /api/v1/portal/sessions/{id}/agent-bundle` -- clear back to
//     the operator's plain default. Its own route (not a bind with a null/
//     empty id) -- see `agent_routes.py`'s own "Unbinding" section for why
//     a single required `bundle_id` has no natural empty sentinel the way
//     KbSelector's `kb_ids` list does.
//
// Reuses `portalApi` from `@/api/portal` exactly like `KbSelector.vue` --
// see that file's own comment for why (CSRF double-submit header already
// wired into the request interceptor; no reason to hand-roll a second
// fetch helper here).
//
// Does NOT import `@/components/chat/ProviderModelMenu.vue` or
// `@/components/chat/ConfigSelector.vue` (the operator's own
// provider/model + full config pickers): those are precisely the
// end-user-facing surface this whole phase exists to keep away from
// portal users (the server resolves a bundle's provider server-side; the
// client only ever names a `bundle_id`). The ESLint portal import-boundary
// rule also refuses `ProviderModelMenu.vue` if this file ever tried.
import { computed, onMounted, ref } from "vue";
import { useModuleI18n } from "@/i18n/composables";
import { portalApi } from "@/api/portal";

const { tm: t } = useModuleI18n("features/auth");

const props = withDefaults(
  defineProps<{
    sessionId: string;
    disabled?: boolean;
  }>(),
  { disabled: false },
);

// Fires once a bind/unbind actually succeeds server-side -- carries either
// the narrow `{bundle_id, name}` the server just confirmed, or `null` for
// "cleared back to default". Never fires for the initial `loadBinding()`
// GET on mount; only for a user-initiated change. The parent
// (`PortalChatShell.vue`) is the one that decides whether/how to make this
// legible in the message thread (this component only knows about the API,
// not about thread rendering) -- see that file's own handler for the
// chosen treatment (a client-side-only divider record).
const emit = defineEmits<{
  switched: [bundle: { bundle_id: string; name: string } | null];
}>();

interface ConsumerBundleEntry {
  bundle_id: string;
  name: string;
  description: string | null;
  sort_order: number;
  is_default: boolean;
}

const listState = ref<"loading" | "ready" | "error">("loading");
const availableBundles = ref<ConsumerBundleEntry[]>([]);
const listErrorDetail = ref("");

const bindingState = ref<"loading" | "ready" | "error">("loading");
const bindingErrorDetail = ref("");

// Last known server truth for this session's selection (`null` = no
// bundle bound, i.e. the operator's plain default is in effect). Unlike
// `KbSelector.vue` there is no separate "working copy" ref: a bundle
// selection applies immediately on click, so `boundBundle` is the only
// piece of selection state this component holds.
const boundBundle = ref<ConsumerBundleEntry | null>(null);

const menuOpen = ref(false);
const applying = ref(false);
const applyError = ref("");
const applyErrorDetail = ref("");

const triggerLabel = computed(() => {
  if (bindingState.value === "loading") return t("portal.agentBundle.triggerLoading");
  if (!boundBundle.value) return t("portal.agentBundle.triggerDefault");
  return boundBundle.value.name;
});

const triggerTooltip = computed(() => boundBundle.value?.description || "");

// --- Axios error -> friendly message ---------------------------------------
// Mirrors `KbSelector.vue`'s own small helper verbatim (no shared portal
// API-error-util module exists across the two components' file
// ownership -- see `PortalChatShell.vue`'s own note on why no such shared
// client module was introduced in this phase).
function extractErrorParts(err: unknown): { status: number | null; detail: string } {
  const anyErr = err as { response?: { status?: number; data?: { detail?: string } } } | undefined;
  const status = typeof anyErr?.response?.status === "number" ? anyErr.response.status : null;
  const detail = typeof anyErr?.response?.data?.detail === "string" ? anyErr.response.data.detail : "";
  return { status, detail };
}

function friendlyApplyError(status: number | null): string {
  if (status === 404) return t("portal.agentBundle.applyErrorNotFound");
  if (status === 403) return t("portal.agentBundle.applyErrorDisabled");
  if (status === null) return t("portal.agentBundle.applyErrorNetwork");
  return t("portal.agentBundle.applyErrorUnknown");
}

// --- Loaders ----------------------------------------------------------------

async function loadAvailableBundles() {
  listState.value = "loading";
  listErrorDetail.value = "";
  try {
    const res = await portalApi.get("/agent-bundles");
    const entries = Array.isArray(res.data?.agent_bundles) ? res.data.agent_bundles : [];
    availableBundles.value = entries as ConsumerBundleEntry[];
    listState.value = "ready";
  } catch (err) {
    const { detail } = extractErrorParts(err);
    listErrorDetail.value = detail;
    availableBundles.value = [];
    listState.value = "error";
  }
}

async function loadBinding() {
  bindingState.value = "loading";
  bindingErrorDetail.value = "";
  try {
    const res = await portalApi.get(
      `/sessions/${encodeURIComponent(props.sessionId)}/agent-bundle`,
    );
    const entry = res.data?.agent_bundle;
    boundBundle.value = entry ? (entry as ConsumerBundleEntry) : null;
    bindingState.value = "ready";
  } catch (err) {
    const { detail } = extractErrorParts(err);
    bindingErrorDetail.value = detail;
    bindingState.value = "error";
  }
}

function onMenuToggle(open: boolean) {
  if (!open) {
    applyError.value = "";
    applyErrorDetail.value = "";
  }
}

// --- Apply (immediate, no staged/dirty state -- see template docstring) ----

async function selectBundle(bundle: ConsumerBundleEntry) {
  if (applying.value) return;
  if (boundBundle.value?.bundle_id === bundle.bundle_id) {
    // Already selected -- nothing to apply, just close like any other
    // completed choice.
    menuOpen.value = false;
    return;
  }
  applying.value = true;
  applyError.value = "";
  applyErrorDetail.value = "";
  try {
    const res = await portalApi.post(
      `/sessions/${encodeURIComponent(props.sessionId)}/agent-bundle`,
      // The ONLY field ever sent. See module docstring / rule 6 -- there is
      // no provider/model/persona field here or anywhere in this file.
      { bundle_id: bundle.bundle_id },
    );
    const entry = res.data as ConsumerBundleEntry;
    boundBundle.value = entry;
    menuOpen.value = false;
    emit("switched", { bundle_id: entry.bundle_id, name: entry.name });
    // The apply just succeeded, but if it had instead failed with 404 (a
    // previously-listed bundle vanished/went dangling between list-fetch
    // and apply), the available list would now be stale -- refresh it so
    // this picker never keeps offering an entry the server just rejected.
    // Cheap and correct to also do on success.
    void loadAvailableBundles();
  } catch (err) {
    const { status, detail } = extractErrorParts(err);
    applyError.value = friendlyApplyError(status);
    applyErrorDetail.value = detail;
    if (status === 404) void loadAvailableBundles();
  } finally {
    applying.value = false;
  }
}

async function selectDefault() {
  if (applying.value) return;
  if (!boundBundle.value) {
    // Already on the default -- nothing to apply, just close.
    menuOpen.value = false;
    return;
  }
  applying.value = true;
  applyError.value = "";
  applyErrorDetail.value = "";
  try {
    await portalApi.delete(`/sessions/${encodeURIComponent(props.sessionId)}/agent-bundle`);
    boundBundle.value = null;
    menuOpen.value = false;
    emit("switched", null);
  } catch (err) {
    const { detail } = extractErrorParts(err);
    applyError.value = t("portal.agentBundle.applyErrorUnknown");
    applyErrorDetail.value = detail;
  } finally {
    applying.value = false;
  }
}

onMounted(() => {
  void loadAvailableBundles();
  void loadBinding();
});
</script>

<style scoped>
.bundle-picker {
  display: inline-flex;
  align-items: center;
}

.bundle-trigger {
  text-transform: none;
  font-weight: 500;
}

.bundle-list-error-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: none;
  border-radius: 999px;
  padding: 4px 10px;
  background: rgba(var(--v-theme-error), 0.1);
  color: rgb(var(--v-theme-error));
  font-size: 12px;
  cursor: pointer;
}

.bundle-empty-hint {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border-radius: 999px;
  padding: 4px 10px;
  background: rgba(var(--v-theme-on-surface), 0.06);
  color: rgba(var(--v-theme-on-surface), 0.55);
  font-size: 12px;
  cursor: default;
  user-select: none;
}

.bundle-panel {
  width: 340px;
  max-width: calc(100vw - 32px);
  padding: 14px 16px 12px;
}

.bundle-panel-header {
  margin-bottom: 6px;
}

.bundle-panel-title {
  font-size: 14px;
  font-weight: 700;
}

.bundle-panel-subtitle {
  margin: 2px 0 0;
  font-size: 12px;
  color: rgba(var(--v-theme-on-surface), 0.6);
}

.bundle-panel-center {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 18px 0;
  text-align: center;
}

.bundle-panel-error {
  color: rgb(var(--v-theme-error));
  font-size: 12.5px;
}

.bundle-option {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  width: 100%;
  padding: 6px 4px;
  border: none;
  background: transparent;
  border-radius: 8px;
  cursor: pointer;
  text-align: left;
  color: inherit;
  font: inherit;
}

.bundle-option:hover,
.bundle-option.active {
  background: rgba(var(--v-theme-primary), 0.08);
}

.bundle-option:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.bundle-option-copy {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.bundle-option-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  overflow-wrap: anywhere;
}

.bundle-recommended-chip {
  font-weight: 600;
}

.bundle-option-hint,
.bundle-option-desc {
  font-size: 11.5px;
  color: rgba(var(--v-theme-on-surface), 0.6);
  overflow-wrap: anywhere;
}

.bundle-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 300px;
  overflow-y: auto;
}

.bundle-alert {
  margin-top: 8px;
  font-size: 12.5px;
}

.bundle-error-detail {
  margin-top: 2px;
  font-size: 11px;
  opacity: 0.85;
  overflow-wrap: anywhere;
}

.bundle-panel-applying {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 12px;
  color: rgba(var(--v-theme-on-surface), 0.6);
}
</style>
