<template>
  <div class="portal-composer" :class="{ 'is-disabled': disabled }">
    <UploadDropzone
      ref="dropzoneRef"
      :disabled="disabled"
      @uploading-change="onUploadingChange"
      @ready-change="onReadyChange"
    >
      <div class="composer-surface">
        <v-btn
          class="composer-btn attach-btn"
          icon="mdi-paperclip"
          size="small"
          variant="text"
          :disabled="disabled"
          :title="t('portal.upload.attach')"
          :aria-label="t('portal.upload.attach')"
          @click="dropzoneRef?.openFilePicker()"
        />

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

        <div class="composer-actions">
          <span v-if="hintText" class="composer-hint">{{ hintText }}</span>
          <v-btn
            v-if="isGenerating"
            class="composer-btn stop-btn"
            icon="mdi-stop"
            size="small"
            variant="flat"
            color="error"
            @click="emit('stop')"
          />
          <v-btn
            v-else
            class="composer-btn send-btn"
            icon="mdi-arrow-up"
            size="small"
            variant="flat"
            color="primary"
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
  }>(),
  {
    isGenerating: false,
    disabled: false,
    placeholder: "输入消息，Enter 发送，Shift+Enter 换行",
    hintText: "",
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

function autoResize() {
  const el = textareaRef.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT_PX)}px`;
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

.composer-surface {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  width: 100%;
  padding: 10px 10px 10px 10px;
  border-radius: 22px;
  border: 1px solid rgba(var(--v-border-color), 0.18);
  background: rgb(var(--v-theme-surface));
  transition: border-color 0.15s ease;
}

.portal-composer:not(.is-disabled) .composer-surface:focus-within {
  border-color: rgba(var(--v-theme-primary), 0.55);
}

.composer-textarea {
  flex: 1 1 auto;
  min-height: 24px;
  max-height: 220px;
  padding: 6px 0;
  border: 0;
  outline: none;
  resize: none;
  overflow-y: auto;
  background: transparent;
  color: rgb(var(--v-theme-on-surface));
  font: inherit;
  line-height: 1.5;
}

.composer-textarea::placeholder {
  color: rgba(var(--v-theme-on-surface), 0.45);
}

.composer-actions {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 8px;
  padding-bottom: 2px;
}

.composer-hint {
  color: rgba(var(--v-theme-on-surface), 0.5);
  font-size: 11px;
  white-space: nowrap;
}

.composer-btn {
  flex-shrink: 0;
}

.attach-btn {
  margin-bottom: 1px;
}
</style>
