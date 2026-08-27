"""FR-12.2 模型自查报告：纯格式逻辑 + Api.get_diagnostics 取数（不触网、不连真模型）。

运行：python tests/test_modeldiag.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.bridge import Api  # noqa: E402
from agentcore.config import (  # noqa: E402
    AgentConfig, AppConfig, MCPConfig, MemoryConfig, ModelConfig, StorageConfig,
)
from agentcore.modeldiag import ProfileInfo, build_model_report  # noqa: E402


def _p(name="m1", provider="anthropic", base="https://api.deepseek.com/anthropic",
       key_env="K", key_set=True):
    return ProfileInfo(name=name, provider=provider, model="deepseek-v4-flash",
                       base_url=base, key_env=key_env, key_set=key_set)


def _report(**kw):
    args = dict(version="9.9.9", session_label="#7", active="m1", profiles=[_p()],
                extras=[("委派子任务（delegate）", "（跟随主对话）")], recent=[("#7", "2026-08-26 10:00", "m1")])
    args.update(kw)
    return build_model_report(**args)


def test_report_names_protocol_endpoint_and_key_source():
    """协议、端点、key 来源都得写出来——把协议认错，后面全是白功夫。"""
    r = _report()
    assert "provider=anthropic" in r
    assert "https://api.deepseek.com/anthropic" in r
    assert "key 取自 K（已设置）" in r
    assert "← 就是它" in r          # 当前档要一眼认出


def test_report_never_contains_the_key_itself():
    """这份报告就是给人贴出来求助的：只说 key 来自哪个环境变量，绝不带 key 本身。"""
    r = build_model_report(version="1", session_label="#1", active="m1",
                           profiles=[_p(key_env="MY_SECRET_ENV")],
                           extras=[], recent=[])
    assert "MY_SECRET_ENV" in r and "sk-" not in r
    assert "不含任何 API key" in r


def test_report_flags_unset_key_and_missing_profile():
    assert "⚠ 未设置" in _report(profiles=[_p(key_set=False)])
    # 会话记的档后来被删了：如实说，别装作没这回事
    assert "已不在配置里" in _report(active="没了的档")
    assert "还没选模型档" in _report(active="")


def test_report_lists_the_other_places_that_call_models():
    """委派/压缩摘要各有自己的档，最容易被忘——报错可能来自它们。"""
    r = _report(extras=[("委派子任务（delegate）", "另一个档"), ("上下文压缩摘要", "（跟随主对话）")])
    assert "另一个档" in r and "上下文压缩摘要" in r


def test_report_shows_recent_sessions_as_record_not_memory():
    r = _report(recent=[("#7", "2026-08-26 10:00", "m1"), ("#6", "2026-08-25 09:00", "")])
    assert "这是记录，不是记忆" in r
    assert "#6" in r and "（未设）" in r      # 没落下档名的会话也照实显示


def _api(tmp: Path) -> Api:
    return Api(AppConfig(
        active_model="m1",
        models={"m1": ModelConfig(provider="anthropic", model="deepseek-v4-flash",
                                  api_key_env="K_NOT_SET",
                                  base_url="https://api.deepseek.com/anthropic")},
        agent=AgentConfig(workspaces_root=str(tmp / "ws"), auto_conventions=False),
        storage=StorageConfig(enabled=True, db_path=str(tmp / "h.db")),
        memory=MemoryConfig(enabled=False),
        mcp=MCPConfig(enabled=False),
    ))


def test_api_get_diagnostics_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        r = _api(Path(td)).get_diagnostics()
    assert r["ok"] is True
    t = r["text"]
    assert "provider=anthropic" in t and "https://api.deepseek.com/anthropic" in t
    assert "⚠ 未设置" in t                      # 环境里没有 K_NOT_SET
    assert "草稿对话" in t                       # 还没落库的对话如实说
    assert "委派子任务（delegate）" in t


def test_api_diagnostics_covers_per_role_models():
    """按角色配的模型（agent.roles.<角色>.model）最容易被忘：`research-report` 那类技能
    带 role=researcher 委派，走的可能压根不是主对话那个档——漏列这一项就会把排查引向错误的账户。"""
    from agentcore.config import RoleSpec
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        api = Api(AppConfig(
            active_model="m1",
            models={"m1": ModelConfig(provider="anthropic", model="a", api_key_env="K1",
                                      base_url="https://api.deepseek.com/anthropic"),
                    "relay": ModelConfig(provider="openai", model="a", api_key_env="K2",
                                         base_url="https://relay.example.com/v1")},
            agent=AgentConfig(workspaces_root=str(tmp / "ws"), auto_conventions=False,
                              roles={"researcher": RoleSpec(label="调研", model="relay")}),
            storage=StorageConfig(enabled=True, db_path=str(tmp / "h.db")),
            memory=MemoryConfig(enabled=False), mcp=MCPConfig(enabled=False),
        ))
        t = api.get_diagnostics()["text"]
    assert "子 Agent 角色 researcher：relay" in t     # 角色档要单独点名
    assert "provider=openai" in t and "relay.example.com" in t   # 它的协议/端点也要摊开
    assert "上下文压缩摘要" in t                        # 另一个易漏项也在


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
