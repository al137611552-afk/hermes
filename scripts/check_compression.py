"""体检：对本机已存的每个会话，报告估算 token 数、以及在当前预算下「会不会触发 P6.2 压缩」。

用途：确认你真实聊天的内容是否够长到触发自动上下文压缩。无 GUI、无网络，只读 DB。

在项目根目录运行：
    python scripts/check_compression.py
可选指定库路径：
    python scripts/check_compression.py path\\to\\hermes.db
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许直接 python scripts/xxx.py 运行（把 src 加进路径）
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore.config import load_config  # noqa: E402
from agentcore.context import compress, estimate_tokens  # noqa: E402
from agentcore.providers import Message  # noqa: E402
from agentcore.store import Store  # noqa: E402


def main() -> None:
    cfg = load_config()
    cc = cfg.context
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else cfg.storage.resolve_db_path()

    print(f"DB: {db_path}")
    print(f"压缩开关 enabled={cc.enabled} | 预算 max_input_tokens={cc.max_input_tokens} "
          f"| 至少保留近 {cc.keep_recent_turns} 个用户回合\n")

    if not db_path.exists():
        print("⚠ 找不到该 DB 文件——可能你还没聊过天，或路径不对。")
        return

    store = Store(db_path, externalize_images=cfg.storage.externalize_images)
    sessions = store.list_sessions()
    if not sessions:
        print("（库里还没有任何会话）")
        return

    system = cfg.system_prompt  # 注：实际运行还会注入长期记忆，token 会再多一点
    any_trigger = False
    print(f"{'会话':<22}{'消息数':>6}{'估算tok':>9}  压缩?")
    print("-" * 60)
    for s in sessions:
        msgs = store.get_messages(s["id"])
        history = [Message(m["role"], m["content"]) for m in msgs]
        tok = estimate_tokens(history, system)
        res = compress(history, system,
                       budget=cc.max_input_tokens, keep_recent_turns=cc.keep_recent_turns)
        title = (s["title"] or "")[:20]
        mark = "✅ 会触发" if res.compressed else "—"
        if res.compressed:
            any_trigger = True
            mark += f"(丢{res.dropped}条 {res.before_tokens}->{res.after_tokens})"
        print(f"{title:<22}{len(msgs):>6}{tok:>9}  {mark}")

    print("-" * 60)
    near = cc.max_input_tokens
    if not any_trigger:
        biggest = max(
            (estimate_tokens([Message(m["role"], m["content"]) for m in store.get_messages(s["id"])], system)
             for s in sessions),
            default=0,
        )
        print(f"结论：没有任何会话达到压缩阈值。最大的一个约 {biggest} tok，预算 {near} tok。")
        print("想验证压缩，可临时把 config.yaml 的 context.max_input_tokens 调小"
              "（如 2000）再在那个会话里发一条，应能看到 🗜 提示。")
    else:
        print("结论：上面标 ✅ 的会话在发送时会触发压缩（前端会显示 🗜 提示）。")
    store.close()


if __name__ == "__main__":
    main()
