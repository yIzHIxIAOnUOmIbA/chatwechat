# -*- coding: utf-8 -*-
"""微信消息库(imhere): 只读新账号库, 解密 + 增量查询 + 历史上下文

数据源固定为 config.MSG_DB / config.KEYS_JSON(账号 your_wechat_id),
不引用旧账号的任何库或密钥。"""
import sys, os, json, io, sqlite3, hashlib, re, time
import html as _html

import wcdb_key_tool_windows as W   # 解密核心已随交付同目录
W._print = lambda *a, **kw: None
from wcdb_key_tool_windows import _decrypt_database, _lock_perms

import config

_keys = None
_contact_db = None


def _load_keys():
    global _keys
    if _keys is None:
        _keys = json.load(open(config.KEYS_JSON, encoding='utf-8'))
    return _keys


_tmp_seq = 0            # 临时库序号: 同一进程内每次解密换一个名字
_fallback_tmp = None    # 上次因 MSG_DEC 被占用而改读的那份临时库, 下次解密前清掉


def decrypt():
    """解密 message_0.db -> MSG_DEC, 返回 sqlite3 connection.
    先写临时文件再原子替换: 主循环是"每次指纹变化就解密", 若直接覆盖 MSG_DEC,
    一旦这轮被中断(或微信同时大量写库)就会留下半截文件, 下次 open 直接报
    'database disk image is malformed' 且再也不会自愈。

    临时名带 pid + 序号, 而不是原来固定的 '.tmp': 常驻的 main.py 一直持着 MSG_DEC 的
    connection, 此时再开一个进程(test_offline.py / --probe / 第二个实例)走这里, 固定名
    会互相删掉对方写了一半的文件。更要紧的是那一步 os.replace 会因目标被占用而失败 ——
    旧代码把异常整个吞掉、睡 3×0.3s, 再返回**旧的 MSG_DEC**: 对外表现成"解密要 1.1s"
    (实测 1.3s), 实际是悄悄读到过期数据, 而日志里一个字都没有。"""
    global _tmp_seq, _fallback_tmp
    keys = _load_keys()
    enc_key = bytes.fromhex(keys['message\\message_0.db']['enc_key'])
    # 上一次退回来的临时库: 调用方这一轮应该已经关掉 connection 了, 顺手清掉(含
    # sqlite 为它建的 -wal/-shm, 否则每次退化都留一份垃圾)
    if _fallback_tmp:
        for suf in ('', '-wal', '-shm'):
            try:
                os.remove(_fallback_tmp + suf)
            except OSError:
                pass        # 还开着就留着, 无非多占 152KB, 不影响正确性
        _fallback_tmp = None
    _tmp_seq += 1
    tmp = '%s.%d.%d.tmp' % (config.MSG_DEC, os.getpid(), _tmp_seq)
    try:
        ok = _decrypt_database(config.MSG_DB, tmp, enc_key)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if not ok:
        # 它返回 False 只有两种可能: 源库太小 / Page 1 HMAC 校验不过。而 wx_db 把
        # 它的 _print 置成空函数了, 所以这里必须自己把话说出来 —— 否则就是拿一个
        # 残缺的临时文件去替换好库(silent 的下一层: 换完连报错都没有)。
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError('解密失败: 源库太小或 Page 1 HMAC 校验不过 '
                           f'(源 {os.path.getsize(config.MSG_DB)} B)')
    try:
        os.replace(tmp, config.MSG_DEC)
        _lock_perms(config.MSG_DEC)      # M2: 重设 ACL, 限仅本人可读写
    except OSError as e:
        # 目标是别的进程开着的库, 换不掉。那就直接读**刚解出来的这一份**: 它和主库
        # 同一秒, 是最新的。绝不退回读旧 MSG_DEC —— 那条路会让调用方一直读到过期消息,
        # 而且没有任何痕迹(这才是"解密 1.1s"的真身)。
        print(f'[db] MSG_DEC 被占用, 本次改读临时副本 {os.path.basename(tmp)} ({e})')
        _fallback_tmp = tmp
        _lock_perms(tmp)                 # M2: 临时副本同样限仅本人可读写
        conn = sqlite3.connect(tmp)
        conn.text_factory = str
        return conn
    conn = sqlite3.connect(config.MSG_DEC)
    conn.text_factory = str
    # 自愈(对齐 chat 版): 半截/损坏库能 open 成功但读表才抛 malformed, 这里先校验,
    # 不通过就删掉重解一次; 两次都不行也返回连接, 由上层按空数据处理。
    try:
        row = conn.execute('PRAGMA quick_check(1)').fetchone()
        if row and str(row[0]).strip().lower() == 'ok':
            return conn
    except sqlite3.DatabaseError:
        pass
    print('  [解密] MSG_DEC 校验不过, 删掉重解一次')
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        os.remove(config.MSG_DEC)
    except OSError:
        pass
    _tmp_seq += 1
    tmp = '%s.%d.%d.tmp' % (config.MSG_DEC, os.getpid(), _tmp_seq)
    if _decrypt_database(config.MSG_DB, tmp, enc_key):
        try:
            os.replace(tmp, config.MSG_DEC)
            _lock_perms(config.MSG_DEC)      # M2
        except OSError:
            pass
    conn = sqlite3.connect(config.MSG_DEC)
    conn.text_factory = str
    return conn


def msg_table(username):
    return 'Msg_' + hashlib.md5(username.encode()).hexdigest()


def db_signature():
    """文件指纹: 主库+wal+shm 的 (size, mtime).
    SQLite WAL 模式下写消息先进 -wal 文件, 主库可能不变, 故三个一起监控.
    指纹变化才值得解密读取, 空闲时零开销."""
    sig = []
    for p in (config.MSG_DB, config.MSG_DB + '-wal', config.MSG_DB + '-shm'):
        try:
            st = os.stat(p)
            sig.append((st.st_size, st.st_mtime))   # 保留亚秒精度, 更快捕捉写入
        except OSError:
            sig.append(None)
    return tuple(sig)


def whitelist_tables(conn, usernames):
    """只返回白名单用户实际存在的消息表, 避免全量扫 Name2Id"""
    out = []
    for un in usernames:
        t = msg_table(un)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone():
            out.append((un, t))
    return out


def contact_info(username):
    """查通讯录: 返回 (nick_name, alias, remark); 查不到返回 (None, None, None)"""
    if not os.path.exists(config.CONTACT_DB):
        return None, None, None
    try:
        conn = sqlite3.connect(config.CONTACT_DB)
        conn.text_factory = str
        try:
            row = conn.execute("SELECT nick_name, alias, remark FROM contact "
                               "WHERE username=?", (username,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None, None, None
    if not row:
        return None, None, None
    return ((row[0] or '').strip(), (row[1] or '').strip(), (row[2] or '').strip())


def find_username(display_name):
    """展示名 -> username。给"回复谁就搜谁的微信号"用(见 sender._search_key)。

    依次精确匹配 remark / nick_name / alias, 都不中再做一次模糊匹配。
    查不到返回 None —— 调用方自己决定退回原名搜索。
    """
    name = (display_name or '').strip()
    if not name or not os.path.exists(config.CONTACT_DB):
        return None
    try:
        conn = sqlite3.connect(config.CONTACT_DB)
        conn.text_factory = str
        try:
            for col in ('remark', 'nick_name', 'alias'):
                row = conn.execute(
                    "SELECT username FROM contact WHERE %s=? LIMIT 1" % col,
                    (name,)).fetchone()
                if row and row[0]:
                    return row[0]
            row = conn.execute(
                "SELECT username FROM contact WHERE remark LIKE ? OR nick_name LIKE ? "
                "ORDER BY length(nick_name) LIMIT 1",
                ('%%%s%%' % name, '%%%s%%' % name)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    return row[0] if row and row[0] else None


def resolve_target(username=None):
    """确认目标会话(本实现只服务一个会话), 返回 (username, 展示名)。

    参考实现有一整套 1024xxxx 备注白名单 + 双因子首缀比对; 这里不需要——
    目标写死在 config.TARGET_USERNAME, 通讯录只用来取展示名做日志。
    """
    un = username or config.TARGET_USERNAME
    nick, alias, remark = contact_info(un)
    if nick is None:
        return un, config.TARGET_NAME
    return un, (remark or nick or alias or config.TARGET_NAME)


def sessions(conn):
    """返回 {username: 表名} 列表"""
    out = []
    for (un,) in conn.execute("SELECT user_name FROM Name2Id"):
        t = msg_table(un)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone():
            out.append((un, t))
    return out


_PLACEHOLDER_WORDS = ('动画表情', '表情', '贴图', '图片', '语音', '视频', '文件')


def _is_media_placeholder(t):
    """表情包/动画表情等媒体消息偶尔以 local_type=1 + 占位文本入库(如 '[动画表情]'、
    '动画表情'), 会被误当文字触发 AI 回复。识别: 短文本且形如 [xxx] 或就是常见占位词。"""
    if not t or len(t) > 12:
        return False
    if re.match(r'^[\[【][^\[\]【】]{1,10}[\]】]$', t):
        return True
    return t in _PLACEHOLDER_WORDS


def _msg_type(local_type):
    """取消息的真实类型码。
    微信 4.x 把标志位塞进了 local_type 的高 32 位: 引用/文件/链接等 appmsg 实测为
    266287972401 / 8589934592049 / 399431958577, 低 32 位才是类型码(49)。
    不归一化的话 lt==1 / lt==49 这类判断全部失效, 整类消息被静默丢弃。"""
    try:
        return int(local_type) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return -1


_ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'


def _decompress(raw):
    """微信 4.x 把引用消息等 appmsg 的 XML 用 zstd 压缩后入库(魔数 28 b5 2f fd),
    不解压只能看到乱码, 引用时新写的文字全部丢失。非压缩数据原样返回。"""
    if not isinstance(raw, (bytes, bytearray)) or bytes(raw[:4]) != _ZSTD_MAGIC:
        return raw
    try:
        import zstandard
    except ImportError:
        return raw
    data = bytes(raw)
    try:
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=1 << 22)
    except Exception:
        pass
    try:            # 多帧/流式兜底
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as r:
            return r.read(1 << 22)
    except Exception:
        return raw


# ---------------- 发送者行号(SQL 里最容易写错的一处) ----------------
# Msg_xxx.real_sender_id 存的是 Name2Id.rowid, 实测本机: 1='weixin', 2=自己, 3=对方。
# 它随账号/会话不同而变, 绝不能写死"1=自己": 写死 1 等于谁都不过滤 —— 自己刚发出去的
# 话会被当成对方的新消息再回一遍(实际踩到: 对方每条消息收到两个回复)。
_rowid_cache = {}


def _rowid_of(conn, un):
    """username -> Name2Id.rowid。库里存裸 wxid(wxid_example_acct), 而配置里可能带
    设备后缀(…_0f40), 所以精确匹配不成时再按"配置值以库值开头"的最长前缀匹配。"""
    if not un:
        return None
    if un in _rowid_cache:
        return _rowid_cache[un] or None
    rid = None
    try:
        r = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?",
                         (un,)).fetchone()
        if not r:
            r = conn.execute(
                "SELECT rowid FROM Name2Id WHERE ? LIKE user_name || '%' "
                "ORDER BY length(user_name) DESC LIMIT 1", (un,)).fetchone()
        rid = r[0] if r else None
    except Exception:
        rid = None
    _rowid_cache[un] = rid or 0
    if rid is None:
        print(f'[db] 警告: 未能在 Name2Id 定位账号 {un!r}, 本轮不处理消息')
    return rid


def peer_rowid(conn):
    """对方(唯一会话对象)的行号: 只认这个 sid, 自己的/系统的消息一律不看"""
    return _rowid_of(conn, config.TARGET_USERNAME)


def self_rowid(conn):
    """自己(本机账号)的行号: 用来判断用户有没有人工插话"""
    return _rowid_of(conn, os.path.basename(config.WX_ROOT.rstrip('\\/')))


def new_messages(conn, table, last_id):
    """增量查对方发来的消息: real_sender_id == 对方行号, 按类型分类返回.
    type: text / image / voice / video / emoji(表情,静默处理)
    (图片等非文本消息 content 是加密 blob, extract_text 提取不出文本, 需按 local_type 识别)"""
    peer = peer_rowid(conn)
    if peer is None:
        return []
    rows = conn.execute(
        f"SELECT local_id, real_sender_id, create_time, local_type, message_content "
        f"FROM '{table}' WHERE local_id > ? AND real_sender_id = ? "
        f"ORDER BY local_id ASC", (last_id, peer)).fetchall()
    out = []
    for lid, sid, ctime, raw_type, content in rows:
        lt = _msg_type(raw_type)        # 微信 4.x 的高位标志必须先剥掉
        t = extract_text(content)
        if lt == 1 and t:
            if t.startswith('<'):
                # 解压出来是 XML(voicemsg/appmsg 等)说明根本不是纯文本消息,
                # 别把整段 XML 当聊天内容丢给 AI
                continue
            if _is_media_placeholder(t):
                # 占位文本(表情包等)归为 emoji, 配合 main.py 静默已读, 不触发 AI
                out.append({'local_id': lid, 'sender_id': sid,
                            'time': ctime, 'text': t, 'type': 'emoji'})
            else:
                out.append({'local_id': lid, 'sender_id': sid,
                            'time': ctime, 'text': t, 'type': 'text'})
        elif lt == 3:                       # 图片
            out.append({'local_id': lid, 'sender_id': sid,
                        'time': ctime, 'text': '[图片]', 'type': 'image'})
        elif lt == 34:                      # 语音
            out.append({'local_id': lid, 'sender_id': sid,
                        'time': ctime, 'text': '[语音]', 'type': 'voice'})
        elif lt == 43:                      # 视频
            out.append({'local_id': lid, 'sender_id': sid,
                        'time': ctime, 'text': '[视频]', 'type': 'video'})
        elif lt == 47:                      # 表情/贴图
            out.append({'local_id': lid, 'sender_id': sid,
                        'time': ctime, 'text': '[表情]', 'type': 'emoji'})
        else:
            # 引用消息(通常 local_type=49: 引用前文+新回复): 提取引用时新写的文字
            # 才有回复价值; 纯引用/系统消息(10000)提取不出 -> 忽略不回复。
            # 引用消息一旦进 msgs, main.py 用 max(local_id) 推进游标, 不再卡住轮询。
            q = _quote_reply_text(content)
            if q:
                out.append({'local_id': lid, 'sender_id': sid,
                            'time': ctime, 'text': q, 'type': 'text'})
    return out


# 引用消息 XML: <refermsg> 内 <comment> 是引用时新输入的文字,
# <refer><content> 是被引用的原文。只取 comment, 绝不复读被引用的原文(会像在复读对方)。
_REFER_COMMENT_RE = re.compile(r'<comment[^>]*>([\s\S]*?)</comment>', re.I)


def _quote_reply_text(content):
    """解析引用消息(引用前文+新回复), 返回引用时新写的文字; 无新文字返回 None"""
    content = _decompress(content)
    s = content.decode('utf-8', 'ignore') if isinstance(content, bytes) else (content or '')
    m = _REFER_COMMENT_RE.search(s)
    if not m:
        return None
    t = _html.unescape(m.group(1)).strip()
    return t or None


def self_has_replied(conn, table, last_id):
    """last_id 之后, 用户自己是否在"对方最新消息之后"又发过消息.
    若用户已人工插话, 跳过 AI, 避免重复/乱回。"""
    peer = peer_rowid(conn)
    me = self_rowid(conn)
    if peer is None or me is None:
        return False
    row = conn.execute(
        f"SELECT local_id FROM '{table}' WHERE local_id > ? AND real_sender_id = ? "
        f"AND (local_type & 4294967295) = 1 ORDER BY local_id DESC LIMIT 1",
        (last_id, peer)).fetchone()
    if not row:
        return False
    last_their = row[0]
    r = conn.execute(
        f"SELECT 1 FROM '{table}' WHERE local_id > ? AND real_sender_id = ? "
        f"AND (local_type & 4294967295) = 1 LIMIT 1", (last_their, me)).fetchone()
    return r is not None


def extract_text(raw):
    """从 message_content 提取文字(兼容明文str / zstd压缩 / 二进制中混入的utf8)"""
    if raw is None:
        return ''
    if isinstance(raw, str):
        return raw.strip()
    raw = _decompress(raw)
    if isinstance(raw, str):
        return raw.strip()
    try:
        return raw.decode('utf-8').strip()
    except Exception:
        pass
    # 二进制: 提取其中的 utf-8 可读片段(中文/可打印字符)
    parts = []
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b < 0x80:
            i += 1
            continue
        if 0xC2 <= b <= 0xDF:
            l = 2
        elif 0xE0 <= b <= 0xEF:
            l = 3
        elif 0xF0 <= b <= 0xF4:
            l = 4
        else:
            i += 1
            continue
        chunk = raw[i:i + l]
        try:
            s = chunk.decode('utf-8')
            if any('\u4e00' <= c <= '\u9fff' for c in s) or s.isprintable():
                parts.append(s)
            i += l
        except Exception:
            i += 1
    return ''.join(parts).strip()


# ---------------- 历史上下文 ----------------
def recent_history(conn, table, limit, max_age=None):
    """取该会话最近 limit 条双方文字消息, 返回 [(who, text)] 时间正序。
    给助手当背景用: 让它知道刚才聊到哪, 而不是从零开始。

    max_age: 只取这么多秒以内的(默认 config.HISTORY_MAX_AGE, 0=不限)。
    只按条数取会出事 —— 实测 23:43 那条回复照抄了 21:36 的跑偏回复("从屏幕上看到
    Agents 窗口…"), 两条隔了两小时却被当成同一段连续对话, 模型便把它当成"我刚才
    在做的事"继续复述。隔久了重新开口就是新话题, 旧上下文只会带偏。
    """
    peer = peer_rowid(conn)
    age = config.HISTORY_MAX_AGE if max_age is None else max_age
    sql = (f"SELECT real_sender_id, message_content, create_time FROM '{table}' "
           f"WHERE (local_type & 4294967295) = 1")
    args = []
    if age:
        sql += ' AND create_time >= ?'
        args.append(int(time.time()) - int(age))
    sql += ' ORDER BY local_id DESC LIMIT ?'
    args.append(limit)
    rows = conn.execute(sql, args).fetchall()
    out = []
    for sid, content, _ctime in reversed(rows):
        text = extract_text(content)
        if text.startswith(config.AI_PREFIX):
            # 前缀只是发送时的标记, 别喂给模型 —— 否则它会学着在正文里也写 [AI],
            # 发出去就成了 [AI] [AI] xxx
            text = text[len(config.AI_PREFIX):].strip()
        if text and not text.startswith('<'):
            # 只有"对方行号"才是对方, 其余(自己/系统)都算"我";
            # 以前写 sid == 1 是一直不成立的, 自己发的话全被喂成"对方说过"
            out.append(('我' if sid != peer else '对方', text))
    return out
