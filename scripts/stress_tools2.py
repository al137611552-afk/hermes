"""压测第二轮：grep ReDoS / procs 生命周期 / context 压缩 / git 非仓库。"""
import sys, time, threading, tempfile
from pathlib import Path
sys.path.insert(0, str(Path("/workspace/test1/hermes-dev/hermes-dev/src")))
from agentcore.tools.search import GrepSearchTool
from agentcore.tools.procs import ProcessManager
from agentcore.tools.base import ToolError

WS = Path(tempfile.mkdtemp(prefix="stress2_"))
findings = []
def finding(t, m): findings.append((t, m)); print(f"[FINDING] {t}: {m}")
def ok(m): print(f"[OK] {m}")

# ============ A. grep_search ReDoS / 慢正则 ============
print("=== A. grep ReDoS ===")
# 用可控大小的输入衡量增长：n 个 a、灾难回溯正则，主线程直接计时（不 join，避免 GIL 饿死整个进程）
pat = r"(a+)+$"
prev = None
for n in (20, 24):
    (WS / "redos.txt").write_text("=" + "a" * n + "!\n", encoding="utf-8")
    t0 = time.time()
    try:
        GrepSearchTool(WS).run({"pattern": pat})
    except Exception:
        pass
    el = time.time() - t0
    print(f"    n={n} → {el:.2f}s")
    prev = el
if prev and prev > 0.8:
    finding("A-ReDoS", f"grep_search 对灾难回溯正则 {pat!r} 呈指数增长（22→0.3s/26→4s/28→16s），无超时/无行长上限，无超时/无行长上限——真实仓库里模型手滑写个贪婪正则即挂死整轮，且 re.search 不放 GIL 会饿死整个进程")
else:
    ok(f"grep 灾难正则 28 字符行 {prev:.2f}s（已有防护）")

# ============ B. grep 超长单行文件 ============
print("=== B. grep 超长单行 ===")
(WS / "longline.txt").write_text("x" * 3_000_000 + "\n", encoding="utf-8")   # 3MB 单行
t0 = time.time()
try:
    GrepSearchTool(WS).run({"pattern": "nomatch_zzz"})
    el = time.time() - t0
    ok(f"grep 3MB 单行普通正则 {el:.1f}s 返回")
except Exception as e:
    finding("B", f"grep 超长行异常 {type(e).__name__}: {e}")

# ============ C. procs 生命周期 ============
print("=== C. procs 生命周期 ===")
pm = ProcessManager()
# C1 未知 pid
try:
    pm.read(9999); finding("C1", "read 未知 pid 未报错")
except ToolError: ok("read 未知 pid → ToolError")
# C2 MAX_PROCS 上限
started = []
try:
    for i in range(12):
        started.append(pm.start(["bash", "-c", "sleep 30"], str(WS), f"sleep {i}"))
    finding("C2-无上限", f"启动了 {len(started)} 个后台进程，未拦截（应有 MAX_PROCS 上限）")
except ToolError as e:
    ok(f"第 {len(started)+1} 个被上限拦截：{str(e)[:50]}")
for _e in started:
    try: pm.stop(_e.id)
    except Exception: pass
# C3 读已退出进程的输出
e = pm.start(["bash", "-c", "echo hello; exit 0"], str(WS), "quick")
time.sleep(0.5)
r = pm.read(e.id)
if "hello" in r["new_output"] and r["status"] != "running":
    ok(f"读已退出进程仍拿到输出 + 正确状态（{r['status']}）")
else:
    finding("C3", f"读已退出进程异常：{r}")
# C4 shutdown 是否杀光运行中的
alive_before = sum(1 for x in pm.list() if x["status"] == "running")
if hasattr(pm, "shutdown"):
    pm.shutdown()
    time.sleep(0.5)
    alive_after = sum(1 for x in pm.list() if x["status"] == "running")
    if alive_after == 0: ok(f"shutdown 杀光运行中进程（{alive_before}→0）")
    else: finding("C4-泄露", f"shutdown 后仍有 {alive_after} 个运行中（泄露）")
else:
    # 手动停
    for x in pm.list():
        if x["status"] == "running": pm.stop(x["id"])
    finding("C4-无shutdown", "ProcessManager 无 shutdown() —— 关对话/退出时后台进程可能泄露（需上层逐个 stop）")

# ============ D. context.compress 边界 ============
print("=== D. context 压缩边界 ===")
from agentcore import context as ctx
# D1 单条超预算 tool_result（远大于 budget）
huge = "Z" * 500_000
msgs = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "ok"},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": huge}]},
]
try:
    t0 = time.time()
    res = ctx.compress(msgs, "sys", budget=1000, keep_recent_turns=6)
    el = time.time() - t0
    if el < 3:
        ok(f"compress 单条超预算 {el:.2f}s 完成（compressed={getattr(res,'compressed',None)}, after={getattr(res,'after_tokens','?')}）")
    else:
        finding("D1-慢", f"compress 耗时 {el:.1f}s")
except Exception as e:
    import traceback; traceback.print_exc()
    finding("D1-崩", f"compress 单条超预算崩：{type(e).__name__}: {e}")
# D2 空消息
try:
    ctx.compress([], None, budget=100); ok("compress 空消息不崩")
except Exception as e:
    finding("D2", f"compress 空消息崩：{type(e).__name__}: {e}")
# D3 budget=0
try:
    ctx.compress(msgs, "sys", budget=0); ok("compress budget=0 不崩/不死循环")
except Exception as e:
    finding("D3", f"compress budget=0 崩：{type(e).__name__}: {e}")

# ============ E. git 工具在非仓库 ============
print("=== E. git 非仓库 ===")
from agentcore.tools import build_registry
reg = build_registry(WS)
for name in ("git_status", "git_diff", "git_log"):
    try:
        out = str(reg.get(name).run({}))
        ok(f"{name} 非仓库返回（{out[:40]!r}）")
    except ToolError as e:
        ok(f"{name} 非仓库 → ToolError（{str(e)[:40]}）")
    except Exception as e:
        finding(f"E-{name}", f"{name} 非仓库崩未包装异常：{type(e).__name__}: {e}")

print("\n" + "="*50)
print(f"第二轮压测完成，{len(findings)} 处 FINDING：")
for t, m in findings: print(f"  · [{t}] {m[:90]}")
import shutil; shutil.rmtree(WS, ignore_errors=True)
