Hermes —— 首次运行必读
======================

如果双击 hermes-dev.exe 弹出一串以
    RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize
结尾的错误，请照下面做一次，之后就正常了。

原因
----
这个程序是从网上下载的。Windows 会给下载来的压缩包解出的每个文件盖一个
「来自 Internet」的标记，而程序用到的 .NET 组件拒绝从被标记的文件加载。
程序文件本身是完好的，只是被系统拦住了。

解决办法（二选一）
------------------
【A】解压之后：在这个目录上点右键 →「在终端中打开」（或开 PowerShell 切到这里），
     粘贴运行：

         Get-ChildItem . -Recurse | Unblock-File

     然后重新双击 hermes-dev.exe。

【B】解压之前：右键那个 .zip →「属性」→ 勾选右下角的「解除锁定」→ 确定，再解压。
     这样解出来的文件不带标记，一步到位。

注意：要对**整个目录**解除锁定（上面命令里的 -Recurse 就是干这个的），
只解锁单个文件不够，_internal 目录下有几百个文件都被盖了标记。

其它
----
- 首次运行 SmartScreen 可能拦一下（本程序未做代码签名），选「仍要运行」。
- 若已按上面解除锁定仍报同样的错，请确认系统的 .NET Framework 版本不低于 4.7.2。
- 把你的 .env（API key）放在 hermes-dev.exe 旁边即可；密钥刻意不打进程序。
