<template>
  <div
    class="portal-composer"
    :class="{ 'is-disabled': disabled, 'is-dark': isDark }"
  >
    <UploadDropzone
      ref="dropzoneRef"
      :disabled="disabled"
      @uploading-change="onUploadingChange"
      @ready-change="onReadyChange"
    >
      <div class="composer-surface" :class="{ 'is-multiline': isMultiline }">
        <v-btn
          class="composer-btn attach-btn"
          icon="mdi-plus"
          variant="outlined"
          :disabled="disabled"
          :title="t('portal.upload.attach')"
          :aria-label="t('portal.upload.attach')"
          @click="dropzoneRef?.openFilePicker()"
        />

        <div class="composer-field">
          <textarea
            ref="textareaRef"
            v-model="draft"
            class="composer-textarea"
            :placeholder="placeholder"
            :disabled="disabled"
            rows="1"
            @keydown="handleKeydown"
            @input="autoResize"
            @paste="onPaste"
          />
        </div>

        <div class="composer-actions">
          <span v-if="hintText" class="composer-hint">{{ hintText }}</span>
          <v-btn
            v-if="isGenerating"
            class="composer-btn stop-btn"
            icon="mdi-stop"
            variant="flat"
            @click="emit('stop')"
          />
          <v-btn
            v-else
            class="composer-btn send-btn"
            icon="mdi-arrow-up"
            variant="flat"
            :disabled="disabled || !canSend || isUploading"
            @click="submit"
          />
        </div>
      </div>
    </UploadDropzone>
  </div>
</template>

<script setup lang="ts">
// A consumer-grade message composer for the portal chat surface.
//
// Deliberately NOT forked from `@/components/chat/ChatInput.vue`: that
// component is 1700+ lines and directly imports the three admin-only
// modules the ESLint boundary rule forbids here
// (`CommandSuggestion.vue`/`ConfigSelector.vue`/`ProviderModelMenu.vue`),
// plus provider/model-picker UI Phase 1/2 must not ship at all. Rebuilding a
// small, purpose-built composer from scratch is both safer against the
// boundary rule (nothing to accidentally leave behind from a fork) and
// matches the "no provider or model picker" requirement exactly, with no
// dead code to strip. There is no markdown/code/math rendering in a
// composer, so this file has no reuse obligation toward the shared
// renderer.
//
// Phase 2 adds the real upload affordance Phase 1 deliberately shipped
// without: an attach button plus `UploadDropzone.vue` (drag-and-drop +
// click-to-select + clipboard paste, per-file progress, client-side
// pre-validation, thumbnail/filename chips, remove-before-send -- see that
// component's own header for the full design). `UploadDropzone` wraps this
// component's own textarea/actions via its default slot so the whole
// composer surface becomes a drop target, and owns the pending-attachment
// list via `usePortalMediaHandling` -- this component only reacts to two
// events (`uploading-change`/`ready-change`) and calls two exposed methods
// (`toMessageParts`/`reset`) on send; it never touches attachment state
// directly.
//
// `send` now emits `string | PortalMessagePart[]` instead of a bare
// `string` -- a plain string when there are no attachments (unchanged wire
// shape from Phase 1), or the multimodal list form `SendMessageRequest`
// expects when there is at least one successfully uploaded attachment. Media
// parts carry only `attachment_id` and a display `filename` -- never
// `path`/`stored_filename` (the server strips/ignores those unconditionally
// regardless; this composer simply never produces them in the first
// place).
import { computed, nextTick, ref, watch } from "vue";
import { useModuleI18n } from "@/i18n/composables";
import UploadDropzone from "@/components/portal/UploadDropzone.vue";
import type { PortalMessagePart } from "@/composables/usePortalMediaHandling";

const { tm: t } = useModuleI18n("features/auth");

const props = withDefaults(
  defineProps<{
    isGenerating?: boolean;
    disabled?: boolean;
    placeholder?: string;
    hintText?: string;
    isDark?: boolean;
  }>(),
  {
    isGenerating: false,
    disabled: false,
    // Matches the operator ChatUI's own placeholder ("想问什么都可以") rather
    // than spelling out the keyboard shortcuts: the shortcut hint belongs in
    // `hintText`, and an invitation reads better than an instruction here.
    placeholder: "想问什么都可以",
    hintText: "",
    isDark: false,
  },
);

const emit = defineEmits<{
  send: [message: string | PortalMessagePart[]];
  stop: [];
}>();

const draft = ref("");
const textareaRef = ref<HTMLTextAreaElement | null>(null);
const dropzoneRef = ref<InstanceType<typeof UploadDropzone> | null>(null);

// Mirrors of UploadDropzone's aggregate attachment state, kept in sync via
// its `uploading-change`/`ready-change` events rather than read back through
// its exposed instance -- see UploadDropzone.vue's own comment on why
// `defineExpose` refs are not auto-unwrapped on the parent side, which makes
// an explicit event contract the more robust choice here.
const isUploading = ref(false);
const hasReadyAttachments = ref(false);

function onUploadingChange(uploading: boolean) {
  isUploading.value = uploading;
}

function onReadyChange(hasReady: boolean) {
  hasReadyAttachments.value = hasReady;
}

// A message is sendable with just an image/file and no text, so this can't
// be a bare `draft.trim().length > 0` check anymore.
const canSend = computed(
  () => draft.value.trim().length > 0 || hasReadyAttachments.value,
);

const MAX_HEIGHT_PX = 220;

// One-line height threshold. Below it the surface stays a pill (999px radius,
// actions on the same row); above it the surface relaxes into the rounded-rect
// shape with the field on its own row -- mirroring the operator ChatUI's
// `.input-container.is-multiline` treatment.
const SINGLE_LINE_MAX_PX = 30;
const isMultiline = ref(false);

function autoResize() {
  const el = textareaRef.value;
  if (!el) return;
  el.style.height = "auto";
  const next = Math.min(el.scrollHeight, MAX_HEIGHT_PX);
  el.style.height = `${next}px`;
  isMultiline.value = next > SINGLE_LINE_MAX_PX;
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key !== "Enter") return;
  // Shift+Enter (or Ctrl/Cmd+Enter) inserts a newline; plain Enter sends.
  if (event.shiftKey || event.ctrlKey || event.metaKey) return;
  // Allow IME composition (e.g. Chinese/Japanese input) to finish before
  // treating Enter as "send".
  if (event.isComposing) return;
  event.preventDefault();
  submit();
}

function onPaste(event: ClipboardEvent) {
  dropzoneRef.value?.handlePaste(event);
}

function submit() {
  if (props.disabled || props.isGenerating || isUploading.value) return;
  const text = draft.value.trim();
  const payload = dropzoneRef.value?.toMessageParts(text) ?? text;
  const isEmpty = typeof payload === "string" ? !payload : payload.length === 0;
  if (isEmpty) return;

  emit("send", payload);
  draft.value = "";
  dropzoneRef.value?.reset();
  nextTick(autoResize);
}

watch(
  () => props.isGenerating,
  (generating) => {
    if (!generating) {
      nextTick(() => textareaRef.value?.focus());
    }
  },
);

defineExpose({ focus: () => textareaRef.value?.focus() });
</script>

<style scoped>
.portal-composer {
  width: 100%;
}

/* Geometry, colour and shadow values below are taken from the operator
   ChatUI's `.input-container` (`@/components/chat/ChatInput.vue`) so the two
   composers read as one product. They are duplicated rather than imported:
   that component is 1700+ lines and pulls in three admin-only modules the
   portal's ESLint boundary forbids, so the design is replicated, not reused. */
.composer-surface {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  grid-template-areas: "left field right";
  align-items: center;
  column-gap: 10px;
  width: 100%;
  min-height: 64px;
  padding: 6px 12px 6px 14px;
  border: 1px solid #f0f0f0;
  border-radius: 999px;
  background: #fff;
  box-shadow: 0 8px 22px rgba(0, 0, 0, 0.08);
  transition:
    border-radius 0.16s ease,
    padding 0.16s ease;
}

.composer-surface.is-multiline {
  grid-template-areas:
    "field field field"
    "left . right";
  row-gap: 10px;
  align-items: end;
  padding: 16px 20px 14px;
  border-radius: 34px;
}

.portal-composer.is-dark .composer-surface {
  border-color: rgba(255, 255, 255, 0.12);
  background: #2d2d2d;
  box-shadow: none;
}

.composer-field {
  grid-area: field;
  display: flex;
  align-items: center;
  min-width: 0;
  min-height: 52px;
}

.composer-surface.is-multiline .composer-field {
  align-items: flex-start;
  min-height: 0;
}

.composer-textarea {
  width: 100%;
  min-height: 24px;
  max-height: 220px;
  border: 0;
  outline: none;
  resize: none;
  overflow-y: auto;
  background: transparent;
  color: rgb(var(--v-theme-on-surface));
  font: inherit;
  font-size: 15px;
  line-height: 1.5;
}

.composer-textarea::placeholder {
  color: rgba(var(--v-theme-on-surface), 0.45);
}

.attach-btn {
  grid-area: left;
}

.composer-actions {
  grid-area: right;
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 8px;
}

.composer-hint {
  color: var(--chat-muted, rgba(var(--v-theme-on-surface), 0.5));
  font-size: 11px;
  white-space: nowrap;
}

/* Circular 40px controls, matching the operator composer's button sizing. */
.composer-btn {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  min-width: 40px;
  border-radius: 50%;
}

.attach-btn {
  border-color: rgba(var(--v-border-color), 0.24) !important;
  color: #6f6f6f !important;
}

.portal-composer.is-dark .attach-btn {
  border-color: rgba(255, 255, 255, 0.16) !important;
  color: rgba(255, 255, 255, 0.72) !important;
}

.send-btn {
  background: #5594c6 !important;
  color: #fff !important;
}

.send-btn:hover:not(:disabled) {
  background: #4c86b3 !important;
}

.send-btn:disabled {
  background: rgba(85, 148, 198, 0.24) !important;
  color: rgba(255, 255, 255, 0.72) !important;
}

.stop-btn {
  background: rgba(var(--v-theme-error), 0.92) !important;
  color: #fff !important;
}
</style>
