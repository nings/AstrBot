import { computed, ref } from 'vue';
import portalApi from '@/api/portal';

/**
 * Portal-only analogue of `@/composables/useMediaHandling.ts`.
 *
 * Before writing this file, `@/composables/useMediaHandling.ts` (and
 * `@/composables/useRecording.ts`) were checked for operator-dashboard reuse
 * by grepping every `.vue`/`.ts` consumer: both are directly imported by the
 * *operator* chat surfaces `components/chat/Chat.vue` and
 * `components/chat/StandaloneChat.vue`, and `useMediaHandling.ts` calls the
 * core `/api/v1/files/*` endpoints via the Bearer-token `fileApi` client
 * (`@/api/v1`). Editing that shared file to point at the portal's
 * cookie+CSRF `/api/v1/portal/files` route instead would either (a) force it
 * to juggle two unrelated auth models behind one API, silently regressing
 * the operator dashboard's own upload widget, or (b) require a branch on
 * "which surface is this", which is exactly the kind of shared-file
 * entanglement design/portal-multiuser-plan.md's Phase 2 deliverables list
 * calls out ("若原文件被运营端同时使用则新建 usePortalMediaHandling.ts 而非直接改动共享
 * 文件，避免破坏运营端行为"). It is also not merely a style preference: core's
 * own file-serving endpoints must never be reachable from the portal SPA at
 * all (plan section 4 checklist item 4 / this phase's verified ground
 * truth), so reusing `useMediaHandling.ts` unmodified would be a functional
 * regression, not just a maintenance risk. This file is therefore new and
 * independent -- `useMediaHandling.ts`/`useRecording.ts` are untouched.
 *
 * Voice recording (`useRecording.ts`'s concern) is out of this file's scope:
 * the assignment for Phase 2's upload experience is drag/drop + click-to-
 * select + clipboard-paste of images, not a microphone composer control.
 */

// ---------------------------------------------------------------------------
// Client-side mirror of astrbot/portal/config.py's upload policy
// ---------------------------------------------------------------------------
//
// These constants exist ONLY to fail fast in the browser before a network
// round trip. The server
// (astrbot/portal/services/attachment_service.py, reading
// astrbot.portal.config.get_portal_settings()) is the sole authority and
// re-validates every one of these independently -- this is a UX
// optimization, not a security boundary. There is no shared-config
// endpoint the client can read these from yet (see `PortalSettings`'
// own "no admin-configurable override" note), so if the server-side
// defaults ever change, these must be updated here by hand to match.

/** Mirrors `PortalSettings.max_upload_file_bytes` (20 MiB per file). */
export const PORTAL_MAX_UPLOAD_FILE_BYTES = 20 * 1024 * 1024;

/** Mirrors `PortalSettings.allowed_upload_mime_types` verbatim. */
export const PORTAL_ALLOWED_UPLOAD_MIME_TYPES: ReadonlySet<string> = new Set([
  // Images
  'image/jpeg',
  'image/png',
  'image/webp',
  'image/gif',
  // Audio / voice messages
  'audio/mpeg',
  'audio/mp4',
  'audio/wav',
  'audio/x-wav',
  'audio/webm',
  'audio/ogg',
  'audio/aac',
  // Video
  'video/mp4',
  'video/webm',
  'video/quicktime',
  // Plain documents
  'application/pdf',
  'text/plain',
  'text/csv',
  'application/json',
]);

/** Extension hints for the native file-picker's `accept` attribute -- a UX
 * nicety only. The MIME allowlist above (checked against `file.type`) is
 * the actual client-side gate, and the server is the real one. */
export const PORTAL_UPLOAD_ACCEPT_ATTR =
  '.jpg,.jpeg,.png,.webp,.gif,.mp3,.m4a,.wav,.webm,.ogg,.aac,.mp4,.mov,.pdf,.txt,.csv,.json,' +
  Array.from(PORTAL_ALLOWED_UPLOAD_MIME_TYPES).join(',');

export type PortalAttachType = 'image' | 'record' | 'video' | 'file';
export type PortalAttachmentStatus = 'pending' | 'uploading' | 'done' | 'error';

export type PortalUploadErrorCode =
  | 'too_large'
  | 'unsupported_type'
  | 'quota_exceeded'
  | 'network'
  | 'aborted'
  | 'unknown';

export interface PortalUploadError {
  code: PortalUploadErrorCode;
  /** Raw server-provided detail string, when there was one (415/400). */
  serverMessage?: string;
  /** Remaining daily quota in bytes -- only populated for `quota_exceeded`,
   * from `attachment_service.QuotaExceededError.remaining_bytes` via the
   * 429 response body's `remaining_quota_bytes`. */
  remainingQuotaBytes?: number;
}

export interface PortalPendingAttachment {
  localId: string;
  file: File;
  name: string;
  size: number;
  declaredMimeType: string;
  /** Best-effort client guess (from the declared MIME prefix) used only for
   * icon/preview selection before the server's authoritative classification
   * comes back; overwritten with the server's own `attach_type` on success. */
  attachType: PortalAttachType;
  status: PortalAttachmentStatus;
  /** 0-100. Only meaningful while `status === 'uploading'`. */
  progress: number;
  /** Object URL for image thumbnails. Revoked on removal/reset. */
  previewUrl?: string;
  attachmentId?: string;
  error?: PortalUploadError;
}

/**
 * The exact multimodal part shape `astrbot/portal/api/chat_routes.py`'s
 * `MessagePart` model accepts: `type` + `attachment_id` + `filename` for a
 * media part, `type: 'plain'` + `text` for the text part. Deliberately has
 * no `path`/`stored_filename` field -- the server whitelists/strips those
 * unconditionally (`core_facade`'s part-sanitizer), and sending them would
 * misleadingly imply the client controls a server-side path.
 */
export interface PortalMessagePart {
  type: PortalAttachType | 'plain';
  text?: string;
  attachment_id?: string;
  filename?: string;
}

function guessAttachType(mimeType: string): PortalAttachType {
  if (mimeType.startsWith('image/')) return 'image';
  if (mimeType.startsWith('audio/')) return 'record';
  if (mimeType.startsWith('video/')) return 'video';
  return 'file';
}

function makeLocalId(): string {
  return crypto.randomUUID?.() ?? `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/** Small, dependency-free byte formatter (the repo has no shared one --
 * every consumer, e.g. `StorageCleanupPanel.vue`/`BackupDialog.vue`, defines
 * its own local copy; this follows that existing convention). */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unitIndex]}`;
}

/**
 * Client-side pre-validation mirroring the server's size cap and MIME
 * allowlist, so an obviously-rejected file fails fast without a round trip.
 * Returns `null` when the file passes. This is a UX optimization, never a
 * security boundary -- `attachment_service.py` re-checks both
 * independently and is the sole authority.
 */
export function validateFileClientSide(file: File): PortalUploadError | null {
  if (file.size > PORTAL_MAX_UPLOAD_FILE_BYTES) {
    return { code: 'too_large' };
  }
  // An empty `file.type` means the browser could not determine a MIME type
  // (happens for some extensions/platforms) -- don't block on that
  // ambiguity; let the server's declared-Content-Type check be the one
  // that decides, rather than risk a false-positive client-side rejection.
  if (file.type && !PORTAL_ALLOWED_UPLOAD_MIME_TYPES.has(file.type)) {
    return { code: 'unsupported_type' };
  }
  return null;
}

function parseUploadError(err: any): PortalUploadError {
  const status = err?.response?.status;
  const detail = err?.response?.data?.detail;

  if (status === 429) {
    // Shape from file_routes.py: { message: string, remaining_quota_bytes: number }
    const remaining =
      detail && typeof detail === 'object' ? Number(detail.remaining_quota_bytes) : undefined;
    return {
      code: 'quota_exceeded',
      remainingQuotaBytes: Number.isFinite(remaining) ? remaining : undefined,
      serverMessage: detail && typeof detail === 'object' ? String(detail.message ?? '') : undefined,
    };
  }
  if (status === 415) {
    return { code: 'unsupported_type', serverMessage: typeof detail === 'string' ? detail : undefined };
  }
  if (status === 400) {
    return { code: 'too_large', serverMessage: typeof detail === 'string' ? detail : undefined };
  }
  if (!err?.response) {
    return { code: 'network' };
  }
  return { code: 'unknown', serverMessage: typeof detail === 'string' ? detail : undefined };
}

/**
 * Portal upload-attachment composable.
 *
 * Owns the pending-attachment list for one composer instance: client-side
 * validation, upload with per-file progress against
 * `POST /api/v1/portal/files` (CSRF header attached automatically by
 * `portalApi`'s interceptor), and the exact multimodal payload shape the
 * send endpoint expects via `toMessageParts`.
 */
export function usePortalMediaHandling() {
  const attachments = ref<PortalPendingAttachment[]>([]);
  const abortControllers = new Map<string, AbortController>();

  const isUploading = computed(() => attachments.value.some((a) => a.status === 'uploading'));
  const hasReadyAttachments = computed(() => attachments.value.some((a) => a.status === 'done'));

  function revokePreview(attachment: PortalPendingAttachment) {
    if (attachment.previewUrl) {
      URL.revokeObjectURL(attachment.previewUrl);
    }
  }

  async function uploadOne(attachment: PortalPendingAttachment) {
    // `attachment` here is the reactive array element itself (the caller
    // passes `attachments.value[i]`, not the pre-push raw object) -- Vue's
    // reactivity only triggers view updates for mutations that go through
    // the reactive proxy, so every status/progress write below must be
    // applied to that same reference, never to a detached plain object.
    const controller = new AbortController();
    abortControllers.set(attachment.localId, controller);
    attachment.status = 'uploading';
    attachment.progress = 0;

    const formData = new FormData();
    formData.append('file', attachment.file);

    try {
      const response = await portalApi.post('/files', formData, {
        signal: controller.signal,
        onUploadProgress: (evt) => {
          if (!evt.total) return;
          attachment.progress = Math.round((evt.loaded / evt.total) * 100);
        },
      });
      const data = response.data as {
        attachment_id: string;
        filename: string;
        attach_type: PortalAttachType;
        mime_type: string;
      };
      attachment.attachmentId = data.attachment_id;
      attachment.attachType = data.attach_type;
      attachment.status = 'done';
      attachment.progress = 100;
    } catch (err: any) {
      if (controller.signal.aborted) {
        attachment.status = 'error';
        attachment.error = { code: 'aborted' };
        return;
      }
      attachment.status = 'error';
      attachment.error = parseUploadError(err);
    } finally {
      abortControllers.delete(attachment.localId);
    }
  }

  /** Validate and stage one or more files, kicking off an upload
   * immediately for each one that passes client-side validation. Files that
   * fail validation are still added to the list, in an `error` state, so
   * the user gets visible feedback instead of the file silently vanishing. */
  function addFiles(files: FileList | File[]) {
    for (const file of Array.from(files)) {
      const attachType = guessAttachType(file.type);
      const pending: PortalPendingAttachment = {
        localId: makeLocalId(),
        file,
        name: file.name || 'file',
        size: file.size,
        declaredMimeType: file.type,
        attachType,
        status: 'pending',
        progress: 0,
        previewUrl: attachType === 'image' ? URL.createObjectURL(file) : undefined,
      };

      const clientError = validateFileClientSide(file);
      attachments.value.push(pending);
      // Re-read the just-pushed element from the reactive array so every
      // later mutation goes through Vue's reactive proxy (see the note in
      // `uploadOne`).
      const reactiveEntry = attachments.value[attachments.value.length - 1];

      if (clientError) {
        reactiveEntry.status = 'error';
        reactiveEntry.error = clientError;
        continue;
      }
      void uploadOne(reactiveEntry);
    }
  }

  /** Remove-before-send: aborts any in-flight upload for this attachment,
   * revokes its preview URL, and drops it from the pending list. */
  function removeAttachment(localId: string) {
    const index = attachments.value.findIndex((a) => a.localId === localId);
    if (index === -1) return;
    abortControllers.get(localId)?.abort();
    abortControllers.delete(localId);
    const [removed] = attachments.value.splice(index, 1);
    revokePreview(removed);
  }

  /** Clears all pending attachments (called after a successful send, or
   * when the composer is otherwise reset). Aborts anything still uploading. */
  function reset() {
    for (const controller of abortControllers.values()) controller.abort();
    abortControllers.clear();
    attachments.value.forEach(revokePreview);
    attachments.value = [];
  }

  /**
   * Builds the exact `message` payload the send endpoint expects. Returns a
   * plain trimmed string when there are no successfully-uploaded
   * attachments (unchanged from the pre-Phase-2 wire shape), or the
   * multimodal list form otherwise: one part per successfully uploaded
   * attachment (`type` + `attachment_id` + `filename` only -- never
   * `path`/`stored_filename`), followed by a trailing `plain` part when
   * there is any text. Attachments that failed or are still uploading are
   * silently excluded -- callers are expected to gate sending on
   * `isUploading` beforehand so this exclusion should only ever apply to
   * `error` entries the user chose not to remove.
   */
  function toMessageParts(text: string): string | PortalMessagePart[] {
    const trimmed = text.trim();
    const ready = attachments.value.filter((a) => a.status === 'done' && a.attachmentId);
    if (ready.length === 0) {
      return trimmed;
    }
    const parts: PortalMessagePart[] = ready.map((a) => ({
      type: a.attachType,
      attachment_id: a.attachmentId as string,
      filename: a.name,
    }));
    if (trimmed) {
      parts.push({ type: 'plain', text: trimmed });
    }
    return parts;
  }

  /** Clipboard paste of images. Only intercepts the paste event when it
   * actually carried image data -- otherwise the browser's default
   * text-paste behaviour in the composer's textarea proceeds untouched. */
  function handlePaste(event: ClipboardEvent) {
    const items = event.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (let i = 0; i < items.length; i += 1) {
      if (items[i].type.startsWith('image/')) {
        const file = items[i].getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length) {
      event.preventDefault();
      addFiles(files);
    }
  }

  return {
    attachments,
    isUploading,
    hasReadyAttachments,
    addFiles,
    removeAttachment,
    reset,
    toMessageParts,
    handlePaste,
  };
}
