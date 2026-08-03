#!/usr/bin/env bash
# Portal 服务端补丁 —— 套用。
#
# 会先跑一遍 preflight.sh；任何一项不通过就中止，不留下半套状态。
# 套用前把 5 个被修改的既有文件备份到 .portal-patch-backup/，
# rollback.sh 依赖这份备份还原。
#
# 用法：  ./apply.sh /path/to/AstrBot
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$(cd "${1:-.}" && pwd)"
PATCH="$HERE/portal-server.patch"
BACKUP="$TARGET/.portal-patch-backup"

# 会被改动的既有文件 —— 需要备份的就这几个。
# router.py 不在补丁里（上游会重构它），由 integrate_router.py 程序化挂载，
# 但同样要备份以便还原。
MODIFIED=(
  "astrbot/core/config/default.py"
  "astrbot/core/platform/sources/webchat/webchat_adapter.py"
  "astrbot/dashboard/api/app.py"
  "astrbot/dashboard/api/router.py"
  "astrbot/dashboard/services/session_management_service.py"
)

echo "=== 预检 ==="
"$HERE/preflight.sh" "$TARGET" || { echo; echo "预检未通过，已中止。未修改任何文件。"; exit 1; }

echo
echo "=== 备份既有文件 -> $BACKUP ==="
if [ -e "$BACKUP" ]; then
  echo "备份目录已存在，说明可能套用过。请先 ./rollback.sh 或手动清理。"; exit 1
fi
mkdir -p "$BACKUP"
for f in "${MODIFIED[@]}"; do
  mkdir -p "$BACKUP/$(dirname "$f")"
  cp -p "$TARGET/$f" "$BACKUP/$f"
  echo "  已备份 $f"
done

echo
echo "=== 套用补丁（portal 模块 + 附件暂存 + 4 个集成点） ==="
if git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$TARGET" apply "$PATCH"
  echo "  git apply 完成"
else
  patch -p1 -d "$TARGET" < "$PATCH"
  echo "  patch 完成"
fi

echo
echo "=== 挂载 portal_router（程序化，兼容上游两种结构） ==="
PY="${PYTHON:-python3}"; [ -x "$TARGET/.venv/bin/python" ] && PY="$TARGET/.venv/bin/python"
"$PY" "$HERE/integrate_router.py" "$TARGET" | sed 's/^/  /'

echo
echo "=== 套用后自检 ==="
[ -d "$TARGET/astrbot/portal" ] && echo "  ✅ astrbot/portal 已就位（$(find "$TARGET/astrbot/portal" -name '*.py' | wc -l | tr -d ' ') 个 .py）" || { echo "  ❌ astrbot/portal 缺失"; exit 1; }
# 两种形态都算挂载成功：元组条目 或 include_router 调用
grep -qE 'portal_router,|include_router\(portal_router\)' "$TARGET/astrbot/dashboard/api/router.py" \
  && echo "  ✅ portal_router 已挂载" || { echo "  ❌ router.py 未包含 portal_router"; exit 1; }

grep -q 'stage_webchat_attachments' "$TARGET/astrbot/core/platform/sources/webchat/webchat_adapter.py" \
  && echo "  ✅ 附件暂存已接入 webchat_adapter" || { echo "  ❌ webchat_adapter.py 未接入附件暂存"; exit 1; }

if (cd "$TARGET" && "$PY" -c "import astrbot.portal.api.router" 2>/tmp/portal-import-err); then
  echo "  ✅ astrbot.portal 可导入"
else
  echo "  ❌ 导入失败:"; sed 's/^/     /' /tmp/portal-import-err
  echo "     可执行 ./rollback.sh $TARGET 还原"
  exit 1
fi

cat <<EOF

套用完成 ✅

接下来：
  1. 重启 AstrBot
  2. 自检（返回 401 而不是 404 即为就绪）：
       curl -s -o /dev/null -w '%{http_code}\\n' http://<host>:6185/api/v1/portal/auth/me
  3. 首次启动会自动建 portal 相关的表，并把 portal 配置项合并进
     data/cmd_config.json（默认 self_registration_open=true，允许自助注册）

如需还原：  ./rollback.sh $TARGET
EOF
