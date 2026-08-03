#!/usr/bin/env python3
"""把 portal_router 挂进 dashboard 的 API 路由表。

这一步不用文本补丁，因为 ``astrbot/dashboard/api/router.py`` 是上游会重构的文件：
它曾经是一串 ``router.include_router(x)`` 调用，后来被改成
``child_routers = (...)`` 元组 + 循环（循环末尾仍是 include_router）。
文本补丁绑死上下文，遇到这种重构就套不上；这里改成识别结构再插入，
两种形态都支持，且可重复执行不会插两次。

用法：  python3 integrate_router.py /path/to/AstrBot            # 挂载
        python3 integrate_router.py /path/to/AstrBot --remove   # 摘除
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

IMPORT_LINE = "from astrbot.portal.api.router import portal_router"
TUPLE_ENTRY = "        portal_router,"
CALL_LINE = "    router.include_router(portal_router)"


def find_router_file(root: Path) -> Path:
    path = root / "astrbot" / "dashboard" / "api" / "router.py"
    if not path.is_file():
        sys.exit(f"找不到 {path}")
    return path


def add_import(src: str) -> str:
    if IMPORT_LINE in src:
        return src
    # 挂在最后一处 fastapi 导入之后，避免插进相对导入块中间
    matches = list(re.finditer(r"^from fastapi[^\n]*\n", src, re.M))
    if not matches:
        sys.exit("router.py 里没有 fastapi 导入，文件结构与预期不符，已中止。")
    at = matches[-1].end()
    return src[:at] + "\n" + IMPORT_LINE + "\n" + src[at:]


def mount(src: str) -> tuple[str, str]:
    """返回 (新内容, 采用的形态)。"""
    # 形态 A：child_routers 元组（较新的上游）
    m = re.search(r"child_routers\s*=\s*\(\n(.*?)\n(\s*)\)", src, re.S)
    if m:
        if "portal_router," in m.group(1):
            return src, "元组（已挂载）"
        insert_at = m.end(1)
        return src[:insert_at] + "\n" + TUPLE_ENTRY + src[insert_at:], "元组"

    # 形态 B：一串 include_router 调用（较旧的上游）
    calls = list(re.finditer(r"^\s*router\.include_router\([a-z_]+\)\n", src, re.M))
    if calls:
        if "include_router(portal_router)" in src:
            return src, "调用序列（已挂载）"
        at = calls[-1].end()
        return src[:at] + CALL_LINE + "\n" + src[at:], "调用序列"

    sys.exit(
        "router.py 里既没有 child_routers 元组，也没有 include_router 调用序列。\n"
        "上游结构可能又变了 —— 请人工把 portal_router 挂进 build_api_router()。"
    )


def unmount(src: str) -> str:
    stripped = src
    for line in (IMPORT_LINE, TUPLE_ENTRY, CALL_LINE):
        stripped = stripped.replace(line + "\n", "")
    if stripped == src:
        # 本就没挂载：原样返回。不能顺手做空行规范化 ——
        # 那会改动文件里原有的连续空行，让「回滚后与原始一致」不成立。
        return src
    # 只在确实删掉了内容时，收拾导入处留下的多余空行
    return re.sub(r"\n{3,}", "\n\n", stripped)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    root = Path(sys.argv[1]).resolve()
    removing = "--remove" in sys.argv[2:]
    path = find_router_file(root)
    original = path.read_text(encoding="utf-8")

    if removing:
        updated = unmount(original)
        if updated == original:
            print("portal_router 本就未挂载，无需改动。")
            return
        path.write_text(updated, encoding="utf-8")
        print(f"已从 {path.name} 摘除 portal_router。")
        return

    updated, shape = mount(add_import(original))
    if updated == original:
        print(f"portal_router 已挂载（{shape}），无需改动。")
        return
    path.write_text(updated, encoding="utf-8")
    print(f"已挂载 portal_router 到 {path.name}（形态：{shape}）。")


if __name__ == "__main__":
    main()
