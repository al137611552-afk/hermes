"""「这次到底用的哪个模型档」自查报告（FR-12.2）。

为什么要它：模型侧报错**不带档名**，而一台机器上通常有好几个档在同时工作——
主对话一个、委派子任务一个（`agent.subagent_model`）、压缩摘要一个（`context.summary_model`）、
视觉预处理又一个。用户凭记忆说"我用的是 X"，查的却可能是另一个档对应的账户，
一路查错方向（2026-08-26 就这么绕了几轮）。**记忆不可靠，记录可靠**：
每个会话当时用的档名是落了库的（`sessions.model`），这里把它连同各档的 provider/端点一起摊开。

纪律两条：
  ① **绝不打印 key 本身**，只说它取自哪个环境变量、设没设上——这份报告就是给人贴出来求助用的。
  ② provider 字段要显眼：它决定走哪套 SDK，而两套 SDK 对同一个 HTTP 错误的报法不一样，
     排查时把协议认错，后面全是白功夫。

纯逻辑（本模块）与取数（`Api.get_diagnostics`）分离：报告格式可脱离配置/数据库单测。
"""
from __future__ import annotations

from dataclasses import dataclass

UNSET = "（未设）"
FOLLOW = "（跟随主对话）"


@dataclass
class ProfileInfo:
    """一个模型档的自查视图。**不含 key**，只有 key 的来源与是否设上。"""
    name: str
    provider: str
    model: str
    base_url: str
    key_env: str
    key_set: bool


def format_profile(p: ProfileInfo, *, mark: str = "") -> str:
    key = f"key 取自 {p.key_env}（{'已设置' if p.key_set else '⚠ 未设置'}）" if p.key_env else "⚠ 没配 key 来源"
    return (f"  {p.name}{mark}\n"
            f"      provider={p.provider}   model={p.model}\n"
            f"      端点={p.base_url or '（该 provider 的默认官方端点）'}\n"
            f"      {key}")


def build_model_report(*, version: str, session_label: str, active: str,
                       profiles: "list[ProfileInfo]", extras: "list[tuple[str, str]]",
                       recent: "list[tuple[str, str, str]]") -> str:
    """拼出可直接贴出来的自查报告（纯函数）。

    session_label：当前会话的人话标识；active：当前会话用的档名；
    extras：[(哪里会用模型, 用的哪个档)]；recent：[(会话号, 时间, 档名)]。
    """
    by_name = {p.name: p for p in profiles}
    out = [f"hermes {version}　模型自查", "", "■ 当前对话用的档"]
    out.append(f"  会话：{session_label}")
    if active and active in by_name:
        out.append(format_profile(by_name[active], mark="  ← 就是它"))
    elif active:
        out.append(f"  {active}\n      ⚠ 这个档现在已不在配置里（改过或删过）")
    else:
        out.append(f"  {UNSET}——还没选模型档")

    out += ["", "■ 其它也会调模型的地方（容易被忘掉，报错可能来自它们）"]
    out += [f"  {where}：{which}" for where, which in extras] or ["  （无）"]

    out += ["", f"■ 已配置的模型档（共 {len(profiles)} 个）"]
    for p in profiles:
        out.append(format_profile(p, mark="  ← 当前" if p.name == active else ""))
    if not profiles:
        out.append("  （一个都没有）")

    if recent:
        out += ["", f"■ 最近 {len(recent)} 个会话当时用的档（这是记录，不是记忆）"]
        out += [f"  {sid:<6} {when}   {model or UNSET}" for sid, when, model in recent]

    out += ["", "（本报告不含任何 API key，可直接贴出来求助。）"]
    return "\n".join(out)
