"""探针：跟 `codex mcp-server` 直接对话，把**原始 JSON-RPC** 打出来（不经 hermes）。

    python scripts/diag_codex_probe.py                 # 默认在临时目录、只读沙箱跑一句话
    python scripts/diag_codex_probe.py "你的提示词"

为什么要它：hermes 拿到结果会先 `convert_result` 转成纯文本再显示，**结构就丢了**。
而两件事必须看结构才能实现：
  ① 续话用的 thread id 叫什么键、在哪一层（决定 codex-reply 能不能自动接续）；
  ② 过程通知（notifications/progress）的真实形状与粒度（决定实时流够不够看）。

只读沙箱 + 一句话提示，消耗极小。**输出里可能带你的路径**，贴出来前自己扫一眼。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time

PROMPT = sys.argv[1] if len(sys.argv) > 1 else "只回答 OK 两个字。不要读文件、不要改文件。"


def main() -> int:
    ws = tempfile.mkdtemp(prefix="codexprobe_")
    p = subprocess.Popen(["codex", "mcp-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, bufsize=1, encoding="utf-8",
                         errors="replace")

    def send(obj):
        p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        p.stdin.flush()

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "hermes-probe", "version": "1"}}})
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    # _meta.progressToken 是拿到过程通知的前提——不给就只有最终结果
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "codex",
        "arguments": {"prompt": PROMPT, "sandbox": "read-only",
                      "approval-policy": "never", "cwd": ws},
        "_meta": {"progressToken": "probe-1"}}})

    print(f"[探针] 工作目录 {ws}\n[探针] 已发 initialize + tools/call，等回复（最多 180 秒）…\n")
    deadline = time.time() + 180
    n = 0
    while time.time() < deadline:
        line = p.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        n += 1
        try:
            msg = json.loads(line)
        except ValueError:
            print(f"[{n}] (非 JSON) {line[:300]}")
            continue
        kind = msg.get("method") or (f"result#{msg.get('id')}" if "result" in msg else "error")
        print(f"[{n}] {kind}")
        print(json.dumps(msg, ensure_ascii=False, indent=2)[:1500])
        print("-" * 60)
        if msg.get("id") == 2:          # 最终结果到了
            break
    p.kill()
    err = (p.stderr.read() or "").strip()
    if err:
        print("── stderr ──")
        print(err[:800])
    print(f"\n[探针] 共收到 {n} 条消息。把上面的输出原样发回即可（先扫一眼有没有敏感路径）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
