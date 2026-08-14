"""启动期故障的人话翻译（纯逻辑，可脱离 Windows/GUI 单测）。

**为什么需要这一层**（2026-08-14 CI 首个自动发版的包踩到）：从网上下载的 zip，Windows 解压时会给
每个文件盖「来自 Internet」的标记（NTFS 的 `Zone.Identifier` 备用数据流，俗称 Mark of the Web），
而 .NET Framework **拒绝从被标记的文件加载程序集** → pywebview 导入 winforms 时炸在
`Failed to resolve Python.Runtime.Loader.Initialize`。文件本身完好（与官方 wheel 字节一致），
纯粹是被拦。而 spec 里 `console=False`，用户看到的是 PyInstaller 弹的原始 traceback 对话框——
**一串 .NET 堆栈对普通用户等于没有信息**，而这个故障恰恰是一条命令就能自救的。

只翻译"用户自己动手能解决"的那一类；认不出来的原样抛出，**别把真异常吃掉**。
"""
from __future__ import annotations

# 认这个故障的特征串。取自 pythonnet/clr_loader 的调用链，不依赖具体版本的措辞。
_CLR_MARKERS = ("Python.Runtime", "clr_loader", "pythonnet")


def _chain(exc: BaseException) -> list[BaseException]:
    """异常自身 + 它的 __cause__/__context__ 链（真因常被包在下层）。"""
    seen: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in seen:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    return seen


def looks_like_blocked_clr(exc: BaseException) -> bool:
    """这是不是「.NET 组件加载不起来」那一类失败。"""
    text = " ".join(f"{type(e).__name__}: {e}" for e in _chain(exc))
    return any(m in text for m in _CLR_MARKERS)


def clr_load_hint(exc: BaseException, app_dir: str) -> str | None:
    """认出来就给一段可照做的说明；认不出来返回 None（调用方应原样抛出）。"""
    if not looks_like_blocked_clr(exc):
        return None
    return (
        "Hermes 无法启动：加载 .NET 组件失败。\n\n"
        "最常见的原因是这个程序从网上下载而来，Windows 给解压出的文件盖了「来自 Internet」的\n"
        "标记，.NET 会拒绝加载被标记的文件（程序文件本身是好的，只是被拦住了）。\n\n"
        "解决办法——用 PowerShell 对整个程序目录跑一次：\n\n"
        f"    Get-ChildItem '{app_dir}' -Recurse | Unblock-File\n\n"
        "然后重新运行本程序即可。（下次也可以在解压前右键 zip → 属性 → 勾选「解除锁定」，\n"
        "一步到位。）\n\n"
        "若已解除锁定仍是这个错，请确认系统的 .NET Framework 版本不低于 4.7.2。"
    )
