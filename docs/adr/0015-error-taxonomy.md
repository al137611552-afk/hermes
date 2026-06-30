# ADR 0015 — 错误分类（Error Taxonomy）

- 状态：Accepted（块C 已实现）
- 日期：2026-06-30
- 关联：[ADR 0014 评估/策略分层架构](0014-evaluation-policy-architecture.md)（本 ADR 是其块C 的固化）

## 背景

块B 让每个 Skill 产出结构化 `Evaluation`（事实：metrics/signals/issues）。但"失败"
还只是自由文本信号，无法聚合、无法当 key。要做：
- 块D 自动重试——必须能判"这次失败该不该重试"（只有瞬时类该重试）。
- 块E Failure-Memory——必须能按"哪类失败"持久化、查重、避坑。
- 块G Learning——必须能统计"最近 N 次里各类失败占比"，才知道往哪优化。

这些都需要一个**稳定、小、可枚举**的失败分类。

## 决策

### ErrorClass：9 类，与 Need 正交

`ErrorClass` 答"这次失败是哪一类根因"，`Need` 答"世界缺什么"——两者正交、各管一维。

| 类别 | 含义 | 典型信号 |
|---|---|---|
| `TRANSIENT_IO` | 瞬时 IO/网络抖动 | 超时、connection refused、端口占用、EADDRINUSE |
| `AUTH` | 鉴权/授权失败 | 401/403、permission denied、invalid token、凭证过期 |
| `NOT_FOUND` | 找不到资源 | no such file、ModuleNotFoundError、command not found、未找到、缺失 |
| `SYNTAX` | 编译/解析错 | SyntaxError、IndentationError、parse error、编译报错 |
| `LOGIC` | 逻辑/断言失败 | AssertionError、测试未通过、FAILED、expected…but |
| `RESOURCE` | 资源/配额耗尽 | OOM、no space left、429、quota、限流 |
| `AMBIGUOUS` | 指令/匹配不唯一 | ambiguous、did you mean、多个匹配 |
| `EXTERNAL_BLOCKED` | 第三方硬阻塞 | 登录墙、验证码、503、被封 |
| `UNKNOWN` | 有失败但未归类 | —（兜底，绝不丢失败） |

### 分类是"规则先行、UNKNOWN 收口"

`src/agentcore/agent/taxonomy.py`：
- `classify_text(text) -> list[ErrorClass]`：纯正则规则跑一段文本，命中即收，按**优先级**去重排序。
- `classify(evaluation, output="") -> list[ErrorClass]`：主入口，**失败才分类**。

**两条纪律**：
1. **失败判定 = `evaluation.issues` 非空**（块B 把 blocker 都放进 issues）。没失败 → 返回 `[]`，
   不污染 Failure-Memory。例：空检索结果（SearchEvaluator 不判 issue）不算失败。
2. **有失败必给类**：规则没命中 → `[UNKNOWN]`，绝不把失败吞成"没事"。

### 优先级：可行动 / 根因 在前

返回列表的**首元素 = 主类**。排序原则：
- `TRANSIENT_IO` 最前——最可行动（直接重试），块D 据 `TRANSIENT_IO in classes` 触发退避。
- 根因类（`NOT_FOUND`/`SYNTAX`）排在表象类（`LOGIC`）前——`ModuleNotFoundError` 常是断言失败的真因，
  修 import 比改断言对。
- 一次失败可命中多类（列表全返回），主类驱动决策、其余供诊断。

### 为什么先规则、不先上模型

规则零成本、确定、可测、可解释，覆盖 90% 常见失败。`UNKNOWN` 占比就是"规则盲区"的度量——
块G Learning 可据此决定要不要给某段补规则、或引入模型分类。**对外形态（两个函数 + 枚举）不变**，
未来把 `classify_text` 内部换成模型/混合，调用方无感。

## 接线（块C 落地点）

`loop.py _emit_result`：能评估的工具结果，在 `eval` 里附 `error_classes: [...]`（纯观测，
**不参与控制流**，try/except 包死）。前端 `formatEval` 把分类标签 `[transient_io]` 缀在
摘要条末尾，给人快速根因感。

## 后果

- 块D 有了重试判据：`TRANSIENT_IO in error_classes`。
- 块E/G 有了稳定聚合 key：`(ErrorClass, 上下文指纹)`。
- 风险：正则误判/漏判。对策——`UNKNOWN` 兜底使漏判可见可度量；新模式补进 `_RULES` 即可，配测试。

## 验收

`tests/test_taxonomy.py` 20 测：8 类各自规则命中 + 优先级（TRANSIENT 最前、NOT_FOUND 先于 LOGIC）
+ `classify` 失败门控 + UNKNOWN 兜底 + 三类 Evaluator 典型失败端到端可分类 + 枚举完整性。全回归绿。
