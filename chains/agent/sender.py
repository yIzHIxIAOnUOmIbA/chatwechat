# -*- coding: utf-8 -*-
"""UI 自动化发送(imhere)
激活: 直接 API 激活已登录的微信主窗口(FindWindow + ALT技巧抢前台, 不弹运行框不开新进程)
      仅当微信完全没运行时才直接启动 exe(不用运行窗口)
      窗口识别: 本机同时登录两个账号, 两个窗口标题/类名完全一样, 唯一可靠的区分点是
                截屏 OCR 左侧聊天列表里能不能认出目标联系人(见 config.OCR_MARKER)。
                因此 OCR 命中是"置信为目标窗口"的判据, 不是可选优化。
发送: ctrl+F 搜索固定词 config.SEARCH_WORD -> Enter 进会话 -> 再 OCR 复核一次 -> 粘贴 -> Enter 发送
前缀: 所有发送文本自动加 config.AI_PREFIX
经验: SendInput 在64位下报错87不可用; keybd_event 可用(已实测弹出运行框)
     中文输入法会吞 Enter, 需关IME + 多按 Enter 兜底
     SetForegroundWindow 受前台锁定限制, 需先模拟 ALT 键获取"用户输入"权限
"""
import ctypes, io, json, os, re, subprocess, sys, threading, time

import config

# core/ 是大小号共用的底层(静默投递/头像模板/前台守卫), 与 chains/agent 差两级目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core import win as cwin
from core import subwin as csub
from core import acct as cacct

user32 = ctypes.windll.user32
imm32 = ctypes.windll.imm32
kernel32 = ctypes.windll.kernel32

# 抢前台诊断/兜底用到的三个 API 必须先声明签名 ---------------------------------
# GetWindowThreadProcessId 不声明 argtypes 的话 hwnd 会按 c_int 传, 64 位句柄一旦
# 超过 2^31 就被截断成一个别的线程 id, 后面的 AttachThreadInput 会静默失败 ——
# "兜底兜了个寂寞"。句柄一律当 c_void_p 传。
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_ulong)]
user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
user32.AttachThreadInput.restype = ctypes.c_bool

# 抢焦点的操作(激活窗口 / 键鼠 / 剪贴板 / 发消息)必须全局串行。锁实体放在这里的原因:
# main.py 是直接调 send_to() 的, 不经过 pc_tools.dispatch; 而转交 CodeBuddy 之后的
# "回图"是后台线程发的 —— 两边若不是同一把锁, 一次 ctrl+V 就可能落进另一个窗口。
# pc_tools._INPUT_LOCK 直接引用本对象, 全程序只有这一把。
INPUT_LOCK = threading.RLock()

import ctypes.wintypes as _wintypes  # 必须显式 import: 子模块不会自动挂到 ctypes 上

_rect_t = _wintypes.RECT

user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
user32.PrintWindow.restype = ctypes.c_bool

VK_LWIN = 0x5B
VK_R = 0x52
VK_CTRL = 0x11
VK_V = 0x56
VK_F = 0x46
VK_A = 0x41
VK_BACK = 0x08
VK_ENTER = 0x0D
VK_DELETE = 0x2E
KEYEVENTF_KEYUP = 0x0002


def _key(vk, up=False):
    """keybd_event 按键(已实测可用)"""
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def _combo(*keys):
    for k in keys:
        _key(k)
        time.sleep(0.05)
    for k in reversed(keys):
        _key(k, up=True)
        time.sleep(0.05)


def _press_enter(n=2, gap=0.3):
    """连按 Enter(中文输入法可能吞回车, 多按兜底)"""
    for _ in range(n):
        _key(VK_ENTER)
        _key(VK_ENTER, up=True)
        time.sleep(gap)


def _type_ascii(text):
    """用 VK 码输入 ASCII 文本(搜索词是数字, 无 IME 问题)"""
    for ch in text:
        if '0' <= ch <= '9':
            vk = ord(ch)          # '0'=0x30 .. '9'=0x39
        elif 'a' <= ch <= 'z':
            vk = ord(ch.upper())
        elif 'A' <= ch <= 'Z':
            vk = ord(ch)
        else:
            continue
        _key(vk)
        _key(vk, up=True)
        time.sleep(0.02)


def _read_clipboard_text():
    """读回剪贴板文本(UTF-16LE), 供写入自检; 失败返回 None"""
    CF_UNICODETEXT = 13
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    # GlobalLock/GlobalUnlock 必须声明 argtypes: 句柄是指针宽度, 不声明会被按 c_int
    # 截断并抛 OverflowError(单独调用本函数时 _set_clipboard 还没跑过, argtypes 是空的)
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(0):
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def _set_clipboard(text):
    """写入剪贴板。OpenClipboard 失败(被其他进程占用)时重试3次, 仍失败必须抛错——
    绝不静默返回, 否则后续 ctrl+V 会粘贴旧内容(上次搜索用的备注=别人名字)。
    写入后读回自检, 粘贴前保证剪贴板里就是目标文本。"""
    CF_UNICODETEXT = 13
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    for _ in range(3):
        if user32.OpenClipboard(0):
            break
        time.sleep(0.2)
    else:
        raise OSError('OpenClipboard 失败(剪贴板被其他进程占用)')
    try:
        user32.EmptyClipboard()
        data = text.encode('utf-16-le') + b'\x00\x00'
        h = kernel32.GlobalAlloc(0x0042, len(data))
        if not h:
            raise OSError('GlobalAlloc 失败')
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
            kernel32.GlobalFree(h)
            raise OSError('GlobalLock 失败')
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            raise OSError('SetClipboardData 失败')
    finally:
        user32.CloseClipboard()
    # 读回自检: 写入后确认剪贴板就是目标文本, 不一致立即报错(防静默失效导致发错)
    if _read_clipboard_text() != text:
        raise OSError(f'剪贴板写入校验失败(实际内容≠目标文本): {text[:20]!r}')


def _read_clipboard_dib():
    """读回剪贴板位图尺寸 (w, h); 没有位图返回 None。给 _set_clipboard_image 自检用。"""
    CF_DIB = 8
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(0):
        return None
    try:
        h = user32.GetClipboardData(CF_DIB)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            hdr = ctypes.string_at(p, 12)          # BITMAPINFOHEADER 前 12 字节
            w = int.from_bytes(hdr[4:8], 'little', signed=True)
            hh = int.from_bytes(hdr[8:12], 'little', signed=True)
            return (w, abs(hh))
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def _set_clipboard_image(path):
    """把图片放进剪贴板, 格式 CF_DIB —— 微信输入框 ctrl+V 能直接粘出图。

    为什么拿 BMP 截头, 而不是手搓 DIB: PIL 存出来的 BMP 就是
    "14 字节 BITMAPFILEHEADER + DIB", 砍掉前 14 字节正好是 CF_DIB 要的东西
    (24 位、bottom-up、行 4 字节对齐), 不用自己算位深/对齐/翻转 —— 手搓这四样
    任何一样错了, 现象都是"ctrl+V 粘不出东西", 还看不出错在哪。

    与 _set_clipboard 同样的要求: OpenClipboard 重试 3 次; 写完读回自检尺寸,
    尺寸对不上立刻抛错(否则就是往对方会话里粘了一张上次遗留的旧图)。
    """
    from PIL import Image
    if not path or not os.path.exists(path):
        raise OSError(f'图片不存在: {path!r}')
    img = Image.open(path).convert('RGB')
    buf = io.BytesIO()
    img.save(buf, 'BMP')
    data = buf.getvalue()[14:]                     # 砍掉 BITMAPFILEHEADER
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    for _ in range(3):
        if user32.OpenClipboard(0):
            break
        time.sleep(0.2)
    else:
        raise OSError('OpenClipboard 失败(剪贴板被其他进程占用)')
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0042, len(data))    # GMEM_MOVEABLE|GMEM_ZEROINIT
        if not h:
            raise OSError('GlobalAlloc 失败(图太大?)')
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
            kernel32.GlobalFree(h)
            raise OSError('GlobalLock 失败')
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(8, h):          # 8 = CF_DIB
            kernel32.GlobalFree(h)
            raise OSError('SetClipboardData(CF_DIB) 失败')
    finally:
        user32.CloseClipboard()
    got = _read_clipboard_dib()
    if got != img.size:
        raise OSError(f'剪贴板图片校验失败(剪贴板 {got} ≠ 目标 {img.size})')


def _close_ime():
    """关闭前台窗口所属线程的输入法(英文模式), 防止 Enter 被 IME 截获"""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        ime = user32.ImmGetContext(hwnd)
        if not ime:
            return False
        ok = imm32.ImmSetOpenStatus(ime, False)
        user32.ImmReleaseContext(hwnd, ime)
        return bool(ok)
    except Exception:
        return False


def _win_size(hwnd):
    """返回 (宽, 高); 失败返回 (0, 0)"""
    rect = _rect_t()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return 0, 0
    return rect.right - rect.left, rect.bottom - rect.top


def _is_resizable(hwnd):
    """是否有 WS_THICKFRAME: 主窗口可自由调整大小, 语音/视频通话弹窗通常固定"""
    return bool(user32.GetWindowLongW(hwnd, -16) & 0x00040000)


class _BMIH(ctypes.Structure):
    _fields_ = [
        ('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32),
        ('biHeight', ctypes.c_int32), ('biPlanes', ctypes.c_uint16),
        ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
        ('biSizeImage', ctypes.c_uint32), ('biXPelsPerMeter', ctypes.c_int32),
        ('biYPelsPerMeter', ctypes.c_int32), ('biClrUsed', ctypes.c_uint32),
        ('biClrImportant', ctypes.c_uint32),
    ]


class _BMI(ctypes.Structure):
    _fields_ = [('bmiHeader', _BMIH), ('bmiColors', ctypes.c_uint32 * 3)]


def _is_blank_image(img, tol=6):
    """整幅图基本只有一种颜色(全黑/全白) -> 认定没抓到内容"""
    lo, hi = img.convert('L').resize((32, 32)).getextrema()
    return (hi - lo) <= tol


def _grab_screen(rect, w, h):
    """从屏幕 DC 拷窗口矩形(要求窗口可见/在前台); 空图或失败返回 None"""
    from PIL import Image
    gdi32 = ctypes.windll.gdi32
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
        gdi32.BitBlt(mem, 0, 0, w, h, screen, rect.left, rect.top, 0x00CC0020)
        bmi = _BMI()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h                # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 24
        buf = ctypes.create_string_buffer(w * h * 3)
        if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0) != h:
            return None
        img = Image.frombuffer('RGB', (w, h), buf, 'raw', 'BGR', 0, 1).copy()
        return None if _is_blank_image(img) else img
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem:
            gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, screen)


def _capture_window(hwnd):
    """PrintWindow 截取窗口内容 -> PIL Image; 失败返回 None"""
    try:
        from PIL import Image
    except ImportError:
        return None
    gdi32 = ctypes.windll.gdi32
    rect = _rect_t()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0 or w > 5000 or h > 5000:
        return None
    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return None
    mem = bmp = None
    try:
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        if not mem or not bmp:
            return None
        gdi32.SelectObject(mem, bmp)
        if not user32.PrintWindow(hwnd, mem, 2):   # PW_RENDERFULLCONTENT
            user32.PrintWindow(hwnd, mem, 0)       # 退回普通渲染
        bmi = _BMI()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h                # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 24
        buf = ctypes.create_string_buffer(w * h * 3)
        if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0) != h:
            return None
        img = Image.frombuffer('RGB', (w, h), buf, 'raw', 'BGR', 0, 1).copy()
        if not _is_blank_image(img):
            return img
        # PrintWindow 会"成功但画出一张全黑图"(某些 GPU 渲染的窗口) —— 旧版把它
        # 当有效图交给 OCR, OCR 认不出字, 最后就变成"复核失败"把回复吞掉。
        # 退回屏幕 DC: 发送流程里窗口已被置前, 那时屏幕上是真内容。
        print(f'[send] PrintWindow 截到空图(hwnd={hwnd} {w}x{h}), 改用屏幕 DC')
        return _grab_screen(rect, w, h)
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem:
            gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


_ocr_broken = False   # OCR 环境不可用后熔断, 后续直接跳过确认避免重复耗时
_ocr_crash = 0        # OCR 帮工子进程异常退出的累计次数
_OCR_CRASH_MAX = 3    # 连续异常这么多次才熔断: 偶发一次不该把目标会话复核彻底关掉
_OCR_TIMEOUT = 20     # 帮工最长等这么久(python 启动 + 导入 winrt + 识别)
_HERE = os.path.dirname(os.path.abspath(__file__))
_OCR_PROC = os.path.join(_HERE, 'ocr_proc.py')


def _ocr_window_text(img):
    """winrt OCR(Windows 自带引擎)识别图像中的文本; 失败返回 ''

    实现上 OCR 一律跑在一次性子进程里(ocr_proc.py), 原因见那个文件头:
    本机 winrt 的 _winrt_windows_globalization 会**段错误(0xc0000005)**,
    而段错误捕不住、也没有 traceback —— 它直接带走整个解释器, 现象就是
    "干完一件事进程就没了"(13:32:52 实测: 日志断在 [send] 搜索会话..., 
    run_err.log 一片空白)。改成子进程后, 它崩了只是这一次拿不到文字,
    主进程照样活着, 下一次复核照常做。
    """
    global _ocr_broken, _ocr_crash
    if _ocr_broken:
        return ''
    try:
        buf = io.BytesIO()
        img.save(buf, format='PNG')     # 预处理交给帮工, 识别效果与原实现一致
        p = subprocess.run([sys.executable, _OCR_PROC],
                           input=buf.getvalue(), capture_output=True,
                           timeout=_OCR_TIMEOUT, cwd=_HERE,
                           env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    except Exception as e:                                  # noqa: BLE001
        _ocr_crash += 1
        print(f'[send] OCR 子进程调用失败(第{_ocr_crash}次): '
              f'{type(e).__name__}: {e}')
        return ''
    if p.returncode != 0:
        _ocr_crash += 1
        print(f'[send] OCR 子进程异常退出 code={p.returncode} '
              f'(0x{p.returncode & 0xffffffff:08X}) 第{_ocr_crash}/{_OCR_CRASH_MAX} 次: '
              + repr(p.stderr.decode('utf-8', 'ignore').strip()[:160]))
        if _ocr_crash >= _OCR_CRASH_MAX:
            print('[send] OCR 连续异常, 熔断: 后续跳过 OCR 复核')
            _ocr_broken = True
        return ''
    try:
        data = json.loads(p.stdout.decode('utf-8', 'ignore') or '{}')
    except Exception as e:                                  # noqa: BLE001
        print(f'[send] OCR 子进程输出解析失败: {type(e).__name__}: {e}; '
              f'raw={p.stdout[:120]!r}')
        return ''
    if not data.get('ok'):
        print(f"[send] OCR 子进程报错: {data.get('err')}")
        return ''
    _ocr_crash = 0
    return data.get('text') or ''


# 左侧聊天列表所在的比例区域 (x0, y0, x1, y1)。
# 旧值 (0.06, 0.05, 0.40, 0.45) 在 y 方向只盖到 0.45h, 而目标联系人在聊天列表里
# 常排在 y≈0.55h~0.62h 一带 —— 复核区因此永远看不到它, 复核 100% 失败、回复被丢弃。
# 实测 (hwnd=69958, 1073x1057, 同一张截图各跑两遍结果一致):
#   (0.06,0.05,0.40,0.45) -> '三文件骺蝓助手'               覆盖率  0%  ← 旧复核区
#   (0.03,0.04,0.49,0.55) -> '0三文件助手'                  覆盖率  0%
#   (0.03,0.04,0.49,0.70) -> '0一只刂-吧文件助手'            覆盖率 50%
#   (0.04,0.04,0.47,0.62) -> '0一一 只 小 - 吧 三文件传助手'   覆盖率 67%  ← 采用
# 右界别往右挪: >0.50w 就侵入右侧聊天内容区, 识别率反而掉到 17%~50%。
_LIST_BOX = (0.04, 0.04, 0.47, 0.62)


def _list_box(img):
    """把 _LIST_BOX 换算成该图片尺寸下的像素框"""
    w, h = img.size
    return (int(w * _LIST_BOX[0]), int(h * _LIST_BOX[1]),
            int(w * _LIST_BOX[2]), int(h * _LIST_BOX[3]))


def _ocr_confirm_main(hwnd):
    """窗口已在前台: 截屏, 在左侧聊天列表区域 OCR, 命中"文件传输助手"等主窗口特征词
    PrintWindow 对后台 Qt 窗口只渲染缓存, 必须在前台截屏才可靠"""
    img = _capture_window(hwnd)
    if img is None:
        return False
    text = _ocr_window_text(img.crop(_list_box(img)))
    if not text:
        return False
    norm = text.replace(' ', '').replace('\u3000', '').replace('\n', '')
    hit = [m for m in config.WECHAT_MAIN_MARKERS if m in norm]
    if hit:
        print(f'[send] OCR确认主窗口特征 {hit}, hwnd={hwnd}')
        return True
    return False


def _win_title(hwnd):
    """取窗口标题文本; 失败返回 ''"""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def _win_class(hwnd):
    """取窗口类名; 失败返回 ''。抢前台失败时要打印它 —— 光有句柄和标题, 看日志的人
    分不清挡住的是微信的另一个同标题窗口、还是 CodeBuddy(Chrome_WidgetWin_1)。"""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _title_is_wechat(title):
    """标题是否像微信窗口: 旧版是'微信', 新版(4.x)是登录账号昵称(如'示例联系人')"""
    return any(m and m in title for m in config.WECHAT_TITLE_MARKERS)


def _wechat_candidates():
    """枚举可见窗口打分排序, 返回 [(score, hwnd), ...] 降序; 已排除通话等辅助窗口
    微信 4.x 主窗口/通话窗口类名同为 Qt51514QWindowIcon, 无法仅靠类名区分,
    打分特征: 标题命中识别词(旧版'微信' / 新版账号昵称)+大尺寸(主窗口) vs 通话弹窗;
    辅助窗口按标题关键词直接剔除。
    注意: 4.x 主窗口标题已不含'微信'二字, 只认'微信'会把主窗口整个漏掉
    (表现为"微信未运行"-> 反复拉起 exe), 故标题判定必须走 WECHAT_TITLE_MARKERS。
    """
    for cls in config.WECHAT_MAIN_CLASSES:
        h = user32.FindWindowW(cls, None)
        if h:
            return [(1000, h)]       # 精确类名(兼容 3.x)最可信, 直接采用
    cands = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        cname = cls_buf.value
        if cname in config.WECHAT_EXCLUDE_CLASSES:
            return True
        title = _win_title(hwnd)
        if 'imhere' in title or 'replyIsTooEasy' in title or '自动回复' in title:
            return True          # 本 bot 的启动终端窗口(标题含"微信"会被误判为候选)
        if any(w in title for w in ('语音', '通话', '视频', '文件', '设置', '扫一扫')):
            return True          # 明确是辅助窗口标题
        if not _title_is_wechat(title) and cname not in config.WECHAT_KNOWN_CLASSES:
            return True          # 与微信无关的窗口不参与
        w, h = _win_size(hwnd)
        big = w >= 600 and h >= 500   # 主窗口特征: 大尺寸
        score = 0
        if cname in config.WECHAT_MAIN_CLASSES:
            score = 100
        elif cname in config.WECHAT_QT_CLASSES:
            score = 80           # 4.x 类名: 可信但与通话弹窗同名, 分数压低
        if title in config.WECHAT_TITLE_MARKERS:
            score = max(score, 120 if big else 90)   # 精确等于"微信"/账号昵称
        elif _title_is_wechat(title):
            score = max(score, 60 if big else 40)
        if _is_resizable(hwnd):
            score += 20            # 主窗口可调整大小, 通话弹窗通常固定
        cands.append((score, hwnd))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(cb), 0)
    cands.sort(key=lambda x: -x[0])
    return cands


def _wechat_hwnd():
    """兼容接口: 返回分数最高的微信候选窗口"""
    cands = _wechat_candidates()
    return cands[0][1] if cands else 0


def _attach_foreground(hwnd):
    """把自己附着到当前前台线程, 再抢一次前台 —— 绕过"前台锁定"的常规做法。

    SetForegroundWindow 的判定条件里有一条是"调用线程属于前台线程"。AttachThreadInput
    之后这条就成立, 于是 SPI_GETFOREGROUNDLOCKTIMEOUT(本机 200000ms = 200 秒)那种
    "距最后一次真人输入太久就不许抢前台"的限制也拦不住它。
    放在 _bring_to_front 的最后一道: 前面两招(ALT 技巧 / 最小化还原)都没成时才用,
    是纯增量兜底, 不改变原本能成功那条路径的行为。

    签名声明见文件顶部: 不声明 argtypes, 64 位句柄会被当成 c_int 截断, 这里会静默失败。
    """
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    my_tid = kernel32.GetCurrentThreadId()
    if not fg_tid or fg_tid == my_tid:
        return False                      # 附着自己没意义, 也避免多余的系统调用
    attached = False
    try:
        if not user32.AttachThreadInput(my_tid, fg_tid, True):
            return False
        attached = True
        user32.ShowWindow(hwnd, 9)        # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    except Exception as e:                                  # noqa: BLE001
        print(f'[send] AttachThreadInput 兜底异常: {type(e).__name__}: {e}')
        return False
    finally:
        if attached:
            user32.AttachThreadInput(my_tid, fg_tid, False)
    time.sleep(0.25)
    return user32.GetForegroundWindow() == hwnd


def _bring_to_front(hwnd):
    """还原 + ALT技巧抢前台; 失败则最小化再还原强制激活; 再失败附着前台线程抢一次。

    最后一段诊断打印是给"回复发不出去"这类故障留的现场: 原来的日志只有调用方那句
    '无微信候选窗口可置前', 看不出是候选句柄失效、被前台锁挡住, 还是被别的窗口
    (本机实测常见: CodeBuddy 的 Agents 窗口) 一直抢回去。
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        time.sleep(0.4)
    _key(0x12)
    _key(0x12, up=True)
    time.sleep(0.05)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    if user32.GetForegroundWindow() != hwnd:
        user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE
        time.sleep(0.15)
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        time.sleep(0.4)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
    if user32.GetForegroundWindow() == hwnd:
        return True
    if _attach_foreground(hwnd):
        print(f'[send] ALT/还原两招都没成, AttachThreadInput 兜底抢到了 hwnd={hwnd}')
        return True
    fg = user32.GetForegroundWindow()
    print(f'[send] 抢前台三招全失败: 目标 hwnd={hwnd} 类名={_win_class(hwnd)!r} '
          f'标题={_win_title(hwnd)!r} 最小化={bool(user32.IsIconic(hwnd))} '
          f'可见={bool(user32.IsWindowVisible(hwnd))} | 现在前台 hwnd={fg} '
          f'类名={_win_class(fg)!r} 标题={_win_title(fg)!r}')
    return False


_confirmed_hwnd = 0    # 已确认过的微信主窗口句柄(微信不重启就不会变, 可一直复用)
_ocr_miss = 0          # OCR 连续"没确认出来"的次数
_OCR_MISS_MAX = 3      # 连续这么多次仍确认不了, 就先跳过 OCR 确认(但仍会周期性重试)
_activate_n = 0        # _activate() 被调用的次数, 用于熔断后周期性重试确认
_OCR_RETRY_EVERY = 10  # 熔断后每隔这么多次 _activate() 就重新试一次 OCR 确认
# 说明: OCR 确认(截屏 + winrt OCR 识别)实测 1~3s, 而实测日志里几乎每次都是
# "OCR未确认主窗口" 白跑一遍——特征词没出现在截取区域。每发一条就白等这几秒,
# 是"回复慢"里纯浪费的一部分。故加两级优化: 句柄缓存 + 连续未命中熔断。


def _activate_once():
    """激活已登录的微信主窗口: 候选窗口逐个"激活->截屏OCR确认", 确保不误中通话等辅助窗口
    不弹运行框、不启动新进程(避免微信多开要扫码); 仅当微信完全没运行时才直接启动 exe

    加速: ① 已确认过的 hwnd 直接复用, 不再 OCR; ② OCR 连续识别不出主窗口特征时
    熔断, 退回"能置前的候选"(原本本来也是这么兜底的, 只是每次都要先白跑 OCR)。
    熔断不是永久放弃: 每 _OCR_RETRY_EVERY 次激活重试一次 OCR 确认 —— 未命中很可能
    只是那几次截屏/识别的抖动, 真窗口一直都在。

    注意: 外部一律走下面的 _activate() 包装 —— 它在这里的成功/失败之上做了
    "连续失败就清状态、再不行就重启进程"的自愈(见 _ACT_RESET_AT / SELF_HEAL)。
    """
    global _confirmed_hwnd, _ocr_miss, _activate_n, _ACCT_REJECT
    _activate_n += 1             # 计数用于"熔断后周期性重试 OCR 确认"
    _ACCT_REJECT = False
    # 0) 账号锁定(双开必须): 两个号的窗口标题(都叫账号昵称)、类名、尺寸全相同,
    #    按分数挑必然选错 —— 实测小号跑回复时被选到 196734(大号)窗口, 搜索词在那边
    #    根本不存在, 全靠 OCR 复核才挡住。锁不上就拒发, 不改选(见 _account_hwnd)。
    #    微信没开时不在这里拒绝, 交给下面的启动分支, 起来后走同一套锁号。
    own = _own_dir()
    prefer = int(getattr(config, 'WECHAT_ACCOUNT_HWND', 0) or 0)
    if (own or prefer) and (_wechat_candidates() or (prefer and user32.IsWindow(prefer))):
        h, why = _account_hwnd()
        if h and _bring_to_front(h):
            _confirmed_hwnd = h
            print(f'[send] 账号锁定({why}): hwnd={h}')
            return h
        print(f'[send] 账号锁定失败({why or "无账号证据"}), 拒绝改选其它号, 本条不发; '
              f'候选 {cacct.describe([x for _, x in _wechat_candidates()])}')
        _ACCT_REJECT = True
        return None
    # 1) 缓存命中: 句柄仍有效且能置前 -> 直接用, 跳过全部枚举与 OCR
    if _confirmed_hwnd and user32.IsWindow(_confirmed_hwnd):
        if _bring_to_front(_confirmed_hwnd):
            return _confirmed_hwnd
        _confirmed_hwnd = 0      # 窗口没了/置前失败, 缓存作废, 走完整流程

    cands = _wechat_candidates()
    if not cands:
        print('[send] 微信未运行, 直接启动...')
        try:
            subprocess.Popen([config.WECHAT_EXE])
        except Exception as e:
            print(f'[send] 启动微信失败: {e}')
            return None
        for _ in range(12):          # 最多等 6s
            time.sleep(0.5)
            cands = _wechat_candidates()
            if cands:
                break
        if not cands:
            return None
    # 2) OCR 确认(仅当 OCR 可用且还没熔断): 按分数逐个激活后截屏, 命中特征词即主窗口
    #    熔断后不是"永久放弃": 每 _activate_n 到 _OCR_RETRY_EVERY 的整数倍就重试一次,
    #    因为 OCR 未命中可能只是那几次截屏/识别的抖动, 真窗口一直都在。
    ocr_retry = (_ocr_miss >= _OCR_MISS_MAX
                 and _activate_n % _OCR_RETRY_EVERY == 0)
    if not _ocr_broken and (_ocr_miss < _OCR_MISS_MAX or ocr_retry):
        if ocr_retry:
            print(f'[send] OCR 确认熔断中(连续未确认 {_ocr_miss} 次), '
                  f'第 {_activate_n} 次激活, 周期性重试确认')
        for score, hwnd in cands[:3]:
            if not _bring_to_front(hwnd):
                continue
            if _ocr_confirm_main(hwnd):
                _ocr_miss = 0
                _confirmed_hwnd = hwnd      # 记住, 后续发送直接复用
                return hwnd
        _ocr_miss += 1
        if _ocr_miss >= _OCR_MISS_MAX:
            print(f'[send] OCR 连续 {_ocr_miss} 次未确认主窗口, 后续跳过 OCR 确认'
                  f'(每 {_OCR_RETRY_EVERY} 次激活重试一次)')
    # 3) 兜底: 退回候选, 但仍必须能置前, 否则后续 ctrl+F 会落到别的窗口
    for score, hwnd in cands[:3]:
        if _bring_to_front(hwnd):
            print(f'[send] 使用可置前候选 hwnd={hwnd} '
                  f'title={_win_title(hwnd)!r} (score={score})')
            return hwnd
    print('[send] 无微信候选窗口可置前, 放弃本次激活; 候选 '
          f'{len(cands)} 个: '
          + ' | '.join(f'hwnd={h} 类名={_win_class(h)!r} 标题={_win_title(h)!r} '
                       f'最小化={bool(user32.IsIconic(h))} score={s}'
                       for s, h in cands[:5]))
    return None


# ============================================================
# 置前失败的自愈
# ============================================================
# 实测 10-08 17:31 那次"发出去没反应": 20 秒内"把微信抢到前台"这个动作连续失败 4 次
# (发图 1 + 文字 1 + 补发 3), 回复只能进 unsent.log; 而 18:32 手动重启进程之后, 同一条
# 指令 12.6 秒就发完了 —— 说明卡住的是**进程内的窗口/前台状态**(过期句柄、attach 残留),
# 不是微信本身。既然如此, 自愈就该当场做, 而不是让每条回复都等到补发 3 次失败后
# 掉进 unsent.log。
# 两级自救, 成本从低到高:
#   第 1 级(_ACT_RESET_AT 次起): 就地清掉进程里的窗口记忆(句柄缓存 + OCR 熔断计数),
#                                下次从干净起点重新枚举、重新确认, 不碰微信、毫秒级。
#   第 2 级(_ACT_HEAL_AT 次起): 就地清状态也没用 -> 置 SELF_HEAL 请求 main 重启本进程
#                                (main 用 os.execv 原地替换自己, PID 不变, 见 restart_self)。
_ACT_RESET_AT = 2          # 连续失败到这么多次: 清窗口状态自救
_ACT_HEAL_AT = 4           # 清状态后仍连续失败到这么多次: 请求重启进程
_act_fail = 0              # 连续置前失败次数(任何一次成功都清零)
_ACCT_REJECT = False       # True = 这次失败是账号约束拒绝(不该触发窗口自愈)
SELF_HEAL = False          # True = main 应重启本进程(由 main 轮询后复位)


def _reset_window_state():
    """清掉进程里"上次成功时的窗口记忆"。

    _confirmed_hwnd 是句柄缓存、_ocr_miss 是 OCR 熔断计数 —— 一旦它们跟现实脱节
    (微信重启过 / 窗口重建过), 后面每次发送都会拿着过期记忆去撞同一堵墙:
    句柄失效 -> 置前失败, OCR 熔断 -> 少一道"这是不是主窗口"的确认。
    清它们零成本、不动微信, 所以是自愈的第一招。
    """
    global _confirmed_hwnd, _ocr_miss, _activate_n
    _confirmed_hwnd = 0
    _ocr_miss = 0
    _activate_n = 0
    print('[send] 已清空窗口句柄缓存与 OCR 熔断计数, 下次从干净状态重新识别')


def _activate():
    """激活微信主窗口(对外唯一入口): 连续置前失败会自动清状态, 再不行就请求重启进程。"""
    global _act_fail, SELF_HEAL
    hwnd = _activate_once()
    if hwnd:
        if _act_fail:
            print(f'[send] 置前已恢复正常(此前连续失败 {_act_fail} 次)')
        _act_fail = 0                 # 成功即清零"连击"
        return hwnd
    if _ACCT_REJECT:
        return None                   # 账号约束拒绝: 不是窗口状态问题, 不触发自愈
    _act_fail += 1
    if _act_fail == _ACT_RESET_AT:
        _reset_window_state()         # 第一级: 清状态, 下次重试
        return None
    if _act_fail >= _ACT_HEAL_AT and not SELF_HEAL:
        SELF_HEAL = True              # 第二级: 举手, 由 main 重启本进程
        print(f'[send] 置前连续失败 {_act_fail} 次, 就地自救无效 -> '
              f'请求重启本进程(清掉全部进程内状态; 见 main.restart_self)')
    return None


def _heal_backoff():
    """暂停自愈 —— 由 main 在"窗口内重启多次仍没恢复"时调用。

    重启都救不回来, 说明根因在进程外面(最典型: 微信被关掉/退出登录了)。那种情况下
    继续攒失败计数只会每一轮都再请求一次重启, 把服务反复打断。把计数和标志清掉,
    让发送链路退回"老实重试 + 补发"的老路, 等微信回来自然恢复。
    """
    global _act_fail, SELF_HEAL
    _act_fail = 0
    SELF_HEAL = False


def _is_wechat_fg():
    """前台窗口是不是微信(主窗口或它自己的独立聊天窗); 是则返回该前台 hwnd, 否则 0。

    必须把独立聊天窗也算进来。冷路径(没有现成聊天窗)靠双击会话列表建窗, 微信会把
    **聊天窗**顶到前台, 结束时前台就停在那个标题为备注名的 Qt 窗口上。原先只认主
    窗口, _restore_focus 判成"前台不是微信"直接放弃还原 —— 实测 QZTEST-COLD 发送
    结束后前台一直停在 1638906('Weixin' 聊天窗) 回不来, 用户正在打字的窗口被顶掉。
    """
    try:
        fg = user32.GetForegroundWindow()
        if not fg:
            return 0
        mains = [h for _score, h in _wechat_candidates()]
        if fg in mains:
            return fg
        fpid = cwin.pid_of(fg)
        if not fpid:
            return 0
        for h in mains:
            if cwin.pid_of(h) == fpid:
                return fg
    except Exception:                                       # noqa: BLE001
        pass
    return 0


def _restore_focus(prev):
    """发送结束, 把前台窗口还给"发送前那个"; 只在微信仍占着前台时才还。

    为什么必须有这一步: send_to 每次都要抢微信前台(激活 + ctrl+F 搜会话 + 粘贴),
    发完就停在微信的**聊天输入框**上。而助手在两次播报之间还要接着操作别的窗口,
    pc_tools 的 type_text / send_keys 只认"当前焦点" —— 实测 10-07 17:37
    "帮我在桌面写一个猜数字py": 播报"正在输入文字"把微信置前后, 紧接着的
    type_text 把 261 字符代码粘进了微信聊天输入框, 下一条进度播报又粘在它后面
    一起回车发了出去 —— 对方收到一条以 import random 开头的消息, 而桌面上
    根本没有那个文件。还回焦点(pc_tools 里另有一道微信护栏)才补上这整条链。

    只在"前台仍是微信"(没人接手)且"原窗口还有效"时才还, 绝不抢用户正在用的窗口。
    """
    try:
        if not prev or not user32.IsWindow(prev) or not _is_wechat_fg():
            return False
        ok = _bring_to_front(prev)
        if ok:
            # 建窗/投递引发的激活会迟到: 实测冷路径在发送进程收尾后仍有 Qt 自激活,
            # 只还一次会被它随后顶掉。盯 350ms 把迟到的也压回去(与 fg_guard 同一套)。
            cwin.watch_fg(prev, dur=0.35)
        return ok
    except Exception:                                       # noqa: BLE001
        return False


_step_t0 = None        # 上一步的时刻(算增量)


def _step_reset():
    global _step_t0
    _step_t0 = time.time()


def _mark(msg):
    """带"距上一步耗时"的日志。

    发送是端到端里最大的一笔固定开销(实测单次 8.2s, 一次播报就要一整个来回), 但日志里
    只有一串裸文字, 谁也说不清这 8s 花在哪一段 —— 于是"能不能省、该动哪一行"全靠猜。
    这里只加时间, 不动任何等待与校验逻辑。
    """
    global _step_t0
    now = time.time()
    dt = now - _step_t0 if _step_t0 else 0.0
    _step_t0 = now
    print(f'[send] {msg}  (+{dt:.2f}s)')


_NUM_PREFIX = re.compile(r'^[0-9]{4,}')


def _ascii_only(s):
    return bool(s) and all(ord(c) < 128 for c in s)


def _pinyin_initials(name):
    """中文 -> 拼音首字母(微信搜索框按首字母匹配: '服务号' 打 fwh 就出来, 见用户截图)。
    非汉字的 ASCII 字符原样保留, 生僻到取不出音的字跳过。"""
    try:
        from pypinyin import lazy_pinyin, Style
    except ImportError:
        return ''
    out = []
    for ch in name:
        if ord(ch) < 128:
            out.append(ch)
            continue
        p = lazy_pinyin(ch, style=Style.FIRST_LETTER, errors='ignore')
        out.append(p[0] if p else '')
    return ''.join(out).strip()


def _search_key(name):
    """把联系人标识换成"不会触发联网搜索"的 ASCII 搜索词。

    实测 4.2.0.12: 搜索框里输中文, 下拉第一项是"搜索网络结果", 直接 Enter 会去搜网页;
    输编号或拼音首字母时, 下拉首项就是联系人("功能"区), Enter 直接进会话。
    优先级: 备注里的编号 > 纯 ASCII 原名 > 中文名的拼音首字母 > 原名兜底。
    编号取完整数字(不是前 4 位), 否则 1024 会同时命中 示例会话/示例会话/示例会话 三个。
    微信号(filehelper/wxid_xxx)不作为搜索词: 实测搜 filehelper 出不来传输助手。
    """
    if not name:
        return config.SEARCH_WORD
    m = _NUM_PREFIX.match(name)
    if m:
        return m.group(0)
    if _ascii_only(name) and not name.startswith('@'):
        return name
    py = _pinyin_initials(name)
    if len(py) >= 2:
        return py
    return name


LAST_FAIL_HARD = False   # 上次静默失败是否"不该回退老路"(分不清账号/可能已发出)


def _quiet_ready(search_word=None):
    """静默发送是否可用。

    两条路径:
    1. 固定目标(不传 search_word) + config.QUIET_TMPL 模板存在 -> 点头像切会话再投递;
    2. 传了 search_word(给任意联系人发) -> 走独立聊天窗(core.subwin): 已有窗复用,
       没有就双击会话列表首行建一个(标题校验), 之后永久 0 占用。
       这条路 2026-11-02 实测: 建窗后挪到屏幕外投字 + 回车, 2ms 采样全程 0 偏离、
       0 占用, 单条 0.5~0.9s; 代价只有每个新联系人第一次建窗那一抢(~150ms)。
    都不满足返回 False, send_to 继续走激活 + 剪贴板 + OCR 复核, 行为不变。
    """
    if search_word:
        return True
    key = getattr(config, 'QUIET_TMPL', '')
    return bool(key) and cwin.tmpl_exists(key)


def _own_dir():
    """本账号的 WeChat 目录名(如 wxid_example_acct_0f40), 取自 config.WX_ROOT。"""
    return os.path.basename(str(getattr(config, 'WX_ROOT', '') or '').rstrip('\\/'))


def _account_hwnd():
    """定位"本账号"主窗口, 返回 (hwnd, why)。

    证据链由强到弱: 进程账号指纹 > wechat_account_hwnd 写死。
    指纹 = hwnd->pid->该进程打开着 Documents\\xwechat_files\\wxid_xxx 下的文件,
    微信重启后 hwnd/pid 全变, 这条依然成立(实现与实测见 core/acct.py), 所以
    config 里的 hwnd 现在只当兜底, 不再是唯一活路。
    hwnd=0 表示有账号配置却锁不上(why 给原因); 什么都没配时返回 (0, '')
    由调用方走枚举。双开必须锁死一个: 两个号标题/类名/尺寸/头像全一样,
    任何"按分数/模板挑一个"的兜底在实测里都锁错过号(196734 收过本该发到
    132646 的回复)。
    微信把主窗藏进托盘时先不抢焦点地显示出来 —— 静默投递要求窗口可见才有像素。
    """
    own = _own_dir()
    prefer = int(getattr(config, 'WECHAT_ACCOUNT_HWND', 0) or 0)
    if own:
        h, why = cacct.find_account_hwnd(own)    # 自己枚举, 含托盘里的不可见主窗
        if h:
            return h, why
        if not prefer:
            return 0, f'账号指纹未命中: {why}'
    if not prefer:
        return 0, ''
    if not user32.IsWindow(prefer) or 'QWindowIcon' not in (cwin._cls(prefer) or ''):
        return 0, (f'账号句柄 {prefer} 已失效或已被复用(微信重启过?), 且 '
                   f'{own or "未配 wx_root"} 的进程指纹也未命中; 启动对应账号后重试')
    if not user32.IsWindowVisible(prefer):
        user32.ShowWindow(prefer, 4)        # SW_SHOWNOACTIVATE
        time.sleep(0.4)
    return prefer, 'config 指定'


def _send_via_subwindow(contact, msg):
    """给任意联系人静默发送: 复用/新建他的独立聊天窗, 投字 + 回车。

    双开时先确定"这个联系人属于哪个号"(见 csub.pick_account): 两个号的类名和标题
    完全一样, 唯一可靠差异是各自会话列表里有谁。判不出来就硬失败, 不回退激活路 ——
    老路用的是同一套候选枚举, 换条路只会把"猜"藏得更深。
    """
    global LAST_FAIL_HARD
    prefer, why0 = _account_hwnd()
    if not prefer and why0:
        _mark(f'静默: {why0}, 放弃(不回退)')
        LAST_FAIL_HARD = True
        return False
    hwnds = [h for _, h in _wechat_candidates()[:4]]
    if prefer:
        hwnd, why = prefer, why0
    elif len(hwnds) == 1:
        hwnd, why = hwnds[0], '只有一个微信主窗口'
    elif hwnds:
        hwnd, why = csub.pick_account(
            hwnds, contact=contact,
            tmpl=getattr(config, 'WECHAT_ACCOUNT_TMPL', ''))
    else:
        hwnd, why = 0, ''
    if not hwnd:
        _mark(f'静默: 双开无法确定 {contact} 属于哪个号, 放弃(不回退): '
              f'{csub.describe_accounts(hwnds) if hwnds else "没有微信主窗口"}')
        LAST_FAIL_HARD = True
        return False

    info = csub.send_to_contact(hwnd, msg, contact=contact)
    s = info.get('send') or {}
    if info.get('ok'):
        _mark(f'静默: {info["reason"]}({why}) 建窗抢 {info["steal_ms"]}ms '
              f'投递 {s.get("elapsed_ms")}ms 偏离 {s.get("deviation")} 次')
        return True
    if info.get('unverified'):
        _mark(f'静默: 已投递但无法验证({info["reason"]}), 不重发以免重复')
        LAST_FAIL_HARD = True
        return False
    _mark(f'静默: 独立聊天窗失败({info.get("reason")})')
    return False


def _send_to_quiet(text, prefix=None, search_word=None):
    """静默发送: 有 search_word 走独立聊天窗, 否则点头像切固定会话。

    与 _send_to_inner 的区别: 全程不激活微信、不碰剪贴板、不按真实键。
    实测(2026-11-01) 大号(主实例)每投一个字会自己激活一次, 靠 fg_guard 2ms
    轮询夺回, 单条消息累计占用 ~0.06s; 小号(第二实例)投递完全不动前台。
    """
    pfx = config.AI_PREFIX if prefix is None else prefix
    msg = f'{pfx}{text}'
    if search_word:
        return _send_via_subwindow(search_word, msg)
    key = config.QUIET_TMPL

    with INPUT_LOCK:
        hwnds = [h for _, h in _wechat_candidates()[:4]]
        # 先用账号句柄锁定"本账号窗口", 再在这个窗口里点头像切会话。
        # 不能只靠模板找窗口: 两个号昵称/头像设成同一张图时, 模板在两个窗口都
        # 0.99+ (实测 196734 score=1.0000 / 132646 score=0.9991), find_hwnd_by_tmpl
        # 会判"无法区分"返回 0; 而某一刻只剩一个窗口可见时又会"单命中"锁到别的号
        # (实测小号窗口藏进托盘后, 回复被投进了 196734)。所以只认句柄。
        prefer, why0 = _account_hwnd()
        if prefer:
            hwnd, score = prefer, 1.0
            _mark(f'静默: 账号句柄锁定窗口 {hwnd}')
        elif why0:
            _mark(f'静默: {why0}, 放弃(不回退)')
            LAST_FAIL_HARD = True
            return False
        else:
            hwnd, score = cwin.find_hwnd_by_tmpl(key, hwnds)
            if not hwnd:
                _mark(f'静默: 没有窗口的列表里有模板 {key}, 放弃')
                return False
            _mark(f'静默: 锁定窗口 {hwnd} (score={score:.4f})')
        home = user32.GetForegroundWindow()
        # 主窗内嵌的聊天区不吃 WM_IME_CHAR: 实测 goto_chat_by_list 成功、send_text
        # 也报 ok=True, 但界面和数据库毫无变化(纯假成功)。只有独立聊天窗才吃这个
        # 投递, 所以固定目标也走"按模板开独立窗 -> 往子窗投"这条路。
        # title 要用"窗口标题"去找, 而微信独立聊天窗的标题是**备注名**(这里就是
        # 搜索词那个单引号), 不是昵称 —— 用 TARGET_NAME 找不到已存在的窗, 于是
        # 反复双击建窗, 微信认为窗口已在就不新建, 3 秒后报"未出现新的聊天窗"。
        sub = csub.ensure_sub(hwnd, key,
                              title=config.SEARCH_WORD or config.TARGET_NAME,
                              home=home)
        if not sub.get('ok'):
            _mark(f'静默: 拿独立聊天窗失败({sub.get("reason")})')
            return False
        r = csub.send_silent(sub['hwnd'], msg)
        _mark(f'静默: 投递完成 steal={r.get("steal_ms", 0)}ms '
              f'unverified={r.get("unverified")}')
        return bool(r.get('ok'))


def send_to(text, search_word=None, marker=None, prefix=None):
    """统一入口: 只走静默链路。发不出去就返回 False, 由调用方下一轮重试。

    原本这里在静默链路失败后会回退 _send_to_inner(激活 + ctrl+F + 剪贴板 +
    真实按键)。那条老路要把微信拉到前台 3~5s, 会打断用户正在做的事 —— 实测
    "发消息的时候屏幕乱跳、输入焦点被抢走"全是它造成的。已删除:
    宁可这一条晚一点发(下一轮重试), 也不抢一次前台。

    search_word / marker / prefix: 给 wx_agent(会话2) 发任意联系人用。
    """
    global LAST_FAIL_HARD
    t0 = time.time()
    _step_reset()
    LAST_FAIL_HARD = False
    prev = user32.GetForegroundWindow()
    try:
        if not _quiet_ready(search_word):
            _mark('静默链路不可用, 本条不发(不抢前台), 等下一轮重试')
            return False
        if _send_to_quiet(text, prefix, search_word):
            return True
        if LAST_FAIL_HARD:
            _mark('静默链路硬失败(分不清账号或可能已发出), 不重发')
        else:
            _mark('静默链路没成功, 本条不发(不抢前台), 等下一轮重试')
        return False
    finally:
        if _restore_focus(prev):
            _mark('已把前台窗口还给发送前的那个')
        print(f'[send] 本次发送累计 {time.time() - t0:.2f}s')


def _send_to_inner(text, search_word=None, marker=None, prefix=None):
    """统一入口: 加前缀 -> 激活并 OCR 置信目标窗口 -> 搜索固定词进会话 -> 复核 -> 发送。

    与参考实现的差别: 搜索词不再是"联系人备注前8位", 而是 config.SEARCH_WORD。
    本实例只服务这一个会话, 固定词必然唯一命中, 也不会把别人的名字写进搜索框。
    搜索词用 ' 而不是备注"示例联系人": 微信搜索框按昵称/备注/wxid 混匹配,
    中文备注不一定命中(用户 10-14 实测)。进错会话有 OCR 复核兜底(见下)。

    search_word/marker/prefix 非 None 时覆盖 config —— wx_agent 用它发任意联系人。
    """
    global _confirmed_hwnd
    sw = _search_key(search_word or config.SEARCH_WORD)
    mk = marker or config.OCR_MARKER
    pfx = config.AI_PREFIX if prefix is None else prefix
    text = f'{pfx}{text}'
    _mark(f'准备发送: {text[:60]} (搜索词 {sw})')
    hwnd = _activate()
    if not hwnd:
        _mark('目标微信窗口激活失败 (放弃)')
        return False
    _mark('微信已激活, 写入剪贴板...')
    time.sleep(0.5)
    try:
        _set_clipboard(text)
    except OSError as e:
        _mark(f'剪贴板写入失败: {e}, 放弃本轮发送(回队重试)')
        return False
    time.sleep(0.3)
    _close_ime()

    # 关键: ctrl+F 前强制确认微信仍在最前, 否则按键会落到其他窗口(弹搜索框/误操作)
    if user32.GetForegroundWindow() != hwnd:
        _mark('微信不在前台, 重新置前...')
        if not _bring_to_front(hwnd):
            _mark('微信置前失败, 放弃本轮发送, 消息回队重试')
            return False
    # ctrl+F 打开搜索(输入框内也可用, 用户已验证)
    _mark('搜索会话...')
    _combo(VK_CTRL, VK_F)
    time.sleep(0.8)
    _combo(VK_CTRL, VK_A)          # 清空搜索框
    _key(VK_BACK)
    time.sleep(0.3)
    try:
        _set_clipboard(sw)   # 搜索词(默认 config.SEARCH_WORD, wx_agent 传联系人)
    except OSError as e:
        _mark(f'搜索词写入剪贴板失败: {e}, 放弃本轮发送(回队重试)')
        return False
    _combo(VK_CTRL, VK_V)
    time.sleep(0.8)
    _press_enter(2, gap=0.25)      # 进入目标会话(3 次改 2 次: 已先关 IME, 实测够用)
    time.sleep(1.0)
    # 发送前复核: 进了会话后左侧列表顶部就是目标联系人, OCR 必须还能认出它。
    # 两个微信账号同时登录时, 这一步是"发错账号"的最后一道闸, 代价 1~3s。
    # 单账号常驻/想换速度可以关(config.SEND_VERIFY=False), 双开千万别关。
    if config.SEND_VERIFY and not _ocr_broken:
        verdict = verify_target_chat(hwnd, mk)
        if verdict == VERIFY_MISMATCH:
            # 缓存窗口已不可信(它认出的不是目标会话): 作废它, 下次 _activate() 重新
            # 枚举+确认。否则一旦缓存到一个"再也认不出特征词"的窗口, 就会永远失败。
            _confirmed_hwnd = 0
            _mark('复核确认不在目标会话, 放弃本轮发送(回复进待重发队列)')
            return False
        if verdict == VERIFY_UNKNOWN:
            if config.VERIFY_UNKNOWN_AS_PASS:
                _mark('复核测不出来(截屏/OCR 抖动), 不是"发错人"的证据, '
                      '按配置放行')
            else:
                _mark('复核测不出来且配置为拦截, 放弃本轮发送'
                      '(回复进待重发队列)')
                return False
    # 关键: 搜索时剪贴板已被搜索词覆盖, 发送前必须重新写入回复文本
    try:
        _set_clipboard(text)
    except OSError as e:
        _mark(f'发送前剪贴板写入失败: {e}, 放弃本轮发送(回队重试)')
        return False
    _combo(VK_CTRL, VK_V)          # 粘贴回复
    time.sleep(0.5)
    _press_enter(3, gap=0.2)       # 发送(保留 3 次: 吞了回车就是"没发出去", 不能省)
    time.sleep(config.SEND_DELAY)
    _mark('发送流程完成')
    return True


def send_image_to(path):
    """把一张图发到目标会话(图片版 send_to): 同样先记下前台, 无论哪条分支都还回去。"""
    with INPUT_LOCK:
        t0 = time.time()
        _step_reset()
        prev = user32.GetForegroundWindow()
        try:
            return _send_image_inner(path)
        finally:
            if _restore_focus(prev):
                _mark('已把前台窗口还给发送前的那个')
            print(f'[send] 本次发图累计 {time.time() - t0:.2f}s')


def _send_image_inner(path):
    """把一张图发进目标会话。链路与 _send_to_inner 逐段一致(激活 -> ctrl+F 搜索固定词
    -> Enter 进会话 -> OCR 复核 -> 粘贴 -> Enter), 唯一差别是往剪贴板里放的是位图 CF_DIB。

    复核一步都不省, 而且比发文本更要紧: 文本发错人还能撤回重发, 截图发错人就是把
    这边的屏幕内容白送出去了。图片没法带 config.AI_PREFIX(前缀是文本才有的概念)。
    """
    global _confirmed_hwnd
    if not path or not os.path.exists(path):
        _mark(f'图片不存在: {path!r}')
        return False
    _mark(f'准备发图: {os.path.basename(path)} (搜索词 {config.SEARCH_WORD})')
    hwnd = _activate()
    if not hwnd:
        _mark('目标微信窗口激活失败 (放弃)')
        return False
    time.sleep(0.5)
    _close_ime()
    if user32.GetForegroundWindow() != hwnd:
        _mark('微信不在前台, 重新置前...')
        if not _bring_to_front(hwnd):
            _mark('微信置前失败, 放弃本轮发图')
            return False
    _mark('搜索会话(发图)...')
    _combo(VK_CTRL, VK_F)
    time.sleep(0.8)
    _combo(VK_CTRL, VK_A)
    _key(VK_BACK)
    time.sleep(0.3)
    try:
        _set_clipboard(config.SEARCH_WORD)   # 搜索用固定词, 后面会被图片覆盖
    except OSError as e:
        _mark(f'搜索词写入剪贴板失败: {e}, 放弃本轮发图')
        return False
    _combo(VK_CTRL, VK_V)
    time.sleep(0.8)
    _press_enter(2, gap=0.25)
    time.sleep(1.0)
    if config.SEND_VERIFY and not _ocr_broken:
        verdict = verify_target_chat(hwnd)
        if verdict == VERIFY_MISMATCH:
            _confirmed_hwnd = 0
            _mark('复核确认不在目标会话, 放弃本轮发图')
            return False
        if verdict == VERIFY_UNKNOWN:
            if config.VERIFY_UNKNOWN_AS_PASS:
                _mark('复核测不出来(截屏/OCR 抖动), 不是"发错人"的证据, 按配置放行')
            else:
                _mark('复核测不出来且配置为拦截, 放弃本轮发图')
                return False
    try:
        _set_clipboard_image(path)
    except OSError as e:
        _mark(f'图片写入剪贴板失败: {e}, 放弃本轮发图')
        return False
    time.sleep(0.4)
    _combo(VK_CTRL, VK_V)
    time.sleep(1.2)                # 位图粘贴比文本慢, 多等一会儿再回车, 否则可能发个空
    _press_enter(3, gap=0.2)
    time.sleep(config.SEND_DELAY)
    _mark('发图流程完成')
    return True


# 复核结论三态: 旧版只返回 bool, "截屏失败/OCR 空"和"确认在别的会话"混成同一个
# False, 既分不清原因(日志对不上账), 又都把回复直接丢掉了。
VERIFY_OK = 'ok'              # 确认停在目标会话
VERIFY_MISMATCH = 'mismatch'  # 认得出字, 但不是目标会话 -> 确认发错, 必须拦
VERIFY_UNKNOWN = 'unknown'    # 截屏/OCR 拿不到内容 -> 测不出来, 不等于发错


def _marker_ratio(marker, norm_text):
    """特征词在 OCR 文本里的字符覆盖率(容忍 OCR 缺笔画)"""
    chars = [c for c in marker if not c.isspace()]
    if not chars:
        return 1.0
    return sum(1 for c in chars if c in norm_text) / len(chars)


def verify_target_chat(hwnd, marker=None):
    """三态复核当前是否停在目标会话; 每个出口都留日志, 不再有静默分支。

    marker: 期望在会话标题处认出的特征词; None 用 config.OCR_MARKER。
    wx_agent 发任意联系人时传该联系人的名字, 复核的就是"有没有进错人"。
    """
    mk = marker or config.OCR_MARKER
    img = _capture_window(hwnd)
    if img is None:
        print('[send] 复核测不出来: 截屏拿不到内容(窗口 DC 与屏幕 DC 都是空图)')
        return VERIFY_UNKNOWN
    w, h = img.size
    text = _ocr_window_text(img.crop(_list_box(img)))
    norm = text.replace(' ', '').replace('\u3000', '').replace('\n', '')
    if not norm:
        print(f'[send] 复核测不出来: 复核区 OCR 文本为空(窗口 {w}x{h}, 疑似空图)')
        return VERIFY_UNKNOWN
    ratio = _marker_ratio(mk, norm)
    if ratio >= config.VERIFY_MATCH_RATIO:
        print(f'[send] 已确认目标会话(特征 {mk!r} 覆盖率 {ratio:.0%})')
        return VERIFY_OK
    print(f'[send] 未命中目标会话(特征 {mk!r} 覆盖率 {ratio:.0%}'
          f' < {config.VERIFY_MATCH_RATIO:.0%}); 复核区 OCR={norm[:60]!r}')
    return VERIFY_MISMATCH


def check_target_chat(hwnd):
    """(保留旧签名) 是否已确认停在目标会话; 仅供 main/自检调用。"""
    return verify_target_chat(hwnd) == VERIFY_OK
