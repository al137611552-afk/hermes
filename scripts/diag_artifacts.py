"""ADR 0021 工具产物化 自测：真 Api → 真 Conversation → 真注册表 → 真子进程（不含模型）。

用法（项目根目录下）：
    python scripts/diag_artifacts.py

单测覆盖的是各部件；这里验的是**接线**：产物入口有没有随工作区建起来、有没有交到
run_bash / web_fetch / 后台进程手上、拿到句柄后能不能用现成工具（grep_search/read_file）下钻。
逐项打 [PASS]/[FAIL]，全过退出码 0（末行 RESULT 一目了然）。用临时目录，不污染 data/。
Windows 上重点看：产物路径分隔符、后台进程 tee、命令输出编码。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# —— 按脚本位置定位 src/，不依赖 cwd ——
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
os.chdir(_ROOT)

from agentcore.config import load_config    # noqa: E402
from agentcore.bridge.api import Api        # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra else ""))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="artifact-wiring-"))
    ws = tmp / "proj"
    ws.mkdir()
    (ws / "gen.py").write_text(
        "import sys\n"
        "for i in range(3000):\n"
        "    if i == 1500: sys.stdout.write('SECRET_CODE=ZQ-7741\\n')\n"
        "    sys.stdout.write('noise %d ' % i + 'y'*90 + '\\n')\n"
        "sys.stdout.write('=== FINAL VERDICT: 3 failed ===\\n')\n",
        encoding="utf-8")

    cfg = load_config()
    cfg.agent.workspace = str(ws)
    cfg.agent.per_session_workspace = False
    cfg.agent.shell = "powershell" if os.name == "nt" else "bash"
    cfg.agent.screenshot = False
    cfg.mcp.enabled = False
    cfg.memory.enabled = False
    cfg.storage.db_path = str(tmp / "h.db")
    cfg.agent.permissions.allow = [f"run_{cfg.agent.shell}(*)"]

    api = Api(cfg, emit=lambda *a, **k: None)
    try:
        conv = api.active
        shell_tool = f"run_{cfg.agent.shell}"
        check("产物入口随工作区建起来", conv.artifacts is not None)
        check("后台进程管理器拿到同一个入口", conv.procs.artifacts is conv.artifacts)
        reg = conv.registry
        check("run_bash 拿到产物入口", reg.get(shell_tool)._artifacts is conv.artifacts)

        # —— 模型视角第一步：跑命令 ——
        py = "python" if os.name == "nt" else "python3"
        out = reg.get(shell_tool).run({"command": f"{py} gen.py"})
        check("工具结果被压成摘要（不再是 20 万字符）", len(out) < 20_000, f"{len(out)} 字符")
        check("尾部结论还在（老行为会把它截掉）", "FINAL VERDICT: 3 failed" in out)
        check("中间的 SECRET_CODE 不在摘要里（所以必须下钻）", "ZQ-7741" not in out)
        check("给了句柄", "art_0001" in out and ".hermes/artifacts/" in out)

        # —— 模型视角第二步：按句柄下钻，用的是现成工具、没有新工具 ——
        rel = [ln for ln in out.splitlines() if ".hermes/artifacts/" in ln][0]
        path = rel.split(".hermes/artifacts/")[1].split()[0].rstrip("]）)")
        art_rel = f".hermes/artifacts/{path}"
        hit = reg.get("grep_search").run({"pattern": "SECRET_CODE", "path": art_rel})
        check("grep_search 能在产物里找到中间那行", "ZQ-7741" in hit, hit.strip()[:60])
        head = reg.get("read_file").run({"path": art_rel, "offset": 1499, "limit": 3})
        check("read_file 带 offset 能定位读", "ZQ-7741" in str(head))

        # —— 全库检索不被产物污染 ——
        noise = reg.get("grep_search").run({"pattern": "noise 1", "path": "."})
        check("默认全库 grep 不扫产物", ".hermes" not in noise)
        check("产物没进 git 视野（自我忽略）",
              (ws / ".hermes" / ".gitignore").read_text(encoding="utf-8").strip() == "*")

        # —— 后台进程 tee ——
        py = "python" if os.name == "nt" else "python3"
        bg = reg.get(shell_tool).run({"command": f"{py} gen.py", "background": True})
        pid_id = int(bg.split("#")[1].split("（")[0])
        import time
        for _ in range(200):
            r = conv.procs.read(pid_id)
            if conv.procs._get(pid_id).proc.poll() is not None:
                break
            time.sleep(0.05)
        time.sleep(0.5)
        r = conv.procs.read(pid_id)
        art2 = (ws / r["artifact_rel"]) if r["artifact_rel"] else None
        check("后台进程 tee 落了完整日志", art2 is not None and art2.is_file())
        if art2:
            body = art2.read_text(encoding="utf-8")
            check("环形缓冲冲掉的早期输出仍在产物里", "noise 0 " in body and "ZQ-7741" in body,
                  f"{len(body):,} 字符")
    finally:
        api.close()

    print("\nRESULT:", "ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
