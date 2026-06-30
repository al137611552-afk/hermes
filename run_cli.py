"""命令行入口脚本（FR-11.7）：免安装直接跑无头 CLI。

把 src 加入路径后转交 agentcore.cli。等价于装好后用的 `hermes-cli`，但**无需 pip install**：
    python run_cli.py "把测试跑一遍并报告" -w ./myproj
    python run_cli.py "梳理架构给方案" -w . --plan
    echo "调研这个项目" | python run_cli.py -w . -

（GUI 入口是 run.py / `python -m agentcore.app`；本脚本是它的命令行对应物。）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from agentcore.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
