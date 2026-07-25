# AstrBot 多用户 C 端聊天前端 —— 最终架构决策与实施路线图

版本：v1.1 作者：首席架构师（综合 P1/P2/P3 三方案 + 三评审视角）
状态：**Phase 1-5 已全部实现**。上文第 1-8 节保留为定案时的原始设计基线；实现过程中的偏差、以及尚未达成的项，一律记录在**第 9 节**，不回改上文。

> **读本文档前请先读第 9 节。** 上文若与代码冲突，以代码 + 第 9 节为准。特别注意第 4 节第 13 条（AGPL §13 源码端点）**尚未实现**，对外提供网络服务前是硬性前置。

---

## 0. 一句话结论

**采用 P2「in-tree portal 模块 + umo 身份代入」方案作为基础架构**，新增 `astrbot/portal/`（后端）与 `dashboard/portal.html` 多入口前端（同仓库、独立构建产物），对 `astrbot/` 核心仅做 **2 处结构性强制、单行、加代码注释可追溯的改动**（外加 1 处默认关闭、可延后的第 3 处）。在此基础上，**从 P1 移植"能力隔离墙"和"网络/路径级隔离"两个思想**，从 P3 移植"启动期 umo 路由健康检查"思想，用以补齐三位评审共同指出的 P2 原始设计中的两个可修复缺陷（provider 越权、无独立能力边界）。P1、P3 均不作为主线，理由见第 7 节。

---

## 1. 结论与推荐架构

### 1.1 为什么是 P2 而不是 P1（尽管两者聚合分打平 7.17）

三位评审对聚合分的贡献方向并不一致：安全评审给 P1>P2>P3，务实工程评审给 P2>P1>P3，产品评审给 P1>P2>P3。聚合分打平掩盖了一个事实——**P1 和 P2 各自的"胜出"都伴随一条评审明确指出的、非空谈的致命缺陷**，必须逐条对照裁决，而不是简单取平均。

P1（独立 BFF 网关）的两条被评审判定为"真实、结构性"的缺陷：
1. **共享身份坍缩**：所有终端用户经 BFF 中转后，在 AstrBot 侧统一坍缩为同一个 `api_key:<key_id>` 身份（因为 AstrBot 的 `require_scope` 对 API Key 认证的返回值就是如此，见 `astrbot/dashboard/api/auth.py:130-137`）。这意味着一旦这把共享 API Key 泄漏、被日志记录、或"AstrBot 端口绝不可从公网直连"这条 P1 自认"全设计中最具杠杆性的控制项"被基础设施误配置击穿，攻击者**同时**拿到全体用户的会话与附件访问权——不是拿到一个用户的权限，是拿到所有用户的权限。这是安全评审明确定性为"fixable but requires becoming more like P2"的缺陷。
2. **前端"复制而非导入"**：P1 明确把 15 个 `dashboard/src/components/chat/*` 文件**复制**进独立仓库/独立 App，而不是通过同仓库直接 import 复用。这与硬约束 #3"最大化复用现有聊天组件"的精神相悖，且是 P1 自己在 `Self-declared weaknesses` 中承认的"持续性维护成本，upstream 改进需要人工搬运，两个代码库会分叉"。

P1 换来的东西——"AstrBot 侧的 chat/file scope 限定 API Key 是代码之外的凭证级防御墙"——是真实优点，但代价是：多一个独立进程/独立数据库/独立 k8s 部署单元（TLS、反向代理、网络隔离策略全部要新建），每条消息和每条 SSE 流多两跳网络，且要重新实现一遍 AstrBot 已经跑通的可续传 pub/sub（`ChatService.build_chat_stream`/`build_chat_run_stream`，见 `astrbot/dashboard/services/chat_service.py:787-888`）。这些不是空谈的运维成本，是务实工程评审具体核算过的。

**关键判断**：P1 的"凭证级防御墙"这个*思想*值得要，但不必靠"整体搬到另一个进程"来获得——可以在 P2 的同进程模块内用一道**代码级能力墙**（见 4.2 节）达成同等效果的运行时后备防线，而不用支付独立服务的运维与两跳延迟成本，也不用被迫复制前端组件。这是本文档"移植 P1 思想、拒绝 P1 整体架构"的核心论证。

### 1.2 为什么不是 P3

P3 声称"零核心改动"，但这个说法本身就是问题：安全评审和务实工程评审**各自独立**发现同一个事实——AstrBot 的 `register_web_api`/插件分发路径（`require_scope(request,'plugin')`，见 `astrbot/dashboard/api/plugins.py`）**没有"匿名可访问"这个概念**，导致 P3 为了让一个全新访客能看到登录页，被迫把一把**必须公开嵌入前端 JS、无法保密**的共享 `plugin` scope API Key 写死在客户端——按 P3 自己的原话，这把 Key 泄漏后"可被重放到任何其他已安装插件的 `registered_web_api` 路由"，这是一个会随着运维给 AstrBot 装的插件越来越多而**持续放大**的攻击面，且 P3 自己提出的解法（上游合并一个可选的 `public=True` route flag PR）不受本项目控制，与其"零核心改动"的核心卖点自相矛盾。此外 P3 的实现成本（MVP 13.5 人周）**高于**其"零核心改动"应该带来的省力承诺——因为插件没有中间件能力，每个写操作 handler 都要手撸 CSRF/限流校验，这是一项持续增长的维护负担。P3 在三个评审维度上全部排名最后（7/10、5.5/10、5.5/10），聚合分 4.67，明显劣于 P1/P2，予以否决。

### 1.3 最终决策

**主架构 = P2**（`astrbot/portal/` in-tree 模块 + `dashboard/portal.html` 多入口前端），**移植修正**：
- 从 P1 移植：① 代码级"能力墙"（内部 facade + import 边界 lint，模拟 P1 API Key scope 限定的效果）；② 显式要求反向代理层做**路径级**网络隔离（公网只准访问 `/api/v1/portal/*` 与 `/portal/*`，`/api/v1/conversations`、`/api/v1/sessions`、`/api/v1/session-groups`、`/api/v1/plugins`、`/api/v1/config`、`/api/v1/api-keys`、`/api/v1/persona` 等管理面路径一律仅限内网/VPN）；③ refresh token 轮换 + 重放检测（P1 明确描述，P2 原文亦隐含但未强调，本文档显式写入验收标准）；④ 永不转发客户端 `selected_provider`/`selected_model` 字段的显式契约。
- 从 P3 移植：umo 路由健康检查——模块启动时校验 `umop_config_router` 是否已把 portal 流量的 umo 前缀路由到一个禁用危险工具/子代理的专用 `AstrBotConfig` profile，未配置则响亮告警（Phase 4 落地，而非仅写 runbook）。
- 从 P2 自曝缺陷直接修复：前端 ESLint `no-restricted-imports` 规则，禁止 `dashboard/src/components/portal/**` 与 `portal-main.ts` 依赖图引入 `ConfigSelector.vue`/`ProviderModelMenu.vue`/`CommandSuggestion.vue`/`Chat.vue`/`ChatMessageList.vue`/`useSessions.ts` 等运营端专属组件（CI 强制，而非仅靠 code review）。

本文档第 4-6 节即该修正后架构的完整规格。

---

## 2. 现状盘点（已验证，不得重造轮子）

### 2.1 后端已有能力

| 能力 | 位置 | 说明 |
|---|---|---|
| Web 聊天完整引擎 | `astrbot/dashboard/services/chat_service.py` | session/thread/checkpoint/regenerate/stop/attachment 全套业务逻辑；`build_chat_stream`/`build_chat_run_stream` 提供可续传 SSE（`run_snapshot` 快照 + revision 递增 fan-out） |
| SSE 契约 | 同上 :824-1046 | 帧格式 `data: {json}\n\n`，心跳 `: heartbeat\n\n` 每 1s；帧类型 `plain/image/record/file/video/end/complete/agent_stats/attachment_saved/message_saved/error` |
| Web Chat 平台适配器 | `astrbot/core/platform/sources/webchat/webchat_adapter.py` | 已经是"每用户 session_id 携带身份"的合成 Web 适配器，umo 形如 `webchat:{type}:webchat!{creator}!{session_id}` |
| 会话所有权检查 | `chat_service.py` 多处 `session.creator != username` | 字符串相等检查，与身份来源无关，只要把真实 user_id 灌进 `creator`/`username`，检查天然生效 |
| Persona 按会话覆盖 | `astrbot/core/persona_mgr.py:75-127` (`resolve_selected_persona`) + `conv_mgr.update_conversation(persona_id=...)` | 已有"每会话选择 persona"的合法挂载点，无需改核心 |
| 知识库检索 | `astrbot/core/tools/knowledge_base_tools.py:24-87` + `sp` scope='umo' key='kb_config' | 检索函数本身无鉴权，鉴权必须在新模块里做 |
| 独立数据库范式 | `astrbot/core/knowledge_base/{models.py,kb_db_sqlite.py}` | 已验证的"自有 MetaData() + 自有 SQLite 文件 + 自有 engine + `metadata.create_all()`"配方，KB 模块正在用，新模块照抄零风险 |
| 密码哈希 | `astrbot/core/utils/auth_password.py` | PBKDF2-SHA256 600k 轮、逐用户随机盐，可直接复用（注意不要复用 `api_key_service.py` 的固定 pepper 哈希） |
| 路由注册唯一合法挂点 | `astrbot/dashboard/api/router.py:35-63` `build_api_router()` | 新增 `router.include_router(...)` 一行即可挂载 `/api/v1/*` 下的新路由 |
| 静态资源注册顺序约束 | `astrbot/dashboard/api/app.py:188-214` | 现有 catch-all 静态路由最后注册且贪婪匹配，第二个 SPA 的静态路由**必须**在其之前注册 |
| Preference/SharedPreferences 通用 KV | `astrbot/core/utils/shared_preferences.py` + `astrbot/core/db/po.py:199-223` | 可安全存放 `portal_jwt_secret`、迁移完成标记等，无需新表无需迁移 |

### 2.2 已确认的单租户假设（不可依赖，必须在新模块内补）

- `AuthContext.scopes=['*']`：任何合法 dashboard JWT 天然拥有全部 scope（`astrbot/dashboard/api/auth.py:170-173`）——新模块必须使用**完全独立**的 JWT 体系，绝不能复用/派生 `config['dashboard']['jwt_secret']`。
- `Attachment` 表（`astrbot/core/db/po.py:340-368`）**无 owner 列**，`GET /api/v1/files/{id}` 无所有权检查——必须在新模块自建影子所有权表。
- `/conversations/*`、`/sessions/*`、`/session-groups/*`（`conversations.py`、`session_management_service.py`）**系统级无按调用者过滤**——这两组路由**永远不能**暴露给终端用户前端。
- `astr_main_agent.py:223-232 _select_provider`：任何 chat-scope 调用方可通过 `selected_provider` 字段任选已配置的 provider，无白名单——新模块的每一次消息发送都必须在服务端剥离/覆盖该字段，绝不透传客户端请求体。
- `SubAgentOrchestrator`（`astrbot/core/subagent_orchestrator.py`）是**全局单一配置**，无 per-umo 过滤点——MVP 用"persona+provider+KB 组合套餐（Agent Bundle）"规避，真正的子代理级选择需要 Phase 4 的可选核心补丁。
- `chat_runs`/`chat_runs_by_session`（`chat_service.py:476-513`）是进程内内存字典，无跨进程/跨重启持久化——继承此限制，MVP 阶段保持单进程部署（与现有 `k8s/astrbot/02-deployment.yaml` 的 `replicas:1` 拓扑一致），不在本项目范围内解决。
- SQLite 单写者（`astrbot/core/db/sqlite.py` WAL + busy_timeout）——见第 6 节风险与缓解。

---

## 3. 目标架构

### 3.1 身份模型

三阶段身份，统一落在新模块自有的 `PortalUser` 表，`user_id`（uuid）是唯一贯穿全链路的"真实身份"字符串：

- **Phase 1（自助注册）**：`PortalUser(auth_method='local')`，密码用 `hash_dashboard_password`/`verify_dashboard_password`（`astrbot/core/utils/auth_password.py`，逐用户随机盐）。
- **Phase 5a（管理员开户）**：新增 `Depends(require_system_scope)`（**原样导入**自 `astrbot/dashboard/api/auth.py`，不做修改）网关的 `.../admin/users` 端点，生成一次性邀请 token，`PortalUser.status='invited'` → 用户设密码后 `'active'`。
- **Phase 5b（SSO/OAuth）**：`PortalOAuthIdentity(provider, provider_subject)` 表在 Phase 1 就建好（空表），Phase 5 接入 authlib 标准 OIDC 授权码流程，要求"已登录本地账号 + 二次确认"才允许绑定，避免静默账号接管。

**会话机制**：
- Access token：15 分钟短期 JWT，`{sub: user_id, sid, exp}`，HS256，签名密钥在模块首次启动时随机生成并存入 `SharedPreferences(scope='global', scope_id='portal', key='jwt_secret')`——**绝不**与 `config['dashboard']['jwt_secret']` 共享或派生。Cookie 名 `astrbot_portal_jwt`（与核心的 `astrbot_dashboard_jwt` 严格区分，防止任何中间件按 cookie 名混淆两者）。
- Refresh token：不透明 256-bit 随机值，仅以 PBKDF2 哈希存于 `PortalSession` 表，**轮换 + 重放检测**——一旦检测到已被消费的 refresh token 被重放，整条 token family 立即吊销（移植自 P1 的显式设计）。Cookie 名 `astrbot_portal_refresh`，Path 限定为仅 refresh 端点。
- CSRF：cookie 承载认证，所有写操作要求非 httpOnly 的 `astrbot_portal_csrf` cookie 值与自定义头 `X-Portal-CSRF-Token` 双提交匹配。

**管理员/终端用户分离**（三层独立生效，单点失效不塌方）：
1. 密钥层：portal JWT 用模块自生成密钥签名，核心 `require_scope`/`require_dashboard_user` 只认 `config['dashboard']['jwt_secret']`，两者互不识别，呈现即 401。
2. 传输层：不同 cookie 名 + 不同 Path 作用域。
3. UI 层：终端用户 SPA 是物理独立的第二构建产物（`dashboard/portal.html`），其依赖图经 ESLint 规则强制不可达运营端组件（见 4.4）。
4. **网络层（本次移植自 P1）**：反向代理仅对公网开放 `/api/v1/portal/*`、`/portal/*` 与静态资源；`/api/v1/conversations`、`/api/v1/sessions`、`/api/v1/session-groups`、`/api/v1/config`、`/api/v1/api-keys`、`/api/v1/plugins`、`/api/v1/persona`、`/api/v1/subagents` 等管理面路径**仅限内网/VPN 访问**——这是本设计相对纯 P2 原始文档新增的显式部署强制项，直接对冲"P2 没有说明白 admin dashboard 需要网络隔离"这一产品评审指出的缺口。

### 3.2 能力墙（本次移植自 P1，修复安全评审对 P2 的头号 fatal flaw）

安全评审对 P2 的核心批评是："同进程模块对核心的调用是纯 Python import，没有任何运行时能力边界；一个 bug 或未来贡献者顺手 import `SessionManagementService`/`ConversationService` 就会绕过所有隔离，而 P1 有 API Key scope 这道代码之外的硬墙。"

修正方案——**内部 Facade + Import 边界强制**：

```
astrbot/portal/services/core_facade.py   # 全模块唯一允许 import 以下核心对象的文件：
    - astrbot.dashboard.services.chat_service.ChatService
    - astrbot.core.conversation_mgr.ConversationManager (conv_mgr)
    - astrbot.core.utils.shared_preferences.SharedPreferences (sp)
    - astrbot.core.persona_mgr.PersonaManager
显式禁止 import：
    - astrbot.dashboard.services.conversation_service.ConversationService
    - astrbot.dashboard.services.session_management_service.SessionManagementService
    - astrbot.dashboard.services.api_key_service.ApiKeyService
    - astrbot.core.subagent_orchestrator.SubAgentOrchestrator（除非走 Phase4.5 白名单接口）
```

`core_facade.py` 对外只暴露**已按 user_id 做所有权收窄**的函数（如 `get_owned_session(user_id, session_id)`），其余 `astrbot/portal/**` 文件一律通过 `core_facade` 间接访问核心状态，不得直接 `import astrbot.dashboard.services.*`。CI 新增一条 `import-linter`（Python 侧，`pyproject.toml` 新增 `[tool.importlinter]` contract）强制此边界，任何 PR 违反直接挂红——这是把 P1"credential 级防御墙"翻译成"代码级防御墙"的具体落地,不需要新增进程或凭证系统。

### 3.3 数据模型（全部在新文件 `astrbot/data/portal/portal.db`，独立 `MetaData()`，零改核心表结构）

| 表 | 关键字段 | 用途 |
|---|---|---|
| `PortalUser` | user_id(uuid pk), email, password_hash, display_name, auth_method, role, status, created_at | 根身份表 |
| `PortalSession` | session_id, user_id fk, refresh_token_hash, family_id, expires_at, revoked_at | 可撤销、可重放检测的 refresh token 存储 |
| `PortalOAuthIdentity` | user_id fk, provider, provider_subject | Phase 5 SSO 身份绑定（Phase 1 建空表，Phase 5 启用） |
| `PortalInvite` | invite_id, token_hash, target_email, expires_at, consumed_at, revoked_at, created_by | Phase 5 管理员开户的一次性邀请。**未在本表最初的规划内**，是 Phase 5 实现时新增的 |
| ~~`PortalPasswordResetToken` / `PortalEmailVerificationToken`~~ | — | **未实现，且短期内不应实现**：两者都以"能发邮件"为前提，而本仓库没有任何邮件设施（无 smtplib/aiosmtplib 使用）。后果是**目前没有密码重置流程**——用户忘记密码只能由运维方通过 `/admin` 重新签发邀请。要真正补上这一环，前置条件是先引入邮件发送能力，那是独立立项 |
| `PortalAttachmentOwner` | attachment_id（字符串关联核心 Attachment.attachment_id，非 FK）, user_id fk | 核心 Attachment 表无 owner 列的唯一补救 |
| `PortalKbVisibility` | kb_id, enabled_for_enduser | 管理员维护的 KB 白名单 |
| `PortalAgentBundle` | bundle_id, display_name, persona_id, provider_id, kb_ids(json) | MVP 对需求(d)的回答：策展好的 persona+provider+KB 组合，而非裸露子代理 |
| `PortalUsageCounter` | user_id, day, message_count, upload_bytes, concurrent_runs | 核心完全没有配额概念，纯新建 |

零改动：`astrbot/core/db/po.py` 不动一行；所有与核心数据的关联，均通过把 `user_id` 字符串灌入核心既有的 `creator`/`username` 自由文本字段实现，与核心现有"无 FK、纯字符串约定"的风格一致。

### 3.4 API 结构（挂载于 `/api/v1/portal/*`，通过 `astrbot/dashboard/api/router.py` 一行 `include_router`）

```
POST   /api/v1/portal/auth/register
POST   /api/v1/portal/auth/login
POST   /api/v1/portal/auth/refresh
POST   /api/v1/portal/auth/logout
GET    /api/v1/portal/auth/me
GET    /api/v1/portal/sessions              # 仅返回 caller 自己的 session（server 端按 user_id 过滤,永不接受client传入的owner)
POST   /api/v1/portal/sessions
GET/DELETE /api/v1/portal/sessions/{id}
POST   /api/v1/portal/sessions/{id}/messages      # SSE，服务端强制剥离 selected_provider/selected_model
GET    /api/v1/portal/runs/{run_id}/stream        # 续传
POST   /api/v1/portal/sessions/{id}/stop
POST   /api/v1/portal/sessions/{id}/messages/{mid}/regenerate
POST   /api/v1/portal/files                       # Phase 2
GET    /api/v1/portal/files/{attachment_id}       # Phase 2, PortalAttachmentOwner 校验后才转发
GET    /api/v1/portal/knowledge-bases             # Phase 3
POST   /api/v1/portal/sessions/{id}/knowledge-base   # Phase 3
GET    /api/v1/portal/agent-bundles               # Phase 4
POST   /api/v1/portal/sessions/{id}/agent-bundle  # Phase 4
GET    /api/v1/portal/source                      # AGPL §13 源码指针，公开无需登录
GET/POST /api/v1/portal/admin/users               # Phase 5a, Depends(require_system_scope) 原样导入
POST   /api/v1/portal/admin/kb-visibility         # Phase 3 admin 侧
POST   /api/v1/portal/admin/agent-bundles         # Phase 4 admin 侧
GET/POST /api/v1/portal/auth/oauth/{provider}/{start,callback}  # Phase 5b
```

### 3.5 前端结构

同一 `dashboard/` 仓库，第二个 Vite 多页面入口（不是合并进现有路由树，不是独立 npm 包/仓库）：

- `dashboard/portal.html` + `dashboard/src/portal-main.ts`：独立 `createApp()` + 独立 Pinia 实例 + 独立 router（`createWebHashHistory`，仅两态守卫：非 `/login`,`/register` 路由一律先 `GET /api/v1/portal/auth/me`）。
- `dashboard/src/components/portal/`：新组件（`LoginPage.vue`、`RegisterPage.vue`、`PortalChatShell.vue`、`PortalComposer.vue`(从 `ChatInput.vue` fork 并物理删除 `CommandSuggestion`/`ConfigSelector`/`ProviderModelMenu` 三处 import，而非隐藏)、`AgentBundlePicker.vue`、`KbSelector.vue`、`UploadDropzone.vue`、`QuotaBanner.vue`、`SourceLinkFooter.vue`）。
- **直接 ES import 复用**（同仓库,非复制）：`MarkdownMessagePart.vue`、`ThreadedMarkdownMessagePart.vue`、`ThemeAwareMarkdownCodeBlock.vue`、`RefNode.vue`、`ThreadNode.vue`、`HtmlGenUiNode.vue`、`ToolCallCard.vue`、`ToolCallItem.vue`、`IPythonToolBlock.vue`、`ReasoningBlock.vue`、`ReasoningTimeline.vue`、`ActionRef.vue`、`BlankLayout.vue`。这是相对 P1"复制 15 个文件"的关键优势：upstream 改进自动继承，零人工搬运。
- `dashboard/src/components/portal/portalMarkdownComponents.ts`（新文件，独立于 `chatMarkdownComponents.ts`）：以新 custom-id `'portal-chat'` 注册上述节点渲染器，避免与运营端 `'chat-message'` id 冲突。
- `dashboard/src/composables/`：`useMessages.ts`/`useMediaHandling.ts`/`useRecording.ts` **adapt**（改指向新 endpoint + cookie 认证替代 localStorage bearer），SSE 解析与指数退避续传逻辑保持不变。
- `dashboard/vite.portal.config.mts`（新文件，复用现有 shiki/mermaid alias）；`package.json` 新增 `build-portal`/`dev:portal` 脚本。
- `dashboard/eslint.config.*`（**MODIFIED**）：新增 `no-restricted-imports` 规则，`src/components/portal/**` 与 `portal-main.ts` 依赖图禁止引用 `ConfigSelector.vue`/`ProviderModelMenu.vue`/`CommandSuggestion.vue`/`Chat.vue`/`ChatMessageList.vue`/`useSessions.ts`/`commandApi`（P2 自曝缺陷的直接修复，CI 强制而非仅靠 review）。
- 后端 `astrbot/portal/api/static_router.py`（新文件）仿照 `static_files.py` 的固定索引白名单 + 安全 catch-all，serve `dist-portal/`，在 `astrbot/dashboard/api/app.py` 中于既有 `static_files_router` **之前** `include_router`。

### 3.6 请求/流式链路

```mermaid
sequenceDiagram
    participant B as Browser (portal SPA)
    participant W as astrbot/portal/api/chat_routes.py
    participant F as core_facade.py (能力墙)
    participant C as ChatService (核心, 复用不改)
    participant Q as webchat_queue_mgr / WebChatAdapter
    participant P as PipelineScheduler / build_main_agent

    B->>W: POST /api/v1/portal/sessions/{id}/messages (cookie+CSRF)
    Note over W: 校验 session 归属 user_id;<br/>服务端剥离 selected_provider/model;<br/>配额检查 (PortalUsageCounter)
    W->>F: send(user_id, session_id, payload)
    F->>C: ChatService.build_chat_stream(username=user_id,...)
    C->>Q: push (user_id, conv_id, payload) 入转发队列
    Q->>P: WebChatAdapter -> AstrMessageEvent(session_id="webchat!{user_id}!{cid}") -> EventBus.dispatch
    P-->>C: run_agent() 产出 MessageChain chunks
    C-->>C: _consume_chat_run 累积并 _publish_chat_run(revision++)
    C-->>W: fan-out 到订阅者队列 (ChatService.build_chat_run_stream, 复用不改)
    W-->>B: StreamingResponse text/event-stream (data: {...}\n\n, : heartbeat 每1s)
    Note over B,W: 断线重连: GET /api/v1/portal/runs/{run_id}/stream<br/>-> build_chat_run_stream(include_snapshot=True)<br/>-> run.username==user_id 所有权校验(复用verbatim)
```

关键点：**全程只有一跳网络（Browser↔dashboard 进程），没有 P1 那样的 Browser↔BFF↔AstrBot 两跳**；SSE 续传直接复用 `ChatService.build_chat_run_stream`（含其 `run.username==auth.username` 所有权检查），零新代码实现可续传语义。

---

## 4. 安全与多租户隔离清单（实现方必须逐条打勾）

以下每一条均对应安全评审报告中对 P1/P2 的具体发现，是本架构对其修复的可验证承诺：

1. **[核心] 每一次消息发送，服务端必须显式忽略/覆盖客户端请求体中的 `selected_provider`/`selected_model`/`selected_stt_provider`/`selected_tts_provider` 字段，只允许从该会话已绑定的 `PortalAgentBundle` 派生 provider_id。** 对应 recon 确认的 `astr_main_agent.py:223-232 _select_provider` 无白名单漏洞。验收：单元测试构造恶意请求体注入任意 `selected_provider`，断言最终传给 `ChatService.build_chat_stream` 的 payload 中该字段已被覆盖为服务端值。
2. **[核心] 能力墙强制**：CI 跑 `import-linter`，任何 `astrbot/portal/**`（除 `core_facade.py` 外）文件 import `ConversationService`/`SessionManagementService`/`ApiKeyService`/`SubAgentOrchestrator` 即失败。
3. **每一个会话/线程/项目相关 endpoint**，处理函数签名中绝不允许出现客户端可控的 `owner`/`username`/`user_id` 字段——一律从已验证的 JWT `sub` claim 派生。CI 增加一条 grep-based lint：路由文件里出现 `request.json()['user_id']`/`Body(..., alias='username')` 这类模式即告警。
4. **附件所有权**：`GET /api/v1/portal/files/{id}` 必须先查 `PortalAttachmentOwner`，未命中或不属于 caller 一律 404（不是 403，避免枚举）。核心 `GET /api/v1/files/{id}` 永不暴露给 portal 前端(前端生成的 API client 不应包含对 `/api/v1/files/` 的任何引用，CI 断言)。
5. **Token 隔离**：`astrbot_portal_jwt`/`astrbot_portal_refresh` 与 `astrbot_dashboard_jwt` 使用不同签名密钥、不同 claim 形状（`sub=uuid` vs `username=str`）、不同 cookie 名与 Path。集成测试：用 portal JWT 打向任意核心 `/api/v1/{conversations,sessions,config}` 端点，断言 401；反向亦然。
6. **网络/路径隔离**：反向代理配置中，以下路径仅允许来自内网/VPN CIDR 的请求：
   - 核心运营面：`/api/v1/conversations*`、`/api/v1/sessions*`、`/api/v1/session-groups*`、`/api/v1/config*`、`/api/v1/api-keys*`、`/api/v1/plugins*`、`/api/v1/persona*`、`/api/v1/subagents*`
   - **`/api/v1/portal/admin/*`** —— portal 自己的运营 CRUD（KB 可见性、Agent Bundle、用户开户与邀请）。**这一条是 Phase 3 引入、Phase 4/5 扩大的，本条清单最初漏写。** 它落在 `/api/v1/portal/*` 前缀下，而该前缀整体是需要公网可达的（终端用户就在这里），所以"把 `/api/v1/portal/*` 开放给公网"这句话必须带上这个例外，否则运维方照做就会把开户、邀请签发、语料授权全部暴露到公网，只剩 `require_system_scope` 一道防线。

   公网只应可达：`/api/v1/portal/*` **减去** `/api/v1/portal/admin/*`，以及 `/portal/*`（SPA 静态资源）。

   部署清单必须包含此反向代理规则的自动化测试（例如从公网出口探测这些路径应返回连接拒绝/403，而非 200），且该测试必须显式覆盖 `/api/v1/portal/admin/*`——它与相邻的终端用户路径只差一个路径段，是最容易被规则写漏的一条。
7. **Refresh token 轮换 + 重放检测**：`PortalSession` 记录 `family_id`；一旦检测到已消费 token 被再次提交，立即吊销整条 family 并强制重新登录。测试：模拟 replay attack，断言吊销生效。
8. **KB/Agent Bundle 选择白名单**：`POST .../sessions/{id}/knowledge-base` 与 `.../agent-bundle` 必须校验 `kb_id`/`bundle_id` 在管理员维护的 `PortalKbVisibility`/`PortalAgentBundle` 表中，umo 由已完成所有权校验的 session 服务端派生，绝不接受客户端传入的 umo 字符串（对应 recon 明确警告 `SharedPreferences` 本身零鉴权）。
9. **umo 路由健康检查（移植自 P3）**：模块 `initialize()` 阶段调用 `umop_config_router` 查询，验证 portal 流量的 umo 前缀（如 `webchat:FriendMessage:webchat!*`）是否路由到一个 `subagent_orchestrator.main_enable=false` 且未挂载危险工具（文件写入/shell/无限制 web-fetch）的专用 config profile；未通过则在启动日志打印 CRITICAL 并可配置为拒绝启动。
10. **配额/限流**：注册/登录/刷新走 IP+邮箱维度的令牌桶限流（复用 `server.py` 的 `_AuthRateLimiter` 形状）+ 失败固定 3 秒 sleep（复用 `auth_service.py` 既有模式）；每用户每日消息数/上传字节数/并发 run 数上限写入 `PortalUsageCounter` 并在发送前检查。
11. **SSRF/Prompt Injection 边界**：上传接口只接受本地 multipart 字节，绝不服务端代发起 URL 抓取；`PortalAgentBundle` 中的 persona/provider 必须由管理员显式审核后加入白名单，公开可选集合默认不包含具备无限制网络访问/文件系统写入能力的工具。
12. **前端 import 边界**：ESLint `no-restricted-imports` 规则覆盖 `dashboard/src/components/portal/**`，CI 阻断任何引入运营端专属组件的 PR。
13. **AGPL §13 源码指针**：`GET /api/v1/portal/source` 公开无需登录，返回当前运行的 AstrBot commit SHA + portal 模块 commit SHA + 对应源码链接，构建时通过 `SOURCE_COMMIT` build arg 注入,前端登录页/页脚常驻可见（即使未登录）。

    > **⚠️ 本条未达成（Phase 1-5 结束时的实际状态）。** `GET /api/v1/portal/source` 这个端点**不存在**——全仓 grep 无任何实现。前端登录页确实有"查看源代码"链接与 AGPL 说明文案，但它指向上游仓库，而不是本次实际运行的 commit。
    >
    > 这是本清单中唯一未达成的条目，且它不是代码质量问题而是**合规问题**：AGPL §13 要求向通过网络使用本服务的人提供**其所实际运行的那份**源码。指向上游 `AstrBotDevs/AstrBot` 不满足这个要求，因为运行的是本分支（含 33k 行新增代码）。
    >
    > 上线前必须补齐：一个公开的 `/api/v1/portal/source` 端点 + 构建期注入 commit SHA + 前端链接指向该端点。如果本分支永不对外提供网络服务（仅内部试验），则此条不适用——但一旦有外部用户访问，它就是硬性前置。

---

## 5. 分阶段实施路线图

> 估算假设：1-2 名工程师团队，人天为工作量估算而非日历时间。所有 Phase 均在 `astrbot/portal/` 与 `dashboard/src/components/portal/`、`dashboard/portal.html` 等新路径内进行，核心改动仅出现在 Phase 1（2 处强制）与 Phase 4（1 处可选延后）。

### Phase 1 —— MVP：登录 + 多会话 + 流式回复 + 按用户历史隔离

**目标**：一个真实用户能注册/登录、创建多个会话、发文本消息拿到流式回复、刷新页面/断线重连不丢失上下文、且完全看不到、摸不到其他用户的任何会话。

**交付物**：
- `astrbot/portal/__init__.py`、`astrbot/portal/config.py`（新）
- `astrbot/portal/db/models.py`（新）：`PortalUser`、`PortalSession`、`PortalOAuthIdentity`(占位空表)、`PortalPasswordResetToken`、`PortalEmailVerificationToken`
- `astrbot/portal/db/engine.py`（新）：仿 `KBSQLiteDatabase` 的独立 engine/metadata
- `astrbot/portal/auth/security.py`（新）：密码哈希（复用 `astrbot/core/utils/auth_password.py`）、JWT mint/verify、cookie helper
- `astrbot/portal/auth/dependencies.py`（新）：`require_portal_user`
- `astrbot/portal/auth/rate_limit.py`（新）：仿 `server.py` `_AuthRateLimiter`
- `astrbot/portal/services/core_facade.py`（新）：能力墙，唯一可 import `ChatService`/`conv_mgr`/`sp` 的文件
- `astrbot/portal/services/session_service.py`（新）
- `astrbot/portal/api/auth_routes.py`（新）
- `astrbot/portal/api/chat_routes.py`（新）：sessions CRUD、messages(SSE)、runs/stream、stop、regenerate
- `astrbot/portal/api/router.py`（新）：聚合子路由
- `astrbot/portal/api/static_router.py`（新）
- `astrbot/dashboard/api/router.py`（**MODIFIED**，1 行）：`router.include_router(portal_router)`
- `astrbot/dashboard/api/app.py`（**MODIFIED**，1 行 + 顺序调整）：`app.include_router(portal_static_router)` 置于既有 `static_files_router` 之前
- `dashboard/portal.html`、`dashboard/src/portal-main.ts`（新）
- `dashboard/src/router/EndUserRoutes.ts`（新）
- `dashboard/src/stores/portalAuth.ts`（新）
- `dashboard/src/api/portal.ts`（新，仿 `v1.ts` 手写包装）+ `dashboard/openspec/openapi-v1-portal.yaml`（新）
- `dashboard/src/components/portal/{LoginPage,RegisterPage,PortalChatShell,PortalComposer}.vue`（新，`PortalComposer.vue` 从 `ChatInput.vue` fork 并物理删除 3 处 import）
- `dashboard/src/components/portal/portalMarkdownComponents.ts`（新）
- `dashboard/vite.portal.config.mts`（新）、`dashboard/package.json`（**MODIFIED**：新增 `build-portal`/`dev:portal` 脚本）
- `dashboard/eslint.config.*`（**MODIFIED**：新增 import 边界规则）
- `pyproject.toml`（**MODIFIED**：新增 `[tool.importlinter]` contract）
- `tests/test_portal_auth.py`、`tests/test_portal_chat_isolation.py`（新）

**验收标准（可测试）**：
1. 两个不同账号注册后各自创建会话，A 账号的 `GET /api/v1/portal/sessions` 响应中不包含任何 B 账号的 session_id（自动化测试）。
2. A 尝试 `GET /api/v1/portal/sessions/{B的session_id}` 返回 404。
3. 发送消息后前端实时渲染流式 token；中途关闭标签页重新打开同一会话，`GET /api/v1/portal/runs/{run_id}/stream` 能续传出未看到的部分（集成测试模拟断线）。
4. 用 portal JWT 打向 `/api/v1/conversations`、`/api/v1/sessions`（核心管理面路由）均返回 401。
5. `import-linter` CI 检查通过；ESLint 边界规则检查通过。
6. 3 秒失败延迟 + IP 限流对暴力破解登录生效（测试用例验证 6 次错误密码后触发限流）。

**估算**：约 29 人天（模块骨架 3 + 认证 5 + 能力墙与 chat_routes 6 + provider 剥离 1 + 核心接入与静态路由 2 + 前端 8 + 隔离测试 4）。

---

### Phase 2 —— 文件/图片上传（多模态输入）

**交付物**：
- `astrbot/portal/db/models.py`（**MODIFIED**：新增 `PortalAttachmentOwner`）
- `astrbot/portal/services/attachment_service.py`（新）：包装 `core_facade` 的 `save_uploaded_file`/`resolve_attachment_file`，写入/校验所有权表
- `astrbot/portal/api/file_routes.py`（新）：上传/下载，含大小与 MIME 类型限制、每用户累计字节配额检查
- `dashboard/src/components/portal/UploadDropzone.vue`（新）
- `dashboard/src/composables/`：`useMediaHandling.ts`/`useRecording.ts` 复用点调整（**MODIFIED**，改指向新 endpoint；若原文件被运营端同时使用则新建 `usePortalMediaHandling.ts` 而非直接改动共享文件，避免破坏运营端行为——具体取哪种方式由实现时检查该 composable 是否被 `Chat.vue` 等运营端组件共享决定）
- `tests/test_portal_attachment_isolation.py`（新）

**验收标准**：
1. A 上传图片得到 attachment_id，B 用该 id 调 `GET /api/v1/portal/files/{id}` 返回 404。
2. 核心 `GET /api/v1/files/{id}` 未出现在 portal 前端生成的 API client 代码中（CI grep 断言）。
3. 超出单用户每日上传字节配额时,上传请求返回 429 并提示剩余额度。
4. 图片可随消息一起发送并触发多模态回复（端到端手测 + 截图存档）。

**估算**：约 12 人天。

---

### Phase 3 —— 知识库问答

**交付物**：
- `astrbot/portal/db/models.py`（**MODIFIED**：新增 `PortalKbVisibility`）
- `astrbot/portal/services/kb_visibility_service.py`（新）
- `astrbot/portal/api/kb_routes.py`（新）：`GET .../knowledge-bases`、`POST .../sessions/{id}/knowledge-base`
- `astrbot/portal/startup_checks.py`（新）：umo 路由健康检查（移植自 P3 思想，第 4 节第 9 条）
- `dashboard/src/components/portal/KbSelector.vue`（新）
- 管理端（加入既有 `dashboard/src/router/MainRoutes.ts` **MODIFIED**，复用 `require_system_scope`）：`dashboard/src/components/admin/PortalKbVisibilityAdminPage.vue`（新）
- `astrbot/portal/api/admin_routes.py`（新，本阶段先支撑 kb-visibility 部分）
- `tests/test_portal_kb_whitelist.py`（新）

**验收标准**：
1. 管理员未加入白名单的 KB，终端用户请求绑定该 kb_id 返回 403。
2. 绑定白名单内 KB 后，提问能检索到该 KB 内容并在回答中体现（手测+截图）。
3. 模块启动日志中出现 umo 路由健康检查结果；故意不配置隔离 profile 时,日志出现 CRITICAL 告警（测试用例注入未配置场景，断言日志包含告警字符串）。

**估算**：约 11 人天。

---

### Phase 4 —— 用户可选 AI Agent / Persona / 子代理

**交付物**：
- `astrbot/portal/db/models.py`（**MODIFIED**：新增 `PortalAgentBundle`）
- `astrbot/portal/services/agent_bundle_service.py`（新）
- `astrbot/portal/api/agent_routes.py`（新）：`GET .../agent-bundles`、`POST .../sessions/{id}/agent-bundle`（内部调用 `conv_mgr.update_conversation(persona_id=...)`，provider_id 由 bundle 服务端解析，绝不接受客户端字段——对应第 4 节第 1 条契约的具体落地点）
- `dashboard/src/components/portal/AgentBundlePicker.vue`（新）
- 管理端：`dashboard/src/components/admin/PortalAgentBundlesAdminPage.vue`（新），`astrbot/portal/api/admin_routes.py`（**MODIFIED**）
- `tests/test_portal_agent_bundle.py`（新，含"注入 selected_provider 字段应被忽略"的回归测试，对应第 4 节第 1 条）

**Phase 4.5（可选，默认不做，仅在产品明确要求"裸露子代理级选择"而非"persona+provider+KB 套餐"时启用）**：
- `astrbot/core/astr_main_agent.py`（**MODIFIED**，唯一的第 3 处核心改动，默认关闭无副作用）：在 `_ensure_persona_and_skills` 子代理合并步骤前，读取 `sp.get_async(scope='umo', scope_id=umo, key='portal_selected_subagent')`，若存在则过滤 `so.handoffs` 白名单；键不存在时行为与现状逐字节一致。
- `astrbot/portal/api/agent_routes.py`（**MODIFIED**）：新增写该 sp key 的 owner-checked 端点。
- 该项列为**可选、延后交付**，理由与 P1/P2 一致：子代理编排在核心是全局单一配置，无 per-umo 挂点，真正隔离必须触碰核心；MVP 用 persona+provider+KB 套餐已满足需求(d)的产品意图。

**验收标准**：
1. 用户能在会话中切换预设 Agent Bundle，之后的回复体现新 persona 语气/新 provider。
2. 恶意构造的 `selected_provider` 字段在服务端被覆盖（自动化回归测试，见 4 节第 1 条）。
3.（若做 4.5）非白名单子代理名称在 `so.handoffs` 过滤后不可被 LLM 调用（测试用例验证 tool 列表不包含未授权子代理）。

**估算**：约 14 人天（+ 可选 3 人天）。

---

### Phase 5 —— 剩余两种身份方式：管理员开户 + SSO/OAuth

**交付物**：
- `astrbot/portal/api/admin_routes.py`（**MODIFIED**）：`GET/POST .../admin/users`，`Depends(require_system_scope)` **原样导入**自 `astrbot/dashboard/api/auth.py`
- `astrbot/portal/services/invite_service.py`（新）
- `astrbot/portal/api/auth_routes.py`（**MODIFIED**）：OAuth `start`/`callback`，authlib 集成，账号绑定二次确认流程
- `dashboard/src/components/portal/{InviteAcceptPage,OAuthCallbackPage}.vue`（新）
- `dashboard/src/components/admin/PortalUsersAdminPage.vue`（新）
- `tests/test_portal_admin_provision.py`、`tests/test_portal_oauth.py`（新，含 state/nonce CSRF 测试、邀请 token 重放/过期测试）

**验收标准**：
1. 管理员（真实 dashboard admin JWT 持有者）能创建邀请，普通 portal 用户无法访问该端点（403）。
2. 邀请 token 使用后立即失效，重放返回 410。
3. OAuth 回调正确处理 state/nonce，拒绝 CSRF 伪造回调（测试用例注入伪造 state）。
4. 已登录本地账号用户绑定 SSO 身份需二次确认，未确认不生效。

**估算**：约 21 人天。

**全量估算合计**：Phase1 29 + Phase2 12 + Phase3 11 + Phase4 14(+3) + Phase5 21 ≈ **87-90 人天**（约 17-18 周单人力，双人团队约 9 周日历时间），与 P2 原始文档"全量 17 人周"量级吻合。

---

## 6. 风险与开放问题

| 风险 | 等级 | 缓解 |
|---|---|---|
| **SQLite 单写者并发上限**：`astrbot/core/db/sqlite.py` 全局 WAL+busy_timeout 但仍单写者；portal 自身表在独立文件不受影响，但所有 `ChatService`/`conv_mgr`/`sp` 调用仍写入核心共享 `data_v4.db` | 高（增长期必现） | Phase 1 上线即监控 `database is locked` 异常率；超过阈值时评估把核心 SQLite 迁移为更高并发后端属于超出本项目范围的核心级重构，需另立项；短期可通过限制每用户并发 run 数（`PortalUsageCounter`）降低峰值写压力 |
| **AGPL §13 义务** | 高（法律） | `astrbot/portal/` 与 `dashboard/portal.html` 前端代码判定为与核心同进程直接 import 的组合作品，按 AGPL-3.0-or-later 授权，不采用"网络对等程序独立"的脆弱论证（该论证仅在真正独立进程如 P1 时才有讨论空间，且 P1 自己也承认前端仍不可避免地是衍生作品）。落地：`GET /api/v1/portal/source` 端点 + 登录页/页脚常驻链接 + Docker 构建注入 `SOURCE_COMMIT` build arg，任何 fork 修改后必须同步更新可下载源码，这是运维方职责,模块只负责让其"可轻易做到"，不能代为强制执行 |
| **provider 越权（安全评审头号 fatal flaw）** | 已缓解 | 见第 4 节第 1 条契约 + Phase 4 回归测试 |
| **能力墙形同虚设（安全评审头号 fatal flaw）** | 已缓解 | 见第 3.2 节 + import-linter CI |
| **前端 import 边界无强制（P2 自曝缺陷）** | 已缓解 | ESLint `no-restricted-imports` CI 强制 |
| **P2 原文未言明网络隔离** | 已缓解 | 第 3.1/4 节第 6 条反向代理路径级隔离要求 |
| **chat_runs 进程内内存态,重启即丢** | 中（已知,继承核心限制） | 与现有 k8s `replicas:1` 拓扑一致,不在本项目范围内解决；前端需实现"续传失败→回退读取已持久化的 PlatformMessageHistory→提示用户重新发送"的兜底 UX（Phase1 交付内） |
| **核心 Python 方法签名漂移**：`core_facade.py` 直接 import `ChatService`/`conv_mgr` 方法，AstrBot 升级时签名变化会静默破坏 portal 而无 HTTP 契约测试拦截 | 中 | `tests/test_portal_core_facade_contract.py`（Phase1 交付）对 `core_facade.py` 依赖的每个核心方法签名做 pin 测试,每次升级 AstrBot 版本前必须先跑该测试套件 |
| **umop_config_router 误配置导致 prompt injection 触达危险工具** | 高（无法被代码完全消除） | 第 4 节第 9 条启动健康检查只能告警/拒绝启动,不能替代管理员正确配置专用 profile；需写入部署 runbook 作为强制步骤 |
| **openapi-v1-portal.yaml 与实际路由漂移** | 低 | 沿用仓库现有惯例（`openspec/openapi-v1.yaml` 本身也是手工维护、无自动漂移检测）,新增一条 CI smoke test 对照关键端点存在性 |
| **开放问题**：`GET /api/v1/chat/configs` 是否泄露 provider secret 给 chat scope 调用方，尚未在 recon 中确认——本设计不复用该端点，但若未来任何 portal 端点考虑透传 provider 元数据，需先验证 | — | 实现前需单独确认 `config_profile_service.py::list_profiles()` 返回体 |
| **开放问题**：`useMediaHandling.ts`/`useRecording.ts` 是否被运营端组件共享，决定 Phase2 是"adapt 原文件"还是"新建 portal 专属 composable" | — | Phase2 开工前 15 分钟内用 grep 确认调用方 |

---

## 7. 被否决方案的记录与理由（供未来审计）

### P1：独立 BFF 网关服务
**未采纳理由**：
1. 安全评审明确指出的**共享身份坍缩**（所有终端用户在 AstrBot 侧统一坍缩为 `api_key:<key_id>`）是本设计认为不可接受的架构性缺陷——一次凭证/网络隔离层面的失误即导致全体用户数据同时暴露，而非单一用户暴露。
2. 前端"复制而非导入"15 个组件文件，与硬约束 #3"最大化复用"的精神冲突，且是 P1 自己承认的持续性维护税。
3. 多一个独立进程/数据库/k8s 部署单元的运维成本，是三份文档中最重的（务实工程评审 6.5/10，明确扣分点）。
4. 两跳网络 + 需要重新实现一遍核心已跑通的可续传 SSE pub/sub，属于不必要的重复工程。
5. 其"AstrBot 端口永不可从公网直连"这条"全设计最具杠杆性的控制项"是纯基础设施配置、极易静默回归——本设计改用路径级反向代理规则替代（同样是基础设施配置，但作用范围更小、粒度更细，且叠加在同进程架构上而非作为唯一防线）。

**吸收的思想**：能力隔离墙（转译为代码级 import-linter）、网络隔离原则（转译为路径级反代规则）、refresh token 轮换+重放检测、"永不透传客户端 provider 字段"的显式契约。

### P3：插件/Star 挂载式门户
**未采纳理由**：
1. 安全评审与务实工程评审**独立**发现同一致命缺陷：`register_web_api` 分发路径无匿名访问概念，公开登录页只能靠一把"必须公开、无法保密"的共享 `plugin` scope API Key，且该 Key 泄漏后可被重放到系统内**任何其他已安装插件**的路由——攻击面随运维安装的插件数量增长，且此缺陷在"零核心改动"约束下**无法在设计内部修复**（唯一解法是一个不受控的可选上游 PR），与其自称的核心卖点矛盾。
2. 效率上，MVP 13.5 人周是三方案中**最高**的，却对应"零核心改动"这个本应最省力的卖点——因为插件缺乏中间件能力，每个写操作 handler 都要手撸 CSRF/限流，长期维护成本高。
3. 依赖 `register_web_api`/`AuthContext`/`_call_plugin_extension` 等**未被正式版本化的插件内部契约**，一次寻常的核心重构可无预警打破整个门户，风险敞口高于 P2 使用的"标准 FastAPI 路由注册"这一稳定得多的挂点。
4. 三个评审维度全部排名最后（7/10 vs 3/10、8.5/10 vs 5.5/10、8/10 vs 5.5/10），无一维度占优,聚合分 4.67 明显落后。

**吸收的思想**：启动期 umo 路由健康检查（第 4 节第 9 条）、AGPL"同进程即组合作品"的诚实定性（本设计的 AGPL posture 与 P3 结论一致，均判定为需整体 AGPL 授权,不采信"网络对等独立"的脆弱论证）。

---

## 8. 文档使用说明

本文件第 1-8 节是定案时的设计基线；实现偏差记录在第 9 节（见文首提示）。任何后续变更若与本文件冲突，应先更新对应章节或在第 9 节补记并说明理由，再变更代码。

**AstrBot 版本升级 checklist（强制项）。** 以下核心文件被本项目改动过，每次升级后必须重新走查 diff 是否仍然适用：

1. `astrbot/dashboard/api/router.py` —— 一行 `include_router(portal_router)`。
2. `astrbot/dashboard/api/app.py` —— lifespan 中的 portal 初始化（建表、JWT secret、session-rule guard 注册、umo 路由健康检查），以及 portal 静态路由**必须**注册在 catch-all `static_files_router` 之前这一顺序约束。
3. `astrbot/dashboard/services/session_management_service.py` —— **本文件最初未预见的第 3 处核心改动**，Phase 3 安全审计迫出：新增 `SessionRuleGuard` 协议与 `register_session_rule_guard` 注册点。它本身与 portal 无关（核心不 import portal），但 portal 依赖它存在。升级时若上游重构了 `AVAILABLE_SESSION_RULE_KEYS` 或那三个 delete/update 方法，必须确认 guard 调用点仍在原位——**否则 `kb_config`/`session_service_config`/`provider_perf_chat_completion` 的白名单绕过会静默复活**。
4. `astrbot/core/astr_main_agent.py` —— Phase 4.5 可选项，**当前未改动**。若日后启用，加入本清单。

另外两项非核心但同样属于升级面：`pyproject.toml` 的 import-linter 契约（能力墙靠它强制），以及 `scripts/hatch_build.py` / `.github/workflows/{release,docker-image}.yml` 中对 `dist-portal` 的打包处理（漏掉即前端 404）。

**依赖面**：本项目**未新增任何运行时依赖**。OIDC 建在 `pyjwt`+`httpx` 上，企微/飞书建在已有的 `wechatpy`/`lark-oapi` 上。升级这四个包时需复查 `astrbot/portal/services/sso/` 是否仍成立——尤其 `jwt.PyJWKClient` 的行为与 `wechatpy.enterprise` 的模块路径（注意 `wechatpy.work` 在 1.8.x 不存在）。

---

## 9. 实现偏差记录（Phase 1-5 全部完成后回填）

本节记录**实现与本文件规划不一致之处**，以及**已知未达成项**。写在这里而不是悄悄改掉上文，是为了让"当初怎么想的"和"最后怎么做的"都可追溯。

### 9.1 与规划不同的技术决策（均为有意，附理由）

| 项 | 本文原规划 | 实际实现 | 理由 |
|---|---|---|---|
| 授权失败状态码 | 第 5 节 Phase 3 验收标准 1 写 **403** | 统一 **404** | 保持模块既有约定，避免把响应变成"该资源是否存在"的预言机。KB 是运营资源而非用户资产，两种读法都成立，选了不泄露存在性的那个。全部 KB/Bundle 路由一致 |
| OIDC 客户端 | 第 5 节 Phase 5 写「authlib 集成」 | **pyjwt + httpx，零新增依赖** | `pyjwt` 自带 `PyJWKClient`（JWKS 拉取 + 签名校验），`httpx` 做 token 交换，两者已是依赖。对抗审计逐行审过自研实现后判定 `keep-self-built`、未发现缺陷，故不引入 authlib。若日后审计结论改变，换回 authlib 的边界很清晰：仅 `services/sso/oidc.py` |
| 企微/飞书 SDK | 未指定 | `wechatpy.enterprise` + `lark_oapi.api.authen`，同样零新增依赖 | 两者已因 IM 平台适配器成为依赖。注意：**登录用的应用凭证与 IM 适配器的 bot 凭证是两套**，不得混用 |
| 核心侵入范围 | 预算 2 处（+Phase4.5 可选 1 处） | **3 个文件**：`api/router.py`（挂载）、`api/app.py`（lifespan）、`services/session_management_service.py`（新增 guard 注册钩子） | 第 3 个是 Phase 3 安全审计迫出来的：`POST /api/v1/sessions/rules` 仅需 `require_data_scope` 却能写 `kb_config`/`session_service_config`/`provider_perf_chat_completion`，完全绕过 portal 的白名单。核心因此新增一个**与 portal 无关**的 `SessionRuleGuard` 注册点，由 portal 在启动时填充——核心仍不 import portal（除上述两处挂载点），无 portal 部署时行为与改动前逐字节一致 |
| SSO 与账号的关系 | 未明确 | **SSO 只是已有账号的登录方式，不是注册通道** | 未绑定的身份直接拒绝。身份只按 `(provider, subject)` 解析、从不按 email，且无任何路径能从 SSO 回调创建 `PortalUser`。这从结构上消灭了"让 IdP 声称受害者邮箱以取得/抢占其账号"整类攻击，强于任何开关。**若产品预期是"企微扫码即用、无需预先开户"，这是一个需要改的产品决策** |
| Phase 4.5 子代理白名单 | 已列为可选、默认关闭、延后 | **未做** | `SubAgentOrchestrator` 是全局单例、无 per-umo 授权挂点，真正隔离必须改核心。MVP 用 persona+provider+KB 套餐满足需求(d) |

### 9.2 已知未达成 / 受限项

1. **AGPL §13 源码端点未实现** —— 见第 4 节第 13 条的告警框。这是唯一未达成的安全清单条目，且对外提供服务前是硬性前置。
2. **没有密码重置流程** —— 依赖邮件能力，而本仓库无邮件设施。见 3.3 表格。用户忘密码只能由运维方重新签发邀请。
3. **单进程约束（`replicas: 1`）** —— 三处进程内状态：核心 `ChatService.chat_runs`（Phase 1 继承）、限流器计数、SSO 的 state/nonce/PKCE 与 link 确认票据（Phase 5）。水平扩展需要先把这些搬到共享存储。
4. **umo 路由健康检查只能告警** —— 第 4 节第 9 条要求的检查已实现并每次启动打日志，但 `refuse_to_start` 默认关闭且**没有运维可配置的开关**，所以未正确配置隔离 profile 的部署会永远打 CRITICAL 然后照常服务。
5. **无真机端到端验证** —— 以下四条验收标准只有集成层面证据，没有跑起完整栈的真机证据：图片多模态回复（Phase 2 标准 4）、KB 检索命中（Phase 3 标准 2）、Bundle 切换体现新 persona/provider（Phase 4 标准 1）、真实 IdP 往返（Phase 5）。
6. **非图片类型的 MIME 仅声明级校验** —— 核心只嗅探图片；portal 额外为 PDF（magic bytes）和 JSON（可解析）做了内容校验，audio/video/text 仍只信客户端声明的 Content-Type。
7. **persona 重置是快照而非活指针** —— 核心 `update_conversation` 无法把 `persona_id` 写回 NULL，故解绑时写入"当前运营默认 persona"的快照；运营方后来改默认值，已解绑会话不跟随。
8. **`session_service_config` 的通用规则编辑器对 portal 会话不可用** —— guard 协议只拿到 `(umo, rule_key)` 拿不到 value，无法"仅在写 persona_id 时拒绝"。运营方仍可通过 `batch_update_service` 改 `llm_enabled`/`tts_enabled`。
9. **`self_registration_open` 默认仍为 `True`** —— 实现者认为开户/SSO 就绪后应改为 `False` 更正确，但改动会破坏两个既有测试，选择上报而非硬改。
10. **缺"已绑定 SSO 身份"的账号设置页** —— 后端 link/unlink 端点已就绪，但终端用户没有入口自助发起绑定。
11. **thread 维度 umo 未覆盖 KB 默认关闭修复** —— 目前无 portal 路由能创建/进入 thread，故不可利用；一旦放开即为盲点。
12. **没有配置入口（最高优先级待办）** —— `get_portal_settings()` 返回硬编码的 `DEFAULT_PORTAL_SETTINGS`，不读环境变量、不读 `cmd_config.json`、无管理界面、无任何 mutate 路径。Phase 1 的 docstring 写着"a later phase can source overrides"，但后续四个阶段都没做。**直接后果：Phase 5 的三个 SSO provider 完整实现且 51 个测试通过，却在真实部署里无法启用**（`sso_enabled=False`、`sso_enabled_providers` 为空、三组凭证均 `None`）；配额、上限、`self_registration_open` 也全部锁死。目前唯一办法是改 `astrbot/portal/config.py` 并重启。补配置机制需要先决定 overrides 落在哪（env / `cmd_config.json` / 新建管理表），是一个独立的小设计。
13. **OpenAPI spec 未包含 portal 端点** —— `openspec/openapi-v1.yaml` 是**手写维护**的（6224 行 / 223 条路径），缺全部 50 个 portal 端点（其中 16 个在 `admin_routes`）。这是**有意不补**：该 spec 驱动 `npm run generate:api` 生成的 axios client 与 `docs/public/openapi.json`，而 portal 前端不用生成的 client（它有自己的 `src/api/portal.ts`），portal API 也没有第三方消费者。手写 50 个端点的 YAML 成本高、易与代码漂移、收益低。**若日后要对外开放 portal API 供第三方调用，这一条就必须补上。**
