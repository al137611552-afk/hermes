# ADR-0007: 视觉预处理回退 —— 让不支持视觉的模型也能"看图"

- 状态：已接受
- 日期：2026-06-09

## 背景
P4 支持把图片作为 image block 发给模型，但用户主力模型 MiniMax-M2.7 是纯文本模型，
不消费图像。排查确认：OpenClaw 能用 M2.7 识图，是其 minimax 插件先用 MiniMax 的
视觉端点（`MiniMax-VL-01`）把图转成文字描述，再把描述当文本喂给 M2.7。
我们需要同等能力，且尽量复用用户已有的 `MINIMAX_API_KEY`，不强制额外的视觉模型 key。

## 决策
- **按模型声明视觉能力**：`ModelConfig.vision: bool`。`true` 的模型（Claude/gpt-4o）
  直接收原图；`false` 的模型走预处理回退。
- **视觉预处理回退**（`multimodal/vision.py`）：主模型 `vision=false` 且消息含图时，
  对每张图调 VL 端点拿文字描述，把 image block 替换成 `[图片描述：…]` 文本块，
  再交给主模型。VL 的 prompt = 配置基础 prompt + 用户本轮文本（让描述更贴合意图）。
- **VL 端点**：`POST https://api.minimax.io/v1/coding_plan/vlm`，body `{prompt,image_url}`，
  Bearer 鉴权，`MM-API-Source: OpenClaw`。用标准库 `urllib`，**不引入新依赖**。
  端点/超时/prompt/key 环境变量均由 `config.vision_fallback` 控制（注意 VL 域名是
  `api.minimax.io`，与 chat 的 `api.minimaxi.com` 不同）。
- **失败不致命**：单张图识别失败 → 替换成 `[图片识别失败：…]`，对话继续。
- **可观测**：触发预处理时 emit `vision_start`/`vision_done`，前端显示进度与描述摘要。
- 移除 P4 临时加的 `minimax-vl`（M2.5 chat 档案）——M2.5 经 chat 端点同样不识图，
  视觉统一走 VL 端点的预处理路径。

## 备选与权衡
- 让 M2.5/其它 chat 模型直接吃图：实测 MiniMax chat 端点不消费 image block —— 否决。
- 强制用户上 Claude/gpt-4o 才能用图：增加门槛、要额外 key —— 否决（保留为 `vision:true` 直发路径）。
- 视觉预处理（VL→文字描述）：复用同一个 key、对"看截图/报错"场景够用，代价是主模型
  看到的是描述而非原图（有信息损失）—— **采用**，符合 OpenClaw 既有实践。

## 结果
- M2.7 等纯文本模型可"间接"用图；支持视觉的模型仍走原图直发，二者由 `vision` 标记自动切换。
- 视觉源端点完全可配，便于换区域/换厂商的 VLM。
- 描述质量与 OCR 精度取决于 VL-01；若后续需要更高保真，可另开 ADR 引入"原图直发优先"策略。

## 已知限制（2026-06-09 真机验证后补记）
- MiniMax `coding_plan/vlm` 是**编程套餐专属端点**，普通开放平台 API key 访问会报
  "密钥无效"（实测：Global 域 `api.minimax.io` 报无效；CN 域 `api.minimaxi.com` 无此端点）。
- 因此 `vision_fallback` **默认关闭**。实现/配置（端点、key、开关）均已就绪，
  拿到可访问 vlm 的凭证（编程套餐 key），或把 endpoint/key 指向其它视觉服务后，
  开启 `enabled` 即可用。支持视觉的主模型（Claude/gpt-4o）不受此限制，走原图直发。
