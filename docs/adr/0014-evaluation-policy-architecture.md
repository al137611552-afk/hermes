# ADR 0014 — 评估/策略分层架构（Evaluation–Policy Architecture）

- 状态：Accepted（架构契约收口，分块实现中）
- 日期：2026-06-30
- 适用范围：**整个 Hermes 执行内核**，不限于 crazy 模式。Coding / Search / Vision / Research 等所有 Skill 共享同一条数据流。crazy 块2/3/4 是它的第一个落地点。

## 背景

crazy 的 4 块（阶段规划 / 验收门 / 自适应门控 / 阶段后重规划）各自能跑，但彼此的"判断"逻辑是临时拼起来的：验收结果、是否问用户、是否重规划，散落在 `conversation.py` 的字符串 verdict 和 `loop.py` 的 nudge 里。要继续加 Auto-Retry、Failure-Memory、Learning，必须先把**"判断"这件事本身**抽成稳定契约，否则每个新能力都要重写一遍判断逻辑。

讨论中确立的核心原则：**把"事实"、"差距"、"做法"三件事彻底切开**，让每一层只回答一个问题、且越往上越稳定。

## 决策

### 数据流契约

```text
Tool ──► Evaluation ──► Policy ──► Need ──► Planner ──► [Decision + 工具调用] ──► Tool
         （事实）        （判定）   （差距）            （做法，Learning 在此优化）
```

逐层职责：

| 层 | 回答 | 稳定性 | 谁产出 |
|---|---|---|---|
| **Evaluation** | 发生了什么（事实） | 随工具变 | Evaluator（每个 Skill 一个） |
| **Policy** | 这算好还是坏 | 慢变 | 薄规则 或 模型 |
| **Need** | 世界现在缺什么（差距） | 极稳，~8 个枚举 | Policy |
| **Decision** | 用什么补这个差距 | 不断进化 | Planner（即模型本身） |

### 1. Evaluation 只出事实，不出分数、不出意图

Evaluator 的输出是结构化事实，**不是 Score、不是 Intent**：

```jsonc
{
  "metrics":  { "...": "纯事实，可度量（耗时、命中数、测试通过数/总数、退出码）" },
  "signals":  [ "纯事实，离散观察（'端口被占用'、'返回 0 条'、'编译报错 E0432'）" ],
  "issues":   [ "默认策略层：把事实按阈值/严重度归类（'测试未全过=blocker'）" ],
  "confidence": 0.0
}
```

- `metrics` + `signals` 是**事实核**——同样的世界状态，永远是同一份。
- `issues` 已经是**默认策略**——"几个算少"、"哪种算 blocker" 是判断，不是事实。所以 `issues` 可被上层 Policy 覆盖；Evaluator 给的只是合理默认。
- **Score 只是事实的一个投影（用于展示/排序），绝不回喂进决策。** 决策读事实，不读分数。

### 2. Policy 薄，产出 Need

Policy 把 Evaluation 映射成一个 **Need**。Policy 可以是几条确定性规则，也可以是模型。**早期不要建大型规则 DSL**——先用模型 + 最便宜的几条硬规则（transient→retry、撞上限→escalate）。

### 3. Need 是"世界缺什么"，工具无关，全是差距

Need 枚举小、稳、**全部是世界状态的差距，不含任何动作**。草案：

```text
CONTINUE            正常推进，无缺口
NEED_INFORMATION    缺信息/上下文（要去查、去读）
NEED_EXECUTION      缺一次执行（要去跑、去改、去调）
NEED_VALIDATION     缺验证（结果未被确认）
PROGRESS_STALLED    路径在原地打转（事实：N 次无进展）
APPROACH_INVALIDATED 当前路径被证伪（事实：此路不通）
NEED_USER_INPUT     缺人类输入/授权
GOAL_BLOCKED        外部硬阻塞，自身无法推进
GOAL_SATISFIED      目标（或子目标）已达成
```

**关键纪律——Need 里不许出现动作：**
- ❌ `NEED_REPLANNING` / `RETRY_SAME` / `SWITCH_TOOL` 都是**做法（Decision）**，不是差距。
  - "路径在失败" 是事实 → `PROGRESS_STALLED` / `APPROACH_INVALIDATED`（Need）。
  - "所以重规划 / 换工具 / 重试" 是 Planner 的响应（Decision）。
- 命名备注：文献里 "Intention" 常指**已承诺的计划**（恰是 How），易混淆。本架构这一层叫 **Need / Gap**，不叫 Intent。

### 4. Decision 是做法——多数时候让模型直接出，只记标签

Planner（即模型）读 `Evaluation + Need`，选工具。**不为 Decision 单建一套引擎**——"Need→选哪个工具"很多时候就是模型在 reasoning。

- Decision 通常**只作为一个标签被记录**（`USE_BROWSER`、`RETRY_WITH_BACKOFF`），供 Learning / debug。
- 只给**最便宜的确定性分支**配硬规则。

**原则：物化你要从中学习的，别建你不需要的引擎。**
- Need **必须物化**——小、稳、可枚举，是 Learning 聚合的 key（与 Error Taxonomy 同级地位）。
- Decision **多数只记账**——让模型出工具调用，记一个标签即可。

### 5. Learning 优化的精确对象 = `Need → Decision` 映射

Need 钉死成小集合后，Learning Engine 的优化对象被精确锁定：**就是 `Need → Decision` 那张映射表。**

- Need 多年不变 → 上层契约稳定。
- Planner 的 `Need→How` 策略不断进化，且**每条改动都能在回归语料（Golden Dataset）上验证**。
- "Search 失败两次转 Browser"、"小红书用 `site:` 更准" 这类经验，全部活在这张**可学习、可度量、可退役**的映射里，不再污染上层契约。

> 一句话：**学习 = 在稳定的 Need 之上，优化被语料验证过的 `Need→How` 映射。**

## 与现有 crazy 块的映射（落地锚点）

| 现状 | Evaluation 出 | Policy 判 Need | Planner Decision |
|---|---|---|---|
| 块2 验收门红 | 测试通过数/总数、报错码 | `NEED_VALIDATION`/`NEED_EXECUTION` | 修哪、重跑 |
| 块3 NEED_USER / 验收卡死 | 重复无进展计数 | `NEED_USER_INPUT`/`GOAL_BLOCKED` | 措辞、问什么 |
| 块4 阶段过 | 子目标达成事实 | `GOAL_SATISFIED`(子目标) | **重规划=Decision** |
| `loop.py` 现有 nudge | 触发条件 | 对应 Need | 注入哪段 directive |

注意块4 的"重规划"**本来就在 Planner 侧**，天然是 Decision——这正印证了"Need 里不该有 `NEED_REPLANNING`"。

## 后果

- 加新 Skill = 写一个 Evaluator（出 metrics/signals）+ 复用同一套 Need/Policy/Learning，不再各写一套判断。
- 调试可观测：任一步都能看到 `Evaluation→Need→Decision` 三段。
- 风险：过度分层。对策——见决策第 4 条，先物化 Need，Decision 记标签，不强建三引擎。

## 第一步（最小落地，验证契约不重写世界）

**重构 `loop.py` 现有 nudge，让它走 `Evaluation→Need→Decision` 这条路，但不新增能力。**

1. 定义 `Need` 枚举（上面 9 个）+ 一个 `Evaluation` dataclass（metrics/signals/issues/confidence）。
2. 把 crazy 现有的 verdict 字符串（`[[DONE]]/[[CONTINUE]]/[[NEED_USER]]/[[PHASE_DONE]]`）**映射**到 Need，不改行为。
3. `loop.py` 的每个 nudge 触发点，改成"读 Need → 选注入"，保持现有注入内容不变。
4. 全量回归测试必须绿（行为等价重构）。

通过即证明契约可承载现状，再在其上叠 Auto-Retry / Failure-Memory / Learning（见 ROADMAP 块 B 起）。

详见 `docs/ROADMAP.md`。
