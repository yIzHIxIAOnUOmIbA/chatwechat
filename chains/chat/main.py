# -*- coding: utf-8 -*-
"""微信自动回复主循环
流程: 解密消息库 -> 增量查对方消息 -> 白名单对比(备注前8位) -> 静默窗口聚合
      -> qwen 生成回复 -> 独立聊天窗(没有就双击会话首行建一个) -> 投字 + 回车
只有独立窗那条路整体失败时才回退老路(激活 + ctrl+F + 剪贴板 + 真实回车)。
"""
import sys, io, os, json, re, time, threading, sqlite3, ctypes

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)

# core/ 是大小号共用的底层(首次话术/静默投递/账号指纹), 与 chains/chat 差两级目录
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config
import wx_db
import ai_reply
import sender
from core import first_reply as freply
from core import pidfile
from core import unread_ui

UI_FAST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'ui_fast.json')


def _ui_fast_take(un, remark, remark8):
    """取出并消费快通道对该会话的回执, 返回它发出固定话术的时刻(没有则 0)。

    快通道只做像素 diff、不读消息库, 所以它拿不到「这次回了几条」, 回执里
    只有时间戳。主循环 8s 后才看到这批消息, 用 create_time 和该时间戳比:
    对方发送时间早于快通道回复时刻的, 就是固定话术已经覆盖的那几条, 从队列
    头部按条扣掉; 之后(晚于它)的追问才交给 AI。

    不能用「ts - 0.5s」这种余量: 消息库的 create_time 只有秒级精度, 实测两条
    相隔 2s 的消息存成 1793881077 / 1793881079, 加余量会把追问一起吞掉。
    这里用严格小于 ts, 秒级截断的追问最多掉 1 秒也不会被判成已覆盖。

    只消费一次(取完清空回执), 保证同一批消息不会被跳过第二遍。
    60s TTL: 快通道回过但消息始终没入库(被撤回等)时不让它一直挂着。
    """
    d = load_json(UI_FAST_FILE, {})
    ts = float(d.get('ts') or 0)
    if not ts or time.time() - ts > 60:
        return 0.0
    hit = bool(un and d.get('un') and str(d.get('un')) == str(un))
    if not hit:
        norm = lambda s: re.sub(r'\s+', '', str(s or ''))
        title = norm(d.get('title'))
        a, b = norm(remark), norm(remark8)
        hit = bool(title) and bool(
            (a and (title in a or a in title)) or
            (b and (title in b or b in title)))
    if not hit:
        return 0.0
    save_json(UI_FAST_FILE, {})
    return ts


def _ui_fast_match(remarks, title):
    """窗口标题栏的联系人名是否命中白名单。

    微信会把标题截断(如 '示例会话...'), 备注是 '示例备注', 所以用
    互相包含而不是全等。命中返回 username, 没命中返回 None(不回, 不做无差别回复)。
    两道防误伤: 全等优先; 标题短于 6 个字符时不做包含匹配(否则 '10242' 这种
    截断串会命中好几个 10242xxx 的人); 多个候选都包含时取前缀重叠最长的。
    """
    norm = lambda s: re.sub(r'\s+', '', str(s or ''))
    t = norm(title)
    if not t:
        return None
    best = best_ov = None
    for un, pair in (remarks or {}).items():
        for cand in (pair or ()):
            c = norm(cand)
            if not c:
                continue
            if t == c:
                return un
            if len(t) < 6 or not (t in c or c in t):
                continue
            ov = len(os.path.commonprefix([t, c]))
            if best_ov is None or ov > best_ov:
                best, best_ov = un, ov
    return best


def _ui_fast_loop(remarks, state, lock):
    """红点数字一变 -> 找画面变化的后台聊天窗 -> 读标题查白名单 -> 只回首次话术。

    独立 daemon 线程, 与数据库主循环并行; 启动时截一次基线(此时没有新消息),
    之后只做像素 diff, 全程不读消息库、不激活主窗口、不建窗、不双击。
    只对每个会话的第一条消息发固定话术: 首次判定与主循环共用 state 里的
    _first_reply, 发过就不再发, 后续消息留给数据库链路走 AI 回复。
    """
    hwnd = None
    for _ in range(60):
        try:
            hwnd = sender._quiet_main(None)
        except Exception:                                      # noqa: BLE001
            hwnd = None
        if hwnd:
            break
        time.sleep(0.25)
    if not hwnd:
        print('[快通道] 找不到大号主窗口, 退出')
        return
    try:
        text = freply.build(config.USER_CFG)
    except Exception as e:                                     # noqa: BLE001
        print(f'[快通道] 话术构建失败({e}), 用纯文本兜底')
        text = ''
    # 抓基线截图可能拿到空帧(微信刚启动/最小化/正在重绘, OpenCV 直接抛
    # !_src.empty()), 以前一次失败就 return, 快通道整轮降级成数据库链路
    # (首条固定话术也就废了)。这里一直重试: 窗口最小化时 PrintWindow 拿不到
    # 任何画面, 只有用户把微信恢复出来才能救回来, 不能试几次就永久放弃。
    w = None
    attempt = 0
    while w is None:
        attempt += 1
        try:
            hwnd = sender._quiet_main(None) or hwnd
            w = unread_ui.Watcher(hwnd)
        except Exception as e:                                 # noqa: BLE001
            if attempt == 1 or attempt % 15 == 0:
                print(f'[快通道] 初始化失败(第{attempt}次): '
                      f'{type(e).__name__}: {str(e)[:70]} '
                      f'| 微信最小化时拿不到画面, 把窗口恢复出来即可自动接上')
            time.sleep(3.0)
    print(f'[快通道] UI 未读监听就绪(基线已缓存, 第{attempt}次尝试成功)')
    bad = 0
    while True:
        try:
            info = w.poll()
            bad = 0
            if info is None:
                time.sleep(unread_ui.POLL_INTERVAL)
                continue
            t0 = time.time()
            title = info.get('title') or ''
            sub = info.get('sub')
            if not sub:
                print('[快通道] 跳过: 定位不到后台聊天窗(diff=%.2f)'
                      % (info.get('sub_diff') or 0))
                continue
            un = _ui_fast_match(remarks, title)
            if not un:
                print(f'[快通道] 跳过: 窗口标题 {title!r} 不在白名单')
                continue
            with lock:
                first_ok = freply.should_send(state, un, config.USER_CFG)
            if not first_ok:
                print(f'[快通道] 跳过: {title!r} 已发过首次话术, 交给 AI 回复')
                continue
            home = sender.cwin.pick_home_window(exclude=hwnd)
            r = unread_ui.fire(hwnd, info, text, home=home)
            dt = time.time() - t0
            if r.get('ok'):
                with lock:
                    freply.mark(state, un)          # 记下已发, 之后交给 AI
                    save_json(config.STATE, state)
                save_json(UI_FAST_FILE, {'ts': time.time(), 'title': r['title'],
                                         'un': un})
                print(f"[快通道] 命中 {r['title']!r} 方式={r.get('mode')} "
                      f"行y={info.get('row_y')} 窗diff={info.get('sub_diff'):.1f} "
                      f"用时 {dt:.3f}s 偏离 {r.get('deviation')}次 "
                      f"占用 {r.get('occupied_ms')}ms")
            else:
                print(f"[快通道] 未发出: {r.get('reason')}")
        except Exception as e:                                 # noqa: BLE001
            # 限流打印: 这里以前每 2s 一条, 一晚上刷了 30 万行 / 55MB 日志。
            # 只在前 3 次、之后每 300 次(约 10 分钟)打一条, 并逐步退避。
            bad += 1
            if bad <= 3 or bad % 300 == 0:
                print(f'[快通道] 异常 第{bad}次 {type(e).__name__}: {str(e)[:120]}')
            time.sleep(min(2.0, 0.2 * bad))


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def send_keep_focus(remark, text, prefix=None, expect_text=None):
    """发送消息, 全程不抢前台; 发不出去就返回 False, 由下一轮重试。

    只走 send_to_quiet: 复用独立聊天窗 + PostMessage 投文字(0 占用)。
    原来的兜底 send_to(激活 + ctrl+F + 剪贴板 + 真实按键)已删除 —— 它要占用
    前台 3~5s, 用户那边的体感就是"屏幕乱跳、正在打的字被抢走"。
    宁可这一条下一轮再发, 也不抢一次前台。

    prefix=None 走默认的 [AI生成]; 首次话术传 ''(它自己带 [自动回复])。
    """
    u = ctypes.windll.user32
    prev = u.GetForegroundWindow()
    ok = False
    try:
        ok = bool(sender.send_to_quiet(remark, text, home=prev, prefix=prefix,
                                expect_text=expect_text))
        if not ok:
            if getattr(sender, 'last_fail_hard', False):
                print('  [发送] 静默失败且不宜重试(分不清账号 / 可能已发出), 本条不发')
            else:
                print('  [发送] 静默链路没成功, 本条不发(不抢前台), 等下一轮重试')
    except Exception as e:                                      # noqa: BLE001
        print(f'  [发送] 异常({type(e).__name__}): {e}')
    finally:
        if prev and u.IsWindow(prev) and u.GetForegroundWindow() != prev:
            try:
                u.SetForegroundWindow(prev)
            except Exception:                                   # noqa: BLE001
                pass
    return ok


def main():
    # 给控制台留名片: 命令行里只有 "main.py", 外面认不出这是大号还是小号
    pidfile.claim(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'main.pid'))
    print('[main] 启动. 回复引擎: 本地知识库 + 联网搜索(带相关性校验)'
          + (' + 本地大模型' if config.LLM_ENABLED else ' (本地大模型未启用)'))

    # 每次打开清空上次的临时知识库(每会话独立的长期设定/记住的信息不跨进程保留)
    ai_reply.cleanup_sessions()

    # 先校准模型: 配置里写了 8b 但没下完/被删时, 自动降级到候选表里能用的那个,
    # 否则每条兜底消息都要 404 一次再白等超时。
    if config.LLM_ENABLED:
        config.ensure_llm_model()

    # 后台预热模型: 提前加载进显存(ollama 冷启动约 20s), 否则第一条消息要干等。
    # 放守护线程, 加载期间 bot 照常监听; 加载完的消息自然就快了。
    if config.LLM_ENABLED and config.LLM_WARMUP:
        threading.Thread(target=ai_reply.warmup, daemon=True).start()
        print(f'[预热] 后台加载模型 {config.LLM_MODEL} 中(不阻塞监听)...')

    # 白名单: whitelist.json 为基底(用户直接维护), config.json 的 whitelist 字段
    # 可覆盖同名/补充新项。两者都生效, 改任一处都行。
    whitelist = load_json(config.WHITELIST, {}) or {}
    whitelist.update(config.USER_CFG.get('whitelist') or {})
    print(f'[main] 白名单: {whitelist}')
    state = load_json(config.STATE, {})
    state_lock = threading.Lock()   # 快通道与主循环共用同一份 state
    # 一次性加载白名单用户映射: username -> (remark, remark8), 前置过滤
    remarks = wx_db.load_whitelist_remarks(whitelist)
    # 注意: 原写法把 or 挂在整个 f-string 拼接之后(+ 优先级高于 or), 白名单为空时
    # 永远走不到 '(无)' 分支。这里先把列表拼好再判空。
    _r8_list = ', '.join(str(r8) for r8, _ in remarks.values())
    print(f'[监听] 白名单命中 {len(remarks)} 个会话: '
          + (_r8_list or '(无)'))

    pending = {}        # username -> {remark8, remark, msgs:[] , last_ts}
    chat_sessions = {}  # username -> ai_reply 会话(每人一个, 记忆隔离)
    last_sig = None     # 数据库文件指纹, 变化才解密
    last_decrypt = 0.0  # 上次解密时间, 用于节流
    sending = False     # 全局发送互斥: 正在回复(生成+发送)时, 新触发的消息顺延, 防多窗口操作撞车
    cold_start = True   # 本进程还没做过冷启动快照(第一轮扫描只钉水位、不回复)

    # bot 自己发出去的文本前缀: AI 回复 + 首次固定话术。判定"用户是否人工
    # 插话"时要把这些排除掉, 否则快通道刚发的话术会让追问被当成"已回复"吞掉。
    _fr_prefix = freply.settings(config.USER_CFG).get('prefix') or ''
    SELF_PREFIXES = tuple(x for x in (config.AI_PREFIX, _fr_prefix) if x)

    # UI 未读快通道: 后台线程, 只看红点不读库, 把 8s 入库延迟抹平
    threading.Thread(target=_ui_fast_loop, args=(remarks, state, state_lock),
                     daemon=True).start()

    while True:
        # ---- 控制台"重置首发记录": 清内存 + 落盘, 不重启也能重测首次话术 ----
        if os.path.exists(config.RESET_FIRST_FLAG):
            try:
                os.remove(config.RESET_FIRST_FLAG)
            except OSError:
                pass
            n = len(state.get(freply.STATE_KEY) or {})
            if state.pop(freply.STATE_KEY, None) is not None:
                save_json(config.STATE, state)
            print(f'[状态] 已重置首发记录({n} 条), 这些会话再发消息会重新收到固定话术')

        # ---- 触发式: 文件指纹变化才解密查询, 空闲零开销 ----
        sig = wx_db.db_signature()
        if sig != last_sig and time.time() - last_decrypt >= config.MIN_DECRYPT_INTERVAL:
            last_decrypt = time.time()
            # 必须记住本轮指纹: 否则 sig != last_sig 恒为真, 每轮(0.15s)都会
            # 全量解密 26MB 主库, CPU 长期打满、无用 IO 不停。
            last_sig = sig
            try:
                conn = wx_db.decrypt()
            except Exception as e:
                print(f'[main] 解密失败: {e}, 重试...')
                time.sleep(1)
                continue
            try:
                for un, table in wx_db.whitelist_tables(conn, remarks):
                    remark, remark8 = remarks[un]
                    if cold_start:
                        # 冷启动快照: 现存会话一律钉到最新一条, 停机期间积累的未读不回
                        # (防重启轰炸)。这一轮不做任何回复。
                        top = wx_db.max_local_id(conn, table)
                        if state.get(un) != top:
                            state[un] = top
                            save_json(config.STATE, state)
                        continue
                    if un not in state:
                        # 启动之后才出现的会话: 微信是对方发消息才建表, 所以这条新消息
                        # 就是"第一条消息", 从 0 起算正常回复 —— 不能当历史吞掉。
                        # 全量白名单里绝大多数人从没聊过, 全靠这个分支才不会漏回。
                        state[un] = 0
                        save_json(config.STATE, state)
                    last_id = state[un]
                    raw = wx_db.new_messages(conn, table, last_id, un)
                    if not raw:
                        continue
                    # 兜底闸门: 带机器人前缀的必然是本号/对端 bot 刚发出去的回复,
                    # 绝不再当输入(否则两个号会互相刷)。
                    # 整批都被过滤时也要推进水位, 否则会反复解密同一批却永远不回。
                    msgs = [m for m in raw
                            if not str(m.get('text', '')).strip().startswith(
                                config.BOT_PREFIXES)]
                    top_id = max(m['local_id'] for m in raw)   # 水位永远推到头
                    if not msgs:
                        state[un] = top_id
                        save_json(config.STATE, state)
                        print(f"[监听] {remark8} 忽略 bot 发言(前缀命中), 只推进水位")
                        continue
                    # 快通道刚用固定话术回过这批里最早的几条: 回执只消费一次,
                    # 按条剪掉已被话术覆盖的部分, 剩下的追问才交给 AI。
                    # 必须在 self_has_replied 之前消费 —— 否则对方追问那轮会拿着
                    # 上一轮留下的旧回执, 把追问也当成"已经回过了"。
                    fast_ts = _ui_fast_take(un, remark, remark8)
                    if fast_ts:
                        covered = 0
                        for m in msgs:
                            if float(m.get('time') or 0) < fast_ts:
                                covered += 1
                            else:
                                break
                        covered = max(1, covered)     # 至少覆盖触发它的那条
                        msgs = msgs[covered:]
                        if not msgs:
                            state[un] = top_id
                            save_json(config.STATE, state)
                            print(f"[监听] {remark8} 已由 UI 快通道回固定话术, 只推进水位")
                            continue
                        print(f"[快通道] {remark8} 固定话术之后又有 {len(msgs)} 条追问, 走 AI")
                    if last_id > 0 and wx_db.self_has_replied(
                            conn, table, last_id, un, SELF_PREFIXES):
                        # 用户已人工回复过(自己消息在对方最新消息之后): 只标记已读, 跳过 AI
                        state[un] = top_id
                        save_json(config.STATE, state)
                        print(f"[监听] {remark8} {remark} 用户已人工回复, 跳过AI")
                        continue
                    p = pending.setdefault(un, {
                        'remark': remark, 'remark8': remark8,
                        'msgs': [], 'last_ts': 0,
                    })
                    p['msgs'].extend(msgs)
                    p['last_ts'] = time.time()
                    # 立即标记已读, 防重复
                    state[un] = top_id
                    print(f"[监听] {remark8} {remark} 新消息: "
                          + ' | '.join(m['text'][:40] for m in msgs))
            finally:
                conn.close()
            cold_start = False      # 快照这轮走完, 后面才是真正的"新消息"

        # ---- 聚合: 静默超过 QUIET_WINDOW 则触发回复(不依赖解密) ----
        now = time.time()
        done = []
        for un, p in pending.items():
            if now - p['last_ts'] >= config.QUIET_WINDOW and p['msgs']:
                done.append(un)

        for un in done:
            p = pending.pop(un)
            if sending:
                # 全局互斥: 上一轮回复(生成+发送)尚未完成, 本轮不开启新的 AI/发送,
                # 消息放回队列顺延, 防止两个会话同时操作微信窗口互相干扰
                print(f"[发送] 上一轮回复未完成, {p['remark8']} 的消息顺延到下轮")
                p['last_ts'] = time.time()
                pending[un] = p
                continue
            sending = True
            try:
                msgs = p['msgs']
                texts = [m['text'] for m in msgs if m['type'] == 'text']
                # 本轮无文字消息(全是图片/语音/表情): bot 感知不到内容, 固定话术不瞎回
                if not texts:
                    last = msgs[-1]
                    if last['type'] == 'emoji':
                        # 表情/贴图: 静默已读, 不打扰
                        state[un] = max(m['local_id'] for m in msgs)
                        save_json(config.STATE, state)
                        print(f"  [回复] {p['remark8']} 发来表情, 静默已读")
                        continue
                    reply = config.MEDIA_REPLIES.get(last['type'],
                                                     config.MEDIA_REPLIES['media'])
                    print(f"  [回复] {p['remark8']} 发来{last['type']}, 固定话术: {reply}")
                    ok = send_keep_focus(p['remark'], reply,
                                         expect_text=(texts[0] if texts else None))
                    if ok:
                        print(f"  [回复] 已发送到 {p['remark8']}")
                        state[un] = max(m['local_id'] for m in msgs)
                        save_json(config.STATE, state)
                    else:
                        print(f"  [回复] 发送失败, 消息重新入队待重试")
                        p['last_ts'] = time.time()
                        pending[un] = p
                    continue
                # 首次话术: 这个人第一次来消息 -> 先回一段明确答复(等多久/怎么继续),
                # 等他追问了才交给 AI。文案/前缀/生效对象都在 config.json 的
                # first_reply 里, 判定记录在 state 的 _first_reply 下, 与消息库无关
                # (微信重启、清历史都不会让它对同一个人再发一遍)。
                if (freply.enabled_for(config.USER_CFG, un=un, remark=p['remark'],
                                       remark8=p['remark8'])
                        and freply.should_send(state, un, config.USER_CFG)):
                    fr_text = freply.build(config.USER_CFG)
                    print(f"  [首次] {p['remark8']} 第一句话术: {fr_text!r}")
                    ok = send_keep_focus(p['remark'], fr_text, prefix='')
                    if ok:
                        freply.mark(state, un)
                        state[un] = max(m['local_id'] for m in msgs)
                        save_json(config.STATE, state)
                        print(f"  [首次] 已发送到 {p['remark8']}, 等他追问再进 AI")
                    else:
                        # 不记"已发": 下一条消息会重试, 但不能无限重试 ——
                        # 旧写法失败就把 last_ts 归零、1.8s 后原样再试, 遇到
                        # "这一行双击建不出窗"就是死循环: 每 1.8s 抢一次前台、
                        # 弹一次窗, 用户看到的就是"这程序一直在点我电脑"。
                        #
                        # 2026-11-11: 先分清"确定没发"和"可能已发"。后者(输入框未清空)
                        # 实测消息已在库里(status=2), 重试就是重复发 —— 直接记成已发。
                        if getattr(sender, 'last_fail_kind', '') == 'maybe_sent':
                            print(f"  [首次] {p['remark8']} 可能已发出(输入框未清空), "
                                  '按已送达处理, 不重发')
                            freply.mark(state, un)
                            state[un] = max(m['local_id'] for m in msgs)
                            save_json(config.STATE, state)
                            pending.pop(un, None)
                            continue
                        n = int(p.get('fr_fail') or 0) + 1
                        p['fr_fail'] = n
                        if n >= 4:
                            print(f'  [首次] {p["remark8"]} 连试 {n} 次发不出去, '
                                  '放弃这条(不再反复建窗抢前台)')
                            freply.mark(state, un)          # 记成已发, 别再骚扰
                            state[un] = max(m['local_id'] for m in msgs)
                            save_json(config.STATE, state)
                            pending.pop(un, None)
                        else:
                            print(f'  [首次] 发送失败({n}/3), 退避后重试')
                            p['last_ts'] = time.time() + 2.0 * n
                            pending[un] = p
                    continue
                # 上下文: 每轮都从库里取最近N条(含自己发的)合并成一段背景。
                # 原来只在建会话时注入一次, 进程一重启历史就断片; 改成每轮读库,
                # 记忆跟着微信走, 重启/多会话都不会丢。
                # 上一轮没发出去的那条回复: 只重发这一段, 绝不重新问模型。
                # 旧写法失败后原样入队, state 没前进, 下一轮把同一条消息当新消息
                # 再触发一次模型 —— 模型看到自己刚说过的话就换个说法, 对方手机
                # 上会陆续冒出好几条内容不同的回复(实测对一句"？"连发 3 条)。
                retry = p.get('retry_text')
                if retry:
                    reply = retry
                    print(f"[回复] {p['remark8']} 重发上轮未发出的回复"
                          f"(第{int(p.get('retry_n') or 0) + 1}次, 不重新问模型): "
                          f"{str(reply)[:40]}")
                else:
                    # 上下文: 每轮都从库里取最近N条(含自己发的)合并成一段背景。
                    # 原来只在建会话时注入一次, 进程一重启历史就断片; 改成每轮读库,
                    # 记忆跟着微信走, 重启/多会话都不会丢。
                    history = _load_history(un, config.DB_CONTEXT)
                    sess = chat_sessions.get(un)
                    if sess is None:
                        print(f"[会话] 为 {p['remark8']} {p['remark']} 创建独立会话"
                              f"(注入系统提示词 + 最近{len(history)}条库内上下文)")
                        sess = ai_reply.new_session(p['remark'], history)
                        chat_sessions[un] = sess
                    elif history:
                        print(f"[会话] {p['remark8']} 注入库内最近{len(history)}条上下文")
                    print(f"[回复] {p['remark8']} {p['remark']} 触发, 新消息{len(texts)}条")
                    # 直通模型: 不做本地计算/知识库/诗词/短篇名等枚举路由
                    print('[回复] 直接交模型:')
                    reply = ai_reply.chat(sess, texts, context=history)
                    if not reply:
                        print('  [回复] AI 无输出, 跳过本轮')
                        state[un] = max(m['local_id'] for m in msgs)
                        save_json(config.STATE, state)
                        continue
                    print(f"  [回复] AI: {reply}")
                ok = send_keep_focus(p['remark'], reply,
                                     expect_text=(texts[0] if texts else None))
                if ok:
                    print(f"  [回复] 已发送到 {p['remark8']}")
                    p.pop('retry_text', None)
                    p.pop('retry_n', None)
                    state[un] = max(m['local_id'] for m in msgs)
                    save_json(config.STATE, state)
                else:
                    # 同上: 可能已发出(输入框未清空)时绝不重试, 直接推水位。
                    if getattr(sender, 'last_fail_kind', '') == 'maybe_sent':
                        print(f"  [回复] {p['remark8']} 可能已发出(输入框未清空), "
                              '按已送达处理, 不重发')
                        p.pop('retry_text', None)
                        p.pop('retry_n', None)
                        state[un] = max(m['local_id'] for m in msgs)
                        save_json(config.STATE, state)
                        continue
                    n = int(p.get('retry_n') or 0) + 1
                    p['retry_n'] = n
                    p['retry_text'] = reply          # 下轮重发这一段, 不重新生成
                    if n >= 3:
                        print(f"  [回复] {p['remark8']} 连试 {n} 次发不出去, "
                              '放弃这条(不再反复重试同一句)')
                        p.pop('retry_text', None)
                        p.pop('retry_n', None)
                        state[un] = max(m['local_id'] for m in msgs)
                        save_json(config.STATE, state)
                        pending.pop(un, None)
                        continue
                    print(f"  [回复] 发送失败({n}/3), 消息重新入队待重试")
                    p['last_ts'] = time.time()
                    pending[un] = p
            finally:
                sending = False

        time.sleep(config.POLL_INTERVAL)


def _load_history(un, limit):
    """读取该会话最近对话(含双方), 返回 [(who, text)]"""
    try:
        # 直接复用上一轮已解密好的 MSG_DEC: 这里再调一次 wx_db.decrypt() 等于
        # 每触发一次回复就重新全量解密 26MB, 纯属浪费(该文件每轮轮询都已刷新)
        conn = sqlite3.connect(config.MSG_DEC)
        conn.text_factory = str
        table = wx_db.msg_table(un)
        # sid 是 Name2Id.rowid, 不是"1=自己"的布尔值; 按会话动态解析,
        # 否则历史里的发送方全是反的, 模型会开始自言自语。
        peer = wx_db.peer_rowid(conn, un)
        me = wx_db.self_rowid(conn)
        # 时间窗: 只取最近 DB_CONTEXT_MINUTES 分钟。只按条数取最近 N 条时,
        # 隔天再聊会把这几天前的旧话题当成本轮语境, 模型就会答非所问
        # (实测: 对方问"语文做哪些啊", AI 回"没有那些英语翻译资料")。
        since = int(time.time()) - config.DB_CONTEXT_MINUTES * 60
        rows = conn.execute(
            f"SELECT real_sender_id, message_content FROM '{table}' "
            f"WHERE (local_type & 4294967295) = 1 AND create_time >= ? "
            f"ORDER BY local_id DESC LIMIT ?",
            (since, limit)).fetchall()
        conn.close()
        out = []
        for sid, content in reversed(rows):
            if sid not in (peer, me):
                continue
            text = wx_db.extract_text(content)
            if not text or text.startswith('<'):     # appmsg XML 不是聊天内容
                continue
            out.append(('对方' if sid == peer else '用户', text))
        return out
    except Exception as e:
        print(f'  [history] 读取失败: {e}')
        return []


if __name__ == '__main__':
    main()

