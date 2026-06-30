"""PyInstaller 入口脚本：把 src 加入路径后启动 agentcore.app。

源码模式下 `python run.py` 也能跑；打包时 PyInstaller 以此为入口。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from agentcore.app import main  # noqa: E402

if __name__ == "__main__":
    main()
