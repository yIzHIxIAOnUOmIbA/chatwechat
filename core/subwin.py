# -*- coding: utf-8 -*-
"""独立聊天窗(子窗口)通道: 一次打开, 长期后台发送。

背景
----
微信主窗口同一时刻只渲染一个会话, 给不同的人发消息必须反复切会话, 而每次
切换微信都会自己抢前台。对固定名单, 可以换一条路: 双击会话列表的那一行,
微信会给这个会话生成一个**独立的聊天窗**(标题就是对方昵称), 之后所有输入
和发送都只对那个窗口 PostMessage —— 全程不需要激活。

实测(2026-11-02, 小号)
----------------------
* 子窗口创建的那一刻会抢一次前台: 微信是显式 SetForegroundWindow,
  WS_EX_NOACTIVATE / LockSetForegroundWindow / 丢到别的虚拟桌面 都拦不住
  (虚拟桌面更糟: 系统会直接把整个桌面切过去)。
* 窗口 SetWindowPos 移到 (-12000,-12000) 之后: post_text 输入有效、
  回车发送有效、前台 0 变化; PrintWindow 仍能截到完整画面。
* 双击同一个会话不会新建窗口, 而是复用已有窗口并把它提到前台。

所以这里的定位是: **用一次性的建窗代价, 换每个会话长期的零抢占**。

适用范围(2026-11-02 补测)
------------------------
早期结论是"只适合固定名单", 因为建窗要靠头像模板定位列表行, 而自动回复的对象
是任意联系人、没人事先建模板。现在改成**按行号定位**(见 open_by_row): 刚来过
消息的会话就在列表首行, 双击首行即可, 建出来的窗口标题对不上目标就立刻关窗
放弃 —— 于是"回复所有人"也成立了, 模板只留给主动发消息用。

实测(同一台机, 文件传输助手):
* 关掉窗口后走 open_by_row + send_silent: 建窗抢 0ms(fg_guard 视角), 投递 744ms
* 之后复用窗口再发: 投递 668~720ms, 2ms 采样偏离 0 次、占用 0ms
* 长文本 36 字按 0.02s/字投递: 不吞字, 输入框清空校验通过
"""
import ctypes
import ctypes.wintypes as wintypes
import time

from core import win

u = win.u

WM_LBUTTONDBLCLK = 0x0203
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080

PARK_X, PARK_Y = -12000, -12000      # 实测该坐标可用, 且 PrintWindow 仍有画面
MAIN_TITLES = ('微信', 'WeChat')
QT_CLASS_PREFIX = 'Qt515'
TRAY_HINT = 'Tray'

u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           ctypes.c_uint]
u.SetWindowPos.restype = wintypes.BOOL
u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
u.SetWindowLongPtrW.restype = ctypes.c_ssize_t


# ----------------------------------------------------------------------------
# 枚举 / 查询
# ----------------------------------------------------------------------------

def sub_windows(wechat_hwnd, visible_only=False):
    """列出该微信实例的所有独立聊天窗(排除主窗口与托盘图标窗口)。

    判定方式: 同一个 pid、Qt 顶层窗口、类名不是 WxTrayIconMessageWindow,
    标题不是"微信/WeChat"、且不是 wechat_hwnd 本身。

    标题过滤这条是必须的: 双开时另一个号的主窗口也是"微信"标题、同 pid,
    以前会被当成新冒出来的聊天窗 —— 建窗时按"点错人"把它 WM_CLOSE 掉,
    等于给另一个号发退出指令。
    """
    pid = win.pid_of(wechat_hwnd)
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(h, _l):
        p = ctypes.c_ulong()
        u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and h != wechat_hwnd:
            cls = win._cls(h)
            if (cls.startswith(QT_CLASS_PREFIX) and TRAY_HINT not in cls
                    and win._title(h) not in MAIN_TITLES):
                if not visible_only or u.IsWindowVisible(h):
                    out.append(h)
        return True

    u.EnumWindows(cb, 0)
    return out


def find_sub(wechat_hwnd, title):
    """按标题(联系人昵称)找已存在的独立聊天窗, 找不到返回 None。"""
    for h in sub_windows(wechat_hwnd):
        if win._title(h) == title:
            return h
    return None


def is_parked(hwnd, margin=-2000):
    """窗口是否在屏幕外(margin 内都算还在屏幕上)。"""
    l, t, _w, _h = win._rect(hwnd)
    return l < margin or t < margin


# ----------------------------------------------------------------------------
# 摆放 / 打开 / 关闭
# ----------------------------------------------------------------------------

def _set_toolwindow(hwnd, on):
    """切 WS_EX_TOOLWINDOW, 窗口保持 WS_VISIBLE 不变。"""
    if not u.IsWindow(hwnd):
        return False
    ex = int(u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
    want = (ex | WS_EX_TOOLWINDOW) if on else (ex & ~WS_EX_TOOLWINDOW)
    if want == ex:
        return True
    u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, want)
    # SWP_FRAMECHANGED 让窗口管理器立刻重读扩展样式, 否则任务栏要等下次重绘
    u.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                   SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                   | SWP_FRAMECHANGED)
    return (bool(int(u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
                 & WS_EX_TOOLWINDOW) == on)


def hide_from_taskbar(hwnd):
    """让独立聊天窗从任务栏 / Alt+Tab 消失, 同时保住静默投递需要的一切。

    park() 只把窗口挪到屏外, 顶层窗口照样被任务栏枚举 —— 攒几个会话就是一排
    同名窗口, 用户切窗口很难用。WS_EX_TOOLWINDOW 是 Windows 标准的"工具窗"
    标记: 不进任务栏、不进 Alt+Tab, 但窗口仍然 visible, PrintWindow 仍能截图,
    PostMessage 仍能投递, 所以 send_silent 的"字进框 / 回车后清空"像素校验
    全都不受影响(微信自己的悬浮小窗本来就是这个样式)。

    不要用 ShowWindow(SW_HIDE): 隐藏后 Qt 停止绘制, 像素校验会直接失败。
    也不要丢到另一个虚拟桌面: 实测把窗口 MoveWindowToDesktop 会把整个桌面
    切过去, 比抢一次前台更打扰人。
    """
    return _set_toolwindow(hwnd, True)


def restore_in_taskbar(hwnd):
    """unpark 把窗口放回屏幕时撤掉工具窗标记, 免得看得见却 Alt+Tab 不到。"""
    return _set_toolwindow(hwnd, False)


def park(hwnd, x=PARK_X, y=PARK_Y):
    """把窗口挪出屏幕、从任务栏移除, 但不激活。位置变了尺寸不变, 渲染和消息处理照常。

    SetWindowPos 同步生效(没带 SWP_ASYNCWINDOWPOS), 原来固定 sleep(0.15)
    纯属保守 —— "新建窗口再发送"每条都要付这 150ms。
    """
    hide_from_taskbar(hwnd)
    ok = bool(u.SetWindowPos(hwnd, 0, x, y, 0, 0, SWP_NOACTIVATE | SWP_NOSIZE))
    time.sleep(0.03)
    return {'ok': ok, 'rect': win._rect(hwnd)}


def unpark(hwnd, x=200, y=200):
    """把窗口放回屏幕(排查/人工查看用), 并让它重新出现在任务栏 / Alt+Tab。"""
    r = park(hwnd, x, y)
    restore_in_taskbar(hwnd)
    return r


def close_sub(hwnd):
    _BASE_CACHE.pop(hwnd, None)          # 句柄会被系统复用, 留着会串到下一个窗口
    if not u.IsWindow(hwnd):
        return False
    u.PostMessageW(hwnd, 0x0010, 0, 0)       # WM_CLOSE
    # 轮询等它真的消失, 而不是固定睡 0.5s: 关窗通常 50~80ms 就完成,
    # 固定等待在"点错人换下一行"这种连续失败路径上会白白叠好几秒。
    t0 = time.time()
    while time.time() - t0 < 0.5:
        if not u.IsWindow(hwnd):
            return True
        time.sleep(0.02)
    return not u.IsWindow(hwnd)


def _dblclick(hwnd, x, y):
    lp = (y << 16) | (x & 0xFFFF)
    u.PostMessageW(hwnd, win.WM_MOUSEMOVE, 0, lp)
    u.PostMessageW(hwnd, win.WM_LBUTTONDOWN, 1, lp)
    u.PostMessageW(hwnd, win.WM_LBUTTONUP, 0, lp)
    time.sleep(0.02)
    u.PostMessageW(hwnd, WM_LBUTTONDBLCLK, 1, lp)
    u.PostMessageW(hwnd, win.WM_LBUTTONUP, 0, lp)


def open_sub_window(wechat_hwnd, key, title=None, thresh=0.96, margin=0.06,
                    home=None, timeout=3.0, park_after=True):
    """在会话列表里双击 key 对应的会话行, 打开(或复用)它的独立聊天窗。

    参数
    ----
    key    : 头像模板名(core/templates/<key>.png), 与 goto_chat_by_list 同一套
    title  : 联系人昵称。给了就先按标题找已存在的子窗口, 找到直接复用,
             省掉一次建窗(也就省掉那次抢先台)。
    park_after : 打开后立刻移到屏幕外, 避免窗口挡在用户眼前。

    返回 {'ok','hwnd','title','reused','steal_ms','reason'}
    steal_ms 是建窗过程中前台被抢的累计毫秒(复用已有窗口时通常为 0)。
    """
    info = {'ok': False, 'hwnd': None, 'title': title, 'reused': False,
            'steal_ms': 0, 'reason': ''}

    if not u.IsWindow(wechat_hwnd):
        info['reason'] = '主窗口不存在'
        return info

    # 最小化时 grab 只能截到 160x28 的占位图, 模板必然匹配不到;
    # SW_SHOWNOACTIVATE 把窗口还原但不把它提到前台。
    if u.IsIconic(wechat_hwnd):
        u.ShowWindow(wechat_hwnd, 4)
        time.sleep(0.6)

    # 1) 已有同标题窗口 -> 直接复用
    if title:
        exist = find_sub(wechat_hwnd, title)
        if exist:
            if park_after:
                park(exist)
            info.update(ok=True, hwnd=exist, reused=True, reason='复用已有窗口')
            return info

    # 2) 定位会话行(与 goto_chat_by_list 同样的闸门: 宁可失败也不点错人)
    tmpl = win._load_tmpl(key)
    img = win.grab(wechat_hwnd)
    best_y, best, second = win.match_row(img, tmpl)
    info['score'] = round(best, 4)
    info['second'] = round(second, 4)
    if best < thresh:
        info['reason'] = '相似度不足(目标可能不在可视列表)'
        return info
    if best - second < margin:
        info['reason'] = '区分度不足(存在同头像候选)'
        return info

    yc = best_y + win.AV_W // 2
    before = set(sub_windows(wechat_hwnd))
    home = home or win.pick_home_window(exclude=wechat_hwnd)
    if home in (None, wechat_hwnd):
        home = None

    # 3) 双击建窗。建窗瞬间必抢前台, 守卫只能把这段时间压短并保证归还
    new = None
    with win.fg_guard(home=home) as g:
        _dblclick(wechat_hwnd, win.CLICK_X, yc)
        t0 = time.time()
        while time.time() - t0 < timeout:
            fresh = [h for h in sub_windows(wechat_hwnd)
                     if h not in before and u.IsWindowVisible(h)]
            if fresh:
                new = fresh[0]
                break
            time.sleep(0.05)
        info['steal_ms'] = g.steal_ms

    if not new:
        # 没有新窗口: 可能是标题不同(比如群名带后缀), 兜底取最新出现的一个
        info['reason'] = '双击后未出现新的聊天窗'
        return info

    if park_after:
        park(new)
    info.update(ok=True, hwnd=new, title=win._title(new), reason='ok')
    return info


# ----------------------------------------------------------------------------
# 任意联系人: 按"列表第 N 行"建窗, 不需要头像模板
#
# 自动回复场景里要回的会话必然刚来过消息, 就在列表最上面 —— 所以"双击首行"足够
# 定位目标; 模板匹配是给主动发消息用的, 不该作为静默链路的前置门槛(旧实现就卡
# 在这里: 没模板直接放弃, 退回激活 + ctrl+F + 剪贴板, 单次占前台 3~5s)。
# 点错人的风险由标题校验兜住: 建出来的窗口标题对不上, 立刻关窗并放弃。
# ----------------------------------------------------------------------------

ROW_H = 68
FIRST_ROW_Y = win.LIST_Y0 + 32      # 684x911 下 = 112, 实测双击命中首行


def row_y(index=0):
    return FIRST_ROW_Y + int(index) * ROW_H


# ---- 未读红点定位 ----------------------------------------------------------
# 2026-11-11 用户思路: 置顶的前 3 行是固定的, 真正的目标通常在"有红点的那一行";
# 直接双击红点所在行比"从第 4 行往下盲扫"命中率高得多, 也不用 OCR 名字。
DOT_X0, DOT_X1 = 60, 245      # 会话列表里找红点的横向范围(含右侧角标)
DOT_CLICK_X = 205             # 双击红点时用的 x(落在角标上, 也在行内, 不会点到导航栏)
DOT_MIN_PIXELS = 6            # 一行里红像素少于这个数就当没红点(抗数字描边噪声)
PINNED_ROWS = 3               # 置顶行数: 有红点时把这几行排到后面再试


def _red_count(band):
    a = band.astype('int16')
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return int(((r > 165) & (r - g > 70) & (r - b > 70) &
                (g < 155) & (b < 155)).sum())


def unread_rows(wechat_hwnd, n_rows=11):
    """返回"这一行有未读红点"的行号列表, 非置顶行优先, 置顶行排在后面。

    读的是主窗口的一张截图, 不做任何点击 —— 只用来看, 不改前台。
    拿不到截图 / numpy 不可用时返回空列表(调用方回退到按行扫)。
    """
    try:
        im = win.grab(wechat_hwnd)
        if im is None or getattr(im, 'size', 0) == 0:
            return []
        hits = []
        for i in range(int(n_rows)):
            cy = row_y(i)
            y1, y2 = max(0, cy - 28), min(im.shape[0], cy + 28)
            if y2 <= y1:
                break
            c = _red_count(im[y1:y2, DOT_X0:DOT_X1])
            if c >= DOT_MIN_PIXELS:
                hits.append((i, c))
        rest = [i for i, _c in hits if i >= PINNED_ROWS]
        pinned = [i for i, _c in hits if i < PINNED_ROWS]
        return rest + pinned
    except Exception:                                          # noqa: BLE001
        return []


def _norm(s):
    return ''.join(str(s or '').split())


def title_matches(title, expect):
    """窗口标题是否对应目标联系人(互相包含即算, 兼容列表里的省略号截断)。

    2026-11-11 补: 标题里常带"1024xxxx"这种会话 ID, 而备注可能只写了名字的后半段,
    纯包含判断会把**已经正确打开**的窗口判成"别人的"然后关掉(用户实测看到"正确
    窗口被关")。这里再加一条: 8 位数字前缀相同也算命中。
    """
    t, e = _norm(title), _norm(expect)
    if not e:
        return True
    if not t:
        return False
    if t in e or e in t:
        return True
    dt = ''.join(ch for ch in str(title or '') if ch.isdigit())[:8]
    de = ''.join(ch for ch in str(expect or '') if ch.isdigit())[:8]
    return bool(dt) and dt == de


def describe_accounts(mains):
    """把主窗候选打成一句人话, 供"分不清账号"时报错/让人指定用。"""
    out = []
    for h in mains:
        subs = [win._title(x) for x in sub_windows(h)]
        out.append(f'{h}({win._rect(h)[2]}x{win._rect(h)[3]}, '
                   f'{"最小化" if u.IsIconic(h) else "可见"}, '
                   f'{len(subs)}个聊天窗{"/" + "、".join(subs[:3]) if subs else ""})')
    return '; '.join(out)


def pick_account(mains, contact=None, tmpl=None):
    """多开时确定该用哪个账号主窗口, 返回 (hwnd, 依据) 或 (None, '')。

    双开实测(2026-11-02): 两个主窗类名、标题(都叫"微信")、可调大小全部相同,
    连候选分数都会因为"谁恰好被最小化"而翻转。唯一稳定的差异是"各自会话列表里
    有哪些人"。判据按可信度:

      1. 账号指纹模板(tmpl) —— 该号列表里常驻的某个人(如小号置顶了大号本人):
         实测 0.9991 命中、次优 0.52, 一票定音
      2. 目标联系人只在一个号里有独立聊天窗
      3. 只有一个号

    两个号都成立(如都置顶了文件传输助手)就是多义, 返回 None 而不是挑一个:
    猜错会把消息发给另一个号的同名联系人, 是本项目最不能犯的错。
    """
    if tmpl and win.tmpl_exists(tmpl):
        h, score = win.find_hwnd_by_tmpl(tmpl, mains)
        if h:
            return h, f'账号指纹 {tmpl} 命中(score={score:.4f})'
    if contact:
        hit = [m for m in mains
               if any(title_matches(win._title(x), contact)
                      for x in sub_windows(m))]
        if len(hit) == 1:
            return hit[0], f'{contact} 的聊天窗只在 hwnd={hit[0]} 下'
        if len(hit) > 1:
            return None, ''
    if len(mains) == 1:
        return mains[0], '只有一个微信主窗口'
    return None, ''


# ----------------------------------------------------------------------------
# 聊天页自愈 (2026-11-05)
#
# 微信停在「发现 / 视频号」页时中间栏不是会话列表, open_by_row 的 (170,112)
# 会双击到「朋友圈」并弹出朋友圈窗口 —— 标题校验把它关掉后每行都对不上, 表现
# 就是 "独立聊天窗链路失败: 标题不符(窗口='朋友圈' 目标='AAA')"。
# 左栏 tab 是独立 Qt 子控件, PostMessage / SendMessage 都点不动, 只有真实鼠标
# 点击有效; 所以只在检测到确实不在聊天页时才付这一次前台代价。
# ----------------------------------------------------------------------------

CHAT_TAB_X, CHAT_TAB_Y = 38, 117     # 684x911 客户区下左栏「聊天」图标中心


def is_chat_page(wechat_hwnd):
    """PrintWindow 截图判断会话列表是否可见; 判不了返回 None(别猜)。

    用 img.mean(axis=2) 而不是标准灰度: grab 出来的通道顺序可能是 RGB 也可能
    是 BGR, 三通道取平均对顺序不敏感。区域按窗口尺寸取比例, 换分辨率也成立。

    只看会话列表那一栏(x 10%~34%), 不看右栏: 聊天页没有选中任何会话时右栏
    是全空的, 拿右栏亮度当判据会把这种正常状态误判成「不在聊天页」。
    聊天页(有无选中会话都一样): 头像 + 名字 + 消息摘要, 均值 ≈ 69~72;
    发现页: 只有几个小图标和短文字, 均值 ≈ 54。阈值 62 能分开。
    """
    try:
        img = win.grab(wechat_hwnd)
        h, w = img.shape[:2]
        if h < 300 or w < 300:
            return None
        g = img.mean(axis=2)
        # 会话列表栏是固定像素宽(左栏 68px + 列表 172px), 不随窗口宽度变化。
        # 必须用绝对坐标: 用比例算的话窗口一拉宽, 采样区就滑进右栏空白,
        # 均值掉下来会被误判成「不在聊天页」, 进而去真实点左栏 tab 抢前台。
        y1, y2 = 80, min(700, h)
        x1, x2 = 68, min(240, w)
        if y2 <= y1 or x2 <= x1:
            return None
        col = g[y1:y2, x1:x2].mean()
        return bool(col > 62)
    except Exception:
        return None


def ensure_chat_tab(wechat_hwnd):
    """保证微信停在聊天页(会话列表可见), 返回 True 表示可以双击列表行。

    正常路径只截一次图, 0 抢占; 只有不在聊天页时才真实点一下左栏「聊天」图标,
    点完把前台还给原窗口。最小化时先 SW_SHOWNOACTIVATE 还原, 否则 PrintWindow
    只有 160x28 的占位图, 判据会失效。
    """
    if not u.IsWindow(wechat_hwnd):
        return False
    if u.IsIconic(wechat_hwnd):
        u.ShowWindow(wechat_hwnd, 4)
        time.sleep(0.3)
    if is_chat_page(wechat_hwnd) is True:
        return True
    # 2026-11-11 实测: 会话列表明明在(AAA/文件传输/牛哥市… 六行都在), 但
    # is_chat_page 返回 False, 于是聊天页判定失败 -> 整条发送链路直接放弃。
    # 红点只可能出现在会话列表里, 所以"扫得到红点"足以证明列表可见。
    try:
        if unread_rows(wechat_hwnd):
            return True
    except Exception:                                          # noqa: BLE001
        pass

    pt = wintypes.POINT(CHAT_TAB_X, CHAT_TAB_Y)
    u.ClientToScreen(wechat_hwnd, ctypes.byref(pt))
    prev = u.GetForegroundWindow()
    try:
        win.restore_fg(wechat_hwnd)
        time.sleep(0.12)
        u.SetCursorPos(pt.x, pt.y)
        time.sleep(0.08)
        u.mouse_event(0x0002, 0, 0, 0, 0)     # MOUSEEVENTF_LEFTDOWN
        u.mouse_event(0x0004, 0, 0, 0, 0)     # MOUSEEVENTF_LEFTUP
        time.sleep(0.5)
    finally:
        if prev and prev != wechat_hwnd and u.IsWindow(prev):
            win.restore_fg(prev)
    ok = is_chat_page(wechat_hwnd) is True
    print(f'[subwin] 切回聊天页: {"成功" if ok else "失败"}')
    return ok


def nudge_chat_tab(wechat_hwnd, x=None, y=None, gap=0.10, restore=True):
    """双击左栏第一个导航图标(「聊天」), 把微信重新定回聊天页/会话列表。

    每次回复前先做这一步: 让微信自己把会话列表锁回目标联系人, 比"先猜当前停在
    哪一页、再补救"稳 —— 建窗那一步是双击列表行, 只要主窗口还停在别的页
    (发现/通讯录/朋友圈), 双击就会落到别的东西上, 表现为标题不符/建错窗。

    左栏第一个图标是独立 Qt 子控件, PostMessage / SendMessage 都点不动(实测
    见上方注释), 只有真实鼠标有效, 所以这里必然有一次前台切换; 点完立刻把
    前台还给原窗口。返回 True 表示两下都发出去了(不代表微信一定切页成功)。
    """
    if not u.IsWindow(wechat_hwnd):
        return False
    if u.IsIconic(wechat_hwnd):
        # 最小化时客户区是 160x28 的占位图, 点坐标全错; 先还原(不激活)
        u.ShowWindow(wechat_hwnd, 4)
        time.sleep(0.3)
    cx = CHAT_TAB_X if x is None else int(x)
    cy = CHAT_TAB_Y if y is None else int(y)
    pt = wintypes.POINT(cx, cy)
    u.ClientToScreen(wechat_hwnd, ctypes.byref(pt))
    prev = u.GetForegroundWindow()
    try:
        win.restore_fg(wechat_hwnd)
        time.sleep(0.10)
        u.SetCursorPos(pt.x, pt.y)
        time.sleep(0.06)
        for _ in range(2):                   # 两下
            u.mouse_event(0x0002, 0, 0, 0, 0)    # MOUSEEVENTF_LEFTDOWN
            u.mouse_event(0x0004, 0, 0, 0, 0)    # MOUSEEVENTF_LEFTUP
            time.sleep(gap)
    finally:
        if restore and prev and prev != wechat_hwnd and u.IsWindow(prev):
            win.restore_fg(prev)
    print(f'[subwin] 已双击左栏「聊天」图标({cx},{cy}), 让微信锁回会话列表')
    return True


def open_by_row(wechat_hwnd, index=0, expect=None, park_after=True,
                timeout=0.5, home=None, scan=1, rows=None):
    """双击会话列表第 index 行, 拿到它的独立聊天窗, 并用标题校验点对了人。

    scan > 1: 标题对不上就关窗继续试下一行(最多 index+scan-1 行)。
    不能只认首行 —— 置顶会话、文件传输助手、别的未读都会把人往下挤,
    实测小号列表首行常年是"文件传输助手", 只试首行的结果就是
    标题不符 -> 关窗 -> 静默链路整体失败。

    返回 {'ok','hwnd','title','reused','steal_ms','reason','row'}
    已有标题匹配的窗口时直接复用(reused=True, 0 抢占); 扫完仍对不上就返回
    ok=False —— 宁可这次不发, 也不往错的人发。
    """
    info = {'ok': False, 'hwnd': None, 'title': '', 'reused': False,
            'steal_ms': 0, 'reason': '', 'row': index, 'unminimized': False}
    if not u.IsWindow(wechat_hwnd):
        info['reason'] = '主窗口不存在'
        return info

    # 1) 复用: 已有窗口标题对得上就不用再抢那一次前台
    if expect:
        exist = find_sub(wechat_hwnd, expect)
        if not exist:
            for x in sub_windows(wechat_hwnd):
                if title_matches(win._title(x), expect):
                    exist = x
                    break
        if exist and u.IsWindow(exist):
            if park_after and not is_parked(exist):
                park(exist)
            info.update(ok=True, hwnd=exist, title=win._title(exist),
                        reused=True, reason='复用已有窗口')
            return info

    # 主窗最小化时它和子窗都只剩 160x28 占位矩形, 双击坐标不存在。
    # 这里后台恢复一次: SW_SHOWNOACTIVATE(4) 只让窗口变可见, 不抢前台焦点,
    # 焦点仍在用户当前窗口上; 建完窗由调用方按 unminimized 标记最小化回去。
    if u.IsIconic(wechat_hwnd):
        u.ShowWindow(wechat_hwnd, 4)     # SW_SHOWNOACTIVATE
        time.sleep(0.3)
        if u.IsIconic(wechat_hwnd):
            info['reason'] = '主窗口最小化, 无法建聊天窗(需先恢复微信)'
            return info
        info['unminimized'] = True

    home = home or win.pick_home_window(exclude=wechat_hwnd)
    if home in (None, wechat_hwnd):
        home = None

    # 双击的是「会话列表」的行。微信若停在「发现/视频号」页, 这些坐标落在
    # 朋友圈等功能项上, 会弹出一个标题不符的窗口并被逐行关掉。先自愈到聊天页。
    if not ensure_chat_tab(wechat_hwnd):
        info['reason'] = '会话列表不可见(无法切回聊天页)'
        return info

    # 用户指定的顺序: 没有现成子窗时, 先"双击两下导航"把微信定回聊天页,
    # 再双击红点所在位置建子窗, 最后把焦点还给原来的窗口(fg_guard 负责)。
    if rows:
        try:
            nudge_chat_tab(wechat_hwnd, restore=True)
            time.sleep(0.12)
            nudge_chat_tab(wechat_hwnd, restore=True)
            time.sleep(0.12)
        except Exception:                                      # noqa: BLE001
            pass

    first_reason = ''
    miss = 0                    # 连续几行双击后没建出窗口(列表到底/空白区)
    # rows 显式给了行号就按它来(红点定位用); 否则从 index 往下扫 scan 行。
    seq = [int(x) for x in rows] if rows else list(
        range(index, index + max(1, int(scan))))
    # 整个"逐行尝试"共用一次 fg_guard: 中途不回抢焦点, 只在确定拿到正确窗口
    # (或彻底失败)时归还一次。用户反馈"不要频繁归还, 确认正确再还"。
    guard = win.fg_guard(home=home)
    g = guard.__enter__()
    try:
        for i in seq:
            info['row'] = i
            before = set(sub_windows(wechat_hwnd))
            new = None
            _dblclick(wechat_hwnd, (DOT_CLICK_X if rows else win.CLICK_X), row_y(i))
            t0 = time.time()
            while time.time() - t0 < timeout:
                # 判据只有一个: 新冒出来的窗口标题是不是目标。
                fresh = [h for h in sub_windows(wechat_hwnd)
                         if h not in before and u.IsWindowVisible(h)]
                if fresh:
                    for h in fresh:
                        if not expect or title_matches(win._title(h), expect):
                            new = h
                            break
                    if not new:
                        # 点出来的是别人: 不关窗, 只 park 到屏幕外(用户 2026-11-15:
                        # "永不关闭子窗口, 下次不用重复打开") —— 关掉再开要重建窗口、
                        # 还要再抢一次前台, 而且实测会把正好开着的 AAA 窗一并关掉。
                        bad = win._title(fresh[0])
                        for h in fresh:
                            park(h)
                        first_reason = first_reason or (
                            f'标题不符(窗口={bad!r} 目标={expect!r}), 已park')
                        break
                    break
                # 没有新窗口 = 这一行的聊天窗本来就开着(微信会复用而不新建),
                # 直接按标题认, 不用等 timeout 走完。
                if expect:
                    for x in sub_windows(wechat_hwnd):
                        if title_matches(win._title(x), expect):
                            new = x
                            break
                    if new:
                        break
                time.sleep(0.015)

            if not new:
                first_reason = first_reason or '双击后未出现聊天窗'
                miss += 1
                if miss >= 3:       # 连续三行都建不出窗, 再往下也是空白
                    break
                continue

            title = win._title(new)
            if expect and not title_matches(title, expect):
                park(new)           # 永不关窗: 留着下次复用
                first_reason = first_reason or (
                    f'标题不符(窗口={title!r} 目标={expect!r}), 已park')
                continue            # 换下一行继续找

            if park_after:
                park(new)
            info.update(ok=True, hwnd=new, title=title, reason='ok')
            return info
    finally:
        info['steal_ms'] += g.steal_ms
        guard.__exit__(None, None, None)

    info['reason'] = first_reason or '未在会话列表中找到目标'
    return info


def send_to_contact(wechat_hwnd, text, contact=None, index=0, park_after=True,
                    delay=0.006, home=None, shot=None, scan=5, timeout=0.5,
                    nudge=False, rows=None):
    """给任意联系人静默发一条: 有窗复用, 没窗就从第 index 行起往下找着建一个, 再投递。

    scan: 最多找几行(见 open_by_row)。目标刚发过消息时通常在前几行, 5 行足够
    覆盖置顶会话和文件传输助手的挤占。
    timeout: 每行双击后等窗口出现的上限, 命中标题立刻返回, 不等满。
    nudge: 发送前先双击左栏「聊天」图标, 把微信锁回会话列表(见 nudge_chat_tab)。

    返回 {'ok','hwnd','title','opened','steal_ms','send','reason'}
    steal_ms 只在建窗那一下可能不为 0(这是全文唯一的抢占点, 一次性)。
    """
    info = {'ok': False, 'hwnd': None, 'title': contact or '', 'opened': False,
            'steal_ms': 0, 'send': None, 'unverified': False, 'reason': ''}

    if nudge:
        try:
            nudge_chat_tab(wechat_hwnd)
        except Exception as e:                              # noqa: BLE001
            print(f'[subwin] 双击「聊天」图标异常({type(e).__name__}): {e}')

    h = None
    if contact:
        h = find_sub(wechat_hwnd, contact)
        if h is None:
            for x in sub_windows(wechat_hwnd):
                if title_matches(win._title(x), contact):
                    h = x
                    break
    if h and u.IsWindow(h):
        if park_after and not is_parked(h):
            park(h)
        info.update(hwnd=h, title=win._title(h), reason='复用已有窗口')
    else:
        o = open_by_row(wechat_hwnd, index=index, expect=contact,
                        park_after=park_after, home=home, scan=scan,
                        timeout=timeout, rows=rows)
        info['steal_ms'] = o['steal_ms']
        if not o['ok']:
            info['reason'] = o['reason']
            return info
        info.update(hwnd=o['hwnd'], title=o['title'], opened=True,
                    reason='新建窗口')

    # 复用一个已经 parked 的旧窗时不会重走 park(), 这里补一次, 保证存量窗口
    # (脚本改之前就开着的那些) 也一并从任务栏移除
    hide_from_taskbar(info['hwnd'])
    s = send_silent(info['hwnd'], text, delay=delay, shot=shot)
    info['send'] = s
    info['ok'] = bool(s.get('ok'))
    info['unverified'] = bool(s.get('unverified'))
    if not info['ok']:
        info['reason'] = s.get('reason') or '投递失败'
    return info


# ----------------------------------------------------------------------------
# 发送
# ----------------------------------------------------------------------------

def send_via_sub(hwnd, text, delay=0.08, home=None, refocus=False, shot=None):
    """向独立聊天窗发一条文字, 全程不激活、不占前台。

    链路: (可选)点输入框 -> 逐字投 WM_IME_CHAR -> 投回车。

    投递期间**不挂 fg_guard**: 见 win.py 模块头第 5 条 —— 边投边守会让微信在
    "被夺回"和"再抢回"之间反复, 8 个字能引发 33 次前台切换(实测)。这里一次
    都不碰前台, 因为目标窗口在屏幕外, 它激活的是自己而不是用户眼前的任何窗口。

    返回 {'ok','hwnd','text','steal_ms','reason'}
    """
    info = {'ok': False, 'hwnd': hwnd, 'text': text, 'steal_ms': 0, 'reason': ''}
    if not u.IsWindow(hwnd):
        info['reason'] = '聊天窗已失效(微信重启或窗口被关)'
        return info

    if refocus:
        x, y = win.input_point(hwnd)
        win._post_click(hwnd, x, y)
        time.sleep(0.25)

    fg0 = u.GetForegroundWindow()
    win.post_text(hwnd, text, delay=delay)
    time.sleep(0.15)
    win.post_key(hwnd, win.VK_RETURN)
    time.sleep(0.35)

    # 只在这里看一次: 万一子窗口真把前台拿走了, 立刻还回去
    if fg0 and fg0 != hwnd and u.GetForegroundWindow() == hwnd:
        win.restore_fg(fg0)

    info.update(ok=True, steal_ms=0, reason='ok')
    if shot:
        try:
            win.grab(hwnd, shot)
            info['shot'] = shot
        except Exception as e:
            info['shot_error'] = str(e)
    return info


# ----------------------------------------------------------------------------
# 带校验的静默发送(生产入口)
#
# 实测(2026-11-02, 684x911 主窗口, 文件传输助手独立窗 park 到 -12000,-12000):
#   post_text(delay=0.08) + 回车, 2ms 采样 1.86s, 前台偏离 0 次、占用 0ms。
#   连发 3 条结论一致 —— 前提是"输入框当前有 Qt 内部焦点"。
#
# 两个待办都落在这里:
#   1) 速度: delay 固定 0.08 是给搜索框留的余量(见 win.post_text 注释), 独立
#      聊天窗只放正文, 可以先 0.02 试投, 用像素校验判定有没有吞字, 不通过就
#      自动退回 0.08 重投 —— 用"事后校验"换掉"事前留余量"。
#   2) 焦点: 子窗口被 Esc/点到消息区之后输入框会丢焦点, 此时投字全部落空。
#      用输入框区域的像素签名判定, 落空就 PostMessage 点一下输入框再重投;
#      屏外窗口的 Post 点击不会激活它(前台 0 变化已验证)。
# ----------------------------------------------------------------------------

FG_GAP = 0.002          # 采样间隔
TYPED_PIX = 200         # 输入框区域像素差超过它 -> 判定"文字已进框"
CLEAR_PIX = 200         # 回车后差值回落到它以内 -> 判定"已发出并清空"


class _FgSampler:
    """后台采样前台窗口变化, 不碰前台。"""

    def __init__(self, gap=FG_GAP):
        import threading
        self.gap = gap
        self.events = []
        self._stop = False
        self._th = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        last = u.GetForegroundWindow()
        t0 = time.time()
        self.events.append((0.0, last))
        while not self._stop:
            cur = u.GetForegroundWindow()
            if cur != last:
                self.events.append((time.time() - t0, cur))
                last = cur
            time.sleep(self.gap)

    def start(self):
        self._th.start()
        return self

    def stop(self):
        self._stop = True
        self._th.join(timeout=1.0)
        return self

    def deviation(self, fg0):
        return [e for e in self.events if e[1] != fg0]

    def occupied_ms(self, hwnd):
        hit = [e for e in self.events if e[1] == hwnd]
        return round((hit[-1][0] - hit[0][0]) * 1000, 1) if len(hit) > 1 else 0.0


def _input_sig(hwnd):
    """输入框区域的像素签名(用于判断"框里有没有字")。

    PrintWindow 偶发全黑(窗口重建/刚挪位置时), 这时签名返回 None 而不是全 0 ——
    全 0 会让"发前发后一样"被误判成"没发出去", 进而重投一条重复消息。
    """
    img = win.grab(hwnd)
    if img is None or not img.any():
        return None
    h, w = img.shape[:2]
    y0, y1 = int(h * 0.78), int(h * 0.96)
    x0, x1 = int(w * 0.08), int(w * 0.96)
    crop = img[y0:y1, x0:x1].astype('int16')
    return crop if crop.any() else None


_BASE_CACHE = {}


def _cached_base(hwnd, fresh=False):
    """空输入框的基线: 回车后输入框必然回到空, 所以同一窗口的基线能一直复用。

    PrintWindow 对没被合成过的窗口要 200ms 起步(首次 210ms、之后 5~7ms),
    而每条消息前后各要一张图 —— 这是首发耗时里最大的一块。
    缓存后每条只付回车后那一张, 顺带把新基线存回去, 前一张白拿。
    """
    b = None if fresh else _BASE_CACHE.get(hwnd)
    if b is not None:
        return b
    b = _input_sig(hwnd)
    if b is not None:
        _BASE_CACHE[hwnd] = b
    return b


def _pixdiff(a, b):
    if a.shape != b.shape:
        return 10 ** 9
    d = abs(a - b)
    if d.ndim == 3:
        d = d.max(axis=2)
    return int((d > 40).sum())


def _gdiff(a, b):
    """None 安全的像素差: 任一侧截图不可用就返回 None(表示"不知道")。"""
    if a is None or b is None:
        return None
    return _pixdiff(a, b)


def send_silent(hwnd, text, delay=0.02, fallback_delay=0.08, verify=True,
                shot=None, typed_pix=TYPED_PIX, clear_pix=CLEAR_PIX):
    """向独立聊天窗静默发一条文字: 投字 -> (校验) -> 回车 -> (校验清空)。

    返回 {'ok','hwnd','text','delay','verified','retried_focus','retried_slow',
          'steal_ms','occupied_ms','deviation','elapsed_ms','type_ms','reason'}
    ok 的判据是"回车后输入框回到空", 不是"PostMessage 调用没报错"。
    """
    info = {'ok': False, 'hwnd': hwnd, 'text': text, 'delay': delay,
            'verified': False, 'retried_focus': False, 'retried_slow': False,
            'steal_ms': 0, 'occupied_ms': 0.0, 'deviation': 0,
            'elapsed_ms': 0.0, 'type_ms': 0.0, 'reason': ''}
    if not u.IsWindow(hwnd):
        info['reason'] = '聊天窗已失效'
        return info
    # 最小化时 Qt 不重绘, PrintWindow 只能拿到 160x28 的白条(实测), 投字也进不去,
    # 而校验全黑 —— 旧逻辑会因此报"成功"(双开实测: ok=True 但消息没发)。
    # 恢复窗口 = 抢前台: 实测即使先 SetWindowPos 挪到屏外, 再 SW_SHOWNOACTIVATE,
    # Qt 仍会自己 SetForegroundWindow(前景确实变了), 违背静默前提。
    # 所以这里直接拒绝, 让调用方自己决定要不要为这条消息付一次激活。
    if u.IsIconic(hwnd):
        info['reason'] = '聊天窗处于最小化, 静默投递不可用(需先恢复窗口)'
        return info
    if not is_parked(hwnd):
        park(hwnd)

    t_start = time.time()
    base = _cached_base(hwnd) if verify else None
    if verify and base is None:
        verify = False
        info['verify_skipped'] = '发前截图不可用, 跳过校验'
    fg0 = u.GetForegroundWindow()
    sp = _FgSampler().start()

    def _type(d):
        t0 = time.time()
        win.post_text(hwnd, text, delay=d)
        return (time.time() - t0) * 1000

    info['type_ms'] = round(_type(delay), 1)

    win.post_key(hwnd, win.VK_RETURN)

    if verify:
        # 只在发完之后检查这一遍: 输入框回到空 = 发出去了。
        # 旧版投字后还要再截一次图做"吞字检测", 不通过就点输入框重投 + 降速重投,
        # 一次多花 ~80ms 起步, 实测 delay=0.02 并不吞字 —— 去掉了。
        # 真怀疑没发出去时, 上层回读消息库比看像素准。
        deadline = time.time() + 1.2
        time.sleep(0.02)
        after = _input_sig(hwnd)
        left = _gdiff(base, after)
        while (left is not None and left >= clear_pix
               and time.time() < deadline):
            time.sleep(0.03)
            after = _input_sig(hwnd)
            left = _gdiff(base, after)
        if left is not None and left >= 10 ** 9:
            # 基线是旧尺寸的(_pixdiff 遇到 shape 不同直接给 1e9): 判不了。
            # 宁可跳过校验也不能报"没发出去" —— 那会让上层补发一条重复消息。
            left = None
            info['verify_skipped'] = '窗口尺寸变了, 基线失效, 跳过校验'
        info['left_pix'] = left
        if left is None:
            verify = False
            info['verify_skipped'] = (info.get('verify_skipped')
                                      or '回车后截图不可用, 跳过校验')
        else:
            info['verified'] = left < clear_pix
            if not info['verified']:
                info['reason'] = '回车后输入框未清空(可能没发出去)'
            elif after is not None:
                # 这张就是"刚清空的输入框", 直接当下一轮的基线
                _BASE_CACHE[hwnd] = after
    else:
        time.sleep(0.35)

    sp.stop()
    dev = sp.deviation(fg0)
    info['deviation'] = len(dev)
    info['occupied_ms'] = sp.occupied_ms(hwnd)
    if fg0 and u.GetForegroundWindow() == hwnd:
        win.restore_fg(fg0)          # 兜底: 真被抢走就还回去
        info['restored_fg'] = True
    # 三态而不是两态: 验证通过 = 成功; 确定失败 = 可重试; 验证不可用 = 未知,
    # 不能算成功(双开实测踩过: 最小化窗口全黑 -> ok=True, 其实没发出去)。
    # 未知单列一个 unverified 标志, 调用方据此决定"重试"还是"只上报",
    # 避免把一条可能已经发出的消息再发一遍。
    if verify:
        info['ok'] = bool(info['verified'])
    elif info.get('verify_skipped'):
        info['ok'] = False
        info['unverified'] = True
        info['reason'] = f'未验证({info["verify_skipped"]})'
    else:
        info['ok'] = True
    if info['ok']:
        info['reason'] = 'ok'
    info['elapsed_ms'] = round((time.time() - t_start) * 1000, 1)
    if shot:
        try:
            win.grab(hwnd, shot)
            info['shot'] = shot
        except Exception as e:
            info['shot_error'] = str(e)
    return info


def ensure_sub(wechat_hwnd, key, title, **kw):
    """拿一个可用的聊天窗: 已存在且存活就复用, 否则重新打开。"""
    h = find_sub(wechat_hwnd, title) if title else None
    if h and u.IsWindow(h):
        if is_parked(h) is False:
            park(h)
        return {'ok': True, 'hwnd': h, 'reused': True, 'title': title}
    return open_sub_window(wechat_hwnd, key, title=title, **kw)
