<template>
  <!--
    Deliberately renders NOTHING (not even a disabled/dead button) until we
    have positively confirmed there is at least one knowledge base this user
    could ever bind. This covers three cases identically -- the operator
    turned the feature off (`PortalSettings.kb_selection_enabled=false`,
    `GET /knowledge-bases` -> `[]`), the operator hasn't whitelisted anything
    yet (also `[]`), and the initial fetch simply hasn't resolved yet -- all
    of which should look like "no control here" rather than a button that
    then immediately does nothing useful. A genuine *failure* to load the
    list (as opposed to a confirmed-empty list) is shown separately as a
    small retry affordance below, specifically so it is never visually
    indistinguishable from "the feature is off" (see `listState`).
  -->
  <div class="kb-selector">
    <v-menu
      v-if="listState === 'ready' && availableKbs.length > 0"
      v-model="menuOpen"
      :close-on-content-click="false"
      location="bottom start"
      @update:model-value="onMenuToggle"
    >
      <template #activator="{ props: menuProps }">
        <v-btn
          v-bind="menuProps"
          class="kb-trigger"
          size="small"
          variant="tonal"
          :color="boundKbIds.length ? 'primary' : undefined"
          prepend-icon="mdi-database-search-outline"
          :disabled="disabled"
          :title="triggerTooltip"
        >
          {{ triggerLabel }}
        </v-btn>
      </template>

      <v-card class="kb-panel" elevation="6">
        <div class="kb-panel-header">
          <span class="kb-panel-title">{{ t("portal.kb.title") }}</span>
          <p class="kb-panel-subtitle">{{ t("portal.kb.subtitle") }}</p>
        </div>

        <div v-if="bindingState === 'loading'" class="kb-panel-center">
          <v-progress-circular indeterminate size="22" width="2" />
        </div>

        <div v-else-if="bindingState === 'error'" class="kb-panel-center kb-panel-error">
          <span>{{ t("portal.kb.loadBindingErrorTitle") }}</span>
          <span v-if="bindingErrorDetail" class="kb-error-detail">{{ bindingErrorDetail }}</span>
          <v-btn size="x-small" variant="text" color="primary" @click="loadBinding">
            {{ t("portal.kb.retry") }}
          </v-btn>
        </div>

        <template v-else>
          <button
            type="button"
            class="kb-none-option"
            :class="{ active: selectedKbIds.length === 0 }"
            @click="selectedKbIds = []"
          >
            <v-icon size="18">
              {{ selectedKbIds.length === 0 ? "mdi-radiobox-marked" : "mdi-radiobox-blank" }}
            </v-icon>
            <span class="kb-none-copy">
              <span class="kb-none-title">{{ t("portal.kb.noneOption") }}</span>
              <span class="kb-none-hint">{{ t("portal.kb.noneOptionHint") }}</span>
            </span>
          </button>

          <v-divider class="my-2" />

          <div class="kb-list">
            <label
              v-for="kb in availableKbs"
              :key="kb.kb_id"
              class="kb-item"
              :class="{ disabled: !kb.available }"
            >
              <v-checkbox-btn
                :model-value="selectedKbIds.includes(kb.kb_id)"
                :disabled="!kb.available"
                density="compact"
                @update:model-value="(checked) => toggleKb(kb.kb_id, Boolean(checked))"
              />
              <span class="kb-item-copy">
                <span class="kb-item-name">{{ kb.display_name }}</span>
                <span v-if="kb.description" class="kb-item-desc">{{ kb.description }}</span>
                <span class="kb-item-meta">
                  {{ t("portal.kb.docCount", { count: kb.doc_count }) }} ·
                  {{ t("portal.kb.chunkCount", { count: kb.chunk_count }) }}
                </span>
                <span v-if="!kb.available" class="kb-item-unavailable">
                  {{ t("portal.kb.unavailable") }}
                </span>
              </span>
            </label>
          </div>

          <p v-if="selectedKbIds.length >= CLIENT_SOFT_MAX_HINT" class="kb-max-hint">
            {{ t("portal.kb.maxHint", { max: CLIENT_SOFT_MAX_HINT }) }}
          </p>

          <v-alert
            v-if="saveError"
            type="error"
            density="compact"
            variant="tonal"
            class="kb-alert"
          >
            <div>{{ saveError }}</div>
            <div v-if="saveErrorDetail" class="kb-error-detail">{{ saveErrorDetail }}</div>
          </v-alert>
          <v-alert
            v-if="saveSuccess"
            type="success"
            density="compact"
            variant="tonal"
            class="kb-alert"
          >
            {{ t("portal.kb.saveSuccess") }}
          </v-alert>

          <div class="kb-panel-actions">
            <v-btn size="small" variant="text" @click="menuOpen = false">
              {{ t("portal.kb.cancel") }}
            </v-btn>
            <v-btn
              size="small"
              variant="flat"
              color="primary"
              :loading="saving"
              :disabled="!isDirty || saving"
              @click="save"
            >
              {{ t("portal.kb.save") }}
            </v-btn>
          </div>
        </template>
      </v-card>
    </v-menu>

    <button
      v-else-if="listState === 'error'"
      type="button"
      class="kb-list-error-chip"
      :title="listErrorDetail || t('portal.kb.loadListErrorTitle')"
      @click="loadAvailableKbs"
    >
      <v-icon size="14">mdi-alert-circle-outline</v-icon>
      <span>{{ t("portal.kb.loadListErrorTitle") }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
// End-user knowledge-base whitelist selector, scoped to one active
// conversation (Phase 3). Mounted by `PortalChatShell.vue` only while a
// session is active, keyed on that session's id so switching sessions gets
// a fresh component instance -- see this file's own "why keyed, not
// watched" note further down and the parent's own comment at the mount
// site.
//
// Talks to exactly two portal endpoints, both behind the operator-curated
// `PortalKbVisibility` whitelist (`astrbot/portal/api/kb_routes.py` /
// `astrbot/portal/services/kb_visibility_service.py`):
//   - `GET  /api/v1/portal/knowledge-bases` -- the whitelist this user may
//     choose from (never the operator's full KB inventory; see that
//     route's own docstring).
//   - `GET  /api/v1/portal/sessions/{id}/knowledge-base` -- this session's
//     current binding (always a well-shaped `{kb_ids, top_k}`, never
//     `null`, because the facade backfills an explicit empty binding for
//     every session -- the default-open-trap fix).
//   - `POST /api/v1/portal/sessions/{id}/knowledge-base` -- the one write
//     path; an empty `kb_ids` list is how a session is unbound (there is
//     no separate DELETE route).
//
// Deliberately reuses `portalApi` from `@/api/portal` (the shared portal
// axios client with the CSRF double-submit header already wired into its
// request interceptor) rather than duplicating `PortalChatShell.vue`'s own
// hand-rolled `fetch` helper -- this is exactly the "axios client with the
// CSRF header path already established" the assignment calls for, and
// there is no reason to re-implement it a third time in this file.
//
// Does NOT import `@/components/shared/KnowledgeBaseSelector.vue` (the
// existing *operator*-side KB picker): that component assumes an operator
// with visibility into the entire core KB inventory (it is built against
// `astrbot/dashboard/api/knowledge_bases.py`), which is precisely the
// access this phase exists to prevent for end users. This file is a
// separate, purpose-built component against the narrow, whitelist-shaped
// consumer contract above.
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

const emit = defineEmits<{
  updated: [kbConfig: { kb_ids: string[]; top_k: number }];
}>();

interface PortalKbEntry {
  kb_id: string;
  display_name: string;
  description: string | null;
  doc_count: number;
  chunk_count: number;
  available: boolean;
}

interface SessionKbConfig {
  kb_ids: string[];
  top_k: number;
}

// Soft, client-side-only nicety: `PortalSettings.max_kb_ids_per_session`
// (astrbot/portal/config.py) is NOT exposed by any portal API today, so
// this constant is a best-effort mirror of that dataclass's shipped
// default (3), used only to show an early, non-blocking hint once the
// user has selected this many. It is intentionally never used to disable
// further checkboxes: the server (`kb_visibility_service.bind_session`) is
// the sole authority on the real limit, and if an operator ever changes
// that config value this constant will silently drift out of sync with
// it. `save()` always surfaces the server's own rejection text verbatim
// (see `saveErrorDetail`) precisely so a stale guess here can never hide
// or misrepresent the real, current limit. See this phase's
// `notes_for_integrator` for why a settings-exposing endpoint would close
// this gap properly.
const CLIENT_SOFT_MAX_HINT = 3;

const listState = ref<"loading" | "ready" | "error">("loading");
const availableKbs = ref<PortalKbEntry[]>([]);
const listErrorDetail = ref("");

const bindingState = ref<"loading" | "ready" | "error">("loading");
const bindingErrorDetail = ref("");

// `boundKbIds`/`topK` are the last known server truth (used for the
// trigger label and the dirty check); `selectedKbIds` is the in-progress
// working copy the checkboxes/radio edit. They intentionally diverge until
// `save()` succeeds.
const boundKbIds = ref<string[]>([]);
const selectedKbIds = ref<string[]>([]);
const topK = ref<number | null>(null);

const menuOpen = ref(false);
const saving = ref(false);
const saveError = ref("");
const saveErrorDetail = ref("");
const saveSuccess = ref(false);
let successTimer: ReturnType<typeof setTimeout> | null = null;

const triggerLabel = computed(() => {
  if (bindingState.value === "loading") return t("portal.kb.triggerLoading");
  if (boundKbIds.value.length === 0) return t("portal.kb.triggerNone");
  return t("portal.kb.triggerBound", { count: boundKbIds.value.length });
});

const triggerTooltip = computed(() => {
  if (!boundKbIds.value.length) return "";
  const names = boundKbIds.value
    .map((id) => availableKbs.value.find((kb) => kb.kb_id === id)?.display_name)
    .filter((name): name is string => Boolean(name));
  return names.join(", ");
});

function sortedKey(ids: string[]) {
  return [...ids].sort().join(" ");
}

const isDirty = computed(() => sortedKey(selectedKbIds.value) !== sortedKey(boundKbIds.value));

function toggleKb(kbId: string, checked: boolean) {
  if (checked) {
    if (!selectedKbIds.value.includes(kbId)) selectedKbIds.value = [...selectedKbIds.value, kbId];
  } else {
    selectedKbIds.value = selectedKbIds.value.filter((id) => id !== kbId);
  }
}

// --- Axios error -> friendly message ---------------------------------------
//
// Every branch also keeps the server's own raw `detail` string (FastAPI's
// actual error-body field -- not `message`) as a secondary, un-swallowed
// line, specifically for the 400 cases (`TooManyKbIdsError`/
// `InvalidTopKError`) whose exact figures (the real configured max/ceiling)
// only the server can state authoritatively; see `CLIENT_SOFT_MAX_HINT`
// above for why this file never tries to guess those numbers itself.
function extractErrorParts(err: unknown): { status: number | null; detail: string } {
  const anyErr = err as { response?: { status?: number; data?: { detail?: string } } } | undefined;
  const status = typeof anyErr?.response?.status === "number" ? anyErr.response.status : null;
  const detail = typeof anyErr?.response?.data?.detail === "string" ? anyErr.response.data.detail : "";
  return { status, detail };
}

function friendlyBindError(status: number | null): string {
  if (status === 404) return t("portal.kb.bindErrorNotFound");
  if (status === 403) return t("portal.kb.bindErrorDisabled");
  if (status === 400) return t("portal.kb.bindErrorTooMany");
  if (status === null) return t("portal.kb.bindErrorNetwork");
  return t("portal.kb.bindErrorUnknown");
}

// --- Loaders ----------------------------------------------------------------

async function loadAvailableKbs() {
  listState.value = "loading";
  listErrorDetail.value = "";
  try {
    const res = await portalApi.get("/knowledge-bases");
    const entries = Array.isArray(res.data?.knowledge_bases) ? res.data.knowledge_bases : [];
    availableKbs.value = entries as PortalKbEntry[];
    listState.value = "ready";
  } catch (err) {
    const { detail } = extractErrorParts(err);
    listErrorDetail.value = detail;
    availableKbs.value = [];
    listState.value = "error";
  }
}

async function loadBinding() {
  bindingState.value = "loading";
  bindingErrorDetail.value = "";
  try {
    const res = await portalApi.get(
      `/sessions/${encodeURIComponent(props.sessionId)}/knowledge-base`,
    );
    const config = res.data as SessionKbConfig;
    boundKbIds.value = Array.isArray(config?.kb_ids) ? [...config.kb_ids] : [];
    selectedKbIds.value = [...boundKbIds.value];
    topK.value = typeof config?.top_k === "number" ? config.top_k : null;
    bindingState.value = "ready";
  } catch (err) {
    const { detail } = extractErrorParts(err);
    bindingErrorDetail.value = detail;
    bindingState.value = "error";
  }
}

function onMenuToggle(open: boolean) {
  if (!open) {
    // Closing without saving discards the in-progress selection back to
    // the last known-bound state, rather than leaving a half-edited
    // selection silently lingering for the next time the menu opens.
    selectedKbIds.value = [...boundKbIds.value];
    saveError.value = "";
    saveErrorDetail.value = "";
  }
}

async function save() {
  if (!isDirty.value || saving.value) return;
  saving.value = true;
  saveError.value = "";
  saveErrorDetail.value = "";
  saveSuccess.value = false;
  if (successTimer) clearTimeout(successTimer);

  try {
    const res = await portalApi.post(
      `/sessions/${encodeURIComponent(props.sessionId)}/knowledge-base`,
      {
        kb_ids: selectedKbIds.value,
        // Always send the currently-known top_k back rather than omitting
        // it: the server treats an omitted top_k as "reset to the
        // deployment default", not "leave unchanged" (see
        // kb_visibility_service.bind_session's docstring) -- omitting it
        // here would silently overwrite any previously-set value every
        // time the user only meant to change which KBs are bound.
        top_k: topK.value ?? undefined,
      },
    );
    const config = res.data as SessionKbConfig;
    boundKbIds.value = Array.isArray(config?.kb_ids) ? [...config.kb_ids] : [...selectedKbIds.value];
    selectedKbIds.value = [...boundKbIds.value];
    if (typeof config?.top_k === "number") topK.value = config.top_k;
    saveSuccess.value = true;
    successTimer = setTimeout(() => {
      saveSuccess.value = false;
    }, 2500);
    emit("updated", { kb_ids: boundKbIds.value, top_k: topK.value ?? 0 });

    // The bind just succeeded, but if it had instead failed with 404 (a
    // whitelisted id vanished from core between list-fetch and bind), the
    // available list would now be stale -- refresh it so the picker never
    // keeps offering an entry the server just rejected. Cheap and correct;
    // no harm in doing it unconditionally on success too.
    void loadAvailableKbs();
  } catch (err) {
    const { status, detail } = extractErrorParts(err);
    saveError.value = friendlyBindError(status);
    saveErrorDetail.value = detail;
    if (status === 404) {
      // A previously-visible entry likely just disappeared from the
      // whitelist/core -- refresh so the stale option isn't offered again.
      void loadAvailableKbs();
    }
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  void loadAvailableKbs();
  void loadBinding();
});
</script>

<style scoped>
.kb-selector {
  display: inline-flex;
  align-items: center;
}

.kb-trigger {
  text-transform: none;
  font-weight: 500;
}

.kb-list-error-chip {
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

.kb-panel {
  width: 340px;
  max-width: calc(100vw - 32px);
  padding: 14px 16px 12px;
}

.kb-panel-header {
  margin-bottom: 6px;
}

.kb-panel-title {
  font-size: 14px;
  font-weight: 700;
}

.kb-panel-subtitle {
  margin: 2px 0 0;
  font-size: 12px;
  color: rgba(var(--v-theme-on-surface), 0.6);
}

.kb-panel-center {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 18px 0;
  text-align: center;
}

.kb-panel-error {
  color: rgb(var(--v-theme-error));
  font-size: 12.5px;
}

.kb-none-option {
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

.kb-none-option:hover,
.kb-none-option.active {
  background: rgba(var(--v-theme-primary), 0.08);
}

.kb-none-copy {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.kb-none-title {
  font-size: 13px;
  font-weight: 600;
}

.kb-none-hint {
  font-size: 11.5px;
  color: rgba(var(--v-theme-on-surface), 0.6);
}

.kb-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 260px;
  overflow-y: auto;
}

.kb-item {
  display: flex;
  align-items: flex-start;
  gap: 4px;
  padding: 4px;
  border-radius: 8px;
  cursor: pointer;
}

.kb-item:hover {
  background: rgba(var(--v-theme-on-surface), 0.05);
}

.kb-item.disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.kb-item-copy {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding-top: 6px;
  min-width: 0;
}

.kb-item-name {
  font-size: 13px;
  font-weight: 600;
  overflow-wrap: anywhere;
}

.kb-item-desc {
  font-size: 12px;
  color: rgba(var(--v-theme-on-surface), 0.65);
  overflow-wrap: anywhere;
}

.kb-item-meta {
  font-size: 11px;
  color: rgba(var(--v-theme-on-surface), 0.5);
}

.kb-item-unavailable {
  font-size: 11px;
  font-weight: 600;
  color: rgb(var(--v-theme-error));
}

.kb-max-hint {
  margin: 6px 2px 0;
  font-size: 11.5px;
  color: rgba(var(--v-theme-on-surface), 0.55);
}

.kb-alert {
  margin-top: 8px;
  font-size: 12.5px;
}

.kb-error-detail {
  margin-top: 2px;
  font-size: 11px;
  opacity: 0.85;
  overflow-wrap: anywhere;
}

.kb-panel-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 10px;
}
</style>
