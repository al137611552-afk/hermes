# 打包成 Windows exe（P7 / FR-7.1）

把 hermes-dev 打包成**免 Python 环境、双击即用**的 exe（onedir 形态）。
**必须在 Windows 上构建**（PyInstaller 产物是平台相关的，Linux 上生成不了 Windows exe）。

## 一键构建
在项目根目录的 PowerShell 里：
```powershell
.\build.ps1
```
它会：`pip install -e .` → `pip install pyinstaller` → `pyinstaller --noconfirm hermes-dev.spec`。
产物在 **`dist\hermes-dev\`**，运行 **`dist\hermes-dev\hermes-dev.exe`**。

（等价手动命令：`pip install -e .; pip install pyinstaller; pyinstaller --noconfirm hermes-dev.spec`）

## 打包后的目录布局
```
dist\hermes-dev\
  hermes-dev.exe        ← 双击运行
  _internal\            ← 依赖（PyInstaller 自动生成，别动）
  config.yaml           ← 首次运行自动释放，可编辑（改模型/开关）
  .env                  ← 你手动放进来（含 ARK_API_KEY 等密钥）
  data\                 ← 自动生成（会话库 / 记忆库 / 工作区）
  scripts\              ← mcp_echo_server.py 等
```
- **只读资源**（前端 web/、默认 config.yaml）打进 exe 内；
- **可写文件**（config.yaml / .env / data/）在 exe 旁边，便于编辑与持久。

## 首次运行
1. 跑一次 `hermes-dev.exe` → 它会在同目录释放默认 `config.yaml`。
2. 把你的 **`.env`** 放到 `dist\hermes-dev\` 下（不放则没有 API key、发消息会报错）。
3. 再次运行即可。

## 前置依赖
- **WebView2 运行时**：现代 Win10/11 基本自带（Edge 带来）。若启动报缺 WebView2，
  装一下微软的「Evergreen WebView2 Runtime」即可。exe 本身**不含 Python**。
- **Git for Windows**（2.0.0 起）：git 工具五件套与面板 git 模式调用系统 `git` 命令，
  exe 不捆绑；目标机未装 git 时这些功能给可读报错、其余功能不受影响。

## 调试构建问题
- spec 里 `console=True`（默认）：双击会同时弹一个黑色控制台，**能看到启动/导入报错**。
- 若报 `ModuleNotFoundError: xxx`：把 `xxx` 加到 `hermes-dev.spec` 的 `hiddenimports` 再重打。
- 一切正常后，把 spec 里 `console=True` 改成 `False`，重打一次去掉黑窗，即发布版。

## 分发
把整个 `dist\hermes-dev\` 文件夹压缩成 zip 发给别人即可（对方解压、放自己的 .env、双击运行）。
