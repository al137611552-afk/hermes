"""无头命令行入口（FR-11.7）：单任务进 / 单任务出，不起 GUI。解锁 CI、脚本化、批处理。

用法：
    hermes-cli "把 src 下的测试都跑一遍，报告结果" -w ./myproj
    echo "调研这个项目的架构" | hermes-cli -w ./myproj -      # 从 stdin 读任务
    hermes-cli "梳理架构" -w . --plan                          # 只读规划态，不改文件
    hermes-cli "修复测试" -w . --json                          # 机器可读输出（结尾一行 JSON）

行为：
- 复用与 GUI 完全相同的内核（providers / agent loop / 工具 / 检查点 / 委派…），只是事件流
  打到终端而非 WebView。默认**自动批准**危险操作（同你本机自己跑命令）；config 的
  `agent.permissions.deny` 规则仍然拦截（不被自动批准绕过）。`--plan` 进只读规划态最稳。
- 默认：助手文本流到 **stdout**，工具活动流到 **stderr**（便于 `>` 只取答案）。
  `--json`：stdout 只输出结尾的一行 JSON（ok/answer/tools/subagents/elapsed/error）。
- 退出码：成功 0 / 出错 1。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _read_prompt(args) -> str:
    parts = [p for p in (args.prompt or []) if p != "-"]
    text = " ".join(parts).strip()
    # 有 "-" 或完全没给位置参数且 stdin 是管道 → 读 stdin
    if "-" in (args.prompt or []) or (not text and not sys.stdin.isatty()):
        piped = sys.stdin.read().strip()
        text = (text + "\n" + piped).strip() if text else piped
    return text


def run(args) -> int:
    # 延迟导入，避免 `--help` 也加载重依赖
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import os
    from agentcore.bridge.api import Api
    from agentcore.config import load_config

    prompt = _read_prompt(args)
    if not prompt:
        print("错误：没有任务内容（用位置参数、--prompt 或管道 stdin 提供）。", file=sys.stderr)
        return 2

    cfg = load_config()
    cfg.agent.workspace = str(Path(args.workspace).expanduser().resolve())
    cfg.agent.shell = "powershell" if os.name == "nt" else "bash"
    cfg.agent.auto_conventions = False   # 无头任务不静默生成 hermes.md（避免意外写文件）
    cfg.agent.screenshot = False         # 无显示器，截屏无意义
    if args.model:
        cfg.active_model = args.model
    if args.max_steps:
        cfg.agent.max_steps = args.max_steps

    json_mode = args.json
    quiet = args.quiet
    chunks: list[str] = []
    tools: list[str] = []
    subs = [0]
    errors: list[str] = []
    usage = {}

    def emit(event, data, cid=None):
        if event == "chunk":
            chunks.append(data)
            if not json_mode:
                sys.stdout.write(data)
                sys.stdout.flush()
            return
        if event == "tool_use":
            tools.append(data["name"])
            if not quiet and not json_mode:
                print(f"  · {data['name']} {str(data.get('input', ''))[:100]}", file=sys.stderr)
        elif event == "subagent_start":
            subs[0] += 1
            if not quiet and not json_mode:
                print(f"  ⮑ 子任务[{data.get('role')}] {str(data.get('task',''))[:70]}", file=sys.stderr)
        elif event in ("error",):
            errors.append(str(data))
            if not json_mode:
                print(f"  ⚠ {data}", file=sys.stderr)
        elif event == "checkpoint_created" and not quiet and not json_mode:
            print(f"  📌 检查点：{data.get('label','')}", file=sys.stderr)
        elif event == "usage":
            usage.update(data)
            if not quiet and not json_mode:
                print(f"  📊 输入 {data.get('input',0)} / 输出 {data.get('output',0)} tokens"
                      f"，{data.get('steps',0)} 步", file=sys.stderr)

    t0 = time.time()
    api = Api(cfg, emit=emit)
    conv = api.active
    if args.plan:
        conv.set_plan_mode(True)
    else:
        conv.gate._allow_all = True       # 无头：自动批准（deny 规则仍拦截）
    try:
        ret = conv.send_message(prompt)
        ok = bool(ret.get("ok")) and not errors
    except Exception as e:  # noqa: BLE001
        ret, ok = {"ok": False, "error": f"{type(e).__name__}: {e}"}, False
        errors.append(ret["error"])
    finally:
        try:
            api.close()
        except Exception:  # noqa: BLE001
            pass

    answer = "".join(chunks).strip()
    if json_mode:
        print(json.dumps({
            "ok": ok, "answer": answer, "tools": tools, "subagents": subs[0],
            "elapsed": round(time.time() - t0, 1), "usage": usage,
            "error": (errors[-1] if errors else ret.get("error", "")),
        }, ensure_ascii=False))
    elif not answer and not ok:
        print(f"\n[失败] {errors[-1] if errors else ret.get('error','未知错误')}", file=sys.stderr)
    else:
        print()  # 收尾换行
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="hermes-cli",
        description="hermes-dev 无头命令行：单任务进出，复用 GUI 同款内核（FR-11.7）。",
    )
    ap.add_argument("prompt", nargs="*", help="任务内容（多个词会拼起来；用 - 或管道从 stdin 读）")
    ap.add_argument("-w", "--workspace", default=".", help="工作区目录（默认当前目录）")
    ap.add_argument("-m", "--model", help="模型档案名（默认用 config 的 active_model）")
    ap.add_argument("--plan", action="store_true", help="只读规划态：只勘察产出方案、不改文件/不执行")
    ap.add_argument("--json", action="store_true", help="机器可读：stdout 只输出结尾一行 JSON")
    ap.add_argument("--quiet", action="store_true", help="不打印工具活动")
    ap.add_argument("--max-steps", type=int, dest="max_steps", help="单轮最多工具步数（覆盖 config）")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
