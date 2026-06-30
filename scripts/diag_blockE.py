"""块E Windows 自测：World State + Failure Memory（死路记忆）。

用法（Windows 项目根目录下）：
    python scripts/diag_blockE.py

逐项打 [PASS]/[FAIL]，全过退出码 0，任一失败退出码 1（末行 RESULT 一目了然）。
跑真实代码路径（含 loop.py 的 detect_repeated_failure），用临时目录建库、不污染 data/。
重点验 Windows 专属风险：① SQLite 文件落盘 + **跨会话重开仍记得死路**；② 中文报错文案的分类；
③ 瞬时 IO 不被误判成死路。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# —— 健壮地把 src/ 进 sys.path（按脚本位置，不依赖 cwd）——
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from agentcore.agent.world_state import WorldState, FailureMemory, fingerprint  # noqa: E402
from agentcore.agent.loop import detect_repeated_failure                        # noqa: E402

_results = []


def check(name, cond, extra=""):
    _results.append(bool(cond))
    tag = "PASS" if cond else "FAIL"
    line = f"[{tag}] {name}"
    if extra:
        line += f"  ({extra})"
    print(line)


class _Call:
    """仿 loop 里的工具调用对象（detect_repeated_failure 只用 .id/.name/.input）。"""
    def __init__(self, i, tool, params):
        self.id, self.name, self.input = str(i), tool, params


def main():
    print("===== 块E 自测：World State + Failure Memory =====")
    print(f"src = {_ROOT / 'src'}\n")

    # 1) 指纹：同工具同关键入参 → 同指纹；改入参 → 不同指纹
    fp1 = fingerprint("run_powershell", {"command": "pytest broken"})
    fp2 = fingerprint("run_powershell", {"command": "PYTEST   BROKEN"})   # 归一化后应同
    fp3 = fingerprint("run_powershell", {"command": "pytest other"})
    check("指纹稳定（空白/大小写归一化后同一条路同指纹）", fp1 == fp2,
          f"{fp1[:8]}=={fp2[:8]}")
    check("指纹区分（不同命令不同指纹）", fp1 != fp3, f"{fp1[:8]}!={fp3[:8]}")

    # 2) WorldState 单会话累计
    ws = WorldState()
    n1 = ws.record_failure(fp1, detail="boom")
    n2 = ws.record_failure(fp1, detail="boom again")
    check("WorldState 同指纹累计计数", n1 == 1 and n2 == 2, f"n1={n1} n2={n2}")

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "data" / "failures.db"      # 故意带子目录，验自动建目录

        # 3) FailureMemory 落盘 + 阈值判死路
        fm = FailureMemory(db)
        check("FailureMemory 自动建 data/ 目录并打开库", db.exists(), str(db))
        fm.record(fp1, ["logic"], detail="AssertionError")
        de1 = fm.known_deadend(fp1, threshold=2)
        fm.record(fp1, ["logic"], detail="AssertionError")
        de2 = fm.known_deadend(fp1, threshold=2)
        check("第一次失败不算死路（阈值2）", de1 is None, f"de1={de1}")
        check("第二次失败 → 已知死路", de2 is not None and de2[0] == 2, f"de2={de2}")
        fm.close()

        # 4) ★Windows 重点：跨会话重开同一 db 文件，死路记忆仍在（证明 SQLite 文件落盘 OK）
        fm2 = FailureMemory(db)
        de_reopen = fm2.known_deadend(fp1, threshold=2)
        check("★跨会话重开 db → 仍记得死路（SQLite 文件在 Windows 正确落盘/读回）",
              de_reopen is not None and de_reopen[0] == 2, f"reopen={de_reopen}")
        fm2.close()

        # 5) 真实 detect_repeated_failure（loop.py）：反复 pytest 失败 → 第2次起提示换思路
        fm3 = FailureMemory(Path(d) / "fm3.db")
        world, nudged = WorldState(), set()
        out = "==== 1 failed, 2 passed ====\nAssertionError: 期望 200 实际 500"  # 含中文
        msgs = []
        for i in range(1, 4):
            m = detect_repeated_failure(
                [_Call(i, "run_powershell", {"command": "pytest app"})],
                {str(i): out}, world, fm3, nudged, threshold=2)
            msgs.append(m)
        check("反复同种失败：第1次不提示", msgs[0] is None)
        check("反复同种失败：第2次提示换思路", bool(msgs[1]), (msgs[1] or "")[:40])
        check("每指纹每轮只提示一次：第3次不再重复", msgs[2] is None)

        # 6) 瞬时 IO 不算死路（归块D 重试，不该进死路记忆）
        fm4 = FailureMemory(Path(d) / "fm4.db")
        w2, n2set = WorldState(), set()
        trans = "[exit code] 1\n[stderr]\ncurl: (7) Connection refused"
        tmsgs = [detect_repeated_failure(
            [_Call(i, "run_powershell", {"command": "curl http://x"})],
            {str(i): trans}, w2, fm4, n2set, threshold=2) for i in range(1, 4)]
        check("瞬时 IO（connection refused）反复也不判死路", all(m is None for m in tmsgs))

    ok = all(_results)
    print()
    if ok:
        print(f"===== RESULT: ALL PASS ({len(_results)}/{len(_results)}) =====")
        return 0
    failed = len(_results) - sum(_results)
    print(f"===== RESULT: {failed} FAILED （共 {len(_results)} 项）=====")
    sys.stderr.write(f"块E 自测有 {failed} 项失败\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
