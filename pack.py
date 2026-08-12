#!/usr/bin/env python3
"""把源码打成给用户的 zip（解压即用，不是 exe——exe 走 build.ps1）。

    python pack.py                 # 自用包：**含 .env**（省得手填 key），别外发
    python pack.py --dist          # 分发包：.env 用空模板占位，可安全外发
    python pack.py --name xxx.zip  # 指定文件名（默认 hermes-dev-<版本>[-日期].zip）

约定（见 CLAUDE.md「打包分发」）：
- 解压后必须是**单层** `hermes-dev/`（别多套一层目录）；
- 排除缓存/构建产物/数据库/会话数据/git 目录/旧 zip——那些既没用又可能带隐私。
"""
from __future__ import annotations

import argparse
import re
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOP = "hermes-dev"          # 解压后的单层目录名

# 目录级排除：命中即整棵子树跳过
SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "build", "dist", "data",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".playwright-mcp",
    "node_modules", ".idea", ".vscode",
}
# 文件级排除（后缀 / 精确名）
SKIP_SUFFIX = {".pyc", ".pyo", ".db", ".zip", ".log"}
SKIP_NAMES = {".DS_Store", "providers.yaml", "user_models.yaml"}   # 后两个是运行时配置，各机器自己的

ENV_TEMPLATE = """# 填入你的 API key 后保存（此文件不要外发）。
# 火山方舟（ark-* 档案共用）
ARK_API_KEY=
"""


def version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text("utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def wanted(p: Path) -> bool:
    rel = p.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in rel.parts):
        return False
    if p.suffix in SKIP_SUFFIX or p.name in SKIP_NAMES:
        return False
    if p.name == ".env":            # .env 单独处理（自用含 key / 分发用模板）
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", action="store_true", help="分发包：不带真实 key")
    ap.add_argument("--name", default="", help="输出文件名")
    ap.add_argument("--out", default=str(ROOT.parent), help="输出目录（默认项目上一级）")
    a = ap.parse_args()

    tag = version() + ("" if a.dist else "-" + time.strftime("%Y%m%d"))
    name = a.name or f"hermes-dev-{tag}.zip"
    out = Path(a.out).expanduser().resolve() / name

    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(ROOT.rglob("*")):
            if p.is_dir() or not wanted(p):
                continue
            z.write(p, f"{TOP}/{p.relative_to(ROOT).as_posix()}")
            n += 1
        env = ROOT / ".env"
        if a.dist or not env.exists():
            z.writestr(f"{TOP}/.env", ENV_TEMPLATE)
        else:
            z.write(env, f"{TOP}/.env")     # 自用包含真实 key
        n += 1

    mb = out.stat().st_size / 1024 / 1024
    print(f"✅ {out}  （{n} 个文件，{mb:.1f} MB）")
    if not a.dist:
        print("⚠ 这个包**含真实 API key**（.env）——只给自己用，别外发/传公开处。")
    print("解压后：cd hermes-dev && pip install -e . && python -m agentcore.app")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
