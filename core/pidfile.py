# -*- coding: utf-8 -*-
"""服务进程登记 —— 给控制台认人用。

为什么需要这个:
    两个服务都是用**相对路径**起的(`python -u main.py`, cwd 在各自 app 目录),
    所以进程命令行里只有 "main.py" / "guard.py", 在一堆 python 进程里根本分不清
    哪个是大号、哪个是小号。而"关掉服务"又要精确杀到自己人, 不能靠猜名字。
    于是让服务自己登记: 启动时写 pid 文件, 正常退出时删掉。

被强杀时留下的文件是"过期登记": 控制台读出来之前先用 alive() 验一次死活,
死了就当没登记, 不会出现"显示运行中却怎么都关不掉"。

用法:
    from core import pidfile
    pidfile.claim(os.path.join(HERE, 'main.pid'))
    ...
    pidfile.release(os.path.join(HERE, 'main.pid'))     # atexit 也会兜一次
"""
import atexit
import os

STILL_ACTIVE = 259


def claim(path):
    """把自己的 pid 写进 path, 返回 pid; 进程退出时自动清掉。"""
    pid = os.getpid()
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, 'w', encoding='ascii') as f:
            f.write(str(pid))
    except Exception:                                       # noqa: BLE001
        return None
    atexit.register(_quiet_release, path, pid)
    return pid


def release(path):
    """只删自己的登记 —— 看门狗重启子进程时, 不删掉新一轮写的那个。"""
    _quiet_release(path, os.getpid())


def _quiet_release(path, pid):
    try:
        if read(path) == pid:
            os.remove(path)
    except Exception:                                       # noqa: BLE001
        pass


def read(path):
    """读登记里的 pid; 文件不存在/内容坏了都返回 None。"""
    try:
        with open(path, encoding='ascii') as f:
            s = f.read().strip()
        return int(s) if s.isdigit() else None
    except Exception:                                       # noqa: BLE001
        return None


def alive(pid):
    """pid 还活着吗。Windows 走 OpenProcess(别用 os.kill(pid,0), 那玩意儿在
    Windows 上会真的去 TerminateProcess——查死活变成杀进程)。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)             # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def live_pid(path):
    """登记文件里那个进程还在跑就返回 pid, 否则 None(顺手删掉过期文件)。"""
    pid = read(path)
    if pid and alive(pid):
        return pid
    if pid:
        _quiet_release(path, pid)
    return None
