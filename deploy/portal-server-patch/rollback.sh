#!/usr/bin/env bash
# Portal 服务端补丁 —— 还原。
#
# 删除补丁新增的 astrbot/portal/ 与 attachment_staging.py，并用 apply.sh 留下的备份还原那 5 个
# 被修改的文件。不动 data/ —— 已建的 portal 数据表和配置项会保留，
# 重新套用时可直接复用；确实要清干净的话见 README 的「彻底清除」。
#
# 用法：  ./rollback.sh /path/to/AstrBot
set -euo pipefail

TARGET="$(cd "${1:-.}" && pwd)"
BACKUP="$TARGET/.portal-patch-backup"

[ -d "$BACKUP" ] || { echo "找不到备份目录 $BACKUP —— 无法自动还原。"; exit 1; }

echo "=== 还原被修改的文件 ==="
while IFS= read -r f; do
  rel="${f#"$BACKUP"/}"
  cp -p "$f" "$TARGET/$rel"
  echo "  已还原 $rel"
done < <(find "$BACKUP" -type f)

echo "=== 摘除 portal_router 挂载 ==="
PY="${PYTHON:-python3}"; [ -x "$TARGET/.venv/bin/python" ] && PY="$TARGET/.venv/bin/python"
"$PY" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/integrate_router.py" "$TARGET" --remove | sed 's/^/  /' || true

echo "=== 移除补丁新增的模块 ==="
if [ -d "$TARGET/astrbot/portal" ]; then
  rm -rf "$TARGET/astrbot/portal"
  echo "  已删除 astrbot/portal"
else
  echo "  astrbot/portal 不存在，跳过"
fi
# 补丁新增的单文件。必须删干净：留下它，下次 apply 会因文件已存在而失败。
STAGING="$TARGET/astrbot/core/platform/sources/webchat/attachment_staging.py"
if [ -f "$STAGING" ]; then
  rm -f "$STAGING"
  echo "  已删除 webchat/attachment_staging.py"
else
  echo "  webchat/attachment_staging.py 不存在，跳过"
fi

rm -rf "$BACKUP"
echo "  已清理备份目录"

echo
echo "还原完成 ✅ —— 请重启 AstrBot。"
echo "注意：data/ 下的 portal 数据表与配置项未删除（见 README「彻底清除」）。"
