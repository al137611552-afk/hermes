"""对抗性压测 hermes 工具层：像 RL 训练环境那样丢工程场景，找短板。
只在临时工作区跑，不碰真仓库。每条打印 [OK]/[FINDING]/[INFO]。
"""
import os, sys, tempfile, shutil, time, traceback
from pathlib import Path
sys.path.insert(0, str(Path("/workspace/test1/hermes-dev/hermes-dev/src")))

from agentcore.tools.shell import RunShellTool, hardened_env, _looks_long_running
from agentcore.tools.fs import ReadFileTool, WriteFileTool, EditFileTool, MultiEditTool, ListDirTool
from agentcore.tools.base import ToolError

WS = Path(tempfile.mkdtemp(prefix="hermes_stress_"))
findings = []
def finding(tag, msg): findings.append((tag, msg)); print(f"[FINDING] {tag}: {msg}")
def ok(msg): print(f"[OK] {msg}")
def info(msg): print(f"[INFO] {msg}")

def sh(cmd, timeout=8, background=False):
    return RunShellTool(WS, shell="bash", timeout=timeout).run({"command": cmd, "background": background})

# ============ A. 密钥泄露：shell 是否把 .env 里的 key 透传给任意命令 ============
print("\n=== A. 环境变量/密钥泄露面 ===")
os.environ["ARK_API_KEY"] = "sk-SECRET-shouldnotleak-123"  # 模拟 .env 加载后的态
env = hardened_env()
if env.get("ARK_API_KEY") == "sk-SECRET-shouldnotleak-123":
    finding("A1-密钥透传", "hardened_env() 把 ARK_API_KEY 原样传给子 shell；模型跑 `env`/`printenv`/`echo $ARK_API_KEY` 即可把用户 API key 打进上下文（→ 可能被日志/模型端记录）。Claude Code 默认也传环境，但 hermes 的 key 是 provider 计费密钥，泄露=盗刷。")
out = str(sh("echo key=$ARK_API_KEY"))
if "sk-SECRET" in out:
    finding("A2-实测泄露", f"实跑 `echo $ARK_API_KEY` 输出含明文密钥：{out.strip()[:60]}...")

# ============ B. edit/write 遇非 UTF-8/二进制文件 ============
print("\n=== B. 二进制/非 UTF-8 文件的读写编辑 ===")
binf = WS / "data.bin"
binf.write_bytes(b"\xff\xfe\x00\x01hello\x80world\xff")
# B1: write_file 覆盖二进制文件（会先 read_text 存 before）
try:
    WriteFileTool(WS).run({"path": "data.bin", "content": "now text"})
    ok("write_file 覆盖二进制文件成功")
except ToolError as e:
    finding("B1-write崩", f"write_file 覆盖二进制文件抛 ToolError（可回灌）：{e}")
except Exception as e:
    finding("B1-write崩", f"write_file 覆盖二进制文件抛未包装异常 {type(e).__name__}: {e} —— 非 ToolError，可能中断工具循环")
# B2: edit_file 编辑二进制文件
binf.write_bytes(b"\xff\xfe\x00\x01hello\x80world\xff")
try:
    EditFileTool(WS).run({"path": "data.bin", "old_string": "hello", "new_string": "HI"})
    ok("edit_file 编辑含非UTF8字节的文件成功")
except ToolError as e:
    info(f"edit_file 二进制抛 ToolError（可接受）：{e}")
except Exception as e:
    finding("B2-edit崩", f"edit_file 编辑二进制文件抛未包装异常 {type(e).__name__}: {e} —— 非 ToolError")
# B3: multi_edit 同理
binf.write_bytes(b"\xff\xfehello\x80")
try:
    MultiEditTool(WS).run({"path": "data.bin", "edits": [{"old_string": "hello", "new_string": "HI"}]})
    ok("multi_edit 二进制成功")
except ToolError as e:
    info(f"multi_edit 二进制抛 ToolError：{e}")
except Exception as e:
    finding("B3-multiedit崩", f"multi_edit 抛未包装异常 {type(e).__name__}: {e}")

# ============ C. 路径约束：越界 / 符号链接逃逸 ============
print("\n=== C. 路径约束 ===")
for bad in ["../../../etc/passwd", "/etc/passwd", "..%2f..%2fetc", "\0etc"]:
    try:
        ReadFileTool(WS).run({"path": bad})
        finding("C1-越界", f"read_file 允许访问越界路径：{bad!r}")
    except ToolError:
        pass
    except Exception as e:
        info(f"read_file({bad!r}) 抛 {type(e).__name__}: {e}")
ok("read_file 相对/绝对越界路径均被拒（符合预期）")
# 符号链接逃逸
try:
    (WS / "escape").symlink_to("/etc")
    ReadFileTool(WS).run({"path": "escape/passwd"})
    finding("C2-软链逃逸", "符号链接指向 /etc 后可读 escape/passwd —— 路径约束被软链绕过")
except ToolError:
    ok("符号链接逃逸被拒（resolve() 跟随软链后判越界）")
except Exception as e:
    info(f"软链测试异常 {type(e).__name__}: {e}")

# ============ D. 探针误判：正常一次性命令别被当服务杀 ============
print("\n=== D. 探针假阳性 ===")
false_pos = ["npm run build", "npm install", "npm test", "yarn build", "pip install foo",
             "git serve-doc", "myserver --help", "echo starting server", "cat server.py",
             "python manage.py migrate", "docker build .", "make serve-docs-once"]
for c in false_pos:
    if _looks_long_running(c):
        finding("D-假阳性", f"一次性命令被误判为常驻服务（会被 12s 探针窗口误杀/误导）：{c!r}")
misses = [c for c in ["streamlit run x", "uvicorn app:x", "npm run dev", "vite", "next dev",
                       "python3 -m http.server", "flask run", "nodemon x", "tsc --watch"] if not _looks_long_running(c)]
if misses: finding("D-漏判", f"这些常驻服务没被识别：{misses}")
else: ok("常见常驻服务全部识别，且抽样一次性命令无假阳性")

# ============ E. ListDir 大目录无上限 ============
print("\n=== E. list_dir 大目录 ===")
big = WS / "bigdir"; big.mkdir()
for i in range(5000): (big / f"f{i}.txt").touch()
out = str(ListDirTool(WS).run({"path": "bigdir"}))
if out.count("\n") >= 4999:
    finding("E-无上限", f"list_dir 一次性吐出 {out.count(chr(10))+1} 个条目、无截断/分页 —— 大目录会灌爆上下文（read_file 有上限、它没有）")

# ============ F. write_file 原子性 ============
print("\n=== F. write_file 原子性 ===")
import inspect
src = inspect.getsource(WriteFileTool.run)
if "write_text" in src and "tmp" not in src.lower() and "replace(" not in src:
    finding("F-非原子", "write_file 直接 p.write_text 覆盖，非「临时文件+rename」原子写；写大文件中途崩溃/断电会留半截损坏文件（Claude Code/多数编辑器用原子写）")

# ============ G. 超长单行输出（无换行）会不会撑爆 drain 读线程 ============
print("\n=== G. 超长无换行输出 ===")
t0 = time.time()
try:
    out = str(sh("head -c 5000000 /dev/zero | tr '\\0' 'a'", timeout=8))  # 5MB 单行无换行
    el = time.time() - t0
    if "截断" in out or "truncated" in out.lower() or len(out) < 1_000_000:
        ok(f"5MB 单行输出被上限截断（{el:.1f}s，返回 {len(out)} 字符）")
    else:
        finding("G-无界", f"5MB 单行输出未截断，返回 {len(out)} 字符")
except Exception as e:
    finding("G-崩", f"超长单行输出异常 {type(e).__name__}: {e}")

# ============ H. 探针兜底真实计时（疑似服务前台跑）============
print("\n=== H. 探针兜底端到端计时 ===")
import agentcore.tools.shell as shmod
orig = shmod._PROBE_SECONDS; shmod._PROBE_SECONDS = 2
t0 = time.time()
try:
    sh("python3 -m http.server 0", timeout=30)
    finding("H", "疑似服务前台跑竟正常返回（未被探针拦）")
except ToolError as e:
    el = time.time() - t0
    if el < 6 and "background:true" in str(e):
        ok(f"探针在 {el:.1f}s 内兜底并指向 background:true（未等满 30s）")
    else:
        finding("H-探针", f"探针未按预期：{el:.1f}s / msg={str(e)[:80]}")
finally:
    shmod._PROBE_SECONDS = orig

# ============ I. read_file 对超大文件是否 OK（内存/分段）============
print("\n=== I. read_file 超大文件 ===")
huge = WS / "huge.txt"
with huge.open("w") as f:
    for i in range(500000): f.write(f"line {i} " + "x"*50 + "\n")   # ~30MB
t0 = time.time()
out = str(ReadFileTool(WS).run({"path": "huge.txt"}))
el = time.time() - t0
if "offset=" in out and el < 5:
    ok(f"read_file 大文件按行分段读、{el:.1f}s 返回并提示续读 offset")
else:
    finding("I", f"read_file 大文件耗时 {el:.1f}s / 无续读提示")

print("\n" + "="*60)
print(f"压测完成，共 {len(findings)} 处 FINDING：")
for tag, msg in findings:
    print(f"  · [{tag}] {msg[:100]}")
shutil.rmtree(WS, ignore_errors=True)
