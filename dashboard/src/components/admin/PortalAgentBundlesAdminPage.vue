<template>
  <div class="portal-agent-bundles-page">
    <!-- Consequence banner. Deliberately persistent (no close button) and
         worded as a spend control, not a routine feature switch -- see this
         page's assignment: enabling a bundle here lets EVERY portal end
         user route their conversation to the provider it names, which is a
         direct cost decision, not just a UX preset. -->
    <v-alert
      type="warning"
      variant="tonal"
      icon="mdi-cash-sync"
      density="comfortable"
      class="mb-4"
      border="start"
    >
      <div class="text-subtitle-2 font-weight-bold mb-1">这是决定门户用户能把钱花在哪个模型上的控制台</div>
      <div class="text-body-2">
        一个套餐 = 一个人格 + 一个对话模型提供商 + 一组知识库的组合。只要某个套餐被标记为「已启用」，
        <strong>所有</strong>门户终端用户都可以在会话中选择它 —— 选择的那一刻起，该用户的每一轮对话都会被
        路由到这里指定的提供商，产生的调用成本由运营方承担。请像审批一次预算授权一样对待每一次启用，而不是
        当作界面上的一个普通开关。人格与知识库同理会立即对所有用户生效。
      </div>
    </v-alert>

    <v-card flat>
      <v-card-title class="d-flex align-center flex-wrap py-3 px-4" style="gap: 8px">
        <span class="text-h5">门户 Agent 套餐</span>
        <v-chip size="small" class="ml-1">{{ bundles.length }} 条记录</v-chip>
        <v-chip v-if="danglingCount > 0" size="small" color="error" variant="tonal" class="ml-1">
          <v-icon start size="14">mdi-alert-circle-outline</v-icon>
          {{ danglingCount }} 条引用失效
        </v-chip>
        <v-spacer />
        <v-btn
          color="primary"
          prepend-icon="mdi-plus"
          variant="tonal"
          size="small"
          class="mr-2"
          :disabled="loading"
          @click="openCreateDialog"
        >
          创建新套餐
        </v-btn>
        <v-btn
          color="default"
          prepend-icon="mdi-refresh"
          variant="tonal"
          size="small"
          :loading="loading"
          @click="refreshAll"
        >
          刷新
        </v-btn>
      </v-card-title>

      <v-divider></v-divider>

      <v-alert v-if="loadError" type="error" variant="tonal" density="compact" class="ma-4">
        {{ loadError }}
      </v-alert>

      <v-card-text class="pa-0">
        <v-data-table
          :headers="headers"
          :items="bundles"
          :loading="loading"
          item-value="bundle_id"
          class="elevation-0"
        >
          <template #item.status="{ item }">
            <div class="d-flex flex-column ga-1">
              <v-tooltip location="top" :disabled="!item.dangling">
                <template #activator="{ props: tooltipProps }">
                  <div v-bind="tooltipProps">
                    <v-switch
                      :model-value="item.enabled"
                      color="warning"
                      density="compact"
                      hide-details
                      inset
                      :loading="togglingId === item.bundle_id"
                      :disabled="togglingId === item.bundle_id"
                      @update:model-value="(val) => onToggleEnabled(item, Boolean(val))"
                    />
                  </div>
                </template>
                <span>该套餐的人格/提供商/知识库存在失效引用，即便启用，门户用户在修复前也无法实际选中它</span>
              </v-tooltip>
              <v-chip v-if="item.is_default" size="x-small" color="primary" variant="tonal">默认</v-chip>
            </div>
          </template>

          <template #item.bundle="{ item }">
            <div class="py-1">
              <div class="text-body-2 font-weight-medium">{{ item.name }}</div>
              <div
                v-if="item.description"
                class="text-caption text-medium-emphasis"
                style="max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap"
                :title="item.description"
              >
                {{ item.description }}
              </div>
              <div class="text-caption text-medium-emphasis" style="font-family: monospace">
                {{ item.bundle_id }}
              </div>
            </div>
          </template>

          <template #item.persona="{ item }">
            <div v-if="item.persona_id" class="d-flex flex-column ga-1">
              <span class="text-body-2">{{ item.persona_id }}</span>
              <v-chip v-if="item.persona_dangling" size="x-small" color="error" variant="tonal">
                <v-icon start size="12">mdi-link-off</v-icon>
                人格已失效
              </v-chip>
            </div>
            <span v-else class="text-body-2 text-medium-emphasis">不指定（沿用会话原有人格）</span>
          </template>

          <template #item.provider="{ item }">
            <div v-if="item.chat_provider_id" class="d-flex flex-column ga-1">
              <span class="text-body-2 font-weight-medium">{{ item.chat_provider_id }}</span>
              <v-chip v-if="item.provider_dangling" size="x-small" color="error" variant="tonal">
                <v-icon start size="12">mdi-link-off</v-icon>
                提供商已失效
              </v-chip>
            </div>
            <span v-else class="text-body-2 text-medium-emphasis">不指定（使用运营方默认提供商）</span>
          </template>

          <template #item.kb="{ item }">
            <div class="d-flex flex-column ga-1">
              <span class="text-body-2">{{ item.kb_ids.length }} 个知识库</span>
              <v-chip v-if="item.dangling_kb_ids.length > 0" size="x-small" color="error" variant="tonal">
                <v-icon start size="12">mdi-link-off</v-icon>
                {{ item.dangling_kb_ids.length }} 个已失效
              </v-chip>
            </div>
          </template>

          <template #item.updated="{ item }">
            <div class="text-body-2 text-medium-emphasis">{{ item.updated_by || '-' }}</div>
            <div class="text-caption text-medium-emphasis">{{ formatTime(item.updated_at) }}</div>
          </template>

          <template #item.actions="{ item }">
            <v-btn size="small" variant="tonal" color="primary" @click="openEditDialog(item)">
              <v-icon>mdi-pencil</v-icon>
              <v-tooltip activator="parent" location="top">编辑套餐</v-tooltip>
            </v-btn>
          </template>

          <template #no-data>
            <div class="text-center py-8">
              <v-icon size="64" color="grey-400">mdi-robot-confused-outline</v-icon>
              <div class="text-h6 mt-4 text-grey-600">尚未创建任何套餐</div>
              <div class="text-body-2 text-grey-500">在此之前，门户终端用户看不到任何可选的 Agent 套餐。</div>
              <v-btn color="primary" variant="tonal" class="mt-4" @click="openCreateDialog">
                <v-icon start>mdi-plus</v-icon>
                创建新套餐
              </v-btn>
            </div>
          </template>
        </v-data-table>
      </v-card-text>
    </v-card>

    <!-- Create / edit dialog. Always pre-loads the full current bundle
         (edit) or blank defaults (create) because admin_update is a FULL
         REPLACE on the backend -- there is no partial-patch semantics, so
         every field must round-trip through this form on every save. -->
    <v-dialog v-model="formDialog.open" max-width="680" persistent scrollable>
      <v-card>
        <v-card-title class="text-h6">{{ formDialog.editing ? '编辑套餐' : '创建新套餐' }}</v-card-title>
        <v-card-text style="max-height: 70vh">
          <v-alert type="warning" variant="tonal" density="compact" class="mb-4">
            启用后，所有门户终端用户都能立即选择这个套餐，并把对话路由到下方指定的提供商 —— 这直接决定了
            运营方在该提供商上的花费。请仅在确认这个组合可以对外承担成本时启用。
          </v-alert>

          <v-text-field
            v-model="form.name"
            label="套餐名称（面向门户用户展示）"
            variant="outlined"
            density="comfortable"
            maxlength="100"
            counter
            class="mb-2"
          />
          <v-textarea
            v-model="form.description"
            label="套餐描述（面向门户用户展示，可选）"
            variant="outlined"
            density="comfortable"
            maxlength="1000"
            counter
            rows="2"
            class="mb-2"
          />

          <v-select
            v-model="form.persona_id"
            :items="personaItems"
            item-title="title"
            item-value="value"
            label="人格（可留空：不指定，沿用会话原有人格）"
            variant="outlined"
            density="comfortable"
            :loading="loadingPersonas"
            clearable
            class="mb-2"
          >
            <template #no-data>
              <div class="pa-4 text-body-2 text-medium-emphasis">核心中暂无可用人格</div>
            </template>
          </v-select>

          <v-select
            v-model="form.chat_provider_id"
            :items="providerItems"
            item-title="title"
            item-value="value"
            label="对话模型提供商（可留空：不指定，使用运营方默认提供商）"
            variant="outlined"
            density="comfortable"
            :loading="loadingProviders"
            clearable
            hint="只列出当前已启用的对话（chat_completion）类提供商"
            persistent-hint
            class="mb-2"
          >
            <template #no-data>
              <div class="pa-4 text-body-2 text-medium-emphasis">核心中暂无已启用的对话提供商</div>
            </template>
          </v-select>

          <v-select
            v-model="form.kb_ids"
            :items="kbItems"
            item-title="title"
            item-value="value"
            label="知识库（仅可选择已对门户授权可见的知识库，可多选）"
            variant="outlined"
            density="comfortable"
            :loading="loadingKbs"
            multiple
            chips
            closable-chips
            hint="来源于「门户知识库可见性」页面的已授权名单；数量上限由运营侧配置校验，超出会在保存时报错"
            persistent-hint
            class="mb-2"
          >
            <template #no-data>
              <div class="pa-4 text-body-2 text-medium-emphasis">尚无已授权门户可见的知识库</div>
            </template>
          </v-select>

          <v-row>
            <v-col cols="6">
              <v-text-field
                v-model.number="form.sort_order"
                type="number"
                label="排序权重（越小越靠前，不影响权限）"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="6" class="d-flex align-center ga-4">
              <v-switch v-model="form.is_default" label="标记为默认套餐（仅前端提示，不自动绑定）" density="comfortable" hide-details inset />
            </v-col>
          </v-row>

          <v-switch
            v-model="form.enabled"
            :label="form.enabled ? '已启用（门户用户可选择）' : '已停用（门户用户不可选择）'"
            color="warning"
            density="comfortable"
            hide-details
            inset
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="formSaving" @click="formDialog.open = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="formSaving"
            :disabled="!form.name.trim()"
            @click="submitForm"
          >
            {{ formDialog.editing ? '保存' : '创建' }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snackbar.open" :color="snackbar.color" :timeout="4000">
      {{ snackbar.text }}
    </v-snackbar>
  </div>
</template>

<script setup lang="ts">
// Operator-only admin page for curating the portal's Agent Bundle catalog
// (Phase 4). Lives in the OPERATOR dashboard app (dashboard/index.html),
// gated client-side by MainRoutes.ts's `requiresAuth: true` and
// server-side by every `/api/v1/portal/admin/agent-bundles*` route
// requiring `Depends(require_system_scope)` -- same reasoning as
// PortalKbVisibilityAdminPage.vue (Phase 3), which this page is
// deliberately built to look and behave like: an operator will use both
// pages together (curate KB visibility, then compose bundles that draw
// from it), so the layout, copy register, and interaction patterns
// (banner, chip counts, dangling flags, confirm-before-enable) are shared
// on purpose rather than re-invented.
//
// Reuses the dashboard's OWN api client (`apiV1Client` from `@/api/http`,
// baseURL '/api/v1', Bearer-token-from-localStorage auth via its existing
// interceptors) for the bundle CRUD itself -- NOT `@/api/portal.ts` (the
// end-user portal SPA's cookie+CSRF client, a different auth domain this
// operator page must never touch). `personaApi`/`providerApi` from
// `@/api/v1` are the SAME generated operator-dashboard clients every other
// admin view (PersonaSelector.vue, ProviderPage.vue) already uses to read
// the live persona/provider inventory -- this page is the one place an
// operator picks from that inventory to compose a bundle, so it draws on
// the real inventory rather than re-deriving it.
//
// Backend contract (astrbot/portal/api/admin_routes.py +
// astrbot/portal/services/agent_bundle_service.py, both on disk, not owned
// by this assignment):
//   GET    /portal/admin/agent-bundles                -> { agent_bundles: AgentBundleEntry[] }
//   POST   /portal/admin/agent-bundles                 (create, full body)
//   PUT    /portal/admin/agent-bundles/{bundle_id}      (full-replace update -- every
//          mutable field must be resupplied, `persona_id`/`chat_provider_id`
//          being `null` is itself meaningful ("this bundle doesn't touch
//          that dimension"), never a partial patch -- this is why
//          openEditDialog() pre-loads every field from the row rather than
//          leaving anything to a server-side "leave unchanged" default)
//   PATCH  /portal/admin/agent-bundles/{bundle_id}/enabled  (body: { enabled })
// `kb_ids` must be drawn from the portal's own KB-visibility whitelist
// (enabled=true rows of GET /portal/admin/kb-visibility), not from core's
// raw KB catalog -- the backend re-validates this at both save time and
// bundle-apply time (see agent_bundle_service.py's own docstring), so this
// page's picker mirrors that same whitelist rather than the full core KB
// list PortalKbVisibilityAdminPage's own "core-kbs" endpoint exposes.
//
// i18n note: same precedent as PortalKbVisibilityAdminPage.vue -- text is
// hardcoded in Simplified Chinese rather than wired into the global i18n
// system, since translations.ts (statically-imported module list) is not
// in this assignment's file ownership.
import { computed, onMounted, reactive, ref } from 'vue';
import { apiV1Client } from '@/api/http';
import { personaApi, providerApi } from '@/api/v1';

const ADMIN_BASE = '/portal/admin/agent-bundles';
const KB_VISIBILITY_BASE = '/portal/admin/kb-visibility';

interface AgentBundleEntry {
  bundle_id: string;
  name: string;
  description: string | null;
  persona_id: string | null;
  chat_provider_id: string | null;
  kb_ids: string[];
  enabled: boolean;
  sort_order: number;
  is_default: boolean;
  updated_by: string | null;
  created_at: string;
  updated_at: string;
  persona_dangling: boolean;
  provider_dangling: boolean;
  dangling_kb_ids: string[];
  dangling: boolean;
}

interface PersonaOption {
  persona_id: string;
  system_prompt?: string;
  [key: string]: unknown;
}

interface ProviderOption {
  id: string;
  enable?: boolean;
  model?: string;
  provider?: string;
  [key: string]: unknown;
}

interface KbVisibilityOption {
  kb_id: string;
  kb_name: string;
  display_name: string | null;
  enabled: boolean;
  [key: string]: unknown;
}

const bundles = ref<AgentBundleEntry[]>([]);
const personas = ref<PersonaOption[]>([]);
const providers = ref<ProviderOption[]>([]);
const kbVisibility = ref<KbVisibilityOption[]>([]);

const loading = ref(false);
const loadingPersonas = ref(false);
const loadingProviders = ref(false);
const loadingKbs = ref(false);
const loadError = ref('');
const togglingId = ref<string | null>(null);

const headers = [
  { title: '状态', key: 'status', sortable: false, width: '110px' },
  { title: '套餐', key: 'bundle', sortable: false, minWidth: '220px' },
  { title: '人格', key: 'persona', sortable: false, minWidth: '160px' },
  { title: '提供商', key: 'provider', sortable: false, minWidth: '160px' },
  { title: '知识库', key: 'kb', sortable: false, width: '130px' },
  { title: '更新', key: 'updated', sortable: false, width: '160px' },
  { title: '操作', key: 'actions', sortable: false, width: '90px' },
];

const danglingCount = computed(() => bundles.value.filter((b) => b.dangling).length);

const snackbar = reactive({ open: false, text: '', color: 'success' as 'success' | 'error' });
function notify(text: string, color: 'success' | 'error' = 'success') {
  snackbar.text = text;
  snackbar.color = color;
  snackbar.open = true;
}

function extractErrorMessage(error: unknown, fallback: string): string {
  const err = error as { response?: { data?: { detail?: unknown } }; message?: string };
  const detail = err.response?.data?.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: string };
    if (first?.msg) return first.msg;
  }
  return err.message || fallback;
}

function formatTime(iso: string): string {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

// ---------------------------------------------------------------------
// Loading: bundles (this page's own data) + the three inventories a
// bundle is composed from (personas / chat providers / KB whitelist).
// ---------------------------------------------------------------------

async function loadBundles() {
  const res = await apiV1Client.get<{ agent_bundles: AgentBundleEntry[] }>(ADMIN_BASE);
  bundles.value = res.data.agent_bundles || [];
}

async function loadPersonas() {
  loadingPersonas.value = true;
  try {
    // No folder_id query param -> service.list_personas(None, False) ->
    // get_all_personas(): every persona regardless of folder, which is
    // exactly the flat inventory this picker needs.
    const res = await personaApi.list();
    personas.value = (res.data.data || []) as PersonaOption[];
  } finally {
    loadingPersonas.value = false;
  }
}

async function loadProviders() {
  loadingProviders.value = true;
  try {
    // capability: 'chat' -> provider_type 'chat_completion', matching
    // this bundle's own `chat_provider_id` dimension (core's
    // provider_perf_chat_completion key) -- never speech/embedding/rerank
    // providers, which this phase's bundles do not touch.
    const res = await providerApi.list({ capability: 'chat' });
    providers.value = (res.data.data?.providers || []) as ProviderOption[];
  } finally {
    loadingProviders.value = false;
  }
}

async function loadKbVisibility() {
  loadingKbs.value = true;
  try {
    const res = await apiV1Client.get<{ kb_visibility: KbVisibilityOption[] }>(KB_VISIBILITY_BASE);
    kbVisibility.value = res.data.kb_visibility || [];
  } finally {
    loadingKbs.value = false;
  }
}

async function refreshAll() {
  loading.value = true;
  loadError.value = '';
  try {
    await Promise.all([loadBundles(), loadPersonas(), loadProviders(), loadKbVisibility()]);
  } catch (error) {
    loadError.value = extractErrorMessage(error, '加载 Agent 套餐列表失败');
  } finally {
    loading.value = false;
  }
}

// ---------------------------------------------------------------------
// Picker options. Each includes the currently-edited bundle's own value
// even if it has gone dangling/disabled/invisible in the meantime, so the
// form never silently blanks out what a bundle is actually currently
// configured to -- the operator must see and consciously replace a broken
// reference, not have it disappear.
// ---------------------------------------------------------------------

const personaItems = computed(() => {
  const items = personas.value.map((p) => ({
    title: p.system_prompt
      ? `${p.persona_id}（${String(p.system_prompt).slice(0, 24)}...）`
      : p.persona_id,
    value: p.persona_id,
  }));
  const current = form.persona_id;
  if (current && !items.some((i) => i.value === current)) {
    items.push({ title: `${current}（已失效：核心中不存在此人格）`, value: current });
  }
  return items;
});

const providerItems = computed(() => {
  const items = providers.value
    .filter((p) => p.enable)
    .map((p) => ({
      title: p.model ? `${p.id}（${p.model}）` : p.id,
      value: p.id,
    }));
  const current = form.chat_provider_id;
  if (current && !items.some((i) => i.value === current)) {
    items.push({ title: `${current}（已失效：核心中不存在或已停用此提供商）`, value: current });
  }
  return items;
});

const kbItems = computed(() => {
  const items = kbVisibility.value
    .filter((kb) => kb.enabled)
    .map((kb) => ({
      title: kb.display_name || kb.kb_name || kb.kb_id,
      value: kb.kb_id,
    }));
  for (const kbId of form.kb_ids) {
    if (!items.some((i) => i.value === kbId)) {
      items.push({ title: `${kbId}（已失效：不再对门户可见）`, value: kbId });
    }
  }
  return items;
});

// ---------------------------------------------------------------------
// Create / edit dialog. `formDialog.editing` holds the bundle_id being
// edited (null while creating), matching admin_update's full-replace
// contract: every field the form shows is exactly what gets resubmitted.
// ---------------------------------------------------------------------

const formDialog = reactive<{ open: boolean; editing: string | null }>({ open: false, editing: null });
const formSaving = ref(false);

function blankForm() {
  return {
    name: '',
    description: '',
    persona_id: null as string | null,
    chat_provider_id: null as string | null,
    kb_ids: [] as string[],
    enabled: true,
    sort_order: 0,
    is_default: false,
  };
}

const form = reactive(blankForm());

function openCreateDialog() {
  Object.assign(form, blankForm());
  formDialog.editing = null;
  formDialog.open = true;
  // Inventory may have changed since the last refresh (an operator could
  // have created/deleted a persona, provider, or KB grant in another tab);
  // refresh every time this dialog opens so the picker never offers a
  // stale list -- same precedent as PortalKbVisibilityAdminPage's grant
  // dialog.
  void loadPersonas();
  void loadProviders();
  void loadKbVisibility();
}

function openEditDialog(item: AgentBundleEntry) {
  Object.assign(form, {
    name: item.name,
    description: item.description || '',
    persona_id: item.persona_id,
    chat_provider_id: item.chat_provider_id,
    kb_ids: [...item.kb_ids],
    enabled: item.enabled,
    sort_order: item.sort_order,
    is_default: item.is_default,
  });
  formDialog.editing = item.bundle_id;
  formDialog.open = true;
  void loadPersonas();
  void loadProviders();
  void loadKbVisibility();
}

async function submitForm() {
  const name = form.name.trim();
  if (!name) return;

  const willEnable = form.enabled;
  if (willEnable) {
    const providerLabel = form.chat_provider_id || '（沿用运营方默认提供商）';
    const confirmed = window.confirm(
      `即将保存并${form.enabled ? '启用' : '停用'}套餐「${name}」。\n` +
        `启用后，所有门户终端用户都可以选择它，其对话将被路由到提供商「${providerLabel}」，产生的调用成本由运营方承担。\n\n` +
        '是否继续？',
    );
    if (!confirmed) return;
  }

  formSaving.value = true;
  try {
    const body = {
      name,
      description: form.description || null,
      persona_id: form.persona_id || null,
      chat_provider_id: form.chat_provider_id || null,
      kb_ids: form.kb_ids,
      enabled: form.enabled,
      sort_order: form.sort_order || 0,
      is_default: form.is_default,
    };
    if (formDialog.editing) {
      await apiV1Client.put(`${ADMIN_BASE}/${encodeURIComponent(formDialog.editing)}`, body);
      notify(`已保存套餐「${name}」`);
    } else {
      await apiV1Client.post(ADMIN_BASE, body);
      notify(`已创建套餐「${name}」`);
    }
    formDialog.open = false;
    await loadBundles();
  } catch (error) {
    notify(extractErrorMessage(error, '保存套餐失败'), 'error');
  } finally {
    formSaving.value = false;
  }
}

// ---------------------------------------------------------------------
// Enable / disable toggle (does not delete the row -- mirrors
// PortalKbVisibility's own audit-trail precedent, and this model has no
// admin_delete at all -- see agent_bundle_service.py's own gaps).
// ---------------------------------------------------------------------

async function onToggleEnabled(item: AgentBundleEntry, next: boolean) {
  if (next) {
    const providerLabel = item.chat_provider_id || '（沿用运营方默认提供商）';
    const confirmed = window.confirm(
      `即将启用套餐「${item.name}」向所有门户终端用户开放选择。\n` +
        `选中它的用户，对话会被路由到提供商「${providerLabel}」，产生的调用成本由运营方承担。\n\n是否继续？`,
    );
    if (!confirmed) return;
  } else {
    const confirmed = window.confirm(
      `确定要停用套餐「${item.name}」吗？\n` +
        '停用后，门户用户将无法再选择它；已经绑定它的历史会话保持现状不受影响。该记录不会被删除，可随时重新启用。',
    );
    if (!confirmed) return;
  }

  togglingId.value = item.bundle_id;
  try {
    await apiV1Client.patch(`${ADMIN_BASE}/${encodeURIComponent(item.bundle_id)}/enabled`, {
      enabled: next,
    });
    notify(next ? `已启用「${item.name}」` : `已停用「${item.name}」`);
    await loadBundles();
  } catch (error) {
    notify(extractErrorMessage(error, '切换状态失败'), 'error');
  } finally {
    togglingId.value = null;
  }
}

onMounted(refreshAll);
</script>

<style scoped>
.portal-agent-bundles-page :deep(.v-data-table__td) {
  vertical-align: middle;
}
</style>
