<template>
  <div class="portal-users-page">
    <!-- Consequence banner. Deliberately persistent (no close button) and
         worded around the one fact that makes this page different from a
         normal user-admin screen: there is no mail infrastructure in this
         repository, so an invite link's confidentiality between "issued"
         and "in the invitee's hands" is a 100% human/process control, not
         a technical one. See this page's assignment. -->
    <v-alert
      type="warning"
      variant="tonal"
      icon="mdi-link-lock"
      density="comfortable"
      class="mb-4"
      border="start"
    >
      <div class="text-subtitle-2 font-weight-bold mb-1">邀请链接只会显示这一次，之后任何人都无法再次找回</div>
      <div class="text-body-2">
        本系统没有邮件发送能力，创建邀请后生成的一次性链接必须由你手动、安全地交给受邀人（私信、工单、线下口头
        等任何你信任的渠道）—— 这是唯一的投递方式，也是链接保密性的唯一保障。链接只会在创建成功的那一刻显示
        一次；关闭弹窗后，服务端只保留其哈希值，包括你自己在内都无法再次查看明文，只能撤销后重新邀请。
      </div>
    </v-alert>

    <!-- ===================================================================
         Invite management
         =================================================================== -->
    <v-card flat class="mb-6">
      <v-card-title class="d-flex align-center flex-wrap py-3 px-4" style="gap: 8px">
        <span class="text-h5">门户邀请</span>
        <v-chip size="small" class="ml-1">{{ invites.length }} 条记录</v-chip>
        <v-chip v-if="pendingInviteCount > 0" size="small" color="info" variant="tonal" class="ml-1">
          {{ pendingInviteCount }} 条待兑换
        </v-chip>
        <v-spacer />
        <v-btn
          color="primary"
          prepend-icon="mdi-account-plus-outline"
          variant="tonal"
          size="small"
          class="mr-2"
          :disabled="loadingInvites"
          @click="openCreateDialog()"
        >
          创建邀请
        </v-btn>
        <v-btn
          color="default"
          prepend-icon="mdi-refresh"
          variant="tonal"
          size="small"
          :loading="loadingInvites"
          @click="loadInvites"
        >
          刷新
        </v-btn>
      </v-card-title>

      <v-divider></v-divider>

      <v-alert v-if="inviteLoadError" type="error" variant="tonal" density="compact" class="ma-4">
        {{ inviteLoadError }}
      </v-alert>

      <v-card-text class="pa-0">
        <v-data-table
          :headers="inviteHeaders"
          :items="invites"
          :loading="loadingInvites"
          item-value="invite_id"
          class="elevation-0"
        >
          <template #item.status="{ item }">
            <v-chip size="small" :color="inviteStatusColor(item.status)" variant="tonal">
              {{ inviteStatusLabel(item.status) }}
            </v-chip>
          </template>

          <template #item.target="{ item }">
            <div class="py-1">
              <div class="text-body-2 font-weight-medium">{{ item.target_email }}</div>
              <div class="text-caption text-medium-emphasis" style="font-family: monospace">
                {{ item.invite_id }}
              </div>
            </div>
          </template>

          <template #item.created="{ item }">
            <div class="text-body-2 text-medium-emphasis">{{ item.created_by || '-' }}</div>
            <div class="text-caption text-medium-emphasis">{{ formatTime(item.created_at) }}</div>
          </template>

          <template #item.expires_at="{ item }">
            <span class="text-body-2 text-medium-emphasis">{{ formatTime(item.expires_at) }}</span>
          </template>

          <template #item.resolution="{ item }">
            <div v-if="item.status === 'consumed'" class="text-body-2 text-medium-emphasis">
              已于 {{ formatTime(item.consumed_at) }} 兑换
            </div>
            <div v-else-if="item.status === 'revoked'" class="text-body-2 text-medium-emphasis">
              由 {{ item.revoked_by || '-' }} 于 {{ formatTime(item.revoked_at) }} 撤销
            </div>
            <span v-else class="text-body-2 text-medium-emphasis">-</span>
          </template>

          <template #item.actions="{ item }">
            <v-btn
              v-if="item.status === 'pending'"
              size="small"
              variant="tonal"
              color="error"
              :loading="revokingId === item.invite_id"
              @click="onRevokeInvite(item)"
            >
              撤销
              <v-tooltip activator="parent" location="top">撤销后该链接立即失效，受邀人无法再用它激活账户</v-tooltip>
            </v-btn>
            <span v-else class="text-caption text-medium-emphasis">-</span>
          </template>

          <template #no-data>
            <div class="text-center py-8">
              <v-icon size="64" color="grey-400">mdi-email-off-outline</v-icon>
              <div class="text-h6 mt-4 text-grey-600">尚未创建任何邀请</div>
              <div class="text-body-2 text-grey-500">通过邀请，运营方可以为指定邮箱开通门户账户，无需自助注册。</div>
              <v-btn color="primary" variant="tonal" class="mt-4" @click="openCreateDialog()">
                <v-icon start>mdi-account-plus-outline</v-icon>
                创建邀请
              </v-btn>
            </div>
          </template>
        </v-data-table>
      </v-card-text>
    </v-card>

    <!-- ===================================================================
         Portal user management
         =================================================================== -->
    <v-card flat>
      <v-card-title class="d-flex align-center flex-wrap py-3 px-4" style="gap: 8px">
        <span class="text-h5">门户用户</span>
        <v-chip size="small" class="ml-1">{{ users.length }} 条记录</v-chip>
        <v-spacer />
        <v-btn
          color="default"
          prepend-icon="mdi-refresh"
          variant="tonal"
          size="small"
          :loading="loadingUsers"
          @click="loadUsers"
        >
          刷新
        </v-btn>
      </v-card-title>

      <v-divider></v-divider>

      <v-alert v-if="userLoadError" type="error" variant="tonal" density="compact" class="ma-4">
        {{ userLoadError }}
      </v-alert>

      <v-card-text class="pa-0">
        <v-data-table
          :headers="userHeaders"
          :items="users"
          :loading="loadingUsers"
          item-value="user_id"
          class="elevation-0"
        >
          <template #item.status="{ item }">
            <v-chip size="small" :color="userStatusColor(item.status)" variant="tonal">
              {{ userStatusLabel(item.status) }}
            </v-chip>
          </template>

          <template #item.user="{ item }">
            <div class="py-1">
              <div class="text-body-2 font-weight-medium">{{ item.display_name }}</div>
              <div class="text-caption text-medium-emphasis">{{ item.email }}</div>
              <div class="text-caption text-medium-emphasis" style="font-family: monospace">
                {{ item.user_id }}
              </div>
            </div>
          </template>

          <template #item.auth_method="{ item }">
            <span class="text-body-2">{{ authMethodLabel(item.auth_method) }}</span>
          </template>

          <template #item.role="{ item }">
            <span class="text-body-2">{{ item.role }}</span>
          </template>

          <template #item.updated_at="{ item }">
            <span class="text-body-2 text-medium-emphasis">{{ formatTime(item.updated_at) }}</span>
          </template>

          <template #item.actions="{ item }">
            <v-btn
              v-if="item.status === 'active'"
              size="small"
              variant="tonal"
              color="error"
              :loading="togglingUserId === item.user_id"
              @click="onDisableUser(item)"
            >
              禁用
            </v-btn>
            <v-btn
              v-else-if="item.status === 'disabled'"
              size="small"
              variant="tonal"
              color="success"
              :loading="togglingUserId === item.user_id"
              @click="onEnableUser(item)"
            >
              重新启用
            </v-btn>
            <v-tooltip v-else location="top">
              <template #activator="{ props: tooltipProps }">
                <v-btn v-bind="tooltipProps" size="small" variant="tonal" color="default" disabled>
                  重新启用
                </v-btn>
              </template>
              <span>该账户尚未兑换邀请，无法直接启用；请改为「创建邀请」重新向此邮箱发起一次邀请</span>
            </v-tooltip>
            <v-btn
              v-if="item.status === 'invited'"
              size="small"
              variant="text"
              color="primary"
              class="ml-1"
              @click="openCreateDialog(item.email, item.display_name)"
            >
              重新邀请
            </v-btn>
          </template>

          <template #no-data>
            <div class="text-center py-8">
              <v-icon size="64" color="grey-400">mdi-account-off-outline</v-icon>
              <div class="text-h6 mt-4 text-grey-600">尚无任何门户用户</div>
              <div class="text-body-2 text-grey-500">通过上方「创建邀请」或开放自助注册后，用户列表会显示在这里。</div>
            </div>
          </template>
        </v-data-table>
      </v-card-text>
    </v-card>

    <!-- ===================================================================
         Create invite dialog
         =================================================================== -->
    <v-dialog v-model="createDialog.open" max-width="560" persistent>
      <v-card>
        <v-card-title class="text-h6">创建门户邀请</v-card-title>
        <v-card-text>
          <v-alert type="info" variant="tonal" density="compact" class="mb-4">
            提交后会立即生成一次性邀请链接 —— 只会显示这一次，请在下一步弹窗中及时复制并妥善转交给受邀人。
          </v-alert>
          <v-text-field
            v-model="createForm.email"
            label="受邀人邮箱"
            type="email"
            variant="outlined"
            density="comfortable"
            maxlength="255"
            autofocus
            class="mb-2"
          />
          <v-text-field
            v-model="createForm.display_name"
            label="显示名称（可选，留空则使用邮箱前缀作为占位名）"
            variant="outlined"
            density="comfortable"
            maxlength="100"
            hint="受邀人激活账户时可以自行修改"
            persistent-hint
          />
          <div class="text-caption text-medium-emphasis mt-3">
            若该邮箱已存在一个「待激活」（尚未兑换邀请）的账户，提交会为同一账户重新生成一条新邀请，旧链接随之
            失效；若邮箱已属于一个已激活或已禁用的账户，则会被拒绝。
          </div>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" :disabled="creatingInvite" @click="createDialog.open = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="creatingInvite"
            :disabled="!isValidEmail(createForm.email)"
            @click="submitCreateInvite"
          >
            创建邀请
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- ===================================================================
         One-time link reveal dialog. This is the page's single most
         important interaction: the plaintext token/link is never
         retrievable again after this dialog closes (server only keeps a
         PBKDF2 hash from here on). Deliberately NOT dismissible by
         clicking outside or pressing Escape (persistent + no activator
         click-close), and the close button stays disabled until the
         operator explicitly acknowledges they have copied and will
         deliver the link out-of-band themselves.
         =================================================================== -->
    <v-dialog v-model="revealDialog.open" max-width="640" persistent>
      <v-card v-if="revealDialog.entry" class="reveal-card">
        <v-card-title class="d-flex align-center ga-2 text-h6">
          <v-icon color="error">mdi-alert-octagon-outline</v-icon>
          此链接现在是唯一一次显示的机会
        </v-card-title>
        <v-card-text>
          <v-alert type="error" variant="tonal" density="comfortable" class="mb-4">
            <div class="text-subtitle-2 font-weight-bold mb-1">关闭这个弹窗之后，任何人（包括你自己）都无法再次看到这个链接</div>
            <div class="text-body-2">
              服务端从下一刻起只保存它的哈希值，不保留明文。你必须现在就把下面的链接安全地交给
              <strong>{{ revealDialog.entry.target_email }}</strong
              >—— 本系统不发送邮件，这一步完全依赖你自己选择的可信渠道（私信、工单、当面告知等）。如果链接被
              发错人或者中途遗失，唯一的补救办法是回到列表里撤销这条邀请、再创建一条新的。
            </div>
          </v-alert>

          <div class="text-caption text-medium-emphasis mb-1">邀请链接（推荐直接复制这一整条）</div>
          <v-text-field
            :model-value="revealLinkAbsolute"
            readonly
            variant="outlined"
            density="comfortable"
            class="mb-2 mono-field"
            append-inner-icon="mdi-content-copy"
            @click:append-inner="copyText(revealLinkAbsolute, '邀请链接')"
          />

          <v-expansion-panels variant="accordion" class="mb-2">
            <v-expansion-panel title="仅需要原始邀请令牌？（高级）">
              <v-expansion-panel-text>
                <v-text-field
                  :model-value="revealDialog.token"
                  readonly
                  variant="outlined"
                  density="comfortable"
                  class="mono-field"
                  append-inner-icon="mdi-content-copy"
                  @click:append-inner="copyText(revealDialog.token, '邀请令牌')"
                />
              </v-expansion-panel-text>
            </v-expansion-panel>
          </v-expansion-panels>

          <div class="text-caption text-medium-emphasis mt-2">
            过期时间：{{ formatTime(revealDialog.entry.expires_at) }}。过期或撤销之前，该链接可以被兑换一次。
          </div>

          <v-checkbox
            v-model="revealDialog.ack"
            density="comfortable"
            hide-details
            class="mt-3"
            color="error"
          >
            <template #label>
              <span class="text-body-2">
                我已经复制此链接，并会通过安全的渠道亲自转交给受邀人；我理解关闭后无法再次找回它。
              </span>
            </template>
          </v-checkbox>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn color="error" variant="flat" :disabled="!revealDialog.ack" @click="closeRevealDialog">
            我已妥善保存，关闭
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
// Operator-only admin page for provisioning and managing portal end-user
// accounts (Phase 5): issuing/revoking invites and disabling/re-enabling
// portal users. Lives in the OPERATOR dashboard app (dashboard/index.html),
// gated client-side by MainRoutes.ts's `requiresAuth: true` and server-side
// by every `/api/v1/portal/admin/{invites,users}*` route requiring
// `Depends(require_system_scope)` -- deliberately built to look and behave
// like PortalKbVisibilityAdminPage.vue (Phase 3) and
// PortalAgentBundlesAdminPage.vue (Phase 4): same banner-then-card layout,
// same confirm-before-consequential-action pattern, same api client, so an
// operator moving between all three pages sees one coherent admin surface
// rather than three differently-behaving screens.
//
// Reuses the dashboard's OWN api client (`apiV1Client` from `@/api/http`,
// baseURL '/api/v1', Bearer-token-from-localStorage auth via its existing
// interceptors) -- NOT `@/api/portal.ts` (the end-user portal SPA's
// cookie+CSRF client, a completely different auth domain this operator page
// must never touch).
//
// Security-critical UI contract for this page (see astrbot/portal/api/
// admin_routes.py's create_invite docstring, the authoritative source):
//   - POST .../admin/invites returns invite_token/invite_link exactly ONCE,
//     in that single response. There is no GET that can ever return them
//     again -- the server only stores a PBKDF2 hash from then on. This page
//     must never attempt to cache, re-fetch, or re-display that value after
//     the reveal dialog closes, and GET .../admin/invites's list rows never
//     carry a token/token_hash field at all (enforced server-side).
//   - Disabling a user is presented as consequential, not a routine toggle:
//     the confirm copy states the actual mechanics (every active session
//     killed immediately, including an already-issued still-unexpired
//     access token, and the refresh cookie can no longer mint a new one),
//     not just "user disabled".
//
// i18n note: same precedent as the Phase 3/4 sibling pages -- text is
// hardcoded in Simplified Chinese rather than wired into the global i18n
// system, since translations.ts (statically-imported module list) is not in
// this assignment's file ownership.
import { computed, onMounted, reactive, ref } from 'vue';
import { apiV1Client } from '@/api/http';

const INVITES_BASE = '/portal/admin/invites';
const USERS_BASE = '/portal/admin/users';

type InviteStatus = 'pending' | 'consumed' | 'revoked' | 'expired';
type PortalUserStatus = 'active' | 'invited' | 'disabled';
type PortalAuthMethod = 'local' | 'invited' | 'oauth';

interface InviteEntry {
  invite_id: string;
  user_id: string;
  target_email: string;
  created_by: string | null;
  created_at: string;
  expires_at: string;
  consumed_at: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
  status: InviteStatus;
}

interface PortalUserEntry {
  user_id: string;
  email: string;
  display_name: string;
  role: string;
  status: PortalUserStatus;
  auth_method: PortalAuthMethod;
  created_at: string;
  updated_at: string;
}

// Shape of a fresh POST .../admin/invites response: every InviteEntry field
// PLUS the one-time-only invite_token/invite_link pair. Kept as a separate
// type (rather than widening InviteEntry) so it is structurally obvious
// this shape only ever comes from the create response, never from the list.
interface CreateInviteResponse extends InviteEntry {
  invite_token: string;
  invite_link: string;
}

const invites = ref<InviteEntry[]>([]);
const users = ref<PortalUserEntry[]>([]);

const loadingInvites = ref(false);
const loadingUsers = ref(false);
const inviteLoadError = ref('');
const userLoadError = ref('');
const revokingId = ref<string | null>(null);
const togglingUserId = ref<string | null>(null);

const inviteHeaders = [
  { title: '状态', key: 'status', sortable: false, width: '110px' },
  { title: '受邀邮箱', key: 'target', sortable: false, minWidth: '220px' },
  { title: '创建人 / 时间', key: 'created', sortable: false, width: '190px' },
  { title: '过期时间', key: 'expires_at', sortable: false, width: '170px' },
  { title: '结果', key: 'resolution', sortable: false, minWidth: '200px' },
  { title: '操作', key: 'actions', sortable: false, width: '100px' },
];

const userHeaders = [
  { title: '状态', key: 'status', sortable: false, width: '110px' },
  { title: '用户', key: 'user', sortable: false, minWidth: '240px' },
  { title: '认证方式', key: 'auth_method', sortable: false, width: '130px' },
  { title: '角色', key: 'role', sortable: false, width: '90px' },
  { title: '更新时间', key: 'updated_at', sortable: false, width: '170px' },
  { title: '操作', key: 'actions', sortable: false, width: '190px' },
];

const pendingInviteCount = computed(() => invites.value.filter((i) => i.status === 'pending').length);

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

function formatTime(iso: string | null): string {
  if (!iso) return '-';
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function isValidEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());
}

async function copyText(text: string, label: string) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      // Fallback for non-secure contexts (e.g. plain-http LAN deployments)
      // where the async Clipboard API is unavailable.
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
    }
    notify(`已复制${label}`);
  } catch {
    notify(`复制失败，请手动选中并复制${label}`, 'error');
  }
}

// ---------------------------------------------------------------------
// Status/label mapping
// ---------------------------------------------------------------------

function inviteStatusColor(status: InviteStatus): string {
  switch (status) {
    case 'pending':
      return 'info';
    case 'consumed':
      return 'success';
    case 'revoked':
      return 'default';
    case 'expired':
      return 'warning';
    default:
      return 'default';
  }
}

function inviteStatusLabel(status: InviteStatus): string {
  switch (status) {
    case 'pending':
      return '待兑换';
    case 'consumed':
      return '已兑换';
    case 'revoked':
      return '已撤销';
    case 'expired':
      return '已过期';
    default:
      return status;
  }
}

function userStatusColor(status: PortalUserStatus): string {
  switch (status) {
    case 'active':
      return 'success';
    case 'invited':
      return 'info';
    case 'disabled':
      return 'error';
    default:
      return 'default';
  }
}

function userStatusLabel(status: PortalUserStatus): string {
  switch (status) {
    case 'active':
      return '已激活';
    case 'invited':
      return '待激活';
    case 'disabled':
      return '已禁用';
    default:
      return status;
  }
}

function authMethodLabel(method: PortalAuthMethod): string {
  switch (method) {
    case 'local':
      return '本地密码';
    case 'invited':
      return '邀请注册';
    case 'oauth':
      return '单点登录（SSO）';
    default:
      return method;
  }
}

// ---------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------

async function loadInvites() {
  loadingInvites.value = true;
  inviteLoadError.value = '';
  try {
    const res = await apiV1Client.get<{ invites: InviteEntry[] }>(INVITES_BASE);
    invites.value = res.data.invites || [];
  } catch (error) {
    inviteLoadError.value = extractErrorMessage(error, '加载邀请列表失败');
  } finally {
    loadingInvites.value = false;
  }
}

async function loadUsers() {
  loadingUsers.value = true;
  userLoadError.value = '';
  try {
    const res = await apiV1Client.get<{ users: PortalUserEntry[] }>(USERS_BASE);
    users.value = res.data.users || [];
  } catch (error) {
    userLoadError.value = extractErrorMessage(error, '加载门户用户列表失败');
  } finally {
    loadingUsers.value = false;
  }
}

async function refreshAll() {
  await Promise.all([loadInvites(), loadUsers()]);
}

// ---------------------------------------------------------------------
// Create invite dialog + one-time reveal dialog
// ---------------------------------------------------------------------

const createDialog = reactive({ open: false });
const createForm = reactive({ email: '', display_name: '' });
const creatingInvite = ref(false);

function openCreateDialog(prefillEmail = '', prefillDisplayName = '') {
  createForm.email = prefillEmail;
  createForm.display_name = prefillDisplayName;
  createDialog.open = true;
}

const revealDialog = reactive<{
  open: boolean;
  ack: boolean;
  entry: InviteEntry | null;
  token: string;
  link: string;
}>({
  open: false,
  ack: false,
  entry: null,
  token: '',
  link: '',
});

// The invite_link the backend returns is an app-relative path (e.g.
// "/portal/invite/accept?token=..."), not a full URL an operator can just
// paste into a chat message and expect to work for the recipient -- resolve
// it against this page's own origin, since the portal SPA is served from
// the same origin per this phase's ground truth (served at /portal).
const revealLinkAbsolute = computed(() => {
  if (!revealDialog.link) return '';
  try {
    return new URL(revealDialog.link, window.location.origin).toString();
  } catch {
    return revealDialog.link;
  }
});

async function submitCreateInvite() {
  const email = createForm.email.trim();
  if (!isValidEmail(email)) return;

  creatingInvite.value = true;
  try {
    const res = await apiV1Client.post<CreateInviteResponse>(INVITES_BASE, {
      email,
      display_name: createForm.display_name.trim() || undefined,
    });
    const { invite_token, invite_link, ...entry } = res.data;
    createDialog.open = false;
    revealDialog.entry = entry;
    revealDialog.token = invite_token;
    revealDialog.link = invite_link;
    revealDialog.ack = false;
    revealDialog.open = true;
    await loadInvites();
  } catch (error) {
    notify(extractErrorMessage(error, '创建邀请失败'), 'error');
  } finally {
    creatingInvite.value = false;
  }
}

function closeRevealDialog() {
  if (!revealDialog.ack) return;
  revealDialog.open = false;
  // Deliberately drop the plaintext token/link from memory once
  // acknowledged-closed -- nothing on this page holds onto it any longer
  // than the reveal dialog itself needs it.
  revealDialog.entry = null;
  revealDialog.token = '';
  revealDialog.link = '';
}

// ---------------------------------------------------------------------
// Revoke invite
// ---------------------------------------------------------------------

async function onRevokeInvite(item: InviteEntry) {
  const confirmed = window.confirm(
    `确定要撤销对「${item.target_email}」的邀请吗？\n` +
      '撤销后，此前发出的链接立即失效，受邀人再打开它只会看到"邀请已失效"，无法用它激活账户。' +
      '如需重新邀请，可再次创建一条新邀请。',
  );
  if (!confirmed) return;

  revokingId.value = item.invite_id;
  try {
    await apiV1Client.post(`${INVITES_BASE}/${encodeURIComponent(item.invite_id)}/revoke`);
    notify(`已撤销对「${item.target_email}」的邀请`);
    await loadInvites();
  } catch (error) {
    notify(extractErrorMessage(error, '撤销邀请失败'), 'error');
  } finally {
    revokingId.value = null;
  }
}

// ---------------------------------------------------------------------
// Disable / enable portal user
// ---------------------------------------------------------------------

async function onDisableUser(item: PortalUserEntry) {
  const confirmed = window.confirm(
    `确定要禁用用户「${item.display_name}」（${item.email}）吗？\n\n` +
      '这个操作会立即生效，而不是仅仅打一个标记：\n' +
      '· 该用户当前在所有设备上的登录会话会被立即全部终止 —— 即使他此刻正在使用中，下一次请求也会被拒绝；\n' +
      '· 他保存下来的登录状态无法再用于换取新的访问权限，必须等待重新启用后重新用密码/SSO 登录；\n' +
      '· 该操作不会删除他的账户或数据，随时可以在下方重新启用。\n\n是否继续？',
  );
  if (!confirmed) return;

  togglingUserId.value = item.user_id;
  try {
    await apiV1Client.post(`${USERS_BASE}/${encodeURIComponent(item.user_id)}/disable`);
    notify(`已禁用「${item.display_name}」，其所有登录会话已被终止`);
    await loadUsers();
  } catch (error) {
    notify(extractErrorMessage(error, '禁用用户失败'), 'error');
  } finally {
    togglingUserId.value = null;
  }
}

async function onEnableUser(item: PortalUserEntry) {
  const confirmed = window.confirm(
    `确定要重新启用用户「${item.display_name}」（${item.email}）吗？\n` +
      '启用后，该用户可以重新用密码/SSO 正常登录；此前因禁用而终止的会话不会自动恢复，他需要重新登录一次。\n\n是否继续？',
  );
  if (!confirmed) return;

  togglingUserId.value = item.user_id;
  try {
    await apiV1Client.post(`${USERS_BASE}/${encodeURIComponent(item.user_id)}/enable`);
    notify(`已重新启用「${item.display_name}」`);
    await loadUsers();
  } catch (error) {
    notify(extractErrorMessage(error, '启用用户失败'), 'error');
  } finally {
    togglingUserId.value = null;
  }
}

onMounted(refreshAll);
</script>

<style scoped>
.portal-users-page :deep(.v-data-table__td) {
  vertical-align: middle;
}
.mono-field :deep(input) {
  font-family: monospace;
}
.reveal-card {
  border: 2px solid rgb(var(--v-theme-error));
}
</style>
