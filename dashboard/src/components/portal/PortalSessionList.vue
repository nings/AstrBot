<template>
  <div class="portal-session-list">
    <!-- Brand row, then the primary action, then the list -- the operator
         ChatUI's sidebar order. `ChatUILogo` is the shared mark and is not on
         the portal's forbidden-import list. -->
    <div class="rail-brand">
      <ChatUILogo class="rail-logo" />
      <span class="rail-brand-name">AstrBot</span>
    </div>

    <button
      type="button"
      class="rail-action"
      :disabled="creating"
      @click="createSession"
    >
      <v-icon icon="mdi-square-edit-outline" size="18" />
      <span>创建对话</span>
      <v-progress-circular
        v-if="creating"
        class="rail-action-spinner"
        indeterminate
        size="14"
        width="2"
      />
    </button>

    <div class="session-list-header">
      <span class="session-list-title">对话</span>
    </div>

    <div v-if="loadError" class="session-list-error">
      <span>{{ loadError }}</span>
      <v-btn size="x-small" variant="text" @click="refresh">重试</v-btn>
    </div>

    <div v-if="loading && !sessions.length" class="session-list-center">
      <v-progress-circular indeterminate size="24" width="2" />
    </div>

    <div v-else-if="!sessions.length" class="session-list-empty">
      <span>还没有会话</span>
      <v-btn size="small" variant="text" color="primary" @click="createSession">
        新建一个
      </v-btn>
    </div>

    <div v-else class="session-list-items">
      <div
        v-for="session in sessions"
        :key="session.session_id"
        class="session-item"
        :class="{ active: session.session_id === activeSessionId }"
        @click="select(session.session_id)"
      >
        <template v-if="renamingId === session.session_id">
          <input
            ref="renameInputRef"
            v-model="renameDraft"
            class="session-rename-input"
            @click.stop
            @keydown.enter="commitRename(session.session_id)"
            @keydown.esc="cancelRename"
            @blur="commitRename(session.session_id)"
          />
        </template>
        <template v-else>
          <span class="session-title" :title="sessionLabel(session)">
            {{ sessionLabel(session) }}
          </span>
          <span class="session-actions">
            <v-btn
              icon="mdi-pencil-outline"
              size="x-small"
              variant="text"
              @click.stop="startRename(session)"
            />
            <v-btn
              icon="mdi-delete-outline"
              size="x-small"
              variant="text"
              @click.stop="confirmDelete(session)"
            />
          </span>
        </template>
      </div>

      <div v-if="hasMore" class="session-list-more">
        <v-btn size="small" variant="text" :loading="loadingMore" @click="loadMore">
          加载更多
        </v-btn>
      </div>
    </div>

    <v-dialog v-model="deleteDialog.open" max-width="360">
      <v-card>
        <v-card-title>删除会话</v-card-title>
        <v-card-text>
          确定要删除会话「{{ deleteDialog.label }}」吗？此操作无法撤销。
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="deleteDialog.open = false">取消</v-btn>
          <v-btn color="error" variant="flat" :loading="deleting" @click="doDelete">
            删除
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script setup lang="ts">
// Session list/create/rename/delete against the portal module's own
// `/api/v1/portal/chat/sessions*` endpoints (astrbot/portal/api/chat_routes.py).
//
// This component owns its own tiny fetch/CSRF helper rather than importing
// `@/api/http`'s `fetchWithAuth`/`apiV1Client`: those attach the operator
// dashboard's `Authorization: Bearer <token>` header from `localStorage`,
// which is the wrong auth mechanism entirely for portal end users (who are
// authenticated via the httpOnly `astrbot_portal_jwt` cookie + CSRF
// double-submit, never a bearer token). No shared portal API-client module
// exists in this phase's file ownership, so the same small helper is
// duplicated here and in `PortalChatShell.vue`.
import { computed, nextTick, onMounted, reactive, ref } from "vue";
// The shared AstrBot mark. Importable here: the portal's ESLint boundary
// forbids the operator *chat surface* components (Chat.vue, ChatMessageList,
// ConfigSelector, ProviderModelMenu, CommandSuggestion, useSessions), none of
// which this is -- it is a standalone inline SVG with no operator assumptions.
import ChatUILogo from "@/components/chat/ChatUILogo.vue";

interface PortalSession {
  session_id: string;
  platform_id: string;
  creator: string;
  display_name: string | null;
  is_group: boolean;
  created_at: string;
  updated_at: string;
}

defineProps<{
  activeSessionId?: string | null;
}>();

const emit = defineEmits<{
  select: [sessionId: string, displayName: string | null];
  created: [sessionId: string];
  deleted: [sessionId: string];
  renamed: [sessionId: string, displayName: string];
}>();

const PORTAL_API_BASE = "/api/v1/portal";
const CSRF_COOKIE_NAME = "astrbot_portal_csrf";
const CSRF_HEADER_NAME = "X-Portal-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);

function readCookie(name: string): string | null {
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}=([^;]*)`),
  );
  return match ? decodeURIComponent(match[1]) : null;
}

async function rawPortalFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const method = (init.method || "GET").toUpperCase();
  const headers = new Headers(init.headers || {});
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!SAFE_METHODS.has(method)) {
    const csrf = readCookie(CSRF_COOKIE_NAME);
    if (csrf) headers.set(CSRF_HEADER_NAME, csrf);
  }
  return fetch(`${PORTAL_API_BASE}${path}`, {
    ...init,
    method,
    headers,
    credentials: "include",
  });
}

// The 15-minute access-JWT is very plausibly expired mid-browsing-session,
// well before the user actively sends a chat message. Transparently attempt
// exactly one silent refresh (POST /auth/refresh, which rotates the
// httpOnly refresh cookie) and retry the original request once before
// surfacing a 401 to the UI as a real error.
let refreshInFlight: Promise<boolean> | null = null;

async function tryRefreshSession(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = rawPortalFetch("/auth/refresh", { method: "POST" })
      .then((response) => response.ok)
      .catch(() => false)
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

async function portalFetch(
  path: string,
  init: RequestInit = {},
  _retried = false,
): Promise<Response> {
  const response = await rawPortalFetch(path, init);
  if (response.status === 401 && !_retried && path !== "/auth/refresh") {
    const refreshed = await tryRefreshSession();
    if (refreshed) return portalFetch(path, init, true);
  }
  return response;
}

const sessions = ref<PortalSession[]>([]);
const loading = ref(false);
const loadingMore = ref(false);
const loadError = ref("");
const creating = ref(false);
const deleting = ref(false);
const page = ref(1);
const pageSize = 50;
const totalPages = ref(1);
const hasMore = computed(() => page.value < totalPages.value);

const renamingId = ref<string | null>(null);
const renameDraft = ref("");
const renameInputRef = ref<HTMLInputElement[] | HTMLInputElement | null>(null);

const deleteDialog = reactive<{ open: boolean; id: string; label: string }>({
  open: false,
  id: "",
  label: "",
});

function sessionLabel(session: PortalSession) {
  return session.display_name?.trim() || `会话 ${session.session_id.slice(0, 8)}`;
}

async function fetchPage(targetPage: number) {
  const response = await portalFetch(
    `/chat/sessions?page=${targetPage}&page_size=${pageSize}`,
  );
  if (!response.ok) {
    throw new Error(`加载会话列表失败（${response.status}）`);
  }
  return (await response.json()) as {
    sessions: PortalSession[];
    pagination: { page: number; page_size: number; total: number; total_pages: number };
  };
}

async function refresh() {
  loading.value = true;
  loadError.value = "";
  try {
    const data = await fetchPage(1);
    sessions.value = data.sessions;
    page.value = data.pagination.page;
    totalPages.value = Math.max(data.pagination.total_pages, 1);
  } catch (error) {
    loadError.value = (error as Error).message || "加载会话列表失败";
  } finally {
    loading.value = false;
  }
}

async function loadMore() {
  if (!hasMore.value || loadingMore.value) return;
  loadingMore.value = true;
  try {
    const data = await fetchPage(page.value + 1);
    sessions.value = [...sessions.value, ...data.sessions];
    page.value = data.pagination.page;
    totalPages.value = Math.max(data.pagination.total_pages, 1);
  } catch (error) {
    loadError.value = (error as Error).message || "加载更多会话失败";
  } finally {
    loadingMore.value = false;
  }
}

async function createSession() {
  if (creating.value) return;
  creating.value = true;
  try {
    const response = await portalFetch("/chat/sessions", { method: "POST" });
    if (!response.ok) {
      throw new Error(`创建会话失败（${response.status}）`);
    }
    const created = (await response.json()) as {
      session_id: string;
      platform_id: string;
    };
    const now = new Date().toISOString();
    sessions.value = [
      {
        session_id: created.session_id,
        platform_id: created.platform_id,
        creator: "",
        display_name: null,
        is_group: false,
        created_at: now,
        updated_at: now,
      },
      ...sessions.value,
    ];
    emit("created", created.session_id);
    emit("select", created.session_id, null);
    void refresh();
  } catch (error) {
    loadError.value = (error as Error).message || "创建会话失败";
  } finally {
    creating.value = false;
  }
}

function select(sessionId: string) {
  if (renamingId.value) return;
  const target = sessions.value.find((s) => s.session_id === sessionId);
  emit("select", sessionId, target?.display_name ?? null);
}

function startRename(session: PortalSession) {
  renamingId.value = session.session_id;
  renameDraft.value = session.display_name || "";
  nextTick(() => {
    const el = Array.isArray(renameInputRef.value)
      ? renameInputRef.value[0]
      : renameInputRef.value;
    el?.focus();
    el?.select();
  });
}

function cancelRename() {
  renamingId.value = null;
  renameDraft.value = "";
}

async function commitRename(sessionId: string) {
  if (renamingId.value !== sessionId) return;
  const displayName = renameDraft.value.trim();
  renamingId.value = null;
  if (!displayName) return;

  const target = sessions.value.find((s) => s.session_id === sessionId);
  if (target && target.display_name === displayName) return;

  try {
    const response = await portalFetch(`/chat/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      body: JSON.stringify({ display_name: displayName }),
    });
    if (!response.ok) {
      throw new Error(`重命名失败（${response.status}）`);
    }
    if (target) target.display_name = displayName;
    emit("renamed", sessionId, displayName);
  } catch (error) {
    loadError.value = (error as Error).message || "重命名失败";
  }
}

function confirmDelete(session: PortalSession) {
  deleteDialog.open = true;
  deleteDialog.id = session.session_id;
  deleteDialog.label = sessionLabel(session);
}

async function doDelete() {
  if (!deleteDialog.id || deleting.value) return;
  deleting.value = true;
  const sessionId = deleteDialog.id;
  try {
    const response = await portalFetch(`/chat/sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    });
    if (!response.ok && response.status !== 204) {
      throw new Error(`删除失败（${response.status}）`);
    }
    sessions.value = sessions.value.filter((s) => s.session_id !== sessionId);
    deleteDialog.open = false;
    emit("deleted", sessionId);
  } catch (error) {
    loadError.value = (error as Error).message || "删除会话失败";
  } finally {
    deleting.value = false;
  }
}

onMounted(refresh);

defineExpose({ refresh });
</script>

<style scoped>
.portal-session-list {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  color: rgb(var(--v-theme-on-surface));
}

.rail-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  padding: 14px 14px 10px;
}

.rail-logo {
  width: 22px;
  height: 22px;
  flex-shrink: 0;
}

.rail-brand-name {
  font-family: "Outfit", "Noto Sans", sans-serif;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: -0.01em;
}

/* A quiet full-width row rather than a filled button: the primary action in
   the operator sidebar reads as navigation, not as a call to action. */
.rail-action {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
  width: calc(100% - 12px);
  margin: 0 6px 10px;
  padding: 8px 10px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: rgb(var(--v-theme-on-surface));
  font: inherit;
  font-size: 14px;
  text-align: left;
  cursor: pointer;
}

.rail-action:hover:not(:disabled) {
  background: rgba(var(--v-theme-on-surface), 0.06);
}

.rail-action:disabled {
  cursor: default;
  opacity: 0.6;
}

.rail-action-spinner {
  margin-left: auto;
}

.session-list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 16px 4px;
  flex-shrink: 0;
}

/* Sentence case at label scale, using the shared section-label token -- the
   uppercase/letterspaced treatment this replaced belonged to a different
   design language than the rest of the product. */
.session-list-title {
  font-size: 12px;
  font-weight: 500;
  color: var(--chat-section-label, rgba(var(--v-theme-on-surface), 0.48));
}

.session-list-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 0 10px 6px;
  padding: 6px 10px;
  border-radius: 8px;
  background: rgba(var(--v-theme-error), 0.1);
  color: rgb(var(--v-theme-error));
  font-size: 12px;
}

.session-list-center,
.session-list-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 24px 12px;
  color: rgba(var(--v-theme-on-surface), 0.55);
  font-size: 13px;
}

.session-list-items {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 0 6px 8px;
}

.session-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 9px 10px;
  border-radius: 10px;
  cursor: pointer;
  color: rgba(var(--v-theme-on-surface), 0.82);
}

.session-item:hover {
  background: rgba(var(--v-theme-on-surface), 0.06);
}

/* Neutral selection, matching the operator sidebar. A primary-tinted, primary-
   coloured row competes with the conversation for attention; which item is
   selected is positional information, not an accent. */
.session-item.active {
  background: var(--chat-session-active-bg, rgba(var(--v-theme-on-surface), 0.08));
  color: rgb(var(--v-theme-on-surface));
  font-weight: 500;
}

.session-title {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13.5px;
}

.session-rename-input {
  flex: 1 1 auto;
  min-width: 0;
  border: 1px solid rgba(var(--v-theme-primary), 0.5);
  border-radius: 6px;
  padding: 3px 6px;
  background: rgb(var(--v-theme-surface));
  color: rgb(var(--v-theme-on-surface));
  font: inherit;
  font-size: 13.5px;
}

.session-actions {
  display: flex;
  flex-shrink: 0;
  gap: 2px;
  opacity: 0;
  transition: opacity 0.12s ease;
}

.session-item:hover .session-actions,
.session-item.active .session-actions {
  opacity: 1;
}

.session-list-more {
  display: flex;
  justify-content: center;
  padding: 4px 0 8px;
}
</style>
