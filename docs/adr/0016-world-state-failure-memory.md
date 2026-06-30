# ADR 0016 — World State + Failure Memory（块E）

- 状态：Accepted（块E 已实现）
- 日期：2026-06-30
- 关联：[ADR 0014 评估/策略分层架构](0014-evaluation-policy-architecture.md)（其块E 的固化）、[ADR 0015 错误分类](0015-error-taxonomy.md)（失败的 key）

## 背景

到块D 为止，每一步的"判断"都是**无记忆**的：同一条死路（同一工具+同一入参以同一种方式
失败）可以被反复尝试，模型每次从零判断、每次撞墙。要让 Agent"吃一堑长一智"，必须把
"差距"和"失败"**物化并记住**——本会话内，以及跨会话。

ADR 0014 的不变量③："**物化你要学习的，别建你不需要的引擎**"。所以块E 只做两件事：
记录事实 + 把"此路已 N 次不通"当**事实**喂回模型；**不**建决策引擎——选路仍由模型做。

## 决策

两个数据结构（`src/agentcore/agent/world_state.py`）：

### 1. WorldState（单会话，纯内存）

每个 `AgentLoop.run` 一个实例，累积本轮事实：
- `need_history` —— 逐轮 Need（小而稳，是聚合 key）。
- 按**指纹**聚合的失败计数（`record_failure` / `failures_for` / `classes_for`）。
- `invalidated` —— 已证伪路径（APPROACH_INVALIDATED 落地的具体描述）。
- `blocked` —— 未决阻塞（GOAL_BLOCKED）。

### 2. FailureMemory（跨会话，SQLite）

独立文件 `data/failures.db`，复用标准库 `sqlite3`（无新依赖）。
- key = `(指纹, 错误分类, 失败的 Decision 标签)`，记 `count` + 首/末时间。
- **一次失败事件 = 一行增量**：classify 可能给一次失败多个分类，只记**主分类**
  （classify 已按优先级排序，第一个=根因），避免一次失败被重复计数。
- `known_deadend(指纹, threshold)` —— 累计失败 ≥ 阈值 → 返回 `(次数, 主分类)`，供 E3。

### 指纹（fingerprint）

`工具名 + 归一化的关键入参`（command/path/pattern/query/url/name），sha1 截 16 位。
归一化折叠空白 + 小写，让"同一条路"稳定收敛到同一 key，无关入参（如 background 标志）不参与。

### E3 接线（loop.py `detect_repeated_failure`）

仿现有 nudge（stuck/browse/login）模式：探测 + 记录 + 返回注入文案。
- 每个失败工具结果走 `_assess`（块B+C）拿分类；**瞬时 IO 不计**——那是块D 自动重试的活，不是死路。
- 非瞬时失败 → 记入 WorldState + FailureMemory。
- 本会话累计 ≥ `deadend_threshold`（默认 2）**或**跨会话已知死路 → 注入一条事实：
  "这条路已累计 N 次以「X」失败，请换一条思路"。每指纹每轮只提一次。
- 整段 try/except 包死：记忆/分类故障绝不影响工具结果回灌。默认开
  （config `failure_memory: true` / `deadend_threshold: 2`）；构造器默认 `failure_memory=None`
  → 存量测试零行为变化。

## 为什么是"喂事实"而非"硬拦截"

死路判断有误报风险（指纹可能过粗/过细，失败可能是环境抖动）。**硬拦截**（直接禁止再调）会把
误报变成功能缺失；**喂事实**让模型带着"此路 N 次不通"的信息自己决定——它可以换思路，也可以
在确有把握时坚持。这与块A 的 nudge、块D"撞上限把失败回灌而非伪造 Need"一脉相承：
**决策层只提供事实与硬规则，最终判断交给模型**。真正的"自动避坑"留到块F（Golden 门）兜底后、
块G（Learning）按语料证据收紧。

## 影响

- 新增 `agent/world_state.py` + `tests/test_world_state.py`（15 测）+ config 两项 + `data/failures.db`。
- `deadend_hint` 事件（纯观测，前端暂不特殊渲染，与其它 *_hint 一致）。
- 为块F/G 备好聚合 key：`(指纹, taxonomy, Decision) → 成功率` 的统计可直接建在 FailureMemory 上。
