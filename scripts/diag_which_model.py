"""这次对话到底用的哪个模型档？（读本机 hermes.db + config，不触网、不花钱）

    python scripts/diag_which_model.py            # 最近 10 个会话
    python scripts/diag_which_model.py 20         # 最近 20 个

为什么要它：报错里**不带模型档名**（v3.76.x 之前都不带），而一台机器上常常有好几个档——
主对话一个、`agent.subagent_model` 一个、`context.summary_model` 一个、视觉档一个。
"我用的是 X" 是记忆，`sessions.model` 是**记录**：hermes 把每个会话当时用的档名落了库。
排查跨 provider 的报错时，先用记录把档对上，再去查那个档对应的账户——顺序反了会一路查错账户。

输出把档名 → provider 实现 / base_url 一并展开，因为 **provider 字段决定走哪套 SDK**，
而两套 SDK 对同一个 HTTP 错误的报法不一样（openai 那套报裸的 `APIStatusError: ...`）。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore.config import load_config  # noqa: E402


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    cfg = load_config()
    db = cfg.storage.resolve_db_path()      # None 时它自己落到 ROOT/data/hermes.db
    print(f"[库] {db}{'' if db.exists() else '  ← 不存在'}")
    print(f"[当前 active_model] {cfg.active_model or '（未设）'}"
          f"   [委派档 subagent_model] {cfg.agent.subagent_model or '（跟随主模型）'}"
          f"   [压缩摘要档 summary_model] {getattr(cfg.context, 'summary_model', '') or '（跟随主模型）'}")

    print("\n■ 已配置的模型档")
    for name, mc in cfg.models.items():
        mark = " ←当前" if name == cfg.active_model else ""
        print(f"  {name:24s} provider={mc.provider:9s} model={mc.model:24s} "
              f"base_url={mc.base_url or '（默认官方端点）'}{mark}")

    if not db.exists():
        print("\n（没有会话库，跳过历史）")
        return 0
    print(f"\n■ 最近 {n} 个会话当时用的档")
    con = sqlite3.connect(str(db))
    rows = con.execute("SELECT id, title, model, created_at FROM sessions "
                       "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    for sid, title, model, ts in rows:
        when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
        mc = cfg.models.get(model)
        detail = (f"provider={mc.provider}, base_url={mc.base_url or '官方'}"
                  if mc else "⚠ 这个档现在已经不在配置里了")
        print(f"  #{sid:<4} {when}  档={model or '（空）'}\n        {detail}\n        标题：{(title or '')[:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
