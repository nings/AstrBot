<template>
  <div class="portal-chat-shell" :class="{ 'is-dark': isDark }">
    <PortalSessionList
      ref="sessionListRef"
      class="session-rail"
      :active-session-id="activeSessionId"
      @select="onSelectSession"
      @deleted="onSessionDeleted"
      @renamed="onSessionRenamed"
    />

    <div class="chat-main">
      <div v-if="sessionExpired" class="banner banner-error">
        <span>登录状态已过期，请重新登录后继续对话。</span>
        <a href="#/login">前往登录</a>
      </div>

      <div v-if="!activeSessionId" class="chat-empty-state">
        <p>选择左侧的一个会话，或新建一个开始聊天。</p>
      </div>

      <template v-else>
        <div class="chat-header">
          <span class="chat-title">{{ activeSessionTitle || "新会话" }}</span>
          <KbSelector
            :key="activeSessionId"
            class="chat-header-kb-selector"
            :session-id="activeSessionId ?? ''"
            :disabled="isGenerating"
          />
        </div>

        <div v-if="resumeNotice" class="banner banner-warning">
          {{ resumeNotice }}
        </div>

        <div ref="messageScrollRef" class="chat-scroll">
          <div v-if="loadingMessages" class="chat-center-state">
            <v-progress-circular indeterminate size="28" width="3" />
          </div>

          <div v-else-if="!messages.length" class="chat-center-state">
            <span>还没有消息，发送第一条试试。</span>
          </div>

          <div v-else class="chat-messages">
            <div
              v-for="(msg, msgIndex) in messages"
              :key="msg.id ?? `${msgIndex}-${msg.created_at || ''}`"
              class="message-row"
              :class="isUserMessage(msg) ? 'from-user' : 'from-bot'"
            >
              <v-avatar v-if="!isUserMessage(msg)" class="bot-avatar" size="36">
                <v-progress-circular
                  v-if="isMessageStreaming(msgIndex)"
                  indeterminate
                  size="16"
                  width="2"
                />
                <span v-else class="bot-avatar-symbol" aria-hidden="true">✦</span>
              </v-avatar>

              <div class="message-stack">
                <div
                  class="message-bubble"
                  :class="{ user: isUserMessage(msg), bot: !isUserMessage(msg) }"
                >
                  <div v-if="messageContent(msg).isLoading" class="loading-message">
                    <span>正在生成…</span>
                  </div>

                  <template v-else>
                    <template
                      v-for="(block, blockIndex) in renderBlocks(msg)"
                      :key="`${msgIndex}-block-${blockIndex}-${block.kind}`"
                    >
                      <ReasoningBlock
                        v-if="block.kind === 'thinking'"
                        :parts="block.parts"
                        :is-dark="isDark"
                        :initial-expanded="false"
                        :is-streaming="isMessageStreaming(msgIndex)"
                        :has-non-reasoning-content="hasFollowingContentBlock(msg, blockIndex)"
                      />

                      <template v-else>
                        <template
                          v-for="(part, partIndex) in block.parts"
                          :key="`${msgIndex}-${blockIndex}-${partIndex}-${part.type}`"
                        >
                          <div
                            v-if="part.type === 'plain' && isUserMessage(msg)"
                            class="plain-content"
                          >
                            {{ part.text || "" }}
                          </div>

                          <PortalMarkdownPart
                            v-else-if="part.type === 'plain'"
                            :content="part.text || ''"
                            :refs="resolvedMessageRefs(msg)"
                            :is-dark="isDark"
                            :is-streaming="isMessageStreaming(msgIndex)"
                          />

                          <PortalAttachment
                            v-else-if="['image', 'record', 'video', 'file'].includes(part.type)"
                            :part="part"
                            :is-dark="isDark"
                          />

                          <div v-else-if="part.type === 'tool_call'" class="tool-call-block">
                            <template v-for="tool in part.tool_calls || []" :key="tool.id || tool.name">
                              <ToolCallItem v-if="isIPythonToolCall(tool)" :is-dark="isDark">
                                <template #label>
                                  <v-icon size="14">mdi-code-json</v-icon>
                                  <span>{{ tool.name || "python" }}</span>
                                  <span class="tool-call-inline-status">
                                    {{ toolCallStatusText(tool) }}
                                  </span>
                                </template>
                                <template #details>
                                  <IPythonToolBlock
                                    :tool-call="normalizeToolCall(tool)"
                                    :is-dark="isDark"
                                    :show-header="false"
                                    :force-expanded="true"
                                  />
                                </template>
                              </ToolCallItem>
                              <ToolCallCard v-else :tool-call="normalizeToolCall(tool)" :is-dark="isDark" />
                            </template>
                          </div>

                          <div v-else class="unknown-part">{{ formatJson(part) }}</div>
                        </template>
                      </template>
                    </template>
                  </template>
                </div>

                <div v-if="showMessageMeta(msg, msgIndex)" class="message-meta">
                  <span v-if="msg.created_at">{{ formatTime(msg.created_at) }}</span>
                  <v-btn
                    v-if="!isUserMessage(msg)"
                    icon="mdi-content-copy"
                    size="x-small"
                    variant="text"
                    @click="copyMessage(msg)"
                  />
                  <v-btn
                    v-if="canRegenerate(msg, msgIndex)"
                    icon="mdi-refresh"
                    size="x-small"
                    variant="text"
                    @click="regenerateLast"
                  />
                  <v-menu v-if="messageContent(msg).agentStats" location="bottom">
                    <template #activator="{ props: statsProps }">
                      <v-btn
                        v-bind="statsProps"
                        icon="mdi-information-outline"
                        size="x-small"
                        variant="text"
                      />
                    </template>
                    <v-card class="stats-card" elevation="4">
                      <div v-if="cachedInputTokens(messageContent(msg).agentStats) > 0" class="stats-row">
                        <span>缓存输入</span>
                        <strong>{{ cachedInputTokens(messageContent(msg).agentStats) }}</strong>
                      </div>
                      <div class="stats-row">
                        <span>输入</span>
                        <strong>{{ inputTokens(messageContent(msg).agentStats) }}</strong>
                      </div>
                      <div class="stats-row">
                        <span>输出</span>
                        <strong>{{ outputTokens(messageContent(msg).agentStats) }}</strong>
                      </div>
                      <div v-if="agentTtft(messageContent(msg).agentStats)" class="stats-row">
                        <span>首字耗时</span>
                        <strong>{{ agentTtft(messageContent(msg).agentStats) }}</strong>
                      </div>
                      <div class="stats-row">
                        <span>总耗时</span>
                        <strong>{{ agentDuration(messageContent(msg).agentStats) }}</strong>
                      </div>
                    </v-card>
                  </v-menu>
                  <div v-if="messageRefs(msg).length" class="message-meta-refs">
                    <ActionRef :refs="resolvedMessageRefs(msg)" @open-refs="openRefsSidebar" />
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="chat-composer">
          <PortalComposer
            ref="composerRef"
            :is-generating="isGenerating"
            :disabled="!activeSessionId"
            @send="sendMessage"
            @stop="stopGeneration"
          />
        </div>
      </template>
    </div>

    <RefsSidebar v-model="refsSidebarOpen" :refs="selectedRefs" />
  </div>
</template>

<script setup lang="ts">
// Main layout for the portal end-user chat surface: session list + message
// thread + composer, wired directly to `astrbot/portal/api/chat_routes.py`.
//
// Reuse audit (see the assignment's "reuse by import vs reimplement"
// requirement):
//
//   REUSED BY DIRECT ES IMPORT (the actual markdown/code/math renderer and
//   its supporting node components -- none of these transitively import
//   any of the six admin-only modules the ESLint boundary rule forbids):
//     - `MarkdownRender` from `markstream-vue` (the actual streaming
//       markdown/shiki/katex/mermaid renderer).
//     - `@/components/portal/portalMarkdownComponents` (this module's own
//       renderer wiring, registering the same node components below under
//       a portal-only custom-id).
//     - `@/components/chat/markdownRenderConfig`
//       (`MARKDOWN_RENDER_MAX_LIVE_NODES`).
//     - `@/components/chat/message_list_comps/{ReasoningBlock,ToolCallCard,
//       ToolCallItem,IPythonToolBlock,ActionRef,RefsSidebar}.vue` (reasoning
//       timeline, tool-call cards, web-search reference chips/sidebar).
//     - `@/composables/useMessages`'s *pure* exports: `messageBlocks`,
//       `normalizeMessageParts`, `extractReasoningText`, `appendPlain`,
//       `appendReasoningPart`, `upsertToolCall`, `finishToolCall`,
//       `markMessageStarted`, `hasPlainText`, `payloadText`,
//       `parseJsonSafe`, `buildChatRequestFlags`, and the `ChatContent`/
//       `ChatRecord`/`MessagePart`/`MessageDisplayBlock` types -- this is
//       the exact "turn accumulated SSE frames into renderable message
//       content" logic, reused verbatim rather than reimplemented.
//     - `@/i18n/composables` (`useModuleI18n("features/chat")`, the same
//       translation namespace the reused components above already read
//       from) and `@/utils/clipboard` (`copyToClipboard`).
//     - `./PortalAttachment.vue` (this module's own owned attachment
//       renderer, Phase 2) for every `image`/`record`/`video`/`file`
//       message part -- both on outgoing user messages (attached
//       optimistically at send time, see `sendMessage`) and incoming
//       assistant messages (wired from the `image`/`record`/`file`/
//       `video` + `attachment_saved` SSE frame pair below, see
//       `applyStreamFrame`).
//
//   DELIBERATELY NOT REUSED, WITH REASONS:
//     - `useMessages()` (the composable *factory*, as opposed to its pure
//       named exports above) is NOT called here: its `sendMessageStream`/
//       `regenerateMessage`/`loadSessionMessages` etc. call the *operator*
//       dashboard's `chatApi` endpoints (`/api/v1/chat/...`, Bearer-token
//       auth) and unconditionally send `selected_provider`/`selected_model`
//       fields -- exactly the privilege-escalation vector the verified
//       ground truth calls out (`astr_main_agent.py:223-237`). This file
//       instead implements its own thin SSE transport against the
//       portal-scoped endpoints (`/api/v1/portal/chat/...`, httpOnly-cookie +
//       CSRF auth), while still funneling every frame through the pure,
//       reused helpers above.
//     - `@/components/chat/MessageList.vue` (a non-forbidden file that
//       happens to already assemble most of this same reuse set) is not
//       imported directly: it hardcodes the `"chat-message"` markstream-vue
//       custom-id (via `chatMarkdownComponents.ts` and
//       `MarkdownMessagePart.vue`), whereas the architecture plan (section
//       3.5) requires a portal-only custom-id (`"portal-chat"`) so the two
//       chat surfaces never share a renderer-registration namespace. The
//       per-message template structure below is therefore a purpose-built
//       re-assembly around that different custom-id, not a rebuild of the
//       renderer itself.
//     - `@/api/http`'s `fetchWithAuth`/`apiV1Client` are not used: they
//       attach the operator dashboard's `Authorization: Bearer <token>`
//       header from `localStorage`, the wrong auth mechanism for a portal
//       end user (httpOnly-cookie + CSRF double-submit). A small local
//       fetch helper is used instead (mirrors `PortalSessionList.vue`; no
//       shared portal API-client module exists in this phase's file
//       ownership -- see this component's `notes_for_integrator`).
//     - Attachment content (`image`/`record`/`video`/`file` parts, whether
//       emitted mid-stream by tool output or attached to an outgoing user
//       message) is rendered as a real, fetchable `<img>`/`<audio>`/
//       `<video>`/download-chip by `./PortalAttachment.vue` -- but never
//       against `@/api/v1`'s `fileApi`: that hits the *admin*
//       `/api/v1/files/*` endpoints, which the architecture plan's
//       verified ground truth explicitly forbids exposing to the portal
//       frontend (no ownership check at all on that route). Every URL
//       `PortalAttachment.vue` builds points at this phase's own
//       ownership-checked `GET /api/v1/portal/files/{attachment_id}`
//       instead (`astrbot/portal/api/file_routes.py`).
import {
  computed,
  defineComponent,
  h,
  nextTick,
  onBeforeUnmount,
  onMounted,
  provide,
  reactive,
  ref,
  type PropType,
} from "vue";
import { MarkdownRender } from "markstream-vue";
import {
  PORTAL_CHAT_CUSTOM_ID,
  PORTAL_CHAT_MARKDOWN_CUSTOM_TAGS,
  registerPortalChatMarkdownComponents,
} from "@/components/portal/portalMarkdownComponents";
import { MARKDOWN_RENDER_MAX_LIVE_NODES } from "@/components/chat/markdownRenderConfig";
import ReasoningBlock from "@/components/chat/message_list_comps/ReasoningBlock.vue";
import ToolCallCard from "@/components/chat/message_list_comps/ToolCallCard.vue";
import ToolCallItem from "@/components/chat/message_list_comps/ToolCallItem.vue";
import IPythonToolBlock from "@/components/chat/message_list_comps/IPythonToolBlock.vue";
import ActionRef from "@/components/chat/message_list_comps/ActionRef.vue";
import RefsSidebar from "@/components/chat/message_list_comps/RefsSidebar.vue";
import {
  appendPlain,
  appendReasoningPart,
  buildChatRequestFlags,
  extractReasoningText,
  finishToolCall,
  hasPlainText,
  markMessageStarted,
  messageBlocks as buildMessageBlocks,
  normalizeMessageParts,
  parseJsonSafe,
  payloadText,
  upsertToolCall,
  type ChatContent,
  type ChatRecord,
  type MessageDisplayBlock,
  type MessagePart,
} from "@/composables/useMessages";
import { copyToClipboard } from "@/utils/clipboard";
import PortalSessionList from "@/components/portal/PortalSessionList.vue";
import PortalComposer from "@/components/portal/PortalComposer.vue";
import PortalAttachment from "@/components/portal/PortalAttachment.vue";
import KbSelector from "@/components/portal/KbSelector.vue";

registerPortalChatMarkdownComponents();

// ---------------------------------------------------------------------------
// Per-message markdown renderer. A real child component instance is needed
// (not just a template-scoped variable) because `provide()`/`inject()` for
// `isDark`/`webSearchResults` (consumed by the reused `RefNode`/
// `ThemeAwareMarkdownCodeBlock` node renderers) is scoped per component
// instance -- exactly mirroring what the existing, non-forbidden
// `MarkdownMessagePart.vue` does, just inlined here since a new standalone
// `.vue` file is outside this assignment's owned-file list.
// ---------------------------------------------------------------------------
const PortalMarkdownPart = defineComponent({
  name: "PortalMarkdownPart",
  props: {
    content: { type: String, required: true },
    refs: {
      type: Object as PropType<{ used?: Array<Record<string, unknown>> } | null>,
      default: null,
    },
    isDark: { type: Boolean, default: false },
    isStreaming: { type: Boolean, default: false },
  },
  setup(props) {
    const isDarkRef = computed(() => props.isDark);
    const refsByIndex = computed(() => {
      const used = Array.isArray(props.refs?.used) ? props.refs!.used! : [];
      return used.reduce<Record<string, Record<string, unknown>>>((acc, item) => {
        if (item && (item as any).index != null) {
          acc[String((item as any).index)] = item as Record<string, unknown>;
        }
        return acc;
      }, {});
    });
    provide("isDark", isDarkRef);
    provide("webSearchResults", () => refsByIndex.value);

    return () =>
      h(MarkdownRender, {
        "custom-id": PORTAL_CHAT_CUSTOM_ID,
        content: props.content,
        "custom-html-tags": PORTAL_CHAT_MARKDOWN_CUSTOM_TAGS,
        "is-dark": props.isDark,
        final: !props.isStreaming,
        "smooth-streaming": props.isStreaming ? "auto" : false,
        fade: false,
        typewriter: false,
        "max-live-nodes": MARKDOWN_RENDER_MAX_LIVE_NODES,
      });
  },
});

// ---------------------------------------------------------------------------
// Own fetch/CSRF helper. See module docstring above for why
// `@/api/http`'s `fetchWithAuth` is the wrong tool here.
// ---------------------------------------------------------------------------
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
    sessionExpired.value = true;
  }
  return response;
}

async function safeErrorDetail(response: Response): Promise<string> {
  try {
    const data = await response.clone().json();
    if (typeof data?.detail === "string") return data.detail;
  } catch {
    // Non-JSON error body (e.g. a plain 500 page) -- fall through.
  }
  return "";
}

// ---------------------------------------------------------------------------
// SSE frame transport. `readSseStream` mirrors the byte-for-byte `data:
// {json}\n\n` framing documented in `chat_routes.py`'s own module
// docstring; `applyStreamFrame` re-implements exactly the frame-type
// dispatch of `useMessages.ts`'s (non-exported) `processStreamPayload`,
// but delegates every actual content mutation to that file's *exported*
// pure helpers (see the reuse audit above) rather than duplicating their
// logic.
// ---------------------------------------------------------------------------
async function readSseStream(
  body: ReadableStream<Uint8Array>,
  onPayload: (payload: any) => void,
) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  // eslint-disable-next-line no-constant-condition -- intentional read-until-done loop
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() || "";

    for (const event of events) {
      const data = event
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (!data) continue;
      try {
        onPayload(JSON.parse(data));
      } catch (error) {
        console.error("Failed to parse SSE payload:", error, data);
      }
    }
  }
}

/** Applies one decoded SSE frame to `botRecord`/`userRecord`. Returns
 * `true` if this frame is the terminal `"end"` frame. */
function applyStreamFrame(
  botRecord: ChatRecord,
  userRecord: ChatRecord | null,
  raw: any,
): boolean {
  const msgType = raw?.type;
  const chainType = raw?.chain_type;
  const data = raw?.data ?? "";

  if (msgType === "run_started") return false;
  if (msgType === "session_id" || msgType === "session_bound") return false;

  if (msgType === "run_snapshot") {
    const snapshot = data && typeof data === "object" ? data : {};
    const normalizedMessage = normalizeMessageParts(
      snapshot.content?.message || [],
      snapshot.content?.reasoning || "",
    );
    botRecord.content = {
      type: "bot",
      message: normalizedMessage,
      reasoning: extractReasoningText(normalizedMessage, snapshot.content?.reasoning || ""),
      isLoading: snapshot.status === "running" && normalizedMessage.length === 0,
    };
    botRecord.llm_checkpoint_id = snapshot.llm_checkpoint_id ?? null;
    return false;
  }

  if (msgType === "user_message_saved") {
    if (userRecord) {
      userRecord.id = data?.id ?? userRecord.id;
      userRecord.created_at = data?.created_at ?? userRecord.created_at;
      userRecord.llm_checkpoint_id = data?.llm_checkpoint_id ?? userRecord.llm_checkpoint_id;
    }
    return false;
  }

  if (msgType === "message_saved") {
    markMessageStarted(botRecord);
    botRecord.id = data?.id ?? botRecord.id;
    botRecord.created_at = data?.created_at ?? botRecord.created_at;
    botRecord.llm_checkpoint_id = data?.llm_checkpoint_id ?? botRecord.llm_checkpoint_id;
    if (data?.refs) botRecord.content.refs = data.refs;
    return false;
  }

  if (msgType === "agent_stats" || chainType === "agent_stats") {
    markMessageStarted(botRecord);
    botRecord.content.agentStats = data;
    return false;
  }

  if (msgType === "error") {
    markMessageStarted(botRecord);
    appendPlain(botRecord, `\n\n${String(data)}`);
    return false;
  }

  if (msgType === "complete" || msgType === "break") {
    markMessageStarted(botRecord);
    const finalText = payloadText(data);
    if (finalText && !hasPlainText(botRecord)) appendPlain(botRecord, finalText, false);
    return false;
  }

  if (msgType === "end") {
    markMessageStarted(botRecord);
    return true;
  }

  if (msgType === "plain") {
    markMessageStarted(botRecord);
    if (chainType === "reasoning") {
      appendReasoningPart(botRecord, payloadText(data));
      return false;
    }
    if (chainType === "tool_call") {
      upsertToolCall(botRecord, parseJsonSafe(data));
      return false;
    }
    if (chainType === "tool_call_result") {
      finishToolCall(botRecord, parseJsonSafe(data));
      return false;
    }
    appendPlain(botRecord, payloadText(data), raw.streaming !== false);
    return false;
  }

  if (["image", "record", "file", "video"].includes(msgType)) {
    markMessageStarted(botRecord);
    const rawFilename = String(data).replace(/^\[(IMAGE|RECORD|FILE|VIDEO)\]/, "");
    const separatorIndex = rawFilename.indexOf("|");
    const filename = separatorIndex >= 0 ? rawFilename.slice(separatorIndex + 1) : rawFilename;
    // No `attachment_id` yet -- this raw frame only carries the tool's
    // on-disk filename, which is not a portal attachment_id and cannot be
    // used to build a fetchable URL. `PortalAttachment.vue` renders this
    // as a "processing" placeholder until the `attachment_saved` frame
    // below (published immediately after core finishes writing the file
    // and registering it, see `chat_service.py:965-998`) supplies the
    // real id.
    botRecord.content.message.push({ type: msgType, filename });
    return false;
  }

  if (msgType === "attachment_saved") {
    // Backfills the `attachment_id` onto the most recently pushed media
    // part of the same type that doesn't have one yet. Core publishes
    // this frame immediately after the raw media frame it corresponds to
    // (one core-attachment-creation call per loop iteration, synchronously
    // before the next tool message is even processed -- see
    // `chat_service.py:965-1006`), so at the moment this frame arrives
    // there is at most one outstanding part per type, and searching from
    // the end for the nearest unmatched part of `data.type` reliably picks
    // it out even if several media parts of the same type appear across
    // one turn.
    const parts = botRecord.content.message;
    for (let i = parts.length - 1; i >= 0; i -= 1) {
      const part = parts[i];
      if (part.type === data?.type && !part.attachment_id) {
        part.attachment_id = data?.id;
        break;
      }
    }
    return false;
  }

  return false;
}

async function consumeStream(
  response: Response,
  botRecord: ChatRecord,
  userRecord: ChatRecord | null,
): Promise<boolean> {
  if (!response.body) throw new Error("空响应流");
  let ended = false;
  await readSseStream(response.body, (payload) => {
    if (applyStreamFrame(botRecord, userRecord, payload)) ended = true;
  });
  return ended;
}

// ---------------------------------------------------------------------------
// Component state
// ---------------------------------------------------------------------------
const activeSessionId = ref<string | null>(null);
const activeSessionTitle = ref("");
const messages = ref<ChatRecord[]>([]);
const loadingMessages = ref(false);
const isGenerating = ref(false);
const resumeNotice = ref("");
const sessionExpired = ref(false);
const isDark = ref(
  typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-color-scheme: dark)").matches === true,
);

const sessionListRef = ref<InstanceType<typeof PortalSessionList> | null>(null);
const composerRef = ref<InstanceType<typeof PortalComposer> | null>(null);
const messageScrollRef = ref<HTMLElement | null>(null);
const refsSidebarOpen = ref(false);
const selectedRefs = ref<Record<string, unknown> | null>(null);

let currentAbort: AbortController | null = null;
let darkMediaQuery: MediaQueryList | null = null;

function handleDarkChange(event: MediaQueryListEvent) {
  isDark.value = event.matches;
}

onMounted(() => {
  darkMediaQuery = window.matchMedia?.("(prefers-color-scheme: dark)") ?? null;
  darkMediaQuery?.addEventListener("change", handleDarkChange);
});

onBeforeUnmount(() => {
  darkMediaQuery?.removeEventListener("change", handleDarkChange);
  currentAbort?.abort();
});

function scrollToBottomSoon() {
  nextTick(() => {
    const el = messageScrollRef.value;
    if (el) el.scrollTop = el.scrollHeight;
  });
}

// ---------------------------------------------------------------------------
// Small pure display helpers (view-local, not "renderer" logic -- the
// actual content transforms live in `@/composables/useMessages`, imported
// above).
// ---------------------------------------------------------------------------
function messageContent(message: ChatRecord): ChatContent {
  return message.content || { type: "bot", message: [] };
}

function isUserMessage(message: ChatRecord) {
  return messageContent(message).type === "user";
}

function renderBlocks(message: ChatRecord): MessageDisplayBlock[] {
  if (isUserMessage(message)) {
    const parts = messageContent(message).message || [];
    return parts.length ? [{ kind: "content", parts }] : [];
  }
  return buildMessageBlocks(messageContent(message));
}

function hasFollowingContentBlock(message: ChatRecord, blockIndex: number) {
  return renderBlocks(message)
    .slice(blockIndex + 1)
    .some((block) => block.kind === "content");
}

function isMessageStreaming(index: number) {
  return isGenerating.value && index === messages.value.length - 1;
}

function showMessageMeta(message: ChatRecord, index: number) {
  return !messageContent(message).isLoading && !isMessageStreaming(index);
}

function canRegenerate(message: ChatRecord, index: number) {
  if (isGenerating.value || isUserMessage(message)) return false;
  if (index !== messages.value.length - 1) return false;
  const id = message.id;
  if (id == null) return false;
  const idStr = String(id);
  return !idStr.startsWith("local-") && !idStr.startsWith("active-run-");
}

function normalizeHistoryRecord(record: any): ChatRecord {
  const content = record.content || {};
  const normalizedMessage = normalizeMessageParts(content.message || [], content.reasoning || "");
  return {
    ...record,
    content: {
      type: content.type || (record.sender_id === "bot" ? "bot" : "user"),
      message: normalizedMessage,
      reasoning: extractReasoningText(normalizedMessage, content.reasoning || ""),
      agentStats: content.agentStats || content.agent_stats,
      refs: content.refs,
    },
  };
}

function normalizeToolCall(tool: Record<string, unknown>) {
  const normalized: Record<string, any> = { ...tool };
  normalized.args = parseJsonSafe(normalized.args ?? normalized.arguments ?? {});
  normalized.result = parseJsonSafe(normalized.result);
  normalized.ts = normalized.ts ?? Date.now() / 1000;
  if (normalized.result && typeof normalized.result === "object") {
    normalized.result = JSON.stringify(normalized.result, null, 2);
  }
  return normalized;
}

function isIPythonToolCall(tool: Record<string, unknown>) {
  const name = String(tool.name || "").toLowerCase();
  return name.includes("python") || name.includes("ipython");
}

function toolCallStatusText(tool: Record<string, unknown>) {
  return tool.finished_ts ? "已完成" : "运行中";
}

function formatJson(value: unknown) {
  if (typeof value === "string") {
    const parsed = parseJsonSafe(value);
    if (parsed !== value) return JSON.stringify(parsed, null, 2);
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? "");
  }
}

function messageRefs(message: ChatRecord) {
  return resolvedMessageRefs(message).used;
}

function resolvedMessageRefs(message: ChatRecord) {
  return normalizeRefs(messageContent(message).refs);
}

function normalizeRefs(refs: unknown) {
  if (!refs) return { used: [] as Array<Record<string, unknown>> };
  const refsValue = refs as { used?: unknown };
  const used = Array.isArray(refsValue.used)
    ? refsValue.used
    : Array.isArray(refs)
    ? refs
    : [];
  return { used: normalizeRefItems(used as unknown[]) };
}

function normalizeRefItems(items: unknown[]) {
  return items
    .map((item: any) => ({
      index: item?.index,
      title: item?.title || item?.url || "参考来源",
      url: item?.url,
      snippet: item?.snippet,
      favicon: item?.favicon,
    }))
    .filter((item) => item.url);
}

function openRefsSidebar(refs: unknown) {
  selectedRefs.value = refs && typeof refs === "object" ? (refs as Record<string, unknown>) : null;
  refsSidebarOpen.value = true;
}

function formatTime(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function plainTextFromMessage(message: ChatRecord) {
  return (messageContent(message).message || [])
    .filter((part) => part.type === "plain" && part.text)
    .map((part) => part.text)
    .join("\n");
}

async function copyMessage(message: ChatRecord) {
  const text = plainTextFromMessage(message);
  if (!text) return;
  await copyToClipboard(text, { container: messageScrollRef.value });
}

function inputTokens(stats: any) {
  return stats?.token_usage?.input_other || 0;
}

function outputTokens(stats: any) {
  return stats?.token_usage?.output || 0;
}

function cachedInputTokens(stats: any) {
  return stats?.token_usage?.input_cached || 0;
}

function readPositiveNumber(source: any, keys: string[]) {
  for (const key of keys) {
    const value = Number(source?.[key]);
    if (Number.isFinite(value) && value > 0) return value;
  }
  return null;
}

function formatDuration(seconds: number) {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const restSeconds = Math.round(seconds % 60);
  return `${minutes}m ${restSeconds}s`;
}

function agentDuration(stats: any) {
  const directDuration = readPositiveNumber(stats, ["duration", "total_duration"]);
  if (directDuration !== null) return formatDuration(directDuration);
  const startTime = readPositiveNumber(stats, ["start_time"]);
  const endTime = readPositiveNumber(stats, ["end_time"]);
  if (startTime === null || endTime === null || endTime < startTime) return "-";
  return formatDuration(endTime - startTime);
}

function agentTtft(stats: any) {
  const ttft = readPositiveNumber(stats, ["time_to_first_token", "ttft", "first_token_latency"]);
  if (ttft === null) return "";
  return formatDuration(ttft);
}

// ---------------------------------------------------------------------------
// Session selection / loading
// ---------------------------------------------------------------------------
function onSelectSession(sessionId: string, label: string | null) {
  void selectSession(sessionId, label);
}

function onSessionDeleted(sessionId: string) {
  if (activeSessionId.value !== sessionId) return;
  currentAbort?.abort();
  isGenerating.value = false;
  activeSessionId.value = null;
  activeSessionTitle.value = "";
  messages.value = [];
  resumeNotice.value = "";
}

function onSessionRenamed(sessionId: string, displayName: string) {
  if (activeSessionId.value === sessionId) activeSessionTitle.value = displayName;
}

async function selectSession(sessionId: string, label: string | null) {
  if (activeSessionId.value === sessionId) return;
  currentAbort?.abort();
  isGenerating.value = false;
  activeSessionId.value = sessionId;
  activeSessionTitle.value = label || "";
  resumeNotice.value = "";
  await loadSession(sessionId);
  nextTick(() => composerRef.value?.focus());
}

async function loadSession(sessionId: string) {
  loadingMessages.value = true;
  try {
    const response = await portalFetch(`/chat/sessions/${encodeURIComponent(sessionId)}`);
    if (!response.ok) throw new Error(`加载会话失败（${response.status}）`);
    const payload = await response.json();
    if (activeSessionId.value !== sessionId) return;
    messages.value = (payload.history || []).map(normalizeHistoryRecord);
    loadingMessages.value = false;
    scrollToBottomSoon();

    const activeRuns = Array.isArray(payload.active_runs) ? payload.active_runs : [];
    if (activeRuns.length) {
      await resumeActiveRun(sessionId, activeRuns[0]);
    }
  } catch (error) {
    loadingMessages.value = false;
    resumeNotice.value = (error as Error)?.message || "加载会话失败";
  }
}

async function reloadPersistedHistory(sessionId: string) {
  try {
    const response = await portalFetch(`/chat/sessions/${encodeURIComponent(sessionId)}`);
    if (!response.ok || activeSessionId.value !== sessionId) return;
    const payload = await response.json();
    if (activeSessionId.value !== sessionId) return;
    messages.value = (payload.history || []).map(normalizeHistoryRecord);
    scrollToBottomSoon();
  } catch (error) {
    console.error("Failed to reload persisted history after resume failure:", error);
  }
}

/**
 * Resume a chat run that was still in flight when this session was
 * (re)loaded -- e.g. the tab was reloaded mid-generation. Per the
 * architecture plan's stated risk ("chat_runs is in-process memory state
 * and does not survive a restart/replica change"), this retries a few
 * times with backoff and, if resuming genuinely cannot succeed, falls back
 * to the last persisted history (which will never include the incomplete
 * turn -- it was never committed) and tells the user plainly to resend,
 * rather than leaving a stuck spinner or silently dropping the turn.
 */
async function resumeActiveRun(sessionId: string, run: any) {
  if (!run?.run_id) return;

  const checkpointId = run.llm_checkpoint_id ?? null;
  const filtered = messages.value.filter((record) => {
    return !(
      checkpointId &&
      record.llm_checkpoint_id === checkpointId &&
      messageContent(record).type === "bot"
    );
  });

  const botRecord = reactive(
    normalizeHistoryRecord({
      id: `active-run-${run.run_id}`,
      content: run.content || { type: "bot", message: [] },
      llm_checkpoint_id: checkpointId,
      created_at: new Date().toISOString(),
    }),
  ) as ChatRecord;
  botRecord.content.isLoading = botRecord.content.message.length === 0;

  messages.value = [...filtered, botRecord];
  isGenerating.value = true;
  scrollToBottomSoon();

  const maxAttempts = 3;
  let succeeded = false;

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (activeSessionId.value !== sessionId) return;
    const abort = new AbortController();
    currentAbort = abort;
    try {
      const response = await portalFetch(`/chat/runs/${encodeURIComponent(run.run_id)}/stream`, {
        headers: { Accept: "text/event-stream" },
        signal: abort.signal,
      });
      const contentType = response.headers.get("content-type") || "";
      if (!response.ok || !response.body || !contentType.includes("text/event-stream")) {
        throw new Error(`恢复对话流失败（${response.status}）`);
      }
      const ended = await consumeStream(response, botRecord, null);
      if (ended) {
        succeeded = true;
        break;
      }
      throw new Error("对话流在完成前中断。");
    } catch (error) {
      if (abort.signal.aborted) return;
      if (attempt === maxAttempts - 1) break;
      await new Promise((resolve) => setTimeout(resolve, 400 * 2 ** attempt));
    }
  }

  isGenerating.value = false;
  currentAbort = null;

  if (!succeeded && activeSessionId.value === sessionId) {
    await reloadPersistedHistory(sessionId);
    resumeNotice.value =
      "之前那条回复未能恢复实时输出（连接已中断，无法续传）。请重新发送这条消息以继续对话。";
  }
}

// ---------------------------------------------------------------------------
// Send / stop / regenerate
// ---------------------------------------------------------------------------

/**
 * Structural (not imported) mirror of `usePortalMediaHandling.ts`'s
 * `PortalMessagePart` -- the exact shape `PortalComposer.vue`'s `send`
 * event now carries as its multimodal list form (its own `toMessageParts`
 * return type). Declared locally rather than imported so this file's
 * accepted parameter type doesn't hard-depend on a sibling composable's
 * type export surface; it is kept in lock-step with that shape by hand
 * (`type` + optional `text`/`attachment_id`/`filename`, nothing else) --
 * `MessagePart` from `useMessages.ts` cannot be used directly for the
 * *parameter* type here despite being structurally similar, since its
 * `[key: string]: unknown` index signature makes plain object-literal
 * shapes like `PortalMessagePart` (which has no index signature of its
 * own) fail assignability as a function parameter.
 */
interface IncomingMessagePart {
  type: string;
  text?: string;
  attachment_id?: string;
  filename?: string;
}

/**
 * `sendMessage`'s single argument is whatever the composer already
 * resolved the turn's wire payload down to -- a bare trimmed string when
 * there are no successfully uploaded attachments (byte-identical to this
 * phase's pre-attachment wire shape), or the multimodal list form
 * otherwise: one `{type, attachment_id, filename}` part per uploaded
 * attachment (already real, already ownership-registered, since the
 * composer only ever hands back attachments whose upload has completed)
 * followed by an optional trailing `{type: "plain", text}` part.
 *
 * Displaying it is then just "use these parts as the optimistic user
 * message's content" -- no separate attachment-list parameter, no
 * reassembly: whatever the server will receive is exactly what
 * `PortalAttachment.vue`/the plain-text renderer render immediately,
 * satisfying "display attachments on outgoing user messages
 * optimistically, right after send" without a second source of truth.
 */
async function sendMessage(message: string | IncomingMessagePart[]) {
  const sessionId = activeSessionId.value;
  if (!sessionId || isGenerating.value) return;

  const messageIsList = Array.isArray(message);
  const trimmedText = messageIsList ? "" : message.trim();
  if (messageIsList ? message.length === 0 : !trimmedText) return;

  // Rebuilt as fresh `MessagePart` object literals (rather than assigning
  // `message` in place) precisely to cross the index-signature boundary
  // noted on `IncomingMessagePart` above -- each entry keeps only the keys
  // `MessagePart`/the server actually understand.
  const displayParts: MessagePart[] = messageIsList
    ? message.map((part) => ({
        type: part.type,
        text: part.text,
        attachment_id: part.attachment_id,
        filename: part.filename,
      }))
    : [{ type: "plain", text: trimmedText }];

  const userRecord = reactive<ChatRecord>({
    id: `local-user-${Date.now()}`,
    created_at: new Date().toISOString(),
    content: { type: "user", message: displayParts },
  });
  const botRecord = reactive<ChatRecord>({
    id: `local-bot-${Date.now()}`,
    created_at: new Date().toISOString(),
    content: { type: "bot", message: [], reasoning: "", isLoading: true },
  });

  messages.value = [...messages.value, userRecord, botRecord];
  resumeNotice.value = "";
  isGenerating.value = true;
  scrollToBottomSoon();

  // Forwarded to the server exactly as received -- matches
  // `SendMessageRequest.message: str | list[MessagePart]` in
  // `chat_routes.py` verbatim, list form or bare string alike.
  const abort = new AbortController();
  currentAbort = abort;
  try {
    const response = await portalFetch(`/chat/sessions/${encodeURIComponent(sessionId)}/messages`, {
      method: "POST",
      body: JSON.stringify({ message, flags: buildChatRequestFlags(true) }),
      signal: abort.signal,
    });
    if (!response.ok || !response.body) {
      const detail = await safeErrorDetail(response);
      throw new Error(detail || `发送失败（${response.status}）`);
    }
    await consumeStream(response, botRecord, userRecord);
  } catch (error) {
    if (!abort.signal.aborted) {
      markMessageStarted(botRecord);
      appendPlain(botRecord, `\n\n${String((error as Error)?.message || error)}`);
    }
  } finally {
    isGenerating.value = false;
    currentAbort = null;
    sessionListRef.value?.refresh();
  }
}

async function stopGeneration() {
  const sessionId = activeSessionId.value;
  currentAbort?.abort();
  if (!sessionId) return;
  try {
    await portalFetch(`/chat/sessions/${encodeURIComponent(sessionId)}/stop`, { method: "POST" });
  } catch (error) {
    console.error("Failed to stop chat run:", error);
  }
}

async function regenerateLast() {
  const sessionId = activeSessionId.value;
  if (!sessionId || isGenerating.value) return;
  const lastIndex = messages.value.length - 1;
  const target = messages.value[lastIndex];
  if (!target || isUserMessage(target) || target.id == null) return;
  const messageId = target.id;

  target.content = { type: "bot", message: [], reasoning: "", isLoading: true };
  isGenerating.value = true;
  resumeNotice.value = "";

  const abort = new AbortController();
  currentAbort = abort;
  try {
    const response = await portalFetch(
      `/chat/sessions/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(
        String(messageId),
      )}/regenerate`,
      { method: "POST", signal: abort.signal },
    );
    if (!response.ok || !response.body) {
      const detail = await safeErrorDetail(response);
      throw new Error(detail || `重新生成失败（${response.status}）`);
    }
    await consumeStream(response, target, null);
  } catch (error) {
    if (!abort.signal.aborted) {
      markMessageStarted(target);
      appendPlain(target, `\n\n${String((error as Error)?.message || error)}`);
    }
  } finally {
    isGenerating.value = false;
    currentAbort = null;
  }
}
</script>

<style scoped>
.portal-chat-shell {
  --chat-border: rgba(var(--v-border-color), 0.16);
  --chat-muted: rgba(var(--v-theme-on-surface), 0.62);
  display: flex;
  height: 100%;
  min-height: 0;
  color: rgb(var(--v-theme-on-surface));
  background: rgb(var(--v-theme-background));
}

.portal-chat-shell.is-dark {
  --chat-border: rgba(255, 255, 255, 0.1);
}

.session-rail {
  width: 260px;
  flex: 0 0 260px;
  border-right: 1px solid var(--chat-border);
}

.chat-main {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: 10px 16px 0;
  padding: 8px 14px;
  border-radius: 8px;
  font-size: 13px;
}

.banner-error {
  background: rgba(var(--v-theme-error), 0.1);
  color: rgb(var(--v-theme-error));
}

.banner-error a {
  color: inherit;
  font-weight: 600;
  text-decoration: underline;
}

.banner-warning {
  background: rgba(var(--v-theme-warning), 0.12);
  color: rgb(var(--v-theme-warning));
}

.chat-empty-state,
.chat-center-state {
  display: flex;
  flex: 1 1 auto;
  align-items: center;
  justify-content: center;
  color: var(--chat-muted);
  font-size: 14px;
}

.chat-header {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--chat-border);
}

.chat-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 15px;
  font-weight: 600;
}

.chat-header-kb-selector {
  flex-shrink: 0;
}

.chat-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 20px;
}

.chat-messages {
  display: flex;
  flex-direction: column;
  gap: 22px;
}

.message-row {
  display: flex;
  gap: 10px;
  max-width: 100%;
}

.message-row.from-user {
  justify-content: flex-end;
}

.message-stack {
  max-width: min(760px, 82%);
}

.from-bot .message-stack {
  flex: 1 1 0;
  min-width: 0;
  max-width: 760px;
}

.from-user .message-stack {
  align-items: flex-end;
  max-width: 60%;
}

.bot-avatar {
  margin-top: 2px;
  color: rgb(var(--v-theme-primary));
  user-select: none;
}

.bot-avatar-symbol {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  line-height: 0;
  pointer-events: none;
  user-select: none;
}

.message-bubble {
  max-width: 100%;
  border-radius: 8px;
  padding: 10px 14px;
  line-height: 1.65;
  overflow-wrap: anywhere;
}

.message-bubble.user {
  padding: 12px 18px;
  font-size: 15px;
  border-radius: 1.5rem;
  background: rgba(var(--v-theme-primary), 0.12);
}

.message-bubble.bot {
  background: transparent;
  padding-left: 0;
}

.plain-content {
  white-space: pre-wrap;
}

.loading-message {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--chat-muted);
}

.tool-call-block {
  margin: 8px 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tool-call-inline-status {
  color: var(--chat-muted);
  font-size: 12px;
}

.unknown-part {
  max-width: 100%;
  overflow-x: auto;
  border-radius: 8px;
  padding: 10px;
  background: rgba(var(--v-theme-on-surface), 0.06);
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
}

.message-meta {
  display: flex;
  align-items: center;
  gap: 2px;
  min-height: 24px;
  color: var(--chat-muted);
  font-size: 12px;
}

.message-meta-refs {
  display: flex;
  align-items: center;
}

.from-user .message-meta {
  justify-content: flex-end;
}

.stats-card {
  min-width: 150px;
  padding: 8px 10px;
}

.stats-row {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  padding: 2px 0;
  font-size: 12px;
  line-height: 1.35;
}

.stats-row span {
  color: var(--chat-muted);
}

.stats-row strong {
  font-size: 12px;
  font-weight: 600;
}

.chat-composer {
  flex-shrink: 0;
  padding: 14px 20px 18px;
  border-top: 1px solid var(--chat-border);
}

@media (max-width: 760px) {
  .session-rail {
    display: none;
  }

  .message-stack,
  .from-user .message-stack {
    max-width: 88%;
  }
}
</style>
