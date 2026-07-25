# Portal（多用户 C 端聊天前端）

Portal 是一个面向**终端用户**的聊天界面：用户各自注册/登录，各自拥有互相隔离的会话历史，对话由你在 AstrBot 里配置好的 AI Agent 处理。

它与 [WebUI](./webui.md) 是两个不同的东西，不要混淆：

| | WebUI | Portal |
|---|---|---|
| 面向谁 | **你**（运维方） | **你的用户** |
| 身份模型 | 单一管理员账号 | 多用户，各自独立账号 |
| 能做什么 | 配置模型、装插件、看日志、改配置 | 只能聊天 |
| 访问路径 | `/` | `/portal` |
| Cookie | `astrbot_dashboard_jwt` | `astrbot_portal_jwt` |

两套认证体系的签名密钥、Cookie 名、claim 形状完全独立，互不识别：Portal 的 token 打到 WebUI 的接口是 401，反之亦然。

> [!WARNING]
> Portal 让**不受信任的外部用户**接触到你的 AstrBot 实例。上线前请务必读完本页的「运维方必做」一节——其中每一条都不是可选加固，而是缺了就会出事的前提。

## 快速开始

Portal 是与 WebUI 并列的第二个前端构建产物。

```bash
cd dashboard
npm run build:portal      # 产物在 dashboard/dist-portal/
```

启动 AstrBot 后访问 `http://localhost:6185/portal`。首次访问会看到登录页，可以直接注册（默认开放自助注册）。

开发时热重载：

```bash
npm run dev:portal
```

Docker 镜像与 release 压缩包已包含这个产物，无需额外操作。若你自己打 wheel，需要 `ASTRBOT_BUILD_DASHBOARD=1 uv build`。

## 用户从哪来

Portal 支持三种身份方式，但**当前只有第一种真正可用**：

### 1. 自助注册（可用）

默认开放。用户在 `/portal` 自己注册邮箱与密码，密码策略沿用 AstrBot 既有的强度要求（≥8 位、含大小写与数字），哈希用 PBKDF2-SHA256。

### 2. 管理员开户 / 邀请（可用）

在 WebUI 左侧菜单进入 **Portal 用户管理**，为指定邮箱签发邀请，得到一条一次性链接。

> [!IMPORTANT]
> **AstrBot 没有邮件发送能力**，所以这条链接不会自动发给用户——需要你自己通过 IM、企业内网或其它渠道交给对方。链接**只显示一次**，关闭弹窗后无法再取回，它的保密性完全取决于你的投递方式。

邀请为一次性使用，默认 7 天过期。已使用的链接被再次打开返回 410。

### 3. SSO（已实现，但当前无法启用）

通用 OIDC、企业微信、飞书三种 SSO 都已完整实现并有测试覆盖，但**目前没有配置入口**——见下方「配置」一节。在补上配置机制之前，登录页不会出现任何 SSO 按钮。

另外请注意一个产品行为：**SSO 只是已有账号的登录方式，不是注册通道。** 未绑定的身份直接被拒绝，用户必须先由你开户或邀请，然后在已登录状态下绑定 SSO 身份（需二次确认）。这样设计是为了从结构上消灭"让身份提供方声称某个邮箱以取得该账号"这类接管攻击。如果你的预期是"扫码即用、无需预先开户"，这是需要改的产品决策。

## 运维方必做

以下三条缺一不可。

### 1. 反向代理必须挡住管理面路径

Portal 的终端用户接口在 `/api/v1/portal/*` 下，**必须公网可达**。但同一前缀下还有 `/api/v1/portal/admin/*`——它承载用户开户、邀请签发、知识库授权、Agent Bundle 管理。它只差一个路径段，是最容易漏配的一条。

公网**只应**可达：

- `/portal/*`（SPA 静态资源）
- `/api/v1/portal/*` **减去** `/api/v1/portal/admin/*`

以下路径**一律仅限内网/VPN**：

```
/api/v1/portal/admin/*      ← 最容易漏！
/api/v1/conversations*
/api/v1/sessions*
/api/v1/session-groups*
/api/v1/config*
/api/v1/api-keys*
/api/v1/plugins*
/api/v1/persona*
/api/v1/subagents*
```

Nginx 示例：

```nginx
# 先拒绝，再放行——顺序很重要
location ^~ /api/v1/portal/admin/ {
    allow 10.0.0.0/8;
    deny  all;
    proxy_pass http://astrbot;
}
location ^~ /api/v1/portal/ {
    proxy_pass http://astrbot;
}
```

配好后请从公网出口实际探测一次 `/api/v1/portal/admin/`，确认返回连接拒绝或 403 而不是 200。

### 2. 必须为 Portal 流量配置隔离的 config profile

终端用户的输入会进入一个**具备工具调用能力**的 Agent。AstrBot 的子代理编排（`SubAgentOrchestrator`）是全局单一配置，没有按会话授权工具的机制——所以爆炸半径就是你全局启用的那些能力，可能包括文件写入、shell、无限制网络访问。

你需要用 `umop_config_router` 把 Portal 流量的 umo 前缀（`webchat:FriendMessage:webchat!*`）路由到一个**禁用危险工具与子代理**的专用 AstrBotConfig profile。

AstrBot 启动时会检查这一项并把结果打进日志。**没配的话你会看到 CRITICAL 告警，但服务仍会照常启动**——目前没有"未隔离就拒绝启动"的开关，所以这条告警必须被人看到并处理。

### 3. 只能单进程部署

保持 `replicas: 1`。有三处状态在进程内存里：核心的 chat run 表、限流计数、SSO 的 state/nonce 与绑定确认票据。多副本会导致流式续传失败、限流被绕过、SSO 回调随机失败。

## 你可以策展什么

Portal 的设计原则是：**终端用户只能选择你预先批准的东西**，永远不能直接指定 provider、模型或知识库。

### 知识库白名单

WebUI → **Portal 知识库可见性**。只有你在这里显式授权的知识库，终端用户才能在对话中选用。

> [!CAUTION]
> 授权一个知识库意味着**每一个** Portal 用户都能选它，并让其内容出现在回答里。这是内部语料与外部用户之间唯一的一道闸门。

未授权的知识库对终端用户完全不可见（请求返回 404 而非 403，避免被用来枚举你有哪些语料）。

### Agent Bundle

WebUI → **Portal Agent Bundle**。一个 Bundle 是你组合好的 **persona + 对话模型 + 知识库** 套餐，用户按名字选择，看不到里面具体用了哪个模型。

> [!CAUTION]
> 启用一个 Bundle 意味着每个 Portal 用户都能把对话路由到那个模型——这是直接的成本决策。

客户端请求体里的 `selected_provider`、`selected_model`、`provider_id`、`persona_id` 会被服务端**无条件丢弃**。模型选择永远由 Bundle 在服务端解析。

## 终端用户能做什么

- 注册/登录，多会话，会话重命名与删除
- 流式回复，中途停止，重新生成
- 刷新页面或断线后**续传**正在进行的回复
- 上传图片、音频、视频与文档（PDF/文本/CSV/JSON）做多模态输入
- 在你授权的知识库里为当前会话选择
- 在你启用的 Agent Bundle 之间切换

用户之间完全隔离：会话、消息、附件、知识库绑定都按用户隔离，越权访问一律返回 404。

## 配置

> [!WARNING]
> **当前 Portal 没有配置入口。** `get_portal_settings()` 返回代码里的硬编码默认值，不读环境变量、不读 `cmd_config.json`、也没有管理界面。要改任何一项，目前唯一的办法是编辑 `astrbot/portal/config.py` 中的 `PortalSettings` 默认值并重启。
>
> 这直接的后果是 **SSO 无法启用**（`sso_enabled = False`、`sso_enabled_providers` 为空、三组凭证均为 `None`），配额与上限也全部锁死在下表数值。补上配置机制是这个模块最优先的待办项。

主要默认值（`astrbot/portal/config.py`）：

| 项 | 默认值 | 说明 |
|---|---|---|
| `self_registration_open` | `True` | 是否开放自助注册 |
| `access_token_ttl_seconds` | `900` | access token 15 分钟 |
| `refresh_token_ttl_seconds` | `2592000` | refresh token 30 天，轮换 + 重放检测 |
| `auth_rate_limit_capacity` | `5` | 登录失败次数上限，超出退避 |
| `max_requests_per_window` | `200` | 每用户每日消息数 |
| `usage_window_seconds` | `86400` | 配额窗口 = 1 天 |
| `max_concurrent_runs_per_user` | `2` | 每用户并发生成数 |
| `max_upload_file_bytes` | `20 MiB` | 单文件上限 |
| `max_daily_upload_bytes` | `200 MiB` | 每用户每日上传总量 |
| `kb_selection_enabled` | `True` | 是否允许用户选知识库 |
| `max_kb_ids_per_session` | `3` | 单会话可绑定知识库数 |
| `agent_bundle_selection_enabled` | `True` | 是否允许用户选 Agent Bundle |
| `invite_token_ttl_seconds` | `604800` | 邀请链接 7 天有效 |
| `sso_enabled` | `False` | SSO 总开关 |

允许上传的 MIME 类型是**白名单**（AstrBot 核心本身对上传既无大小限制也无类型限制，所有边界都来自这里）：图片 png/jpeg/gif/webp、音频 wav/mpeg/aac/ogg/webm/mp4、视频 mp4/webm/quicktime、文档 pdf/plain/csv/json。

## 已知限制

除上面已说明的「无配置入口」与「单进程」之外：

1. **没有密码重置流程。** 它依赖发邮件，而 AstrBot 没有邮件能力。用户忘记密码只能由你重新签发邀请。
2. **AGPL §13 源码端点未实现。** AstrBot 以 AGPL-3.0 授权，对外提供网络服务时需要向使用者提供**实际运行的那份**源码。登录页目前的源码链接指向上游仓库，而不是你实际运行的 commit。**如果你把 Portal 开放给外部用户，这是必须先补的合规前置。**
3. **非图片类型的 MIME 只做声明级校验。** 核心只嗅探图片真实类型；Portal 额外为 PDF（magic bytes）和 JSON（可解析性）做了内容校验，音视频与文本仍只信客户端声明的 Content-Type。
4. **配额是每用户维度，没有全局磁盘水位保护。** 所有用户合起来仍可能填满磁盘。
5. **无自助 SSO 绑定入口。** 后端的绑定/解绑接口已就绪，但终端用户界面里还没有账号设置页去发起它。
6. **未做子代理级选择。** 用户只能选 Bundle（persona+模型+知识库套餐），不能直接选子代理——真正的子代理隔离需要改动 AstrBot 核心。

## 相关文档

- [WebUI](./webui.md) —— 运维方的管理面板
- [知识库](./knowledge-base.md) —— 先在这里创建知识库，才能在 Portal 里授权
- [SubAgent 编排](./subagent.md) —— 理解 Portal 为什么必须配隔离 profile
- 架构决策与实现记录：仓库内 `design/portal-multiuser-plan.md`（含完整的安全清单与实现偏差记录）
