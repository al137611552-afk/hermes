"""配置加载：config.yaml（模型档案） + .env（密钥）。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from .paths import APP_DIR, BUNDLE_DIR

# 可写基目录：config.yaml / .env / data/ 都相对它。源码模式 = 项目根，打包后 = exe 旁边。
ROOT = APP_DIR


class ModelConfig(BaseModel):
    provider: Literal["anthropic", "openai"]
    model: str
    api_key_env: str
    base_url: str | None = None
    max_tokens: int = 4096
    temperature: float | None = None  # 采样温度；None = 用各 provider 默认
    vision: bool = False  # 模型是否原生支持图像输入；否则走视觉预处理回退
    prompt_cache: bool = True  # anthropic 协议加 cache_control 前缀缓存（FR-10.4b，实测方舟支持）；
                               # 不支持的端点自动降级，本开关可按档案强关


class PermissionsConfig(BaseModel):
    """细粒度权限规则（FR-11.4）：免确认 / 拦截。deny 优先于 allow。

    规则形如 `工具名` 或 `工具名(glob)`，glob 匹配该工具的「主体」（命令/路径/URL）。
    例：allow: ["run_powershell(git *)", "write_file(docs/*)"]；deny: ["run_powershell(rm *)"]。
    """
    allow: list[str] = []
    deny: list[str] = []


class RoleSpec(BaseModel):
    """自定义子 Agent 角色（FR-10.5，`agent.roles` 的值；可新增或同名覆盖内置角色）。"""
    label: str = ""                  # 前端子任务块显示名；空 = 用角色名
    directive: str = ""              # 追加到子 Agent system 的职责说明
    tools: list[str] | None = None   # 工具白名单（所列即所得）；省略 = 全工具
    model: str | None = None         # 该角色用的模型档案名；省略 = subagent_model → 当前对话模型


class HookConfig(BaseModel):
    """可编程生命周期 hook（对标 Claude Code PreToolUse/PostToolUse）。

    event：`PreToolUse`（工具执行前，退出码 2=拦截/1=警告/0=放行）或 `PostToolUse`（执行后，stdout 追加到结果）。
    matcher：对工具名 re.search 的正则（如 `write_file|edit_file`、`run_`）；空=匹配全部。
    command：要跑的命令（cwd=工作区，stdin 收到 {event,tool,params,workspace[,result]} 的 JSON）。
    """
    event: str                       # PreToolUse | PostToolUse
    command: str                     # shell 命令
    matcher: str = ""                # 工具名正则，空=全部
    name: str = ""                   # 显示名（回灌信息里标注哪个 hook）
    timeout: int = 15                # 单 hook 超时秒


class AgentConfig(BaseModel):
    """Agent 工具循环相关配置（P3）。"""
    workspace: str | None = None  # 显式固定工作区；设了则全局用它、并关闭"按会话隔离"
    per_session_workspace: bool = True   # 每个会话用独立文件夹（隔离不同项目，避免互相污染）
    workspaces_root: str | None = None   # 会话工作区根；None -> ROOT/data/workspaces
    max_steps: int = 25
    model_max_tokens: int = 0     # 主模型输出上限覆盖（0=跟随模型档 max_tokens）。长任务被截断时在设置里调高
    token_budget: int = 0         # 会话累计 token 预算总额（0=不设）。顶部用量芯片据此显示已用百分比、还剩多少
    shell: Literal["powershell", "pwsh", "cmd", "bash", "zsh"] = "powershell"
    shell_timeout: int = 180   # 前台命令超时（秒）：放宽到 180s 容纳装依赖/编译这类真·慢命令；
                               # 交互命令已由 stdin=DEVNULL 快速失败、长服务该 background:true，不靠超时兜
    screenshot: bool = True  # 是否给 Agent 截屏工具（take_screenshot）；执行仍过权限 gate
    conventions_file: str = "hermes.md"  # 工作区根的项目规范文件，注入 system（"" 关闭）
    auto_conventions: bool = True  # 工作区有项目内容但缺 conventions_file 时，自动生成一版
    subagent_model: str | None = None  # 委派子任务用的模型档案名；None=用当前主模型（FR-9.3）
    subagent_max_steps: int = 15       # 子 Agent 循环步数上限（独立于主循环 max_steps）
    subagent_max_tokens: int = 0       # 子 Agent 输出上限覆盖（0=跟随子模型档 max_tokens）
    roles: dict[str, RoleSpec] = {}    # 自定义子 Agent 角色（FR-10.5）；与内置角色合并、同名覆盖
    permissions: PermissionsConfig = PermissionsConfig()  # 细粒度权限规则（FR-11.4）
    auto_approve_safe: bool = True     # 智能确认分级（Tier1，对标 Claude Auto mode / Cursor Auto-review）：
                                       # 自动放行「明显安全」的只读/检视/测试 shell 命令（ls/cat/grep/git status/
                                       # pytest 等），不再逐次弹确认。safe-by-default——写文件/编辑/commit/装依赖/
                                       # 拿不准的命令仍照常确认；毁灭性命令永远拦。默认开，🛠 面板可关
    auto_checkpoint: bool = True       # 每个回合首次写文件前自动打检查点（P12 方案A，对标 Claude Code/
                                       # Cursor 自动打点）：不靠模型自觉，用户可在面板一键回到任意回合前
    auto_verify: bool = True           # 写文件后零成本语法校验（FR-11.2a）：py/json 必校，js 有 node 才校
    auto_review: bool = False          # 一轮里改过文件就在收尾自动派 reviewer 子 Agent 审 diff（FR-11.2b）；
                                       # 默认关——每次会多一次模型调用，按需开；纯对话/只读轮不触发
    auto_test: bool = False            # 验证闭环（FR-11.2c）：一轮改过文件就在收尾跑 test_command，失败把
                                       # 输出回灌、复用同一循环让模型修，限 test_max_iters 次。默认关
    test_command: str = ""             # 测试命令（如 "pytest -q" / "npm test"）；空=不跑。shell 执行、cwd=工作区
    test_max_iters: int = 2            # auto_test 失败后自动迭代修复的最大轮数（防死循环）
    auto_affected_test: bool = False   # 编辑后跑定向测试（FR-13.C）：改完文件即识别**受影响的测试**并直跑、
                                       # 失败附加回灌（区别于 auto_test 的收尾跑整套）。默认关——会起子进程，按需开
    affected_test_runner: str = "auto" # FR-13.C 运行器：auto（.py 有 pytest 用 pytest 否则独立脚本）/pytest/python
    delegate_max_revisions: int = 0    # 委派评分回炉（借 Claude Code Performance Outcomes）：子 Agent 产出后由
                                       # lead 模型按验收标准评分，不达标带反馈打回重做，最多 N 轮。0=关（不评、行为同旧版）
    hooks: list[HookConfig] = []       # 可编程生命周期 hooks（PreToolUse/PostToolUse）：用户自定义守卫/动作
    stuck_edit_threshold: int = 3      # 情境自启：同一文件被改 ≥N 次且仍在失败时，自动提示模型用 trace_run
                                       # 看中间值定位（不再盲改）。0=关。零用户配置、由 hermes 判断时机
    search_nudge_files: int = 40       # 情境自启：项目代码文件 ≥N 时，若模型逐个浏览很多文件还没用
                                       # search_code，自动提示它按意图检索（省步数）。0=关
    crazy_max_rounds: int = 20         # 自主/crazy 模式外层目标循环的最大轮数（无人值守的预算护栏）
    crazy_max_seconds: int = 3600      # crazy 墙钟时间预算（秒，=1 小时），0=不限；超时回合间停
    crazy_max_tokens: int = 3000000    # crazy 累计 token 预算（300 万，资源止损防无人值守烧飞），0=不限；超预算回合间停
    crazy_stall_rounds: int = 3        # crazy 连续多少轮没动用任何工具（疑似空转）就停
    crazy_gate_ask: bool = True        # 块3 自适应过门：撞设计岔路/目标模糊（[[NEED_USER]]）或验收反复修不过时，
                                       # **停下来真问用户**（类人协作）。False=纯无人值守（按合理默认自走、从不停下问）
    crazy_verify_ask_at: int = 3       # 验收门连续修不过这么多次后，停下来问用户（换思路/跳过/接手）；gate_ask=False 时只按预算兜
    crazy_replan: bool = True          # 块4 阶段后重规划：每个阶段通过验收（[[PHASE_DONE]]）后，下一轮先按这阶段
                                       # 学到的（难点/新约束/更省事的做法）调整剩余阶段，再继续。False=死守初始拆分、不重规划
    auto_retry: bool = True            # 块D 自动重试：单个工具调用因**瞬时 IO**（超时/网络抖动/端口占用）失败时，
                                       # 自动退避重试，不打扰模型。False=关（失败直接回灌模型）
    retry_max_attempts: int = 2        # auto_retry 的最大重试次数（不含首次执行）；总执行 = 1 + 本值
    retry_backoff_base: float = 0.5    # auto_retry 退避基数秒，第 n 次重试前等 base*2^(n-1)（指数退避）
    failure_memory: bool = True        # 块E 死路记忆：同一条路（工具+关键入参）反复**非瞬时**失败时，
                                       # 记入跨会话记忆并提示模型换思路（不再原样重试）。False=关
    deadend_threshold: int = 2         # 同一条路累计失败 ≥ 此值 → 提示换思路（瞬时 IO 不计，归 auto_retry）
    research_refine: bool = True       # 块H2：联网搜索返回了但不达标（如无一在预算内）时提示换词/换源重搜。False=关
    research_refine_max: int = 1       # 同一搜索 query 最多催重搜几次（防无限重搜）
    research_max_rounds: int = 3        # **整轮**催重搜总预算；达上限→停搜、用现有内容综合作答+声明局限（防换词绕过 per-query cap 无限重搜）
    research_judge: bool = True        # 块H3a：H2 正则拦不住时再过模型裁判判语义相关性（"夏季"≠秋冬款）。
                                       # 每次搜索后多一次模型调用（有成本/延迟）；False=关（只用 H1/H2 正则）
    design_review: bool = True         # ADR 0019 Architecture Review Mode：规划模式下多角色（Execution⟷Architecture）
                                       # 评审方案、产出四态共识、开工 gate 卡"未决阻塞==0"。默认开——只在用户点「评审」时才跑，
                                       # 不点零成本（区别于 auto_review/auto_test 那种每轮自动触发的功能，故不套"额外调用→默认关"惯例）
    design_review_max_rounds: int = 4  # 评审轮数上限（含初始规划快照）：4 = 最多 3 个讨论轮（v5 hub-and-spoke），防无限互评
    design_review_timeout_s: int = 90        # 单个评审角色单次调用超时（秒），慢/卡按空评审跳过
    design_review_verdict_max_tokens: int = 2048  # 单角色评审结论输出上限（防长篇大论的安全网，非抽取）
    design_review_models: dict = {}    # 异构路由：reviewer 名→模型档案名（如 {"technical":"openai/gpt-4o"}）。
                                       # 角色名：product（产品/市场镜头）、technical（技术镜头）；旧键 execution/architecture 读时自动归一。
                                       # 空=全部用当前主模型（同模型双角色，离线零成本）；推荐给两角色各配一个异构档形成真·双脑评审

    def resolve_workspace(self) -> Path:
        return Path(self.workspace).expanduser().resolve() if self.workspace else ROOT

    def resolve_workspaces_root(self) -> Path:
        if self.workspaces_root:
            return Path(self.workspaces_root).expanduser().resolve()
        return ROOT / "data" / "workspaces"


class MultimodalConfig(BaseModel):
    """多模态输入限制（P4）。"""
    max_image_mb: int = 5
    max_doc_chars: int = 100_000
    max_attachments: int = 10


class VisionFallbackConfig(BaseModel):
    """视觉预处理回退（P4.1）：主模型不支持视觉时，用 VL 端点把图转文字描述。

    默认关闭：MiniMax 的 coding_plan/vlm 端点需要编程套餐凭证，普通开放平台 API key
    访问会报"密钥无效"。拿到可访问 vlm 的凭证（或换其它视觉服务端点）后再开启。
    """
    enabled: bool = False
    endpoint: str = "https://api.minimax.io/v1/coding_plan/vlm"
    model: str = "MiniMax-VL-01"  # 备注用；端点路径已隐含模型
    api_key_env: str = "MINIMAX_API_KEY"
    prompt: str = "详细描述这张图片的内容，包括其中的文字、图表、界面元素与报错信息等。"
    timeout: int = 60


class StorageConfig(BaseModel):
    """会话持久化（P6.1）。"""
    enabled: bool = True
    db_path: str | None = None  # None -> ROOT/data/hermes.db
    externalize_images: bool = True  # 图片外置 blob 存储（P5.1），避免 base64 撑大 DB

    def resolve_db_path(self) -> Path:
        return Path(self.db_path).expanduser() if self.db_path else (ROOT / "data" / "hermes.db")


class ContextConfig(BaseModel):
    """上下文 token 预算与压缩（P6.2）。

    会话变长后整段历史喂给模型会撑爆上下文窗口、抬高成本。开启后在喂给模型前，
    超预算则保留最近 keep_recent_turns 个回合、把更早内容压成摘要塞进 system。
    token 为启发式估算（无新依赖），按预算决策足够。
    """
    # model_summary 字段以 model_ 开头，撞 pydantic v2 保留前缀；显式放开该模型的保护命名空间消警告
    model_config = {"protected_namespaces": ()}
    enabled: bool = True
    max_input_tokens: int = 32000   # 喂给模型的输入 token 预算（含 system）
    keep_recent_turns: int = 6      # 至少保留最近多少个用户回合
    model_summary: bool = True      # 压缩摘要由模型生成（FR-10.4a，对标 /compact）；
                                    # 按覆盖范围缓存、增量合并，失败回退启发式截断
    summary_model: str = ""         # 生成摘要用的模型档案名；空 = 当前对话模型


class MemoryConfig(BaseModel):
    """长期记忆（P6.3）：跨会话持久的事实 / 偏好 / 项目背景。

    模型可用 remember / recall / forget 工具增删查；离开会话（新建 / 切换）时可自动
    从刚结束的对话里抽取要点存为记忆。每次发消息时把记忆注入 system，使新会话也「记得」。
    用独立 SQLite 文件（默认 data/memory.db），与会话库解耦。
    """
    enabled: bool = True
    auto_capture: bool = True         # 离开会话时自动抽取记忆（额外一次模型调用）
    db_path: str | None = None        # None -> ROOT/data/memory.db
    max_inject: int = 30              # 注入 system 的记忆条数上限
    max_inject_chars: int = 2000      # 注入 system 的字符预算
    min_messages_to_capture: int = 4  # 对话至少这么多条消息才触发自动抽取
    auto_consolidate: bool = True     # 攒够碎片后离线把它们固化成框架原则（类人记忆：提炼框架）
    consolidate_threshold: int = 16   # fact 碎片攒到这么多、且较上次固化新增足够，就触发一次固化

    def resolve_db_path(self) -> Path:
        return Path(self.db_path).expanduser() if self.db_path else (ROOT / "data" / "memory.db")


class McpServerConfig(BaseModel):
    """单个 MCP server（stdio 本地子进程）。形状对齐 Claude Desktop 的 mcpServers。"""
    enabled: bool = True
    command: str                              # 启动命令，如 npx / uvx / python
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None                    # 子进程工作目录
    trust: bool = False                       # true -> 该 server 工具免权限 gate（默认逐次确认）


class MCPConfig(BaseModel):
    """MCP 工具接入（P6.4）：作为客户端连接外部 MCP server，把其工具接进 Agent 循环。

    本期仅 stdio 传输、仅 tools。外部工具默认 dangerous（逐次过权限 gate），
    server 配 trust:true 可免确认。默认关闭：需本机有运行 server 的环境（node 的 npx /
    python 的 uvx 等）；未装 mcp SDK 时只要 enabled=false 即不受影响。
    """
    enabled: bool = False
    connect_timeout: float = 60               # 单个 server 启动 + 握手 + 列工具超时（秒）；
                                              # 60s 是为容纳「首次连接 npx/uvx 下载 server 包」（首跑慢、之后有缓存快）
    call_timeout: float = 60                  # 单次工具调用超时（秒）
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    @field_validator("servers", mode="before")
    @classmethod
    def _servers_none_to_empty(cls, v):
        # 容错：config.yaml 写成 `servers:`（后面留空/null）当作「无 server」，
        # 不必非写 `servers: {}`——避免用户解注释 server 块时撞到 `{}` 导致 YAML 报错。
        return v or {}


class WebConfig(BaseModel):
    """联网检索（FR-11.1）：web_search / web_fetch 两个只读工具。"""
    enabled: bool = True
    search_engine: str = "auto"   # auto（Bing 优先、DDG 兜底）/ bing / duckduckgo
    timeout: int = 20             # 单次请求超时（秒）
    max_results: int = 5          # 搜索默认条数（硬上限 10）
    fetch_max_chars: int = 20000  # web_fetch 正文输出默认上限


class AppConfig(BaseModel):
    active_model: str
    system_prompt: str = "You are a helpful coding assistant."
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)
    vision_fallback: VisionFallbackConfig = Field(default_factory=VisionFallbackConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    web: WebConfig = Field(default_factory=WebConfig)

    def get_model(self, name: str | None = None) -> ModelConfig:
        key = name or self.active_model
        if key not in self.models:
            raise KeyError(f"未找到模型档案 '{key}'，请检查 config.yaml")
        return self.models[key]

    def resolve_api_key(self, mc: ModelConfig) -> str:
        return self.resolve_api_key_env(mc.api_key_env)

    def resolve_api_key_env(self, env_name: str) -> str:
        key = os.getenv(env_name, "").strip()
        if not key:
            raise RuntimeError(
                f"环境变量 {env_name} 未设置。请在 .env 中填入对应的 API key。"
            )
        return key


USER_MODELS_FILE = "user_models.yaml"  # 用户在设置面板加/改的模型档案，与内置 config.yaml models 合并


def merge_models(base: "dict | None", user: "dict | None") -> dict:
    """合并内置模型档案（config.yaml）与用户档案（user_models.yaml）：用户同名覆盖、新名新增。"""
    out = dict(base or {})
    out.update(user or {})
    return out


def load_user_models(path: "Path | None" = None) -> dict:
    """读用户模型档案文件；不存在 / 解析失败返回 {}（回退只用内置档案，不致命）。"""
    p = path or (APP_DIR / USER_MODELS_FILE)
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def save_user_models(models: dict, path: "Path | None" = None) -> None:
    """把用户模型档案写回独立文件（纯 yaml dump，与含大量注释的 config.yaml 解耦、不怕弄坏注释）。"""
    p = path or (APP_DIR / USER_MODELS_FILE)
    p.write_text(yaml.safe_dump(models or {}, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")


# ---- Provider 中心配置（产品化③：provider 配一次 key/url/格式，下挂多个模型）------------
DEFAULT_MAX_TOKENS = 16384
PROVIDERS_FILE = "providers.yaml"  # 用户的 provider 配置（启用状态 / url 覆盖 / 启用的模型 / 自定义）

# 内置 provider 预设：用户只填 key 即可用；base_url / 协议格式 / 常见模型都预填、可改。
# provider 字段 = 协议格式（anthropic / openai，对应 ModelConfig.provider）。
PROVIDER_PRESETS: dict = {
    "volcengine-ark": {
        "label": "火山方舟", "provider": "anthropic", "api_key_env": "ARK_API_KEY",
        "base_url": "https://ark.cn-beijing.volces.com/api/coding",
        "models": [
            {"id": "kimi-k2.6", "vision": True}, {"id": "deepseek-v4-pro"},
            {"id": "doubao-seed-2.0-pro"}, {"id": "glm-5.1"}, {"id": "minimax-m2.7"},
        ],
    },
    "anthropic": {
        "label": "Anthropic", "provider": "anthropic", "api_key_env": "ANTHROPIC_API_KEY",
        "base_url": "",
        "models": [{"id": "claude-opus-4-8", "vision": True}, {"id": "claude-sonnet-4-6", "vision": True}],
    },
    "openai": {
        "label": "OpenAI", "provider": "openai", "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "models": [{"id": "gpt-4o", "vision": True}],
    },
    "deepseek": {
        "label": "DeepSeek", "provider": "openai", "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
        "models": [{"id": "deepseek-chat"}],
    },
    "moonshot": {
        "label": "Kimi (Moonshot)", "provider": "openai", "api_key_env": "MOONSHOT_API_KEY",
        "base_url": "https://api.moonshot.cn/v1",
        "models": [{"id": "kimi-k2.6"}, {"id": "kimi-k2.5"}],
    },
}


def expand_provider_profiles(presets: dict, user_providers: "dict | None",
                             default_max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """把「启用的 provider × 启用的模型」展开成扁平模型档案 {name: profile}，喂给 build_provider
    （所以核心一行不改）。name = f"{provider_key}/{model_id}"。

    user_providers[key] 字段：enabled（默认 False，启用才展开）/ base_url（非空则覆盖预设）/
    models（启用的 model id 列表，省略=该 provider 全部模型）/ custom_models（额外加的 model id）/
    以及自定义 provider 需带 provider·api_key_env（预设外的新 provider）。"""
    out: dict = {}
    up = user_providers or {}
    keys = list(presets.keys()) + [k for k in up if k not in presets]
    for key in keys:
        base = dict(presets.get(key, {}))
        uc = dict(up.get(key, {}))
        if not uc.get("enabled", False):
            continue
        provider = uc.get("provider") or base.get("provider", "anthropic")
        api_key_env = uc.get("api_key_env") or base.get("api_key_env", "")
        base_url = uc.get("base_url") or base.get("base_url") or None
        # 可选模型集合：预设模型（dict 带 vision）+ 自定义模型（裸 id）
        catalog = {m["id"]: m for m in base.get("models", []) if isinstance(m, dict)}
        for mid in (uc.get("custom_models") or []):
            catalog.setdefault(mid, {"id": mid})
        enabled_ids = uc.get("models")
        if enabled_ids is None:
            enabled_ids = list(catalog.keys())
        for mid in enabled_ids:
            m = catalog.get(mid, {"id": mid})
            prof = {
                "provider": provider, "model": mid, "api_key_env": api_key_env,
                "vision": bool(m.get("vision", False)),
                "max_tokens": int(m.get("max_tokens", default_max_tokens)),
            }
            if base_url:
                prof["base_url"] = base_url
            out[f"{key}/{mid}"] = prof
    return out


def load_user_providers(path: "Path | None" = None) -> dict:
    """读用户 provider 配置；不存在 / 解析失败返回 {}（= 没启用任何 provider，回退现有扁平档案）。"""
    p = path or (APP_DIR / PROVIDERS_FILE)
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def save_user_providers(providers: dict, path: "Path | None" = None) -> None:
    """把用户 provider 配置写回独立文件（纯 yaml dump）。"""
    p = path or (APP_DIR / PROVIDERS_FILE)
    p.write_text(yaml.safe_dump(providers or {}, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")


# 开箱默认：从未配过 provider 时，启用火山方舟 + 只勾选 kimi-k2.6（填 ARK_API_KEY 即用）。
DEFAULT_PROVIDERS = {"volcengine-ark": {"enabled": True, "models": ["kimi-k2.6"]}}


# ---- 浏览器穿透（Playwright MCP）一键开关（GUI 管理，与手编 config.yaml 的 mcp 段并存）------------
BROWSER_MCP_FILE = "mcp_browser.json"  # 存「是否启用 / 是否有头登录态」状态，GUI 开关写它、load_config 据此合并 server


def browser_mcp_state(path: "Path | None" = None) -> dict:
    """读浏览器穿透全量状态：{enabled, headed}。不存在/坏档返回 {}。"""
    p = path or (APP_DIR / BROWSER_MCP_FILE)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def browser_mcp_enabled(path: "Path | None" = None) -> bool:
    """GUI 是否启用了浏览器穿透。"""
    return bool(browser_mcp_state(path).get("enabled"))


def browser_mcp_headed(path: "Path | None" = None) -> bool:
    """是否「有头·登录态」模式（可见浏览器、用于手动登录/划滑块，登录态存持久 profile 复用）。"""
    return bool(browser_mcp_state(path).get("headed"))


def set_browser_mcp_state(on: bool, headed: "bool | None" = None,
                          path: "Path | None" = None) -> None:
    """写浏览器穿透开关状态。headed=None 表示不改有头设置（仅切启用/关闭时保留原值）。"""
    p = path or (APP_DIR / BROWSER_MCP_FILE)
    cur = browser_mcp_state(p)
    cur["enabled"] = bool(on)
    if headed is not None:
        cur["headed"] = bool(headed)
    p.write_text(json.dumps(cur), encoding="utf-8")


def browser_mcp_args(headed: bool) -> list:
    """构造 Playwright MCP 启动参数。

    有头（headed=True）：**去掉 `--headless`**，弹出可见浏览器——你可手动登录/划滑块那一次；
    登录态自动存进 Playwright MCP 的**持久 profile**（默认行为），以后每次浏览都是已登录、类人查询。
    无头（默认）：后台跑、快、无人值守，但易撞反爬。
    """
    args = ["-y", "@playwright/mcp@latest"]
    if not headed:
        args.append("--headless")
    # 必须用 chrome（@playwright/mcp 的 --browser 合法值只有 chrome/firefox/webkit/msedge，**没有 chromium**）；
    # chrome 通道直接用系统已装的 Google Chrome（多数 Windows 本就有→零下载），缺则由 playwright install chrome 装。
    args += ["--browser", "chrome"]
    return args


FEATURE_FLAGS_FILE = "feature_flags.json"  # GUI「功能开关」面板写它、load_config 据此覆盖 agent 默认
# GUI 面板可即时开关并持久化的 agent 开关白名单（布尔）+ test_command（字符串）。
TOGGLEABLE_FLAGS = ("auto_affected_test", "auto_review", "auto_test", "affected_test_runner",
                    "delegate_max_revisions", "auto_approve_safe")


def read_feature_flags(path: "Path | None" = None) -> dict:
    """读 GUI 功能开关状态（不存在/坏档返回 {}）。"""
    p = path or (APP_DIR / FEATURE_FLAGS_FILE)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def set_feature_flags(updates: dict, path: "Path | None" = None) -> dict:
    """合并写入功能开关（只接受白名单键 + test_command 字符串），返回合并后的全量状态。"""
    p = path or (APP_DIR / FEATURE_FLAGS_FILE)
    cur = read_feature_flags(p)
    for k, v in (updates or {}).items():
        if k in TOGGLEABLE_FLAGS:
            cur[k] = v
        elif k == "test_command":
            cur[k] = str(v or "")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    return cur


def merge_feature_flags(data: dict, path: "Path | None" = None) -> dict:
    """把 GUI 保存的功能开关覆盖到 data['agent']（load_config 用）。无保存项则原样返回。"""
    flags = read_feature_flags(path)
    if not flags:
        return data
    agent = dict(data.get("agent") or {})
    for k in (*TOGGLEABLE_FLAGS, "test_command"):
        if k in flags:
            agent[k] = flags[k]
    data["agent"] = agent
    return data


# ── 统一「限额与预算」面板（GUI 改数值参数，免手编 config.yaml）────────────────
# 单一数据源 LIMITS_SPEC 同时驱动：① 校验/持久化 ② 前端渲染。key = "section.field" 点分路径。
# 留空/0 的语义由各字段默认与 zero 提示表达；merge_limits 把保存值覆盖进对应 config 段。
LIMITS_FILE = "limits.json"  # GUI「限额与预算」面板写它、load_config 据此覆盖各段默认
LIMITS_SPEC = (
    # 预算
    {"key": "agent.token_budget", "group": "预算", "label": "会话 token 预算总额",
     "hint": "0 = 不设；设了顶部用量芯片显示已用百分比、还剩多少", "type": "int", "min": 0, "max": 100000000},
    # 主模型 / 主循环
    {"key": "agent.model_max_tokens", "group": "主模型 / 主循环", "label": "输出上限 max_tokens",
     "hint": "0 = 跟随模型档；长任务被截断时调高", "type": "int", "min": 0, "max": 200000},
    {"key": "agent.max_steps", "group": "主模型 / 主循环", "label": "单轮最多工具步数",
     "hint": "一轮对话内最多工具调用步数，防死循环", "type": "int", "min": 1, "max": 500},
    {"key": "agent.shell_timeout", "group": "主模型 / 主循环", "label": "前台命令超时（秒）",
     "hint": "装依赖/编译等慢命令的上限；长服务用 background:true", "type": "int", "min": 5, "max": 3600},
    # 委派 / 子 Agent
    {"key": "agent.subagent_max_tokens", "group": "委派 / 子 Agent", "label": "输出上限 max_tokens",
     "hint": "0 = 跟随子模型档", "type": "int", "min": 0, "max": 200000},
    {"key": "agent.subagent_max_steps", "group": "委派 / 子 Agent", "label": "子 Agent 步数上限",
     "hint": "子 Agent 循环步数（独立于主循环）", "type": "int", "min": 1, "max": 500},
    {"key": "agent.delegate_max_revisions", "group": "委派 / 子 Agent", "label": "回炉重评轮数",
     "hint": "0 = 关；子产出由 lead 评分不达标打回重做的最多轮数", "type": "int", "min": 0, "max": 10},
    # 评审
    {"key": "agent.design_review_max_rounds", "group": "评审", "label": "评审最大轮数",
     "hint": "防无限互评", "type": "int", "min": 1, "max": 20},
    {"key": "agent.design_review_timeout_s", "group": "评审", "label": "单角色超时（秒）",
     "hint": "慢/卡的评审调用按空评审跳过", "type": "int", "min": 10, "max": 600},
    {"key": "agent.design_review_verdict_max_tokens", "group": "评审", "label": "评审结论上限 max_tokens",
     "hint": "防长篇大论的安全网；被截断可调高", "type": "int", "min": 256, "max": 32000},
    # 自主 / crazy
    {"key": "agent.crazy_max_rounds", "group": "自主模式（crazy）", "label": "外层目标循环最大轮数",
     "hint": "无人值守的预算护栏", "type": "int", "min": 1, "max": 500},
    {"key": "agent.crazy_max_seconds", "group": "自主模式（crazy）", "label": "墙钟时间预算（秒）",
     "hint": "0 = 不限；超时回合间停", "type": "int", "min": 0, "max": 86400},
    {"key": "agent.crazy_max_tokens", "group": "自主模式（crazy）", "label": "累计 token 预算",
     "hint": "0 = 不限；资源止损，超预算回合间停", "type": "int", "min": 0, "max": 100000000},
    {"key": "agent.crazy_stall_rounds", "group": "自主模式（crazy）", "label": "空转停机轮数",
     "hint": "连续多少轮没动用工具就停", "type": "int", "min": 1, "max": 50},
    {"key": "agent.crazy_verify_ask_at", "group": "自主模式（crazy）", "label": "验收连败问用户阈值",
     "hint": "验收门连续修不过这么多次后停下问用户", "type": "int", "min": 1, "max": 20},
    # 重试 / 研究 / 测试
    {"key": "agent.retry_max_attempts", "group": "重试 / 研究 / 测试", "label": "瞬时失败最大重试次数",
     "hint": "不含首次；总执行 = 1 + 本值", "type": "int", "min": 0, "max": 10},
    {"key": "agent.research_max_rounds", "group": "重试 / 研究 / 测试", "label": "整轮催重搜总预算",
     "hint": "达上限→停搜、用现有内容综合作答", "type": "int", "min": 1, "max": 20},
    {"key": "agent.research_refine_max", "group": "重试 / 研究 / 测试", "label": "同 query 催重搜上限",
     "hint": "防同词无限重搜", "type": "int", "min": 1, "max": 10},
    {"key": "agent.test_max_iters", "group": "重试 / 研究 / 测试", "label": "自动测试修复轮数",
     "hint": "auto_test 失败后自动迭代修复的最多轮数", "type": "int", "min": 1, "max": 10},
    {"key": "agent.deadend_threshold", "group": "重试 / 研究 / 测试", "label": "死路提示阈值",
     "hint": "同一条路累计非瞬时失败 ≥ 此值 → 提示换思路", "type": "int", "min": 1, "max": 10},
    # 联网 / MCP 超时
    {"key": "web.timeout", "group": "联网 / MCP", "label": "web 请求超时（秒）",
     "hint": "单次 web_search/web_fetch 请求超时", "type": "int", "min": 5, "max": 300},
    {"key": "web.fetch_max_chars", "group": "联网 / MCP", "label": "web_fetch 正文上限（字符）",
     "hint": "抓取正文默认上限，模型可临时调大", "type": "int", "min": 1000, "max": 200000},
    {"key": "mcp.connect_timeout", "group": "联网 / MCP", "label": "MCP 连接超时（秒）",
     "hint": "server 启动+握手+列工具超时", "type": "int", "min": 5, "max": 600},
    {"key": "mcp.call_timeout", "group": "联网 / MCP", "label": "MCP 调用超时（秒）",
     "hint": "单次 MCP 工具调用超时", "type": "int", "min": 5, "max": 600},
    # 多模态输入
    {"key": "multimodal.max_image_mb", "group": "多模态输入", "label": "单图大小上限（MB）",
     "type": "int", "min": 1, "max": 100},
    {"key": "multimodal.max_doc_chars", "group": "多模态输入", "label": "单文档字符上限",
     "type": "int", "min": 1000, "max": 2000000},
    {"key": "multimodal.max_attachments", "group": "多模态输入", "label": "单条消息附件数上限",
     "type": "int", "min": 1, "max": 100},
)
_LIMITS_BY_KEY = {s["key"]: s for s in LIMITS_SPEC}


def _coerce_limit(spec: dict, v):
    """按 spec 把值转成 int/float 并夹在 [min,max]；失败返回 None（跳过该项）。"""
    try:
        num = int(v) if spec["type"] == "int" else float(v)
    except (TypeError, ValueError):
        return None
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None:
        num = max(lo, num)
    if hi is not None:
        num = min(hi, num)
    return num


def read_limits(path: "Path | None" = None) -> dict:
    """读 GUI「限额与预算」保存值（点分 key→数值）。不存在/坏档返回 {}。"""
    p = path or (APP_DIR / LIMITS_FILE)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def set_limits(updates: dict, path: "Path | None" = None) -> dict:
    """合并写入限额（只收 LIMITS_SPEC 白名单 key、按类型/范围校验），返回合并后全量。"""
    p = path or (APP_DIR / LIMITS_FILE)
    cur = read_limits(p)
    for k, v in (updates or {}).items():
        spec = _LIMITS_BY_KEY.get(k)
        if spec is None:
            continue
        num = _coerce_limit(spec, v)
        if num is not None:
            cur[k] = num
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    return cur


def merge_limits(data: dict, path: "Path | None" = None) -> dict:
    """把 GUI 保存的限额覆盖到对应 config 段（load_config 用）。key='section.field'。"""
    saved = read_limits(path)
    if not saved:
        return data
    for k, v in saved.items():
        spec = _LIMITS_BY_KEY.get(k)
        if spec is None:
            continue
        num = _coerce_limit(spec, v)
        if num is None:
            continue
        section, _, field = k.partition(".")
        if not field:
            continue
        sec = dict(data.get(section) or {})
        sec[field] = num
        data[section] = sec
    return data


def merge_browser_mcp(data: dict) -> dict:
    """若 GUI 启用了浏览器穿透，把 Playwright MCP server 合并进 data['mcp']（不动用户手编的其它 server）。"""
    if not browser_mcp_enabled():
        return data
    m = dict(data.get("mcp") or {})
    m["enabled"] = True
    servers = dict(m.get("servers") or {})
    mcp_args = browser_mcp_args(browser_mcp_headed())  # 有头登录态 / 无头，据状态构造
    # Windows 下 npx 实际是 npx.cmd，MCP 子进程必须经 cmd /c 启动，否则 FileNotFoundError / 非 Win32 程序
    if os.name == "nt":
        servers["browser"] = {"command": "cmd", "args": ["/c", "npx", *mcp_args], "trust": True}
    else:
        servers["browser"] = {"command": "npx", "args": list(mcp_args), "trust": True}
    m["servers"] = servers
    data["mcp"] = m
    return data


# ── 用户在「统一管理面」加的 MCP server（Tier2-①）──────────────────────────
# 运行时覆盖文件（仿 feature_flags.json / mcp_browser.json）：GUI 增删改写它，load_config 合并进
# data['mcp']['servers']。与 config.yaml 手编的 server 互不干扰、与穿透托管的 browser 也分开。
USER_MCP_FILE = "user_mcp.json"
_WIN_SHIM_CMDS = {"npx", "npm", "pnpm", "yarn", "pnpx"}  # Windows 下是 .cmd 垫片，子进程须经 cmd /c 启动


def read_user_mcp(path: "Path | None" = None) -> dict:
    """读用户加的 MCP server（name -> spec）。不存在/坏档返回 {}。"""
    p = path or (APP_DIR / USER_MCP_FILE)
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_user_mcp(servers: dict, path: "Path | None" = None) -> None:
    p = path or (APP_DIR / USER_MCP_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(servers or {}, ensure_ascii=False, indent=2), encoding="utf-8")


def set_user_mcp_server(name: str, spec: dict, path: "Path | None" = None) -> dict:
    """新增/改一个 MCP server（spec: command/args/env/trust/enabled）。返回全量 servers。"""
    name = (name or "").strip()
    servers = read_user_mcp(path)
    if name:
        servers[name] = {
            "command": str((spec or {}).get("command") or "").strip(),
            "args": [str(a) for a in ((spec or {}).get("args") or [])],
            "env": {str(k): str(v) for k, v in ((spec or {}).get("env") or {}).items()},
            "trust": bool((spec or {}).get("trust", False)),
            "enabled": bool((spec or {}).get("enabled", True)),
        }
        write_user_mcp(servers, path)
    return servers


def remove_user_mcp_server(name: str, path: "Path | None" = None) -> dict:
    servers = read_user_mcp(path)
    servers.pop(name, None)
    write_user_mcp(servers, path)
    return servers


def _apply_user_mcp(data: dict, user: dict) -> dict:
    """纯逻辑：把 user servers 合并进 data['mcp']（便于单测）。只挂 enabled 且 command 非空的；
    有任一启用就把 mcp.enabled 设 True（省得用户单独开 MCP 总开关）；Windows 下 npx 类命令包 cmd /c。"""
    if not user:
        return data
    m = dict(data.get("mcp") or {})
    servers = dict(m.get("servers") or {})
    any_on = False
    for name, spec in user.items():
        if not isinstance(spec, dict) or not spec.get("enabled", True) or not spec.get("command"):
            continue
        command, args = spec["command"], list(spec.get("args") or [])
        if os.name == "nt" and command in _WIN_SHIM_CMDS:
            command, args = "cmd", ["/c", spec["command"], *args]
        servers[name] = {"command": command, "args": args,
                         "env": dict(spec.get("env") or {}), "trust": bool(spec.get("trust", False))}
        any_on = True
    if any_on:
        m["enabled"] = True
    m["servers"] = servers
    data["mcp"] = m
    return data


def merge_user_mcp(data: dict) -> dict:
    """把用户在设置面板加的 MCP server 合并进 data['mcp']（不动 config.yaml 手编的 server）。"""
    return _apply_user_mcp(data, read_user_mcp())


# ── 用户在「统一管理面」加的 hooks（Tier2-①）──────────────────────────────
# 运行时覆盖文件：GUI 增删改写它，load_config 把 enabled 的追加进 data['agent']['hooks']。
# 与 config.yaml 手编的 hooks 共存（都生效）。
USER_HOOKS_FILE = "user_hooks.json"
_HOOK_EVENTS = ("PreToolUse", "PostToolUse")


def read_user_hooks(path: "Path | None" = None) -> list:
    """读用户加的 hooks（list）。不存在/坏档返回 []。"""
    p = path or (APP_DIR / USER_HOOKS_FILE)
    if not p.is_file():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:  # noqa: BLE001
        return []


def write_user_hooks(hooks: list, path: "Path | None" = None) -> None:
    p = path or (APP_DIR / USER_HOOKS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(hooks or [], ensure_ascii=False, indent=2), encoding="utf-8")


def _norm_hook(spec: dict) -> dict:
    spec = spec or {}
    event = str(spec.get("event") or "PreToolUse")
    if event not in _HOOK_EVENTS:
        event = "PreToolUse"
    try:
        timeout = max(1, int(spec.get("timeout") or 15))
    except (TypeError, ValueError):
        timeout = 15
    return {"event": event, "command": str(spec.get("command") or "").strip(),
            "matcher": str(spec.get("matcher") or "").strip(),
            "name": str(spec.get("name") or "").strip(),
            "timeout": timeout, "enabled": bool(spec.get("enabled", True))}


def upsert_user_hook(index: "int | None", spec: dict, path: "Path | None" = None) -> list:
    """新增（index 越界/None）或改（合法 index）一个 hook。返回全量 list。"""
    hooks = read_user_hooks(path)
    h = _norm_hook(spec)
    if index is None or index < 0 or index >= len(hooks):
        hooks.append(h)
    else:
        hooks[index] = h
    write_user_hooks(hooks, path)
    return hooks


def remove_user_hook(index: int, path: "Path | None" = None) -> list:
    hooks = read_user_hooks(path)
    if 0 <= index < len(hooks):
        hooks.pop(index)
        write_user_hooks(hooks, path)
    return hooks


def _apply_user_hooks(data: dict, user: list) -> dict:
    """纯逻辑：把 enabled 且 command 非空的 user hooks 追加进 data['agent']['hooks']（便于单测）。
    剥掉 enabled 字段（HookConfig 没有），只传 event/command/matcher/name/timeout。"""
    if not user:
        return data
    agent = dict(data.get("agent") or {})
    hooks = list(agent.get("hooks") or [])
    for h in user:
        if not isinstance(h, dict) or not h.get("enabled", True) or not h.get("command"):
            continue
        hooks.append({k: h[k] for k in ("event", "command", "matcher", "name", "timeout") if k in h})
    agent["hooks"] = hooks
    data["agent"] = agent
    return data


def merge_user_hooks(data: dict) -> dict:
    """把用户在面板加的 hooks 合并进 data['agent']['hooks']（不动 config.yaml 手编的）。"""
    return _apply_user_hooks(data, read_user_hooks())


def effective_user_providers(path: "Path | None" = None) -> dict:
    """供「展开档案 load_config」与「设置面板 get_providers」**共用**的有效配置：以 DEFAULT_PROVIDERS
    为基底、用户 providers.yaml 按 key 覆盖。这样①开箱即用（火山方舟默认启用+勾 kimi）；②UI 勾选状态与
    实际展开一致；③即使用户配过别的 provider、写出了 providers.yaml，**没单独配过的默认 provider 也不会
    丢**（之前的 bug：文件一存在就整体替换默认 → 火山方舟消失）。"""
    out = {k: dict(v) for k, v in DEFAULT_PROVIDERS.items()}
    for k, v in load_user_providers(path).items():
        out[k] = v          # 文件里配过的 key 整体覆盖默认（含 disable / 改模型）
    return out


def load_config(config_path: Path | None = None) -> AppConfig:
    """加载 .env 与 config.yaml，返回 AppConfig。

    打包后：.env / config.yaml 在 exe 旁边（APP_DIR）。首次运行若没有 config.yaml，
    自动从内置默认（BUNDLE_DIR）拷一份出来供用户编辑。
    """
    load_dotenv(APP_DIR / ".env")
    path = config_path or (APP_DIR / "config.yaml")
    if not path.exists():
        # 首次运行：从内置模板释放一份用户可编辑的 config.yaml。优先 config.default.yaml——
        # 分发包只带 config.default.yaml（不带 config.yaml），这样**更新解压不会覆盖用户改过的
        # config.yaml**（之前的坑：包里直接带 config.yaml，每次更新把用户开的 MCP/browser 等改动冲掉）。
        import shutil
        for tmpl in (BUNDLE_DIR / "config.default.yaml", BUNDLE_DIR / "config.yaml"):
            if tmpl.exists() and tmpl.resolve() != path.resolve():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(tmpl, path)
                break
    if not path.exists():
        raise FileNotFoundError(f"找不到配置文件：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    user_models = load_user_models()
    if user_models:  # 用户在设置面板加/改的档案，与内置合并（用户档案覆盖同名内置）
        data["models"] = merge_models(data.get("models", {}), user_models)
    # provider 中心（产品化③）：启用的 provider × 勾选的模型 展开成档案，合并进 models。
    # 用 effective_user_providers（含开箱默认），与设置面板 get_providers 同源、勾选状态一致。
    provider_models = expand_provider_profiles(PROVIDER_PRESETS, effective_user_providers())
    if provider_models:
        data["models"] = {**data.get("models", {}), **provider_models}
    data = merge_user_mcp(data)     # 「统一管理面」加的 MCP server（Tier2-①）
    data = merge_user_hooks(data)   # 「统一管理面」加的 hooks（Tier2-①）
    data = merge_browser_mcp(data)  # GUI 一键开关启用的浏览器穿透（Playwright MCP；穿透 browser 优先）
    data = merge_feature_flags(data)  # GUI「功能开关」面板保存的 agent 开关（覆盖 config.yaml 默认）
    data = merge_limits(data)         # GUI「限额与预算」面板保存的数值参数（覆盖各段默认）
    _resolve_shell(data)              # shell: auto / 缺省 → 按系统选（Windows→powershell，macOS/Linux→bash）
    return AppConfig(**data)


def default_shell() -> str:
    """按当前系统选默认 shell：Windows→powershell，macOS/Linux→bash（POSIX 通用、可移植）。"""
    return "powershell" if os.name == "nt" else "bash"


def _resolve_shell(data: dict) -> None:
    """把 agent.shell 的 `auto`（或缺省）就地解析成当前系统对应的 shell，让同一份 config 跨平台可用。"""
    agent = data.setdefault("agent", {})
    sh = (agent.get("shell") or "auto").strip().lower()
    if sh in ("", "auto"):
        agent["shell"] = default_shell()


def read_project_config(workspace) -> dict:
    """读工作区根的 `.hermes.yaml`（项目级配置，按项目覆盖全局，目前用于 test_command）。
    无文件 / 解析失败返回 {}（回退全局）。"""
    p = Path(workspace) / ".hermes.yaml"
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — 配置坏不致命，回退全局
        return {}


def persist_model_selection(active: "str | None" = None,
                            subagent: "str | None" = None,
                            update_subagent: bool = False,
                            path: "Path | None" = None) -> None:
    """把主模型 / 子任务模型选择写回 config.yaml（按行替换，保留注释、system_prompt 等其余内容；
    不整文件 yaml.dump——那会丢掉大量注释与多行 system_prompt）。

    active：非空则更新顶层 `active_model:` 行。
    update_subagent=True：更新 `agent.subagent_model` 行；subagent=None 写成注释（= 跟随主模型）。
    找不到对应行则跳过（内存已生效，只是这次没持久化）。
    """
    path = path or (APP_DIR / "config.yaml")
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if active:
        text = re.sub(r"(?m)^active_model:.*$", f"active_model: {active}", text, count=1)
    if update_subagent:
        if subagent:
            repl = rf"\1subagent_model: {subagent}"
        else:
            repl = r"\1# subagent_model:   # 省略 = 委派子任务用当前对话模型"
        text = re.sub(r"(?m)^(\s*)#?\s*subagent_model:.*$", repl, text, count=1)
    path.write_text(text, encoding="utf-8")


def persist_design_review_models(mapping: dict, path: "Path | None" = None) -> None:
    """把评审异构模型映射写回 config.yaml 的 `design_review_models:` 行（inline flow，保留其余注释）。"""
    path = path or (APP_DIR / "config.yaml")
    if not path.exists():
        return
    clean = {k: v for k, v in (mapping or {}).items() if v}
    flow = "{" + ", ".join(f'{k}: "{v}"' for k, v in clean.items()) + "}" if clean else "{}"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^(\s*)design_review_models:.*$", rf"\g<1>design_review_models: {flow}",
                  text, count=1)
    path.write_text(text, encoding="utf-8")


# ---- API key 配置（产品化：设置面板填 key 写回 .env，不把真实 key 内置进包）----------

def collect_key_requirements(models) -> "list[dict]":
    """从模型档案收集需要的 API key 环境变量名，去重并关联用到它的模型，按 env 名排序。
    返回 [{"env": "ARK_API_KEY", "models": ["ark-kimi", ...]}]，供设置面板列出「要填哪些 key、
    各被哪些模型用」。models 的值可为 ModelConfig 或等价 dict。"""
    by_env: dict = {}
    for name, mc in (models or {}).items():
        env = mc.get("api_key_env", "") if isinstance(mc, dict) else getattr(mc, "api_key_env", "")
        if not env:
            continue
        by_env.setdefault(env, []).append(name)
    return [{"env": e, "models": sorted(ms)} for e, ms in sorted(by_env.items())]


def upsert_env_line(env_text: str, key: str, value: str) -> str:
    """把 `key=value` 写进 .env 文本：已有该 key 的非注释行就替换，否则末尾追加；
    其它行（注释、别的 key）原样保留。value 原样写、不加引号。纯函数，便于单测。"""
    lines = (env_text or "").splitlines()
    found = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        if name == key:
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def mask_key(value: str) -> str:
    """把 key 掩码成预览（设置面板显示用，绝不回传明文）：保留首尾各 4 位、中间省略。"""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "•" * len(v)
    return v[:4] + "…" + v[-4:]
