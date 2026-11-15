# -*- coding: utf-8 -*-
"""电脑操作工具集(imhere): 键鼠模拟 / Win+R / 命令执行 / 截屏 OCR / 联网搜索

本模块是**唯一的工具清单出口**(相当于 MCP 里的工具注册表):
    TOOL_SPECS  —— 喂给模型的 schema 清单(pc_tools 原生 + net_tools 联网)
    dispatch()  —— 把「工具名 + 参数」映射成真实操作, 异常一律转文本
供 agent.py 以「工具调用」方式驱动, 让助手真的能操作这台电脑。

实现来源说明(两套桌面工具已实测可用, 这里直接沿用其结论):
  * 键盘: keybd_event —— SendInput 在 64 位 Python 下报错 87, 不可用
  * 鼠标: SendInput + 绝对坐标 + 进程 DPI 感知 —— 与 Desktop\\CoordHotkeyService\\
    click_terminal.py 同源(不设 DPI 感知时高缩放下坐标会被虚拟化缩放, 点击错位)
  * OCR: sender._ocr_window_text (Windows 自带 winrt OCR 引擎, 中文可用)
"""
import ctypes
import os
import re
import subprocess
import sys
import threading
import time

from PIL import ImageGrab

import config
import net_tools
import sender          # 抢焦点锁的实体在 sender 那边(见它文件头的 INPUT_LOCK 注释)
import skills          # SKILL.md 技能表(use_skill / list_skills)
import subagent        # 子代理(只查不动手), 见 subagent.py 文件头的安全边界
import chain_bridge    # 链式代理(受控本地任务, 过闸门+落审计), 默认关闭, 见 chain_bridge.py

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

# ---------------- DPI 感知: 坐标用物理像素, 否则点击会错位 ----------------
_dpi_ok = False
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_DPI_AWARE
    _dpi_ok = True
except Exception:
    try:
        user32.SetProcessDPIAware()
        _dpi_ok = True
    except Exception:
        pass

KEYEVENTF_KEYUP = 0x0002


# ============================================================
# 键盘
# ============================================================
VK_MAP = {
    'ctrl': 0x11, 'control': 0x11, 'alt': 0x12, 'shift': 0x10, 'win': 0x5B,
    'enter': 0x0D, 'return': 0x0D, 'esc': 0x1B, 'escape': 0x1B, 'tab': 0x09,
    'space': 0x20, 'backspace': 0x08, 'delete': 0x2E, 'del': 0x2E,
    'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
    'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pagedown': 0x22,
    'insert': 0x2D, 'capslock': 0x14, 'printscreen': 0x2C,
    '-': 0xBD, '=': 0xBB, '[': 0xDB, ']': 0xDD, '\\': 0xDC,
    ';': 0xBA, "'": 0xDE, ',': 0xBC, '.': 0xBE, '/': 0xBF, '`': 0xC0,
}
for _i in range(1, 13):
    VK_MAP[f'f{_i}'] = 0x6F + _i          # F1=0x70
for _c in 'abcdefghijklmnopqrstuvwxyz':
    VK_MAP[_c] = ord(_c.upper())
for _c in '0123456789':
    VK_MAP[_c] = ord(_c)


def _key(vk, up=False):
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def _vk_of(name):
    n = name.strip().lower()
    if n in VK_MAP:
        return VK_MAP[n]
    if len(n) == 1:
        return ord(n.upper())
    raise ValueError(f'不认识的按键: {name}')


def _send_keys_raw(keys):
    """直接发按键, 不过微信护栏。只给"系统级热键"用(目前只有 win_run 的 Win+R)。

    Win+R 是 Explorer 直接截获的系统热键: 按键在进任何窗口之前就被系统拿走了,
    不可能落进微信聊天框。而走 send_keys 的话, 微信只要在前台就被整条拒发 ——
    于是运行框永远呼不出来。实测 10-07 18:34 和 18:42 两次失败的真身就是这个
    (旧代码还把失败原因错报成 UIPI/注入被拦截, 排查方向全被带偏)。

    往窗口里"输入内容"绝不能走这里 —— 那还是要过 send_keys 的护栏。
    """
    parts = [p for p in keys.replace(',', '+').split('+') if p.strip()]
    if not parts:
        raise ValueError('按键为空')
    vks = [_vk_of(p) for p in parts]
    for v in vks:
        _key(v)
        time.sleep(0.05)
    for v in reversed(vks):
        _key(v, up=True)
        time.sleep(0.05)
    return f'已发送按键 {keys}'


def send_keys(keys):
    """发送按键/组合键。支持 'enter' / 'ctrl+s' / 'ctrl,shift,esc' 三种写法。"""
    wx = _wechat_in_front()
    if wx:
        return _WX_REFUSE.format(title=wx)
    return _send_keys_raw(keys)


def _wechat_in_front():
    """前台窗口如果是微信主窗口, 返回它的标题; 否则返回 ''。

    聊天输入框就在这个窗口里, 而 sender 每次发送都会把微信置前 —— 于是"往当前
    焦点打字/按键"的 type_text / send_keys 极容易落进对方的聊天输入框。这道护栏
    不看提示词脸色, 直接按窗口判: 是微信就一律拒绝。
    """
    import sender
    try:
        fg = user32.GetForegroundWindow()
        if not fg:
            return ''
        for _score, h in sender._wechat_candidates():
            if h == fg:
                return _window_map().get(fg) or '微信'
    except Exception:                                       # noqa: BLE001
        pass
    return ''


_WX_REFUSE = ('不能往微信窗口({title})里打字/按键: 聊天输入框就在里面, 敲进去的'
              '东西会跟着下一条消息一起发给对方(实测: 261 字符的代码就是这么发出去的)。'
              '要生成文件请用 write_file 把内容整份写到磁盘; 要往别的窗口输入, '
              '先用 focus_window 把那个窗口切到前台。')

# "整段程序源码"的判定词。命中 >=2 个且带换行, 就当代码处理
_CODE_MARKS = ('import ', 'from ', 'def ', 'class ', 'print(', 'input(', 'while ',
               'for ', '#include', 'function ', 'console.log', 'public static',
               'printf', 'echo ', '<?php', 'var ', 'select ')


def _looks_like_code(text):
    """这段文本像不像"一段程序源码"。

    为什么在工具层拦: 生成文件是这助手的高频活, 而 4B 模型对"写文件"的理解就是
    "开记事本把它敲进去" —— 实测两次都这样(17:37 敲完还说"我先用记事本看看有没有
    写错的地方"就交付, 桌面上一个 .py 都没有)。文件没落盘, 内容却在屏幕上滚过一遍,
    还可能被下一条消息一起发出去。提示词里写明用 write_file 也拦不住, 所以这里按
    内容判定, 把整段代码的 type_text 顶回去, 逼它走 write_file。
    """
    t = str(text or '')
    if '\n' not in t:
        return False
    low = t.lower()
    return sum(1 for m in _CODE_MARKS if m in low) >= 2


def type_text(text, paste=True):
    """输入文本。默认走剪贴板 + ctrl+V —— 中文/长文本只能这么输(VK 码输不了中文)。"""
    import sender                      # 复用已验证的剪贴板实现(带写入自检)
    wx = _wechat_in_front()
    if wx:
        return _WX_REFUSE.format(title=wx)
    if _looks_like_code(text):
        return ('这段内容看着是代码: 别用记事本 + type_text 去"写文件" —— 那样磁盘上'
                '什么都不会有(文件不落盘, 记事本还多半没保存)。用 write_file(path, '
                'content) 一次写完, 路径写全, 例如 C:\\Users\\<用户名>\\Desktop\\guess.py, '
                '写完再用 read_file 复验它真的在。')
    if not paste:
        for ch in text:
            v = _vk_of(ch)
            _key(v)
            _key(v, up=True)
            time.sleep(0.02)
        return f'已键入 {len(text)} 个字符'
    sender._set_clipboard(text)
    time.sleep(0.2)
    send_keys('ctrl+v')
    return f'已粘贴 {len(text)} 个字符'


_DIALOG_CLASS = '#32770'          # Windows 标准对话框类名(运行框/错误框都是它)
WM_CLOSE = 0x0010


def _window_info():
    """{hwnd: (标题, 类名)} —— 只看可见且有标题的顶层窗口。
    必须带类名: 回车失败时系统弹出的"找不到程序"错误框, 类名和运行框一样是 #32770,
    只有标题能区分(错误框标题=命令原文, 运行框标题=运行/Run)。"""
    out = {}

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            tb = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, tb, 256)
            title = tb.value.strip()
            if title:
                cl = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cl, 256)
                out[hwnd] = (title, cl.value)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool,
                                          ctypes.c_void_p, ctypes.c_void_p)(cb), 0)
    return out


def _window_map():
    """{hwnd: 标题}"""
    return {h: t for h, (t, _c) in _window_info().items()}


def _window_pid(hwnd):
    """这个窗口属于哪个进程, 拿不到返回 0。

    run_java 判"窗口程序起来了"必须按 PID 认: 只比"有没有新窗口"会被别的程序骗 ——
    实测 10-26 跑图书管理系统时, 微信恰好冒出个新窗口, 于是报了
    "跑完了... 窗口标题 'Weixin', 可以交付了", 而真正的程序窗口根本没验证过。
    """
    try:
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:                                       # noqa: BLE001
        return 0


def _close_window(hwnd):
    """只关这一个窗口(用来收拾系统错误框, 不让它留在对方屏幕上)"""
    try:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True
    except Exception:
        return False


# 运行框标题(本地化): 中文系统是 '运行', 英文系统是 'Run'
_RUN_TITLES = ('运行', 'Run')


def _run_dialog():
    """运行框句柄; 没开(或已被关掉)返回 0"""
    for h, (t, c) in _window_info().items():
        if c == _DIALOG_CLASS and t in _RUN_TITLES:
            return h
    return 0


def _cleanup_run_dialog():
    """收尾: 把残留的运行框关掉, 别在对方屏幕上留个框(实测失败路径会留下)"""
    for _ in range(3):
        h = _run_dialog()
        if not h:
            return
        _close_window(h)
        time.sleep(0.2)


def open_app(name, wait=6.0):
    """打开程序/文件/网址 —— 打开程序请优先用它, 不要用 win_run。

    为什么不走 Win+R 打开程序(实测踩到的三个坑):
      1) 中文程序名直接失败: 运行框里填"记事本"回车 = Windows 找不到该程序。
         程序名和界面上的中文名根本不是一回事, 必须先翻译成 notepad/calc。
      2) 它要抢前台窗口, 还得赌"运行框真的弹出来了"(实测 0.4s 弹出, 但机器忙时会更久),
         赌输了输入就落到别的窗口上。
      3) 成功了没成功都返回同一句话 `已用 Win+R 执行: xxx`, 程序没打开它也这么说。
    这里改走 CreateProcess("start"), 不碰键盘、不依赖前台窗口, 并且**真的验证**:
    拿启动前后的可见窗口列表做差, 没出现新窗口就如实说不确定。
    """
    raw = str(name or '').strip().strip('"').strip("'")
    if not raw:
        return '程序名为空'
    key = raw.lower()
    for suf in ('.exe', '.lnk'):
        if key.endswith(suf):
            key = key[:-len(suf)]
    exe = (config.APP_ALIASES.get(key) or config.APP_ALIASES.get(raw)
           or config.APP_ALIASES.get(raw.lower()) or raw)
    before = _window_info()
    # 启动方式: os.startfile(等价于在资源管理器里双击) —— 不能再用 cmd /c start +
    # CREATE_NO_WINDOW: 那样启的 cmd 自己**没有控制台**, 它 start 出来的控制台程序
    # (powershell / cmd)会继承"无控制台", 于是永远不弹窗(实测: 命令发出去了,
    # 6s 内一个新窗口都没有, 而 calc/notepad 这类 GUI 程序正常 —— 所以这个坑
    # 只在开控制台程序时暴露, 极容易误判成"模型没调工具")。
    try:
        os.startfile(exe)
    except Exception as e:
        try:
            subprocess.Popen(['cmd', '/c', 'start', '', exe],
                             creationflags=0x08000000)   # 兜底(实测仅对 GUI 程序有效)
        except Exception as e2:
            return (f'启动 {name!r}({exe}) 失败: {type(e).__name__}: {e}; '
                    f'兜底也失败: {type(e2).__name__}: {e2}')
    t0 = time.time()
    while time.time() - t0 < float(wait):
        time.sleep(0.3)
        new = {h: v for h, v in _window_info().items() if h not in before}
        # 先找系统错误框 —— 那是"程序名不存在"的铁证, 绝不能算启动成功。
        # (实测踩过: `start "" 某个不存在的程序xyz` 会弹一个标题=命令原文的 #32770 框,
        #  当时按"有没有新窗口"判断, 就把这个错误框当成了"启动成功"。)
        for h, (t, c) in new.items():
            if c == _DIALOG_CLASS and t not in _RUN_TITLES:
                _close_window(h)
                return (f'启动失败: 系统弹出错误框 {t!r} —— 说明 {name!r} 不是有效的程序名'
                        f'(已翻译为 {exe!r})。请换成真实的程序名或完整路径。')
        app = [t for _h, (t, c) in new.items() if c != _DIALOG_CLASS]
        if app:
            return (f'已启动 {name}({exe}), {time.time() - t0:.1f}s 后出现新窗口: '
                    f'{app[0]!r}')
    return (f'已发出启动命令 {name}({exe}), 但 {float(wait):.0f}s 内没看到新窗口 —— '
            f'要么它本来就在运行(那就用 focus_window 切过去), 要么这个程序名不对')


def win_run(command, wait=1.2):
    """Win+R 打开运行框 -> 输入 -> 回车。适合路径/网址/系统命令, 打开程序用 open_app。

    每一步都验证, 不再靠 sleep 猜时间: 以前猜错也照样返回"已执行", 于是出现
    "说打开了其实没打开"(最典型的就是中文程序名)。现在呼不出运行框 / 输完回车
    对话框不关, 都会如实报错并告诉模型改用什么工具。
    """
    _cleanup_run_dialog()                   # 先收拾残留的旧运行框, 否则输入会落到它上面
    # Win+R 走底层发送(不过微信护栏): 它是系统级热键, 由系统直接截获, 不会漏进微信。
    # 走 send_keys 的话微信在前台就被整条拒发, 运行框永远呼不出来 —— 见 _send_keys_raw。
    # 发一次不够稳(实测偶发不灵), 所以没弹框就收拾干净重试一次。
    h = 0
    for _attempt in (1, 2):
        _send_keys_raw('win+r')
        deadline = time.time() + max(1.5, float(wait))
        while time.time() < deadline:
            h = _run_dialog()
            if h:
                break
            time.sleep(0.1)
        if h:
            break
        _cleanup_run_dialog()
        time.sleep(0.3)
    if not h:
        return ('运行框没被呼出(Win+R 连续两次都没反应): 前台窗口可能有更高权限(UIPI)'
                '或键盘注入被拦截。打开程序改用 open_app, 执行命令改用 run_command。')
    time.sleep(0.2)
    # 输入前必须确认运行框真的拿到了前台 —— 否则 ctrl+V 会落进当时的前台窗口(最坏
    # 就是微信聊天框, 那正是护栏要防的事)。没抢到就一个字都不输, 老实说没做成。
    if user32.GetForegroundWindow() != h:
        _cleanup_run_dialog()
        return ('运行框弹出来了但没抢到前台, 没敢往里输入(怕打进微信/别的窗口里)。'
                '改用 run_command 执行命令, 或先用 focus_window 把目标窗口切到前台。')
    typed = type_text(command)
    # type_text 被微信护栏拒了 = 运行框没抢到前台, 再回车就是往微信聊天输入框里回车。
    # 这里必须中止(并把残留的运行框收掉), 不能当成"已执行"糊过去。
    if '不能往微信窗口' in typed:
        _cleanup_run_dialog()
        return f'没执行: 输入落不到运行框上({typed})'
    time.sleep(0.3)
    pre = _window_info()          # 回车前记一笔, 用来认出"回车之后才冒出来的错误框"
    send_keys('enter')
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not _run_dialog():
            break
        time.sleep(0.1)
    else:
        send_keys('esc')
        return (f'运行框里输入 {command!r} 回车后没生效(对话框没关)。'
                f'注意程序名不能用中文(要 notepad 不是"记事本"), 或者改用 open_app。')
    # 运行框关掉了不等于成功: 命令无效时系统会立刻补一个错误框(标题就是命令原文)
    time.sleep(0.4)
    for h, (t, c) in _window_info().items():
        if h not in pre and c == _DIALOG_CLASS and t not in _RUN_TITLES:
            _close_window(h)
            time.sleep(0.3)
            _cleanup_run_dialog()     # 关掉错误框后运行框常常还杵在那儿, 一并收拾
            return (f'运行框接受了 {command!r}, 但系统弹出错误框 {t!r} —— 命令并没有被执行。'
                    f'程序名别用中文(要 notepad 不是"记事本"), 或者改用 open_app。')
    # 到这里只能确认"运行框接受了并回车了"。但这条路**永远拿不到命令的输出**, 也无法
    # 确认它到底成功没有 —— 有输出的命令会另开一个窗口自己跑, 那个窗口不归我们管。
    # 原来这里只说"已用 Win+R 执行: xxx", 听起来像成了: 实测五类回归, 4B 拿它当"我拿到
    # 结果了", 顺手编了个"Python 版本是 3.9.7"交付(本机真版本 3.13.7)。必须把这一层说透,
    # 并点名 run_command。
    return (f'已用 Win+R 把 {command!r} 丢进运行框了(它另开窗口自己跑)。'
            f'**这条路拿不到任何输出, 也无法确认它成功没有** —— 你并不知道它返回了什么。'
            f'要拿命令的输出 / 版本号 / 返回值, 必须改用 '
            f'run_command(command="{command}")。')


# ============================================================
# 鼠标
# ============================================================
ULONG_PTR = ctypes.c_void_p


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long),
                ('mouseData', ctypes.c_ulong), ('dwFlags', ctypes.c_ulong),
                ('time', ctypes.c_ulong), ('dwExtraInfo', ULONG_PTR)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [('mi', MOUSEINPUT)]

    _anonymous_ = ('u',)
    _fields_ = [('type', ctypes.c_ulong), ('u', _U)]


INPUT_MOUSE = 0
M_MOVE, M_ABSOLUTE = 0x0001, 0x8000
M_VIRTUALDESK = 0x4000     # 绝对坐标按"整个虚拟桌面"算, 不按主屏 —— 多屏必带(见 move_mouse)
M_LEFTDOWN, M_LEFTUP = 0x0002, 0x0004
M_RIGHTDOWN, M_RIGHTUP = 0x0008, 0x0010
M_MIDDLEDOWN, M_MIDDLEUP = 0x0020, 0x0040
M_WHEEL = 0x0800


class POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


def screen_size():
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def cursor_pos():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _send(flags, dx=0, dy=0, data=0):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.u.mi.dx, inp.u.mi.dy = dx, dy
    inp.u.mi.mouseData = data
    inp.u.mi.dwFlags = flags
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def move_mouse(x, y):
    """把鼠标移到屏幕物理坐标 (x, y)。坐标允许是负的(左屏)。

    按**整个虚拟桌面**归一化, 不是主屏: 本机左边挂着一块 1080x1920 的竖屏, 它的
    坐标是负的(虚拟桌面 x 从 -1080 起), CodeBuddy 就摆在那边。旧写法拿主屏
    1920x1080 直接算, x=-46 会得出负的 dx —— SendInput 绝对坐标只认 0~65535,
    负数直接失效, 现象是"点了没反应"或者点歪到主屏上去。
    配上 MOUSEEVENTF_VIRTUALDESK 才是全桌面口径; 单屏时两者等价(起点就是 0,0)。
    """
    vl, vt = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    dx = int(round((int(x) - vl) * 65535.0 / max(vw - 1, 1)))
    dy = int(round((int(y) - vt) * 65535.0 / max(vh - 1, 1)))
    return _send(M_MOVE | M_ABSOLUTE | M_VIRTUALDESK, dx, dy)


def click(x, y, button='left', double=False):
    """移动到 (x,y) 再点击。x/y 为屏幕物理像素坐标(可用 screenshot+ocr 定位)。"""
    move_mouse(x, y)
    time.sleep(0.15)
    move_mouse(x, y)          # 二次精确定位, 确保停在目标点
    time.sleep(0.08)
    down, up = {'left': (M_LEFTDOWN, M_LEFTUP),
                'right': (M_RIGHTDOWN, M_RIGHTUP),
                'middle': (M_MIDDLEDOWN, M_MIDDLEUP)}.get(button, (M_LEFTDOWN, M_LEFTUP))
    times = 2 if double else 1
    for _ in range(times):
        _send(down)
        time.sleep(0.08)
        _send(up)
        if double:
            time.sleep(0.06)
    return f'已{"双击" if double else "单击"}({button}) ({x},{y})'


def scroll(amount):
    """滚轮: 正数向上, 负数向下(1 档 = 120)"""
    _send(M_WHEEL, data=int(amount) * 120)
    return f'滚轮 {amount} 档'


# ============================================================
# 命令 / 窗口 / 截屏 / OCR
# ============================================================
# ---- 危险命令 / 危险脚本内容扫描 -------------------------------------------
# H2: run_command 用 shell=True, 仅靠子串黑名单易被绕过(powershell -command /
#     powershell.exe / cmd.exe / del 各种开关)。改为"基命令精确拦截 + 子串兜底",
#     基命令还去掉 .exe 后缀, 防 powershell.exe 这类绕过。
# H1: 黑名单原来只挡 run_command, 而 run_py / run_java 直接跑文件、不过黑名单,
#     于是"写个 .py 再 run_py"能完全绕过。这里补上脚本内容扫描, 并在 run_command
#     拉起脚本文件 / 内联 -c 代码时也扫一遍。
_FORBIDDEN_BASE = {
    'powershell', 'pwsh', 'cmd', 'wscript', 'cscript', 'mshta', 'regsvr32',
    'rundll32', 'certutil', 'bitsadmin', 'schtasks', 'net', 'wmic', 'sc',
    'shutdown', 'restart-computer', 'stop-computer', 'diskpart', 'format',
    'vssadmin', 'bcdedit', 'takeown', 'icacls', 'cacls', 'reg', 'wevtutil',
    'netsh', 'dism', 'sfc', 'at', 'taskkill', 'rmdir', 'rd', 'del', 'rm',
    'fsutil', 'mountvol', 'defrag', 'cipher', 'attrib', 'erase', 'tskill',
    'psexec', 'wusa',
}

# 脚本内容里出现这些即视为危险: 系统级执行 / 删文件 / 拉外部程序。
_SCRIPT_DANGER_RES = (
    re.compile(r'os\.system|os\.popen|subprocess|shutil\.rmtree|os\.remove|'
               r'os\.unlink|os\.rmdir|os\.kill|os\._exit|eval\(|exec\(|'
               r'ctypes\.(?:windll|cdll)|__import__|getattr\(os'),
    re.compile(r'runtime\.getruntime\(\)\.exec|processbuilder|files\.delete|'
               r'file\.delete'),
    re.compile(r'\b(?:powershell|remove-item|rmdir|rd|del|shutdown|diskpart|'
               r'vssadmin|bcdedit|takeown|icacls|cacls|reg|wevtutil|netsh|'
               r'dism|sfc|certutil|bitsadmin|mshta|regsvr32|rundll32|'
               r'wscript|cscript|schtasks|cmd|curl|wget)\b'),
    re.compile(r'del\s+/[fsqa]|rmdir\s+/s|rd\s+/s|reg\s+delete|cmd\s+/[ck]|'
               r'rm\s+-r[f]?\b|format\s+[a-z]:|format\s+/[qf]|'
               r'invoke-webrequest|downloadstring|downloadfile'),
)
_SCRIPT_DANGER_EXTS = ('.py', '.java', '.ps1', '.bat', '.cmd', '.sh',
                       '.vbs', '.js')


def _script_danger(text):
    """脚本内容是否命中危险调用; 命中返回片段, 否则 None。"""
    if not text:
        return None
    low = str(text).lower()
    for rx in _SCRIPT_DANGER_RES:
        m = rx.search(low)
        if m:
            return m.group(0)
    return None


def _scan_script_file(path):
    """读脚本文件扫危险内容; 不存在/读不出返回 None(不误拦)。"""
    try:
        if not str(path).lower().endswith(_SCRIPT_DANGER_EXTS):
            return None
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return _script_danger(f.read())
    except Exception:
        return None


def _guard(command):
    raw = str(command)
    low = ' ' + re.sub(r'\s+', ' ', raw.lower()) + ' '
    # 去掉可执行文件后缀再判基命令, 防 powershell.exe / cmd.exe 绕过
    norm = re.sub(r'\.(?:exe|cmd|bat|ps1|vbs|js|com)\b', ' ', low)
    toks = norm.split()
    if toks:
        base = toks[0].strip('"\'`').rstrip(':')
        if base in _FORBIDDEN_BASE:
            raise PermissionError(f'命令基名 {base!r} 命中危险命令名单, 已拒绝执行')
    # 命令字符串本身(含 -c 内联代码)也过危险内容扫描, 挡住 os.system / 内联 rm -rf 等
    hit = _script_danger(raw)
    if hit:
        raise PermissionError(f'命令内容命中危险调用 {hit!r}, 已拒绝执行')
    # 子串兜底: 保留原 DANGEROUS_PATTERNS, 挡住写在参数里的危险片段
    for p in config.DANGEROUS_PATTERNS:
        if p.lower() in low:
            raise PermissionError(f'命令命中危险模式 {p!r}, 已拒绝执行')
    # 命令里若直接拉起脚本文件, 其内容也要过危险扫描(防投毒脚本绕过黑名单)
    for tok in re.findall(r'["\']?([\w./:\\-]+\.(?:py|ps1|bat|cmd|sh|vbs|js))'
                          r'["\']?', raw, re.I):
        hit = _scan_script_file(tok)
        if hit:
            raise PermissionError(
                f'命令拉起的脚本 {tok} 命中危险内容 {hit!r}, 已拒绝执行(防投毒脚本)')


def run_command(command, timeout=20):
    """执行系统命令并返回输出(比 Win+R 可靠: 能拿到 stdout/退出码, 且不依赖前台窗口)。"""
    _guard(command)
    try:
        r = subprocess.run(command, shell=True, capture_output=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return f'命令超时({timeout}s): {command}'
    out = (r.stdout or b'').decode('gbk', 'ignore').strip()
    err = (r.stderr or b'').decode('gbk', 'ignore').strip()
    parts = [f'退出码 {r.returncode}']
    if out:
        parts.append('输出:\n' + out[:2000])
    if err:
        parts.append('错误:\n' + err[:800])
    return '\n'.join(parts)


def list_windows():
    """列出可见且有标题的顶层窗口"""
    wm = _window_map()
    if not wm:
        return '没有可见窗口'
    return '可见窗口:\n' + '\n'.join(f'{h}: {t}' for h, t in list(wm.items())[:40])


def find_window(title):
    """按标题关键词找窗口句柄(返回 hwnd, 没找到返回 0)"""
    if not title:
        return 0
    for h, t in _window_map().items():
        if title in t:
            return h
    return 0


def focus_window(title):
    """按标题关键词激活窗口"""
    import sender
    hwnd = find_window(title)
    if not hwnd:
        # 光说"没找到"它只会原地重试同一个词。实测 13:53 打开资源管理器: open_app
        # 成功开了窗口(标题是 '-'), 它却拿"资源管理器"去 focus, 失败了还接着试 ——
        # 把当前真实的窗口标题一并给它, 它才有机会换个准确的名字。
        titles = [t for t in _window_map().values() if t][:8]
        return (f'没找到标题含 {title!r} 的窗口。当前可见窗口的真实标题有: '
                + '; '.join(titles)
                + ' —— 从里面挑一个准确的标题再试, 不要重复用刚才那个。')
    ok = sender._bring_to_front(hwnd)
    return f'{"已激活" if ok else "激活失败"}: {title!r} (hwnd={hwnd})'


# ============================================================
# 把内容送进别的窗口 —— 在微信里指挥电脑上的 CodeBuddy
# ============================================================
# 找窗口为什么按**进程名**而不是标题: 实测这台机器上 CodeBuddy CN 的主窗口标题是
# "Agents"(VS Code 系的标题跟着界面状态走), 拿 "codebuddy" 去匹配标题一个字都对不上;
# 而 exe 名是稳定的: CodeBuddy CN.exe。只按标题找是这套东西最容易失灵的地方。
_APP_ALIAS = {
    'codebuddy': ('codebuddy',), 'code buddy': ('codebuddy',),
    '编程助手': ('codebuddy',), '代码助手': ('codebuddy',),
    '微信': ('weixin',), 'wechat': ('weixin',),
}
_PROC_QUERY_LIMITED = 0x1000       # PROCESS_QUERY_LIMITED_INFORMATION


def _proc_name(hwnd):
    """这个窗口属于哪个进程(exe 名小写, 例如 'codebuddy cn.exe'); 拿不到返回 ''"""
    try:
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ''
        h = kernel32.OpenProcess(_PROC_QUERY_LIMITED, False, pid.value)
        if not h:
            return ''
        try:
            buf = ctypes.create_unicode_buffer(600)
            size = ctypes.c_ulong(600)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(h)
    except Exception:                                       # noqa: BLE001
        pass
    return ''


def _app_windows(app):
    """按 app 名找窗口 -> [(hwnd, 标题, exe 名)]。进程名先试, 标题再兜一层。"""
    key = str(app or '').strip().lower()
    if not key:
        return []
    want = _APP_ALIAS.get(key, ())
    hits = []
    for h, t in _window_map().items():
        exe = _proc_name(h)
        if (want and any(w in exe for w in want)) or key in t.lower() or \
                key.replace(' ', '') in exe.replace(' ', ''):
            hits.append((h, t, exe))
    return hits


def _app_window(app):
    """挑一个: 同一个进程可能开着好几个窗口, 取面积最大的那个(主窗口)"""
    import sender
    best = None
    for h, t, exe in _app_windows(app):
        try:
            w, ht = sender._win_size(h)
        except Exception:                                   # noqa: BLE001
            w = ht = 0
        if best is None or w * ht > best[0]:
            best = (w * ht, h, t, exe)
    return best or (0, 0, '', '')


def paste_to_app(app, text, submit=False, wait=1.2):
    """把 text 粘贴进 app 的窗口里(默认只粘贴, 不回车提交)。

    为什么要有这个工具: 对方在微信里说"让 codebuddy 干 X", 这句话本身不是让自己
    执行的活, 而是要送进电脑上开着的 CodeBuddy 窗口 —— 识别出这个意图后, 找到窗口、
    切到前台、把内容粘进去就完事。

    安全性靠"先确认目标在前台, 再按键"这条, 和 type_text 正好相反:
      * type_text 是往当前焦点敲, 所以它必须拒绝微信窗口(敲进去会发给对方);
      * 这里是先点名目标窗口, 切上去、确认前台就是它, 才发 ctrl+V。
    前台确认不了就一个键都不按 —— 宁可失败, 也不能把内容敲进别的窗口去。
    目标是微信一律拒绝: 往聊天框粘贴等于直接发给对方, 那是发消息那条链路的活。

    CodeBuddy 落在左屏时, 切到前台之后还会先点一下它的输入框(见 focus_codebuddy_input)
    —— 只切前台保证不了焦点在输入框上, 不点这一下 ctrl+V 很可能贴不进去, 后面的回车也就白发。
    """
    import sender
    if not _app_windows(app):
        titles = '; '.join(f'{t}({_proc_name(h)})'
                           for h, t in list(_window_map().items())[:8])
        return (f'没找到 {app!r} 的窗口。当前可见窗口有: {titles}'
                f' —— 换个准确的进程名或标题再试; 它要是没开着, 先用 open_app 开起来。')
    _area, hwnd, title, exe = _app_window(app)
    if 'weixin' in exe:
        return (f'{app!r} 是微信窗口({title}): 贴进聊天框的内容会直接发出去, '
                f'要往微信发消息请走发消息的接口, 不要用这个工具。')
    body = str(text or '')
    if not body.strip():
        return 'paste_to_app 没给内容(text 是空的)'
    ok = False
    for _ in range(3):
        sender._bring_to_front(hwnd)
        if user32.GetForegroundWindow() == hwnd:
            ok = True
            break
        time.sleep(0.4)
    if not ok:
        return (f'没敢粘贴: 没能把 {title!r}({exe}) 切到前台 —— 键盘现在不在它身上, '
                f'发出去的字符会落进别的窗口, 所以一个键都没按。等一下再试。')
    wx = _wechat_in_front()                    # 双保险: 万一这会儿前台又变回微信
    if wx:
        return _WX_REFUSE.format(title=wx)
    # 目标在左屏: 先点一下输入框, 否则 ctrl+V 贴不进去, 后面回车也送不出去
    focus_note = focus_codebuddy_input(hwnd, exe)
    if focus_note:
        print(f'[paste] {focus_note}')
    sender._set_clipboard(body)
    time.sleep(max(0.2, min(float(wait or 1.2), 5.0) * 0.2))
    send_keys('ctrl+v')
    time.sleep(0.4)
    done = f'已把 {len(body)} 字符粘贴到 {title!r}({exe})'
    if submit:
        time.sleep(0.3)
        send_keys('enter')
        done += '，并回车提交了'
    else:
        done += '，没有回车(需要它开始干活就再按一次回车)'
    if user32.GetForegroundWindow() != hwnd:
        done += '\n注意: 粘完之后前台窗口变了, 内容可能没进它的输入框, 值得截图看一眼。'
    return done


class _RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_ulong), ('rcMonitor', _RECT),
                ('rcWork', _RECT), ('dwFlags', ctypes.c_ulong)]


def _window_rect(hwnd):
    """窗口的屏幕矩形 (left, top, right, bottom); 拿不到返回 None。

    这是系统直接给的窗口外框位置, 不是去认里面的控件 —— 只用来把"相对窗口的坐标"
    换算成屏幕绝对坐标, 顺带让窗口挪到哪块屏都能跟住。
    """
    r = _RECT()
    try:
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
            return None
    except Exception:
        return None
    return (r.left, r.top, r.right, r.bottom)


def resolve_click(raw, hwnd=None):
    """把配置里"点哪儿"换算成屏幕绝对坐标; 换算不出来返回 None(表示不做这个动作)。

    支持两种写法(见 config.py 里 cb_left_click / cb_input_click 的说明):
      [x, y]             原样返回, 就是整块虚拟桌面的绝对坐标(左屏为负)
      {"rel": [dx, dy]}  相对目标窗口左上角; 需要 hwnd, 拿不到窗口矩形就放弃
    """
    if not raw:
        return None
    if isinstance(raw, dict):
        rel = raw.get('rel')
        if not rel or hwnd is None:
            return None
        box = _window_rect(hwnd)
        if not box:
            return None
        return (box[0] + int(rel[0]), box[1] + int(rel[1]))
    try:
        x, y = raw
        return (int(x), int(y))
    except Exception:
        return None


def _is_relative(raw):
    """坐标是不是"相对窗口"写法 —— 相对的跟窗口走, 就不必再要求窗口在左屏。"""
    return isinstance(raw, dict) and bool(raw.get('rel'))


def _monitor_left(hwnd, primary=False):
    """窗口所在显示器的左边界(虚拟桌面坐标); 拿不到返回 None。

    DEFAULTTONEAREST = 2(取离窗口最近的那块屏), DEFAULTPRIMARY = 1(主屏)。
    显示器句柄在 64 位下是指针, 不声明 argtypes 会被 ctypes 当 32 位 int 截断。
    """
    user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    flag = 1 if primary else 2
    hmon = user32.MonitorFromWindow(ctypes.c_void_p(0 if primary else hwnd), flag)
    if not hmon:
        return None
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
        return None
    return mi.rcMonitor.left


def window_on_left_screen(hwnd):
    """这个窗口是不是摆在主显示器**左边**那块屏上(那块屏的 x 是负的)。

    判据是"窗口所在显示器的左边界 < 主显示器的左边界", 而不是"坐标为负" ——
    后者只在主屏起点为 0、左屏恰好在负方向时才凑巧成立。
    """
    mine, prim = _monitor_left(hwnd), _monitor_left(0, primary=True)
    if mine is None or prim is None:
        return False
    return mine < prim


def _left_screen_prep(hwnd):
    """CodeBuddy 落在左屏时, 抓图前先在那个固定点上按一下把它叫醒。返回说明串。

    为什么非要有这一下: 左屏那块是竖屏, CodeBuddy 挂在上面时往往是非激活/被盖住的,
    后台窗口的画面未必继续刷新, 直接 PrintWindow 抓到的可能是上一帧 —— 发回微信就成
    了一张"过期截图", 比不发的误导性更大。按一下这个固定点(默认 -46,1401, 它界面
    左下那块)能让它刷出当前画面。坐标与等待时长都在 config 里, cb_left_click 设 None 即关。
    """
    pt = resolve_click(config.CB_LEFT_CLICK, hwnd)
    if not pt:
        return ''
    # 绝对坐标只在那块左屏上成立; 相对窗口的写法跟着窗口走, 不必挑屏
    if not _is_relative(config.CB_LEFT_CLICK) and not window_on_left_screen(hwnd):
        return ''
    x, y = pt
    with _INPUT_LOCK:                  # 点鼠标是抢焦点操作, 和发消息共用同一把锁
        click(x, y)
    time.sleep(config.CB_LEFT_CLICK_WAIT)
    return f'已先点一下 ({x},{y}) 把 CodeBuddy 叫醒再抓图'


def focus_codebuddy_input(hwnd, exe=''):
    """给 CodeBuddy 送字之前, 先点一下它的输入框, 让键盘焦点真的落进去。

    为什么"切到前台"还不够: _bring_to_front 只保证窗口在前面, 焦点落到窗口内部哪个
    控件上是不确定的。不点这一下, 后面的 ctrl+V 可能压根没进输入框, 紧接着回车也白发,
    对方那边看到的就是"消息石沉大海"。实测在左屏那块竖屏上要先按 (-512,1506)。

    只在两个条件都成立时才点: 目标确实是 CodeBuddy, 且它此刻就在左屏 —— 这个坐标是
    左屏的绝对坐标, 窗口挪到别的屏上就不对了。返回说明串; 什么都没做时返回 ''。
    """
    if 'codebuddy' not in str(exe or '').lower():
        return ''
    pt = resolve_click(config.CB_INPUT_CLICK, hwnd)
    if not pt:
        return ''
    # 绝对坐标是按左屏量的; 相对窗口的写法跟窗口走, 窗口在哪块屏都对
    if not _is_relative(config.CB_INPUT_CLICK) and not window_on_left_screen(hwnd):
        return ''
    x, y = pt
    with _INPUT_LOCK:                  # 抢焦点操作, 和截图/发消息共用同一把锁
        click(x, y)
    time.sleep(config.CB_INPUT_CLICK_WAIT)
    return f'已点一下 CodeBuddy 输入框 ({x},{y}), 焦点应该在输入框里了'


def screenshot_app(app='codebuddy', tag=None):
    """截某个程序的主窗口(默认 CodeBuddy)存进 shots/, 返回 PNG 路径; 失败返回 ''。

    为什么不用现成的 screenshot(): 那个是全屏 ImageGrab, 而 CodeBuddy 经常被微信
    盖住 —— 全屏抓回来的是微信的脸, 发过去对方看到的是一张无关的屏幕。这里走
    PrintWindow 抓窗口本身, 遮挡不影响, 也不会顺手把别的窗口内容拍进图里。
    窗口在左屏时, 抓之前先点一下固定点刷新画面(见 _left_screen_prep)。
    """
    if app:
        hit = _app_window(app)
        if not hit:
            return ''
        hwnd = hit[1]
        note = _left_screen_prep(hwnd)
        if note:
            print(f'[shot] {note}')
    else:
        hwnd = user32.GetForegroundWindow()
    img = sender._capture_window(hwnd)
    if img is None:
        return ''
    os.makedirs(config.SHOT_DIR, exist_ok=True)
    name = tag or (os.path.splitext(_proc_name(hwnd))[0].replace(' ', '') or 'app')
    path = os.path.join(config.SHOT_DIR, f'{name}_{time.strftime("%m%d_%H%M%S")}.png')
    img.save(path)
    return path


def shot_to_wechat(app='codebuddy'):
    """截 app 的主窗口, 直接通过微信发回给对方(截图 + 发送一条龙)。

    对应微信里那句"截个图看看 codebuddy 现在怎么样了": 先把窗口抓下来, 再走 sender
    的发图链路(激活微信 -> 搜目标会话 -> OCR 复核 -> 粘贴位图 -> 回车)。
    找不到窗口 / 抓到空白 / 发送失败, 三种都回实话, 不谎报"已发送"。
    """
    path = screenshot_app(app)
    if not path:
        return (f'没能截到 {app} 的窗口(没找到它的窗口, 或者抓到的是空白图), '
                f'这一张没发出去')
    fname = os.path.basename(path)
    if sender.send_image_to(path):
        return f'已截 {app} 的窗口并发到微信了: {fname}'
    return f'截图截到了({fname})但没发进微信, 图留在 {path}'


# ============================================================
# 静默发消息给任意联系人(独立聊天窗通道)
# ============================================================
def _wx_chat_handles():
    """不激活地拿到微信主窗口句柄列表(候选按可信度降序)。"""
    try:
        cands = sender._wechat_candidates()
    except Exception:                                            # noqa: BLE001
        return []
    return [h for _score, h in cands[:4]]


def wechat_send(contact, text):
    """给指定联系人发一条文字, 全程后台静默: 不抢前台、不碰剪贴板、不按真实键。

    与 sender.send_to 的差别: 那条要激活微信 -> ctrl+F -> 粘贴 -> 真实回车, 单次
    占前台 3~5s; 这条路走**独立聊天窗** —— 没有就先双击会话列表首行建一个, 建好后
    把窗口挪到屏幕外, 用 PostMessage 投字符 + 投回车。实测(2026-11-02) 2ms 采样
    全程 0 次前台偏离、0ms 占用, 单条 0.5~0.9s。

    代价与边界: 每个**新**联系人第一次要抢一次前台(建窗那一下 ~150ms, 微信显式
    SetForegroundWindow, 拦不住), 之后永久为 0; 而且只能建"会话列表里点得到"的人
    —— 也就是最近有聊天记录的人。建出来的窗口标题对不上 contact 时自动关窗放弃,
    绝不往错的人发。
    """
    from core import subwin as csub
    hwnds = _wx_chat_handles()
    if not hwnds:
        return '微信没在运行(找不到主窗口), 这条没发出去'

    # 双开时两个主窗的类名和标题(都叫"微信")完全一样, 只能靠"谁的会话列表里有这个人"
    # 来判断。判不出来就拒发 —— 猜错会把消息发给另一个号里的同名联系人, 比不发更糟。
    prefer = int(getattr(config, 'WECHAT_ACCOUNT_HWND', 0) or 0)
    if prefer and prefer in hwnds:
        hwnd, why = prefer, 'config 指定'
    elif len(hwnds) == 1:
        hwnd, why = hwnds[0], '只有一个微信主窗口'
    else:
        hwnd, why = csub.pick_account(
            hwnds, contact=contact,
            tmpl=getattr(config, 'WECHAT_ACCOUNT_TMPL', ''))
    if not hwnd:
        return (f'没发出去: 同时开着 {len(hwnds)} 个微信, 无法确定 {contact} 属于哪个号'
                f'(两个号都有同名的人时就会这样)。现有窗口: '
                f'{csub.describe_accounts(hwnds)}。'
                f'请在 config.json 写 "wechat_account_hwnd": <句柄> 指定用哪个号。')

    msg = f'{config.AI_PREFIX}{text}'
    info = csub.send_to_contact(hwnd, msg, contact=contact)
    s = info.get('send') or {}
    if info.get('ok'):
        how = ('复用已有聊天窗' if not info.get('opened')
               else f'新建聊天窗(抢前台 {info.get("steal_ms")}ms)')
        return (f'已发给 {contact}: {text[:40]}({how}, 号={why}, '
                f'投递 {s.get("elapsed_ms")}ms, 前台偏离 {s.get("deviation")} 次, '
                f'占用 {s.get("occupied_ms")}ms)')
    if info.get('unverified'):
        return (f'已投进 {contact} 的输入框, 但拿不到截图证据判断有没有发出去'
                f'({info.get("reason")}), 为避免重复消息没有再发一次 —— 请人工看一眼。')
    return (f'没发出去: {info.get("reason")}。'
            f'说明这个人在会话列表里找不到(独立窗只能建"最近有聊天记录"的人)。')


def wechat_windows():
    """列出当前已打开的独立聊天窗(标题就是对方昵称), 排查"能不能给他发"用。"""
    from core import subwin as csub
    hwnds = _wx_chat_handles()
    if not hwnds:
        return '微信没在运行(找不到主窗口)'
    rows = []
    for h in hwnds:
        for x in csub.sub_windows(h):
            rows.append(f'{csub.win._title(x)}@{h}'
                        f'({"屏幕外" if csub.is_parked(x) else "屏幕内"})')
    if not rows:
        return '当前没有任何独立聊天窗(还没有人建过窗口)'
    return '已打开的独立聊天窗: ' + '、'.join(rows)


_STILL_TOL = 6.0        # 画面指纹的平均差 <= 这个值就算"没在变"(0~255)


def _img_signature(img, size=(64, 40)):
    """把窗口截图压成可比较的小指纹(灰度缩略图字节串), 用来判断画面变没变。"""
    return img.convert('L').resize(size).tobytes()


def _img_diff(a, b):
    """两个指纹的平均绝对差(0~255); 长度不一致按最大差处理。"""
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def wait_codebuddy_idle(app='codebuddy'):
    """等 CodeBuddy 的画面稳定下来(≈它的活干完了), 返回它的 hwnd; 找不到窗口返回 0。

    为什么用"画面不动"判断跑完: CodeBuddy 干活时会不停刷输出, 停下来画面就基本不变。
    这比 OCR 找"停止/取消"按钮可靠得多(那些文案不固定、也未必显示), 且完全不碰它的界面。
    判稳口径: 相邻两次采样差异 <= _STILL_TOL, 且这样连续保持 config.CB_SHOT_IDLE 秒。
    一直不稳(比如活很长)就等到 config.CB_SHOT_MAX_WAIT, 照样返回 —— 调用方截当前画面,
    有张"还在跑"的图也比什么都不发强。
    """
    hit = _app_window(app)
    if not hit:
        return 0
    hwnd = hit[1]
    t0 = time.time()
    if config.CB_SHOT_START > 0:
        time.sleep(config.CB_SHOT_START)   # 先等它动起来, 否则开局那一下本来就是静止的
    prev, still_since = None, None
    while time.time() - t0 < config.CB_SHOT_MAX_WAIT:
        img = sender._capture_window(hwnd)
        if img is None:
            time.sleep(config.CB_SHOT_POLL)
            continue
        cur = _img_signature(img)
        if prev is not None and _img_diff(prev, cur) <= _STILL_TOL:
            still_since = still_since or time.time()
            if time.time() - still_since >= config.CB_SHOT_IDLE:
                print(f'[cb-shot] 画面已稳定 {config.CB_SHOT_IDLE:.0f}s, 当作跑完了 '
                      f'(共等 {time.time() - t0:.0f}s)')
                if config.CB_SHOT_SETTLE > 0:
                    time.sleep(config.CB_SHOT_SETTLE)
                return hwnd
        else:
            still_since = None
        prev = cur
        time.sleep(config.CB_SHOT_POLL)
    print(f'[cb-shot] 等到上限 {config.CB_SHOT_MAX_WAIT}s 画面还没稳, 截当前这张')
    return hwnd


def screenshot(tag='shot'):
    """全屏截图存到 shots/, 返回文件路径"""
    os.makedirs(config.SHOT_DIR, exist_ok=True)
    path = os.path.join(config.SHOT_DIR,
                        f'{tag}_{time.strftime("%m%d_%H%M%S")}.png')
    ImageGrab.grab(all_screens=True).save(path)
    w, h = screen_size()
    return f'已截图 {path} (屏幕 {w}x{h})'


def ocr_screen(box=None):
    """OCR 识别屏幕文字。box 可选 'x1,y1,x2,y2' 限定区域(坐标用物理像素)。"""
    import sender
    img = ImageGrab.grab(all_screens=True)
    if box:
        try:
            x1, y1, x2, y2 = [int(v) for v in str(box).split(',')]
            img = img.crop((x1, y1, x2, y2))
        except Exception:
            return f'box 格式应为 x1,y1,x2,y2, 收到: {box!r}'
    text = sender._ocr_window_text(img)
    return ('屏幕识别到的文字:\n' + text) if text else 'OCR 没识别出文字'


def get_clipboard():
    import sender
    return f'剪贴板内容: {sender._read_clipboard_text()!r}'


def set_clipboard(text):
    import sender
    sender._set_clipboard(text)
    return f'已写入剪贴板({len(text)} 字符)'


# ============================================================
# 文件读取
# ============================================================
READ_MAX_BYTES = 2 * 1024 * 1024      # 超过就不整体读, 免得撑爆上下文


def _abs_path(path, strict=False):
    """把路径规整成绝对路径。

    strict=True(写文件专用)时**相对路径直接返回空**, 由调用方给出带桌面路径的提示。
    为什么不直接 abspath: os.path.abspath 会把相对路径拼到**进程 cwd**, 而 imhere 的
    cwd 是项目目录自己。实测 10-14 公平测试 D 组: 模型两次给 path="gss.py", 文件落进
    C:\\Users\\<用户名>\\Desktop\\imhere\\gss.py —— 既不是桌面也不是它说的 game 文件夹,
    程序还回报"已写入 77 字节"(往项目目录里扔垃圾), 而交付那边照说"已经写好了"。
    读文件那几路不设 strict: 读错位置最多是"文件不存在", 不会污染磁盘。
    """
    p = str(path or '').strip().strip('"').strip("'")
    if not p:
        return ''
    p = os.path.expanduser(p)
    if strict and not os.path.isabs(p):
        return ''
    return os.path.abspath(p)


def _rel_path_hint(raw):
    """相对路径被拒时, 告诉它真实桌面在哪、完整路径长什么样。"""
    desk = os.path.join(os.path.expanduser('~'), 'Desktop')
    return (f'你给的是 {str(raw)[:60]!r}, 这是相对路径 —— 相对路径会被拼到程序自己的'
            f'目录里去, 文件就建错地方了。必须写完整绝对路径: 真桌面是 {desk}, '
            f'要建的文件写成 {desk}\\你要的文件名, 要放进子文件夹就写成 '
            f'{desk}\\子文件夹名\\你要的文件名。')


def _read_text(p):
    """读文件, 返回 (文本, 编码)。

    先按 utf-8 试, 解不开再退到 gbk/mbcs —— Windows 上中文 txt 常年是 GBK,
    只按 utf-8 读会整篇乱码, 模型会以为"内容不对", 然后照着乱码继续改。
    """
    with open(p, 'rb') as f:
        raw = f.read()
    # 只有原本真带 BOM 才能用 utf-8-sig: 拿它去解一个没 BOM 的文件, 写回时会**凭空
    # 补一个 BOM** —— 实测把 a.txt / crlf.txt 的文件头都塞了 3 字节 \xef\xbb\xbf。
    if raw[:3] == b'\xef\xbb\xbf':
        return raw.decode('utf-8-sig'), 'utf-8-sig'
    for enc in ('utf-8', 'gbk', 'mbcs'):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', errors='replace'), 'utf-8'


def read_file(path, start=1, max_lines=200):
    """读回一个文件的文本, 带行号。不存在/是目录/太大都给明确说法。"""
    p = _abs_path(path)
    if not p:
        return 'read_file 没给路径'
    if not os.path.exists(p):
        return (f'文件不存在: {p}\n要新建它用 write_file; '
                f'路径拿不准先用 run_command 看看目录里有什么。')
    if os.path.isdir(p):
        return (f'{p} 是目录不是文件。里面(最多 60 项):\n'
                + '\n'.join(sorted(os.listdir(p))[:60]))
    size = os.path.getsize(p)
    if size > READ_MAX_BYTES:
        return f'文件太大({size} 字节), 不整体读; 用 start / max_lines 分段读。'
    try:
        text, enc = _read_text(p)
    except Exception as e:
        return f'读取失败({type(e).__name__}): {e}'
    # 统一按 \n 切行, 免得 CRLF 的文件每行尾部多一个不可见字符
    lines = text.replace('\r\n', '\n').split('\n')
    total = len(lines)
    b, n = max(1, int(start or 1)), max(1, int(max_lines or 200))
    seg = lines[b - 1:b - 1 + n]
    if not seg:
        return f'{p} 共 {total} 行, 从第 {b} 行读是空的。'
    body = '\n'.join(f'{b + i:>5}| ' + ln for i, ln in enumerate(seg))
    head = (f'{p}  (共 {total} 行, 编码 {enc})\n'
            f'下面每行开头的"行号|"只给你定位用, 别把它抄进 edit_file 的 old_str\n')
    end = b + len(seg) - 1
    tail = ''
    if end < total:
        tail = f'\n…以上 {b}-{end} 行, 还有 {total - end} 行, 接着读用 start={end + 1}'
    return head + body + tail


# ============================================================
# 文件写入
# ============================================================
def _deny_check(p, action='写入'):
    low = p.lower().replace('/', '\\')
    for bad in config.WRITE_DENY:
        if low == bad or low.startswith(bad + '\\'):
            raise PermissionError(f'系统目录不允许{action}: {p}')


# ---------------- 删除: 走回收站, 删错还能捞回来 ----------------
FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # 有它才是"进回收站", 否则是彻底抹掉
FOF_NOERRORUI = 0x0400


class SHFILEOPSTRUCTW(ctypes.Structure):
    """SHFileOperationW 的参数结构。字段顺序和类型必须和系统对上, 别乱改。"""
    _fields_ = [('hwnd', ctypes.c_void_p),
                ('wFunc', ctypes.c_uint),
                ('pFrom', ctypes.c_wchar_p),
                ('pTo', ctypes.c_wchar_p),
                ('fFlags', ctypes.c_ushort),         # FILEOP_FLAGS 是 WORD, 不是 DWORD
                ('fAnyOperationsAborted', ctypes.c_int),
                ('hNameMappings', ctypes.c_void_p),
                ('lpszProgressTitle', ctypes.c_wchar_p)]


def _to_recycle_bin(p):
    """把文件/目录送进回收站; 成功返回 ''。"""
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = p + '\0'      # 文件列表要 \0 分隔、\0\0 收尾(ctypes 自己再补一个 \0)
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    if rc:
        return f'SHFileOperation 返回 {rc}'
    if op.fAnyOperationsAborted:
        return '操作被系统中止'
    return ''


def delete_file(path):
    """删掉一个文件(进回收站)。删错了还能从回收站还原, 所以比 del 命令稳。

    为什么值得单做一个原生工具: 实测 10-07 18:34 和 18:42 两次「删除桌面上的
    guess.py」—— 模型根本没碰 run_command('del'), 而是绕去 **Win+R 运行框**(它把
    "删除"想成了"打开/运行那个文件"), 连试 6 轮全落空, 最后连回话都没了。提示词
    第 14 条明明写着"删除 del 文件名", 照样没拦住。这个项目一贯的结论是"提示词管不住
    4B 就上工具层": 给它一个名字直白、只吃一个路径的 delete_file, 路才通。
    """
    p = _abs_path(path)
    if not p:
        return 'delete_file 没给路径'
    _deny_check(p, '删除')
    # ↓ 这两句会原样发到微信(程序兜底直接拿返回值当回话, 见 agent._rescue_delete),
    #   所以必须是人话 —— 实测 19:0x 把"如实告诉对方就行, 别再换个工具反复试"
    #   这种给模型的指令直接发出去了。
    if not os.path.exists(p):
        return f'{p} 已经不在了，没有可删的东西'
    if os.path.isdir(p) and os.listdir(p):
        return f'{p} 是个非空文件夹，整个删掉太危险，得把里面的东西一个个点名删'
    err = _to_recycle_bin(p)
    if err:
        return f'删除 {p} 失败: {err}'
    if os.path.exists(p):
        return f'删除 {p} 失败: 它还在原地(多半被别的程序占着)。'
    return f'已删除 {p}(进回收站了, 反悔还能还原)'


PY_EXTS = ('.py', '.pyw')


def _py_syntax_gate(p, text, base=None):
    """写 .py 之前先编译一遍: 语法错就拒绝落盘, 把报错原文回给模型让它重写。

    为什么放在工具层, 而不是只写在提示词里: "写完不跑" 是 4B 级模型的稳定行为,
    提示词管不住; 但"这份 .py 编译不过"是**机器一定判得出来**的事实, 不该靠模型自觉。
    成本是一次 compile(), 收益是这类事故根本不落盘。

    实测 10-07 16:16: "在桌面建 poem/a2fd381ed.py, 逐字输出静夜思" —— 模型给出
    content 里的字符串中间夹了一个**真换行**(它想在 Python 层写 \\n, 但它此刻在写
    的是工具参数的 JSON, 那一层只认 \\n), 产物第 1 行就
    `SyntaxError: unterminated string literal`, 而 write_file 照旧回报"已写入",
    模型接着宣称"该文件实现了逐字输出"。一次 compile() 就能拦住整条链。

    base: edit_file 传原文 —— 原文件本来就是坏的(这次就是在修它)就放行, 不挡修 bug。
    """
    if not str(p).lower().endswith(PY_EXTS):
        return ''
    if base is not None:
        try:
            compile(base, p, 'exec')
        except (SyntaxError, ValueError):
            return ''                      # 原文编译不过 -> 这次编辑是来修它的, 放行
    try:
        compile(text, p, 'exec')
    except SyntaxError as exc:
        bad = (exc.text or '').rstrip('\n')
        tip = ''
        if 'unterminated' in str(exc.msg):
            tip = ('\n  多半是字符串里的换行写成了真换行: 要换行得在字符串里写转义 '
                   '\\n; 而工具参数是 JSON, 反斜杠还要再转义一层; '
                   '或者干脆把整段用三引号包起来。')
        return (f'没写入: {os.path.basename(p)} 编译不过, 拒绝落盘。\n'
                f'  {type(exc).__name__}: {exc.msg} (第 {exc.lineno} 行)\n'
                f'  那一行原文: {bad!r}\n'
                f'  按报错改好内容再写一次。' + tip)
    except ValueError as exc:              # 内容里含 \x00 之类
        return f'没写入: {os.path.basename(p)} 无法编译 ({exc})。'
    return ''


def write_file(path, content):
    """把一段文本写成文件(自动创建父目录)。**整份覆盖**; 改已有文件用 edit_file。

    为什么要有这个原生工具: "在桌面建个冒泡排序的cpp"这类活, 让模型用 run_command
    拼 PowerShell here-string 写多行代码, 引号/换行/转义几乎必错 —— 实测它在
    "写文件"这件事上失败的原因全在"拼命令", 不在"写代码"。给它一个"把这段内容写到
    这个路径"的工具, 就绕开了整类语法错误。

    但它只能整份覆盖: 让它"把 a.txt 第二行改一下"必然把别的行写丢 —— 见 edit_file。
    """
    p = _abs_path(path, strict=True)
    if not p:
        return f'write_file 没给路径' if not str(path or '').strip() \
            else f'write_file 不接受相对路径。{_rel_path_hint(path)}'
    # 项目目录: 直接落在桌面根目录的文件统一收进 Desktop\wxduo_work\ ——
    # 实测模型爱往桌面根写, 一次任务能留下 5 个 guess_number_*.py, 桌面很快就脏了。
    _desk = os.path.join(os.path.expanduser('~'), 'Desktop')
    if os.path.dirname(os.path.normcase(p)) == os.path.normcase(_desk):
        _work = getattr(config, 'WORK_DIR', os.path.join(_desk, 'wxduo_work'))
        p = os.path.join(_work, os.path.basename(p))
        print(f'[write_file] 桌面根目录文件改放到项目目录: {p}')
    _deny_check(p)
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    text = '' if content is None else str(content)
    blocked = _py_syntax_gate(p, text)
    if blocked:
        return blocked
    # H1: 脚本类文件落盘前也扫一遍危险内容, 给模型更早的反馈(执行时 run_py/run_java 还会再扫)
    if str(p).lower().endswith(_SCRIPT_DANGER_EXTS):
        hit = _script_danger(text)
        if hit:
            return (f'不允许: 这份 {os.path.basename(p)} 的内容命中危险调用 {hit!r}。'
                    f'脚本里不应直接做系统级危险操作(执行命令 / 删文件 / 拉外部程序)。'
                    f'需要跑命令请用 run_command 工具(走安全闸)。')
    over = ''
    if os.path.isfile(p):
        over = f' (整份覆盖了原有 {os.path.getsize(p)} 字节)'
    with open(p, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    done = (f'已写入 {p}{over} ({len(text.encode("utf-8"))} 字节, '
            f'{len(text.splitlines())} 行)')
    if str(p).lower().endswith(PY_EXTS):
        # 把"下一步"塞在工具结果里, 而不是只写在提示词里 —— 模型刚写完文件的这一刻
        # 是它最可能照着做的时刻。实测它写完 .py 就直接宣布交付, 从不回头看提示词。
        done += ('\n下一步(交付前必做): 用 run_py(path=..., stdin_text=..., expect=...) '
                 '真跑一遍 —— 自检通过才算写完; 自检失败就用 edit_file 改掉再跑。')
    return done


def edit_file(path, old_str, new_str, replace_all=False):
    """把文件里的一段原文替换成新内容, 只动这一处, 其余原样保留。

    old_str 必须在文件里唯一出现(除非 replace_all=True), 否则拒绝执行并报出次数 ——
    这是"改错地方"唯一可靠的防线。原来只有 write_file(整份覆盖), 模型改一行只能凭
    记忆重写整个文件, 必然把别的行写丢。
    """
    p = _abs_path(path, strict=True)
    if not p:
        return f'edit_file 没给路径' if not str(path or '').strip() \
            else f'edit_file 不接受相对路径。{_rel_path_hint(path)}'
    _deny_check(p)
    if not os.path.isfile(p):
        return f'文件不存在: {p} —— edit_file 只改已有文件, 要新建用 write_file。'
    if not old_str:
        return ('old_str 是空的。要改内容必须给出"文件里现在长什么样"的那段原文, '
                '先用 read_file 看一眼。')
    text, enc = _read_text(p)
    if '\x00' in text:
        return f'{p} 看着是二进制文件, 不改。'
    old_raw = str(old_str)
    new_raw = '' if new_str is None else str(new_str)
    # 归一化只用来"计数和定位"(决定要不要拒绝), 不直接拿去写回。
    norm = text.replace('\r\n', '\n')
    old = old_raw.replace('\r\n', '\n')
    new = new_raw.replace('\r\n', '\n')
    cnt = norm.count(old)
    if cnt == 0:
        return (f'没找到这段原文, 一个字都没改。多半是空格/换行/标点差了一点, '
                f'先用 read_file 看清 {p} 的原文。\n你要替换的开头是: {old[:60]!r}')
    if cnt > 1 and not replace_all:
        return (f'这段原文在文件里出现 {cnt} 次, 不敢动(怕改错地方)。'
                f'把 old_str 连同前后文加长让它唯一; 确认要全改就传 replace_all=true。')
    # 写回优先走"原文精确匹配": old_str 在原文里原样出现, 就替换原文里那一处,
    # 整份文件除了它一个字节都不动。这一步是给混合换行的文件留的 —— 实测一份
    # line1\r\n / line2\n / line3\r\n / line4\n 的文件改 line2 时, 归一化后统一
    # 回填会把原本是 LF 的 line3/line4 也改成 CRLF: 没编辑的字节被改了,
    # diff 会整篇飘红, 对代码仓库是灾难。
    raw_cnt = text.count(old_raw)
    if raw_cnt >= 1 and (replace_all or raw_cnt == 1):
        out = (text.replace(old_raw, new_raw) if replace_all
               else text.replace(old_raw, new_raw, 1))
    else:
        # 原文里命不中(模型给的 old_str 用 LF 而文件是 CRLF 之类)才退回归一化路径,
        # 并按文件原本的风格还原换行; 本来就混着两种换行的文件不做还原(还原必然改错)。
        out = norm.replace(old, new) if replace_all else norm.replace(old, new, 1)
        if '\r\n' in text and norm.count('\n') == text.count('\r\n'):
            out = out.replace('\n', '\r\n')
    blocked = _py_syntax_gate(p, out, base=text)
    if blocked:
        return blocked.replace('重新写入', '再 edit_file')
    try:
        data, enc_used = out.encode(enc), enc
    except UnicodeEncodeError:
        data, enc_used = out.encode('utf-8'), 'utf-8'
    with open(p, 'wb') as f:
        f.write(data)
    line_no = norm.count('\n', 0, norm.find(old)) + 1
    done = f'{cnt} 处' if replace_all else f'第 {line_no} 行附近'
    note = '' if enc_used == enc else f' (原编码 {enc} 装不下新字符, 已存为 utf-8)'
    return (f'已改 {p} {done}, 现 {os.path.getsize(p)} 字节{note}。\n'
            f'改前: {old[:70]}\n改后: {new[:70]}')


# ============================================================
# 真跑一遍 —— 逻辑错的唯一硬抓手
# ============================================================
# 首行标记: agent 只认这几个词判"这次自检过没过", 改措辞时两边一起改
RUNPY_PASS = ('自检通过', '跑完了')
RUNPY_FAIL = ('自检失败', '跑出错了', '跑不起来')

_NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW: 子进程别弹黑框、别抢前台


def run_py(path, stdin_text='', expect='', forbid='', timeout=10):
    """把自己刚写的 .py 真跑一遍: 管道喂输入, 拿退出码和输出判断它对不对。

    为什么必须有这个工具: compile() 只拦语法错, "猜的数比答案小却输出太大了" 这种
    **逻辑错**它一个字都看不出来。实测 10-07 17:37 "帮我在桌面写一个猜数字py":
    大小提示全写反, 而助手从头到尾没跑过它, 直接说"我先用记事本看看有没有写错的
    地方"就交付了。提示词里"交付前逐行核对"是做不到的期望 —— 能判对错的只有机器,
    所以把"跑一遍"做成一次调用就能完成的工具, 而不是一句要求。

    expect/forbid 是给逻辑错用的断言: expect="太小了" = 输出里必须出现这几个字,
    forbid 反过来。给了断言就是自检, 通过才算这活干完; 不给就只是跑一次看输出。
    交互式程序(猜数字要 input())用 stdin_text 把每行输入按顺序喂进去 —— 确定的输入
    配确定的输出 = 可复现的自检, 不用人点。
    """
    p = _abs_path(path)
    if not p:
        return 'run_py 没给路径'
    if not str(p).lower().endswith(PY_EXTS):
        return f'run_py 只能跑 .py 文件, 这个是: {p}'
    if not os.path.isfile(p):
        return f'跑不起来: 没有这个文件 {p}(要先用 write_file 把它写出来)'
    # H1: 跑之前先扫脚本内容, 堵住"写个 .py 再 run_py"绕过 DANGEROUS_PATTERNS 的口子
    hit = _scan_script_file(p)
    if hit:
        return (f'不允许执行: {os.path.basename(p)} 的内容命中危险调用 {hit!r}。'
                f'脚本里不应直接做系统级危险操作; 需要跑命令请用 run_command '
                f'工具(走安全闸)。')
    secs = max(1.0, min(float(timeout or 10), 60.0))
    feed = '' if stdin_text is None else str(stdin_text)
    if feed and not feed.endswith('\n'):
        feed += '\n'                # input() 读到 EOF 会抛异常, 补个换行更像真人敲回车
    want = str(expect) if expect else ''
    hate = str(forbid) if forbid else ''
    env = dict(os.environ, PYTHONIOENCODING='utf-8')    # 否则子进程 print 中文会乱码
    try:
        r = subprocess.run([sys.executable, '-u', p], input=feed,
                           capture_output=True, text=True,
                           encoding='utf-8', errors='replace',
                           timeout=secs, cwd=os.path.dirname(p) or None,
                           env=env, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return (f'跑出错了: {os.path.basename(p)} 跑满 {secs:g}s 还没结束, 已被掐掉。'
                f'多半是它在等输入(把每行输入写进 stdin_text)或者死循环了。')
    except Exception as e:                                      # noqa: BLE001
        return f'跑不起来: {type(e).__name__}: {e}'

    out = (r.stdout or '').strip()
    err = (r.stderr or '').strip()
    name = os.path.basename(p)
    # input() 读到 EOF 会抛 EOFError, 但这两件事完全不同:
    #   fed=False -> 一行输入都没喂, 它在干等, 必须让它回去补 stdin_text
    #   fed=True  -> 输入喂完了, 循环程序正常收尾(猜数字喂了 1 就该等到提示语再结束)
    # 混为一谈会把"喂够了的正确程序"判成崩了, 模型就会去改一份本来没错的代码。
    fed = bool(feed.strip())
    starved = 'EOFError' in err and not fed
    exhausted = 'EOFError' in err and fed
    # 只留尾部: 报错和结果基本都在最后, 而模型的上下文很贵
    shown = out if len(out) <= 600 else '…(前面省略)…' + out[-600:]
    errline = ''
    for ln in reversed(err.splitlines()):
        if ln.strip():
            errline = ln.strip()[:200]
            break
    hint = ''
    if 'EOFError' in err:
        hint = ('\n  它在等输入: 把要喂给它的每一行(按顺序)写进 stdin_text 再跑一次。')
    elif 'SyntaxError' in err or 'IndentationError' in err:
        hint = '\n  这是语法错, 用 edit_file 改掉再跑。'

    if want and want not in out:
        return (f'自检失败: 期望输出里出现 {want!r}, 实际没有 —— 这段逻辑和你说的不一样, '
                f'不许说完成。\n  实际输出: {shown or "(什么都没有)"}'
                + (f'\n  报错: {errline}' if errline else '') + hint
                + '\n  用 edit_file 改掉错的那处, 再 run_py 跑一次。')
    if hate and hate in out:
        return (f'自检失败: 输出里出现了不该有的 {hate!r}。\n'
                f'  实际输出: {shown or "(什么都没有)"}' + hint
                + '\n  用 edit_file 改掉, 再 run_py 跑一次。')
    if want or hate:
        if r.returncode != 0 and not exhausted:
            return (f'自检失败: 断言虽然对上了, 但程序是崩的(退出码 {r.returncode})。'
                    + (f'\n  报错: {errline}' if errline else '') + hint
                    + '\n  先把它改到能正常退出, 再跑一次。')
        tail = ('' if not exhausted else
                '\n  (它是在等下一行输入时结束的 —— 输入已经喂完, 这算正常结束, 不用改)')
        return (f'自检通过: 断言对上了({(want or hate)!r} 的检查通过), '
                f'退出码 {r.returncode}。\n  输出: {shown or "(它没有输出)"}' + tail)
    if r.returncode == 0:
        return f'跑完了(退出码 0): {shown or "(它没有任何输出)"}'
    if exhausted:
        return (f'跑完了(输入喂完了它才结束, 退出码 {r.returncode}): '
                f'{shown or "(没有输出)"}\n  (这是正常的, 不用改代码; 想要完整跑完'
                f'就多喂几行输入。)')
    return (f'跑出错了(退出码 {r.returncode}): {shown or "(没有输出)"}'
            + (f'\n  报错: {errline}' if errline else '') + hint)


# ============================================================
# Java: 全量编译 + 运行
# ============================================================
# 为什么单独做一个工具, 而不是让模型用 run_command 拼 javac:
#  1) 源码是我们用 write_file 按 UTF-8 写的, 而 javac 在中文 Windows 上默认按 GBK
#     读源文件 —— 只要代码里有中文注释/中文提示, 必然报"编码GBK的不可映射字符",
#     而且报错行为诡异(列号错位)。必须固定加 -encoding UTF-8, 这一条不能交给模型记。
#  2) java 命令的输出编码同理: JDK 17 的 System.out 走 native.encoding(GBK),
#     程序 print 中文回来就是乱码, 模型会以为程序算错了。
#  3) 多类项目必须**一次性全量编译**: 让模型 javac A.java 再 javac B.java, 共享类
#     在两次调用之间找不到符号, 它会开始瞎改本来就正确的代码。
JAVA_EXT = '.java'
JAVA_SKIP_DIRS = {'out', 'build', 'target', 'bin', 'classes', '.git', '.idea'}
_JAVA_MAIN_RE = re.compile(r'static\s+void\s+main\s*\(')
_JAVA_PKG_RE = re.compile(r'^\s*package\s+([\w.]+)\s*;', re.M)


def _java_sources(root):
    """收集 root 下所有 .java(跳过编译输出目录)"""
    out = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs
                         if d.lower() not in JAVA_SKIP_DIRS and not d.startswith('.'))
        for f in files:
            if f.lower().endswith(JAVA_EXT):
                out.append(os.path.join(cur, f))
    return sorted(out)


def _decode_any(raw):
    """先按 UTF-8 解, 不行再按 GBK —— javac 的中文报错是 GBK, 我们自己拼的提示是 UTF-8"""
    raw = raw or b''
    for enc in ('utf-8', 'gbk'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('gbk', 'replace')


def _java_main_class(src):
    """抠出主类全名(package.Class), 没有 main 方法返回 ''。

    类名直接用文件名: Java 规定 public 类名必须等于文件名, 比正则匹配 class 关键字
    可靠(正则容易抓到内部类或开头那个无关的类)。
    """
    try:
        txt = _read_text(src)[0]        # _read_text 返回 (文本, 编码)
    except Exception:                                          # noqa: BLE001
        return ''
    if not _JAVA_MAIN_RE.search(txt):
        return ''
    cls = os.path.splitext(os.path.basename(src))[0]
    m = _JAVA_PKG_RE.search(txt)
    return (m.group(1) + '.' + cls) if m else cls


def run_java(path, main_class='', args='', expect='', forbid='',
             timeout=60, gui=False):
    """把自己刚写的 Java 项目真编译一遍再跑起来, 用输出判断它对不对。

    path 给 .java 文件或项目目录都行: 编译范围是**那个目录下全部 .java**
    (含子目录), 所以先写完所有类再调一次就行, 别一个类一个类地编译。

    gui=true 用于 Swing/JavaFX 窗口程序: 窗口程序本来就不会自己退出,
    不带 gui 会被当成"卡死"掐掉。不带 gui 时程序跑过 timeout 却**新出现了一个
    窗口**, 也按窗口程序处理并保留进程 —— 模型经常忘给这个参数。

    expect/forbid 同 run_py: 给了就是自检, 通过才算干完。
    """
    p = _abs_path(path, strict=True)
    if not p:
        return (f'run_java 不接受相对路径。{_rel_path_hint(path)}'
                if str(path or '').strip() else 'run_java 没给路径')
    _deny_check(p)
    if os.path.isdir(p):
        root = p
    elif os.path.isfile(p):
        root = os.path.dirname(p)
    else:
        return f'编译不了: 没有这个路径 {p}(要先用 write_file 把 .java 写出来)'
    srcs = _java_sources(root)
    if not srcs:
        return f'编译不了: {root} 下面一个 .java 都没有(先用 write_file 把类写出来)'
    # H1: 编译前扫全部源码, 挡住 Runtime.exec / ProcessBuilder / 删文件 等危险调用
    for s in srcs:
        try:
            with open(s, 'r', encoding='utf-8', errors='ignore') as _fh:
                hit = _script_danger(_fh.read())
        except Exception:
            hit = None
        if hit:
            return (f'不允许编译: {os.path.basename(s)} 的内容命中危险调用 {hit!r}。'
                    f'Java 里不应直接 Runtime.exec / ProcessBuilder / 删文件; '
                    f'需要跑命令请用 run_command 工具。')

    out = os.path.join(root, 'out')
    try:
        os.makedirs(out, exist_ok=True)
    except OSError as e:
        return f'编译不了: 建不了输出目录 {out} ({e})'
    # @argfile: 类一多命令行会超出 Windows 的 8191 字符上限, 用文件传源码清单。
    argfile = os.path.join(out, '_sources.txt')
    with open(argfile, 'w', encoding='utf-8') as f:
        f.write('\n'.join(srcs))
    secs = max(10.0, min(float(timeout or 60), 180.0))
    cmd = ['javac', '-encoding', 'UTF-8', '-d', out, '-classpath', out,
           '@' + argfile]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=secs,
                           cwd=root, creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return '编译不了: 这台机器上找不到 javac —— PATH 里没有 JDK。'
    except subprocess.TimeoutExpired:
        return (f'跑出错了: javac 跑了 {secs:g}s 还没结束(项目太大?), 已掐掉。'
                f'\n  共 {len(srcs)} 个 .java, 先只留下需要的类。')
    if r.returncode != 0:
        err = _decode_any(r.stderr or b'') or _decode_any(r.stdout or b'')
        lines = [ln for ln in err.strip().splitlines() if ln.strip()][:25]
        return ('跑出错了(编译不过, 一个类都没跑起来):\n  '
                + '\n  '.join(lines)
                + '\n按报错里的文件名和行号用 edit_file 改掉, 再 run_java 编译一次。'
                + '\n如果是"找不到符号/程序包不存在": 类名或包名写错、或大小写不一致。'
                + '\n如果是"编码GBK的不可映射字符": 那个文件不是 UTF-8, 用 write_file 整份重写。'
                + '\n如果是"类 X 是公共的, 应在名为 X.java 的文件中声明": 文件名必须等于类名。')

    main = str(main_class or '').strip()
    if not main:
        for s in srcs:
            main = _java_main_class(s)
            if main:
                break
    if not main:
        return (f'编译通过了({len(srcs)} 个类都没有语法错误), 但没找到带 '
                f'public static void main(String[] args) 的类, 所以还没法运行。'
                f'\n  要真跑起来: 在入口类里加上 main 方法, 再 run_java。')

    cmd = ['java', '-Dfile.encoding=UTF-8', '-Dsun.stdout.encoding=UTF-8',
           '-Dsun.stderr.encoding=UTF-8', '-classpath', out, main]
    if args:
        cmd += [a for a in _decode_any(str(args).encode('utf-8')).split() if a]
    win_before = set(_window_map().keys())
    try:
        proc = subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return '编译不了: 这台机器上找不到 java —— PATH 里没有 JDK。'
    def _own_windows():
        """这次的 java 进程自己开出来的新窗口标题(按 PID 认, 别人的不算)"""
        return [t for h, (t, _c) in _window_info().items()
                if h not in win_before and t.strip() and _window_pid(h) == proc.pid]

    def _ok_text(title):
        return ('跑完了(退出码 0): 窗口程序已经启动并留在桌面上'
                + (f', 窗口标题 {title!r}' if title else '')
                + f', 进程 PID {proc.pid}。不会自己退出是正常的, 可以交付了。')

    if gui:
        # 窗口程序不会自己退出, 死等满超时纯属浪费(实测白等 60s):
        # 每 0.5s 看一眼**它自己**的窗口, 出现就立刻返回。
        deadline = time.time() + min(secs, 20.0)
        while time.time() < deadline:
            mine = _own_windows()
            if mine:
                return _ok_text(mine[0])
            if proc.poll() is not None:      # 自己退了(可能崩了), 走正常出口
                break
            time.sleep(0.5)
        if proc.poll() is None:
            others = [t for h, (t, _c) in _window_info().items()
                      if h not in win_before and t.strip()]
            proc.kill()
            return (f'跑出错了: {main} 进程起来了(PID {proc.pid})但 20s 内没有它自己的'
                    f'窗口, 已掐掉'
                    + (f'; 这期间新冒出来的窗口是别的程序的: {others[:3]}, 不算数'
                       if others else '')
                    + '。\n  多半是界面没弹出来: 确认 main 里最后调了 setVisible(true), '
                      '并且没在 EventQueue 之外直接 new 窗口; '
                      '也可能构造时就抛异常了, 用 run_java 看 stderr。')

    try:
        raw_out, raw_err = proc.communicate(timeout=secs)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        # 没退出: 要么是窗口程序(正常), 要么死循环/等输入(异常)。
        mine = _own_windows()
        if mine:
            return _ok_text(mine[0])
        proc.kill()
        others = [t for h, (t, _c) in _window_info().items()
                  if h not in win_before and t.strip()]
        return (f'跑出错了: {main} 跑满 {secs:g}s 还没结束, 已掐掉。'
                f'\n  命令行程序这样多半是在等输入(Scanner)或死循环。'
                + (f'\n  注意: 期间新冒出来的窗口是别的程序的({others[:3]}), 不是它。'
                   if others else '')
                + f'\n  如果它其实是 Swing/JavaFX 窗口程序, 带上 gui=true 再跑一次。')

    out_s = _decode_any(raw_out).strip()
    err_s = _decode_any(raw_err).strip()
    shown = out_s if len(out_s) <= 900 else '…(前面省略)…' + out_s[-900:]
    errline = ''
    for ln in reversed(err_s.splitlines()):
        if ln.strip():
            errline = ln.strip()[:200]
            break
    want, hate = str(expect or ''), str(forbid or '')
    if want and want not in out_s:
        return (f'自检失败: 期望输出里出现 {want!r}, 实际没有 —— 程序能跑但结果不对。'
                f'\n  实际输出: {shown or "(什么都没有)"}'
                + (f'\n  报错: {errline}' if errline else '')
                + '\n  用 edit_file 改掉错的那处, 再 run_java 跑一次。')
    if hate and hate in out_s:
        return (f'自检失败: 输出里出现了不该有的 {hate!r}。'
                f'\n  实际输出: {shown}'
                + '\n  用 edit_file 改掉, 再 run_java 跑一次。')
    if rc != 0:
        return (f'跑出错了(退出码 {rc}): {shown or "(没有输出)"}'
                + (f'\n  报错: {errline}' if errline else '')
                + '\n  修完再 run_java 跑一次。')
    if want or hate:
        return (f'自检通过: 断言对上了, 退出码 {rc}。\n  '
                f'输出: {shown or "(它没有输出)"}')
    return f'跑完了(退出码 {rc}): {shown or "(它没有任何输出)"}'


def list_files(path, depth=2):
    """看一眼目录里有什么(项目结构), 只给名字和大小, 不读内容。

    多文件项目必须有这个: 模型写完 6 个类之后, 自己也不知道到底落了几个、
    文件名对不对(Java 要求文件名 = 类名), 只能靠列目录核对。
    """
    p = _abs_path(path)
    if not p:
        return 'list_files 没给路径'
    if not os.path.isdir(p):
        return f'{p} 不是目录(看单个文件的内容用 read_file)'
    lvl_max = max(1, min(int(depth or 2), 4))
    lines, count = [], 0
    for cur, dirs, files in os.walk(p):
        rel = os.path.relpath(cur, p)
        lvl = 0 if rel == '.' else rel.count(os.sep) + 1
        if lvl > lvl_max:
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs
                         if d.lower() not in JAVA_SKIP_DIRS and not d.startswith('.'))
        files = sorted(files)
        if rel == '.':
            lines.append(f'{p}\\  ({len(files)} 个文件, {len(dirs)} 个子目录)')
        else:
            lines.append('  ' * lvl + os.path.basename(cur) + '\\')
        for f in files[:40]:
            try:
                sz = os.path.getsize(os.path.join(cur, f)) / 1024.0
            except OSError:
                sz = 0.0
            lines.append('  ' * (lvl + 1) + f'{f}  {sz:.1f}KB')
            count += 1
    if count == 0:
        return f'{p} 是个空目录'
    if len(lines) > 80:
        lines = lines[:80] + [f'…(还有更多, 只列了前 80 行)']
    return '\n'.join(lines)


# ============================================================
# 工具清单(ollama 原生 function calling 的 schema)
# ============================================================
TOOL_SPECS = [
    {
        'type': 'function',
        'function': {
            'name': 'run_java',
            'description': '把刚写的 Java 项目**全量编译**再跑起来: javac(-encoding UTF-8) '
                           '所有 .java -> java 运行 -> 返回编译错误/输出/退出码。'
                           '写 Java 必须用它验证, 不要用 run_command 拼 javac(会因 GBK 编码'
                           '报"不可映射字符", 中文提示也会乱码成乱码判错)。'
                           '同一个目录的类一次全编译, 别一个类一个地编译(共享类会找不到符号)。'
                           'Swing/JavaFX 窗口程序要带 gui=true(窗口程序不会自己退出, '
                           '不带会被当成卡死掐掉); 命令行程序用 expect 给断言当自检。'
                           'path 给任一 .java 或项目目录都行, 编译范围是该目录下全部 .java。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string',
                             'description': '任一 .java 的完整路径, 或项目目录, 如 '
                                            'C:\\Users\\<用户名>\\Desktop\\book\\Main.java'},
                    'main_class': {'type': 'string',
                                   'description': '入口类全名(带包名), 留空则自动找含 main 的类'},
                    'args': {'type': 'string',
                             'description': '传给 main 的命令行参数, 空格分隔, 一般留空'},
                    'expect': {'type': 'string',
                               'description': '断言: 标准输出里必须出现这几个字'},
                    'forbid': {'type': 'string',
                               'description': '断言: 标准输出里不许出现这几个字'},
                    'timeout': {'type': 'number',
                                'description': '秒, 默认 60, 上限 180'},
                    'gui': {'type': 'boolean',
                            'description': '窗口程序(Swing/JavaFX)设 true, 启动后不等待退出'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_files',
            'description': '列出一个目录下的文件和子目录(带大小), 不读内容。'
                           '多文件项目写完/改完用它核对到底落了哪些文件、名字对不对'
                           '(Java 要求文件名必须等于 public 类名)。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '要列的目录完整路径'},
                    'depth': {'type': 'number', 'description': '往下几层, 默认 2, 最多 4'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'write_file',
            'description': '把一段文本整份写进一个文件(父目录不存在会自动创建)。'
                           '只在新建文件、或整个文件要重写时用它; 文件已存在、只改'
                           '少量内容, 一律用 edit_file —— 这个工具是整份覆盖, '
                           '拿来改一行会把别的行写丢。不要用 run_command 拼 echo 写文件。'
                           '写 .py/.pyw 会先编译一遍: 编译不过直接拒收, 并把报错原文'
                           '回给你, 按它改完再写一次。'
                           'path 必须是完整绝对路径, 相对路径会被拒绝',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string',
                             'description': '文件完整路径, 如 '
                                            'C:\\Users\\<用户名>\\Desktop\\bubble_sort.cpp'},
                    'content': {'type': 'string',
                                'description': '要写入的完整文件内容, 可以多行。'
                                               '写 .py 时这一层是 JSON: 字符串里要'
                                               '换行得把反斜杠转义两层(JSON 一层 + '
                                               'Python 一层), 内容编译不过会被拒收。'},
                },
                'required': ['path', 'content'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'run_py',
            'description': '把自己刚写的 .py 真跑一遍(子进程执行), 用管道喂输入, '
                           '返回退出码和输出。写完 .py 交付前**必须**用它验证: 语法'
                           '对不对 compile 能判, 逻辑对不对只有跑一遍才知道 —— 比如'
                           '猜数字把"太大/太小"提示写反了, 不跑永远看不出来。'
                           '交互式程序用 stdin_text 把每行输入按顺序喂进去(有 input() '
                           '就必须给, 否则它读到空输入直接崩); 再给 expect(输出里必须'
                           '出现的字)当断言, 自检通过才算干完。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '要跑的 .py 完整路径'},
                    'stdin_text': {'type': 'string',
                                   'description': '喂给它的输入, 每行一条, 如 "50\\n30\\n"'},
                    'expect': {'type': 'string',
                               'description': '断言: 输出里必须出现这几个字'
                                              '(如 "太小了"), 没出现 = 自检失败'},
                    'forbid': {'type': 'string',
                               'description': '反向断言: 输出里不许出现这几个字'},
                    'timeout': {'type': 'number',
                                'description': '最多跑几秒, 默认 10'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_file',
            'description': '把某个文件的内容读出来看(带行号)。要改已有文件、要知道'
                           '某个文件里到底写了什么、要确认自己刚写对没有, 都用它。'
                           '不要用 run_command 的 type/cat 去读文件。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '文件完整路径'},
                    'start': {'type': 'integer',
                              'description': '从第几行开始读, 默认 1'},
                    'max_lines': {'type': 'integer',
                                  'description': '最多读多少行, 默认 200'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'edit_file',
            'description': '改已有文件里的一处内容: 给出现在长什么样的原文、和要改成'
                           '什么, 只替换这一处, 其余内容原样保留。改少量内容一律用它, '
                           '不要用 write_file 重写整个文件。old_str 必须在文件里唯一'
                           '出现, 否则会被拒绝。path 同样必须是完整绝对路径',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': '文件完整路径'},
                    'old_str': {'type': 'string',
                                'description': '文件里现有的那段原文, 要和 read_file '
                                               '看到的一字不差(含缩进)'},
                    'new_str': {'type': 'string', 'description': '要替换成的新内容'},
                    'replace_all': {'type': 'boolean',
                                    'description': '原文出现多次时是否全部替换, 默认 false'},
                },
                'required': ['path', 'old_str', 'new_str'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'delete_file',
            'description': '删除一个文件的首选工具(删进回收站, 删错还能还原)。'
                           '对方说"删了它""把这个删掉""清理一下 xxx"就用它, '
                           '参数只有一个完整路径。'
                           '绝对不要拿 win_run(运行框)去删文件 —— 运行框是打开程序用的, '
                           '用它删文件只会弹错误框, 文件一个都不会少。'
                           '非空目录会被拒绝(要清里面的东西请一个个点名)。'
                           '要挪走/改名请用 run_command 的 move, 复制用 copy。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string',
                             'description': '要删除的那个文件的完整路径(照对方原话里的'
                                            '路径写, 不要照抄这里的格式示例)'},
                },
                'required': ['path'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'run_command',
            'description': '执行一条系统命令并返回输出。启动程序、查询系统信息、'
                           '调用命令行工具都用它(比 Win+R 可靠, 能拿到结果)。',
            'parameters': {
                'type': 'object',
                'properties': {'command': {'type': 'string',
                                           'description': '要执行的命令'}},
                'required': ['command'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'open_app',
            'description': '打开程序的首选工具(最可靠): 中文名英文名都能用, '
                           '包括 计算器/calc、画图/mspaint、命令提示符/powershell(终端)、'
                           '浏览器/edge、任务管理器、资源管理器、控制面板、注册表、微信; '
                           '也能打开文件路径、文件夹和网址(用已关联程序打开); '
                           '内部会验证是否真的打开了。要打开程序就用它, 不要用 win_run。'
                           '注意: 写代码文件绝对不要走"打开记事本再往里敲"这条路 —— '
                           '那是 write_file 的活, 而且记事本多半不会保存, 桌面上'
                           '什么都不会有。',
            'parameters': {
                'type': 'object',
                'properties': {'name': {'type': 'string',
                                        'description': '程序名(中文或英文都行)、文件路径或网址'}},
                'required': ['name'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'win_run',
            'description': '模拟按 Win+R 打开"运行"框并输入后回车。'
                           '只用于路径/网址/系统命令(如 D:\\a.txt、cmd); '
                           '打开程序请用 open_app —— 本工具要抢前台窗口, '
                           '而且填中文程序名一定会失败(要 notepad 不是"记事本")。'
                           '**本工具拿不到任何输出**: 命令是另开窗口自己跑的, 你看不到'
                           '它的返回。要版本号/运行结果/命令输出, 一律用 run_command。'
                           '删文件用 delete_file, 复制/改名/移动用 run_command, '
                           '别指望运行框 —— 它执行不了这些, 只会弹错误框。',
            'parameters': {
                'type': 'object',
                'properties': {'command': {'type': 'string',
                                           'description': '要输入的内容, '
                                                          '如 notepad / calc / mspaint / cmd'}},
                'required': ['command'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'send_keys',
            'description': '发送按键或组合键。写法: "enter" / "ctrl+s" / "alt+f4" / '
                           '"ctrl,shift,esc"。',
            'parameters': {
                'type': 'object',
                'properties': {'keys': {'type': 'string', 'description': '按键组合'}},
                'required': ['keys'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'type_text',
            'description': '像用键盘一样把文本输入到当前焦点处(支持中文)。'
                           '只用于往"你已经打开的窗口"里的输入框敲字; '
                           '生成文件一律用 write_file —— 用记事本 + 本工具敲代码'
                           '等于没写文件, 会被直接拒绝; 当前焦点是微信时同样拒绝'
                           '(那里是对方的聊天输入框, 敲进去会被当消息发出去)。',
            'parameters': {
                'type': 'object',
                'properties': {'text': {'type': 'string', 'description': '要输入的文本'}},
                'required': ['text'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'click',
            'description': '在屏幕坐标处点击鼠标。坐标是物理像素, 可先用 screenshot '
                           '和 ocr_screen 定位。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'x': {'type': 'integer'},
                    'y': {'type': 'integer'},
                    'button': {'type': 'string', 'description': 'left/right/middle, 默认 left'},
                    'double': {'type': 'boolean', 'description': '是否双击'},
                },
                'required': ['x', 'y'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'scroll',
            'description': '鼠标滚轮滚动, 正数向上, 负数向下。',
            'parameters': {
                'type': 'object',
                'properties': {'amount': {'type': 'integer'}},
                'required': ['amount'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'focus_window',
            'description': '按标题关键词把某个窗口切到前台(如 "记事本"、"Chrome")。',
            'parameters': {
                'type': 'object',
                'properties': {'title': {'type': 'string', 'description': '标题关键词'}},
                'required': ['title'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'paste_to_app',
            'description': '把一段文字粘贴进指定程序的窗口里(默认只粘贴, 不回车)。'
                           '专门用来"把话转交给电脑上开着的助手" —— 对方在微信里说'
                           '"让 codebuddy 干X/问一下 codebuddy Y", 就用 '
                           'paste_to_app(app="codebuddy", text="要转交的原话")。'
                           'app 按进程名或窗口标题找, 找不到会把当前窗口列出来。'
                           '内部会先把那个窗口切到前台并确认前台就是它, 确认不了'
                           '就一个键都不按(免得内容落进别的窗口)。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'app': {'type': 'string',
                            'description': '目标程序, 如 "codebuddy"、进程名或窗口标题'},
                    'text': {'type': 'string', 'description': '要粘贴进去的内容'},
                    'submit': {'type': 'boolean',
                               'description': '粘完是否回车提交, 默认 false 只粘贴'},
                },
                'required': ['app', 'text'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'shot_to_wechat',
            'description': '截取某个程序的主窗口, 并直接通过微信发回给对方。'
                           '对方说"截个图看看 codebuddy 现在怎么样了""把画面发我"'
                           '这类话时用它: 一次调用就完成"截图 + 发到微信", 不用自己'
                           '先 screenshot 再想办法发图。窗口被别的窗口挡住也照样能'
                           '抓到它本身的内容。默认截 codebuddy。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'app': {'type': 'string',
                            'description': '目标程序, 默认 "codebuddy"; '
                                           '也可以是进程名或窗口标题'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'wechat_send',
            'description': '给微信里某个联系人单独发一条文字消息, 全程后台静默'
                           '(不抢前台、不碰剪贴板)。要"主动给某某发一条""提醒某某"'
                           '时用它, 不要用 send_keys/type_text 往微信里敲。'
                           '只能发给会话列表里存在的人(最近有过聊天记录); '
                           '每个人第一次会自动建一个独立聊天窗, 那一下会闪一次前台, '
                           '之后同一个人再发就完全不打扰。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'contact': {'type': 'string',
                                'description': '对方在微信里的昵称或备注, '
                                               '要跟会话列表里显示的写法一致'},
                    'text': {'type': 'string', 'description': '要发送的内容'},
                },
                'required': ['contact', 'text'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'wechat_windows',
            'description': '列出当前已经打开的微信独立聊天窗(标题即对方昵称), '
                           '用来确认"现在能直接静默发给谁"以及窗口在不在屏幕外。',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_windows',
            'description': '列出当前所有可见窗口的标题, 用来知道屏幕上现在有什么。',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'screenshot',
            'description': '截取当前全屏并保存, 返回图片路径。'
                           '需要确认界面状态时配合 ocr_screen 使用。',
            'parameters': {
                'type': 'object',
                'properties': {'tag': {'type': 'string', 'description': '文件名标记, 可选'}},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'ocr_screen',
            'description': '识别屏幕上的文字(这就是你的"眼睛")。'
                           '可传 box="x1,y1,x2,y2" 只看某个区域。',
            'parameters': {
                'type': 'object',
                'properties': {'box': {'type': 'string',
                                       'description': '可选, 形如 "0,0,800,600"'}},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_clipboard',
            'description': '读取当前剪贴板文本内容。',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_clipboard',
            'description': '把文本写入剪贴板。',
            'parameters': {
                'type': 'object',
                'properties': {'text': {'type': 'string'}},
                'required': ['text'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'sleep',
            'description': '等待若干秒(等程序启动、等界面加载)。',
            'parameters': {
                'type': 'object',
                'properties': {'seconds': {'type': 'number'}},
                'required': ['seconds'],
            },
        },
    },
]

# 联网工具也挂到同一份清单上 —— agent 只认 pc_tools.TOOL_SPECS 这一个出口
TOOL_SPECS = TOOL_SPECS + net_tools.TOOL_SPECS
# 技能(use_skill/list_skills)与子代理(subagent): 同样是"工具", 走同一个出口。
# 两个模块都不 import pc_tools(子代理是延迟 import), 所以这里加 import 不会成环。
TOOL_SPECS = TOOL_SPECS + skills.TOOL_SPECS + subagent.TOOL_SPECS + chain_bridge.TOOL_SPECS

_SPEC_BY_NAME = {s['function']['name']: s['function'] for s in TOOL_SPECS}


def _wanted_args(name):
    """这个工具到底要哪些参数 —— 报错时必须一并告诉它。

    实测 15:07 "帮我在桌面创建文件夹…": 它把 content/folder_path/file_name 这组参数
    拿去调 sleep, 连着三轮只收到"参数不对: <lambda>() got an unexpected keyword
    argument 'content'" —— 既不知道 sleep 要什么, 也不知道写文件该用哪个工具, 改了
    三次参数还是同一份错的, 白烧 3 轮。把参数表和用途一起回给它才有自纠的可能。
    """
    fn = _SPEC_BY_NAME.get(name) or {}
    params = fn.get('parameters') or {}
    props = params.get('properties') or {}
    if not props:
        return ''
    need = params.get('required') or []
    listed = ', '.join(f'{k}(必需)' if k in need else k for k in props)
    desc = (fn.get('description') or '').strip().rstrip('。.；; ')   # 免得拼出"。。"
    return f'{name} 要的参数是: {listed}; 它的作用是: {desc}'


# 参数别名: 4B 会把同义的说法当参数名递进来。实测五类回归 py开发 第1轮:
# write_file{"path": "C:\\Users\\User\\Desktop\\regress_5\\hello_5.py",
#            "text": "print(\"Hello 5\")"} —— 文件名对、内容也对, 只因为叫 text 不叫
# content 就被打回。它之后**没有**改对参数名, 反而开始写 <文件路径> 这种占位符, 6 轮
# 全废。名字不同意思一样, 直接认下, 比回一句"参数不对"再赌它会改靠谱得多。
_ARG_ALIAS = {
    'text': 'content', 'body': 'content', 'data': 'content', 'code': 'content',
    'contents': 'content', 'source': 'content',
    'file_path': 'path', 'filepath': 'path', 'file': 'path', 'filename': 'path',
    'file_name': 'path', 'dir': 'path', 'directory': 'path', 'folder': 'path',
    'cmd': 'command', 'shell': 'command',
}

_PATH_ARGS = ('path', 'src', 'dst')
# 模型偷懒时会拿占位符交差(实测见到过 <文件路径> / <要写入的内容> / <complete-absolute-path>)。
_PLACEHOLDER_RE = re.compile(r'[<>]')


def _valid_params(name):
    """这个工具真正认的参数名 —— 别名只往真实存在的名字上换。"""
    fn = _SPEC_BY_NAME.get(name) or {}
    return set((((fn.get('parameters') or {}).get('properties')) or {}))


def _fix_path(p):
    """把模型"猜"出来的用户目录接回真实的那个。

    实测五类回归 py开发: 4B 把桌面的绝对路径猜成了
    C:\\Users\\User\\Desktop\\regress_5\\hello_5.py —— 用户名是它自己编的("User"),
    于是写入落在一个不存在的目录上。

    实测 10-07 21:47 微信真链路: 编排出来的路径是
    C:\\Users\\User\\Documents\\to_delete.txt —— 用户名同样是编的, 而这次**不是桌面**
    而是 Documents。旧版只认 \\Desktop 这一段, 于是原样放行, 写到受保护的 C:\\Users\\User
    上直接 WinError 5 拒绝访问("工具 write_file 被安全策略拒绝"), 任务彻底断掉。

    本机只有一个真实用户, 而 \\Users\\<谁> 这个目录**不存在**就说明整条路径是编的:
    这时把 "盘:\\Users\\<谁>" 这一段整个换成真家目录, 后面跟的子路径原样保留
    (桌面、文档、下载都是同一个道理, 不必逐个枚举)。
    """
    s = str(p or '').strip().strip('"\'')
    m = re.match(r'^([A-Za-z]):\\Users\\([^\\]+)(?:\\|$)', s, re.I)
    if not m or os.path.isdir(f'{m.group(1)}:\\Users\\{m.group(2)}'):
        return s                     # 不是这个形状, 或者那个用户是真的 —— 一个字都别动
    home = os.path.expanduser('~')
    rest = s[len(m.group(0)):].lstrip('\\')
    return os.path.join(home, rest) if rest else home


def _normalize_args(name, args):
    """纠正参数名和路径 —— 详见 _ARG_ALIAS / _fix_path 的注释。"""
    if not isinstance(args, dict):
        return args
    valid = _valid_params(name)
    out = {}
    for k, v in args.items():
        key = k
        if valid and key not in valid and _ARG_ALIAS.get(key) in valid:
            key = _ARG_ALIAS[key]
        if key in _PATH_ARGS and isinstance(v, str):
            v = _fix_path(v)
        out[key] = v
    return out


def _placeholder_arg(args):
    """哪个路径参数里填的是占位符(带尖括号) —— 填了占位符等于没填。"""
    for k in _PATH_ARGS:
        v = (args or {}).get(k)
        if isinstance(v, str) and _PLACEHOLDER_RE.search(v):
            return k
    return ''


_TOOL_FUNCS = {
    'run_command': run_command,
    'write_file': write_file,
    'run_py': run_py,
    'run_java': run_java,
    'list_files': list_files,
    'read_file': read_file,
    'edit_file': edit_file,
    'delete_file': delete_file,
    'open_app': open_app,
    'win_run': win_run,
    'send_keys': send_keys,
    'type_text': type_text,
    'click': click,
    'scroll': scroll,
    'focus_window': focus_window,
    'paste_to_app': paste_to_app,
    'shot_to_wechat': shot_to_wechat,
    'wechat_send': wechat_send,
    'wechat_windows': wechat_windows,
    'list_windows': list_windows,
    'screenshot': screenshot,
    'ocr_screen': ocr_screen,
    'get_clipboard': get_clipboard,
    'set_clipboard': set_clipboard,
    'sleep': lambda seconds: (time.sleep(min(float(seconds), 30)),
                              f'已等待 {seconds}s')[1],
}
# 联网工具共用同一张分发表
_TOOL_FUNCS.update(net_tools.TOOL_FUNCS)
# 技能 / 子代理
_TOOL_FUNCS.update(skills.TOOL_FUNCS)
_TOOL_FUNCS.update(subagent.TOOL_FUNCS)
_TOOL_FUNCS.update(chain_bridge.TOOL_FUNCS)

# 键鼠/剪贴板都是"抢焦点"的操作, 调用方可能不止一个线程, 这里统一串行。
# 锁实体借用 sender.INPUT_LOCK —— main.py 是直接调 sender.send_to()/send_image_to()
# 的, 不经过本模块的 dispatch; 而转交之后的后台回图线程走的是 dispatch。两边若不是
# 同一把锁, 一次 ctrl+V 就可能被另一个线程的窗口切换带跑, 粘进错误的窗口。
_INPUT_LOCK = sender.INPUT_LOCK


def dispatch(name, args):
    """执行一次工具调用, 返回给模型看的文本结果。异常一律转成文本, 不抛出。"""
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        return f'没有这个工具: {name}'
    args = _normalize_args(name, args)
    holder = _placeholder_arg(args)
    if holder:
        desk = os.path.join(os.path.expanduser('~'), 'Desktop')
        return (f'{name} 的 {holder} 里写的是占位符(<...>), 那不是真路径, 等于没填。'
                f'别写 "<文件名>" 这种东西, 立刻用真实路径重调一次: '
                f'真桌面就是 {desk}, 要建的文件写成 {desk}\\你要的文件名, '
                f'要放进子文件夹就写成 {desk}\\子文件夹名\\你要的文件名。')
    try:
        with _INPUT_LOCK:
            return str(fn(**(args or {})))
    except TypeError as e:
        hint = _wanted_args(name)
        # 实测 10-07 21:47: write_file 少给 content, 只收到"参数不对: ... missing ...
        # 'content'" —— 4B 没去补参数, 反而开始长篇解释, 再读一次占位符路径, 最后
        # 用一句英文放弃。光说"不对"不够, 必须明确要求它**立刻**带全参数重调一次。
        return (f'工具 {name} 参数不对: {e}。' + (f'{hint}。' if hint else '')
                + f'立刻按上面的参数表补齐, 用真实路径重调一次 {name}; '
                  f'不要解释、不要写计划、不要放弃。')
    except PermissionError as e:
        return f'工具 {name} 被安全策略拒绝: {e}'
    except Exception as e:
        return f'工具 {name} 执行出错: {type(e).__name__}: {e}'


def _enum_windows_by_title(keyword):
    """按标题关键词枚举可见顶层窗口 -> [(hwnd, title)]。"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    out = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(h, _l):
        if not u.IsWindowVisible(h):
            return True
        n = u.GetWindowTextLengthW(h)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        if buf.value and keyword.lower() in buf.value.lower():
            out.append((h, buf.value))
        return True

    u.EnumWindows(proto(_cb), 0)
    return out


def shot_chat(contact='', tag=None):
    """静默截"某个联系人的独立聊天窗"(park 在屏幕外的那种) —— 不抢前台、不改桌面。

    为什么不截主窗口: 主窗口经常被最小化, PrintWindow 抓回来是歪斜残帧(实测),
    而屏幕抓取又会拍到遮挡物、还得把窗口掀起来。微信给每个会话开的独立聊天窗
    (park 到 -12000,-12000) 用 PrintWindow 抓出来是完整清晰的, 且全程不碰前台。
    contact 为空时截最上面那个聊天子窗。
    """
    import os
    import time
    import ctypes
    from core import win as cwin

    u = ctypes.windll.user32
    cands = []
    for h, title in _enum_windows_by_title(contact or ''):
        try:
            l, t, w, hh = cwin._rect(h)
        except Exception:                                      # noqa: BLE001
            continue
        # 聊天子窗特征: 小窗(300~900 宽)、独立顶层、标题就是备注
        if 280 <= w <= 900 and 300 <= hh <= 1200:
            cands.append((h, title))
    if not cands:
        return f'没找到标题含 {contact!r} 的聊天子窗（先给它建一个/发一条消息再截）'

    h, title = cands[0]
    shots = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shots')
    os.makedirs(shots, exist_ok=True)
    name = (tag or contact or 'chat').strip() or 'chat'
    safe = ''.join(ch for ch in name if ch.isalnum() or ch in '-_')[:20] or 'chat'
    path = os.path.join(shots, f'{safe}_{time.strftime("%m%d_%H%M%S")}.png')
    fg0 = u.GetForegroundWindow()
    try:
        cwin.grab(h, path=path)          # PrintWindow: 不开前台
    except Exception as e:                                     # noqa: BLE001
        return f'截 {title!r} 失败: {type(e).__name__}: {e}'
    changed = u.GetForegroundWindow() != fg0
    return (f'已截 {title!r} -> {path}（静默截屏, 前台{"有变化" if changed else "没动"}）')


TOOL_SPECS.append({
    'type': 'function',
    'function': {
        'name': 'shot_chat',
        'description': '静默截取某个联系人的独立聊天窗(不抢前台、不掀窗口), 返回图片路径。'
                       '想"看看对方最近发了什么/截图发给对方"时用这个; 不要用全屏截图。',
        'parameters': {
            'type': 'object',
            'properties': {
                'contact': {'type': 'string', 'description': '联系人备注(窗口标题里出现的词), 留空截最上面那个聊天窗'},
            },
            'required': [],
        },
    },
})
_TOOL_FUNCS['shot_chat'] = shot_chat


if __name__ == '__main__':
    print('DPI_aware =', _dpi_ok, ' 屏幕 =', screen_size(), ' 光标 =', cursor_pos())
    print('可用工具:', ', '.join(s['function']['name'] for s in TOOL_SPECS))
