#!/usr/bin/env bash
# Portal 服务端补丁 —— 增量升级：附件可读性修复。
#
# 给**已经套过本补丁包**的机器用。只动 3 个文件，不碰 astrbot/portal 的其余部分，
# 也不碰 data/ —— 用户、会话、上传记录全部保留。
#
#   新增  astrbot/core/platform/sources/webchat/attachment_staging.py
#   改动  astrbot/core/platform/sources/webchat/webchat_adapter.py   +9
#   改动  astrbot/portal/api/file_routes.py                          +11/−1
#
# 解决两个独立问题：
#   1. 终端用户传的文件，模型读不了（附件在共享目录，不在读取白名单里）
#   2. 界面上每个文件附件都显示「不可用或已被删除」（下载路由不接受 HEAD 探测）
#
# 用法：  ./upgrade.sh /path/to/AstrBot              # 升级
#         ./upgrade.sh /path/to/AstrBot --rollback   # 只撤销本次升级
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$(cd "${1:-.}" && pwd)"
PATCH="$HERE/upgrade-attachments.patch"
UP_BACKUP="$TARGET/.portal-upgrade-backup"
BASE_BACKUP="$TARGET/.portal-patch-backup"

ADAPTER="astrbot/core/platform/sources/webchat/webchat_adapter.py"
STAGING="astrbot/core/platform/sources/webchat/attachment_staging.py"
FILE_ROUTES="astrbot/portal/api/file_routes.py"
MODIFIED=("$ADAPTER" "$FILE_ROUTES")

PY="${PYTHON:-python3}"; [ -x "$TARGET/.venv/bin/python" ] && PY="$TARGET/.venv/bin/python"

apply_patch() {  # $1 = 额外参数（如 -R 反向）
  if git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$TARGET" apply ${1:+"$1"} "$PATCH"
  else
    patch -p1 -d "$TARGET" ${1:+"$1"} < "$PATCH"
  fi
}

# ---------------------------------------------------------------- 回滚
if [ "${2:-}" = "--rollback" ]; then
  [ -d "$UP_BACKUP" ] || { echo "找不到 $UP_BACKUP —— 本次升级没做过，或已回滚过。"; exit 1; }
  echo "=== 还原本次升级改动的文件 ==="
  while IFS= read -r f; do
    rel="${f#"$UP_BACKUP"/}"
    cp -p "$f" "$TARGET/$rel"; echo "  已还原 $rel"
  done < <(find "$UP_BACKUP" -type f)
  rm -f "$TARGET/$STAGING" && echo "  已删除 attachment_staging.py"
  rm -rf "$UP_BACKUP"
  echo
  echo "升级已撤销 ✅ —— 请重启 AstrBot。portal 本体与 data/ 未受影响。"
  exit 0
fi

# ---------------------------------------------------------------- 预检
echo "=== 预检（只读）==="
fail=0
ok()  { printf '  ✅ %s\n' "$1"; }
bad() { printf '  ❌ %s\n' "$1"; fail=$((fail+1)); }

[ -d "$TARGET/astrbot/portal" ] \
  && ok "astrbot/portal 已存在（本补丁包已套过）" \
  || bad "astrbot/portal 不存在 —— 这台还没套过基础补丁，请直接用 ./apply.sh，不要用本脚本"

[ -f "$TARGET/$STAGING" ] \
  && bad "attachment_staging.py 已存在 —— 本升级已套过，无需重复" \
  || ok "尚未升级过"

[ -f "$PATCH" ] && ok "找到 upgrade-attachments.patch" || bad "缺少 upgrade-attachments.patch"

if [ "$fail" -eq 0 ]; then
  if git -C "$TARGET" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$TARGET" apply --check "$PATCH" 2>/tmp/portal-upgrade-err \
      && ok "git apply --check 通过" \
      || { bad "git apply --check 失败:"; sed 's/^/       /' /tmp/portal-upgrade-err; }
  else
    patch -p1 -d "$TARGET" --dry-run < "$PATCH" >/tmp/portal-upgrade-err 2>&1 \
      && ok "patch --dry-run 通过" \
      || { bad "patch --dry-run 失败:"; tail -20 /tmp/portal-upgrade-err | sed 's/^/       /'; }
  fi
fi

[ "$fail" -eq 0 ] || { echo; echo "$fail 项失败 ❌ —— 已中止，未修改任何文件。"; exit 1; }

# ---------------------------------------------------------------- 备份
echo
echo "=== 备份 -> $UP_BACKUP ==="
[ -e "$UP_BACKUP" ] && { echo "备份目录已存在，请先 ./upgrade.sh $TARGET --rollback"; exit 1; }
mkdir -p "$UP_BACKUP"
for f in "${MODIFIED[@]}"; do
  mkdir -p "$UP_BACKUP/$(dirname "$f")"; cp -p "$TARGET/$f" "$UP_BACKUP/$f"; echo "  已备份 $f"
done

# webchat_adapter.py 是本次升级新引入的改动点，基础补丁当初没备份它。
# 补进基础备份目录，否则日后跑 rollback.sh 会漏还原它 ——
# 那会留下一个 import 已删除模块的 adapter，AstrBot 起不来。
if [ -d "$BASE_BACKUP" ] && [ ! -f "$BASE_BACKUP/$ADAPTER" ]; then
  mkdir -p "$BASE_BACKUP/$(dirname "$ADAPTER")"
  cp -p "$TARGET/$ADAPTER" "$BASE_BACKUP/$ADAPTER"
  echo "  已补进基础备份（保证 rollback.sh 仍能完整还原）"
fi

# ---------------------------------------------------------------- 套用
echo
echo "=== 套用增量补丁 ==="
apply_patch && echo "  完成"

# ---------------------------------------------------------------- 自检
echo
echo "=== 自检 ==="
[ -f "$TARGET/$STAGING" ] && echo "  ✅ attachment_staging.py 已就位" \
  || { echo "  ❌ attachment_staging.py 缺失"; exit 1; }
grep -q 'stage_webchat_attachments' "$TARGET/$ADAPTER" \
  && echo "  ✅ webchat_adapter 已接入附件暂存" || { echo "  ❌ 未接入"; exit 1; }

(cd "$TARGET" && "$PY" - <<'PYEOF'
import sys
sys.path.insert(0, ".")
from astrbot.portal.api import file_routes
from astrbot.core.platform.sources.webchat import attachment_staging  # noqa: F401

route = next(r for r in file_routes.router.routes
             if "attachment_id" in getattr(r, "path", ""))
methods = sorted(route.methods)
assert "HEAD" in methods, f"下载路由仍不接受 HEAD: {methods}"
print(f"  ✅ 下载路由方法: {methods}")
print("  ✅ attachment_staging 可导入")
PYEOF
) || { echo "  ❌ 自检失败 —— 可执行 ./upgrade.sh $TARGET --rollback 撤销"; exit 1; }

cat <<EOF

升级完成 ✅

接下来：
  1. 重启 AstrBot
  2. 确认下载路由已接受 HEAD（405 表示没生效）：
       curl -s -o /dev/null -w 'HEAD=%{http_code}\\n' -I \\
         http://127.0.0.1:6185/api/v1/portal/files/00000000-0000-0000-0000-000000000000
     期望 401（未带凭证），不再是 405
  3. 在 Portal 里传一个 PDF 让它解读，然后确认副本已生成：
       ls $TARGET/data/workspaces/*/uploads/

撤销本次升级：  ./upgrade.sh $TARGET --rollback
EOF
