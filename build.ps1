# 在 Windows 上把 hermes-dev 打包成 onedir exe。
# 用法（在项目根目录的 PowerShell 里）：  .\build.ps1
$ErrorActionPreference = "Stop"

Write-Host "==> 1/3 安装运行依赖（pip install -e .）"
pip install -e .

Write-Host "==> 2/3 安装 PyInstaller"
pip install pyinstaller

Write-Host "==> 3/3 打包（onedir）"
pyinstaller --noconfirm hermes-dev.spec

Write-Host ""
Write-Host "✅ 完成！产物在 dist\hermes-dev\"
Write-Host "   运行：dist\hermes-dev\hermes-dev.exe"
Write-Host ""
Write-Host "首次运行会在 exe 旁自动释放 config.yaml。"
Write-Host "把你的 .env 放到 dist\hermes-dev\ 目录下（.env 刻意不打进 exe——密钥不进产物）。"
Write-Host "里面写你实际用的那家 provider 的 key，变量名见「设置 → Provider」，例如 DEEPSEEK_API_KEY。"
Write-Host "data\（会话/记忆/工作区）也会生成在该目录里。"
