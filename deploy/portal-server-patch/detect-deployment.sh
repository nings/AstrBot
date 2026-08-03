#!/usr/bin/env bash
# 判定这台机器上的 AstrBot 是怎么部署的 —— 决定后续该走哪条套用路径。
#
# 只读，不改任何东西。在**服务器上**执行：
#   ./detect-deployment.sh            # 自动查找
#   ./detect-deployment.sh /AstrBot   # 指定源码目录
set -uo pipefail

HINT="${1:-}"
echo "===== AstrBot 部署方式探测 ====="
echo

# ---------- 1. Docker ----------
echo "[Docker]"
if command -v docker >/dev/null 2>&1; then
  CIDS=$(docker ps --filter "publish=6185" --format '{{.ID}} {{.Image}} {{.Names}}' 2>/dev/null)
  if [ -n "$CIDS" ]; then
    echo "  ⬢ 发现监听 6185 的容器："
    echo "$CIDS" | sed 's/^/     /'
    CID=$(echo "$CIDS" | head -1 | awk '{print $1}')
    echo "     源码目录（容器内）: $(docker exec "$CID" sh -c 'ls -d /AstrBot 2>/dev/null || pwd' 2>/dev/null)"
    echo "     挂载:"
    docker inspect -f '{{range .Mounts}}       {{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' "$CID" 2>/dev/null
    echo
    echo "  → 结论：Docker 部署。走 README 的「Docker」步骤（构建派生镜像），"
    echo "    不要在运行中的容器里直接打补丁 —— 容器重建即丢失。"
    exit 0
  fi
  echo "  未发现监听 6185 的容器"
else
  echo "  未安装 docker"
fi

# ---------- 2. 源码目录 ----------
echo
echo "[源码目录]"
CANDIDATES=()
[ -n "$HINT" ] && CANDIDATES+=("$HINT")
CANDIDATES+=(/AstrBot /opt/AstrBot /srv/AstrBot "$HOME/AstrBot" ./)
# 从监听 6185 的进程反查工作目录（Linux）
if command -v ss >/dev/null 2>&1; then
  PID=$(ss -lptn 'sport = :6185' 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  [ -n "${PID:-}" ] && [ -r "/proc/$PID/cwd" ] && CANDIDATES+=("$(readlink -f "/proc/$PID/cwd")")
fi

ROOT=""
for c in "${CANDIDATES[@]}"; do
  if [ -f "$c/main.py" ] && [ -d "$c/astrbot" ]; then ROOT="$(cd "$c" && pwd)"; break; fi
done

if [ -z "$ROOT" ]; then
  echo "  ❌ 没找到 AstrBot 源码目录"
  echo "  → 请手动指定：./detect-deployment.sh /实际/路径"
  echo "    若确实找不到源码，可能是 pip/uv 装进了 site-packages —— 见下方 [pip 安装]"
else
  echo "  ✅ 源码目录: $ROOT"
  echo "     Portal 是否已存在: $([ -d "$ROOT/astrbot/portal" ] && echo '是（可能已打过补丁）' || echo '否')"
  if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    echo "     git 仓库: 是"
    echo "       当前提交: $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)"
    echo "       当前分支: $(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null)"
    echo "       远端: $(git -C "$ROOT" remote get-url origin 2>/dev/null || echo '无')"
    echo "       工作区是否干净: $([ -z "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ] && echo 是 || echo '否（有本地改动，套补丁前请先处理）')"
    echo "  → 结论：git 源码部署。走 README 的标准步骤，apply.sh 会用 git apply。"
  else
    echo "     git 仓库: 否（可能是解压的发布包）"
    echo "  → 结论：非 git 源码部署。apply.sh 会自动改用 patch 命令。"
  fi
  echo
  echo "  控制台框架: $(grep -qE '^from fastapi import' "$ROOT/astrbot/dashboard/api/app.py" 2>/dev/null && echo 'FastAPI ✅' || echo '不是 FastAPI ❌ —— 补丁不适用')"
  echo "  Python: $([ -x "$ROOT/.venv/bin/python" ] && echo "$ROOT/.venv/bin/python（venv）" || echo "$(command -v python3)")"
fi

# ---------- 3. pip 安装 ----------
echo
echo "[pip 安装]"
PY="$(command -v python3 || true)"
[ -n "$ROOT" ] && [ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python"
if [ -n "$PY" ]; then
  # 在 / 下执行：否则当前目录里的 astrbot/ 会遮蔽真正安装的那个，
  # 报出来的路径就不是「装在哪」而是「从哪个目录跑的」。
  LOC=$(cd / && "$PY" -c "import astrbot,os;print(os.path.dirname(astrbot.__file__))" 2>/dev/null)
  if [ -n "$LOC" ]; then
    echo "  astrbot 包位置: $LOC"
    case "$LOC" in
      *site-packages*|*dist-packages*)
        echo "  ⚠️  装在 site-packages 里 —— 补丁需打到该目录，但下次 pip 升级会被覆盖。"
        echo "     建议改为 git 源码部署或 Docker 派生镜像。" ;;
      *) echo "  ✅ 从源码目录加载，不是 pip 安装" ;;
    esac
  else
    echo "  当前解释器导入不到 astrbot（正常，若它跑在别的环境里）"
  fi
fi

echo
echo "===== 探测结束 ====="
