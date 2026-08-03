# 在标准版 AstrBot 上启用 Portal —— 分步操作手册

目标：让 `http://8.163.105.82:6185` 这台跑标准版 AstrBot 的服务器具备 Portal 能力，
从而对接挂在主站 `/portal` 下的聊天前端。

**为什么必须改服务端**：Portal 不在任何已发布的 AstrBot 版本里，它是
`nings/AstrBot` 的 `feat/portal-multiuser` 分支上的功能。标准版没有 `astrbot/portal/**`，
所以 `/api/v1/portal/*` 全部返回 404。没有任何开关能打开它 —— 路由是无条件挂载的，
代码里没有就是没有。

预计耗时 15–30 分钟（不含安全加固）。全程可回滚。

---

## 已确认的部署画像（8.163.105.82）

`detect-deployment.sh` 在目标机上的实际输出：

| 项 | 值 | 影响 |
|---|---|---|
| Docker | 否 | 不走派生镜像，直接改源码 |
| 源码目录 | `/home/nw312/AstrBot` | 后续命令的 `<ASTRBOT>` |
| Portal 已存在 | 否 | 干净，未套过补丁 |
| git 仓库 | **否**（解压的发布包） | 走 **4-B**；`apply.sh` 自动改用 `patch`。**没有 git 安全网 —— 第 1 步的备份必做** |
| 控制台框架 | **FastAPI** ✅ | 补丁适用（旧版 Quart 则不适用） |
| Python | `/home/nw312/AstrBot/.venv/bin/python` | 脚本会自动识别 venv，**无需**手动设 `PYTHON` |
| pip 安装 | 否，从源码目录加载 | 改源码即刻生效，不会被 site-packages 副本盖掉 |
| 补丁状态 | 基础补丁 + 附件升级（2026-08-03） | 再有改动走**增量升级**，不要重跑 `apply.sh` |

下文凡出现 `<ASTRBOT>` 一律替换为 `/home/nw312/AstrBot`。

> 换一台机器部署时，先重跑第 0 步重新判定，不要照搬这张表。

---

## 已经套过补丁的机器：只跑增量升级

> **8.163.105.82 已于 2026-08-03 完成本次升级**，实测 `HEAD /api/v1/portal/files/<id>`
> 由 **405 → 401**，`data/` 未受影响。下面的步骤留作再次升级或换机部署时参考。

不要为了几处改动重走下面的 0–8 步，那会把 33 个文件的 portal 模块整个拆掉重装。
用 `upgrade.sh`，只动 3 个文件，不碰 `data/`（用户、会话、上传全留着）：

```bash
# 本地：把补丁包重新传上去（脚本也一并更新）
scp -r deploy/portal-server-patch nw312@8.163.105.82:/tmp/

# 服务器：
ssh nw312@8.163.105.82
cd /tmp/portal-server-patch
./upgrade.sh /home/nw312/AstrBot
```

修的是两件事：终端用户上传的文件模型读不了；界面上每个文件附件都显示
「不可用或已被删除」。升级后按第 5 步重启，再按第 6 步验证。

撤销：`./upgrade.sh /home/nw312/AstrBot --rollback`（只回退本次升级，portal 本体不动）。

> 实测：在「旧补丁 + 增量升级」的树上比对「全新套用新补丁」的树，**逐文件一致**；
> `--rollback` 后与升级前逐字节一致；升级后再跑 `rollback.sh` 仍能完全回到未套补丁的状态。

---

## 第 0 步：判定部署方式

**在服务器上**执行，只读不改动：

```bash
./detect-deployment.sh
```

它会告诉你属于哪一类，第 4 步据此分支：

| 结论 | 走哪条 |
|---|---|
| Docker 部署 | 4-C |
| git 源码部署 | 4-A |
| 非 git 源码（解压包） | **4-B** ← 8.163.105.82 属于这类 |
| 装在 site-packages | 见文末「pip 安装的情况」 |

找不到源码目录时手动指定：`./detect-deployment.sh /实际/路径`

---

## 第 1 步：备份（非 git 部署必做）

这台不是 git 仓库，没有 `git checkout` 可退。`rollback.sh` 只依赖 `apply.sh` 建的备份目录，
所以动手前先留一份整机快照：

```bash
tar czf ~/AstrBot-backup-$(date +%Y%m%d-%H%M).tar.gz -C /home/nw312 AstrBot
ls -lh ~/AstrBot-backup-*.tar.gz      # 确认生成成功再继续
```

同时确认**重启方式**，第 5 步要用：

```bash
# 是不是 systemd 管的
systemctl list-units --type=service 2>/dev/null | grep -i astrbot

# 否则从占用 6185 的进程反查
PID=$(ss -lptn 'sport = :6185' 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
ps -o pid,ppid,user,cmd -p "$PID"     # 看父进程：screen / tmux / supervisor / nohup ...
```

把结果记下来 —— 第 5 步按这个来重启，别猜。

---

## 第 2 步：把补丁包传上去

在**本地**执行：

```bash
scp -r deploy/portal-server-patch  nw312@8.163.105.82:/tmp/
```

---

## 第 3 步：预检（只读，不改任何文件）

```bash
ssh nw312@8.163.105.82
cd /tmp/portal-server-patch
./preflight.sh /home/nw312/AstrBot
```

**7 项全过才继续。** 任何一项失败都**不要**强行套用 —— 被改的文件都在认证、路由与消息投递的
关键路径上，套错了会以难排查的方式坏掉。

几种常见失败：

- **「不是 FastAPI」** → 这台 AstrBot 太老（旧版控制台用 Quart），补丁不适用，需先升级本体。
- **「核心 API 缺失」** → 版本与补丁不兼容，需要调整 Portal 模块代码，别硬套。
- **「astrbot/portal 已存在」** → 之前套过了，先确认状态。
- **「补丁无法套用」** → 版本差异过大，见 README「长期建议」用 rebase 重新生成补丁。

---

## 第 4 步：套用

### 4-B. 非 git 源码（解压包）—— 本次路径

`apply.sh` 会先自动重跑一遍预检，再备份 5 个被改文件，然后用 `patch` 套用：

```bash
./apply.sh /home/nw312/AstrBot
```

预期输出里应有：

```
已挂载 portal_router（形态：元组）        ← 或「调用序列」，取决于该版本的 router.py 结构
✅ astrbot/portal 已就位（33 个 .py）
✅ portal_router 已挂载
✅ 附件暂存已接入 webchat_adapter
✅ astrbot.portal 可导入
```

两种 `router.py` 结构都验证过 —— `portal_router` 不走文本补丁，由 `integrate_router.py`
识别结构后插入，所以上游怎么重构这个文件都不影响。

### 4-A. git 源码部署（其他机器参考）

```bash
cd <ASTRBOT> && git status     # 工作区必须干净，有改动先 stash
cd /tmp/portal-server-patch && ./apply.sh <ASTRBOT>
```

### 4-C. Docker 部署（其他机器参考）

> ⚠️ **不要在运行中的容器里打补丁** —— 容器一重建就没了。

**做法一：派生镜像（推荐）**。在补丁包目录建 `Dockerfile`：

```dockerfile
FROM soulter/astrbot:latest
COPY . /tmp/portal-patch
RUN /tmp/portal-patch/apply.sh /AstrBot && rm -rf /tmp/portal-patch
```

```bash
docker build -t astrbot-portal:local .
docker stop <原容器名> && docker rename <原容器名> <原容器名>-backup
docker run -d --name astrbot -p 6185:6185 \
  -v /your/data:/AstrBot/data  astrbot-portal:local
```

保留 `-backup` 容器，出问题立刻切回。

**做法二：挂载已打补丁的源码**

```bash
docker run -d --name astrbot -p 6185:6185 \
  -v /srv/AstrBot-patched:/AstrBot \
  -v /your/data:/AstrBot/data  soulter/astrbot:latest
```

> Docker 这条路径**未在本地实测**（本机 Docker daemon 未运行）。补丁与套用脚本已在源码树上
> 充分验证；Docker 部分只是把同一套脚本放进构建流程。

---

## 第 5 步：重启

按第 1 步查到的方式来：

```bash
sudo systemctl restart astrbot            # systemd
# 或
docker restart astrbot                    # Docker
# 或 screen/tmux/nohup：先停掉旧进程，再按原命令重启
```

---

## 第 6 步：验证

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:6185/api/v1/portal/auth/me
```

- **401** ✅ 成功 —— 路由已注册，「未登录」是正常响应
- **404** ❌ 未生效 —— 检查是否真的重启了、改的是不是运行中的那份代码

再看启动日志，应当有：

```
[portal] database initialized at .../data/portal/portal.db
[portal] registered the [...] session-rule guard
```

首次启动会自动建表并把 `portal` 配置段合并进 `data/cmd_config.json`，**无需手动迁移**。

### 顺带验证附件能不能读

Portal 跑起来后传一个 PDF 让它总结。看这个目录有没有出现副本：

```bash
ls /home/nw312/AstrBot/data/workspaces/*/uploads/
```

> 必须**重新上传**。暂存只在消息转换时对当前这条消息的附件生效，
> 升级前发出去的历史消息不会被追溯处理 —— 在老会话里追问「再读一遍」是没用的。

- **有副本、模型能读出内容** ✅ 正常
- **没有副本，模型说「受限目录」读不了** ❌ 附件暂存没生效，检查
  `astrbot/core/platform/sources/webchat/attachment_staging.py` 在不在、
  `webchat_adapter.py` 里有没有 `stage_webchat_attachments`

界面上那个文件气泡也要看：显示成**「不可用或已被删除」**说明跑的还是旧补丁。
前端发 HEAD 探测，而旧版下载路由只注册了 GET，FastAPI 对 GET-only 路由的 HEAD
直接回 405 —— 于是每个文件附件都被标成失效。新补丁已改成 `GET, HEAD`。

方法是否放行在路由层就能判定，用不着真的 attachment_id，也不用登录：

```bash
curl -s -o /dev/null -w 'HEAD=%{http_code}\n' -I http://8.163.105.82:6185/api/v1/portal/files/00000000-0000-0000-0000-000000000000
```

**401** = 已就绪（路由在，只是没带凭证）；**405** = 补丁太旧。

上传的文件本体在共享的 `data/attachments/`，而 agent 的文件工具**不允许**读那个目录
（非管理员只放行会话工作区等四类路径）。补丁会把附件复制进当次会话的工作区来解决。
详见 README 的「为什么要动 `webchat_adapter.py`」。

> 磁盘占用：每个会话各留一份副本（单文件上限 20 MiB，单用户每日 200 MiB）。
> 清理时删 `data/workspaces/<会话>/uploads/` 即可，不影响 Portal 数据。

**不要**为此去关 `computer_use_require_admin` 或把 Portal 用户加进 `admins_id` ——
那会同时解锁 shell 与 python 执行工具，等于给任何注册用户一个宿主机 RCE。

---

## 第 7 步：安全加固（公网开放前必做）

启动日志里会出现一条 CRITICAL：

```
PORTAL_UMO_ROUTING_NOT_ISOLATED
```

含义：**Portal 终端用户的输入会进入一个带全局工具的 tool-calling agent**，包括计算机操作类工具。
而 Portal 默认允许任何人自助注册。这条检查**只告警、不阻止启动**，服务看起来一切正常，
所以风险是静默的。

必须做：

1. **隔离 Portal 流量**。新建一个禁用危险工具与子 agent 的 AstrBotConfig profile，
   把 Portal 流量整体路由过去（控制台的 umo-routing 管理界面，或）：

   ```python
   await umop_config_router.update_route(
       'webchat:FriendMessage:webchat!*!*', '<isolated-profile-id>'
   )
   ```

   通配符要覆盖**所有** Portal 用户，不能只配一个。

2. **决定是否开放注册**。不打算对公众开放就在 `data/cmd_config.json` 的 `portal` 段
   把 `self_registration_open` 改成 `false`，改用邀请或 SSO。

3. **加密传输**。目前 `8.163.105.82:6185` 是明文 HTTP，而这条链路上传的
   `astrbot_portal_jwt` / `astrbot_portal_refresh` 是会话凭证，跨公网明文传输可被截获冒用。
   给它配 HTTPS，或让主站与 AstrBot 走内网/同机。

---

## 第 8 步：把主站指过去

远程返回 401 之后，在主站项目里改一行（`.env.local`，不入库）：

```bash
ASTRBOT_ORIGIN=http://8.163.105.82:6185
```

这个变量在**运行时**读取，改完重启 Next 即可，不必重新构建。

CORS **不需要配置** —— 主站以同源方式反代 `/api/v1/portal/*`，
浏览器全程只见一个域名，`cors_allowed_origins` 保持 `[]` 即可。

---

## 出问题了怎么办

```bash
cd /tmp/portal-server-patch
./rollback.sh /home/nw312/AstrBot
```

然后重启。实测：回滚后 5 个被改文件与原始**逐字节一致**，`astrbot/portal` 与
`attachment_staging.py` 移除干净，且可以再次套用（幂等）。

`rollback.sh` 只还原代码，保留 `data/`（重新套用时可复用）。要连数据一起清见 README。

备份脚本失效时的兜底（第 1 步那份快照）：

```bash
sudo systemctl stop astrbot
mv /home/nw312/AstrBot /home/nw312/AstrBot-broken
tar xzf ~/AstrBot-backup-<时间戳>.tar.gz -C /home/nw312
sudo systemctl start astrbot
```

---

## pip 安装的情况

若探测结果是「装在 site-packages」，可以打到那个目录，但**下次 `pip install -U` 会被覆盖**。
建议改成 git 源码部署或 Docker 派生镜像，长期可维护。

（8.163.105.82 不属于这种情况 —— 探测确认它从源码目录加载。）

## 上游更新后

见 README 的「上游更新会不会打断 Portal」。要点：Portal 模块本身不受影响
（依赖的 9 个核心 API 在上游最新版仍然齐全），风险集中在集成点，重跑 `preflight.sh` 即可判断。
