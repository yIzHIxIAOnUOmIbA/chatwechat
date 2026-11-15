# -*- coding: utf-8 -*-
"""UI 未读快通道: 不读消息库, 靠红色角标 + 后台窗口画面 diff 快速回首次话术。

微信把消息写进 message_0.db 有约 8s 延迟, 但首次回复本来就是固定话术,
不需要消息内容。启动时必定没有新消息; 导航栏第一个功能项右上角的未读总数
角标位置固定(实测 bbox (42,98,15,15), 窗口宽 700/856/1000 下不变), 图片可缓存。

链路: 启动缓存基线 -> 每 80ms 后台 PrintWindow 主窗 -> 角标签名变化才算来消息
      -> 对所有「已经 park 在后台的独立聊天窗」做画面 diff, 谁变了谁就是收消息
         对象(不读库、不猜备注、不管对方是谁)
      -> 直接对那个窗 PostMessage 投字投回车。

不双击、不建新窗、不关窗: 双击会复用旧窗并把它提到前台(实测抢 188ms),
既慢又违背「后台静默 P0」。已有后台窗就是为这个准备的, 直接复用即可。
仅当该会话一个后台窗都没有时, 才退化为 PostMessage 点列表行 + 主窗口投递
(仍然不新建独立窗、不抢前台)。
"""
import time

import cv2
import numpy as np

from core import win, subwin

u = win.u

NAV = (42, 98, 57, 113)          # 总未读角标 (x1,y1,x2,y2), 相对客户区左上角
LIST_X1, LIST_X2 = 68, 240       # 会话列表左右边界
FIRST_ROW_Y, ROW_H, N_ROWS = 112, 68, 11
ROW_DIFF_TH = 2.0                # 行签名平均差阈值(静止实测为 0)
SUB_DIFF_TH = 0.3                # 后台窗画面差阈值(静止实测为 0, 别卡太死)
POLL_INTERVAL = 0.08
SKIP_TITLES = ('Weixin', '微信', 'WeChat', '')
# 客户区最小可用尺寸: 小于这个说明窗口被最小化(rect 只剩 160x28 那种)或
# 折叠/正在重绘, PrintWindow 出来的帧切不出角标区, cv2.cvtColor 会抛
# !_src.empty()。实测这种帧一天能刷 30 万条异常, 必须先在 poll 里判定掉。
MIN_W, MIN_H = 260, 130


def _shape(im):
    """帧尺寸的可读描述, 只用于日志。"""
    sh = getattr(im, 'shape', None)
    return 'None' if sh is None else 'x'.join(str(v) for v in sh)


def _usable(im):
    """这一帧能不能用来切角标/列表区域。"""
    sh = getattr(im, 'shape', None)
    if not sh or len(sh) < 2:
        return False
    return sh[0] >= MIN_H and sh[1] >= MIN_W


def _nav_sig(im):
    """角标区域的量化签名: //16 抗屏幕抗锯齿/光标闪烁带来的 1~2 级噪声。"""
    x1, y1, x2, y2 = NAV
    g = cv2.cvtColor(im[y1:y2, x1:x2], cv2.COLOR_RGB2GRAY)
    q = cv2.resize(g, (8, 8), interpolation=cv2.INTER_AREA) // 16
    return q.astype(np.uint8).tobytes()


def _nav_red(im):
    x1, y1, x2, y2 = NAV
    c = im[y1:y2, x1:x2].astype(np.int16)
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    return int(((r > 165) & (r - g > 70) & (r - b > 70) &
                (g < 155) & (b < 155)).sum())


def _row_sigs(im):
    """把会话列表按 68px 行高切成一组 16x8 的量化灰度签名(兜底定位用)。"""
    g = cv2.cvtColor(im[:, LIST_X1:LIST_X2], cv2.COLOR_RGB2GRAY)
    out = []
    for i in range(N_ROWS):
        cy = FIRST_ROW_Y + i * ROW_H
        a = g[max(0, cy - 30):cy + 30, :]
        if a.size == 0:
            break
        q = cv2.resize(a, (16, 8), interpolation=cv2.INTER_AREA) // 16
        out.append(q.astype(np.uint8))
    return out


def _chat_windows(hwnd):
    return [int(x) for x in subwin.sub_windows(hwnd)
            if win._title(x) not in SKIP_TITLES and u.IsWindowVisible(x)]


def _sub_sigs(hwnd):
    """给每个已存在的后台独立窗截一张 32x32 量化灰度签名。"""
    out = {}
    for x in _chat_windows(hwnd):
        try:
            im = win.grab(x)
            g = cv2.cvtColor(im, cv2.COLOR_RGB2GRAY)
            out[x] = (cv2.resize(g, (32, 32), interpolation=cv2.INTER_AREA)
                      // 16).astype(np.uint8)
        except Exception:                                      # noqa: BLE001
            continue
    return out


def _diff(a, b):
    if a is None or b is None:
        return 0.0                 # 新冒出来的窗不算「收到消息」, 忽略
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


class Watcher:
    """维护「当前状态」, 每次 poll 只做 diff。"""

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self._bad = 0                # 连续拿到不可用帧的次数
        im = win.grab(hwnd)
        if not _usable(im):
            raise ValueError(f'窗口不可用或尺寸异常: {_shape(im)}')
        self.nav = _nav_sig(im)
        self.rows = _row_sigs(im)
        self.subs = _sub_sigs(hwnd)
        print('[快通道] 基线: 主窗角标 + 会话列表 + %d 个后台独立窗'
              % len(self.subs))

    def poll(self):
        im = win.grab(self.hwnd)
        if not _usable(im):
            # 这一帧不可用(最小化/折叠/重绘中): 直接当"没动静", 退避后重试。
            # 以前这里放任 cv2 抛异常, 上层每 2s 打一条, 日志直接涨到几十 MB。
            self._bad += 1
            if self._bad in (1, 60, 600):
                print(f'[快通道] 主窗画面不可用({_shape(im)}), 已连续 {self._bad} 次; '
                      '微信最小化时就是这样, 恢复窗口会自动接上')
            time.sleep(min(1.0, 0.02 * self._bad))
            return None
        if self._bad:
            print(f'[快通道] 主窗画面恢复(此前连续 {self._bad} 次不可用)')
            self._bad = 0
        nav, rows = _nav_sig(im), _row_sigs(im)
        # 数字变了且角标仍然是红的才算「来新消息」; 未读被读掉(红块消失)不触发
        changed = (nav != self.nav) and _nav_red(im) > 20
        old = self.rows
        self.nav, self.rows = nav, rows      # 先吃掉这一帧, 避免同一变化反复触发
        if not changed:
            return None

        cur = _sub_sigs(self.hwnd)           # 只在角标变化时才截后台窗, 平时零额外开销
        best, target = 0.0, None
        for h, sig in cur.items():
            d = _diff(self.subs.get(h), sig)
            if d > best:
                best, target = d, h
        self.subs = cur

        diffs = [_diff(a, b) for a, b in zip(old, rows)]
        cand = [i for i, d in enumerate(diffs) if d >= ROW_DIFF_TH]
        row_y = FIRST_ROW_Y + (cand[0] if cand else 0) * ROW_H
        return {'row_y': row_y, 'sub': target if best >= SUB_DIFF_TH else None,
                'sub_diff': best,
                'title': win._title(target) if target else ''}


def fire(hwnd, info, text, home=None):
    """对已存在的后台独立窗直接 PostMessage; 没有窗才退化为点列表行 + 主窗投递。"""
    sub = info.get('sub')
    if sub and u.IsWindow(sub):
        s = subwin.send_silent(sub, text, delay=0.006)
        return {'ok': bool(s.get('ok')), 'title': info.get('title') or win._title(sub),
                'mode': '复用后台独立窗', 'elapsed_ms': s.get('elapsed_ms'),
                'deviation': s.get('deviation'), 'occupied_ms': s.get('occupied_ms'),
                'reason': s.get('reason') or 'ok'}

    # 兜底: 这个会话还没开过独立窗。用 PostMessage 点列表行切会话 + 主窗口投递,
    # 全程不新建子窗、不激活微信(fg_guard 会把前台还给 home)。
    with win.fg_guard(home=home) as g:
        win._post_click(hwnd, win.CLICK_X, int(info.get('row_y') or FIRST_ROW_Y))
        time.sleep(0.15)
        r = win.send_text(hwnd, text, home=home, delay=0.02)
    return {'ok': bool(r.get('ok')), 'title': '(主窗口会话)',
            'mode': '主窗口投递', 'steal_ms': g.steal_ms,
            'reason': r.get('reason') or 'ok'}
