<template>
  <div class="portal-kb-visibility-page">
    <v-container fluid class="pa-0">
      <!-- Consequence banner. Deliberately persistent (no close button) and
           worded as a security control, not a routine feature switch -- see
           this page's assignment: granting visibility here is the single
           thing standing between an internal corpus and every portal end
           user's chat answers. -->
      <v-alert
        type="warning"
        variant="tonal"
        icon="mdi-shield-alert-outline"
        density="comfortable"
        class="mb-4"
        border="start"
      >
        <div class="text-subtitle-2 font-weight-bold mb-1">这是面向公网门户用户的知识库授权表，不是普通开关</div>
        <div class="text-body-2">
          在下方将某个知识库标记为「已授权」后，<strong>所有</strong>门户终端用户都可以在会话中主动选择该知识库，
          其中的文档内容会被检索并直接出现在 AI 给终端用户的回答里。这是防止内部/敏感知识库泄露给外部用户的
          唯一控制点 —— 请像批准一次数据出境一样对待每一次授权，而不是当作界面上的一个普通勾选项。
        </div>
      </v-alert>

      <v-card flat>
        <v-card-title class="d-flex align-center flex-wrap py-3 px-4" style="gap: 8px">
          <span class="text-h5">门户知识库可见性</span>
          <v-chip size="small" class="ml-1">{{ entries.length }} 条记录</v-chip>
          <v-chip v-if="danglingCount > 0" size="small" color="error" variant="tonal" class="ml-1">
            <v-icon start size="14">mdi-alert-circle-outline</v-icon>
            {{ danglingCount }} 条失效
          </v-chip>
          <v-spacer />
          <v-btn
            color="primary"
            prepend-icon="mdi-plus"
            variant="tonal"
            size="small"
            class="mr-2"
            :disabled="loading"
            @click="openGrantDialog"
          >
            授权新知识库
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

        <v-alert
          v-if="loadError"
          type="error"
          variant="tonal"
          density="compact"
          class="ma-4"
        >
          {{ loadError }}
        </v-alert>

        <v-card-text class="pa-0">
          <v-data-table
            :headers="headers"
            :items="entries"
            :loading="loading"
            item-value="kb_id"
            class="elevation-0"
          >
            <template #item.status="{ item }">
              <div class="d-flex flex-column ga-1">
                <v-tooltip location="top" :disabled="!item.dangling || item.enabled">
                  <template #activator="{ props: tooltipProps }">
                    <div v-bind="tooltipProps">
                      <v-switch
                        :model-value="item.enabled"
                        color="warning"
                        density="compact"
                        hide-details
                        inset
                        :loading="togglingKbId === item.kb_id"
                        :disabled="togglingKbId === item.kb_id || (item.dangling && !item.enabled)"
                        @update:model-value="(val) => onToggleEnabled(item, Boolean(val))"
                      />
                    </div>
                  </template>
                  <span>该知识库已从核心中删除，无法重新授权可见性</span>
                </v-tooltip>
                <v-chip v-if="item.dangling" size="x-small" color="error" variant="tonal">
                  <v-icon start size="12">mdi-link-off</v-icon>
                  失效（核心已删除）
                </v-chip>
                <v-chip v-else-if="!item.available" size="x-small" color="grey" variant="tonal">
                  暂不可用
                </v-chip>
              </div>
            </template>

            <template #item.kb="{ item }">
              <div class="py-1">
                <div class="text-body-2 font-weight-medium">
                  {{ item.display_name || item.live_kb_name || item.kb_name }}
                </div>
                <div class="text-caption text-medium-emphasis">
                  核心名称：{{ item.live_kb_name || item.kb_name }}
                </div>
                <div class="text-caption text-medium-emphasis" style="font-family: monospace">
                  {{ item.kb_id }}
                </div>
              </div>
            </template>

            <template #item.description="{ item }">
              <div
                class="text-body-2 text-medium-emphasis"
                style="max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap"
                :title="item.description || ''"
              >
                {{ item.description || '未设置面向用户的描述' }}
              </div>
            </template>

            <template #item.counts="{ item }">
              <div class="text-body-2 text-medium-emphasis">
                {{ item.doc_count ?? '-' }} / {{ item.chunk_count ?? '-' }}
              </div>
            </template>

            <template #item.granted_by="{ item }">
              <span class="text-body-2">{{ item.granted_by || '-' }}</span>
            </template>

            <template #item.updated_at="{ item }">
              <span class="text-body-2 text-medium-emphasis">{{ formatTime(item.updated_at) }}</span>
            </template>

            <template #item.actions="{ item }">
              <v-btn
                size="small"
                variant="tonal"
                color="primary"
                class="mr-1"
                @click="openEditDialog(item)"
              >
                <v-icon>mdi-pencil</v-icon>
                <v-tooltip activator="parent" location="top">编辑面向用户的名称/描述</v-tooltip>
              </v-btn>
            </template>

            <template #no-data>
              <div class="text-center py-8">
                <v-icon size="64" color="grey-400">mdi-book-off-outline</v-icon>
                <div class="text-h6 mt-4 text-grey-600">尚未授权任何知识库</div>
                <div class="text-body-2 text-grey-500">
                  在此之前，门户终端用户看不到任何知识库可供选择。
                </div>
                <v-btn color="primary" variant="tonal" class="mt-4" @click="openGrantDialog">
                  <v-icon start>mdi-plus</v-icon>
                  授权新知识库
                </v-btn>
              </div>
            </template>
          </v-data-table>
        </v-card-text>
      </v-card>
    </v-container>

    <!-- Grant dialog -->
    <v-dialog v-model="grantDialog.open" max-width="620" persistent>
      <v-card>
        <v-card-title class="text-h6">授权新知识库给门户用户</v-card-title>
        <v-card-text>
          <v-alert type="warning" variant="tonal" density="compact" class="mb-4">
            确认后，所有门户终端用户都能立即选择并检索这个知识库的内容。请确保该知识库不包含内部/敏感信息，
            或已明确批准向外部用户开放。
          </v-alert>

          <v-select
            v-model="grantForm.kb_id"
            :items="grantableKbs"
            item-title="label"
            item-value="kb_id"
            label="选择要授权的知识库"
            :loading="loadingCoreKbs"
            :disabled="loadingCoreKbs"
            variant="outlined"
            density="comfortable"
            hint="仅列出核心中当前存在、且尚未处于「已授权」状态的知识库"
            persistent-hint
            class="mb-4"
          >
            <template #no-data>
              <div class="pa-4 text-body-2 text-medium-emphasis">
                没有可供授权的知识库了：要么核心中没有知识库，要么都已被授权。
              </div>
            </template>
          </v-select>

          <v-text-field
            v-model="grantForm.display_name"
            label="面向用户的显示名称（可选，留空则使用知识库原名）"
            variant="outlined"
            density="comfortable"
            maxlength="100"
            counter
            class="mb-2"
          />
          <v-textarea
            v-model="grantForm.description"
            label="面向用户的描述（可选）"
            variant="outlined"
            density="comfortable"
            maxlength="1000"
            counter
            rows="3"
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="grantSaving" @click="grantDialog.open = false">取消</v-btn>
          <v-btn
            color="warning"
            variant="flat"
            :loading="grantSaving"
            :disabled="!grantForm.kb_id"
            @click="submitGrant"
          >
            确认授权并向门户用户公开
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Edit display metadata dialog -->
    <v-dialog v-model="editDialog.open" max-width="560">
      <v-card v-if="editDialog.target">
        <v-card-title class="text-h6">编辑面向用户的展示信息</v-card-title>
        <v-card-text>
          <div class="text-caption text-medium-emphasis mb-3">
            知识库：{{ editDialog.target.live_kb_name || editDialog.target.kb_name }}（{{ editDialog.target.kb_id }}）
          </div>
          <v-text-field
            v-model="editForm.display_name"
            label="面向用户的显示名称"
            variant="outlined"
            density="comfortable"
            maxlength="100"
            counter
            class="mb-2"
          />
          <v-textarea
            v-model="editForm.description"
            label="面向用户的描述"
            variant="outlined"
            density="comfortable"
            maxlength="1000"
            counter
            rows="3"
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="editSaving" @click="editDialog.open = false">取消</v-btn>
          <v-btn color="primary" variant="flat" :loading="editSaving" @click="submitEdit">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snackbar.open" :color="snackbar.color" :timeout="4000">
      {{ snackbar.text }}
    </v-snackbar>
  </div>
</template>

<script setup lang="ts">
// Operator-only admin page for curating the portal's knowledge-base
// visibility whitelist (Phase 3). Lives in the OPERATOR dashboard app
// (dashboard/index.html), gated client-side by MainRoutes.ts's
// `requiresAuth: true` and server-side by every
// `/api/v1/portal/admin/kb-visibility*` route requiring
// `Depends(require_system_scope)` -- any valid operator dashboard JWT
// carries scopes ['*'], so simply being logged into this app is exactly
// the "real operator" gate this page needs, matching every other view
// under dashboard/src/views/ (SessionManagementPage, PersonaPage, ...).
//
// Deliberately reuses the dashboard's OWN api client (`apiV1Client` from
// `@/api/http`, baseURL '/api/v1', Bearer-token-from-localStorage auth via
// its existing interceptors) -- NOT `@/api/portal.ts` (that axios instance
// is the end-user portal SPA's cookie+CSRF client, a completely different
// auth domain that this operator page must never touch). The portal admin
// routes are mounted at `/api/v1/portal/admin/...`, i.e. a plain path
// suffix under the same `/api/v1` prefix `apiV1Client` already targets, so
// no new client/base URL is needed.
//
// i18n note: every other file this page could reuse text from
// (dashboard/src/i18n/locales/**, dashboard/src/i18n/translations.ts) is
// NOT in this assignment's file ownership list, and translations.ts wires
// modules via static imports rather than directory scanning, so adding a
// new module here would require editing a file this assignment does not
// own. Text is therefore hardcoded in Simplified Chinese, matching the
// precedent already set by dashboard/src/components/portal/
// PortalSessionList.vue (a sibling admin-dashboard component that also
// hardcodes Chinese literals rather than wiring into the global i18n
// system). See notes_for_integrator for the follow-up this implies.
import { computed, onMounted, reactive, ref } from 'vue';
import { apiV1Client } from '@/api/http';

interface CoreKb {
  kb_id: string;
  kb_name: string;
  description: string | null;
  emoji: string | null;
  doc_count: number;
  chunk_count: number;
}

interface KbVisibilityEntry {
  kb_id: string;
  kb_name: string;
  display_name: string | null;
  description: string | null;
  enabled: boolean;
  granted_by: string | null;
  created_at: string;
  updated_at: string;
  dangling: boolean;
  live_kb_name: string | null;
  doc_count: number | null;
  chunk_count: number | null;
  available: boolean;
}

const ADMIN_BASE = '/portal/admin/kb-visibility';

const entries = ref<KbVisibilityEntry[]>([]);
const coreKbs = ref<CoreKb[]>([]);
const loading = ref(false);
const loadingCoreKbs = ref(false);
const loadError = ref('');
const togglingKbId = ref<string | null>(null);

const headers = [
  { title: '状态', key: 'status', sortable: false, width: '150px' },
  { title: '知识库', key: 'kb', sortable: false, minWidth: '260px' },
  { title: '面向用户的描述', key: 'description', sortable: false },
  { title: '文档 / 分块', key: 'counts', sortable: false, width: '110px' },
  { title: '授权人', key: 'granted_by', width: '140px' },
  { title: '更新时间', key: 'updated_at', width: '170px' },
  { title: '操作', key: 'actions', sortable: false, width: '90px' },
];

const danglingCount = computed(() => entries.value.filter((e) => e.dangling).length);

// Selectable in the grant dialog: exists live in core right now, and is
// not already an enabled whitelist entry (re-enabling an existing,
// previously-revoked row is still offered here since the same PUT call
// handles both "brand new" and "re-enable" -- see kb_visibility_service
// .admin_grant's own docstring).
const grantableKbs = computed(() => {
  const enabledIds = new Set(entries.value.filter((e) => e.enabled).map((e) => e.kb_id));
  return coreKbs.value
    .filter((kb) => !enabledIds.has(kb.kb_id))
    .map((kb) => ({
      kb_id: kb.kb_id,
      label: `${kb.emoji ? kb.emoji + ' ' : ''}${kb.kb_name}（${kb.doc_count} 篇文档）`,
    }));
});

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

async function loadVisibility() {
  const res = await apiV1Client.get<{ kb_visibility: KbVisibilityEntry[] }>(ADMIN_BASE);
  entries.value = res.data.kb_visibility || [];
}

async function loadCoreKbs() {
  loadingCoreKbs.value = true;
  try {
    const res = await apiV1Client.get<{ core_kbs: CoreKb[] }>(`${ADMIN_BASE}/core-kbs`);
    coreKbs.value = res.data.core_kbs || [];
  } finally {
    loadingCoreKbs.value = false;
  }
}

async function refreshAll() {
  loading.value = true;
  loadError.value = '';
  try {
    await Promise.all([loadVisibility(), loadCoreKbs()]);
  } catch (error) {
    loadError.value = extractErrorMessage(error, '加载知识库可见性列表失败');
  } finally {
    loading.value = false;
  }
}

// ---------------------------------------------------------------------
// Grant dialog
// ---------------------------------------------------------------------

const grantDialog = reactive({ open: false });
const grantForm = reactive({ kb_id: '', display_name: '', description: '' });
const grantSaving = ref(false);

function openGrantDialog() {
  grantForm.kb_id = '';
  grantForm.display_name = '';
  grantForm.description = '';
  grantDialog.open = true;
  // Core KB inventory may have changed since the last refresh (an operator
  // could have created a KB in another tab); refresh it every time this
  // dialog opens so the picker never offers a stale list.
  void loadCoreKbs();
}

async function submitGrant() {
  if (!grantForm.kb_id) return;
  const kb = coreKbs.value.find((k) => k.kb_id === grantForm.kb_id);
  const label = kb?.kb_name || grantForm.kb_id;
  const confirmed = window.confirm(
    `即将授权知识库「${label}」向所有门户终端用户公开可见。\n` +
      '授权后，任何门户用户都可以在自己的会话中选择该知识库，其内容会被检索并出现在回答中。\n' +
      '此操作不会立即撤销的话会一直生效，请确认这不是内部/敏感知识库。\n\n是否继续？',
  );
  if (!confirmed) return;

  grantSaving.value = true;
  try {
    await apiV1Client.put<KbVisibilityEntry>(
      `${ADMIN_BASE}/${encodeURIComponent(grantForm.kb_id)}`,
      {
        display_name: grantForm.display_name || undefined,
        description: grantForm.description || undefined,
      },
    );
    notify(`已授权「${label}」，门户用户现在可以选择该知识库`);
    grantDialog.open = false;
    await refreshAll();
  } catch (error) {
    notify(extractErrorMessage(error, '授权失败'), 'error');
  } finally {
    grantSaving.value = false;
  }
}

// ---------------------------------------------------------------------
// Enable / disable (revoke) toggle
// ---------------------------------------------------------------------

async function onToggleEnabled(item: KbVisibilityEntry, next: boolean) {
  const label = item.display_name || item.live_kb_name || item.kb_name;

  if (next) {
    // Turning an existing (disabled) row back on is the same admin_grant
    // call as a fresh grant -- same consequence, same confirmation bar.
    const confirmed = window.confirm(
      `即将重新授权知识库「${label}」向所有门户终端用户公开可见。\n` +
        '授权后，任何门户用户都可以在自己的会话中选择该知识库并检索其内容。\n\n是否继续？',
    );
    if (!confirmed) return;

    togglingKbId.value = item.kb_id;
    try {
      await apiV1Client.put<KbVisibilityEntry>(
        `${ADMIN_BASE}/${encodeURIComponent(item.kb_id)}`,
        { display_name: item.display_name || undefined, description: item.description || undefined },
      );
      notify(`已重新授权「${label}」`);
      await refreshAll();
    } catch (error) {
      notify(extractErrorMessage(error, '重新授权失败'), 'error');
    } finally {
      togglingKbId.value = null;
    }
    return;
  }

  const confirmed = window.confirm(
    `确定要撤销知识库「${label}」对门户用户的可见性吗？\n` +
      '撤销后，门户用户将无法再选择该知识库；已经绑定它的历史会话在下次检索时会被明确拒绝，' +
      '不会静默改用其他知识库。该记录不会被物理删除，可随时重新授权。',
  );
  if (!confirmed) return;

  togglingKbId.value = item.kb_id;
  try {
    await apiV1Client.delete<KbVisibilityEntry>(`${ADMIN_BASE}/${encodeURIComponent(item.kb_id)}`);
    notify(`已撤销「${label}」的可见性`);
    await refreshAll();
  } catch (error) {
    notify(extractErrorMessage(error, '撤销失败'), 'error');
  } finally {
    togglingKbId.value = null;
  }
}

// ---------------------------------------------------------------------
// Edit display metadata dialog (does not touch enabled/kb_name)
// ---------------------------------------------------------------------

const editDialog = reactive<{ open: boolean; target: KbVisibilityEntry | null }>({
  open: false,
  target: null,
});
const editForm = reactive({ display_name: '', description: '' });
const editSaving = ref(false);

function openEditDialog(item: KbVisibilityEntry) {
  editDialog.target = item;
  editForm.display_name = item.display_name || '';
  editForm.description = item.description || '';
  editDialog.open = true;
}

async function submitEdit() {
  const target = editDialog.target;
  if (!target) return;
  editSaving.value = true;
  try {
    await apiV1Client.patch<KbVisibilityEntry>(
      `${ADMIN_BASE}/${encodeURIComponent(target.kb_id)}`,
      { display_name: editForm.display_name, description: editForm.description },
    );
    notify('已保存展示信息');
    editDialog.open = false;
    await refreshAll();
  } catch (error) {
    notify(extractErrorMessage(error, '保存失败'), 'error');
  } finally {
    editSaving.value = false;
  }
}

onMounted(refreshAll);
</script>

<style scoped>
.portal-kb-visibility-page :deep(.v-data-table__td) {
  vertical-align: middle;
}
</style>
