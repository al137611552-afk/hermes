# ADR-0011: 图像外置 blob 存储

- 状态：已接受
- 日期：2026-06-09

## 背景
P6.1 把消息 content 直接以 JSON 存进 SQLite，其中 image 块的 base64 会全量入库。
P5 让 Agent 能在循环里主动反复截图，单个会话可能堆积多张多 MB 图，base64 再 +33%，
导致：DB 文件急剧膨胀、`load_session` 读出整段含 base64 的 content 拖慢会话切换、备份变重。
PRD FR-5.1.1 要求把图片移出 DB。

## 决策
- **外置 blob + DB 存引用，存储层透明转换**：落库前把 `source.type=="base64"` 的 image 块
  抽出字节写到 `data/blobs/<sha256>.<ext>`，DB 里只留引用块
  `{"type":"image","source":{"type":"hermes_blob","media_type":..,"ref":"<sha>.<ext>"}}`；
  `get_messages` 读出时 rehydrate 回 base64。内核 / provider 始终只见完整 base64。
- **按内容 sha256 去重**：同一张图（如重复粘贴、相同截图）只存一份。
- **GC 用全量引用扫描**：删除会话后，扫描剩余所有消息收集被引用的 ref，删 `blobs/` 下的孤儿
  文件。单用户本地库下 O(消息数) 可接受，且自愈（不依赖精确引用计数）。
- **递归转换**：dehydrate/rehydrate 递归遍历 content（list/dict），覆盖「截屏回合」里
  tool_result 与 image 并列、以及未来可能的嵌套结构；非 image 块原样不动。
- **开关**：`storage.externalize_images`（默认 true）。关掉则退化为旧的 base64 内联存。
- **隔离**：纯函数集中在 `store/blobs.py`，`Store` 仅在 add/get/delete 三处调用，便于单测。

## 备选与权衡
- **只存缩略图 / 占位**：最省事，但重开会话后无法把原图重新喂模型 —— 否决（要保真）。
- **存库前转低质 JPEG/降分辨率**：缓解非根治，且损画质 —— 否决。
- **精确引用计数表**：GC 更高效，但增表与一致性维护成本；本地小库用全量扫描更简单可靠。
- **base64 直接进 tool_result**：与本 ADR 无关，但相关结论见 ADR-0010（端点不解析，故图走并列块）。

## 已知限制
- GC 在每次删会话时全量扫描消息 JSON，超大库下有成本（当前规模可忽略；必要时再上引用计数）。
- 旧库里已内联的 base64 不会自动迁移（向后兼容：rehydrate 对无 blob 引用的内容原样返回）；
  如需迁移旧图可另写一次性脚本。
- blob 文件被外部误删时，rehydrate 优雅降级为「[图片缺失]」文本占位，不抛错。
- 多进程并发写同一库未考虑（应用为单实例本地工具）。

## 结果
- 图片字节移出 DB，长会话（含 Agent 自动截图）不再撑大库、会话切换不被 base64 拖慢。
- 去重 + GC 控制 blobs 目录体积；删会话能回收独有图、保留共享图。
- 保真：重开会话仍能拿到完整图片、可再次喂给视觉模型。
