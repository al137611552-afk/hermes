"""暴露给前端 (window.pywebview.api) 的接口 —— 对话管理器（FR-8.1）。

前端调用 send_message() 后，Python 同步跑 agent 循环（可能含多步工具调用），
每产生一个事件就通过 window.evaluate_js 推回前端 -> 流式渲染。危险工具执行前经
PermissionGate 阻塞等待前端确认（resolve_permission）。

本类现在是**对话管理器**：持有跨对话共享资源（config/store/memory/mcp/...）与一个
「当前活动对话」`active`（Conversation）。每对话的私有状态与逻辑都在 Conversation 里。
公开方法（前端 js_api 调用面）转发到活动对话；会话切换 = 替换 active。

本阶段（8.1）保持「单活动对话、同步执行」语义，对外行为与 1.0.0 一致；后台并发与
事件按 conv_id 路由留到 8.2。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path

from ..config import (
    APP_DIR, PROVIDER_PRESETS, AppConfig, ModelConfig, collect_key_requirements,
    effective_user_providers, load_config, load_user_models, load_user_providers, mask_key,
    persist_model_selection, save_user_models, save_user_providers, upsert_env_line,
)
from ..mcp_client import McpManager
from ..multimodal import Limits
from ..providers import Message
from ..store import MemoryStore, Store
from .conversation import Conversation, Resources


class Api:
    def __init__(self, config: AppConfig, emit=None) -> None:
        self.config = config
        self._window = None  # 由 app.py 注入
        # 无头入口（FR-11.7 CLI）可注入 emit(event, data, cid) 钩子，替代 evaluate_js 推事件
        self._emit_hook = emit
        self._emit_lock = threading.Lock()  # 串行化 evaluate_js（多对话 worker 并发调用）
        self._cid_counter = 0               # 进程内对话 id 计数器
        self.conversations: dict[int, Conversation] = {}  # cid -> 活动运行时（含后台运行中的）
        self._pending_ws_renames: dict[int, str] = {}  # sid->title：运行中/crazy 改标题被跳过，空闲后自动补改文件夹名
        # 当前下拉选中的模型（新会话的默认模型）；与活动对话的 active_model 保持同步
        self.active_model = config.active_model

        # 长期记忆（P6.3）：独立 SQLite 库，跨会话/重启持久
        memory = (
            MemoryStore(config.memory.resolve_db_path()) if config.memory.enabled else None
        )

        # MCP 工具接入（P6.4）：连接外部 server，把其工具收进来（失败不影响启动）
        mcp = McpManager(config.mcp)
        try:
            mcp_tools = mcp.start()
        except Exception as e:  # noqa: BLE001 — 连接问题绝不拖垮启动
            print(f"[MCP] 初始化异常，已忽略：{type(e).__name__}: {e}", file=sys.stderr)
            mcp_tools = []

        # 工作区（按会话隔离）：设了显式 agent.workspace 则固定用它、关闭隔离；
        # 否则每个会话用 workspaces_root/<id>/ 独立文件夹（避免不同项目互相污染）。
        ac = config.agent
        per_session = ac.per_session_workspace and not ac.workspace
        workspaces_root = ac.resolve_workspaces_root()
        if per_session:
            workspaces_root.mkdir(parents=True, exist_ok=True)

        # 多模态附件大小/数量限制
        mm = config.multimodal
        limits = Limits(
            max_image_bytes=mm.max_image_mb * 1024 * 1024,
            max_doc_chars=mm.max_doc_chars,
            max_attachments=mm.max_attachments,
        )
        # 会话持久化（P6.1）
        store = (
            Store(
                config.storage.resolve_db_path(),
                externalize_images=config.storage.externalize_images,
            )
            if config.storage.enabled
            else None
        )

        # 跨对话共享资源（注入给各 Conversation）
        self.res = Resources(
            config=config, memory=memory, mcp=mcp, mcp_tools=mcp_tools, store=store,
            limits=limits, workspaces_root=workspaces_root, per_session=per_session,
            emit=(emit or self._emit),
        )
        # 当前活动对话：起始为一个空白草稿（未落库）
        self.active: Conversation = self._make_conversation(None, [], None)

    # ---- 对话工厂 / 工作区初值 -------------------------------------------

    def _initial_workspace(self, session_id: int | None, pending_workspace: str | None) -> Path:
        ac = self.config.agent
        if not self.res.per_session:
            return ac.resolve_workspace()  # 固定工作区
        if pending_workspace:
            return Path(pending_workspace)  # 打开的已有项目
        if session_id is not None:  # 已落库会话：绑定路径优先，否则默认隔离文件夹
            bound = self.res.store.get_session_workspace(session_id) if self.res.store else None
            return Path(bound) if bound else (self.res.workspaces_root / str(session_id))
        return self.res.workspaces_root / "_scratch"  # 草稿暂存区

    def _make_conversation(
        self, session_id: int | None, history: list[Message], pending_workspace: str | None
    ) -> Conversation:
        self._cid_counter += 1
        # 已有会话：优先用它绑定的模型（每会话可不同、跨重载存活）；新草稿用全局默认
        model = self.active_model
        if session_id is not None and self.res.store:
            stored = self.res.store.get_session_model(session_id)
            if stored and stored in self.config.models:
                model = stored
        conv = Conversation(
            self.res,
            cid=self._cid_counter,
            session_id=session_id,
            history=history,
            workspace=self._initial_workspace(session_id, pending_workspace),
            pending_workspace=pending_workspace,
            active_model=model,
        )
        self.conversations[conv.cid] = conv  # 登记到注册表（后台运行中也保活）
        return conv

    def _leave(self, old: Conversation, *, capture: bool) -> None:
        """离开某对话：按需抽取记忆；若是没内容、空闲的草稿则从注册表丢弃（防堆积）。"""
        if capture:
            old.capture_async()
        if (old is not self.active and old.session_id is None
                and not old.history and not old.is_busy()):
            self.conversations.pop(old.cid, None)

    def _emit_workspace_changed(self) -> None:
        self._emit("workspace_changed",
                   {"root": str(self.active.workspace), "label": self.active.workspace_label()},
                   self.active.cid)

    # ---- 模型选择 --------------------------------------------------------

    def get_models(self) -> dict:
        """返回模型列表与当前主/子任务选中项，供下拉框渲染。"""
        return {"models": list(self.config.models.keys()),
                "active": self.active_model,
                "subagent": self.config.agent.subagent_model}  # None = 委派跟随主模型

    def set_active_model(self, name: str) -> dict:
        if name not in self.config.models:
            return {"ok": False, "error": f"未知模型 {name}"}
        self.active_model = name
        self.active.active_model = name  # 同步当前对话
        # 该会话已落库 -> 把模型绑定到这个会话（每会话各自的模型、跨重载存活）
        if self.active.session_id is not None and self.res.store:
            try:
                self.res.store.set_session_model(self.active.session_id, name)
            except Exception:  # noqa: BLE001
                pass
        persist_model_selection(active=name)  # 存回 config.yaml（新会话默认），重启保留
        return {"ok": True, "active": name}

    def set_subagent_model(self, name: str) -> dict:
        """设置委派子任务用的模型档案；空串/None = 跟随主模型。
        内存即时生效（委派时读 cfg.agent.subagent_model）+ 持久化到 config.yaml。"""
        sub = (name or "").strip() or None
        if sub is not None and sub not in self.config.models:
            return {"ok": False, "error": f"未知模型 {sub}"}
        self.config.agent.subagent_model = sub
        persist_model_selection(subagent=sub, update_subagent=True)
        return {"ok": True, "subagent": sub}

    # ---- API key 配置（产品化：设置面板填 key 写回 .env，不把真实 key 内置进包）----
    def get_api_key_status(self) -> dict:
        """列出所有模型档案需要的 API key：env 名、用它的模型、是否已配置、掩码预览（不回传明文）。"""
        out = []
        for r in collect_key_requirements(self.config.models):
            val = os.getenv(r["env"], "").strip()
            out.append({"env": r["env"], "models": r["models"],
                        "set": bool(val), "preview": mask_key(val)})
        return {"ok": True, "keys": out}

    def set_api_key(self, env_name: str, value: str) -> dict:
        """把一个 API key 写回 exe 旁的 .env 并即时生效（更新 os.environ，无需重启）；
        value 为空串 = 清除该 key。"""
        env_name = (env_name or "").strip()
        if not env_name:
            return {"ok": False, "error": "环境变量名为空"}
        value = (value or "").strip()
        p = APP_DIR / ".env"
        try:
            text = p.read_text(encoding="utf-8") if p.exists() else ""
            p.write_text(upsert_env_line(text, env_name, value), encoding="utf-8")
        except OSError as e:  # noqa: BLE001
            return {"ok": False, "error": f"写入 .env 失败：{e}"}
        os.environ[env_name] = value  # 即时生效，无需重启
        return {"ok": True, "env": env_name, "set": bool(value), "preview": mask_key(value)}

    # ---- 模型档案管理（产品化②：GUI 增删改模型，不碰 config.yaml 注释）------
    def get_model_profiles(self) -> dict:
        """列出所有模型档案及关键字段，标记内置 / 用户（用户档案可改可删）。"""
        user = load_user_models()
        profiles = []
        for name, mc in self.config.models.items():
            profiles.append({
                "name": name, "provider": mc.provider, "model": mc.model,
                "api_key_env": mc.api_key_env, "base_url": mc.base_url or "",
                "max_tokens": mc.max_tokens, "vision": mc.vision,
                "builtin": name not in user,
            })
        return {"ok": True, "profiles": profiles, "active": self.active_model}

    def upsert_model_profile(self, name: str, profile: dict) -> dict:
        """加 / 改一个用户模型档案：校验 → 写 user_models.yaml → 重载合并后的 models 即时生效。
        与内置同名 = 覆盖（用户档案优先）。"""
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "档案名不能为空"}
        prof = dict(profile or {})
        clean = {
            "provider": (prof.get("provider") or "anthropic").strip(),
            "model": (prof.get("model") or "").strip(),
            "api_key_env": (prof.get("api_key_env") or "").strip(),
            "vision": bool(prof.get("vision")),
        }
        bu = (prof.get("base_url") or "").strip()
        if bu:
            clean["base_url"] = bu
        try:
            clean["max_tokens"] = int(prof.get("max_tokens") or 4096)
        except (TypeError, ValueError):
            return {"ok": False, "error": "max_tokens 必须是整数"}
        if not clean["model"] or not clean["api_key_env"]:
            return {"ok": False, "error": "model 与 api_key_env 不能为空"}
        try:
            ModelConfig(**clean)  # 校验合法（provider 枚举等）
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"档案无效：{e}"}
        user = load_user_models()
        user[name] = clean
        save_user_models(user)
        self.config.models = load_config().models  # 重载合并后的 models，即时生效
        return {"ok": True, "name": name}

    def delete_model_profile(self, name: str) -> dict:
        """删一个用户模型档案（内置档案不可删）；删的若是当前主模型则回退到任一可用档案。"""
        name = (name or "").strip()
        user = load_user_models()
        if name not in user:
            return {"ok": False, "error": "内置档案不可删（只有设置面板加的才能删）"}
        del user[name]
        save_user_models(user)
        self.config.models = load_config().models
        if self.active_model not in self.config.models:
            self.active_model = next(iter(self.config.models), self.active_model)
            self.active.active_model = self.active_model
        return {"ok": True, "active": self.active_model}

    # ---- Provider 中心配置（产品化③：provider 配一次 key/url/格式、下挂模型）------
    def get_providers(self) -> dict:
        """列出所有 provider（预设 + 自定义）及状态：启用 / key 是否已配 / base_url / 模型清单。"""
        user = effective_user_providers()  # 含开箱默认，与 load_config 同源、勾选状态一致
        keys = list(PROVIDER_PRESETS.keys()) + [k for k in user if k not in PROVIDER_PRESETS]
        out = []
        for key in keys:
            preset = PROVIDER_PRESETS.get(key, {})
            uc = user.get(key, {})
            api_key_env = uc.get("api_key_env") or preset.get("api_key_env", "")
            catalog = [m["id"] for m in preset.get("models", []) if isinstance(m, dict)]
            for mid in (uc.get("custom_models") or []):
                if mid not in catalog:
                    catalog.append(mid)
            out.append({
                "key": key,
                "label": uc.get("label") or preset.get("label") or key,
                "provider": uc.get("provider") or preset.get("provider", "anthropic"),
                "api_key_env": api_key_env,
                "base_url": uc.get("base_url") or preset.get("base_url", ""),
                "enabled": bool(uc.get("enabled", False)),
                "key_set": bool(os.getenv(api_key_env, "").strip()) if api_key_env else False,
                "key_preview": mask_key(os.getenv(api_key_env, "")) if api_key_env else "",
                "models": catalog,
                "enabled_models": uc.get("models") if uc.get("models") is not None else catalog,
                "custom_models": uc.get("custom_models") or [],
                "builtin": key in PROVIDER_PRESETS,
            })
        return {"ok": True, "providers": out, "active": self.active_model}

    def save_provider(self, key: str, config: dict) -> dict:
        """保存一个 provider 配置（enabled / base_url 覆盖 / 启用模型 / 自定义模型 / 自定义 provider
        字段）→ providers.yaml + 重载 models 即时生效。API key 单独走 set_api_key 存 .env。"""
        key = (key or "").strip()
        if not key:
            return {"ok": False, "error": "provider 标识为空"}
        cfg = dict(config or {})
        user = load_user_providers()
        # 合并基底：该 key 文件里配过就用文件，否则用有效配置（含内置默认的 enabled 等）——
        # 否则首次只改个模型勾选会丢掉默认 enabled，整个 provider 被禁用、模型从下拉消失（真机 bug）。
        cur = dict(user.get(key) or effective_user_providers().get(key) or {})
        for f in ("enabled", "base_url", "models", "custom_models", "label", "provider", "api_key_env"):
            if f in cfg:
                cur[f] = cfg[f]
        user[key] = cur
        save_user_providers(user)
        self.config.models = load_config().models  # 重载（含 provider 展开），即时生效
        if self.active_model not in self.config.models and self.config.models:
            self.active_model = next(iter(self.config.models))
            self.active.active_model = self.active_model
        return {"ok": True, "active": self.active_model}

    def test_provider(self, key: str) -> dict:
        """发一个最小请求测该 provider 的 key/url 是否可用（设置面板「测试连接」）。"""
        from ..config import ModelConfig
        from ..providers import Message, build_provider
        user = load_user_providers()
        preset = PROVIDER_PRESETS.get(key, {})
        uc = user.get(key, {})
        api_key_env = uc.get("api_key_env") or preset.get("api_key_env", "")
        if not os.getenv(api_key_env, "").strip():
            return {"ok": False, "error": "未配置 API Key"}
        provider = uc.get("provider") or preset.get("provider", "anthropic")
        base_url = uc.get("base_url") or preset.get("base_url") or None
        catalog = [m["id"] for m in preset.get("models", []) if isinstance(m, dict)] + list(uc.get("custom_models") or [])
        enabled = uc.get("models")
        model_id = (enabled[0] if enabled else (catalog[0] if catalog else None))
        if not model_id:
            return {"ok": False, "error": "该服务没有可测试的模型，先添加一个"}
        pn = f"__test__/{key}"
        self.config.models[pn] = ModelConfig(provider=provider, model=model_id,
                                             api_key_env=api_key_env, base_url=base_url, max_tokens=16)
        try:
            prov = build_provider(self.config, pn)
            for ev in prov.stream_chat([Message("user", "hi")], system="reply ok"):
                if ev.type == "error":
                    return {"ok": False, "error": ev.text[:200]}
                if ev.type == "done":
                    break
            return {"ok": True, "model": model_id}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:200]}
        finally:
            self.config.models.pop(pn, None)

    def fetch_provider_models(self, key: str) -> dict:
        """拉该 provider 的可用模型 ID。OpenAI 兼容用 GET /models（Bearer）；Anthropic 用
        GET /v1/models（x-api-key + anthropic-version）。两者响应都是 {data:[{id,...}]}。
        自定义端点（如火山方舟 coding）可能不支持列模型 → 优雅返回提示、让用户手动添加。"""
        import json
        import urllib.request
        user = load_user_providers()
        preset = PROVIDER_PRESETS.get(key, {})
        uc = user.get(key, {})
        api_key_env = uc.get("api_key_env") or preset.get("api_key_env", "")
        provider = uc.get("provider") or preset.get("provider", "anthropic")
        base_url = (uc.get("base_url") or preset.get("base_url") or "").rstrip("/")
        key_val = os.getenv(api_key_env, "").strip()
        if not key_val:
            return {"ok": False, "error": "未配置 API Key"}
        if provider == "openai":
            if not base_url:
                return {"ok": False, "error": "未配置 Base URL"}
            url, headers = base_url + "/models", {"Authorization": f"Bearer {key_val}"}
        else:  # anthropic
            root = base_url or "https://api.anthropic.com"
            url = root + ("/models" if root.endswith("/v1") else "/v1/models")
            headers = {"x-api-key": key_val, "anthropic-version": "2023-06-01"}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            ids = sorted({m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")})
            return {"ok": True, "models": ids} if ids else {"ok": False, "error": "该服务未返回模型列表，请手动添加"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"获取失败（该服务可能不支持列模型，可手动添加）：{str(e)[:110]}"}

    def delete_provider(self, key: str) -> dict:
        """删一个自定义 provider（内置预设不可删，只能关「启用」）→ providers.yaml 移除 + 重载。"""
        key = (key or "").strip()
        if key in PROVIDER_PRESETS:
            return {"ok": False, "error": "内置预设不可删（可关掉「启用」开关）"}
        user = load_user_providers()
        if key not in user:
            return {"ok": False, "error": "该服务不存在"}
        del user[key]
        save_user_providers(user)
        self.config.models = load_config().models
        if self.active_model not in self.config.models and self.config.models:
            self.active_model = next(iter(self.config.models))
            self.active.active_model = self.active_model
        return {"ok": True, "active": self.active_model}

    # ---- 浏览器穿透（Playwright MCP）一键开关（深度调研用）----------------------
    def get_browser_mcp_status(self) -> dict:
        """状态：是否启用 / Node 是否可用 / 是否已连上 / 浏览器工具数。"""
        import shutil
        from ..config import browser_mcp_enabled
        from ..config import browser_mcp_headed
        bt = [t for t in (self.res.mcp_tools or []) if t.name.split("__", 1)[0] == "browser"]
        return {"ok": True, "enabled": browser_mcp_enabled(), "headed": browser_mcp_headed(),
                "node": bool(shutil.which("npx") or shutil.which("node")),
                "connected": bool(bt), "tools": len(bt)}

    def get_feature_flags(self) -> dict:
        """GUI「功能开关」面板：返回当前生效的可切换 agent 开关。

        auto_affected_test 取**有效值**＝config/面板显式开 或 当前会话的情境智能默认（情境自启②）；
        并回 auto_affected_test_smart 标记是否由智能默认开启，供前端显示「（自动）」。
        """
        ac = self.config.agent
        smart = bool(getattr(self.active, "_smart_defaults", {}).get("auto_affected_test")) \
            if getattr(self, "active", None) else False
        return {"ok": True,
                "auto_affected_test": ac.auto_affected_test or smart,
                "auto_affected_test_smart": smart and not ac.auto_affected_test,
                "affected_test_runner": ac.affected_test_runner,
                "auto_review": ac.auto_review,
                "auto_test": ac.auto_test,
                "test_command": ac.test_command or "",
                "delegate_max_revisions": ac.delegate_max_revisions,
                "auto_approve_safe": ac.auto_approve_safe}

    def set_feature_flags(self, updates: dict) -> dict:
        """GUI 面板改开关：即时更新活动 config（所有对话共享同一 config 引用、下一步即生效）
        + 持久化到 feature_flags.json（重启仍在）。"""
        from ..config import set_feature_flags as persist_flags
        ac = self.config.agent
        updates = updates or {}
        # 即时生效：直接改活动 config（auto_review/auto_test 每轮现读、auto_affected_test 由现读闭包读）
        if "auto_affected_test" in updates:
            ac.auto_affected_test = bool(updates["auto_affected_test"])
        if "auto_review" in updates:
            ac.auto_review = bool(updates["auto_review"])
        if "auto_test" in updates:
            ac.auto_test = bool(updates["auto_test"])
        if "affected_test_runner" in updates:
            ac.affected_test_runner = str(updates["affected_test_runner"] or "auto")
        if "test_command" in updates:
            ac.test_command = str(updates["test_command"] or "")
        if "delegate_max_revisions" in updates:
            try:
                ac.delegate_max_revisions = max(0, int(updates["delegate_max_revisions"]))
            except (TypeError, ValueError):
                pass
        if "auto_approve_safe" in updates:
            ac.auto_approve_safe = bool(updates["auto_approve_safe"])
        persist_flags(updates)  # 持久化（只取白名单键）
        return self.get_feature_flags()

    def set_browser_headed(self, headed: bool) -> dict:
        """切换浏览器穿透的「有头·登录态」模式：有头=弹出可见浏览器供手动登录/划滑块，登录态持久复用。
        改完重连 MCP（重启 server 让新参数生效）；仅在已启用穿透时有意义。"""
        from ..config import browser_mcp_enabled, set_browser_mcp_state
        headed = bool(headed)
        set_browser_mcp_state(browser_mcp_enabled(), headed=headed)
        tools = self._reconnect_mcp() if browser_mcp_enabled() else 0
        return {"ok": True, "headed": headed, "tools": tools}

    def set_browser_mcp(self, on: bool) -> dict:
        """一键启用/关闭浏览器穿透。关闭：同步、即时。启用：先查 Node，再**后台**装浏览器
        （边下边通过 browser_mcp_progress 事件推进度），立即返回 {status:installing}；装好/失败由
        browser_mcp_done 事件通知——所以安装期间设置面板可随意关、装好会弹提示。"""
        import shutil
        from ..config import set_browser_mcp_state
        on = bool(on)
        if not on:
            set_browser_mcp_state(False)
            self._reconnect_mcp()
            return {"ok": True, "enabled": False, "tools": 0}
        if not (shutil.which("npx") or shutil.which("node")):
            return {"ok": False, "error": "未检测到 Node.js，请先安装 Node（含 npx）后重试"}
        # 立即持久化"启用意图"——这样关窗中断后重进仍显示「已启用」、并能自动续装/连接，
        # 不会丢状态、也不会从零重下（install-browser 幂等：已装的秒过、没装完的续上）。
        set_browser_mcp_state(True)
        threading.Thread(target=self._install_browser_bg, daemon=True).start()
        return {"ok": True, "status": "installing"}

    def _install_browser_bg(self) -> None:
        """后台装浏览器 + 装完重连 MCP，全程发 browser_mcp_progress / browser_mcp_done 事件。"""
        import re
        import subprocess
        from ..config import set_browser_mcp_state
        emit = self.res.emit
        base = ["cmd", "/c", "npx"] if os.name == "nt" else ["npx"]
        try:
            emit("browser_mcp_progress", {"text": "准备 Chrome…", "pct": 0})
            # 用 `playwright install chrome`（chrome 通道）：系统已装 Google Chrome 则秒过、否则下载安装。
            # 旧的 `@playwright/mcp install-browser chrome-for-testing` 在新版只打个 warning、**啥也不装**还退 0，
            # 害得「装好了 23 工具但 browser_navigate 报 chrome-for-testing not installed」——本次根因。
            # encoding/errors 必显式给 utf-8：Windows 中文环境 text=True 默认按 GBK 解码，会撞 'gbk' codec 崩。
            proc = subprocess.Popen(
                base + ["-y", "playwright@latest", "install", "chrome"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            last = -1
            pending = ""
            # 进度条用 \r 在同一行刷新，不能只按 \n 切行——同时按 \r/\n 切，进度才逐条冒出来。
            while True:
                chunk = proc.stdout.read(256)
                if not chunk:
                    break
                pending += chunk
                parts = re.split(r"[\r\n]+", pending)
                pending = parts.pop()  # 末段可能未结束，留到下一轮
                for line in parts:
                    line = line.strip()
                    if not line:
                        continue
                    m = re.search(r"(\d{1,3})%", line)
                    if m:
                        pct = min(100, int(m.group(1)))
                        if pct != last:
                            last = pct
                            emit("browser_mcp_progress", {"text": f"下载中… {pct}%", "pct": pct})
                    else:
                        emit("browser_mcp_progress", {"text": line[:80]})
            proc.wait()
            if proc.returncode not in (0, None):
                set_browser_mcp_state(False)   # 硬失败 → 撤销启用意图，避免每次启动自动重试同一失败
                emit("browser_mcp_done", {"ok": False, "error": "浏览器安装失败（退出码非 0）"})
                return
            emit("browser_mcp_progress", {"text": "连接 MCP…", "pct": 100})
            # enabled 已在 set_browser_mcp 点击时置 True（这里不再重复设），直接重连挂上 browser
            self._reconnect_mcp()
            bt = sum(1 for t in (self.res.mcp_tools or []) if t.name.split("__", 1)[0] == "browser")
            emit("browser_mcp_done", {"ok": bt > 0, "tools": bt,
                                      "error": None if bt > 0 else "浏览器装好但 MCP 没连上，重启再试"})
        except Exception as e:  # noqa: BLE001
            set_browser_mcp_state(False)   # 异常 → 撤销启用意图
            emit("browser_mcp_done", {"ok": False, "error": str(e)[:150]})

    def _reconnect_mcp(self) -> int:
        """重启 MCP manager（读最新配置，含浏览器开关）+ 重建各对话 registry，让工具变更即时生效。"""
        from ..config import load_config
        try:
            self.res.mcp.close()
        except Exception:  # noqa: BLE001
            pass
        mgr = McpManager(load_config().mcp)
        try:
            tools = mgr.start()
        except Exception:  # noqa: BLE001
            tools = []
        self.res.mcp, self.res.mcp_tools = mgr, tools
        for conv in list(self.conversations.values()):
            try:
                conv._build_registry()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.active._build_registry()
        except Exception:  # noqa: BLE001
            pass
        return len(tools)

    # ---- 统一管理面：MCP server 增删改（Tier2-①，不必手编 config.yaml） ----------
    def get_mcp_servers(self) -> dict:
        """列出用户在面板加的 MCP server（不含 config.yaml 手编的、不含穿透托管的 browser）。
        附带每个 server 当前实际连上的工具名，便于 UI 显示连通状态。"""
        from ..config import read_user_mcp
        servers = read_user_mcp()
        # 各 server 实连工具：mcp_tools 的名字形如 "<server>__<tool>"
        by_server: dict = {}
        for t in (self.res.mcp_tools or []):
            srv = getattr(t, "name", "").split("__", 1)[0]
            by_server.setdefault(srv, []).append(getattr(t, "name", ""))
        errors = getattr(self.res.mcp, "errors", {}) or {}
        return {"ok": True, "servers": servers, "connected": by_server, "errors": errors}

    def save_mcp_server(self, name: str, spec: dict) -> dict:
        """新增/改一个 MCP server 并重连生效。spec: {command, args[], env{}, trust, enabled}。"""
        from ..config import read_user_mcp, set_user_mcp_server
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "server 名不能为空"}
        if not (spec or {}).get("command"):
            return {"ok": False, "error": "启动命令不能为空（如 npx / uvx / python）"}
        set_user_mcp_server(name, spec)
        tools = self._reconnect_mcp()
        errors = getattr(self.res.mcp, "errors", {}) or {}
        return {"ok": True, "servers": read_user_mcp(), "tools": tools,
                "errors": errors, "connect_error": errors.get(name)}

    def delete_mcp_server(self, name: str) -> dict:
        from ..config import read_user_mcp, remove_user_mcp_server
        remove_user_mcp_server(name)
        tools = self._reconnect_mcp()
        return {"ok": True, "servers": read_user_mcp(), "tools": tools}

    def toggle_mcp_server(self, name: str, on: bool) -> dict:
        """启用/停用某 server（停用＝不挂载，但保留配置）+ 重连生效。"""
        from ..config import read_user_mcp, set_user_mcp_server
        servers = read_user_mcp()
        if name not in servers:
            return {"ok": False, "error": "无此 server"}
        spec = dict(servers[name]); spec["enabled"] = bool(on)
        set_user_mcp_server(name, spec)
        tools = self._reconnect_mcp()
        return {"ok": True, "servers": read_user_mcp(), "tools": tools}

    # ---- 统一管理面：hooks 增删改（Tier2-①，PreToolUse/PostToolUse 守卫）--------
    def _reload_agent_hooks(self) -> None:
        """重读 config（含 merge_user_hooks）刷新活动 config 的 hooks——下一轮 _make_hook_runner 即生效。
        config 是全对话共享的同一对象（self.config is self.res.config），改它即全局生效。"""
        from ..config import load_config
        self.config.agent.hooks = load_config().agent.hooks

    def get_hooks(self) -> dict:
        """列出用户在面板加的 hooks（不含 config.yaml 手编的）。"""
        from ..config import read_user_hooks
        return {"ok": True, "hooks": read_user_hooks()}

    def save_hook(self, index, spec: dict) -> dict:
        """新增（index=null/-1）或改（合法 index）一个 hook 并即时生效。
        spec: {event(PreToolUse|PostToolUse), command, matcher(工具名正则), name, timeout, enabled}。"""
        from ..config import read_user_hooks, upsert_user_hook
        if not (spec or {}).get("command"):
            return {"ok": False, "error": "hook 命令不能为空"}
        idx = None if index in (None, -1, "") else int(index)
        upsert_user_hook(idx, spec)
        self._reload_agent_hooks()
        return {"ok": True, "hooks": read_user_hooks()}

    def delete_hook(self, index) -> dict:
        from ..config import read_user_hooks, remove_user_hook
        remove_user_hook(int(index))
        self._reload_agent_hooks()
        return {"ok": True, "hooks": read_user_hooks()}

    def toggle_hook(self, index, on: bool) -> dict:
        from ..config import read_user_hooks, upsert_user_hook
        hooks = read_user_hooks()
        i = int(index)
        if not (0 <= i < len(hooks)):
            return {"ok": False, "error": "无此 hook"}
        spec = dict(hooks[i]); spec["enabled"] = bool(on)
        upsert_user_hook(i, spec)
        self._reload_agent_hooks()
        return {"ok": True, "hooks": read_user_hooks()}

    # ---- 会话切换 --------------------------------------------------------

    def new_session(self) -> dict:
        old = self.active
        self.active = self._make_conversation(None, [], None)
        if self.res.per_session:
            self._emit_workspace_changed()  # 回到暂存区，刷新面板/标题
        self._leave(old, capture=True)  # 离开旧会话 -> 自动抽取记忆（旧会话若在后台跑则保活）
        return {"ok": True, "cid": self.active.cid}

    def export_markdown(self, filename: str, content: str) -> dict:
        """弹系统「保存为」对话框让用户选位置存导出的 Markdown，返回实际保存路径。

        无窗口（headless）或对话框不可用时返回 {ok:False}，前端回退到浏览器下载（落 Downloads）。"""
        filename = (filename or "对话.md").strip() or "对话.md"
        if not filename.lower().endswith(".md"):
            filename += ".md"
        try:
            import webview
            if not self._window:
                return {"ok": False, "error": "无窗口"}
            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=filename)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"保存对话框失败：{e}"}
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        try:
            p = Path(path)
            if not p.suffix:
                p = p.with_suffix(".md")
            p.write_text(content or "", encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": f"写入失败：{e}"}
        return {"ok": True, "path": str(p)}

    def pick_directory(self) -> dict:
        """弹系统选文件夹对话框，只返回选中的路径（不起会话）——给 MCP/配置等处填目录用。"""
        try:
            import webview
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG) if self._window else None
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"打开选目录框失败：{e}"}
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        return {"ok": True, "path": str(path)}

    def open_project(self) -> dict:
        """弹系统选目录框，以选中的已有项目文件夹起一个新会话（工作区绑定到该目录）。"""
        if not self.res.per_session:
            return {"ok": False, "error": "已在 config 固定了 agent.workspace，未启用按会话工作区"}
        try:
            import webview
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG) if self._window else None
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"打开选目录框失败：{e}"}
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        p = Path(path)
        if not p.is_dir():
            return {"ok": False, "error": "所选不是有效目录"}
        old = self.active
        # 像新会话一样：清空、待落库；但工作区指向所选项目，首条消息建会话时绑定
        self.active = self._make_conversation(None, [], str(p))
        self._emit_workspace_changed()  # 立刻显示该项目（面板/工具切过去）
        self._leave(old, capture=True)
        return {"ok": True, "path": str(p), "cid": self.active.cid}

    def switch_conversation(self, cid: int) -> dict:
        """切到一个已存在的活动运行时（如后台运行中的对话），不重载、不新建。"""
        target = self.conversations.get(int(cid))
        if target is None:
            return {"ok": False, "error": "对话不存在或已结束"}
        old = self.active
        self.active = target
        if self.res.per_session:
            self._emit_workspace_changed()
        if old is not target:
            self._leave(old, capture=(old.session_id != target.session_id))
        return {"ok": True, "cid": target.cid, "session_id": target.session_id,
                "active_model": target.active_model}   # 切换时同步该会话自己的模型到前端下拉

    # ---- 会话持久化（P6.1） ----------------------------------------------

    def list_sessions(self) -> dict:
        if not self.res.store:
            return {"sessions": [], "active": None, "active_cid": self.active.cid}
        return {"sessions": self.res.store.list_sessions(),
                "active": self.active.session_id, "active_cid": self.active.cid}

    def search_messages(self, query: str) -> dict:
        """跨会话全局搜索（P3）：按关键词检索所有会话的消息内容，供前端跳转。
        复用 store.search_messages（recall_history 工具同源），返回 [{session_id, title, role, text}]。"""
        if not self.res.store or not (query or "").strip():
            return {"ok": True, "results": []}
        try:
            results = self.res.store.search_messages(query, limit=30)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:150], "results": []}
        return {"ok": True, "results": results}

    def load_session(self, session_id: int) -> dict:
        """切换到某会话：优先复用仍活着的运行时（后台跑着的）；否则从库回灌 history。"""
        store = self.res.store
        if not store:
            return {"ok": False, "error": "未启用持久化"}
        sid = int(session_id)
        # 已有该会话的活动运行时（后台运行中/本次已加载）-> 直接切回，不重载、不丢状态
        live = next((c for c in self.conversations.values() if c.session_id == sid), None)
        if live is not None:
            old = self.active
            self.active = live
            if self.res.per_session:
                self._emit_workspace_changed()
            if old is not live:
                self._leave(old, capture=(old.session_id != sid))
            msgs = [{"role": m.role, "content": m.content} for m in live.history]
            return {"ok": True, "messages": msgs, "cid": live.cid, "live": True,
                    "active_model": live.active_model}
        if not store.session_exists(sid):
            return {"ok": False, "error": "会话不存在"}
        msgs = store.get_messages(sid)
        old = self.active
        history = [Message(m["role"], m["content"]) for m in msgs]
        self.active = self._make_conversation(sid, history, None)
        if self.res.per_session:  # 切到该会话的工作区
            self._emit_workspace_changed()
        self._leave(old, capture=(old.session_id != sid))  # 切到别的会话 -> 抽取旧会话记忆
        return {"ok": True, "messages": msgs, "cid": self.active.cid,
                "active_model": self.active.active_model}

    def delete_session(self, session_id: int) -> dict:
        store = self.res.store
        if not store:
            return {"ok": False, "error": "未启用持久化"}
        sid = int(session_id)
        store.delete_session(sid)
        # 丢弃该会话的非活动运行时（shutdown 顺带清理其后台进程，FR-10.3）
        for cid, c in list(self.conversations.items()):
            if c.session_id == sid and c is not self.active:
                self.conversations.pop(cid, None)
                try:
                    c.shutdown(timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass
        if sid == self.active.session_id:  # 删的是当前会话 -> 切到一个新草稿
            old = self.active
            self.active = self._make_conversation(None, [], None)
            self.conversations.pop(old.cid, None)
            try:
                old.shutdown(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass
            if self.res.per_session:
                self._emit_workspace_changed()
        return {"ok": True, "active_cid": self.active.cid}

    def rename_session(self, session_id: int, title: str) -> dict:
        store = self.res.store
        if not store:
            return {"ok": False, "error": "未启用持久化"}
        sid = int(session_id)
        title = (title or "").strip() or "新会话"
        store.rename_session(sid, title)
        # 改标题联动重命名"自动分配的"会话工作区文件夹（data/workspaces/<id> → 标题）。
        # 用户手动绑定的外部真实项目目录、正在运行的会话工作区，均不动（见下）。
        if self.res.per_session:
            self._rename_session_workspace_dir(sid, title)
        if sid == self.active.session_id:  # 改的是当前会话 -> 刷新顶部标题
            self._emit_workspace_changed()
        return {"ok": True}

    def set_session_pinned(self, session_id: int, pinned: bool) -> dict:
        """会话置顶/取消置顶（P3）。"""
        if not self.res.store:
            return {"ok": False, "error": "未启用持久化"}
        self.res.store.set_session_pinned(int(session_id), bool(pinned))
        return {"ok": True}

    @staticmethod
    def _safe_ws_name(title: str, sid: int) -> str:
        """会话标题 -> 安全文件夹名：去 Windows 非法字符、限长；空/保留名回退为 id。"""
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip().strip(". ")
        name = name[:60].strip()
        if not name or name.upper() in {"CON", "PRN", "AUX", "NUL"} or name == "_scratch":
            name = str(sid)
        return name

    def _rename_session_workspace_dir(self, sid: int, title: str) -> None:
        """把会话的自动工作区文件夹改成标题名（纯标题，冲突才加 id 后缀）。
        仅限 workspaces_root 下自动分配的目录；外部绑定的真实项目、运行中的会话均不动。"""
        res = self.res
        root = res.workspaces_root
        bound = res.store.get_session_workspace(sid)
        cur = Path(bound) if bound else (root / str(sid))
        def _log(msg: str) -> None:  # 诊断：用 python -m agentcore.app 启动可在控制台看到
            print(f"[rename_ws sid={sid}] {msg}", file=sys.stderr, flush=True)

        try:  # 外部绑定的真实项目（不在 workspaces_root 下）绝不重命名
            if cur.parent.resolve() != root.resolve():
                _log(f"跳过：非自动工作区（外部绑定）cur={cur} root={root}")
                return
        except Exception as e:  # noqa: BLE001
            _log(f"跳过：路径解析异常 {type(e).__name__}: {e}")
            return
        # 该会话**正在执行一轮**时工作区可能被占用，不移动（标题已改，下次空闲再改）。
        # 用 _running_turn 而非 is_busy：后者含 queued/awaiting/队列非空，过宽，会误跳过。
        # crazy 自主模式是后台、跨多轮的长任务：整个运行期间都锁住目录，不能只看单轮 _running_turn——
        # 否则在两轮空隙改标题会把正在用的工作区搬走、导致自主任务"丢文件"（见 DEVLOG v3.21.5）。
        live = next((c for c in self.conversations.values() if c.session_id == sid), None)
        if live is not None and (live._running_turn.is_set() or getattr(live, "crazy_mode", False)):
            self._pending_ws_renames[sid] = title  # 运行中/crazy：记 pending，空闲后自动补改（_sync_pending_ws_rename）
            _log("跳过：会话正在执行一轮 / crazy（已记 pending，空闲后自动补）")
            return
        new_path = root / self._safe_ws_name(title, sid)
        if new_path == cur:
            _log(f"跳过：目标名与当前相同 {new_path}")
            return
        try:
            if cur.exists():
                if new_path.exists():  # 纯标题撞名 -> 加 id 后缀兜底
                    new_path = root / f"{self._safe_ws_name(title, sid)}-{sid}"
                    if new_path.exists():
                        _log(f"跳过：目标已存在 {new_path}")
                        return
                cur.rename(new_path)
                _log(f"成功：{cur.name} -> {new_path.name}")
            else:
                _log(f"目录不存在仅写回 DB：cur={cur}")
            res.store.set_session_workspace(sid, str(new_path))  # 没建文件夹也写回，下次按新名建
            if live is not None:
                live.set_workspace(new_path)
        except Exception as e:  # noqa: BLE001 — 移动失败（占用/权限/跨盘）不致命，标题已改
            _log(f"失败：{type(e).__name__}: {e}  cur={cur} new={new_path}")
            self._emit("error", f"工作区文件夹改名失败（标题已改）：{type(e).__name__}: {e}",
                       self.active.cid)

    def resolve_permission(self, req_id: int, decision: str, cid: int | None = None) -> dict:
        """前端确认条回调：allow / deny / allow_all。

        按 cid 路由到对应对话的 gate——后台对话也可能在等权限，不能固定解到活动对话
        （各对话 gate 的 req_id 独立、会跨对话撞号）。未给 cid 时退回活动对话（兼容）。
        """
        conv = self.conversations.get(int(cid)) if cid is not None else self.active
        if conv is None:
            return {"ok": False, "error": "对话不存在或已结束"}
        return conv.resolve_permission(int(req_id), decision)

    def resolve_ask_user(self, req_id: int, answer: str, cid: int | None = None) -> dict:
        """前端回调：用户对 ask_user 选项的勾选/补充，按 cid 路由到对应对话。"""
        conv = self.conversations.get(int(cid)) if cid is not None else self.active
        if conv is None:
            return {"ok": False, "error": "对话不存在或已结束"}
        return conv.resolve_ask_user(int(req_id), answer)

    def stop_conversation(self, cid: int) -> dict:
        """中止某对话当前运行/排队的任务（回合间生效，FR-8.3）。"""
        conv = self.conversations.get(int(cid))
        if conv is None:
            return {"ok": False, "error": "对话不存在或已结束"}
        conv.stop()
        return {"ok": True}

    # ---- 发消息（入队到活动对话的后台 worker，非阻塞返回） ----------------

    def send_message(self, text: str, attachments=None) -> dict:
        return self.active.enqueue(text, attachments)

    def regenerate(self, turn: int) -> dict:
        """重新生成第 turn（0-based 用户轮次）的回答：丢弃旧答案及其后、在原用户消息上重跑。"""
        return self.active.regenerate(int(turn))

    def edit_and_resend(self, turn: int, text: str) -> dict:
        """编辑第 turn（0-based 用户轮次）的用户消息为 text：丢弃该消息之后全部、重发重跑。"""
        return self.active.edit_and_resend(int(turn), text)

    # ---- 工作区文件预览（转发到活动对话，右侧面板只读） ------------------

    def get_tasks(self) -> dict:
        """当前活动对话的任务清单（FR-9.1），供前端顶部任务面板渲染。"""
        return {"tasks": self.active.get_tasks(), "cid": self.active.cid}

    def get_notes(self) -> dict:
        """当前活动对话的工作笔记（FR-11.3a）。"""
        return {"notes": self.active.get_notes(), "cid": self.active.cid}

    def set_plan_mode(self, on: bool) -> dict:
        """切换当前活动对话的规划模式（FR-11.5）。"""
        return {"ok": True, "plan_mode": self.active.set_plan_mode(on), "cid": self.active.cid}

    def start_autonomous(self, intent: str, max_rounds: int = 0) -> dict:
        """启动当前活动对话的自主/crazy 模式（无人值守外层目标循环）。用现有「停止」即可中止。"""
        return self.active.start_autonomous(intent, max_rounds or None)

    # ---- 检查点（FR-11.6）：列出/手动建/回退（回退仅经前端确认） ------------

    def get_checkpoints(self) -> dict:
        return {"checkpoints": self.active.list_checkpoints(), "cid": self.active.cid}

    def create_checkpoint(self, label: str) -> dict:
        cid = self.active.create_checkpoint((label or "手动检查点").strip() or "手动检查点")
        return {"ok": cid is not None, "id": cid} if cid is not None \
            else {"ok": False, "error": "当前会话未保存，无法创建检查点"}

    def restore_checkpoint(self, checkpoint_id: int) -> dict:
        return self.active.restore_checkpoint(checkpoint_id)

    # ---- 改动评审与回退（FR-9.4a 台账 / FR-10.1 git 语义，右侧面板「改动」区） ----

    def get_changes(self) -> dict:
        return {"changes": self.active.get_changes(),
                "mode": self.active.changes_mode(), "cid": self.active.cid}

    def get_file_diff(self, path: str) -> dict:
        diff = self.active.get_file_diff(path or "")
        if diff is None:
            return {"ok": False, "error": "该文件不在改动列表或无差异"}
        return {"ok": True, "path": path, "diff": diff}

    def revert_file(self, path: str) -> dict:
        ok = self.active.revert_file(path or "")
        return {"ok": ok} if ok else {"ok": False, "error": "回退失败或不在改动列表"}

    def revert_all_changes(self) -> dict:
        return {"ok": True, "reverted": self.active.revert_all()}

    def add_dir(self, path: str, cid: "int | None" = None) -> dict:
        conv = self.conversations.get(int(cid)) if cid else None
        return (conv or self.active).add_dir(path)

    def add_dir_dialog(self) -> dict:
        """弹系统选目录框，把选中目录授权给当前会话（add-dir，对标 Claude Code）。"""
        try:
            import webview
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG) if self._window else None
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"打开选目录框失败：{e}"}
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        return self.active.add_dir(str(path))

    def remove_dir(self, path: str, cid: "int | None" = None) -> dict:
        conv = self.conversations.get(int(cid)) if cid else None
        return (conv or self.active).remove_dir(path)

    def get_extra_dirs(self, cid: "int | None" = None) -> dict:
        conv = self.conversations.get(int(cid)) if cid else None
        return (conv or self.active).get_extra_dirs()

    def get_workspace_tree(self) -> dict:
        return self.active.get_workspace_tree()

    def read_workspace_file(self, path: str) -> dict:
        return self.active.read_workspace_file(path)

    def open_workspace_file(self, path: str) -> dict:
        return self.active.open_workspace_file(path)

    def get_preview_urls(self) -> dict:
        """实时预览面板（UX Tier1-②）：列出当前会话后台 dev server 可预览的本地 URL，
        供前端 iframe 自动对准（最新启动的在前）。无运行中 server 则空。"""
        conv = getattr(self, "active", None)
        procs = getattr(conv, "procs", None) if conv is not None else None
        if procs is None:
            return {"ok": True, "targets": []}
        try:
            return {"ok": True, "targets": procs.preview_targets()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), "targets": []}

    def open_external(self, url: str) -> dict:
        """用系统默认浏览器打开外部链接（FR-11.1 验证反馈：对话里的 URL 点击曾把
        WebView 整窗导航走且无返回，现由前端拦截所有 <a> 点击转到本方法）。"""
        u = (url or "").strip()
        if not u.startswith(("http://", "https://")):
            return {"ok": False, "error": "仅支持 http(s) 链接"}
        try:
            import webbrowser
            webbrowser.open(u)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ---- 启动诊断：前端把关键耗时上报到本进程 stderr（与终端同处可见） ----

    def client_log(self, msg: str) -> dict:
        # 仅 HERMES_DEBUG=1 时打印，普通启动安静（探针保留以便排查）
        if os.environ.get("HERMES_DEBUG", "").lower() in ("1", "true", "yes"):
            print(f"[前端计时] {msg}", file=sys.stderr, flush=True)
        return {"ok": True}

    # ---- 收尾 ------------------------------------------------------------

    def close(self) -> None:
        """应用退出时收尾：先**同步整理活动会话记忆**（否则直接关程序、没切换过会话会丢最后一段），
        再优雅停所有对话 worker、关 MCP 子进程、存储连接（由 app.py 在窗口关闭后调用）。"""
        for conv in list(self.conversations.values()):
            try:
                conv.shutdown(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.res.mcp.close()
        except Exception:  # noqa: BLE001
            pass
        for store in (self.res.store, self.res.memory):
            try:
                if store is not None:
                    store.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- 推事件给前端 ----------------------------------------------------

    def _sync_pending_ws_rename(self, cid) -> None:
        """会话空闲后，补做之前因运行中/crazy 被跳过的工作区文件夹改名（由 ws_settle 触发）。"""
        conv = self.conversations.get(int(cid)) if cid is not None else None
        if conv is None or conv.session_id is None:
            return
        title = self._pending_ws_renames.pop(conv.session_id, None)
        if not title:
            return
        if conv._running_turn.is_set() or getattr(conv, "crazy_mode", False):
            self._pending_ws_renames[conv.session_id] = title  # 还在忙：留到下次空闲再补
            return
        if self.res.per_session:
            self._rename_session_workspace_dir(conv.session_id, title)

    def _emit(self, event: str, data, cid: int | None = None) -> None:
        """推一个事件给前端。cid 标识来源对话，供前端按对话路由（FR-8.2）。

        多个对话的后台 worker 可能并发调用本方法；evaluate_js 不保证线程安全，
        故用 _emit_lock 串行化。
        """
        if event == "ws_settle":  # 内部事件：会话空闲，补做被跳过的工作区改名，不转前端
            self._sync_pending_ws_rename(cid)
            return
        if self._window is None:
            return
        payload = json.dumps({"event": event, "data": data, "cid": cid})
        with self._emit_lock:
            self._window.evaluate_js(f"window.__onAgentEvent({payload})")
