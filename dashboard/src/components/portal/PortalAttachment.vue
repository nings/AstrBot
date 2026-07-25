<template>
  <div class="portal-attachment" :class="`kind-${kind}`">
    <!-- No attachment_id yet: the SSE `image`/`record`/`file`/`video` frame
         has arrived but the follow-up `attachment_saved` frame (which
         carries the real, ownership-checked attachment_id) has not, e.g.
         a tool is still writing the file to disk server-side. Nothing is
         fetchable yet, so show a lightweight processing placeholder rather
         than guessing at a URL. -->
    <div
      v-if="!attachmentId"
      class="portal-attachment-pending"
      :style="{ '--attachment-color': presentation.color }"
    >
      <v-progress-circular indeterminate size="14" width="2" />
      <v-icon :icon="presentation.icon" size="15" />
      <span class="portal-attachment-pending-label">{{ displayName }}</span>
    </div>

    <!-- Image: inline thumbnail with a click-to-expand full-size lightbox. -->
    <template v-else-if="kind === 'image'">
      <div
        v-if="broken"
        class="portal-attachment-broken"
        :style="{ '--attachment-color': presentation.color }"
      >
        <v-icon icon="mdi-image-broken-variant" size="18" />
        <span>{{ displayName }}（图片不可用或已被删除）</span>
      </div>
      <button
        v-else
        type="button"
        class="portal-attachment-image-trigger"
        :aria-label="`查看图片 ${displayName}`"
        @click="lightboxOpen = true"
      >
        <img
          :src="fileUrl"
          :alt="displayName"
          class="portal-attachment-image"
          loading="lazy"
          @error="onMediaError"
        />
      </button>

      <v-dialog v-model="lightboxOpen" max-width="min(94vw, 1200px)" scrollable>
        <div class="portal-attachment-lightbox" @click.self="lightboxOpen = false">
          <v-btn
            class="portal-attachment-lightbox-close"
            icon="mdi-close"
            size="small"
            variant="flat"
            :aria-label="'关闭'"
            @click="lightboxOpen = false"
          />
          <img :src="fileUrl" :alt="displayName" class="portal-attachment-lightbox-image" />
        </div>
      </v-dialog>
    </template>

    <!-- Audio -->
    <div v-else-if="kind === 'record'" class="portal-attachment-media">
      <div
        v-if="broken"
        class="portal-attachment-broken"
        :style="{ '--attachment-color': presentation.color }"
      >
        <v-icon icon="mdi-file-music-outline" size="18" />
        <span>{{ displayName }}（音频不可用或已被删除）</span>
      </div>
      <audio
        v-else
        controls
        preload="none"
        :src="fileUrl"
        class="portal-attachment-audio"
        @error="onMediaError"
      >
        您的浏览器不支持音频播放。
      </audio>
    </div>

    <!-- Video -->
    <div v-else-if="kind === 'video'" class="portal-attachment-media">
      <div
        v-if="broken"
        class="portal-attachment-broken"
        :style="{ '--attachment-color': presentation.color }"
      >
        <v-icon icon="mdi-file-video-outline" size="18" />
        <span>{{ displayName }}（视频不可用或已被删除）</span>
      </div>
      <video
        v-else
        controls
        preload="metadata"
        :src="fileUrl"
        class="portal-attachment-video"
        @error="onMediaError"
      >
        您的浏览器不支持视频播放。
      </video>
    </div>

    <!-- Generic file (pdf/text/csv/json/...): a download chip. There is no
         native media element here to raise an `error` event, so a lightweight
         HEAD probe (see `probeFileExists`) is used instead to detect a
         deleted/unowned attachment before the user clicks a dead link. -->
    <a
      v-else
      class="portal-attachment-chip"
      :class="{ 'is-broken': broken }"
      :style="{ '--attachment-color': presentation.color }"
      :href="broken ? undefined : fileUrl"
      target="_blank"
      rel="noopener noreferrer"
      @click="onChipClick"
    >
      <v-icon :icon="broken ? 'mdi-file-remove-outline' : presentation.icon" size="18" />
      <span class="portal-attachment-chip-label">{{ displayName }}</span>
      <span class="portal-attachment-chip-meta">{{ broken ? "不可用或已被删除" : sizeLabel }}</span>
    </a>
  </div>
</template>

<script setup lang="ts">
// Renders one media/file attachment inside a portal chat message bubble --
// the single owned renderer for both outgoing (optimistic, right after
// send) and incoming (SSE `image`/`record`/`file`/`video` +
// `attachment_saved` frames, or persisted history) attachment parts. See
// `PortalChatShell.vue`'s own module docstring for how each call site
// supplies `part`.
//
// Security-critical invariant (do not change without re-reading the
// architecture plan's section 4, checklist item 4): every fetchable URL
// this component builds points at the portal's own ownership-checked
// `GET /api/v1/portal/files/{attachment_id}` route
// (`astrbot/portal/api/file_routes.py`) and NOWHERE else. Core's
// `/api/v1/files/*` endpoints have no ownership check at all and must
// never appear in the portal bundle (CI greps for this) -- so this file
// must never import `@/api/v1`'s `fileApi`, construct a `/api/v1/files/...`
// path, or otherwise reference that module.
//
// Reuse: `attachmentName`/`attachmentPresentation` from
// `@/components/chat/attachmentPresentation` are reused by direct import
// (pure icon/label/color helpers, no admin-only dependency) rather than
// reimplemented -- exactly like `PortalChatShell.vue` already does for the
// tool-call/reasoning renderers.
import { computed, ref, watch } from "vue";
import {
  attachmentName,
  attachmentPresentation,
  type AttachmentPresentationInput,
} from "@/components/chat/attachmentPresentation";

interface PortalAttachmentPart extends AttachmentPresentationInput {
  attachment_id?: string;
  byte_size?: number;
  [key: string]: unknown;
}

const props = withDefaults(
  defineProps<{
    part: PortalAttachmentPart;
    /** Reserved for visual parity with the rest of the message thread
     * (dark-mode-sensitive borders/backgrounds); no behavior depends on
     * it today, but it is threaded through so a future dark-specific
     * treatment doesn't need a new prop wired through every call site. */
    isDark?: boolean;
  }>(),
  { isDark: false },
);

/** Sole sanctioned download/preview base -- see module docstring above. */
const PORTAL_FILES_BASE = "/api/v1/portal/files";

const KNOWN_KINDS = new Set(["image", "record", "video"]);

const attachmentId = computed(() => props.part?.attachment_id || "");

/** `type` is the field every call site (SSE frames, persisted history,
 * the optimistic outgoing-message builder) actually uses; `attach_type`
 * is accepted too since that is the literal key name the upload
 * confirmation response (`POST /api/v1/portal/files`) uses, in case a
 * caller forwards that response shape verbatim without renaming the key. */
const kind = computed(() => {
  const raw = String(props.part?.type || props.part?.attach_type || "file");
  return KNOWN_KINDS.has(raw) ? raw : "file";
});

const displayName = computed(() => attachmentName(props.part || {}));
const presentation = computed(() => attachmentPresentation(props.part || {}));

const fileUrl = computed(() =>
  attachmentId.value ? `${PORTAL_FILES_BASE}/${encodeURIComponent(attachmentId.value)}` : "",
);

const sizeLabel = computed(() => formatBytes(props.part?.byte_size));

const broken = ref(false);
const lightboxOpen = ref(false);

function formatBytes(size?: number): string {
  if (!size || size <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = size;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const decimals = unitIndex === 0 || value >= 10 ? 0 : 1;
  return `${value.toFixed(decimals)}${units[unitIndex]}`;
}

/** `<img>`/`<audio>`/`<video>` all raise a native `error` event when the
 * underlying request 404s (deleted attachment) or the ownership check
 * fails (also a 404 -- see file_routes.py's own docstring on why
 * ownership misses are never a distinguishable 403). Catching it here is
 * what turns that into a placeholder instead of a broken-image icon /
 * inert stalled player. */
function onMediaError() {
  broken.value = true;
}

/** The generic-file download chip has no native media element to give an
 * error signal, so a cheap `HEAD` probe stands in for it: Starlette's
 * `FileResponse` (and the GET-route registration in file_routes.py)
 * answers `HEAD` with headers only, no body, so this costs nothing beyond
 * one small round trip and never downloads attacker-supplied bytes. */
async function probeFileExists() {
  if (!fileUrl.value) return;
  try {
    const response = await fetch(fileUrl.value, {
      method: "HEAD",
      credentials: "include",
    });
    broken.value = !response.ok;
  } catch {
    // Network error / offline -- treat the same as "not available" rather
    // than leaving a dead link with no visible signal.
    broken.value = true;
  }
}

function onChipClick(event: MouseEvent) {
  if (broken.value) event.preventDefault();
}

// Re-probe whenever a real attachment_id first becomes available (the
// pending -> confirmed transition once the `attachment_saved` frame
// lands) and whenever it changes to a different id outright. `broken` is
// reset first so a stale placeholder never survives that transition.
watch(
  [attachmentId, kind],
  ([id, currentKind]) => {
    broken.value = false;
    if (id && currentKind === "file") {
      void probeFileExists();
    }
  },
  { immediate: true },
);
</script>

<style scoped>
.portal-attachment {
  display: inline-flex;
  flex-direction: column;
  align-items: flex-start;
  max-width: 100%;
  margin-top: 6px;
}

.portal-attachment-pending {
  --attachment-color: #607d8b;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 8px;
  background: rgba(var(--v-theme-on-surface), 0.055);
  color: var(--attachment-color);
  font-size: 12.5px;
}

.portal-attachment-pending-label {
  color: rgb(var(--v-theme-on-surface));
  font-weight: 500;
}

.portal-attachment-broken {
  --attachment-color: #8a8a8a;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 8px;
  background: rgba(var(--v-theme-on-surface), 0.055);
  color: rgba(var(--v-theme-on-surface), 0.6);
  font-size: 12.5px;
}

.portal-attachment-image-trigger {
  display: inline-block;
  max-width: min(320px, 100%);
  padding: 0;
  border: 0;
  border-radius: 10px;
  overflow: hidden;
  cursor: zoom-in;
  background: none;
  line-height: 0;
}

.portal-attachment-image {
  display: block;
  max-width: 100%;
  max-height: 280px;
  border-radius: 10px;
  object-fit: cover;
}

.portal-attachment-lightbox {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 40vh;
  padding: 20px;
  position: relative;
}

.portal-attachment-lightbox-image {
  max-width: 100%;
  max-height: 86vh;
  border-radius: 6px;
  object-fit: contain;
}

.portal-attachment-lightbox-close {
  position: absolute;
  top: 10px;
  right: 10px;
  z-index: 1;
}

.portal-attachment-media {
  max-width: min(360px, 100%);
}

.portal-attachment-audio {
  width: min(320px, 100%);
}

.portal-attachment-video {
  display: block;
  width: 100%;
  max-width: min(420px, 100%);
  max-height: 320px;
  border-radius: 10px;
  background: #000;
}

.portal-attachment-chip {
  --attachment-color: #607d8b;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 8px;
  background: rgba(var(--v-theme-on-surface), 0.055);
  color: var(--attachment-color);
  font-size: 12.5px;
  text-decoration: none;
  transition: background-color 0.15s ease;
}

.portal-attachment-chip:not(.is-broken):hover {
  background: rgba(var(--v-theme-on-surface), 0.09);
}

.portal-attachment-chip.is-broken {
  cursor: not-allowed;
  color: rgba(var(--v-theme-on-surface), 0.5);
}

.portal-attachment-chip-label {
  color: rgb(var(--v-theme-on-surface));
  font-weight: 500;
  overflow-wrap: anywhere;
}

.portal-attachment-chip.is-broken .portal-attachment-chip-label {
  color: inherit;
}

.portal-attachment-chip-meta {
  color: rgba(var(--v-theme-on-surface), 0.55);
  white-space: nowrap;
}
</style>
