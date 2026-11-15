# -*- coding: utf-8 -*-
"""微信窗口操作: 识别 / 静默切换会话 / 焦点归还。

实测结论(2026-11-01, WeChat 4.x / Qt 5.15.14):
  1. 搜索面板不是独立进程, 而是同一微信进程(pid 相同)的独立顶层窗口,
     class = Qt51514QWindowToolSaveBits。既然是独立 hwnd, 就可以直接用
     PostMessage 往它投递, 不需要它在前台。
  2. 往面板 PostMessage(WM_IME_CHAR) 打字, 面板**失焦时依然生效**;
     再 Post 一个回车, 就能选中第一条结果并进入该会话 —— 同样失焦也生效。
  3. 唯一绕不开激活的是"点放大镜打开面板"那一下(鼠标点击会让窗口自己激活)。
     所以流程是: 点开 -> 立刻把前台还回去 -> 之后全程 PostMessage。
     实测前台全程稳定在用户原来的窗口, 只在开面板瞬间闪动约 0.05s。
  4. 伪造方向键要 lParam 正确: 扫描码必须按 vk 取(MapVirtualKey), 方向键还要
     置 bit24 扩展位; 之前把扫描码固定写成 0x1C(回车), 于是"键送达但不动"。
     改对后**只有微信在前台时才切换会话** —— Qt 丢弃非活动窗口的键盘事件。
     所以键盘切换与静默互斥, 不要指望它替代点击。
  5. 字符输入与回车发送**可以静默**(此条纠正先前"打字必抢前台"的错误结论)。
     前提是: 投递之前**不要动前台**——不要 SetForegroundWindow, 更不要
     AttachThreadInput 强夺。前台由用户自己持有时, 直接往目标 hwnd 投字符:
       - 19 字 + Post 回车: 275 次采样(10ms)偏离 0, 全程 3.1s, 消息正常送达;
       - 5 字两组(用户正在用输入法打字 / 已停手): 各 87 次采样, 偏离均为 0。
     反过来, 一旦脚本先调 restore_fg 把前台从微信手里抢走, 微信之后收到字符
     就会持续抢回: 实测 1 字 174/174、22 字 201/202、20 字 724/872 偏离,
     此时 fg_guard 也压不住(夺回后立刻被抢走, 变成每秒上百次闪烁)。
     结论: 静默发送 = 不碰前台 + 直接 PostMessage; 归还前台只在最后调一次
     restore_fg, 绝不要边投边守。
"""
import ctypes
import ctypes.wintypes as wt
import os
import time

u = ctypes.windll.user32
k32 = ctypes.windll.kernel32

# Windows 默认时钟中断粒度 15.6ms: time.sleep(0.02) 实测睡 30~36ms, 按字符投递
# 27 字就白等近 1 秒。把精度提到 1ms, sleep 才和参数对得上。
try:
    ctypes.windll.winmm.timeBeginPeriod(1)
except Exception:                                              # noqa: BLE001
    pass

WM_IME_CHAR = 0x0286
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002

PANEL_CLASS = 'QWindowToolSaveBits'

# 放大镜候选点击位(客户区坐标)。微信左栏宽度固定, 搜索框位置随窗口高度不变;
# 给几个邻近候选依次试, 命中即停 —— 比硬编码一个坐标稳, 且都远离右侧"+"按钮
# (实测两者相距 37px, 偏 15px 内仍不会误点到新建)。
MAGNIFIER_CANDIDATES = [(166, 57), (166, 26), (158, 40), (174, 40)]   # 684 宽(大号)标定
# 896 宽(小号)标定: 搜索框在 x 77~255, y 43~68, 右侧 '+' 在 x≈280 不能碰
MAGNIFIER_WIDE = [(150, 56), (120, 56), (200, 56), (91, 56)]


def _cls(h):
    b = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(h, b, 256)
    return b.value


def _title(h):
    b = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(h, b, 256)
    return b.value


def _rect(h):
    r = wt.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def pid_of(h):
    d = wt.DWORD()
    u.GetWindowThreadProcessId(h, ctypes.byref(d))
    return d.value


def find_panel():
    """当前是否存在搜索面板(独立顶层窗口), 返回 hwnd 或 None"""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(h) and PANEL_CLASS in _cls(h):
            _, _, w, hh = _rect(h)
            # 同类窗口不止一个, 其中存在 0x0 的隐藏辅助窗口。
            # 只认真正有面积的弹层, 否则会把文字发进空气里。
            if w > 100 and hh > 50:
                found.append(h)
                return False
        return True

    u.EnumWindows(cb, 0)
    return found[0] if found else None


def restore_fg(target):
    """把前台还给 target。非前台进程调 SetForegroundWindow 会被系统拒绝,
    必须先把本线程输入队列挂到当前前台线程上(AttachThreadInput)再调。

    实测补充(2026-11-01): 微信刚自己抢到前台的那一瞬间, AttachThreadInput
    这条路会被拒(SetForegroundWindow 返回 False, 前台仍是微信); 此时先按下
    ALT 再调就能过, 松开 ALT 即恢复。fg_guard 5ms 轮询恰好总落在这个瞬间,
    所以必须有这条回退, 否则夺回会静默失败、前台一直停在微信。"""
    if not target:
        return False
    fg = u.GetForegroundWindow()
    if fg == target:
        return True
    cur = k32.GetCurrentThreadId()
    ftid = u.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if ftid and ftid != cur:
        attached = bool(u.AttachThreadInput(cur, ftid, True))
    try:
        ok = bool(u.SetForegroundWindow(target))
    finally:
        if attached:
            u.AttachThreadInput(cur, ftid, False)
    if ok and u.GetForegroundWindow() == target:
        return True
    u.keybd_event(VK_MENU, 0, 0, 0)
    try:
        u.SetForegroundWindow(target)
    finally:
        u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    return u.GetForegroundWindow() == target


def watch_fg(target, dur=0.35, gap=0.008):
    """投递后短时盯守: 微信会自激活, 抢到就立刻还回去。

    为什么只盯 350ms: 实测微信的自动激活都落在投递后 300ms 内, 再长没有
    收益, 反而会跟用户自己切窗口打架。gap 8ms 是"夺回够快"和"别烧 CPU"
    的折中 —— 实测 20/20 轮可见占用 ≤21ms, 用户感知不到。"""
    if not target:
        return 0
    t_end = time.time() + dur
    n = 0
    while time.time() < t_end:
        if u.GetForegroundWindow() != target:
            restore_fg(target)
            n += 1
        time.sleep(gap)
    return n


def _post_click(hwnd, x, y):
    lp = (y << 16) | (x & 0xFFFF)
    u.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    u.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, lp)
    u.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)


# 微信输入框只认这个当换行(实测)。'\n'(10) / '\r'(13) / U+0085 / U+000B
# 投进去全被吞掉, 两段文字直接粘成一行。Shift+Enter 也走不通: Qt 用
# GetKeyState 判修饰键, PostMessage 的 VK_SHIFT 不改目标线程的键盘状态,
# 结果被当成普通回车, 直接把半截内容发了出去。
LINE_SEP = '\u2028'


def post_text(hwnd, text, delay=0.08):
    """按字符投递(WM_IME_CHAR 传的是字符不是按键), 目标窗口无需前台。

    delay 用忙等而不是 time.sleep: Windows 定时器粒度 15.6ms, sleep(0.006)
    实测仍要睡 15.6ms, 26 字的话术白多花 250ms。忙等才让 delay 真的是 delay。

    换行统一转成 U+2028(见 LINE_SEP): 调用方照常写 '\n' 就行。
    """
    for ch in text:
        if ch in '\r\n':
            ch = LINE_SEP
        u.PostMessageW(hwnd, WM_IME_CHAR, ord(ch), 1)
        end = time.perf_counter() + delay
        while time.perf_counter() < end:
            pass


def post_key(hwnd, vk):
    u.PostMessageW(hwnd, WM_KEYDOWN, vk, 0x001C0001)
    time.sleep(0.02)
    u.PostMessageW(hwnd, WM_KEYUP, vk, 0xC01C0001)


def _is_blank_arr(arr):
    """整张图是不是一片纯色(PrintWindow 对 GPU 合成窗口常见"成功但全黑")。"""
    try:
        return int(arr.max()) - int(arr.min()) < 6
    except Exception:                                          # noqa: BLE001
        return False


def _grab_screen(l, t, w, h):
    """从屏幕 DC BitBlt 抓一块区域(借鉴 imhere/sender._grab_screen)。

    PrintWindow 对微信这类 DirectComposition 窗口会给出残帧/错位图,
    只有"窗口真在前台、真可见"时, 屏幕抓取才是准的。
    """
    import numpy as np
    from PIL import Image

    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                             ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    screen = user32.GetDC(0)
    if not screen:
        return None
    mem = bmp = None
    try:
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
        if not mem or not bmp:
            return None
        gdi32.SelectObject(mem, bmp)
        if not gdi32.BitBlt(mem, 0, 0, w, h, screen, l, t, 0x00CC0020):
            return None

        class BMI(ctypes.Structure):
            _fields_ = [('biSize', wt.DWORD), ('biWidth', ctypes.c_long),
                        ('biHeight', ctypes.c_long), ('biPlanes', wt.WORD),
                        ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
                        ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', ctypes.c_long),
                        ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', wt.DWORD),
                        ('biClrImportant', wt.DWORD)]

        bi = BMI()
        bi.biSize = ctypes.sizeof(BMI)
        bi.biWidth, bi.biHeight = w, -h
        bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 24, 0
        buf = ctypes.create_string_buffer(w * h * 3)
        if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0) != h:
            return None
        img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)[:, :, ::-1].copy()
        return None if _is_blank_arr(img) else img
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem:
            gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, screen)


def grab(hwnd, path=None, prefer_screen=False):
    """截取指定窗口。返回 numpy 数组, 传 path 时同时存盘。

    抓法(借鉴 imhere):
      1) 窗口在前台/可见 或 prefer_screen -> 屏幕 DC 抓(最准, 微信必须走这条);
      2) 否则 PrintWindow(PW_RENDERFULLCONTENT, 0x2) —— 不要求前台, 但微信这类
         DirectComposition 窗口可能给出残帧/错位图;
      3) PrintWindow 出全黑图时, 只要窗口在屏幕内就退回屏幕 DC。
    """
    import numpy as np
    from PIL import Image

    gdi32 = ctypes.windll.gdi32
    # 64 位下 HDC/HBITMAP 都是指针宽度: 不声明 restype/argtypes 会被按 c_int
    # 截断, 句柄一大就抛 OverflowError: int too long to convert。
    u.GetWindowDC.argtypes = [ctypes.c_void_p]
    u.GetWindowDC.restype = ctypes.c_void_p
    u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    u.PrintWindow.restype = ctypes.c_bool
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_uint]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]

    l, t, w, h = _rect(hwnd)
    # 最小化的窗口 rect 只剩 -32000/160x28, PrintWindow 抓回来是残帧(实测微信截图
    # 出来一张歪斜叠影)。先无焦点恢复, 抓完再按原样最小化回去 —— 不改变用户桌面状态。
    was_iconic = bool(u.IsIconic(hwnd))
    if was_iconic:
        u.ShowWindow(hwnd, 4)          # SW_SHOWNOACTIVATE: 显示但不抢焦点
        time.sleep(0.45)
        l, t, w, h = _rect(hwnd)
    try:
        return _grab_inner(hwnd, l, t, w, h, path, prefer_screen)
    finally:
        if was_iconic and u.IsWindow(hwnd):
            u.ShowWindow(hwnd, 6)      # SW_MINIMIZE: 恢复原来的最小化状态


def _grab_inner(hwnd, l, t, w, h, path=None, prefer_screen=False):
    import numpy as np
    from PIL import Image

    gdi32 = ctypes.windll.gdi32
    # 被 park 到 -32000 的窗口, 屏幕 DC 抓出来是空白: 只能走 PrintWindow。
    on_screen = (l > -10000) and (t > -10000) and u.IsWindowVisible(hwnd)
    is_fg = u.GetForegroundWindow() == hwnd

    arr = None
    if on_screen and (prefer_screen or is_fg):
        arr = _grab_screen(l, t, w, h)

    if arr is None:
        hdc = u.GetWindowDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        try:
            u.PrintWindow(hwnd, mdc, 0x2)

            class BMI(ctypes.Structure):
                _fields_ = [('biSize', wt.DWORD), ('biWidth', ctypes.c_long),
                            ('biHeight', ctypes.c_long), ('biPlanes', wt.WORD),
                            ('biBitCount', wt.WORD), ('biCompression', wt.DWORD),
                            ('biSizeImage', wt.DWORD), ('biXPelsPerMeter', ctypes.c_long),
                            ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', wt.DWORD),
                            ('biClrImportant', wt.DWORD)]

            bi = BMI()
            bi.biSize = ctypes.sizeof(BMI)
            bi.biWidth, bi.biHeight = w, -h          # 负高度 = 自上而下
            bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3][:, :, ::-1]
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            u.ReleaseDC(hwnd, hdc)
        # PrintWindow 全黑: 窗口在屏幕内就用屏幕抓, 否则只能认了
        if _is_blank_arr(arr) and on_screen:
            arr = _grab_screen(l, t, w, h) or arr
    img = Image.fromarray(np.array(arr), 'RGB')
    if path:
        img.save(path)
    return np.array(img)


def open_search(wechat_hwnd, keep_fg=None, wait=1.0, stable=0.5):
    """点开放大镜打开搜索面板。返回 (面板 hwnd 或 None)。
    keep_fg: 打开后要归还给的前台窗口(用户原来的窗口), 传 None 不归还。
    这是整条链路里唯一会抢焦点的动作, 所以一打开就还回去。

    stable: 同类弹层不止一种, 有一种 368x225 的假面板 0.2s 内就自己消失
            (实测), 把字符投给它等于投给空气, 后面的回车也落空。
            所以命中后要等 stable 秒复核存活, 假面板直接放弃并换下一候选。

    注意 keep_fg: 这里**不能**归前台。面板是 Qt::Popup, 一旦失活会自动关闭
    (实测: 归还后 0.2s 内面板销毁, 后续字符全部落空)。归还统一放到
    goto_chat 的最后一步, 也就是回车选完会话之后。"""
    p = find_panel()
    if p:
        return p
    deadline = time.time() + wait
    # 搜索框位置随窗口宽度变(左栏变宽), 宽窗优先试宽窗那组, 两组都要兜底
    _, _, _w, _ = _rect(wechat_hwnd)
    if _w >= 800:
        cands = MAGNIFIER_WIDE + MAGNIFIER_CANDIDATES
    else:
        cands = MAGNIFIER_CANDIDATES + MAGNIFIER_WIDE
    for x, y in cands:
        _post_click(wechat_hwnd, x, y)
        for _ in range(10):
            time.sleep(0.05)
            p = find_panel()
            if p:
                time.sleep(stable)
                if u.IsWindow(p):
                    return p
                break                    # 短命假面板, 试下一个坐标
        if time.time() > deadline:
            break
    return None


def goto_chat(wechat_hwnd, word, keep_fg=None, settle=1.6, warmup=0.45):
    """静默把微信切到 word 对应的会话(通常是备注前几位/昵称)。

    链路: 点开搜索 -> Post 文字 -> Post 回车 -> 归还前台。
    返回 True/False。

    注意: 从点开放大镜到回车选完, 微信会一直占着前台(约 1.5~2s), 因为搜索
    面板是 Qt::Popup, 失活即关闭 —— 想"闪一下就还"是做不到的, 那只会把面板
    关掉、字符投空。所以调用方要预期这段短暂的前台占用。

    warmup: 面板刚弹出时输入框还没就绪, 立刻打字会丢字符, 先等一会。
    settle: 打完字要等结果列表替换成搜索结果, 回车才命中目标。
            面板初始显示的是"最近聊天", 抢在刷新前回车会进错会话(已踩)。
    """
    fg0 = keep_fg or u.GetForegroundWindow()
    panel = open_search(wechat_hwnd)
    if not panel:
        restore_fg(fg0)
        return False
    try:
        time.sleep(warmup)
        # 每个字符投递前重新取面板: 面板可能被销毁重建, 拿旧句柄会投空
        for ch in word:
            cur = find_panel()
            if not cur:
                return False
            u.PostMessageW(cur, WM_IME_CHAR, ord(ch), 1)
            time.sleep(0.08)
        time.sleep(settle)               # 等结果列表刷新出来再回车
        cur = find_panel()
        if not cur:
            return False
        post_key(cur, VK_RETURN)         # 回车选中第一条结果并进入会话
        for _ in range(20):
            time.sleep(0.05)
            if not find_panel():
                return True
        return False
    finally:
        p = find_panel()
        if p:
            post_key(p, VK_ESCAPE)
            time.sleep(0.2)
            p = find_panel()
            if p:
                post_key(p, VK_ESCAPE)
        # 面板刚关时 SetForegroundWindow 会被系统拒绝(实测), 等一下再试一次
        restore_fg(fg0)
        if u.GetForegroundWindow() != fg0:
            time.sleep(0.25)
            restore_fg(fg0)


# ============================================================================
# 方案 A: 截屏定位 + 点会话列表 (全程零激活)
#
# 为什么不用搜索面板: 面板是 Qt::Popup, 弹出时微信自己调 SetForegroundWindow,
# 一定抢前台, 而且失活即关闭 —— 只能"抢满整个切会话过程"。而点会话列表行
# 是对普通控件投递 PostMessage, 实测 0 次前台偏移(小号 403 次采样验证)。
#
# 定位不靠文字(窗口窄, 名字被截成 '10242...' 无法区分), 靠头像模板匹配:
# 模板与目标行实测相似度 1.0000, 次优 0.88, 区分度足够。
# ============================================================================

AV_X0, AV_W = 76, 40          # 左栏头像方框(客户区坐标), 由大号 684x911 标定
LIST_Y0 = 80                  # 会话列表起始 y(搜索栏下方)
CLICK_X = 170                 # 点行时用的 x, 落在名字区, 不压头像也不碰时间戳
TMPL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'assets', 'tmpl')


def tmpl_path(key):
    return os.path.join(TMPL_DIR, f'{key}_avatar.png')


def _load_tmpl(key):
    from PIL import Image
    import numpy as np
    return np.array(Image.open(tmpl_path(key)).convert('RGB'))


def tmpl_exists(key):
    """该联系人是否已建过头像模板(决定能不能走零占用的列表点击)。"""
    return bool(key) and os.path.exists(tmpl_path(key))


def find_hwnd_by_tmpl(key, hwnds, thresh=0.96, margin=0.06, verbose=True):
    """在候选窗口里找出"会话列表含该头像"的那个, 返回 (hwnd, score)。

    双开实测(2026-11-01): 两个账号的主窗口标题都是"微信", 类名相同, 启动命令
    行也一模一样 —— 标题/进程/句柄属性全都分不出是谁的窗口, 猜错就是发错账号。
    唯一可靠的区分是会话列表里的头像: 用目标联系人的模板去匹配, 命中的那个窗口
    才是"有这个人"的账号。两个都没有时返回 (0, 0.0), 由调用方决定回退还是放弃。
    """
    if not tmpl_exists(key):
        return 0, 0.0
    tmpl = _load_tmpl(key)
    hits = []
    for h in hwnds:
        try:
            img = grab(h)
            _, best, second = match_row(img, tmpl)
        except Exception as e:                                  # noqa: BLE001
            if verbose:
                print(f'[win] 窗口 {h} 取图失败: {e}')
            continue
        if verbose:
            print(f'[win] 窗口 {h} 模板 {key}: score={best:.4f} 次优={second:.4f}')
        if best >= thresh and best - second >= margin:
            hits.append((h, best))
    if len(hits) > 1:
        # 双开实测(2026-11-02): 两个号昵称和头像都设成同一张图时, 一个模板会在
        # 两个窗口都拿到 0.99+ (实测 196734 score=1.0000 / 132646 score=0.9991),
        # 此时返回"第一个命中的"等于随便挑一个号 —— 正是发错账号。这种情况必须
        # 判为"该模板不能区分账号", 交给调用方用更硬的证据(指定 hwnd)或直接拒发。
        if verbose:
            print(f'[win] 模板 {key} 在 {len(hits)} 个窗口都命中 '
                  f'{[(h, round(s, 4)) for h, s in hits]}, 无法区分账号, 放弃')
        return 0, 0.0
    return hits[0] if hits else (0, 0.0)


def capture_avatar(wechat_hwnd, key, y_center):
    """把左栏某一行的头像存成模板(新联系人建档用)。

    y_center: 该行中心的客户区 y。列表首行约 116, 每行间隔约 66。
    模板只是"图像指纹", 不涉及文字, 所以无所谓备注名怎么改。
    """
    from PIL import Image

    img = grab(wechat_hwnd)
    y0 = y_center - AV_W // 2
    crop = img[y0:y0 + AV_W, AV_X0:AV_X0 + AV_W]
    os.makedirs(TMPL_DIR, exist_ok=True)
    Image.fromarray(crop).save(tmpl_path(key))
    return tmpl_path(key)


def match_row(img, tmpl, x0=AV_X0, y_from=LIST_Y0, y_to=None, edge=8):
    """在左栏竖直滑窗匹配头像, 返回 (y_top, best, second)。

    second 是"离 best 至少 25px 的次优值", 用来算区分度: 微信里可能有同头像
    的两个号(自己给自己发), 只靠 best 高不够, 还要 margin 够大才算唯一命中。

    edge: 只比中心 (40-2*edge) 见方。头像有圆角, 四周 1~2px 会透出列表底色,
    选中态底色是绿的(实测相似度从 1.0000 掉到 0.9367, 闸门会误杀),
    而中心 24x24 在两种状态下都是 1.0000 —— 所以匹配只看中心。
    """
    import numpy as np

    if y_to is None:
        y_to = img.shape[0] - AV_W - 4
    inner = AV_W - 2 * edge
    ti = tmpl[edge:edge + inner, edge:edge + inner].astype(np.int16)
    scored = []
    for y in range(y_from, y_to):
        band = img[y + edge:y + edge + inner,
                   x0 + edge:x0 + edge + inner].astype(np.int16)
        scored.append((1.0 - float(np.abs(band - ti).mean()) / 255.0, y))
    scored.sort(reverse=True)
    best, best_y = scored[0]
    second = 0.0
    for s, y in scored[1:]:
        if abs(y - best_y) > 25:
            second = s
            break
    return best_y, best, second


def row_highlighted(img, y_center):
    """判断该行是否处于选中态(微信选中行底色是绿色, 实测 rgb 13,168,105)。"""
    import numpy as np

    y0, y1 = max(0, y_center - 28), min(img.shape[0], y_center + 28)
    x0, x1 = 62, 224
    box = img[y0:y1, x0:x1].astype(int)
    g, r, b = box[:, :, 1], box[:, :, 0], box[:, :, 2]
    green = (g > r + 40) & (g > b + 30)
    return float(green.mean()) > 0.25


def panel_blank(img, thresh=8.0):
    """右侧聊天区是不是"没打开会话"的空白页。

    空白页只有居中一只灰 logo, 整块标准差极低(实测 2.0), 有内容的会话
    是 20~80。用它当"点开了没有"的判据, 比看列表高亮可靠 —— 窗口一旦失焦,
    微信不再绘制选中行的绿色底(实测绿占比 0.000), 高亮判据在后台完全失效。
    """
    import numpy as np

    h, w = img.shape[:2]
    box = img[int(h * 0.11):int(h * 0.97), int(w * 0.36):int(w * 0.98)]
    return float(box.astype(int).std()) < thresh


DESKTOP_CLASSES = ('Progman', 'WorkerW', 'Shell_TrayWnd',
                   'Windows.UI.Core.CoreWindow')


def _usable_as_home(h, exclude=None, min_w=400, min_h=300):
    """能否充当要守护的前台窗口。

    桌面(Progman/WorkerW)和任务栏必须排除: 它们面积最大、最容易被选中,
    但 SetForegroundWindow(桌面) 拿不到真正的焦点 —— 实测守卫抢了 271 次、
    累计占用 3.0s 依然失败, 前台始终留在微信身上。
    """
    if not h or h == exclude:
        return False
    if not u.IsWindowVisible(h) or u.IsIconic(h):
        return False
    if _cls(h) in DESKTOP_CLASSES:
        return False
    if u.GetWindowTextLengthW(h) < 2:
        return False
    _, _, w, hh = _rect(h)
    return w >= min_w and hh >= min_h


def pick_home_window(exclude=None, min_w=400, min_h=300):
    cur = u.GetForegroundWindow()
    if _usable_as_home(cur, exclude, min_w, min_h):
        return cur

    """挑一个"用户正在用的窗口"当靶子: 可见、非最小化、有标题、不是微信。

    为什么要挑而不是直接用 GetForegroundWindow: 调用时前台可能恰好就是微信
    自己(上一轮操作留下), 那样盯守形同虚设 —— 实测就踩过, steal_ms 报 0 却
    其实一直占着前台。
    """
    cands = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if _usable_as_home(h, exclude, min_w, min_h):
            _, _, w, hh = _rect(h)
            cands.append((w * hh, h))
        return True

    u.EnumWindows(cb, 0)
    cands.sort(reverse=True)
    return cands[0][1] if cands else None


class fg_guard:
    """盯守前台: 一旦被别的窗口顶掉, 立刻夺回。

    为什么必须靠"夺回"而不能"不抢": 点微信会话列表会触发微信自己
    SetForegroundWindow, 实测必然激活(WS_EX_NOACTIVATE 也拦不住, 因为它
    显式调用), 这是它的行为, 改不了。
    但占用时长可以压: 5ms 轮询实测把占用从"一直占着"压到 ~0.05s。
    对比方案 C(搜索面板) 的 3.4s, 差 60 倍。
    """

    def __init__(self, home=None, interval=0.005, timeout=1.5):
        self.home = home or u.GetForegroundWindow()
        self.interval = interval
        self.timeout = timeout
        self.steals = 0
        self.first_delay = None
        self._stop = None
        self._th = None

    def __enter__(self):
        import threading

        self._stop = threading.Event()
        home = self.home
        stop = self._stop

        def run():
            t0 = time.time()
            while not stop.is_set() and time.time() - t0 < self.timeout:
                if u.GetForegroundWindow() != home:
                    self.steals += 1
                    if self.first_delay is None:
                        self.first_delay = time.time() - t0
                    restore_fg(home)
                time.sleep(self.interval)

        self._th = threading.Thread(target=run, daemon=True)
        self._th.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        if self._th:
            self._th.join()
        restore_fg(self.home)
        return False

    @property
    def steal_ms(self):
        return int((self.first_delay or 0) * 1000)


def _diff(a, b):
    """两帧的平均绝对差(0~255)。"""
    import numpy as np
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def _wait_panel(wechat_hwnd, timeout, step=0.04, quiet=1.0):
    """等聊天区切过去并且画面稳定。返回 (是否成功, 最后一帧截图)。

    为什么不能只看"非空白": 点击刚发出去的那一瞬间, 聊天区还停留在上一个会话 ——
    旧内容同样非空白, 于是 0.28s 就判定成功(实测截图仍是上一个会话, 实际 0.5s 后
    才切过去)。所以要等"连续两帧几乎不差", 才算渲染完了。

    为什么不能用列表高亮: 窗口失焦时微信不绘制选中行的绿底(高亮占比实测 0.000),
    而这条链路全程都把前台让给别人, 高亮判据在这里根本用不了。

    点"已经选中"的那一行会让聊天区变空白(见 goto_chat_by_list 的注释), 空白永远
    满足不了"非空白", 会一路等到 timeout 返回 False, 由调用方重点一次。
    """
    prev = grab(wechat_hwnd)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(step)
        cur = grab(wechat_hwnd)
        if not panel_blank(cur) and _diff(prev, cur) < quiet:
            return True, cur
        prev = cur
    return (not panel_blank(prev)), prev


def goto_chat_by_list(wechat_hwnd, key, thresh=0.96, margin=0.06, settle=0.7, home=None):
    """点左栏会话列表切到 key 对应的会话。

    返回 dict: {'ok', 'reason', 'score', 'second', 'y', 'steal_ms'}

    闸门: 模板相似度 < thresh 或与次优差距 < margin, 一律不点 —— 宁可失败,
    也不要把消息发到"看起来差不多"的人身上。

    前台: 微信点击时会自己激活窗口, 无法阻止; 这里用 fg_guard 全程盯守夺回,
    实测占用 ~0.05s(方案 C 的搜索面板是 3.4s)。
    """
    if u.IsIconic(wechat_hwnd):
        u.ShowWindow(wechat_hwnd, 4)         # SW_SHOWNOACTIVATE: 还原但不抢前台
        time.sleep(0.5)

    home = home or pick_home_window(exclude=wechat_hwnd)
    if home in (None, wechat_hwnd):          # 靶子不能是微信自己, 否则等于没守
        home = None

    tmpl = _load_tmpl(key)
    img = grab(wechat_hwnd)
    best_y, best, second = match_row(img, tmpl)

    info = {'ok': False, 'reason': '', 'score': round(best, 4),
            'second': round(second, 4), 'y': best_y + AV_W // 2, 'steal_ms': 0}
    if best < thresh:
        info['reason'] = '相似度不足(目标可能不在可视列表)'
        return info
    if best - second < margin:
        info['reason'] = '区分度不足(存在同头像候选)'
        return info

    yc = best_y + AV_W // 2
    with fg_guard(home=home) as g:
        _post_click(wechat_hwnd, CLICK_X, yc)
        # 边守边截, 不额外占前台; 渲染完成就立刻往下走, 不再死等 settle
        ok, img2 = _wait_panel(wechat_hwnd, settle)
        # 微信 4.x: 点"已经选中"的那一行 = 取消选中, 聊天区会变空白(实测内容
        # 强度 30.8 -> 2.0)。所以空白就不能算成功, 要再点一次重新打开。
        if not ok:
            _post_click(wechat_hwnd, CLICK_X, yc)
            ok, img2 = _wait_panel(wechat_hwnd, settle)
        info['steal_ms'] = g.steal_ms

    if not ok:
        info['reason'] = '点击后聊天区仍然空白'
        return info

    info['ok'] = True
    info['reason'] = 'ok'
    return info


# ============================================================================
# 静默发文字: 投字符 + 投回车, 全程 PostMessage, 不碰前台
#
# 实测(2026-11-01, 小号 852904, 窗口在副屏):
#   WM_IME_CHAR 逐字投主窗口 + 回车, 5 轮共 1000+ 次 5ms 采样, 前台偏离 0。
#   对照: 剪贴板 + 真实键盘那条老路必须先把微信置前, 单次发送占前台 3~5s。
#   注: 字符必须投主窗口, 投搜索面板会全部落空(面板只吃真实键盘, 实测)。
#
# 前提: 聊天输入框要有焦点。刚切完会话时微信会自动聚焦输入框; 状态不明时
# 先调 focus_input() 点一下输入框(代价 ~50ms 前台闪烁, 由 fg_guard 夺回)。
# ============================================================================

def input_point(wechat_hwnd):
    """聊天输入框里的一个安全落点(客户区坐标)。

    输入框贴在右栏底部且随窗口尺寸变化, 所以按宽高比例算:
    684x911 落在 (479, 811), 896x648 落在 (627, 548), 都在框内, 且避开
    右下"发送"按钮与底部表情/文件/剪刀图标那一条。
    """
    _, _, w, h = _rect(wechat_hwnd)
    return int(w * 0.70), int(h - 100)


def focus_input(wechat_hwnd, home=None):
    """点一下聊天输入框让它拿到焦点, 随后立刻把前台夺回。

    点击本身拦不住微信激活(fg_guard 注释里的实测), 所以这里的代价就是
    一次 ~50ms 的前台闪烁, 换"后续投字符保证落在输入框里"。
    """
    x, y = input_point(wechat_hwnd)
    if u.IsIconic(wechat_hwnd):
        u.ShowWindow(wechat_hwnd, 4)
        time.sleep(0.4)
    home = home or pick_home_window(exclude=wechat_hwnd)
    if home == wechat_hwnd:
        home = None
    with fg_guard(home=home) as g:
        _post_click(wechat_hwnd, x, y)
        time.sleep(0.3)
        steal_ms = g.steal_ms
    return {'ok': True, 'point': (x, y), 'steal_ms': steal_ms}


def send_text(wechat_hwnd, text, home=None, delay=0.08, refocus=False, shot=None):
    """静默发一条文字消息, 不需要窗口在前台。

    链路: (可选)点输入框聚焦 -> 同步投 WM_IME_CHAR -> 投回车 -> 短时盯守。
    返回 dict: {'ok', 'steal_ms', 'text'}

    refocus: 状态不明时置 True, 先点一下输入框。已知刚切完会话则可省。
    delay:   仅 post_text 用; 这里的同步投递用不到, 保留参数是为了兼容旧调用。
    shot:    传路径则发完截一张存盘(排查用)。
    """
    if u.IsIconic(wechat_hwnd):
        u.ShowWindow(wechat_hwnd, 4)
        time.sleep(0.4)

    if refocus:
        # 点输入框会让微信激活一次(这一下拦不住); 之后投递期间微信自己就是
        # 前台, 不算"偏离", 结束时统一还回去。
        focus_input(wechat_hwnd, home=home)
        time.sleep(0.2)

    fg0 = u.GetForegroundWindow()

    # 同步投递: SendMessage 让微信当场消费字符(实测 16~21ms/条, 不吞字),
    # 而 PostMessage 每条异步排队, 同样要等它自己把消息消化完。
    # 关键结论(2026-11-02 实测): 字符投进输入框这个动作本身会触发微信自激活,
    # 与投递快慢无关 —— 所以要压的不是投递时长, 而是激活之后没人管的那段时间。
    for ch in text:
        u.SendMessageW(wechat_hwnd, WM_IME_CHAR, ord(ch), 1)
    u.PostMessageW(wechat_hwnd, WM_KEYDOWN, VK_RETURN, 0x001C0001)
    u.PostMessageW(wechat_hwnd, WM_KEYUP, VK_RETURN, 0xC01C0001)

    # 微信的自动激活有约 25% 概率拖到投递之后才发生, 这里盯 350ms 一抢就夺回:
    # 20/20 轮可见占用 ≤21ms。全程只碰前台这一次。
    watch_fg(fg0, dur=0.35)

    if shot:
        grab(wechat_hwnd, shot)
    return {'ok': True, 'steal_ms': 0, 'text': text}
