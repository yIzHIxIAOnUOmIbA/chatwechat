# -*- coding: utf-8 -*-
"""账号指纹: 由"微信进程自己打开的账号目录"判断某个窗口属于哪个号。

为什么要这一层(双开实测 2026-11-02)
------------------------------------
本机同时登录两个号, 两个主窗:
  · 标题都叫「微信」, 类名同为 Qt51514QWindowIcon, 尺寸各自记忆(684x911 / 1340x648);
  · 启动命令行只差一个 --scene=taskbarpins —— 取决于从任务栏还是 exe 启动, 与账号无关;
  · 两个号的昵称/头像还被设成同一张图, 会话列表头像模板会在两个窗口**同时命中**
    (实测 196734 score=1.0000 / 132646 score=0.9991), 等于没有区分力;
  · hwnd 硬编码能用但随微信重启失效(实测踩过: 失效后只能拒发, 要人工改 config)。

唯一与渲染、标题、启动方式都无关的硬证据是: 每个实例都持着自己账号目录
(Documents\\xwechat_files\\wxid_xxx_xxxx)下的一批文件句柄。于是
    hwnd -> pid -> 该 pid 打开的文件路径 -> wxid_xxx_xxxx
与本频道 config.WX_ROOT 的目录名比对, 一致即"这是我的窗口"。
实测: pid 13836 -> wxid_example_acct_0f40(小号), pid 12788 -> wxid_example_target_7ea9(大号)。

三个坑(都踩过, 别改回去)
-------------------------
1. NtQuerySystemInformation 必须 restype=c_long。默认按 c_int 解释, 0xC0000004(缓冲区
   不够)会变成负数 -1073741820, 拿去做 != 判断就再也匹配不上长度不足分支。
2. 结构体字段一律用整数(c_ulonglong)。用 c_void_p 跟 int 比较在某些路径上会崩
   (0xC0000005), 而且崩得没有 traceback, 只看到退出码 -1073741819。
3. 返回的 ENTRY 数组指向那块缓冲区, 缓冲区必须由模块持有引用(_KEEP_ALIVE)。
   否则函数一返回就是野指针, 读第一条就崩。
"""
import ctypes
import ctypes.wintypes as wt
import msvcrt
import os
import re
import time

__all__ = ['account_of_pid', 'account_of_hwnd', 'find_account_hwnd', 'describe']

_ntdll = ctypes.WinDLL('ntdll', use_last_error=True)
_k32 = ctypes.WinDLL('kernel32', use_last_error=True)

_ntdll.NtQuerySystemInformation.restype = ctypes.c_long
_ntdll.NtQuerySystemInformation.argtypes = [
    wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]
_ntdll.NtQueryObject.restype = ctypes.c_long
_ntdll.NtQueryObject.argtypes = [
    wt.HANDLE, wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)]
_k32.OpenProcess.restype = wt.HANDLE
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.DuplicateHandle.argtypes = [wt.HANDLE, wt.HANDLE, wt.HANDLE,
                                ctypes.POINTER(wt.HANDLE), wt.DWORD, wt.BOOL, wt.DWORD]
_k32.DuplicateHandle.restype = wt.BOOL
_k32.GetCurrentProcess.restype = wt.HANDLE

_SYS_EXT_HANDLE = 0x40            # SystemExtendedHandleInformation
_LEN_MISMATCH = 0xC0000004
_PROCESS_DUP_HANDLE = 0x0040
_DUP_SAME_ACCESS = 0x0002
_ACCT_RE = re.compile(r'xwechat_files[\\/](wxid_[^\\/]+)', re.I)
_CACHE_TTL = 60.0                 # pid -> 账号 的缓存秒数(微信重启会换 pid, 过期即重查)

_KEEP_ALIVE = []
_cache = {}                       # pid -> (账号目录名, 时间戳)
_file_type = [None]


class _Entry(ctypes.Structure):
    _fields_ = [('Object', ctypes.c_ulonglong),
                ('UniqueProcessId', ctypes.c_ulonglong),
                ('HandleValue', ctypes.c_ulonglong),
                ('GrantedAccess', wt.ULONG),
                ('CreatorBackTraceIndex', wt.USHORT),
                ('ObjectTypeIndex', wt.USHORT),
                ('HandleAttributes', wt.ULONG),
                ('Reserved', wt.ULONG)]


class _UnicodeString(ctypes.Structure):
    _fields_ = [('Length', wt.USHORT), ('MaximumLength', wt.USHORT),
                ('Buffer', ctypes.c_ulonglong)]


def _handles():
    size = 4 << 20
    need = wt.ULONG()
    while True:
        buf = ctypes.create_string_buffer(size)
        st = _ntdll.NtQuerySystemInformation(_SYS_EXT_HANDLE, buf, size,
                                             ctypes.byref(need))
        if (st & 0xFFFFFFFF) == _LEN_MISMATCH:
            size = need.value + (1 << 20)
            continue
        if st != 0:
            raise OSError(f'NtQuerySystemInformation=0x{st & 0xFFFFFFFF:08X}')
        count = ctypes.c_ulonglong.from_address(ctypes.addressof(buf)).value
        arr = (_Entry * count).from_address(ctypes.addressof(buf) + 16)
        _KEEP_ALIVE.append(buf)          # 见模块文档"坑 3"
        return arr


def _file_type_index():
    """借本进程打开的一个普通文件, 反查"File"对象的 ObjectTypeIndex。

    只查这一种类型: 管道/同步对象上 NtQueryObject 可能卡住(挂起), 而文件对象不会。
    """
    if _file_type[0] is not None:
        return _file_type[0]
    f = open(os.path.abspath(__file__), 'rb')
    try:
        hv = msvcrt.get_osfhandle(f.fileno())
        mine = _k32.GetCurrentProcessId()
        for e in _handles():
            if e.UniqueProcessId == mine and e.HandleValue == hv:
                _file_type[0] = e.ObjectTypeIndex
                break
    finally:
        f.close()
    return _file_type[0]


def _handle_path(pid, value):
    hproc = _k32.OpenProcess(_PROCESS_DUP_HANDLE, False, pid)
    if not hproc:
        return None
    out = wt.HANDLE()
    try:
        if not _k32.DuplicateHandle(hproc, wt.HANDLE(value), _k32.GetCurrentProcess(),
                                    ctypes.byref(out), 0, False, _DUP_SAME_ACCESS):
            return None
        buf = ctypes.create_string_buffer(8192)
        need = wt.ULONG()
        if _ntdll.NtQueryObject(out, 1, buf, 8192, ctypes.byref(need)) != 0:
            return None
        us = _UnicodeString.from_buffer(buf)
        if not us.Buffer or not us.Length or us.Length > 8000:
            return None
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    finally:
        if out:
            _k32.CloseHandle(out)
        _k32.CloseHandle(hproc)


def account_of_pid(pid):
    """返回该进程正在使用的账号目录名(如 wxid_example_acct_0f40), 判不出返回 ''。"""
    if not pid:
        return ''
    hit = _cache.get(pid)
    if hit and time.time() - hit[1] < _CACHE_TTL:
        return hit[0]
    fti = _file_type_index()
    found = ''
    try:
        for e in _handles():
            if e.UniqueProcessId != pid or e.ObjectTypeIndex != fti:
                continue
            path = _handle_path(pid, e.HandleValue)
            if not path:
                continue
            m = _ACCT_RE.search(path)
            if m:
                found = m.group(1)
                break
    except Exception:                                        # noqa: BLE001
        found = ''
    _cache[pid] = (found, time.time())
    return found


def account_of_hwnd(hwnd):
    """该窗口所属进程正在用的账号目录名, 判不出返回 ''。"""
    pid = wt.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
    return account_of_pid(pid.value)


def _main_windows():
    """所有"微信主窗口"候选(含被收进托盘而不可见的), 排除独立聊天窗。

    聊天窗与主窗同类名, 标题是联系人备注; 主窗标题是「微信」。收进托盘时
    IsWindowVisible 为假, 但窗口仍在 —— 账号指纹不需要它可见, 所以照样枚举。
    """
    u = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(h, _l):
        b = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(h, b, 256)
        if 'QWindowIcon' not in b.value:
            return True
        t = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(h, t, 256)
        if t.value in ('微信', 'WeChat') or not t.value:
            out.append(h)
        return True

    u.EnumWindows(_cb, 0)
    return out


def find_account_hwnd(own_dir, hwnds=None):
    """在候选主窗里找出"属于自己这个号"的那个, 返回 (hwnd, 依据) 或 (0, 原因)。

    own_dir: 本账号目录名(config.WX_ROOT 的 basename, 如 wxid_example_acct_0f40)。
    hwnds:   候选窗口; 不给则自己枚举所有微信主窗(含托盘里的)。
    只在**唯一命中**时返回: 命中多个或零个都返回 0 —— 双开下"挑一个"就是发错账号。
    """
    own = os.path.basename(str(own_dir or '').rstrip('\\/')).strip().lower()
    if not own:
        return 0, '没有本账号目录名, 无法按进程识别'
    cands = list(hwnds) if hwnds else _main_windows()
    hits = []
    detail = []
    for h in cands:
        got = account_of_hwnd(h)
        detail.append(f'{h}={got or "?"}')
        if got and got.lower() == own:
            hits.append(h)
    if len(hits) == 1:
        if not ctypes.windll.user32.IsWindowVisible(hits[0]):
            # 静默投递要有像素, 托盘里的主窗先不抢焦点地显示出来
            ctypes.windll.user32.ShowWindow(wt.HWND(hits[0]), 4)
            time.sleep(0.4)
        return hits[0], f'进程账号指纹命中({own})'
    if len(hits) > 1:
        return 0, f'{len(hits)} 个窗口都报同一账号, 无法区分: {", ".join(map(str, hits))}'
    return 0, f'候选窗口里没有 {own}({"; ".join(detail) or "无候选"})'


def describe(hwnds):
    """把候选窗口和它们的账号打成一句人话, 供日志/报错用。"""
    return '; '.join(f'{h}({account_of_hwnd(h) or "?"})' for h in hwnds) or '无候选'
