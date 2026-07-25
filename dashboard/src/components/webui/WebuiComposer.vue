<template>
  <div class="webui-composer" :class="{ 'is-disabled': disabled }">
    <div class="composer-surface">
      <textarea
        ref="textareaRef"
        v-model="draft"
        class="composer-textarea"
        :placeholder="placeholder"
        :disabled="disabled"
        rows="1"
        @keydown="handleKeydown"
        @input="autoResize"
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
          :disabled="disabled || !canSend"
          @click="submit"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// A consumer-grade message composer for the webui chat surface.
//
// Deliberately NOT forked from `@/components/chat/ChatInput.vue`: that
// component is 1700+ lines and directly imports the three admin-only
// modules the ESLint boundary rule forbids here
// (`CommandSuggestion.vue`/`ConfigSelector.vue`/`ProviderModelMenu.vue`),
// plus file-upload and provider/model-picker UI that Phase 1 must not ship
// at all. Rebuilding a small, purpose-built composer from scratch is both
// safer against the boundary rule (nothing to accidentally leave behind
// from a fork) and matches the explicit "no file-upload affordance" /
// "no provider or model picker" requirements exactly, with no dead code to
// strip. There is no markdown/code/math rendering in a composer, so this
// file has no reuse obligation toward the shared renderer.
import { computed, nextTick, ref, watch } from "vue";

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
  send: [text: string];
  stop: [];
}>();

const draft = ref("");
const textareaRef = ref<HTMLTextAreaElement | null>(null);

const canSend = computed(() => draft.value.trim().length > 0);

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

function submit() {
  if (props.disabled || props.isGenerating) return;
  const text = draft.value.trim();
  if (!text) return;
  emit("send", text);
  draft.value = "";
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
.webui-composer {
  width: 100%;
}

.composer-surface {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  width: 100%;
  padding: 10px 10px 10px 16px;
  border-radius: 22px;
  border: 1px solid rgba(var(--v-border-color), 0.18);
  background: rgb(var(--v-theme-surface));
  transition: border-color 0.15s ease;
}

.webui-composer:not(.is-disabled) .composer-surface:focus-within {
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
</style>
