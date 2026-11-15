# -*- coding: utf-8 -*-
"""UI 自动化发送
激活: 直接 API 激活已登录的微信主窗口(FindWindow + ALT技巧抢前台, 不弹运行框不开新进程)
      仅当微信完全没运行时才直接启动 exe(不用运行窗口)
      窗口识别: 旧版标题"微信"; 新版(4.x)标题改成了登录账号昵称(见 config.WECHAT_TITLE_MARKERS),
                类名 Qt51514QWindowIcon 与通话弹窗同名, 只能辅助打分不能单独判定
发送: ctrl+F 搜索前8位备注 -> Enter 进输入框 -> 粘贴 -> Enter 发送
前缀: 所有发送文本自动加 "[AI生成] "
经验: SendInput 在64位下报错87不可用; keybd_event 可用(已实测弹出运行框)
     中文输入法会吞 Enter, 需关IME + 多按 Enter 兜底
     SetForegroundWindow 受前台锁定限制, 需先模拟 ALT 键获取"用户输入"权限
"""
import ctypes, os, sys, time, subprocess

import config

# core/ 是大小号共用的底层(静默投递/窗口识别), chains/chat 与它只差两级目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core import win as cwin
from core import subwin as csub
from core import acct as cacct

user32 = ctypes.windll.user32
imm32 = ctypes.windll.imm32

_rect_t = ctypes.wintypes.RECT if hasattr(ctypes, 'wintypes') else None

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
    kernel32.GlobalLock.restype = ctypes.c_void_p
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
        return Image.frombuffer('RGB', (w, h), buf, 'raw', 'BGR', 0, 1).copy()
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem:
            gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


_ocr_broken = False   # OCR 环境不可用后熔断, 后续直接跳过确认避免重复耗时


def _ocr_window_text(img):
    """winrt OCR(Windows 自带引擎)识别图像中的文本; 失败返回 ''"""
    global _ocr_broken
    if _ocr_broken:
        return ''
    try:
        import asyncio
        from PIL import Image, ImageOps
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat

        async def run():
            gray = img.convert('L')
            if gray.width > 1400:                  # 防超大图, 先压到合理宽度
                k = 1400 / gray.width
                gray = gray.resize((1400, int(gray.height * k)), Image.LANCZOS)
            gray = gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS)
            gray = ImageOps.autocontrast(gray, cutoff=1)
            bgra = gray.convert('RGBA').tobytes('raw', 'BGRA')
            sb = SoftwareBitmap.create_copy_from_buffer(
                bgra, BitmapPixelFormat.BGRA8, gray.width, gray.height)
            engine = OcrEngine.try_create_from_language(Language('zh-CN'))
            if engine is None:
                engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                return ''
            result = await engine.recognize_async(sb)
            return '\n'.join(l.text for l in result.lines)

        return asyncio.run(run())
    except Exception as e:
        print(f'[send] OCR 不可用, 后续跳过OCR确认: {e}')
        _ocr_broken = True
        return ''


def _ocr_confirm_main(hwnd):
    """窗口已在前台: 截屏, 在左侧聊天列表区域 OCR, 命中"文件传输助手"等主窗口特征词
    PrintWindow 对后台 Qt 窗口只渲染缓存, 必须在前台截屏才可靠"""
    img = _capture_window(hwnd)
    if img is None:
        return False
    w, h = img.size
    box = (int(w * 0.06), int(h * 0.05), int(w * 0.34), int(h * 0.60))
    text = _ocr_window_text(img.crop(box))
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
        if 'replyIsTooEasy' in title or '自动回复' in title or 'WeChat AI Bot' in title:
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


def _bring_to_front(hwnd):
    """还原 + ALT技巧抢前台; 失败则最小化再还原强制激活"""
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
    return user32.GetForegroundWindow() == hwnd


_confirmed_hwnd = 0    # 已确认过的微信主窗口句柄(微信不重启就不会变, 可一直复用)
_confirmed_trusted = False   # 上面那个句柄当初是靠证据(模板)确认的, 还是靠猜的
last_fail_hard = False       # 上次静默发送失败是否"不该回退老路"(分不清账号/可能已发出)
last_fail_kind = 'none'      # 'none' / 'unsent'(确定没发) / 'maybe_sent'(可能已发)
_ocr_miss = 0          # OCR 连续"没确认出来"的次数
_OCR_MISS_MAX = 3      # 连续这么多次仍确认不了, 就停用 OCR 确认这一步
# 说明: OCR 确认(截屏 + winrt OCR 识别)实测 1~3s, 而实测日志里几乎每次都是
# "OCR未确认主窗口" 白跑一遍——特征词没出现在截取区域。每发一条就白等这几秒,
# 是"回复慢"里纯浪费的一部分。故加两级优化: 句柄缓存 + 连续未命中熔断。


def _activate():
    """激活已登录的微信主窗口: 候选窗口逐个"激活->截屏OCR确认", 确保不误中通话等辅助窗口
    不弹运行框、不启动新进程(避免微信多开要扫码); 仅当微信完全没运行时才直接启动 exe

    加速: ① 已确认过的 hwnd 直接复用, 不再 OCR; ② OCR 连续识别不出主窗口特征时
    熔断, 退回"能置前的候选"(原本本来也是这么兜底的, 只是每次都要先白跑 OCR)。
    """
    global _confirmed_hwnd, _ocr_miss
    # 0) 账号锁定(双开必须): 优先按"进程打开的账号目录"认自己这个号(core/acct.py) ——
    #    hwnd 写死会随微信重启失效, 头像模板在两号设成同一张图时毫无区分力
    #    (实测两个窗口同时命中 1.0000/0.9991), 进程指纹才是与渲染无关的硬证据。
    own = os.path.basename(str(getattr(config, 'WX_ROOT', '') or '').rstrip('\\/'))
    if own:
        h, why = cacct.find_account_hwnd(own)
        if h and _bring_to_front(h):
            print(f'[send] 账号锁定({why}): hwnd={h}')
            _confirmed_hwnd = h
            return h
    # 0b) 账号约束(双开必须): 两个号的窗口标题(都叫账号昵称)、类名、尺寸全部相同,
    # 光按分数/OCR 特征词挑必然选错号 —— 实测大号最小化时, 老路把消息发进了小号
    # 窗口(搜不到目标会话, 消息直接丢掉)。配了约束就只认约束: 锁不上直接拒发,
    # 绝不回退"按分数猜一个", 猜错 = 把消息发给另一个号, 是本项目最不能犯的错。
    prefer = int(getattr(config, 'WECHAT_ACCOUNT_HWND', 0) or 0)
    tmpl = getattr(config, 'WECHAT_ACCOUNT_TMPL', '')
    if prefer or tmpl:
        if prefer and user32.IsWindow(prefer) and _bring_to_front(prefer):
            _confirmed_hwnd = prefer
            return prefer
        if tmpl:
            try:
                h, why = csub.pick_account(
                    [x for _, x in _wechat_candidates()], tmpl=tmpl)
                if h and _bring_to_front(h):
                    print(f'[send] 账号指纹锁号: {why}')
                    _confirmed_hwnd = h
                    return h
            except Exception as e:                              # noqa: BLE001
                print(f'[send] 账号指纹锁号异常({type(e).__name__}): {e}')
        print(f'[send] 账号约束未命中(句柄 {prefer or "-"} / 指纹 {tmpl or "-"}), '
              f'拒绝按分数猜号, 本条不发')
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
    if not _ocr_broken and _ocr_miss < _OCR_MISS_MAX:
        for score, hwnd in cands[:3]:
            if not _bring_to_front(hwnd):
                continue
            if _ocr_confirm_main(hwnd):
                _ocr_miss = 0
                _confirmed_hwnd = hwnd      # 记住, 后续发送直接复用
                return hwnd
        _ocr_miss += 1
        if _ocr_miss >= _OCR_MISS_MAX:
            print(f'[send] OCR 连续 {_ocr_miss} 次未确认主窗口, 后续跳过 OCR 确认')
    # 3) 兜底: 退回候选, 但仍必须能置前, 否则后续 ctrl+F 会落到别的窗口
    for score, hwnd in cands[:3]:
        if _bring_to_front(hwnd):
            print(f'[send] 使用可置前候选 hwnd={hwnd} '
                  f'title={_win_title(hwnd)!r} (score={score})')
            return hwnd
    print('[send] 无微信候选窗口可置前, 放弃本次激活')
    return None


# 说明: 原来的 _quiet_hwnd 已并入 _quiet_main —— 它会在没有模板时调 _activate
# (抢一次前台做 OCR 确认), 而静默链路的前提就是"一次都不抢"。现在挑窗口只在
# 已确认 > 头像模板 > 候选首个 三档里选, 绝不激活。

def _quiet_main(remark=None):
    """不激活地选出"该用哪个微信账号"的主窗口, 证据不足就返回 None。

    双开实测(2026-11-02): 两个主窗的类名、标题(都叫"微信")、可调大小全部相同,
    连分数都会因为"谁恰好被最小化"而翻转(可见的 140 / 最小化的 110)。所以按分数
    取第一个等于每次都在猜, 猜错就把消息发给另一个号里的同名联系人 —— 这是本项目
    最不能犯的错。判据改成证据链:

        config.WECHAT_ACCOUNT_HWND 写死
        > 该联系人只在一个号里有独立聊天窗
        > 头像模板命中
        > 只有一个号
        > 之前靠证据确认过的缓存

    证据不足时打印候选清单(含每个号已有的聊天窗, 便于人工指定)并返回 None。
    注意: 回退老路(激活 + ctrl+F)同样分不清账号, 所以调用方见 last_fail_hard
    应当直接放弃这一条, 而不是换个方式再猜一次。
    """
    global _confirmed_hwnd, _confirmed_trusted
    # 进程账号指纹(最强证据, 见 core/acct.py): 与标题/尺寸/头像无关, 微信重启也不失效
    own = os.path.basename(str(getattr(config, 'WX_ROOT', '') or '').rstrip('\\/'))
    if own:
        h, why = cacct.find_account_hwnd(own)
        if h:
            if h != _confirmed_hwnd:
                print(f'[send] 账号定位({why}): hwnd={h}')
            _confirmed_hwnd, _confirmed_trusted = h, True
            return h
    prefer = int(getattr(config, 'WECHAT_ACCOUNT_HWND', 0) or 0)
    if prefer and user32.IsWindow(prefer):
        return prefer

    hwnds = [h for _, h in _wechat_candidates()[:4]]
    if not hwnds:
        return None
    if len(hwnds) == 1:
        return hwnds[0]          # 单号: 不存在"发错号"这回事

    h, why = csub.pick_account(
        hwnds, contact=remark,
        tmpl=getattr(config, 'WECHAT_ACCOUNT_TMPL', ''))
    if h:
        print(f'[send] 账号定位({why}): hwnd={h}')
        return h

    digits = ''.join(ch for ch in (remark or '') if ch.isdigit())[:8]
    if digits and cwin.tmpl_exists(digits):
        h, score = cwin.find_hwnd_by_tmpl(digits, hwnds)
        if h:
            print(f'[send] 账号定位(头像模板 score={score:.4f}): hwnd={h}')
            _confirmed_hwnd, _confirmed_trusted = h, True
            return h

    if _confirmed_hwnd and _confirmed_trusted and _confirmed_hwnd in hwnds:
        return _confirmed_hwnd

    print(f'[send] 双开且证据不足, 无法确定 {remark!r} 属于哪个号, 放弃静默发送: '
          f'{csub.describe_accounts(hwnds)}。进程账号指纹也未命中'
          f'({cacct.describe(hwnds)}), 确认该号微信已登录后重试'
          f'(兜底: config.json 的 "wechat_account_hwnd")')
    return None


def _maybe_sent(reason):
    """这个失败原因是否意味着"其实已经发出去了"。

    实测(2026-11-11): 独立聊天窗投递后回车, 输入框不会立刻清空, 旧代码据此判失败;
    但微信 DB 里那一条 status=2(已发送), 48 秒里同一条话术落了 8 条。
    所以"输入框未清空"只能当"不确定", 不能当"没发出"。
    """
    r = str(reason or '')
    return ('输入框未清空' in r) or ('未清空' in r) or ('可能没发出去' in r)


def _ocr_lines_with_boxes(img):
    """OCR 一次, 返回 [(line_text, (x1,y1,x2,y2))]; 坐标是 img 的像素坐标。

    与 _ocr_window_text 的区别: 那个只要文字, 这里要每个词框的位置 ——
    靠 y 反推"这行是会话列表的第几行", 用来直接双击对方所在的那一行。
    """
    global _ocr_broken
    if _ocr_broken:
        return []
    try:
        import asyncio
        from PIL import Image, ImageOps
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat

        async def run():
            gray = img.convert('L')
            scale = 2
            if gray.width > 700:
                scale = 1
            gray = gray.resize((gray.width * scale, gray.height * scale), Image.LANCZOS)
            gray = ImageOps.autocontrast(gray, cutoff=1)
            bgra = gray.convert('RGBA').tobytes('raw', 'BGRA')
            sb = SoftwareBitmap.create_copy_from_buffer(
                bgra, BitmapPixelFormat.BGRA8, gray.width, gray.height)
            engine = OcrEngine.try_create_from_language(Language('zh-CN'))
            if engine is None:
                engine = OcrEngine.try_create_from_user_profile_languages()
            if engine is None:
                return []
            result = await engine.recognize_async(sb)
            out = []
            for line in result.lines:
                rs = [w.bounding_rect for w in line.words]
                if not rs:
                    continue
                x1 = min(r.x for r in rs) / scale
                y1 = min(r.y for r in rs) / scale
                x2 = max(r.x + r.width for r in rs) / scale
                y2 = max(r.y + r.height for r in rs) / scale
                out.append((line.text, (x1, y1, x2, y2)))
            return out

        return asyncio.run(run())
    except Exception as e:                                      # noqa: BLE001
        print(f'[send] OCR 行定位不可用: {e}')
        _ocr_broken = True
        return []


def _norm(s):
    """只保留字母/数字/汉字 —— OCR 会把 "一只小…" 拆成 "一 只 小 “", 还会带引号。"""
    out = []
    for ch in str(s or ''):
        if ch.isalnum() or '\u4e00' <= ch <= '\u9fff':
            out.append(ch.lower())
    return ''.join(out)


def _scan_target_rows(hwnd, contact, expect_text=None, n_rows=11):
    """给会话列表每一行打分, 判断"目标是不是已经出现在列表里"。

    三个维度(用户定的):
      1) OCR 名字命中        权重 3
      2) 该行有未读红点      权重 2
      3) 行内预览文字与对方消息内容对得上(前 6 个字)  权重 2
    返回 [(row, score, detail)], 按分数从高到低; 全 0 返回空列表。
    """
    from core import subwin as csub
    from core import win as cwin
    from PIL import Image

    im = cwin.grab(hwnd)
    if im is None or getattr(im, 'size', 0) == 0:
        return []
    y0 = max(0, csub.row_y(0) - 30)
    y1 = min(im.shape[0], csub.row_y(n_rows - 1) + 30)
    crop = im[y0:y1, csub.DOT_X0:csub.DOT_X1]
    if crop.size == 0:
        return []
    lines = _ocr_lines_with_boxes(Image.fromarray(crop))
    dots = set(csub.unread_rows(hwnd))

    want = _norm(contact)
    want_msg = _norm(expect_text)
    out = []
    for i in range(n_rows):
        cy = csub.row_y(i) - y0
        name_hit = msg_hit = False
        for text, (_x1, by1, _x2, by2) in lines:
            if abs((by1 + by2) / 2 - cy) > 30:
                continue
            t = _norm(text)
            if len(t) >= 3 and want and (
                    want in t or t in want or (len(t) >= 4 and want[:4] == t[:4])):
                name_hit = True
            if len(t) >= 4 and len(want_msg) >= 4 and (
                    want_msg[:6] in t or t[:6] in want_msg):
                msg_hit = True
        score = 3 * int(name_hit) + 2 * int(i in dots) + 2 * int(msg_hit)
        if score:
            out.append((i, score, {
                'name': name_hit, 'dot': i in dots, 'msg': msg_hit}))
    out.sort(key=lambda x: (-x[1], x[0]))
    return out


def find_contact_row(hwnd, contact, n_rows=11):
    """OCR 会话列表, 找到"这一行是目标联系人"的行号; 找不到返回 None。

    一次 OCR 拿整列文字 + 每行框的 y, 再按 y 反推行号 —— 比逐行 OCR 快一个数量级。
    匹配规则: 双向包含(OCR 会把长备注截成"一只小…"), 忽略空白与大小写。
    """
    from core import subwin as csub
    from core import win as cwin
    from PIL import Image

    want = _norm(contact)
    if not want:
        return None
    im = cwin.grab(hwnd)
    if im is None or getattr(im, 'size', 0) == 0:
        return None
    y0 = max(0, csub.row_y(0) - 30)
    y1 = min(im.shape[0], csub.row_y(n_rows - 1) + 30)
    crop = im[y0:y1, csub.DOT_X0:csub.DOT_X1]
    if crop.size == 0:
        return None
    for text, (_bx1, by1, _bx2, by2) in _ocr_lines_with_boxes(Image.fromarray(crop)):
        t = _norm(text)
        if len(t) < 3:
            continue                      # "0" / "1" 这类角标数字不能拿去匹配
        # 会话列表里的名字常被省略号截断, 所以按前缀比: 取双方前 4 字对齐。
        ok = (want in t) or (t in want) or (len(t) >= 4 and want[:4] == t[:4])
        if ok:
            cy = (by1 + by2) / 2 + y0
            idx = int(round((cy - csub.FIRST_ROW_Y) / csub.ROW_H))
            if 0 <= idx < n_rows:
                print(f'[send] OCR 命中 {text!r} -> 第 {idx} 行')
                return idx
    return None


def send_to_quiet(remark, text, home=None, prefix=None, expect_text=None):
    """静默发送: 独立聊天窗 + PostMessage 投字投回车, 全程 0 占用。

    2026-11-02 实测(小号, 684x911 主窗口): 建窗后挪到 (-12000,-12000), 投字 +
    回车, 2ms 采样全程 0 次前台偏离、0ms 占用; 36 字长文本不吞字, 单条 0.5~0.9s。
    与 send_to 的差别: send_to 激活 + ctrl+F + 剪贴板 + 真实回车, 单次占前台 3~5s。

    路径: 复用已有子窗 -> 没有就双击列表首行建窗(标题校验) -> 投递 -> 校验清空。
    建窗那一下微信是显式 SetForegroundWindow, 拦不住, 是一次性代价(实测 ~150ms);
    同一个联系人之后每条都是 0。都不通时保留"模板 + 点头像"老路, 再不行返回
    False 让调用方回退 send_to。
    """
    global last_fail_hard, last_fail_kind
    last_fail_hard = False
    # 失败分两类, 调用方必须区别对待:
    #   'unsent'     明确没发出去(没窗口/没账号/切会话失败) -> 可以重试
    #   'maybe_sent' 已投递但拿不到证据(输入框未清空等)     -> 绝不能重试
    # 旧版把这两类都塞进 last_fail_hard, 调用方没区分, 于是"其实发出去了"的
    # 每一条都被当失败重试, 实测同一条固定话术在 50 秒里发了 8 遍。
    last_fail_kind = 'none'

    hwnd = _quiet_main(remark)
    if not hwnd:
        # 分不清账号时不能回退: 老路(激活 + ctrl+F)用的是同一套候选枚举, 换条路
        # 只会把"猜"藏得更深, 而且会抢前台。直接放弃这一条, 让它停在日志里。
        last_fail_hard = True
        last_fail_kind = 'unsent'
        print('[send] 静默链路: 没有可用的微信主窗口(或账号无法确定)')
        return False
    # prefix=None 走默认的 [AI生成]; 首次话术这类固定文案传 '' 或自带前缀的字符串,
    # 否则会变成 '[AI生成] [自动回复] ...' 两个前缀叠在一起。
    text = f'{config.AI_PREFIX if prefix is None else prefix}{text}'
    home = home or cwin.pick_home_window(exclude=hwnd)

    # 选行(用户 2026-11-15 定的流程):
    #   ① 已有目标窗口 -> 直接复用(open_by_row 第一跳就会按标题认出来);
    #   ② 列表里有「非导航区域的带数字红点」-> 只点这些行, 一行行验标题;
    #   ③ 没有红点 -> 双击左栏「聊天」导航图标跳转, 再看一次红点; 重复 n 次仍无红点,
    #      才退回 OCR 名字打分(会话已读但窗口没开的情况)。
    # 旧版按"分数最高的一行"盲点, 实测会点开 示例会话唯一/服务通知 这种无关窗口。
    scan = 5
    NAV_TRIES = 3

    def _dot_rows():
        try:
            return csub.unread_rows(hwnd)
        except Exception:                                      # noqa: BLE001
            return []

    rows = None
    dots = _dot_rows()
    if dots:
        rows = dots[:scan]
        print(f'[send] 红点行 {rows}(非导航区), 按红点定位')
    else:
        for i in range(NAV_TRIES):
            try:
                csub.nudge_chat_tab(hwnd, restore=True)
                time.sleep(0.25)
            except Exception:                                  # noqa: BLE001
                pass
            dots = _dot_rows()
            if dots:
                rows = dots[:scan]
                print(f'[send] 第{i + 1}次双击导航后出现红点行 {rows}')
                break
        if rows is None:
            cand = []
            try:
                cand = _scan_target_rows(hwnd, remark, expect_text)
            except Exception:                                  # noqa: BLE001
                cand = []
            if cand and cand[0][1] >= 3:
                rows = [cand[0][0]]
                print(f'[send] 无红点(导航跳转 {NAV_TRIES} 次), 退回名字定位 {rows}')
    info = csub.send_to_contact(hwnd, text, contact=remark, home=home, scan=scan,
                                rows=rows, index=(0 if rows else 3),
                                timeout=1.5,
                                nudge=getattr(config, 'CLICK_CHAT_TAB', False))
    s = info.get('send') or {}
    if info.get('ok'):
        print(f'[send] 静默发送成功({info["reason"]}): 建窗抢 {info["steal_ms"]}ms '
              f'投递 {s.get("elapsed_ms")}ms 偏离 {s.get("deviation")}次 '
              f'占用 {s.get("occupied_ms")}ms')
        return True
    print(f'[send] 独立聊天窗链路失败: {info.get("reason")}')

    if info.get('unverified') or _maybe_sent(info.get('reason')):
        # 字投进去了但拿不到证据(典型: 回车后输入框没清空): 消息其实已经发出去了。
        # 重发会造成重复消息, 所以标 maybe_sent + 硬失败, 由调用方按"已送达"处理。
        last_fail_hard = True
        last_fail_kind = 'maybe_sent'
        print('[send] 已投递但无法验证是否送达, 按已送达处理, 不重发(以免重复)')
        return False

    # 2026-11-11: 旧的两条兜底链路整段删除 ——
    #   ① "点头像切会话"(goto_chat_by_list) 会改掉用户眼前的窗口内容;
    #   ② "按备注搜索拉回会话"(send_to search_only) 要激活微信 + ctrl+F + 剪贴板,
    #      单次占前台 3~5s, 用户体感就是"屏幕乱跳、正在打的字被抢走"。
    # 现在只保留静默链路: 复用已有子窗 -> 按行双击建窗(最多 5 次) -> PostMessage 投递。
    # 建不出来就放弃这一条, 绝不抢前台。
    last_fail_kind = 'unsent'
    print(f'[send] {remark} 静默链路没建成窗口(双击已试 {scan} 行), 本条不发(不抢前台)')
    return False


def send_to(remark, text, prefix=None, hwnd=None, search_only=False):
    """统一入口: 自动加 [AI生成] 前缀, 激活微信, 搜索进入目标聊天并发送.
    搜索只粘贴备注的前8位数字(会话ID, 如 '示例会话'), 不粘贴完整名字——
    避免把别人的名字写入剪贴板/搜索框; 前8位数字足以唯一命中目标会话.

    prefix=None 用默认 [AI生成]; 首次话术传 '' 或它自己的前缀, 见 send_to_quiet。
    hwnd: 由 _quiet_main 按进程指纹确认过的目标账号主窗口, 双开时必须传,
          否则 _activate() 可能选到另一个号。
    search_only: 只搜索进入会话(把会话拉回列表首行), 不粘贴不发送。给
          send_to_quiet 当兜底用: 发送仍走带标题校验的独立聊天窗链路。
    """
    prev_fg = user32.GetForegroundWindow() if search_only else None
    text = f'{config.AI_PREFIX if prefix is None else prefix}{text}'
    digits = ''.join(ch for ch in remark if ch.isdigit())
    remark8 = digits[:8] or remark          # 前8位数字=会话ID; 无数字则回退完整备注
    print(f'[send] 准备发送到 {remark}: {text[:60]} (搜索用 {remark8})')
    if hwnd is None:
        hwnd = _activate()
    elif not _bring_to_front(hwnd):
        print('[send] 微信窗口置前失败')
        return False
    if not hwnd:
        print('[send] 微信窗口激活失败')
        return False
    print('[send] 微信已激活, 写入剪贴板...')
    time.sleep(0.5)
    try:
        _set_clipboard(text)
    except OSError as e:
        print(f'[send] 剪贴板写入失败: {e}, 放弃本轮发送(回队重试)')
        return False
    time.sleep(0.3)
    _close_ime()

    # 关键: ctrl+F 前强制确认微信仍在最前, 否则按键会落到其他窗口(弹搜索框/误操作)
    if user32.GetForegroundWindow() != hwnd:
        print('[send] 微信不在前台, 重新置前...')
        if not _bring_to_front(hwnd):
            print('[send] 微信置前失败, 放弃本轮发送, 消息回队重试')
            return False
    # ctrl+F 打开搜索(输入框内也可用, 用户已验证)
    print('[send] 搜索聊天...')
    _combo(VK_CTRL, VK_F)
    time.sleep(0.8)
    _combo(VK_CTRL, VK_A)          # 清空搜索框
    _key(VK_BACK)
    time.sleep(0.3)
    try:
        _set_clipboard(remark8)        # 搜索用: 只粘贴前8位数字会话ID
    except OSError as e:
        print(f'[send] 会话ID写入剪贴板失败: {e}, 放弃本轮发送(回队重试)')
        return False
    _combo(VK_CTRL, VK_V)
    time.sleep(0.8)
    _press_enter(3, gap=0.3)       # 进入目标聊天
    time.sleep(1.0)
    if search_only:
        print(f'[send] 已按 {remark8!r} 搜索进入会话(不发送)')
        if prev_fg and prev_fg != hwnd and user32.IsWindow(prev_fg):
            cwin.restore_fg(prev_fg)
        return True
    # 关键修复: 搜索时剪贴板已被 remark 覆盖, 发送前必须重新写入回复文本,
    # 否则 ctrl+V 粘贴的是别人名字/上次内容 (发错窗口的根因)
    try:
        _set_clipboard(text)
    except OSError as e:
        print(f'[send] 发送前剪贴板写入失败: {e}, 放弃本轮发送(回队重试)')
        return False
    _combo(VK_CTRL, VK_V)          # 粘贴回复
    time.sleep(0.5)
    _press_enter(3, gap=0.3)       # 发送
    time.sleep(config.SEND_DELAY)
    print('[send] 发送流程完成')
    return True
