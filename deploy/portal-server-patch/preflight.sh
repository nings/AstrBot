#!/usr/bin/env bash
# Portal 服务端补丁 —— 兼容性预检。
#
# 只读：不修改任何文件。在 apply.sh 之前跑，确认目标机满足全部前提。
# 任何一项失败都会明确说明原因，因为补丁里 5 个被修改的文件都在
# 认证、路由与消息投递的关键路径上，套错了会以难以排查的方式坏掉。
#
# 用法：  ./preflight.sh /path/to/AstrBot
set -uo pipefail

TARGET="${1:-.}"
PATCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/portal-server.patch"
fail=0
ok()   { printf '  ✅ %s\n' "$1"; }
bad()  { printf '  ❌ %s\n' "$1"; fail=$((fail+1)); }
warn() { printf '  ⚠️  %s\n' "$1"; }

echo "目标目录: $TARGET"
echo "补丁文件: $PATCH"
echo

echo "[1/7] 目标是否是 AstrBot 源码树"
if [ -f "$TARGET/astrbot/__init__.py" ] && [ -f "$TARGET/main.py" ]; then
  ok "找到 astrbot/ 与 main.py"
else
  bad "不像 AstrBot 源码根目录（缺 astrbot/__init__.py 或 main.py）"
fi

echo "[2/7] Portal 是否已存在（避免重复套用）"
if [ -d "$TARGET/astrbot/portal" ]; then
  bad "astrbot/portal 已存在 —— 补丁可能已套用过，请先确认再决定"
else
  ok "astrbot/portal 不存在，可以套用"
fi

echo "[3/7] 控制台是否为 FastAPI（老版本是 Quart，补丁不适用）"
APP="$TARGET/astrbot/dashboard/api/app.py"
if [ -f "$APP" ] && grep -qE '^from fastapi import' "$APP"; then
  ok "dashboard/api/app.py 使用 FastAPI"
elif [ -f "$APP" ]; then
  bad "dashboard/api/app.py 不是 FastAPI（可能是 Quart 老版本），补丁不适用"
else
  bad "缺少 astrbot/dashboard/api/app.py"
fi

echo "[4/7] Portal 依赖的核心 API 是否齐备"
check_sym() {  # 文件 正则 说明
  if [ -f "$TARGET/$1" ] && grep -qE "$2" "$TARGET/$1"; then ok "$3"; else bad "$3 —— 缺失于 $1"; fi
}
check_sym "astrbot/dashboard/api/router.py"              'build_api_router'                'router.py: build_api_router'
check_sym "astrbot/dashboard/api/auth.py"                'AuthContext'                     'auth.py: AuthContext'
check_sym "astrbot/dashboard/api/auth.py"                'require_system_scope'            'auth.py: require_system_scope'
check_sym "astrbot/dashboard/services/chat_service.py"   'class ChatService'               'chat_service.py: ChatService'
check_sym "astrbot/dashboard/services/chat_service.py"   'ChatServiceError'                'chat_service.py: ChatServiceError'
check_sym "astrbot/core/platform/message_type.py"        'class MessageType'               'core: MessageType'
check_sym "astrbot/core/utils/astrbot_path.py"           'get_astrbot_data_path'           'core: get_astrbot_data_path'
# 附件暂存用到的：把上传文件复制进会话工作区，否则终端用户读不了自己传的文件
check_sym "astrbot/core/workspace.py"                    'resolve_workspace_root_for_umo'  'workspace.py: resolve_workspace_root_for_umo'
check_sym "astrbot/core/platform/sources/webchat/webchat_adapter.py" 'async def convert_message' 'webchat_adapter.py: convert_message'

echo "[5/7] Python 依赖（补丁不引入新的运行时依赖，仅确认既有的可用）"
PY="${PYTHON:-python3}"
[ -x "$TARGET/.venv/bin/python" ] && PY="$TARGET/.venv/bin/python"
echo "  解释器: $PY"
# 不吞 stderr：解释器本身出错时要能看见原因
"$PY" - <<'PYEOF' || bad "依赖检查未通过（见上）"
import importlib.util, sys
need = ("fastapi", "sqlalchemy", "aiosqlite", "pydantic")
missing = [m for m in need if importlib.util.find_spec(m) is None]
if missing:
    print("  ❌ 缺少:", ", ".join(missing)); sys.exit(1)
print("  ✅ " + " / ".join(need) + " 均可用")
PYEOF

echo "[6/7] router.py 结构是否可识别（portal_router 由脚本挂载，不走补丁）"
if grep -qE 'child_routers\s*=\s*\(' "$TARGET/astrbot/dashboard/api/router.py" 2>/dev/null; then
  ok "识别到 child_routers 元组结构（较新上游）"
elif grep -qE 'router\.include_router\(' "$TARGET/astrbot/dashboard/api/router.py" 2>/dev/null; then
  ok "识别到 include_router 调用序列（较旧上游）"
else
  bad "router.py 结构无法识别 —— 上游可能又重构了，需人工挂载 portal_router"
fi

echo "[7/7] 补丁能否干净套用（dry-run，不写入）"
if [ ! -f "$PATCH" ]; then
  bad "找不到 portal-server.patch"
elif git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1; then
  if git -C "$TARGET" apply --check "$PATCH" 2>/tmp/portal-apply-err; then
    ok "git apply --check 通过"
  else
    bad "git apply --check 失败:"; sed 's/^/       /' /tmp/portal-apply-err
  fi
else
  warn "目标不是 git 仓库，改用 patch --dry-run"
  if patch -p1 -d "$TARGET" --dry-run < "$PATCH" >/tmp/portal-apply-err 2>&1; then
    ok "patch --dry-run 通过"
  else
    bad "patch --dry-run 失败:"; tail -20 /tmp/portal-apply-err | sed 's/^/       /'
  fi
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "全部通过 ✅ —— 可以执行 ./apply.sh $TARGET"
else
  echo "$fail 项失败 ❌ —— 请先解决后再套用，不要强行 apply"
fi
exit "$fail"
