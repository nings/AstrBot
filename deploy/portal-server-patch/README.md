# Portal 服务端补丁

把 `feat/portal-multiuser` 分支的 **Portal 服务端能力**（多用户注册/登录 + 会话与聊天 API）
移植到一台尚未包含该模块的 AstrBot 上，使其能对接
[astrbot-portal](https://github.com/AstrBotDevs/astrbot-portal) 前端。

## 这个补丁包含什么

| | |
|---|---|
| 新增 | `astrbot/portal/**` —— 33 个 `.py`，约 14.5k 行，外加 1 个附件暂存模块（纯新增，零冲突风险） |
| 修改 | 5 个既有文件，合计 +249 / −3 行 |
| 基线 | `fb02c727`（见 `BASE_COMMIT`），即 `feat/portal-multiuser` 与 `master` 的 merge-base |

被改动的 5 个文件：

```
astrbot/core/config/default.py                        +30   portal 配置项默认值        （补丁）
astrbot/dashboard/api/app.py                          +98   启动钩子：建库、CORS、守卫、检查（补丁）
astrbot/dashboard/services/session_management_service.py +110/−3  会话规则守卫接入点  （补丁）
astrbot/core/platform/sources/webchat/webchat_adapter.py  +9   接入附件暂存           （补丁）
astrbot/dashboard/api/router.py                        +2   挂载 portal_router        （脚本）
```

`router.py` **不在补丁里**，由 `integrate_router.py` 程序化挂载 —— 上游重构过这个文件的结构
（从一串 `include_router(x)` 调用改成 `child_routers = (...)` 元组 + 循环），
文本补丁绑死上下文会套不上。脚本两种形态都识别，且可重复执行。

### 为什么要动 `webchat_adapter.py`

不动它的话，**终端用户读不了自己刚上传的文件**。

上传的文件落在共享的 `data/attachments/`，而 agent 的文件工具对非管理员用户只放行
四类目录（`astrbot/core/tools/computer_tools/fs.py` 的 `_read_allowed_roots`）：
`data/skills`、插件技能、**会话工作区**、两个临时目录。附件目录不在其中。

Portal 终端用户永远是 `member`（`role` 只对 `admins_id` 里的 id 提升为 `admin`），
所以模型一去读用户刚传的 PDF 就撞 `PermissionError`。这对标准版的 webchat 也成立，
只是控制台操作员通常在 admins 列表里，感觉不到。

补丁新增 `astrbot/core/platform/sources/webchat/attachment_staging.py`，
在消息转换时把附件**复制**进该会话自己的工作区，并改写消息段指向副本。
`webchat_adapter.convert_message` 里加的 9 行就是这个调用。

两条更省事的路都不能走：

- **把 `data/attachments` 加进白名单** —— 那是全用户共享的扁平目录，
  A 用户的 agent 能枚举并读取 B 的上传。公开注册场景下是明确的跨租户泄露。
- **关掉 `computer_use_require_admin`** —— 本地运行时下会同时解除全部路径限制
  **并解锁 shell / python 执行工具**，等于给任何注册用户一个宿主机 RCE。

必须是真拷贝，硬链接和符号链接都不行：`fs._reject_multi_link_file` 会以
「可能指向白名单外的内容」为由拒绝 `st_nlink > 1` 的文件，而符号链接在白名单检查前
就被 `.resolve()` 还原回附件目录 —— 两者都会重新触发同一个错误。

代价是每个会话各留一份副本。上传单文件上限 20 MiB、单用户每日 200 MiB，
量级可控；工作区目录（`data/workspaces/<umo>/uploads/`）可按会话生命周期清理。

### 顺带修掉的一个 Portal 缺陷：文件附件全被标成「不可用或已被删除」

`file_routes.py` 的下载路由原本是 `@router.get("/{attachment_id}")`。
FastAPI 的 `@router.get` 只注册 `methods=["GET"]`，**不会**自动接受 HEAD ——
GET-only 路由收到 HEAD 直接回 `405`，不会跑一遍 handler 再丢掉 body：

```
/only-get        GET=200  HEAD=405
/get-and-head    GET=200  HEAD=200
```

而前端的文件气泡在给出下载链接前会先发一个 HEAD 探测
（`PortalAttachment.vue::probeFileExists`），任何非 OK 响应都渲染成
「不可用或已被删除」。结果是**每一个**文件附件在界面上都显示为失效，哪怕字节完好。
图片/音频/视频不受影响 —— 它们用原生 `<img>`/`<audio>`/`<video>` 发的是 GET。

已改为 `@router.api_route("/{attachment_id}", methods=["GET", "HEAD"])`。
Starlette 仍会替我们丢掉 body，所以 HEAD 只花一次 stat，不读文件。
所有权检查照旧走同一个 handler，未授权的 HEAD 同样返回 404 而非 403，
不会变成一个 GET 路径拒绝充当的存在性探针。

**只含服务端 Python。** 控制台里的 Portal 管理页（用户管理、邀请、知识库可见性）是 Vue，
需要在服务器上重建前端才能用，不在本补丁范围内。不装它们不影响 Portal 本身运行 ——
默认 `self_registration_open=true`，终端用户可自助注册；操作员只是暂时没有图形化管理界面。

## 包里有什么

| 文件 | 用途 |
|---|---|
| `detect-deployment.sh` | 判定这台是 Docker / git 源码 / 解压包 / pip 安装（只读） |
| `preflight.sh` | 7 项兼容性预检（只读） |
| `portal-server.patch` | 主补丁：portal 模块 + 4 个集成点 |
| `integrate_router.py` | 程序化挂载 `portal_router`（`router.py` 不走文本补丁） |
| `apply.sh` / `rollback.sh` | 首次套用 / 完整还原 |
| `upgrade-attachments.patch` + `upgrade.sh` | **增量升级**，给已套过补丁的机器用 |
| `BASE_COMMIT` / `UPGRADE_FROM` | 两个补丁各自的基线提交 |
| `RUNBOOK.md` | 面向具体服务器的分步操作手册 |

## 使用

### 首次套用

```bash
# 1. 拷到服务器
scp -r deploy/portal-server-patch user@host:/tmp/

# 2. 预检（只读，不改任何文件）
cd /tmp/portal-server-patch
./preflight.sh /path/to/AstrBot

# 3. 预检全过后再套用（会自动先跑一遍预检，并备份被改的 5 个文件）
./apply.sh /path/to/AstrBot

# 4. 重启 AstrBot，然后自检 —— 返回 401 而不是 404 即为就绪
curl -s -o /dev/null -w '%{http_code}\n' http://<host>:6185/api/v1/portal/auth/me
```

出问题随时还原：`./rollback.sh /path/to/AstrBot`

### 已套过补丁的机器：增量升级

不要为了几处改动重跑 `apply.sh` —— 那会把 33 个文件的 portal 模块整个拆掉重装。
`upgrade.sh` 只动 3 个文件，不碰 `data/`：

```bash
scp -r deploy/portal-server-patch user@host:/tmp/     # 脚本也要一并更新
cd /tmp/portal-server-patch
./upgrade.sh /path/to/AstrBot
```

撤销：`./upgrade.sh /path/to/AstrBot --rollback`（只回退本次升级，portal 本体不动）。

`upgrade.sh` 会先确认「portal 已存在、尚未升级过、补丁能干净套用」，任一不过即中止且不改文件；
并把 `webchat_adapter.py` 的原始副本补进 `.portal-patch-backup/` —— 那是基础补丁当初没备份的文件，
不补的话日后跑 `rollback.sh` 会漏还原它，留下一个 import 已删除模块的 adapter，AstrBot 起不来。

**当前这一版增量升级的内容**：附件暂存（`attachment_staging.py` + `webchat_adapter.py`）
与下载路由的 `HEAD` 支持（`file_routes.py`）—— 缘由见开头「为什么要动 `webchat_adapter.py`」
和「顺带修掉的一个 Portal 缺陷」两节。

### 预检查什么

失败就会中止，因为被改的 5 个文件都在认证、路由与消息投递的关键路径上，套错了会以难排查的方式坏掉。

1. 目标确实是 AstrBot 源码树
2. `astrbot/portal` 尚不存在（防重复套用）
3. **控制台是 FastAPI** —— 更老的 AstrBot 用 Quart，那种情况本补丁完全不适用
4. 依赖的 9 个核心符号齐备（`build_api_router` / `AuthContext` / `require_system_scope` /
   `ChatService` / `ChatServiceError` / `MessageType` / `get_astrbot_data_path` /
   `resolve_workspace_root_for_umo` / `convert_message`）
5. Python 依赖可用（补丁**不引入新的运行时依赖**）
6. `git apply --check` 或 `patch --dry-run` 干净通过

### Docker 部署

本补丁改的是源码，不能套到运行中的容器里（重建即丢）。两种做法：

```dockerfile
# A. 构建期套用（推荐）
COPY deploy/portal-server-patch /tmp/patch
RUN /tmp/patch/apply.sh /AstrBot
```

```bash
# B. 挂载已打补丁的源码树
docker run -v /srv/AstrBot-patched:/AstrBot ...
```

---

## ⚠️ 上线前必须处理的安全问题

打完补丁启动时，AstrBot 自己的启动检查会打出一条 CRITICAL：

```
PORTAL_UMO_ROUTING_NOT_ISOLATED: portal traffic has NO explicit route in
UmopConfigRouter and falls through to the DEFAULT AstrBotConfig profile.
```

含义：**Portal 终端用户的输入会进入一个带全局工具的 tool-calling agent** ——
包括 `astrbot/core/computer/`（计算机操作）和 `astrbot/core/tools/` 下操作员全局启用的一切。
`SubAgentOrchestrator` 的工具白名单是全局的，没有按 umo 做授权。

这在本场景里尤其危险：Portal 挂在公开站点上，且默认允许**任何人自助注册**。

**这条检查只告警、不阻止启动**（`refuse_to_start` 默认 `False`，见
`astrbot/dashboard/api/app.py` 的调用处注释），所以服务会正常跑起来，风险是静默的。

处理方式：新建一个禁用了危险工具与子 agent 的 `AstrBotConfig` profile，把 Portal 流量路由过去：

```python
await umop_config_router.update_route(
    'webchat:FriendMessage:webchat!*!*', '<isolated-profile-id>'
)
```

通配符要覆盖**全部** Portal umo，不能只配某一个用户。也可以用控制台的 umo-routing 管理界面。

另外两项：

- **`self_registration_open`** 默认 `true`。若不打算对公众开放，在 `data/cmd_config.json`
  的 `portal` 段关掉，改用邀请或 SSO。
- **传输加密**：若 Portal 前端与 AstrBot 之间跨公网，务必走 HTTPS 或内网 ——
  这条链路上传的 `astrbot_portal_jwt` / `astrbot_portal_refresh` 是会话凭证。

---

## 首次启动会自动做的事

- 建库：`data/portal/portal.db`（`metadata.create_all`，**无需手动迁移**）
- 把 `portal` 配置段合并进 `data/cmd_config.json`
- 注册会话规则守卫（拒绝操作员对 Portal 会话写入 `kb_config` 等键）

> `metadata.create_all` 只建缺失的**表**，不会改已存在表的结构。日后若模块升级涉及列变更，
> 需要真正的迁移 —— 见 `astrbot/portal/db/engine.py` 的说明。

## 彻底清除

`rollback.sh` 只还原代码，保留 `data/`（便于重新套用时复用）。要连数据一起清：

```bash
rm -rf /path/to/AstrBot/data/portal          # portal 数据库（用户、会话、邀请）
rm -rf /path/to/AstrBot/data/workspaces      # 会话工作区，含 uploads/ 下的附件副本
# 再从 data/cmd_config.json 里删掉 "portal" 段
```

> `data/workspaces` 不是 Portal 专属的，标准版的会话工作区也在这里。只想清附件副本就删
> 各会话下的 `uploads/` 子目录。

## 前端对接

前端是独立项目 [astrbot-portal](https://github.com/AstrBotDevs/astrbot-portal)。
推荐同源部署：前端与 `/api/` 反代放在同一域名下，session cookie 保持第一方、不涉及 CORS，
此时 AstrBot 侧**无需任何 CORS 配置**（保持 `cors_allowed_origins: []`）。

跨域部署需要 `cors_allowed_origins` + `cross_origin_cookies=true`，且两端都必须是 HTTPS
（`SameSite=None` 只在 `Secure` 下生效）。但那样 cookie 会变成第三方 cookie，Safari 默认拦截、
Chrome 正在淘汰 —— 除非单域名确实不可能，否则不要选这条路。

## 上游更新会不会打断 Portal

会有影响，但影响面很窄，而且已经过实测。

**结论：Portal 模块本身不受上游更新影响，风险只在 5 个集成点，其中只有 1 个真的会出问题。**

以 `fb02c727` 基线 vs 上游 `ba7b3157`（领先 **63 个提交**）实测：

| | 结果 |
|---|---|
| 依赖的 9 个核心 API | **全部存在** —— 模块代码不受影响 |
| `astrbot/portal/**` 33 个新文件 + `attachment_staging.py` | 永不冲突（上游不会创建这些文件） |
| `default.py` | 可套用（尽管它每年被改 350+ 次） |
| `app.py` | 可套用 |
| `session_management_service.py` | 可套用 |
| `webchat_adapter.py` | 可套用（约 20 次改动/年） |
| `router.py` | **曾经冲突** —— 上游重构了 `build_api_router()` |

`router.py` 那次冲突正是本工具改用 `integrate_router.py` 的原因。改造后同一套脚本在
**旧基线和上游最新上都能完整跑通**（预检 → 套用 → 回滚 → 再套用），且回滚后 5 个文件与原始逐字节一致。

### 上游更新后的操作

1. 更新前先 `./rollback.sh`（或确保能重新套用）
2. 拉取上游更新
3. 重跑 `./preflight.sh` —— 它会告诉你：
   - 9 个核心 API 是否还在（**这一项失败才是真的麻烦**，说明上游改了 Portal 依赖的接口，
     需要改 Portal 模块本身的代码）
   - `router.py` 结构是否仍可识别（若上游又换了第三种写法，脚本会明确报错让你人工挂载）
   - 补丁能否套用
4. 通过就 `./apply.sh`

### 长期建议：用 rebase，不要靠打补丁追上游

这个补丁包是**部署工具**，不是维护策略。持续跟进上游的正确做法是在
`nings/AstrBot` 里把 `feat/portal-multiuser` 定期 rebase 到上游 master，
让冲突在有完整历史和测试的开发环境里解决，然后重新生成补丁：

```bash
git fetch https://github.com/AstrBotDevs/AstrBot.git master
git rebase FETCH_HEAD          # 在 feat/portal-multiuser 上
# 解决冲突后重新生成：
git diff FETCH_HEAD..HEAD -- 'astrbot/**/*.py' \
  ':(exclude)astrbot/dashboard/api/router.py' > deploy/portal-server-patch/portal-server.patch
git rev-parse FETCH_HEAD > deploy/portal-server-patch/BASE_COMMIT
```

在服务器上硬解冲突是最坏的选择 —— 那 5 个文件都在认证、路由与消息投递的关键路径上。

## 验证记录

在**两个**基线的干净副本上完整跑过：`fb02c727`（旧）与上游 `ba7b3157`（领先 63 提交）。

两者结果一致：

- `preflight.sh` 7 项全过
- `apply.sh` 套用成功 —— 旧基线走「调用序列」形态，上游走「元组」形态
- 重复套用被正确拒绝
- **真实启动**：portal 数据库自动建成，`/api/v1/portal/auth/me` 与 `/chat/sessions`
  返回 **401**（而非套用前的 404），确认路由已注册，无导入/属性错误
- `rollback.sh` 后 5 个被改文件与原始**逐字节一致**，`astrbot/portal` 与
  `attachment_staging.py` 均已移除，可再次套用（幂等）

### 增量升级（`upgrade.sh`）

在模拟「已套旧补丁的非 git 源码树」的沙盒上跑过四项：

| 检查 | 结果 |
|---|---|
| 升级前下载路由的方法 | `['GET']` —— 复现了线上 `HEAD → 405` |
| 升级后 | `['GET', 'HEAD']` |
| 「旧补丁 + 增量升级」vs「全新套用新补丁」 | **逐文件一致**，两条路径等价 |
| `--rollback` 后 vs 升级前 | **逐字节一致** |
| 升级后再跑 `rollback.sh` | 完全回到未套补丁的原始状态 |

最后一项是专门验证上面那条「补进基础备份」的逻辑 —— 没有它，升级过的机器做完整回滚会留下残骸。

### 单元测试

都只存在于 fork 里，不在补丁范围内。

- `tests/unit/test_webchat_attachment_staging.py`（8 个）—— 直接调用
  `fs._normalize_rw_path` 这个原本抛 `PermissionError` 的函数来判定，而不是复述它的逻辑。
  覆盖：暂存前确实被拒、暂存后可读、产物是真拷贝（`st_nlink == 1`）、
  图片与引用消息内的附件同样被改写、**别的用户仍然读不到**、
  非附件目录的路径保持原样、重复暂存不重复拷贝、拷贝失败时退回原路径而不抛异常。
- `tests/test_portal_attachment_isolation.py`（新增 2 个，共 12 个）——
  HEAD 探测对自己的附件返回 200 且 body 为空；对**别人**的附件返回 404 而非 403，
  不让 HEAD 变成 GET 路径拒绝充当的存在性探针。

### 生产部署记录

`8.163.105.82`（`/home/nw312/AstrBot`，非 git 解压包，FastAPI，venv）：

| | 升级前 | 升级后 |
|---|---|---|
| `HEAD /api/v1/portal/files/<id>` | **405** | **401** ✅ |
| `GET /api/v1/portal/files/<id>` | 401 | 401 |
| `GET /api/v1/portal/auth/me` | 401 | 401 |

2026-08-03 用 `upgrade.sh` 完成，`data/` 未受影响。
