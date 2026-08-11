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
    model_list_urls,
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
        # 技能市场仓库归档缓存（FR-13.S2）：预览完接着安装时不重复下载
        self._skill_cache: dict = {}
        self._skill_cache_lock = threading.Lock()
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
        urls = model_list_urls(provider, base_url)
        if not urls:
            return {"ok": False, "error": "未配置 Base URL"}
        if provider == "openai":
            attempts = [{"Authorization": f"Bearer {key_val}"}]
        else:
            # **两种认证头都试**：Anthropic 官方认 x-api-key，但兼容端点未必——实测火山方舟 coding 端点
            # 的 /v1/models 只认 `Authorization: Bearer`，只发 x-api-key 会 401，用户会以为 key 填错了。
            attempts = [{"x-api-key": key_val, "anthropic-version": "2023-06-01"},
                        {"Authorization": f"Bearer {key_val}", "anthropic-version": "2023-06-01"}]
        last = ""
        for url in urls:                       # 同源候选地址依次试（见 model_list_urls）
            for headers in attempts:
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(resp.read().decode())
                    ids = sorted({m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")})
                    return ({"ok": True, "models": ids} if ids
                            else {"ok": False, "error": "该服务未返回模型列表，请手动添加"})
                except Exception as e:  # noqa: BLE001 — 换下一种认证头/下一个地址再试，都失败才报
                    last = str(e)[:110]
        hint = ("该服务的 Anthropic 兼容端点通常只实现 /v1/messages、不提供模型列表——"
                "**不影响对话**，手动添加模型 ID 即可。" if provider != "openai"
                else "该服务可能不支持列模型，可手动添加。")
        return {"ok": False, "error": f"获取失败（{hint}）：{last}"}

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

    def check_update(self) -> dict:
        """应用内更新（ADR 0020 T1）：查 GitHub 最新版本 tag 与本地比对。前端启动时静默调，有新版才弹条幅。
        返回 {ok, current, latest?, newer?, notes_url?, error?}；网络失败 ok=False（前端不打扰）。

        **默认关**（`agent.update_check=false`）：直接返回 disabled，**不发网络请求**——用户不要应用内
        更新提醒。这里是唯一的检查入口（前端启动只调它），所以在这一处拦住就等于整条链路停用。
        """
        if not getattr(self.config.agent, "update_check", False):
            return {"ok": False, "disabled": True}
        from ..updater import check_update as _chk
        try:
            return _chk()
        except Exception as e:  # noqa: BLE001 — 更新检查绝不影响正常使用
            return {"ok": False, "error": str(e)}

    def apply_update(self) -> dict:
        """一键源码自更新：git pull --ff-only + pip install -e .（复用非交互硬化环境）。返回 {ok, steps, message}。
        同步阻塞跑完才返回（内部不 evaluate_js，不触 WebView2 死锁坑）；完成后前端提示重启生效。"""
        from ..updater import apply_update as _apply, repo_root
        try:
            return _apply(repo_root())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "steps": [], "message": f"更新出错：{e}"}

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

    def get_limits(self) -> dict:
        """「限额与预算」面板数据：spec（分组/标签/范围，前端据此渲染）+ 各项当前生效值。"""
        from ..config import LIMITS_SPEC
        values = {}
        for s in LIMITS_SPEC:
            section, _, field = s["key"].partition(".")
            sec = getattr(self.config, section, None)
            values[s["key"]] = getattr(sec, field, None) if sec is not None else None
        return {"ok": True, "spec": [dict(s) for s in LIMITS_SPEC], "values": values}

    def set_limits(self, updates: dict) -> dict:
        """GUI 改限额：持久化到 limits.json（重启仍在）+ 即时改活动 config 对应字段（下一步即生效）。"""
        from ..config import set_limits as persist_limits, _LIMITS_BY_KEY, _coerce_limit
        for k, v in (updates or {}).items():
            spec = _LIMITS_BY_KEY.get(k)
            if spec is None:
                continue
            num = _coerce_limit(spec, v)
            if num is None:
                continue
            section, _, field = k.partition(".")
            sec = getattr(self.config, section, None)
            if sec is not None and hasattr(sec, field):
                setattr(sec, field, num)     # 即时生效（各处现读 config）
        persist_limits(updates)              # 持久化（只取白名单键、按范围校验）
        return self.get_limits()

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

    # ---- 统一管理面：🧩 技能（FR-13.S2 浏览 / 扫描 / 安装 / 卸载） ---------------
    # 分工：浏览只拉小小的 marketplace.json（快）；预览/安装才下载仓库归档（慢，带缓存复用）。
    # **安装前一律先扫描并把结论回给前端**，由前端按 clean/review/warn 决定确认强度——
    # 后端不自作主张装（见 ADR-0015）。

    def _skills_root(self) -> Path:
        """用户全局技能目录（<APP_DIR>/skills）。内置技能在 BUNDLE_DIR，不往那儿装。"""
        from ..paths import APP_DIR
        root = APP_DIR / "skills"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def get_skills(self) -> dict:
        """当前会话可用的技能 + 解析错误（含内置/全局/项目级各自来源）。"""
        return {"ok": True, **self.active.get_skills()}

    # ---- 自定义斜杠命令（FR-13.C1）：/盯盘 这类确定性入口 --------------------

    def get_commands(self) -> dict:
        """当前可用的自定义命令 + 解析错误（补全菜单与管理面都用它）。"""
        return {"ok": True, **self.active.get_commands()}

    def expand_command(self, name: str, arg: str = "") -> dict:
        """展开一条命令：prompt 模式回最终提示词，exec 模式回最终命令行。"""
        return self.active.expand_command(name, arg)

    def run_command(self, name: str, arg: str = "") -> dict:
        """执行 exec 模式命令（后台线程 + 权限 gate，立即返回）。"""
        return self.active.run_command(name, arg)

    def save_command(self, name: str, spec: dict, scope: str = "project") -> dict:
        """新建/编辑一条自定义命令（scope: project=跟项目走 / global=全局可用）。"""
        return self.active.save_command(name, spec, scope)

    def delete_command(self, name: str) -> dict:
        """删除一条自定义命令。"""
        return self.active.delete_command(name)

    # ---- 权限规则（FR-11.4b）：可见、可撤、重启仍在 ------------------------

    def get_permissions(self) -> dict:
        """当前生效的 allow/deny 规则，并标出哪些是面板加的（只有这些能在面板撤销）。"""
        return {"ok": True, **self.active.get_permissions()}

    def add_permission(self, rule: str) -> dict:
        """加一条免确认规则（落盘 + 当前会话立即生效）。"""
        return self.active.add_permission(rule)

    def remove_permission(self, rule: str) -> dict:
        """撤销一条面板加的免确认规则。"""
        return self.active.remove_permission(rule)

    def suggest_permission_for_command(self, name: str) -> dict:
        """给一条 exec 命令推导免确认规则（命令管理面「免确认」按钮用）。"""
        return self.active.suggest_permission_for_command(name)

    def get_skill_markets(self) -> dict:
        """技能市场清单：内置精选（随程序分发、已核实）+ 用户自己加的。"""
        from ..config import read_user_markets
        from ..paths import APP_DIR, BUNDLE_DIR
        from ..skillhub import load_catalog
        builtin = load_catalog(BUNDLE_DIR / "skill_catalog.json")
        if not builtin and BUNDLE_DIR != APP_DIR:
            builtin = load_catalog(APP_DIR / "skill_catalog.json")
        return {"ok": True, "builtin": builtin,
                "user": [{**m, "trust": "user"} for m in read_user_markets()]}

    def add_skill_market(self, repo: str, title: str = "") -> dict:
        """加一个技能市场（GitHub `owner/repo` 或链接）。先真拉一次确认它是市场再存。"""
        from ..config import add_user_market
        from ..skillhub import HubError, fetch_marketplace
        try:
            market, norm = fetch_marketplace(repo)
        except HubError as e:
            return {"ok": False, "error": str(e)}
        add_user_market(norm, title or market.name)
        return {"ok": True, "repo": norm, "name": market.name, "entries": len(market.entries)}

    def remove_skill_market(self, repo: str) -> dict:
        from ..config import remove_user_market
        return {"ok": True, "user": remove_user_market(repo)}

    def browse_skill_market(self, repo: str, deep: bool = False) -> dict:
        """列出一个市场里的条目。

        `deep=False`：只拉 marketplace.json（几十 KB，秒回）——先把列表显示出来。
        `deep=True`：再下载一次仓库归档，数清每个条目**实际含几个技能**（`skill_count`）。
        为什么需要：Claude Code 插件可以只含 commands/agents/hooks 而没有 SKILL.md
        （实测官方市场 13 个条目里只有 4 个含技能）。不深扫的话，用户要点进去等下载完
        才发现"这个装不了"。前端先浅拉出列表、再后台深扫更新计数。
        """
        from ..skillhub import HubError, fetch_marketplace, find_skills_in_tree, resolve_source
        try:
            market, norm = fetch_marketplace(repo)
            root = self._fetch_repo_cached(norm) if deep else None
        except HubError as e:
            return {"ok": False, "error": str(e)}
        entries = []
        for e in market.entries:
            r = resolve_source(e.source)
            item = {
                "name": e.name, "description": e.description, "version": e.version,
                "author": e.author, "category": e.category, "keywords": list(e.keywords),
                "supported": r.kind != "unsupported", "unsupported_reason": r.reason,
                "skill_count": None,   # None = 还没数（未深扫）
            }
            if root is not None and r.kind == "subdir":
                d = root / r.path if r.path else root
                item["skill_count"] = len(find_skills_in_tree(d)) if d.is_dir() else 0
            entries.append(item)
        return {"ok": True, "repo": norm, "name": market.name, "deep": bool(deep),
                "description": market.description, "owner": market.owner,
                "homepage": market.homepage, "entries": entries}

    def _fetch_repo_cached(self, repo: str):
        """下载市场仓库归档到缓存目录并复用（预览完接着安装时不重复下载）。"""
        from ..paths import APP_DIR
        from ..skillhub import download_repo
        with self._skill_cache_lock:
            hit = self._skill_cache.get(repo)
            if hit is not None and Path(hit).is_dir():
                return Path(hit)
            root = download_repo(repo, APP_DIR / "data" / "skill_cache" / repo.replace("/", "__"))
            self._skill_cache[repo] = str(root)
            return root

    def preview_skills(self, repo: str, entry_name: str) -> dict:
        """下载并解析某个市场条目里的技能，**逐个跑本地安全扫描**，回给前端供用户决定。"""
        from ..skillhub import (
            HubError, fetch_marketplace, find_skills_in_tree, read_text_files, resolve_source,
        )
        from ..skills import SkillError, parse_skill_md
        from .. import skillscan
        try:
            market, norm = fetch_marketplace(repo)
            entry = next((e for e in market.entries if e.name == entry_name), None)
            if entry is None:
                return {"ok": False, "error": f"市场 {norm} 里没有条目「{entry_name}」"}
            res = resolve_source(entry.source)
            if res.kind == "unsupported":
                return {"ok": False, "error": f"这个条目暂不支持自动安装：{res.reason}"}
            root = self._fetch_repo_cached(res.repo or norm)
            plugin_dir = root / res.path if res.path else root
            if not plugin_dir.is_dir():
                return {"ok": False, "error": f"条目指向的路径在仓库里不存在：{res.path}"}
            dirs = find_skills_in_tree(plugin_dir)
        except HubError as e:
            return {"ok": False, "error": str(e)}
        if not dirs:
            return {"ok": False, "error": "这个条目里没有找到 SKILL.md（可能不是技能类插件）"}

        installed = {s["name"] for s in self.active.get_skills().get("skills", [])}
        out = []
        for d in dirs:
            try:
                skill = parse_skill_md((d / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
            except (SkillError, OSError) as e:
                out.append({"dir": d.name, "error": f"SKILL.md 不合规范：{e}"})
                continue
            scan = skillscan.merge(skillscan.scan_files(read_text_files(d)),
                                   skillscan.scan_declared_tools(skill.allowed_tools))
            out.append({
                "name": skill.name, "description": skill.description, "dir": str(d),
                "license": skill.license, "compatibility": skill.compatibility,
                "allowed_tools": list(skill.allowed_tools),
                "installed": skill.name in installed,
                "grade": scan.grade, "grade_label": skillscan.GRADE_LABEL[scan.grade],
                "flags": list(scan.flags), "summary": skillscan.summarize(scan),
                "findings": [{"kind": f.kind, "severity": f.severity, "where": f.where,
                              "excerpt": f.excerpt, "why": f.why} for f in scan.findings],
            })
        return {"ok": True, "repo": norm, "entry": entry_name, "skills": out}

    def _install_ledger(self) -> Path:
        """安装来源台账路径（记「这个技能从哪个市场的哪个条目来的」，供检查更新）。"""
        from ..paths import APP_DIR
        return APP_DIR / "skill_installs.json"

    def install_skill(self, src_dir: str, overwrite: bool = False,
                      repo: str = "", entry: str = "") -> dict:
        """安装一个已预览过的技能（src_dir 取自 preview_skills 返回的 dir）。

        只允许装缓存目录里的东西——防前端传任意路径把机器上别处的目录拷进技能库。
        传了 repo/entry 就记进安装台账，之后才能检查更新。
        """
        from ..paths import APP_DIR
        from ..skillhub import HubError, install_skill as do_install, record_install, relative_in
        try:
            src = Path(src_dir).resolve()
            cache_root = (APP_DIR / "data" / "skill_cache").resolve()
            if cache_root not in src.parents:
                return {"ok": False, "error": "只能安装从市场下载下来的技能"}
            target = do_install(src, self._skills_root(), overwrite=bool(overwrite))
        except HubError as e:
            return {"ok": False, "error": str(e)}
        except OSError as e:
            return {"ok": False, "error": f"安装失败：{e}"}

        if repo:
            # src_rel 相对**解压出的仓库根**——归档顶层目录名带 commit sha、每次下载都变，
            # 不能记进去，否则下次更新时按老路径找不到。
            archive_root = self._skill_cache.get(repo)
            src_rel = relative_in(Path(archive_root), src) if archive_root else ""
            record_install(target.name, target, repo=repo, entry=entry, src_rel=src_rel,
                           version="", installed=target, ledger=self._install_ledger())
        self.active._refresh_skills()   # 装完立刻可用，不必重启
        return {"ok": True, "path": str(target), **self.active.get_skills()}

    def promote_skill(self, name: str, scope: str = "global", overwrite: bool = False) -> dict:
        """把一个技能复制到另一层：scope=global → <APP_DIR>/skills；scope=project → <工作区>/.hermes/skills。

        用途：`skill-creator` 生成的技能默认只落项目级，换项目就没了——这里给它一个"装到全局"的入口；
        反向（全局 → 本项目）用于只想在某个项目里改一份，不动全局那份。
        复制走 skillhub.install_skill：它会先校验 SKILL.md 合法且 name 与目录名一致，装了也能被发现。
        """
        from ..skillhub import HubError, install_skill as do_install
        name = (name or "").strip()
        found = next((s for s in self.active._skills if s.name == name), None)
        if found is None:
            return {"ok": False, "error": f"没有名为「{name}」的技能"}
        if scope == "project":
            root = self.active.workspace / ".hermes" / "skills"
        elif scope == "global":
            root = self._skills_root()
        else:
            return {"ok": False, "error": f"scope 只能是 global 或 project，收到 {scope!r}"}
        src = Path(found.path)
        try:
            if src.resolve() == (root / name).resolve():
                return {"ok": False, "error": "源和目标是同一个位置"}
        except OSError:
            pass
        try:
            root.mkdir(parents=True, exist_ok=True)
            target = do_install(src, root, overwrite=bool(overwrite))
        except HubError as e:
            return {"ok": False, "error": str(e), "exists": "已存在" in str(e)}
        except OSError as e:
            return {"ok": False, "error": f"复制失败：{e}"}
        self.active._refresh_skills()
        return {"ok": True, "name": name, "scope": scope, "path": str(target)}

    def uninstall_skill(self, name: str) -> dict:
        from ..skillhub import forget_install, uninstall_skill as do_uninstall
        if not do_uninstall((name or "").strip(), self._skills_root()):
            return {"ok": False, "error": f"没有已安装的技能「{name}」（内置技能不可删）"}
        forget_install(name, self._install_ledger())
        self.active._refresh_skills()
        return {"ok": True, **self.active.get_skills()}

    def check_skill_updates(self) -> dict:
        """检查已装技能有没有更新（FR-13.S3）。

        比对方式是**内容哈希**而非版本号——`version` 在规范里可选、实测很多技能压根没有。
        每个来源仓库只下载一次（按 repo 分组 + 缓存复用）。

        找到更新**不会直接装**：把新版本重新跑一遍安全扫描、连分级一起回给前端，
        走和首次安装同样的三档确认。良性技能的新版本可能变坏，这是真实的供应链风险点。
        """
        from ..skillhub import HubError, dir_hash, find_all_skills, prune_installs, read_text_files
        from .. import skillscan
        ledger = self._install_ledger()
        skills_root = self._skills_root()
        installs = prune_installs(ledger, skills_root)   # 顺手清掉用户手删的

        installed_names = {s["name"] for s in self.active.get_skills().get("skills", [])}
        results, checked_repos = [], {}
        for name in sorted(installed_names):
            local = skills_root / name
            rec = installs.get(name)
            if not local.is_dir():
                continue                    # 内置技能不在这个目录下，跳过（它随程序更新）
            if not rec or not rec.get("repo"):
                results.append({"name": name, "status": "no_source",
                                "note": "手动放进来的，没有来源记录——无法检查更新"})
                continue
            repo = rec["repo"]
            if repo not in checked_repos:
                try:
                    checked_repos[repo] = self._fetch_repo_cached(repo)
                except HubError as e:
                    checked_repos[repo] = e
            root = checked_repos[repo]
            if isinstance(root, HubError):
                results.append({"name": name, "status": "error", "repo": repo,
                                "note": f"取不到来源仓库：{root}"})
                continue

            remote = (root / rec["src_rel"]) if rec.get("src_rel") else None
            if remote is None or not remote.is_dir():
                # 上游改了目录结构：退而按技能名在整个仓库里找一次
                remote = next((d for d in find_all_skills(root) if d.name == name), None)
            if remote is None or not remote.is_dir():
                results.append({"name": name, "status": "gone", "repo": repo,
                                "note": "上游已经找不到这个技能了（可能被删或改名）"})
                continue

            if dir_hash(remote) == dir_hash(local):
                results.append({"name": name, "status": "current", "repo": repo})
                continue

            scan = skillscan.scan_files(read_text_files(remote))
            results.append({
                "name": name, "status": "update", "repo": repo, "entry": rec.get("entry", ""),
                "dir": str(remote), "grade": scan.grade,
                "grade_label": skillscan.GRADE_LABEL[scan.grade],
                "flags": list(scan.flags), "summary": skillscan.summarize(scan),
            })
        return {"ok": True, "results": results,
                "updates": sum(1 for r in results if r["status"] == "update")}

    def read_skill(self, name: str) -> dict:
        """读一个已装技能的 SKILL.md 正文 + 附带文件（面板里"查看"用）。"""
        from ..skillhub import read_text_files
        from ..skills import SkillError, list_skill_files, load_skill_body
        from .. import skillscan
        skill = next((s for s in self.active._skills if s.name == name), None)
        if skill is None:
            return {"ok": False, "error": f"没有名为「{name}」的技能"}
        try:
            loaded = load_skill_body(skill)
        except SkillError as e:
            return {"ok": False, "error": str(e)}
        scan = skillscan.merge(skillscan.scan_files(read_text_files(skill.path)),
                               skillscan.scan_declared_tools(skill.allowed_tools))
        return {"ok": True, "name": loaded.name, "description": loaded.description,
                "body": loaded.body, "path": str(loaded.path), "source": loaded.source,
                "files": list_skill_files(loaded), "grade": scan.grade,
                "flags": list(scan.flags), "summary": skillscan.summarize(scan)}

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

    def save_image(self, data_url: str, filename: str = "") -> dict:
        """弹系统「保存为」对话框把对话里的图片（data:image base64）存到本地，返回实际路径。

        WebView2/pywebview 里 <a download> 点了不落盘，所以图片下载统一走这里。
        无窗口（headless）或对话框不可用时返回 {ok:False}，前端回退到浏览器下载。"""
        import base64
        import re
        m = re.match(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.*)$", (data_url or "").strip(), re.S)
        if not m:
            return {"ok": False, "error": "不是可保存的 base64 图片"}
        ext = m.group(1).lower()
        if ext in ("jpeg", "jpg"):
            ext = "jpg"
        elif ext == "svg+xml":
            ext = "svg"
        name = (filename or "").strip() or ("hermes-image." + ext)
        if not name.lower().endswith("." + ext):
            name += "." + ext
        try:
            data = base64.b64decode(m.group(2), validate=False)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"图片解码失败：{e}"}
        try:
            import webview
            if not self._window:
                return {"ok": False, "error": "无窗口"}
            result = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=name)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"保存对话框失败：{e}"}
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        try:
            p = Path(path)
            if not p.suffix:
                p = p.with_suffix("." + ext)
            p.write_bytes(data)
        except OSError as e:  # noqa: BLE001
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

    def send_message(self, text: str, attachments=None, cid: "int | None" = None) -> dict:
        # cid 缺省=活动对话；评审开工的自动指令带上发起 cid，重排后切走也不会发错对话。
        return self._conv_by_cid(cid).enqueue(text, attachments)

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

    def _conv_by_cid(self, cid: "int | None"):
        """按 cid 路由到对应对话；未给或已失效则退回活动对话（兼容）。
        评审「开始编码」这类跨异步的动作要锁定发起时的对话——重排耗时里用户可能切走，
        不能固定解到活动对话（否则会在新对话里开工，见 resolve_permission 同类修复）。"""
        if cid is None:
            return self.active
        return self.conversations.get(int(cid)) or self.active

    def set_plan_mode(self, on: bool, cid: "int | None" = None) -> dict:
        """切换指定对话（缺省=活动对话）的规划模式（FR-11.5）。"""
        conv = self._conv_by_cid(cid)
        return {"ok": True, "plan_mode": conv.set_plan_mode(on), "cid": conv.cid}

    # ---- 方案评审（ADR 0019 Architecture Review Mode）-----------------------

    def start_design_review(self, proposal_text: "str | None" = None) -> dict:
        """对**指定内容**发起多角色评审（前端传某条回复正文 / 划选的一段）；不传才回退 notes。"""
        return self.active.start_design_review(proposal_text)

    def run_design_review(self) -> dict:
        """第二阶段：对已拆解会话跑多角色评审，回填四态共识 + gate。"""
        return self.active.run_design_review()

    def cancel_design_review(self) -> dict:
        """取消正在跑的评审（关闭评审栏 / 退出规划模式）；幂等。"""
        return self.active.cancel_design_review()

    def get_design_review(self) -> dict:
        """取当前评审状态（共识/gate/决策），未开始则 ok=False。"""
        return self.active.get_design_review()

    def resolve_decision(self, decision_id: str, status: str,
                         current_choice: "str | None" = None) -> dict:
        """用户拍板一个决策（设四态/定稿/清未决，作废旧签字）。"""
        return self.active.resolve_decision(decision_id, status, current_choice)

    def sign_off_design_review(self) -> dict:
        """用户签字确认开工（gate 仍复核未决阻塞==0）。"""
        return self.active.sign_off_design_review()

    def can_start_coding(self, cid: "int | None" = None) -> dict:
        """开工 gate：未决阻塞==0 且已签字。cid 缺省=活动对话。"""
        conv = self._conv_by_cid(cid)
        return {"can_start": conv.can_start_coding(), "cid": conv.cid}

    def apply_review_to_plan(self, cid: "int | None" = None) -> dict:
        """把评审定稿落回规划(notes)+任务清单(tasks)；仅 gate 放行后可用。
        cid 缺省=活动对话；主模型重排耗时里用户可能切走，锁定发起对话才不会落错。"""
        return self._conv_by_cid(cid).apply_review_to_plan()

    def hand_review_to_main(self, cid: "int | None" = None) -> dict:
        """把评审定稿作为一条消息交给主模型，让它据此迭代后续开发（阶段方案的出口，不动 notes/待办）。
        cid 缺省=活动对话；入队耗时里用户可能切走，锁定发起对话才不会发错会话。"""
        return self._conv_by_cid(cid).hand_review_to_main()

    def get_design_review_models(self) -> dict:
        """评审模型选择：两个角色名、可用模型档名、当前映射（空=**自动挑不同模型**，见 review_model_plan）。供 UI 下拉。"""
        from ..agent.design_review import migrate_reviewer_models
        cfg = self.res.config
        return {"reviewers": ["product", "technical"],
                "reviewer_labels": {"product": "产品镜头", "technical": "技术镜头"},
                "available": list(cfg.models.keys()),
                "active_model": cfg.active_model,
                "current": migrate_reviewer_models(cfg.agent.design_review_models or {})}

    def set_design_review_model(self, reviewer: str, profile: "str | None") -> dict:
        """给某评审角色选模型（profile 空/None=**自动挑**，见 review_model_plan），写回内存 + config.yaml。"""
        from ..config import persist_design_review_models
        from ..agent.design_review import migrate_reviewer_models
        cfg = self.res.config
        if reviewer not in ("product", "technical"):
            return {"ok": False, "error": "未知角色"}
        m = migrate_reviewer_models(cfg.agent.design_review_models or {})   # 顺带把旧键归一，避免新旧并存
        if profile and profile in cfg.models:
            m[reviewer] = profile
        else:
            m.pop(reviewer, None)                 # 空或非法档名 → 该角色交给自动挑
        cfg.agent.design_review_models = m
        try:
            persist_design_review_models(m)
        except Exception:  # noqa: BLE001 — 内存已生效，持久化失败不阻断
            pass
        return {"ok": True, "current": m}

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

    def write_process_input(self, pid_id: int, text: str, cid: "int | None" = None) -> dict:
        """人接管：往某个后台进程的 stdin 写一行（P3 / ADR 0022 的"人"入口）。

        与模型侧 `write_process_input` 工具共用同一条通道（`ProcessManager.write_input`），
        区别只在**谁在敲**：这条是用户在 UI 上亲手输入的，**不过权限 gate**——
        用户自己就是授权本身，再弹一个"你确定要输入 y 吗"是纯噪声。
        """
        conv = self.conversations.get(int(cid)) if cid else None
        conv = conv or getattr(self, "active", None)
        procs = getattr(conv, "procs", None) if conv is not None else None
        if procs is None:
            return {"ok": False, "error": "当前会话没有后台进程管理器"}
        try:
            return {"ok": True, "message": procs.write_input(int(pid_id), str(text))}
        except Exception as e:  # noqa: BLE001 — ToolError 等一律转成人话回前端
            return {"ok": False, "error": str(e)}

    def stop_background_process(self, pid_id: int, cid: "int | None" = None) -> dict:
        """人接管的另一半：不想回答就直接终止这个后台进程（同上，不过 gate）。"""
        conv = self.conversations.get(int(cid)) if cid else None
        conv = conv or getattr(self, "active", None)
        procs = getattr(conv, "procs", None) if conv is not None else None
        if procs is None:
            return {"ok": False, "error": "当前会话没有后台进程管理器"}
        try:
            return {"ok": True, "message": procs.stop(int(pid_id))}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

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
