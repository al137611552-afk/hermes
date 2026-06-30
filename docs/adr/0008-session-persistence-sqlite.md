# ADR-0008: 会话历史持久化用 SQLite

- 状态：已接受
- 日期：2026-06-09

## 背景
对话历史此前只存内存（`bridge/Api.history`），关掉应用即丢，也无法多会话切换。
P6.1（PRD FR-6.1）要求重启后能恢复、可管理多个会话。

## 决策
- **SQLite（标准库 sqlite3）**，无新依赖。两张表：`sessions` + `messages`
  （`messages.session_id` 外键，删除会话级联删消息）。
- 消息 `content` 以 **JSON 文本**存（`json.dumps`），读时还原成 `Message.content`
  （`str | list[dict]`）——与既有 tool-use / 多模态 content blocks 表示一致，零转换损耗。
- 存储层 `store/db.py:Store` 封装连接与 CRUD；`check_same_thread=False` + 一把锁，
  适配 pywebview 工作线程并发。
- **会话生命周期**：启动不建空会话；首条消息时才 `create_session`（标题取首条用户
  文本前若干字），避免空会话堆积。`new_session` 只置空当前 id，下次发消息再建。
- DB 路径由 `config.storage.db_path` 控制，默认 `ROOT/data/hermes.db`（`data/` 入 .gitignore）。
- 持久化可由 `storage.enabled` 关闭（此时退化为纯内存，行为同 P5 前）。

## 备选与权衡
- JSON 文件存历史：简单但并发写、查询、删除单会话都笨拙 —— 否决。
- 引入 ORM（SQLAlchemy 等）：对两张表是过度工程、增依赖 —— 否决，直接用 sqlite3。
- 每条消息即时落库（而非整轮结束才写）：进程中途崩溃也不丢历史 —— 采用。

## 结果
- 对话自动存盘、重启恢复、左侧栏多会话管理（新建/切换/删除）。
- content 与内核表示同构，未来加字段（如 token 计数、时间戳展示）只动存储层。
- 为 P6 后续（token 预算、长期记忆）提供了落地的数据基座。
