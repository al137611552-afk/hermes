"""Windows Job Object 杀树诊断（仅 Windows 有意义）。

用途：当 `run_powershell {"command":"Start-Process notepad; Start-Sleep 120"}` 超时后记事本仍残留时，
独立复现「建 job → 起 powershell（内部 Start-Process notepad）→ 并入 job → TerminateJobObject 整组杀」，
逐步打印每个 WinAPI 的成败与 GetLastError，定位到底卡在哪：

  - CreateJobObject / SetInformationJobObject 失败  → job 建不出来（权限/环境）
  - AssignProcessToJobObject 失败                    → 句柄问题（本次已修 64 位截断）
  - IsProcessInJob=False                             → powershell 没进 job
  - 上面都 OK 但 notepad 没关                        → notepad 被 ShellExecute 重定父到 job 外（需换策略）

跑法（在 hermes 目录，PowerShell）：  python scripts\\diag_jobkill.py
观察：脚本会开一个记事本，约 5 秒后打印 "TerminateJobObject ..."，**看记事本是否随之关闭**。
"""
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes

if sys.platform != "win32":
    print("此诊断仅在 Windows 有意义。")
    sys.exit(0)

H = wintypes.HANDLE
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateJobObjectW.restype = H
k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
k32.SetInformationJobObject.restype = wintypes.BOOL
k32.SetInformationJobObject.argtypes = [H, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
k32.AssignProcessToJobObject.restype = wintypes.BOOL
k32.AssignProcessToJobObject.argtypes = [H, H]
k32.IsProcessInJob.restype = wintypes.BOOL
k32.IsProcessInJob.argtypes = [H, H, ctypes.POINTER(wintypes.BOOL)]
k32.TerminateJobObject.restype = wintypes.BOOL
k32.TerminateJobObject.argtypes = [H, wintypes.UINT]
k32.CloseHandle.argtypes = [H]


class _LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IOC(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in
                ("Read", "Write", "Other", "ReadT", "WriteT", "OtherT")]


class _EXT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _LIMIT), ("IoInfo", _IOC),
        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def main():
    job = k32.CreateJobObjectW(None, None)
    print(f"CreateJobObject       -> {job!r}  err={ctypes.get_last_error()}")
    if not job:
        return
    info = _EXT()
    info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    print(f"SetInformationJobObj  -> {bool(ok)}  err={ctypes.get_last_error()}")

    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", "Start-Process notepad; Start-Sleep 30"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    print(f"powershell pid        -> {proc.pid}")
    ok = k32.AssignProcessToJobObject(job, int(proc._handle))
    print(f"AssignProcessToJob    -> {bool(ok)}  err={ctypes.get_last_error()}")

    in_job = wintypes.BOOL()
    k32.IsProcessInJob(int(proc._handle), job, ctypes.byref(in_job))
    print(f"IsProcessInJob(ps)    -> {bool(in_job.value)}")

    print("等 5 秒让记事本打开…")
    time.sleep(5)
    print(">>> TerminateJobObject（看记事本是否随之关闭）")
    k32.TerminateJobObject(job, 1)
    k32.CloseHandle(job)
    time.sleep(2)
    print("结束。若记事本已关＝job 杀树生效；若仍在＝notepad 逃出了 job（把上面各行结果发回）。")


if __name__ == "__main__":
    main()
