# ADR 0027 — 评测语料分层与录制回放（喂饱评估内核）

- **状态**：提议（2026-08-19），待实现。分期见文末「交付分期」。
- **相关**：ADR 0014（评估/策略分层架构）、ADR 0015（Error Taxonomy）、ADR 0016（World State /
  Failure Memory）、ADR 0017（Learning Engine）、ADR 0019（评审 gate 禁模糊分——本文沿用同一条纪律）；
  代码锚点 `scripts/eval/`、`tests/golden/`、`src/agentcore/agent/world_state.py`、`src/agentcore/providers/`
- **来源**：与 deepseek-harness 横向对比后的结论——**hermes 的评估/策略内核（块 A–H）建得比对方完整，
  但它在空转**。进一步读码发现问题不是"没有语料"，而是**语料在产生、却落不了盘，且指纹是脏的**。

## 背景

块 A–H 已经交付了一整套评估内核：`contract.py`（Need 9 枚举）、`evaluators/`（Coding/Search/Shell）、
`taxonomy.py`（ErrorClass 9 类）、`world_state.py`（WorldState + FailureMemory）、
`learning/engine.py`（aggregate → propose → StrategyStore，approve 强制 `golden_passed=True`）。

设计是对的。问题在**输入端**。2026-08-19 读码查证到的硬事实：

| 事实 | 后果 |
|---|---|
| `loop.py` 已 emit 八种 nudge 事件（`login_hint` / `stuck_hint` / `search_hint` / `deadend_hint` / `research_hint` / `truncation_hint` / `learning_shadow` / `learning_advice`），`_emit_result` 还附 `eval`（含 `error_classes`） | **遥测已经齐了**，每一跑都在流过，只是没人接 |
| `harness.py:EvalResult` 只活在内存里，`run_eval.py` 只打印 + 退出码 | 跑完即失，**两次跑无法对比** |
| `world_state.py:fingerprint()` 的 `_KEY_PARAMS` 含 `path` / `file_path` / `command`，而评测在 `tempfile` 建工作区 | **同一个失败，两次跑得到两个不同指纹** |
| `conversation.py:2279` 把 FailureMemory 硬编码到 `ROOT/data/failures.db`；`harness.py` 没关 `agent.failure_memory`（`config.py:130` 默认 `True`） | 评测语料与真实使用语料**混在一个库**，无法分开分析、无法重置重跑 |
| `scripts/eval/tasks.py` 只有 6 个任务；`tests/golden/cases.py` 有 51 条 / 13 类 | Golden 单元层健康，**端到端层量级不足**，中间缺一层 |

指纹污染这条最隐蔽，值得展开：`aggregate()` 按 `error_class` 归并、`paths` 记 distinct 指纹数，
而 `propose(min_count=3, min_paths=2)` 是**双门**。临时路径进指纹之后，同一个失败在 N 次跑里变成
N 条不同的"路"——`paths` **虚高**（假信号越门）、每指纹 `total` **恒 1**（真信号被拆碎、越不过 `min_count`）。
**两个方向同时错**。这不修，攒得再多也是脏语料。

> 待办第二档里已记过半条：「失败语料无来源标记：评测/测试失败混进 FailureMemory → 要改 SQLite schema」。
> 本 ADR 把它与指纹归一合并处理——**来源标记解决"混"，指纹归一解决"碎"，缺一不可**。

## 决策 1 — 语料分三层，各司其职，不互相冒充

| 层 | 是什么 | 成本 | 进 CI | 守什么 |
|---|---|---|---|---|
| **L-Golden**（V0 前 51 条 / 13 类） | 决策函数的 `输入 → 期望输出`，离线确定性、不调模型 | 毫秒 | ✅ 每次 | 决策内核不回归 |
| **L-Cassette**（新） | 录好的模型输出重放，跑完整 loop | 秒级、离线 | ✅ 每次 | **端到端行为**不回归 |
| **L-Live**（扩量） | 真模型真网络跑任务集 | 分钟级、烧 key | ❌ 按需 | **解题率**的绝对水平与 A/B |

三层**不可互相替代**，写清楚免得将来偷懒合并：

- Golden 测不到 loop 的组合行为（两个 detector 同轮都触发会怎样，只有跑完整 loop 才知道）；
- Cassette 测不到"换个模型/改了提示词之后还行不行"（模型输出是录死的）；
- Live 因为有模型随机性，**单跑一次的 pass/fail 是伯努利采样**，不能当回归门。

## 决策 2 — 指纹按工作区相对路径归一；语料按来源分库

**指纹归一**：`fingerprint()` 增加 `workspace` 参数，路径类关键入参（`path` / `file_path`）先转
工作区相对路径，`command` 内嵌的绝对路径同样归一，再取 sha1。纯逻辑不变、无 IO。

**分库**：`FailureMemory` 路径改为可注入。真实使用 `data/failures.db`，评测 `data/failures.eval.db`。

**为什么分库而不是只加一列 `source`**：加列能区分，但**不能重置**。评测要能"清空重跑一批"，
而真实使用的死路记忆是跨会话资产、绝不能被评测流程误删。两个生命周期不同的东西不该共用一个文件——
与 ADR 0025 决策 7（`usage.db` 独立、不进 `hermes.db`）同一条理由。`source` 列仍然加，用于库内细分
（评测 / 自检 / 真实），但**隔离靠分库**。

## 决策 3 — 每次评测跑必须落 Run Record，且自带可比性三件套

`EvalResult` 落 `data/eval_runs/{run_id}/{task}.json`。除已有字段外**必须**记：

1. **git sha**（被测代码是哪一版）
2. **模型档案 + 真实 model id**（`active_model` 是别名，会漂）
3. **配置快照**（影响行为的 `agent.*` 全量：`max_steps` / 各 detector 阈值 / `crazy_*` / 预算上限）

**没有这三样，两份记录不可比**——这是本项目已经踩过的坑的同款：ADR 0025 决策 3 立过
「一个自信的错数比缺失更危险」，两份不可比的记录放一起做对比，产出的就是自信的错数。

`run_eval.py` 增 `--out` / `--repeat N` / `--tag`；新增 `scripts/eval/report.py` 吃两个 run_id 出对比表。
对比表的列**全部从现有事件流直接算**，不改 `loop.py`：

pass@1 / pass@N ｜ 步数 ｜ 工具调用数 ｜ 子任务数 ｜ **八类 nudge 各自触发次数** ｜ 错误分类分布 ｜ 耗时 ｜ token

倒数第四列是 V5 的唯一输入。

## 决策 4 — 录制/回放挂在 provider 层；miss 必须报错，**绝不静默回落真跑**

`providers/base.py:BaseProvider.stream_chat` 已经是统一接口，在 `build_provider()` 外包一层即可，
**不动任何 provider 实现**：

```
RecordingProvider(inner)   真跑 + 落 (请求指纹 → StreamEvent 序列)
ReplayProvider(cassettes)  离线按指纹取回放
```

- **cassette key** = `sha1(model_id + system + messages_json + tools_json)`；messages 里的图片用
  blob hash 代替内容（否则同一张图不同编码会分裂成两个 key）。
- **存** `data/cassettes/{task}/{key}.jsonl`，一行一个 `StreamEvent`。
- **miss 必须硬报错并指出第几步 miss**。静默回落真跑会同时犯两个错：偷偷烧 key，以及把
  "我的改动让轨迹发散了"这个**最有价值的信号**当噪声吞掉。同 ADR 0025 决策 3 的立场——宁缺勿假。

**为什么不做更聪明的模糊匹配**：cassette 的价值全部来自"模型输出逐字固定"这一条。一旦允许近似匹配，
回放就不再是回放，A/B 的对照组也就不成立了。

## 决策 5 — 判分优先程序化；不得已用模型判分**必须多数投票**

沿用现有 `check(ws, result) -> (bool, str)` 签名。优先级：跑测试 > 查文件/git 状态 > 查事件流 > 模型判分。

模型判分**必须 3 次取 2**。理由与 ADR 0019 禁用"共识度 80%"是同一条：**判分器自己的方差会淹没
被测对象的差异**。一个方差比信号大的判分器，比没有判分器更糟——它会让你相信噪声。

## 决策 6 — L2 任务必须**正反成对**，反例优先于正例

每个 detector 至少两个任务：一个**该触发**、一个**不该触发**。

**反例比正例更重要**，因为代价不对称：漏报只是少一次帮助（模型多半还能自己走出来），
**误报是浪费一整轮 + 用一段权威口吻的注入把模型从正确的路上推开**。而现在这些阈值
（`detect_repeated_failure(threshold=2)`、`detect_low_quality_research(max_nudges=1)`、
研究催重搜的全局预算）**全是拍脑袋定的，误报率是零测量**。

同一条延伸到 V5 的验收判据：**触发得准 ≠ 有用**。第三个指标是「触发后那一轮 `Evaluation.issues`
是否减少 / Need 是否前进」——判据现成，`eval` 事件里就有。

### 实现 V2 时对本条的两处修订（2026-08-19）

**① 正反例的判据必须不对称。** 原文写"每个 detector 一对"，隐含两边同权重，实现时确认那是错的：

| | 判据 | 为什么 |
|---|---|---|
| 反例（不该触发） | **硬断言，误报即 FAIL** | 条件不成立时 detector 永远不该响，这是**确定性的**，与模型走哪条路无关 |
| 正例（该触发） | **软观测，只记触发率、不判 FAIL** | 漏报取决于模型愿不愿意走那条坏路，**逼不出来**；硬判会把"模型这次表现好"误记成"detector 坏了" |

正例的真实价值在**触发率**（`report.py` 的 `nudge.*` 列），不在单次通过。
**反例是门、正例是仪表。**

**② 反例不必"一个 detector 一个任务"。** 一个正常任务里**所有** nudge 都不该响，
用 `{"*": False}` 一行表达比拆成八个任务更强（覆盖面更全）也更省（少跑七次模型）。
正例任务同样带 `"*": False`——否则测不出"观测目标响了、但旁边那些也乱响"。

**③ 夹具必须先过启用门。** detector 多数有前置开关（`search_nudge_files` 默认 40 个代码文件、
`stuck_edit_threshold`、`trace_run` 是否可用）。夹具没越过门槛，那个正例就是**哑弹**——
看着有覆盖，其实 detector 压根没被启用。V2 的任务自检里为此专门钉了两条
（正例必须越门、反例必须低于门）。

## 决策 7 — Learning 候选仍须人审 + Golden 门（重申，不改）

语料喂饱之后会有人想加自动采纳。**不做**。理由 ADR 0014/0017 已立，`trajectory.py` 里也论证过同一件事
（"判据不可靠：保守则永不触发，激进则批量生成垃圾"）。`StrategyStore.approve` 强制
`golden_passed=True` 这道硬门保持不变。

**喂饱的定义是"让 `propose()` 产出有证据的候选"，不是"让候选自动上线"。**

## 已知限制

1. **cassette 会因提示词改动全 miss。** 改 system prompt、改任何 nudge 注入文案 → 轨迹发散 → 必须重录。
   所以 **replay 是回归门，不是 A/B 工具**；提示词类改动的效果验证只能靠 L-Live × N 次重复。
   这不是缺陷，是这个方法的定义域，实现时别试图绕。
2. **Live 层仍需 Windows 真机 + key。** 开发机 2 核 4G 跑不动批量真跑（见既定分工）。V3 之后
   V4/V5 可全程在开发机用回放做，只有录 cassette 和 A/B 要搬 Windows。
3. **L2 反例任务本身可能写错。** 换手真跑那次的教训适用：**没跑过的任务，其 fixture 本身也是未验代码**；
   任务挂了要先分清是被测对象错了还是任务设定错了，但也别改到通过为止。
4. **pass@N 需要 N≥3 才有意义**，成本随之线性上升。L3 复合任务单跑就是分钟级，全量 `--repeat 3`
   要按小时算——所以 L1/L2/L3 要能分别跑，别只给一个"全跑"入口。

## 交付分期

| 块 | 目标 | 依赖 | 粗估 | 开发机可做 |
|---|---|---|---|---|
| **V0** | 指纹归一 + 语料分库 + `source` 列 | — | ~1 天 | ✅ |
| **V1** | Run Record 落盘 + `report.py` 对比 | V0 | 1~2 天 | ✅ |
| **V2** | 任务集 6 → ~40（L1 冒烟 / L2 正反成对 / L3 复合） | V1 | 3~5 天，可分批 | 写 ✅ / 验 ❌ |
| **V3** | 录制/回放 + 进 CI | V1 | 2~3 天 | 录 ❌ / 放 ✅ |
| **V4** | `harvest.py`：批跑 → aggregate → propose → 候选报告 | V0,V2,V3 | ~2 天 | ✅ |
| **V5** | detector 阈值调优（触发率/误报率/是否改善） | V4 | 持续 | ✅ |

**V0 必须最先**：它决定后面攒的语料是否可用，是唯一一块"不做就全白做"的。

## 验收判据（可数、可证伪）

- **V0**：两次不同 tempdir 跑同一失败任务 → `failure_memory.rows()` 是**一行 count=2**，不是两行 count=1。
- **V1**：同一 commit 连跑两次 `--repeat 3`，`report.py` 能出差异表；**故意改坏一个 detector 阈值，
  对应那一列必须明显变化**（反向验证报表有效）。
- **V2**：每个 detector 的正反例都存在；L2 反例的**误触发数为 0** 是目标，非 0 即是 V5 的输入。
- **V3**：replay 模式下全量 L1+L2 离线跑通、进 CI；**miss 时报错并指出步号**。
- **V4**：`propose()` 产出 **≥2 条有真实证据**的候选（证据指纹指向真实失败，非构造）。
  一条都产不出 = 任务集失败面不够宽，回 V2 补任务。
- **V5**：每个 detector 三列表齐全；**任何阈值改动必须附 report.py 前后对比**，不接受"感觉更好了"。

> **半年内若 `propose()` 仍产不出可采纳的候选，撤掉 Learning 的运行时接线，只留离线分析。**
> 判据照 ADR 0026 的做法先写死——避免又一个"机制建好了没人用"却一直挂着的能力。
