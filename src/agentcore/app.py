"""入口：加载配置 -> 起 pywebview 窗口 -> 注入 Api 桥。"""
from __future__ import annotations

import os
import sys
import time

import webview

from .bridge import Api
from .config import load_config
from .paths import APP_DIR, bundled
from .startup import clr_load_hint, unblock_result_message, unblock_tree


def _message_box(body: str, title: str, flags: int) -> int | None:
    """弹 Windows 系统对话框。返回按钮 id；非 Windows 或弹窗失败返回 None。

    **发布版是 `console=False`**，往 stderr 打等于没打——所以必须走对话框。
    但 stderr 照打一份：源码模式/重定向日志时还能看见。
    """
    print(f"[{title}]\n{body}", file=sys.stderr, flush=True)
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return int(ctypes.windll.user32.MessageBoxW(0, body, title, flags))
    except Exception:  # noqa: BLE001  弹窗失败不能盖住原始错误
        return None


def _show_fatal(title: str, body: str) -> None:
    _message_box(body, title, 0x10)  # MB_ICONERROR


def _ask_yes_no(title: str, body: str) -> bool:
    """问一句是/否。非 Windows 或弹窗失败＝当作「否」，绝不替用户默认同意。"""
    MB_YESNO, MB_ICONWARNING, IDYES = 0x4, 0x30, 6
    return _message_box(body, title, MB_YESNO | MB_ICONWARNING) == IDYES


def main() -> None:
    # HERMES_DEBUG=1：开 devtools + 打印启动计时探针；默认关，普通启动安静。
    debug = os.environ.get("HERMES_DEBUG", "").lower() in ("1", "true", "yes")

    t0 = time.perf_counter()
    try:
        config = load_config()
    except Exception as e:  # noqa: BLE001
        print(f"[启动失败] {e}", file=sys.stderr)
        sys.exit(1)
    t1 = time.perf_counter()

    api = Api(config)
    t2 = time.perf_counter()
    if debug:
        print(f"[启动计时] load_config={ (t1 - t0) * 1000:.0f}ms  "
              f"Api.__init__={(t2 - t1) * 1000:.0f}ms", file=sys.stderr, flush=True)

    index = bundled("web", "index.html")  # 前端是只读捆绑资源（打包后在 exe 内）

    window = webview.create_window(
        title="Hermes",  # 系统标题栏固定「Hermes」，不随项目变（项目名只在应用内顶栏显示）
        url=str(index),
        js_api=api,
        width=1100,
        height=820,
        min_size=(720, 560),
    )
    api._window = window

    # 注：之前那串 `window.native.AccessibilityObject...` RecursionError + WebView2 COM 跨线程错误，
    # 根因是 pywebview 序列化 js_api 时扎进了我们存的 `Api._window`（pywebview Window→原生对象图）；
    # 已把该引用改为下划线私有（pywebview 跳过 `_` 开头属性）从源头消除，与 debug 开关无关。
    if debug:
        print("[启动计时] 交给 WebView2 渲染页面、建桥…（下面是前端上报的耗时）",
              file=sys.stderr, flush=True)
    try:
        webview.start(debug=debug)
    except Exception as e:  # noqa: BLE001
        # 只拦「.NET 组件加载不起来」这一类可自救的失败（详见 startup 模块的注释）。
        # 认不出来就原样抛出——交回 PyInstaller 的 traceback 对话框，别把真异常吃掉。
        hint = clr_load_hint(e, str(APP_DIR))
        if hint is None:
            raise
        # 问过再动手：这等于替用户抹掉一个安全标记，范围虽只限本程序目录，也不该默认同意。
        if _ask_yes_no("Hermes 启动失败", hint):
            cleared, failed = unblock_tree(str(APP_DIR))
            _show_fatal("Hermes", unblock_result_message(cleared, failed, str(APP_DIR)))
        sys.exit(1)

    # 窗口已正常关闭、start() 返回后收尾：后台整理一次记忆，最多等 5s 不阻塞退出
    # （慢/挂就放弃——靠 extracted_upto「成功才推进」保证不丢、下次切换会话补），再关 MCP / 存储。
    import threading as _th

    def _flush_memory():
        try:
            api.active.capture_sync()
        except Exception:  # noqa: BLE001
            pass

    _flush_t = _th.Thread(target=_flush_memory, daemon=True)
    _flush_t.start()
    _flush_t.join(5)
    api.close()


if __name__ == "__main__":
    main()
