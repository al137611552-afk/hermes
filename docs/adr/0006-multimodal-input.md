# ADR-0006: 多模态输入实现 —— 统一内容块；PDF 抽文本而非渲染图片

- 状态：已接受
- 日期：2026-06-09

## 背景
P4 实现输入侧多模态（图片 + 文档），范围由 ADR-0004 界定（含图片/文档，不含语音）。
需决定：内部如何表示多模态内容、PDF 怎么处理、两个 provider 如何适配。

## 决策
- **统一内容块**：复用 P3 已落地的 Anthropic 风格 content blocks 作为内部单一表示，
  不引入新格式。图片 = `image` block（base64），文档 = `text` block（`<document>` 包裹）。
- **归一边界**：所有附件在 `multimodal/ingest.py:build_user_content()` 转成 blocks，
  内核与 provider 只见统一 blocks（CONVENTIONS §6「多模态边界归一」）。
- **PDF 用 pypdf 抽取文本**，而非逐页渲染成图片喂视觉模型。
- **Provider 适配**：Anthropic 原生支持 `image` block，直传；OpenAI 在
  `_messages_to_openai` 把 `image` block 转成 `image_url`（data URL）。
- **入口三件套**：粘贴、拖拽、选文件按钮；前端读成 base64 传给 bridge。
- **限额**：图片大小、文档字符数、附件数量由 `config.multimodal` 控制，超限拒绝并提示。

## 备选与权衡
- PDF 渲染成图片喂视觉：保留版面/图表/扫描件，但依赖重（PyMuPDF/poppler）、
  Windows 部署麻烦、按页吃 token —— 否决。
- pypdf 抽文本：轻量、省 token、部署简单；代价是丢版面与扫描件 —— **采用**，
  扫描件（抽不到文本）时给出明确提示，未来如需可另开 ADR 增补渲染回退。
- 为多模态新增独立消息格式：与既有 tool-use blocks 重复 —— 否决，统一复用。

## 结果
- 用户可粘贴/拖拽/选择图片与文档作为上下文；文本/代码/PDF 统一为文档文本块。
- 新增模态（如 P5 截图）只需在 `multimodal/` 产出对应 block，provider/内核零改动。
- 图片是否可用取决于所选模型的视觉能力（与厂商解耦，模型不支持时 API 报错冒泡）。
- 图片占用上下文 token，压缩留待 P6。
