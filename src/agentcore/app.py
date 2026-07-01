"""入口：加载配置 -> 起 pywebview 窗口 -> 注入 Api 桥。"""
from __future__ import annotations

import os
import sys
import time

import webview

from .bridge import Api
from .config import load_config
from .paths import bundled


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
    webview.start(debug=debug)

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
