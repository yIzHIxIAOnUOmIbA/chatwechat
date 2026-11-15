# -*- coding: utf-8 -*-
"""imhere 主程序: 监听唯一会话「示例联系人」, 交给桌面助手处理后回发

数据流:
  新账号库(message_0.db, 只读解密)  ->  目标会话新消息
     -> 对方连发时等安静 QUIET_WINDOW 秒合并成一条
     -> agent.ask():
          决策循环: 每轮模型自己判断「继续思考 / 调用工具 / 交付」(收尾标记)
          进度话术走事件通道(默认只打印, 开关在 config.SEND_INTERIM)
          最终正文才交给 sender 发出去
     -> sender.send_to(): 激活 + OCR 置信目标窗口 + 固定词搜索 + 发送

用法:
  python main.py             # 常驻监听
  python main.py --once      # 跑一轮就退出(调试)
  python main.py --dry       # 只算不发, 不碰微信窗口(离线验证)
  python main.py --probe     # 只做一次窗口识别自检
"""
import argparse
import io
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
# core/ 是大小号共用的底层(首次话术/静默投递/账号指纹), 与 chains/agent 差两级目录
_ROOT = os.path.dirname(os.path.dirname(BASE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace', line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8',
                              errors='replace', line_buffering=True)

import agent
import config
import group_chat
import sender
import wx_db
from core import first_reply as freply
from core import pidfile


def load_state():
    try:
        with open(config.STATE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = config.STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config.STATE)


def table_top(conn, table):
    """表里最大的 local_id —— 用来判断存下来的水位还有没有意义"""
    row = conn.execute(f"SELECT MAX(local_id) FROM '{table}'").fetchone()
    return row[0] or 0


def first_scan(conn, table):
    """首扫水位: 回补最近 FIRST_REPLY_MAX 条对方消息(0 则表示从现在开始)"""
    top = table_top(conn, table)
    if config.FIRST_REPLY_MAX <= 0:
        return top
    rows = conn.execute(
        f"SELECT local_id FROM '{table}' WHERE real_sender_id = ? "
        f"ORDER BY local_id DESC LIMIT ?",
        (wx_db.peer_rowid(conn), config.FIRST_REPLY_MAX)).fetchall()
    if not rows:
        return top
    return min(r[0] for r in rows) - 1


def banner(username, name):
    print('=' * 62)
    print(f'  imhere 一对一桌面助手')
    print(f'  账号     : {config.WX_ACCOUNT}  ({os.path.basename(config.WX_ROOT)})')
    print(f'  会话     : {name}  ({username})')
    print(f'  窗口识别 : OCR 命中 {config.OCR_MARKER!r} 才认定目标微信窗口')
    # 搜索词和窗口识别特征词是两回事(见 config.SEARCH_WORD 的注释): 一个负责把会话
    # 捞到最前, 一个负责确认这就是目标会话。大脑那行要能看出主备, 否则排查时
    # 分不清这一轮是谁答的。
    brain = (f'{config.CLOUD_MODEL}(云端) -> 兜底 {config.LLM_MODEL}'
             if config.CLOUD_ENABLE else config.LLM_MODEL)
    print(f'  搜索词   : {config.SEARCH_WORD}   |  大脑: {brain}')
    print('=' * 62)


def probe():
    """窗口识别自检: 现在能不能置信地找到目标微信窗口。

    用 _account_hwnd 而不是 _activate: 后者会把微信拉到前台做 OCR 确认,
    只是自检没必要打断用户(旧路由就是这样一步步把前台抢走的)。
    """
    print('[probe] 正在按账号目录判定目标微信窗口...')
    hwnd, why = sender._account_hwnd()
    if not hwnd:
        print(f'[probe] 失败: {why or "没有候选窗口"}')
        return False
    print(f'[probe] 成功: hwnd={hwnd} 已置信为目标窗口')
    try:
        print(f'[probe] 标题 = {sender._win_title(hwnd)!r}')
    except Exception:
        pass
    return True


HEAL_LOG = os.path.join(config.BASE, 'heal.json')
_HEAL_WINDOW = 600      # 统计窗口(秒)
_HEAL_MAX = 2           # 窗口内最多自愈重启这么多次, 再超就判定为外部原因, 不重启了


def _recent_heals():
    """最近 _HEAL_WINDOW 秒内自愈重启过几次。"""
    try:
        with open(HEAL_LOG, encoding='utf-8') as f:
            hist = json.load(f)
        return [t for t in hist if time.time() - t < _HEAL_WINDOW]
    except Exception:                                       # noqa: BLE001
        return []


def _note_heal():
    hist = _recent_heals() + [time.time()]
    try:
        with open(HEAL_LOG, 'w', encoding='utf-8') as f:
            json.dump(hist[-5:], f)
    except OSError as e:
        print(f'[main] 记录自愈重启失败: {e}')


def restart_self():
    """把自己重启一遍 —— 发送链路怎么救都救不回来时的最后一招。

    为什么"重启"是正解而不是继续重试: 实测 10-08 17:31 那次, "把微信抢到前台" 20 秒内
    连续失败 4 次(发图 1 + 文字 1 + 补发 3), 回复只能进 unsent.log; 而 18:32 手动重启
    进程之后, 同一条指令 12.6 秒就发完了 —— 卡住的是进程内的窗口/前台状态, 重启是
    唯一有效的解法。

    用 os.execv 原地替换进程: PID 不变, 不会出现新旧两个实例同时抢微信窗口(那样两边
    会互相把前台抢走, 谁也发不出去); 被 guard.py 拉起时也仍是它的子进程, 看门狗继续等。
    execv 万一失败就退出, 交给 guard.py 3 秒后拉起 —— 两条路都能把服务恢复起来,
    不会停在"死了"的状态。
    """
    print('[main] 重启自己(发送链路连续失败, 进程内状态已不可信)...')
    # 熔断: 反复重启说明根因不在进程里, 而在外面 —— 最典型的就是"微信被关掉了/退登录了",
    # 那种情况下重启多少次都没用, 只会把服务反复打断(每次重启都要重新解密库 + 重新首扫),
    # 反而不如老老实实按原来的节奏重试、等微信回来。所以窗口内重启超限就收手。
    hist = _recent_heals()
    if len(hist) >= _HEAL_MAX:
        print(f'[main] {_HEAL_WINDOW // 60} 分钟内已自愈重启 {len(hist)} 次仍未恢复, '
              f'判定为外部原因(微信没开/没登录?), 本轮不再重启, 继续按原节奏重试')
        sender._heal_backoff()
        return
    _note_heal()
    try:
        sys.stdout.flush()
    except Exception:                                       # noqa: BLE001
        pass
    try:
        os.execv(sys.executable, [sys.executable, '-u', 'main.py'] + sys.argv[1:])
    except Exception as e:                                  # noqa: BLE001
        print(f'[main] execv 重启失败({type(e).__name__}: {e}), 退出等看门狗拉起')
        sys.exit(0)


def _is_bot_text(text):
    """这条消息是不是某个 bot 的自动发言(含本号/大号/群里的其他机器人)。

    判定只看前缀: 前缀是发送方自己加的, 比"猜测语义"稳定得多。
    前缀表在 config.BOT_PREFIXES, config.json 的 bot_prefixes 可覆盖。
    """
    t = str(text or '').strip()
    return bool(t) and t.startswith(getattr(config, 'BOT_PREFIXES',
                                            ('[AI]', '[AI生成]', '[自动回复]')))


def run(dry=False, once=False):
    username, name = wx_db.resolve_target()
    banner(username, name)
    table = wx_db.msg_table(username)

    state = load_state()
    key = f'last_id_{username}'
    last_id = state.get(key)
    if last_id is not None:
        # 这是"上一批已经处理完"的水位, 不是"读到哪"的水位 ——
        # 读到但没来得及回的消息, 重启后会重新读出来重放(见下面的 acked)。
        print(f'[main] 恢复水位 last_id={last_id} (已处理完的位置)')

    if not dry and config.LLM_WARMUP:
        agent.warmup()

    def interim(note):
        """临时回复(进度话术): 事件通道, 不进模型历史。

        打开 config.SEND_INTERIM 后, 对方在微信里能看到过程: 执行前"正在搜索…",
        执行后"返回结果: …"。默认关是因为发送本身要抢焦点(激活窗口 + ctrl+F 搜索
        会话 + 粘贴), 万一同一次键鼠操作还没做完就插一条会打断它。
        """
        if dry or not config.SEND_INTERIM:
            return
        t0 = time.time()
        try:
            ok = bool(sender.send_to(note))
        except Exception as e:
            print(f'[main] 进度话术发送失败({type(e).__name__}): {e}')
            return
        # 单条 6~8s 是进度播报的主要代价; 打出来才能一眼看出"慢是不是慢在播报上"
        print(f'[main] 进度{"已发出" if ok else "没发出去"}'
              f'({time.time() - t0:.1f}s): {note}')

    session = None
    pending = []
    last_activity = 0.0
    first_seen = 0.0       # 本批第一句的到达时刻(端到端计时的起点)
    sig = None             # 最近一次"看过"的指纹
    dirty = False          # 指纹变过、但还没解密读库 —— 有待读的东西
    last_decrypt = 0.0     # 上次"真的读完库"的时刻(节流与对账都以它为基准)
    last_sig_change = time.time()   # 上次指纹变化的时刻(只为了让"探测失效"说得清)
    conn = None

    def push(text, now):
        """收进待处理批次: 记住第一句什么时候到, 并刷新"对方还在说"的时间戳"""
        nonlocal first_seen, last_activity
        if not pending:
            first_seen = now
        pending.append(text)
        last_activity = now

    # 待重发队列: send_to 失败时把回复留在队列里, 下一轮先补发。
    # 旧版失败只打印一行 "[main] 这条回复没发出去" 就推进水位, 回复永久消失
    # (代码注释里写着"消息回队重试", 但从来没有真的回过队)。
    # 队列必须落盘: 自愈重启(见 restart_self)恰恰发生在"发送连续失败"的那一刻, 而水位
    # 在处理完当时就推进了 —— 队列只留在内存里的话, 重启就等于把最该补发的回复丢掉,
    # 成了"越需要重发的时候越会丢"。
    resend = []
    resend_path = os.path.join(config.BASE, 'resend.json')

    def save_resend():
        try:
            tmp = resend_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(resend, f, ensure_ascii=False)
            os.replace(tmp, resend_path)
        except OSError as e:
            print(f'[main] 保存待重发队列失败: {e}')

    try:
        if os.path.exists(resend_path):
            with open(resend_path, encoding='utf-8') as f:
                resend = [[str(t), int(n)] for t, n in json.load(f)]
            if resend:
                print(f'[main] 接手上次没发出去的回复 {len(resend)} 条')
    except Exception as e:                                  # noqa: BLE001
        print(f'[main] 读取待重发队列失败(忽略): {type(e).__name__}: {e}')
        resend = []

    def flush_resend():
        """补发上一轮没发出去的回复; 超 SEND_RETRY_MAX 次记 unsent.log 后告警丢弃"""
        changed = False        # 队列成员有增删才落盘, 免得每轮空转都写一次磁盘
        while resend:
            item = resend[0]
            if item[1] >= config.SEND_RETRY_MAX:
                line = f'[{time.strftime("%m-%d %H:%M:%S")}] {item[0]}\n'
                try:
                    with open(os.path.join(config.BASE, 'unsent.log'), 'a',
                              encoding='utf-8') as f:
                        f.write(line)
                except OSError as e:
                    print(f'[main] 写 unsent.log 失败: {e}')
                print(f'[main] 补发 {item[1]} 次仍失败, 已记入 unsent.log: '
                      f'{item[0][:60]!r}')
                resend.pop(0)
                changed = True
                continue
            item[1] += 1
            # 队列项 = [正文, 已试次数, 前缀?]; 第三项是群聊给每个模型自带的前缀,
            # 没有就按老行为走全局 AI_PREFIX。
            pfx = item[2] if len(item) > 2 else None
            if dry:
                print(f'[dry] 本应补发: {config.AI_PREFIX if pfx is None else pfx}{item[0]}')
                resend.pop(0)
                changed = True
                continue
            if sender.send_to(item[0], prefix=pfx):
                print(f'[main] 补发成功(第 {item[1]} 次): {item[0][:40]!r}')
                resend.pop(0)
                changed = True
            else:
                print(f'[main] 补发又失败(第 {item[1]} 次), 留到下一轮')
                break
        if changed:
            save_resend()

    while True:
        # ---- 控制台"重置首发记录": 清内存 + 落盘, 不重启也能重测首次话术 ----
        if os.path.exists(config.RESET_FIRST_FLAG):
            try:
                os.remove(config.RESET_FIRST_FLAG)
            except OSError:
                pass
            n = len(state.get(freply.STATE_KEY) or {})
            if state.pop(freply.STATE_KEY, None) is not None:
                save_state(state)
            print(f'[main] 已重置首发记录({n} 条), 这些会话再发消息会重新收到固定话术')

        now = time.time()
        if sender.SELF_HEAL:
            # 发送链路连续失败、就地清状态也没救回来 -> 重启本进程(见 sender._activate
            # 与 restart_self)。放在 flush_resend 之前: 队列已落盘, 重启后照常接手。
            sender.SELF_HEAL = False
            restart_self()
        flush_resend()
        # 这一轮是不是已经该把批次交给大脑了 —— 是的话本轮的解密先让路。
        # 解密是同步的(实测一次 1.1s), 挡在前面就是把它平白推迟一秒多, 而那只发生在
        # 消息最密、最不该慢的时刻。
        handoff_due = bool(pending) and (now - last_activity) >= config.QUIET_WINDOW
        try:
            new_sig = wx_db.db_signature()
        except Exception:
            new_sig = None
        if new_sig != sig:
            # 这里只记"看过", 不在这里解密: 解不解密由下面的节流/对账决定。
            sig = new_sig
            dirty = True
            last_sig_change = now
        elif (not pending) and (now - last_decrypt) >= config.RECONCILE_INTERVAL:
            # 对账: 与指纹无关的定时兜底, 理由见 config.RECONCILE_INTERVAL 的注释。
            print(f'[main] 对账: 指纹已 {now - last_sig_change:.0f}s 没变过, '
                  f'强制读一次库(上次真读库在 {now - last_decrypt:.0f}s 前)')
            dirty = True
        # 节流: 窗口内的多次变化合并成一次解密(理由见 config.MIN_DECRYPT_INTERVAL)。
        # dirty 只在"真的读完库"之后才清 —— 指纹"看过"不等于"读过库",
        # 清早了就是把那几条新消息永久漏掉, 而且同样毫无异常。
        if (dirty and not handoff_due
                and (now - last_decrypt) >= config.MIN_DECRYPT_INTERVAL):
            try:
                if conn is not None:
                    conn.close()
                conn = wx_db.decrypt()
            except Exception as e:
                print(f'[main] 解密失败: {e}')
                time.sleep(1.0)
                continue
            dirty = False
            last_decrypt = time.time()
            if last_id is None:
                last_id = first_scan(conn, table)
                state[key] = last_id
                save_state(state)
                print(f'[main] 首扫水位 last_id={last_id} '
                      f'(回补上限 {config.FIRST_REPLY_MAX} 条)')
            elif last_id > table_top(conn, table):
                # 存档水位比库里最大值还大 —— 库被重建/清理, 或者换过号。
                # 不检查的话从此一条消息都读不出来, 而界面上不会有任何异常提示。
                print(f'[main] 存档水位 {last_id} 超过库里最大值 '
                      f'{table_top(conn, table)}, 判定失效, 重新首扫')
                last_id = first_scan(conn, table)
                state[key] = last_id
                save_state(state)

            msgs = wx_db.new_messages(conn, table, last_id)
            if msgs:
                # 内存游标照常推进(避免同一批被反复读出来), 但这里**不落盘**:
                # 落盘只有一处 —— 见下面"处理完才推进水位"。
                last_id = max(m['local_id'] for m in msgs)
                for m in msgs:
                    t = m['type']
                    stamp = time.strftime('%m-%d %H:%M:%S', time.localtime(now))
                    if t == 'text':
                        # 跨号回环闸门: 大号固定话术带 [自动回复], 大号 AI 带 [AI生成],
                        # 本号带 [AI]。这些都不是真人指令, 不喂给 agent, 否则两个 bot
                        # 会互相喂话题、整夜烧 token。水位已在上面推进, 这里只跳过。
                        # 2026-11-11 补: 旧白名单漏了 [自动回复], 于是模型对着寒暄模板
                        # 认真回了一句"我理解你的情况…"(见 run.log 16:54 段)。
                        if _is_bot_text(m['text']):
                            print(f'[{stamp}] 忽略对端 bot 发言(前缀命中)')
                        else:
                            print(f'[{stamp}] 对方: {m["text"]}')
                            push(m['text'], now)
                    elif t in ('image', 'voice', 'video'):
                        print(f'[{stamp}] 对方发来 {t}')
                        push(config.MEDIA_REPLIES.get(
                            t, config.MEDIA_REPLIES['media']), now)
                    else:
                        print(f'[{stamp}] (忽略 {t})')
                if session is None:
                    session = agent.new_session(
                        name, history=wx_db.recent_history(
                            conn, table, config.MAX_HISTORY))

        # 对方安静下来 -> 处理这一批
        if pending and (time.time() - last_activity) >= config.QUIET_WINDOW:
            batch, pending = pending, []
            done_id = last_id      # 这一批的末尾水位: 处理完才认它
            user_text = '\n'.join(batch)
            # 首次话术: 这个人第一次来消息 -> 先回一段明确答复(等多久/怎么继续),
            # 追问了才交给 AI。文案/前缀/生效对象都在 config.json 的 first_reply 里,
            # 判定记录在 state 的 _first_reply 下, 与消息库无关(重启/清历史都不会重发)。
            if (freply.enabled_for(config.USER_CFG, un=username, remark=name)
                    and freply.should_send(state, username, config.USER_CFG)):
                fr_text = freply.build(config.USER_CFG)
                print(f'[main] 首次话术 -> {fr_text!r}')
                t1 = time.time()
                fr_ok = bool(sender.send_to(fr_text, prefix=''))
                state[key] = done_id
                if fr_ok:
                    freply.mark(state, username)
                    save_state(state)
                    print(f'[main] 首次话术已发出 {time.time() - t1:.1f}s, '
                          f'本轮不进 AI')
                else:
                    # 不记"已发": 下一条消息会重试, 一次发送失败不该永久跳过这个人
                    save_state(state)
                    print(f'[main] 首次话术没发出去 {time.time() - t1:.1f}s, '
                          f'下条消息重试')
                if once:
                    break
                time.sleep(config.POLL_INTERVAL)
                continue
            if session is None:
                session = agent.new_session(name)
            print(f'[main] 处理合并消息({len(batch)} 条): {user_text[:100]}')
            t0 = time.time()
            t_send = 0.0
            if group_chat.enabled():
                # 群聊: 同一段聊天记录并行喂给多个模型, 各自带自己的前缀发出去。
                # 这一层不带工具(原因见 group_chat 开头), 会话历史只记第一条回复,
                # 免得下一轮主模型被自己另外几个分身说过的话带偏。
                session.user(user_text)
                pairs = group_chat.collect(session.wire())
                session.note = None
                t_brain = time.time() - t0
                if pairs:
                    session.assistant(pairs[0][1])
                session.trim()
                sent = []
                for pfx, txt, nm in pairs:
                    if dry:
                        print(f'[dry] 本应发送({nm}): {pfx}{txt}')
                        sent.append(f'{pfx}{txt}')
                        continue
                    t1 = time.time()
                    ok = bool(sender.send_to(txt, prefix=pfx))
                    t_send += time.time() - t1
                    if ok:
                        sent.append(f'{pfx}{txt}')
                    else:
                        resend.append([txt, 0, pfx])
                        save_resend()
                        print(f'[main] {nm} 这条没发出去, 已放进待重发队列'
                              f'(队列 {len(resend)} 条)')
                reply = ' | '.join(sent)
                print(f'[main] 群聊共发出 {len(sent)} 条')
            else:
                reply = agent.ask(session, user_text, on_note=interim)
                t_brain = time.time() - t0
                ok = True
                if reply:
                    if dry:
                        print(f'[dry] 本应发送: {config.AI_PREFIX}{reply}')
                    else:
                        t1 = time.time()
                        ok = bool(sender.send_to(reply))
                        t_send = time.time() - t1
                        if not ok:
                            # 真的入队: 水位照常推进(不阻塞后续消息), 这条回复交给
                            # 下一轮的 flush_resend() 补发, 不再静默消失。
                            resend.append([reply, 0])
                            save_resend()  # 立刻落盘: 万一马上就自愈重启, 这条不能丢
                            print(f'[main] 这条回复没发出去, 已放进待重发队列'
                                  f'(队列 {len(resend)} 条, 见上面 send 的放弃原因)')
                else:
                    print('[main] 助手没有产出回复, 跳过本轮')
            # 处理完才推进水位 —— 这是全程序唯一的落盘点。
            # 原来是在"读到消息"那一刻就落盘, 于是: 读到 -> 落盘 -> 还在静默等待、
            # 或者还在跑 agent(最久 65s) 时进程被杀, 这批消息就永远读不回来了 ——
            # 重启后从存档水位接着读, 那几条既没回过、也再不会被看到, 界面上毫无异常。
            # 挪到这里之后, 中途被杀最多是重启后重放一遍(至多重复回一次), 不会静默丢消息。
            state[key] = done_id
            save_state(state)
            # 分段计时: 出问题时一眼看出慢在哪一段(静默等待 / 大脑 / 发送)
            print(f'[main] 助手耗时 {t_brain:.1f}s | 发送 {t_send:.1f}s | '
                  f'端到端 {time.time() - first_seen:.1f}s(含静默等待 '
                  f'{config.QUIET_WINDOW:.1f}s)'
                  + (f' | 回复: {reply!r}' if reply else ''))

        if once:
            break
        time.sleep(config.POLL_INTERVAL)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='imhere 一对一桌面助手')
    ap.add_argument('--once', action='store_true', help='跑一轮就退出')
    ap.add_argument('--dry', action='store_true', help='只算不发(不碰微信窗口)')
    ap.add_argument('--probe', action='store_true', help='只做窗口识别自检')
    args = ap.parse_args()
    # 给控制台留一张"我在跑"的名片: 命令行里只有 "main.py", 外面认不出大小号
    pidfile.claim(os.path.join(BASE, 'main.pid'))
    try:
        if args.probe:
            sys.exit(0 if probe() else 1)
        run(dry=args.dry, once=args.once)
    except KeyboardInterrupt:
        print('\n[main] 已停止')
