<template>
  <div
    class="portal-upload-dropzone"
    :class="{ 'is-drag-over': isDragOver && !disabled }"
    @dragenter.prevent="onDragEnter"
    @dragover.prevent="onDragOver"
    @dragleave.prevent="onDragLeave"
    @drop.prevent="onDrop"
  >
    <div v-if="attachments.length" class="attachment-chips" role="list">
      <div
        v-for="a in attachments"
        :key="a.localId"
        class="attachment-chip"
        :class="`is-${a.status}`"
        role="listitem"
      >
        <div v-if="a.attachType === 'image' && a.previewUrl" class="chip-thumb">
          <img :src="a.previewUrl" :alt="a.name" />
        </div>
        <div v-else class="chip-icon" :style="{ color: presentationFor(a).color }">
          <v-icon :icon="presentationFor(a).icon" size="20" />
        </div>

        <div class="chip-body">
          <span class="chip-name" :title="a.name">{{ a.name }}</span>
          <span v-if="a.status === 'error'" class="chip-error">{{ errorText(a) }}</span>
          <span v-else-if="a.status === 'uploading'" class="chip-status">
            {{ t('portal.upload.uploadingProgress', { percent: a.progress }) }}
          </span>
          <span v-else-if="a.status === 'done'" class="chip-status chip-status-done">
            {{ formatBytes(a.size) }}
          </span>
        </div>

        <v-progress-linear
          v-if="a.status === 'uploading'"
          :model-value="a.progress"
          height="2"
          color="primary"
          class="chip-progress-bar"
        />

        <button
          type="button"
          class="chip-remove"
          :aria-label="t('portal.upload.remove')"
          :title="t('portal.upload.remove')"
          @click="removeAttachment(a.localId)"
        >
          <v-icon icon="mdi-close" size="14" />
        </button>
      </div>
    </div>

    <div v-if="isDragOver && !disabled" class="drop-overlay">
      <v-icon icon="mdi-tray-arrow-down" size="26" />
      <span>{{ t('portal.upload.dropHint') }}</span>
    </div>

    <input
      ref="fileInputRef"
      type="file"
      multiple
      class="hidden-file-input"
      :accept="acceptAttr"
      :disabled="disabled"
      @change="onFileInputChange"
    />

    <slot />
  </div>
</template>

<script setup lang="ts">
// Drag-and-drop + click-to-select + clipboard-paste upload surface for the
// portal composer.
//
// This component wraps the composer's textarea/actions (via the default
// slot) so the *entire* composer surface is a valid drop target, while
// rendering the pending-attachment chip row above whatever the slot
// contains -- matching PortalComposer.vue's "pending attachments shown
// above the input" requirement without either component needing to know
// the other's internal layout.
//
// State (the pending-attachment list, per-file upload progress, client-side
// validation, the exact send-payload shape) lives entirely in
// `usePortalMediaHandling`, a portal-only composable -- see that file's own
// header comment for why it is not a modification of the shared
// `@/composables/useMediaHandling.ts` (confirmed by grep to be used by the
// *operator* dashboard's Chat.vue/StandaloneChat.vue before writing this).
//
// Icon/color/label presentation for non-image attachment chips reuses
// `@/components/chat/attachmentPresentation` -- a dependency-free pure
// module (no imports of its own) already reused the same way by
// PortalChatShell.vue for rendering attachment chips in message history, so
// this keeps the pending-chip look consistent with the sent-message chip
// look for free.
import { onBeforeUnmount, ref, watch } from "vue";
import { useModuleI18n } from "@/i18n/composables";
import { attachmentPresentation } from "@/components/chat/attachmentPresentation";
import {
  usePortalMediaHandling,
  formatBytes,
  PORTAL_UPLOAD_ACCEPT_ATTR,
  PORTAL_MAX_UPLOAD_FILE_BYTES,
  type PortalPendingAttachment,
  type PortalMessagePart,
} from "@/composables/usePortalMediaHandling";

const { tm: t } = useModuleI18n("features/auth");

const props = withDefaults(
  defineProps<{
    disabled?: boolean;
  }>(),
  {
    disabled: false,
  },
);

const emit = defineEmits<{
  "uploading-change": [uploading: boolean];
  "ready-change": [hasReady: boolean];
}>();

const {
  attachments,
  isUploading,
  hasReadyAttachments,
  addFiles,
  removeAttachment,
  reset,
  toMessageParts,
  handlePaste,
} = usePortalMediaHandling();

const fileInputRef = ref<HTMLInputElement | null>(null);
const isDragOver = ref(false);
let dragDepth = 0;

const acceptAttr = PORTAL_UPLOAD_ACCEPT_ATTR;

// Notify the parent composer every time the aggregate "any file still
// uploading" state changes, so it can disable Send. A `watch` (rather than
// calling this by hand after every mutating action -- add/remove/reset)
// guarantees the parent never misses a transition regardless of which
// interaction path (drag, click, paste, remove) caused it.
watch(
  isUploading,
  (uploading) => emit("uploading-change", uploading),
  { immediate: true },
);

// Same reasoning for "is there at least one successfully uploaded
// attachment" -- the parent needs this (a message with only an image and
// no text is a valid send) but reading it back out through an exposed
// `computed` on the child's public instance would require an extra,
// easy-to-forget `.value` unwrap (defineExpose does not auto-unwrap refs
// the way the normal render-context proxy does), so an event is the more
// robust contract.
watch(
  hasReadyAttachments,
  (hasReady) => emit("ready-change", hasReady),
  { immediate: true },
);

function presentationFor(a: PortalPendingAttachment) {
  return attachmentPresentation({ type: a.attachType, filename: a.name });
}

function errorText(a: PortalPendingAttachment): string {
  const err = a.error;
  if (!err) return "";
  switch (err.code) {
    case "too_large":
      return t("portal.upload.errors.tooLarge", {
        max: formatBytes(PORTAL_MAX_UPLOAD_FILE_BYTES),
      });
    case "unsupported_type":
      return t("portal.upload.errors.unsupportedType");
    case "quota_exceeded":
      return typeof err.remainingQuotaBytes === "number"
        ? t("portal.upload.errors.quotaExceeded", {
            remaining: formatBytes(Math.max(0, err.remainingQuotaBytes)),
          })
        : t("portal.upload.errors.quotaExceededUnknown");
    case "network":
      return t("portal.upload.errors.network");
    case "aborted":
      return t("portal.upload.errors.aborted");
    default:
      return err.serverMessage || t("portal.upload.errors.unknown");
  }
}

function onDragEnter(event: DragEvent) {
  if (props.disabled) return;
  if (!event.dataTransfer?.types?.includes("Files")) return;
  dragDepth += 1;
  isDragOver.value = true;
}

function onDragOver(event: DragEvent) {
  if (props.disabled) return;
  if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
}

function onDragLeave() {
  if (dragDepth > 0) dragDepth -= 1;
  if (dragDepth === 0) isDragOver.value = false;
}

function onDrop(event: DragEvent) {
  dragDepth = 0;
  isDragOver.value = false;
  if (props.disabled) return;
  const files = event.dataTransfer?.files;
  if (files?.length) {
    addFiles(files);
  }
}

function openFilePicker() {
  if (props.disabled) return;
  fileInputRef.value?.click();
}

function onFileInputChange(event: Event) {
  const input = event.target as HTMLInputElement;
  if (input.files?.length) {
    addFiles(input.files);
  }
  // Reset so selecting the exact same file again still fires `change`.
  input.value = "";
}

function onPaste(event: ClipboardEvent) {
  if (props.disabled) return;
  handlePaste(event);
}

onBeforeUnmount(() => {
  reset();
});

defineExpose({
  openFilePicker,
  handlePaste: onPaste,
  toMessageParts: (text: string): string | PortalMessagePart[] => toMessageParts(text),
  reset,
});
</script>

<style scoped>
.portal-upload-dropzone {
  position: relative;
  width: 100%;
}

.portal-upload-dropzone.is-drag-over {
  outline: 2px dashed rgba(var(--v-theme-primary), 0.6);
  outline-offset: 2px;
  border-radius: 22px;
}

.hidden-file-input {
  display: none;
}

.drop-overlay {
  position: absolute;
  inset: 0;
  z-index: 5;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border-radius: 22px;
  background: rgba(var(--v-theme-surface), 0.92);
  color: rgb(var(--v-theme-primary));
  font-size: 13px;
  pointer-events: none;
}

.attachment-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}

.attachment-chip {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 220px;
  padding: 6px 24px 6px 6px;
  border-radius: 12px;
  border: 1px solid rgba(var(--v-border-color), 0.18);
  background: rgb(var(--v-theme-surface));
  overflow: hidden;
}

.attachment-chip.is-error {
  border-color: rgba(var(--v-theme-error), 0.5);
}

.chip-thumb {
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  overflow: hidden;
  background: rgba(var(--v-border-color), 0.1);
}

.chip-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.chip-icon {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: rgba(var(--v-border-color), 0.1);
}

.chip-body {
  display: flex;
  flex-direction: column;
  min-width: 0;
  line-height: 1.25;
}

.chip-name {
  font-size: 12px;
  color: rgb(var(--v-theme-on-surface));
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 140px;
}

.chip-status {
  font-size: 11px;
  color: rgba(var(--v-theme-on-surface), 0.55);
}

.chip-status-done {
  color: rgba(var(--v-theme-success), 0.8);
}

.chip-error {
  font-size: 11px;
  color: rgb(var(--v-theme-error));
  white-space: normal;
  max-width: 160px;
}

.chip-progress-bar {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
}

.chip-remove {
  position: absolute;
  top: 4px;
  right: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border: none;
  border-radius: 50%;
  background: rgba(var(--v-border-color), 0.16);
  color: rgba(var(--v-theme-on-surface), 0.7);
  cursor: pointer;
  padding: 0;
}

.chip-remove:hover {
  background: rgba(var(--v-theme-error), 0.15);
  color: rgb(var(--v-theme-error));
}
</style>
