"""ADR 0019 Architecture Review Mode 引擎 Windows 自测 + 演示。

用法（Windows 项目根目录下）：
    python scripts/diag_design_review.py        （没 python 就用 py）

引擎是**纯逻辑、不连模型、不要 key**：本脚本用一个"剧本化的假 reviewer"完整演一遍评审流程，
让你**亲眼看到**：proposal 抽出的 Decision → 两角色提阻塞/升级 → 四态共识文档 → 开工 gate 从「锁死」到「放开」。
逐项打 [PASS]/[FAIL]，全过退出码 0。真模型当 reviewer 是活体行为（要接线 + Windows GUI），见末尾活体清单。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from agentcore.agent.design_review import (                          # noqa: E402
    ACCEPTED, NEEDUSER, OPEN, Decision, apply_review, can_start_coding,
    count_blocking, gate_status, parse_decisions, render_consensus, run_review,
)

_results = []


def check(name, cond, extra=""):
    _results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra else ""))


def banner(t):
    print("\n" + "=" * 64 + f"\n  {t}\n" + "=" * 64)


# ── 场景：给一个"会话存储"方案做架构评审 ─────────────────────────────────────
PROPOSAL_JSON = '''下面是我的方案（模型本会这样吐 JSON）：
```json
[
  {"id":"store","title":"会话存储引擎","current_choice":"自建 Event Sourcing + Replay",
   "alternatives":[{"choice":"直接 append 日志","tradeoff":"简单但查询弱"}],
   "rationale":"想支持任意时间点回放","status":"Open"},
  {"id":"db","title":"底层数据库","current_choice":"SQLite",
   "alternatives":[{"choice":"DuckDB","tradeoff":"分析快但写入弱"}],"status":"Open"},
  {"id":"index","title":"全文检索","current_choice":"先不做","status":"Open"}
]
```'''


def main():
    banner("1) Proposal → 抽出 Decision 对象（评审单位，不是文档文本）")
    decisions = parse_decisions(PROPOSAL_JSON)
    for d in decisions:
        print(f"  - {d.id}: {d.title} → 当前选择「{d.current_choice}」status={d.status}")
    check("从 proposal 抽出 3 个 Decision", len(decisions) == 3, f"得到 {len(decisions)} 个")
    check("初始全是 Open（未收敛）→ 全部阻塞 gate", count_blocking(decisions) == 3,
          f"未决={count_blocking(decisions)}")

    banner("2) 开工 gate 初始：可数事实锁死（绝不出现百分比）")
    g0 = gate_status(decisions, user_signed=True)
    print(f"  gate: can_start={g0['can_start']}  未决阻塞={g0['blocking_count']}  理由「{g0['reason']}」")
    check("初始 gate 锁死（3 未决）", g0["can_start"] is False and g0["blocking_count"] == 3)
    check("gate 理由里没有任何百分比", "%" not in g0["reason"], "守 ADR 0014 禁 score")

    banner("3) 两角色评审（剧本化假 reviewer，离线、零 key、确定性）")
    # 剧本：Execution 砍掉 Event Sourcing 的复杂度、把"先不做检索"采纳；
    #       Architecture 把"SQLite vs DuckDB"升级为必须用户拍板。第二轮起无新增 → 收敛。
    script = {
        ("execution", 1): '[{"id":"store","status":"Deferred",'
                          '"add_blocking":["Replay 谁用？48h 内做不出可验证切片，先 append 日志"]},'
                          '{"id":"index","status":"Accepted"}]',
        ("architecture", 1): '[{"id":"db","status":"NeedUser",'
                            '"add_blocking":["SQLite vs DuckDB 是方向取舍，需用户拍板"]}]',
    }
    calls = {"execution": 0, "architecture": 0}

    def scripted_review_fn(name, prompt):
        calls[name] += 1
        return script.get((name, calls[name]), "[]")     # 没台词=无意见（推动收敛）

    res = run_review(decisions, scripted_review_fn, max_rounds=4)
    print(f"  评审结束，停止原因：{res['stop_reason']}")
    final = res["decisions"]
    by_id = {d.id: d for d in final}
    check("Execution 把 Event Sourcing 压成 Deferred（拆小先 append）",
          by_id["store"].status == "Deferred", f"store.status={by_id['store'].status}")
    check("Execution 采纳「先不做全文检索」", by_id["index"].status == ACCEPTED)
    check("Architecture 把 DB 选型升级为 NeedUser（要用户拍板）",
          by_id["db"].status == NEEDUSER)
    check("Reviewer 没改动任何 current_choice（结构上禁重写 proposal）",
          all(by_id[d.id].current_choice == d.current_choice for d in decisions),
          "apply_review 碰不到 current_choice")
    check("停止原因是可证伪条件（非共识百分比）",
          res["stop_reason"] in ("no_new_blocking", "wording_only", "max_rounds"))

    banner("4) Consensus 文档（按四态分组 = 一份 ADR）")
    print(res["consensus"])
    check("共识文档含四态标题", all(s in res["consensus"]
          for s in ("Accepted", "Deferred", "Need User Decision")))
    check("共识文档里没有任何百分比", "%" not in res["consensus"])

    banner("5) 开工 gate：仍锁死（2 个未决：db 待拍板 + store 仍挂未澄清问题）")
    g1 = gate_status(final, user_signed=True)
    print(f"  gate: can_start={g1['can_start']}  未决阻塞={g1['blocking_count']}  理由「{g1['reason']}」")
    print("  注：store 虽标 Deferred，但还挂着一条没澄清的 blocking → 引擎仍算它未决，"
          "不因为贴了「Deferred」标签就放行（诚实，不糊弄）。")
    check("评审后 gate 仍锁（db NeedUser + store 带 open blocking = 2 未决）",
          g1["can_start"] is False and g1["blocking_count"] == 2,
          f"未决={g1['blocking_count']}")

    banner("6) 用户拍板 db=SQLite → 未决归零 → 签字 → gate 放开")
    resolved = [Decision(d.id, d.title, "SQLite" if d.id == "db" else d.current_choice,
                         d.alternatives, d.rationale,
                         ACCEPTED if d.id == "db" else d.status, [])
                for d in final]
    check("拍板后未决阻塞 == 0", count_blocking(resolved) == 0,
          f"未决={count_blocking(resolved)}")
    check("未签字仍锁", can_start_coding(resolved, user_signed=False) is False)
    check("零未决 + 用户签字 → 开工解锁", can_start_coding(resolved, user_signed=True) is True)

    # 收尾
    ok = sum(_results)
    total = len(_results)
    print("\n" + "-" * 64)
    print(f"  {ok}/{total} 通过")
    print("-" * 64)
    print("""
活体清单（脚本测不了，需接线 + Windows GUI 真机验，属下一刀）：
  1. 规划模式下，真模型把方案吐成 Decision JSON、被引擎抽取。
  2. 真模型扮 Execution/Architecture，真提出 blocking / 升级 NeedUser（看质量）。
  3. 异构：把 Architecture reviewer 路由到另一模型档（Role.model），观察是否提出
     同模型提不出的反对（错误相关性更低 = 更敢推翻方向）。
  4. 前端：四态共识展示 + 灰/亮「开始编码」按钮 + 逐条拍板 NeedUser。
""")
    sys.exit(0 if ok == total else 1)


if __name__ == "__main__":
    main()
