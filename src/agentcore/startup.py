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
        "要现在自动解除锁定吗？\n"
        f"（只处理本程序自己的目录：{app_dir}，不碰任何其它位置）\n\n"
        "选「否」也可以自己动手——用 PowerShell 跑：\n"
        f"    Get-ChildItem '{app_dir}' -Recurse | Unblock-File\n\n"
        "若已解除锁定仍是这个错，请确认系统的 .NET Framework 版本不低于 4.7.2。"
    )


# NTFS 的「来自 Internet」标记就存在这个备用数据流里，删掉它＝Unblock-File 干的事。
ZONE_STREAM = "Zone.Identifier"


def unblock_tree(root: str, *, walk=None, remove=None) -> tuple[int, int]:
    """清掉 root 下所有文件的「来自 Internet」标记。返回 (清掉几个, 失败几个)。

    **只删这一个备用数据流，不碰文件内容本身**——与 `Unblock-File` 等价，范围限本程序目录。
    `walk`/`remove` 可注入，好让这段在非 Windows 上也能真测（真机上就是 os.walk / os.remove）。
    """
    import os

    walk = walk or os.walk
    remove = remove or os.remove

    cleared = failed = 0
    for dirpath, _dirnames, filenames in walk(root):
        for fn in filenames:
            try:
                remove(os.path.join(dirpath, fn) + ":" + ZONE_STREAM)
            except FileNotFoundError:
                pass          # 这个文件本来就没标记，正常
            except OSError:
                failed += 1   # 没权限/被占用等，如实计数，别假装成功
            else:
                cleared += 1
    return cleared, failed


def unblock_result_message(cleared: int, failed: int, app_dir: str) -> str:
    """把解锁结果翻成人话。**三种结局分开说**——含糊的「已处理」会让用户不知道下一步做什么。"""
    if failed:
        return (
            f"部分文件没能解除锁定（成功 {cleared} 个，失败 {failed} 个）。\n\n"
            "多半是权限不够。请以管理员身份打开 PowerShell 跑：\n"
            f"    Get-ChildItem '{app_dir}' -Recurse | Unblock-File\n\n"
            "或者把整个程序目录移到你的用户目录下（例如桌面）再试。"
        )
    if cleared:
        return (
            f"已解除 {cleared} 个文件的锁定。\n\n请重新启动 Hermes。"
        )
    return (
        "没有找到「来自 Internet」标记，所以问题可能另有原因。\n\n"
        "请确认系统的 .NET Framework 版本不低于 4.7.2；若仍打不开，"
        "把这个提示连同报错一起反馈给开发者。"
    )
