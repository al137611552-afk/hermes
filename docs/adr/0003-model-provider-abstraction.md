# ADR-0003: 模型适配层统一抽象，支持 Anthropic + OpenAI 兼容

- 状态：已接受
- 日期：2026-06-08

## 背景
用户要能自配置/切换模型。需要既支持 Claude，也支持广泛的 OpenAI 兼容服务
（OpenAI、DeepSeek、各类中转）。

## 决策
- 定义统一接口 `providers/base.py:BaseProvider`，方法 `stream_chat() -> Iterator[StreamEvent]`。
- 首批实现两个 Provider：`AnthropicProvider`、`OpenAIProvider`。
- 工厂 `build_provider(config, model_name)` 按 `config.yaml` 的档案构造。
- UI 与 Agent 内核**只依赖 BaseProvider**，不感知具体厂商。

## 备选与权衡
- 直接在业务里调各家 SDK：耦合高，换模型要改多处 —— 否决。
- 统一接口：新增厂商 = 一个实现 + 注册；UI/内核零改动 —— 采用。

## 结果
- 切换模型只改配置。
- `StreamEvent` 预留扩展（P3 加 `tool_use`，P4 加 vision 内容块）。
- 各家 API 差异（system 提示、工具格式、多模态块）在各自 Provider 内部消化。
