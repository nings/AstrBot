// Markdown renderer wiring for the end-user webui chat surface.
//
// Deliberately independent of `@/components/chat/chatMarkdownComponents.ts`:
// that file registers its custom node renderers under the markstream-vue
// custom-id `"chat-message"`, which is the operator dashboard's own
// registration namespace. Per the architecture plan (section 3.5) this
// module registers the *same* underlying node renderer components under a
// new, webui-only custom-id (`"webui-chat"`) so the two chat surfaces never
// share a markstream-vue registration namespace, even though in practice
// they run as two entirely separate Vite entry points/bundles.
//
// The node renderer components themselves (RefNode/ThreadNode/HtmlGenUiNode/
// ThemeAwareMarkdownCodeBlock) are reused by direct ES import from the
// existing chat feature -- none of them import any admin-only module
// (ConfigSelector.vue/ProviderModelMenu.vue/CommandSuggestion.vue/Chat.vue/
// ChatMessageList.vue/useSessions.ts), so importing them here does not cross
// the ESLint boundary rule. This file, together with `MarkdownRender` from
// `markstream-vue` (imported directly by `WebuiChatShell.vue`), is the whole
// of the markdown/code/math rendering reuse surface for the webui module --
// no markdown/shiki/katex/mermaid rendering logic is reimplemented anywhere
// in `dashboard/src/components/webui/**`.
import { setCustomComponents } from "markstream-vue";
import "markstream-vue/index.css";
import RefNode from "@/components/chat/message_list_comps/RefNode.vue";
import ThreadNode from "@/components/chat/message_list_comps/ThreadNode.vue";
import HtmlGenUiNode from "@/components/chat/message_list_comps/HtmlGenUiNode.vue";
import ThemeAwareMarkdownCodeBlock from "@/components/shared/ThemeAwareMarkdownCodeBlock.vue";

/** markstream-vue custom-id used by every webui chat markdown render. */
export const WEBUI_CHAT_CUSTOM_ID = "webui-chat";

/** Custom inline/block HTML tags the webui markdown renderer recognizes. */
export const WEBUI_CHAT_MARKDOWN_CUSTOM_TAGS: string[] = ["ref", "html-genui"];

let registered = false;

/**
 * Register the webui chat surface's custom markdown node renderers under
 * `WEBUI_CHAT_CUSTOM_ID`. Safe to call multiple times (e.g. once per
 * mounted message) -- `setCustomComponents` is idempotent for the same id
 * and this guard just avoids redundant calls.
 */
export function registerWebuiChatMarkdownComponents() {
  if (registered) return;
  setCustomComponents(WEBUI_CHAT_CUSTOM_ID, {
    ref: RefNode,
    thread: ThreadNode,
    "html-genui": HtmlGenUiNode,
    code_block: ThemeAwareMarkdownCodeBlock,
  });
  registered = true;
}
