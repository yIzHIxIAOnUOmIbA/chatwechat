# -*- coding: utf-8 -*-
"""微信消息库: 解密 + 增量查询 + 备注查询"""
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


def _write_dec():
    """解密一次到 MSG_DEC(先写 .tmp 再原子替换), 返回是否成功。"""
    keys = _load_keys()
    enc_key = bytes.fromhex(keys['message\\message_0.db']['enc_key'])
    tmp = config.MSG_DEC + '.tmp'
    ok = False
    for _ in range(3):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
            _decrypt_database(config.MSG_DB, tmp, enc_key)
            os.replace(tmp, config.MSG_DEC)
            _lock_perms(config.MSG_DEC)      # M2: 重设 ACL, 限仅本人可读写
            ok = True
            break
        except Exception:
            time.sleep(0.3)
    if not ok and os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    return ok


def decrypt():
    """解密 message_0.db -> MSG_DEC, 返回 sqlite3 connection.
    先写临时文件再原子替换: 主循环是"每次指纹变化就解密", 若直接覆盖 MSG_DEC,
    一旦这轮被中断(或微信同时大量写库)就会留下半截文件, 下次 open 直接报
    'database disk image is malformed' 且再也不会自愈。

    这里再加一道 quick_check 自愈: 半截文件能 open 成功但读表就抛
    malformed(实到过: 主循环整个崩掉退出), 所以解密完先校验, 不通过就删掉
    重解一次; 两次都不行也返回连接, 由上层按空数据处理, 不让进程死在解密里。
    """
    for attempt in range(2):
        _write_dec()
        conn = sqlite3.connect(config.MSG_DEC)
        conn.text_factory = str
        try:
            row = conn.execute('PRAGMA quick_check(1)').fetchone()
            if row and str(row[0]).strip().lower() == 'ok':
                return conn
            why = row[0] if row else 'empty'
        except sqlite3.DatabaseError as e:
            why = str(e)[:60]
        try:
            conn.close()
        except Exception:                                  # noqa: BLE001
            pass
        print(f'  [解密] 临时库不可用({why}), 删掉重解一次')
        try:
            os.remove(config.MSG_DEC)
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


def load_whitelist_remarks(whitelist):
    """批量加载: username -> (remark, remark8)

    两条路:
    1) 备注以 1024xxxx(白名单ID) 开头: 双因子匹配, 兼容原有联系人;
    2) whitelist.json 直接以 username(wxid_xxx / filehelper / 群聊 id)为键:
       值填能在微信里搜到该会话的名字。contact_dec.db 是离线解密的快照,
       新联系人或刚改过的备注可能根本不在里面, 只靠备注匹配会永远漏掉这类会话。
    """
    out = {}
    rows = []
    if os.path.exists(config.CONTACT_DB):
        try:
            conn = sqlite3.connect(config.CONTACT_DB)
            conn.text_factory = str
            rows = conn.execute(
                "SELECT username, remark FROM contact "
                "WHERE remark IS NOT NULL AND remark != ''").fetchall()
            conn.close()
        except Exception:
            rows = []
    for un, remark in rows:
        remark = str(remark).strip()
        m = re.match(r'^(1024\d{4})', remark)
        if not m:
            continue
        key = m.group(1)
        val = whitelist.get(key)
        if not val:
            continue
        # 双因子: ID 命中白名单 且 剩余部分与白名单名字互为首缀。
        # 白名单值常被直接填成备注全名(自带 1024xxxx 前缀), 先剥掉再比,
        # 否则该联系人会被整体排除出轮询, 收到消息也永远检测不到。
        rest = remark[len(key):].strip()
        v = re.sub(r'^1024\d{4}', '', str(val)).strip() or str(val).strip()
        if len(v) >= 2 and rest and (rest.startswith(v) or v.startswith(rest)):
            out[un] = (remark, key)
    # 直接指定的会话: 键不是 1024xxxx 白名单 ID 时按 username 处理。
    for key, val in (whitelist or {}).items():
        k = str(key).strip()
        if re.match(r'^1024\d{4}$', k) or k in out:
            continue
        if k.startswith(('wxid_', 'filehelper')) or k.endswith('@chatroom'):
            label = str(val).strip() or k
            out[k] = (label, label)
    return out


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


def max_local_id(conn, table):
    """该会话当前最新一条的 local_id, 空表返回 0。

    白名单从 7 人扩到上百人后, 绝大多数会话从没进过 state。若只靠 new_messages
    判空来初始化水位, 空表会话永远拿不到 state 记录, 对方发来的**第一条**消息仍会
    撞上"首次扫描不轰炸"分支被吞掉。所以接入时无论如何都要落一条水位。
    """
    try:
        row = conn.execute(f"SELECT MAX(local_id) FROM '{table}'").fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:                                   # noqa: BLE001
        return 0


# ---------------- 发送者行号(与 chains/agent/wx_db.py 同一个坑) ----------------
# Msg_xxx.real_sender_id 存的是 Name2Id.rowid, 实测本机: 1='weixin', 2=自己, 3=对方,
# 且随账号/会话变化。旧版把系统号 1 当成"自己"来排除, 等于谁都不排除: 自己刚发出去的
# [AI生成] 回复被当成对方新消息再回一遍, 无限自转(一晚上烧完 token)。
# 大号是多会话, 所以按 username 逐会话解析对方行号; 解析不出来宁可不回也不猜。
_rowid_cache = {}


def _rowid_of(conn, un):
    """username -> Name2Id.rowid。配置里的账号目录可能带设备后缀(_xxxx),
    库里存的是裸 wxid, 精确匹配不成时按最长前缀匹配。"""
    if not un:
        return None
    if un in _rowid_cache:
        return _rowid_cache[un] or None
    rid = None
    try:
        r = conn.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (un,)).fetchone()
        if not r:
            r = conn.execute(
                "SELECT rowid FROM Name2Id WHERE ? LIKE user_name || '%' "
                "ORDER BY length(user_name) DESC LIMIT 1", (un,)).fetchone()
        rid = r[0] if r else None
    except Exception:
        rid = None
    _rowid_cache[un] = rid or 0
    return rid


def peer_rowid(conn, un):
    """某个会话里"对方"的行号: 只认这个 sid, 自己发的/系统消息一律不看。"""
    return _rowid_of(conn, un)


def self_rowid(conn):
    """自己(本机账号)的行号: 判断用户有没有人工插话。"""
    return _rowid_of(conn, os.path.basename(config.WX_ROOT.rstrip('\\/')))


def new_messages(conn, table, last_id, username=None):
    """增量查对方发来的消息: real_sender_id == 该会话对方行号。
    type: text / image / voice / video / emoji(表情,静默处理)
    (图片等非文本消息 content 是加密 blob, extract_text 提取不出文本, 需按 local_type 识别)"""
    peer = peer_rowid(conn, username) if username else None
    if peer is None:
        print(f'[db] 未能定位会话 {username!r} 的对方行号, 本轮不处理')
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


def self_has_replied(conn, table, last_id, username=None, ignore_prefixes=()):
    """last_id 之后, 用户自己是否在"对方最新消息之后"又发过消息。
    若用户已人工插话回复, 跳过 AI, 避免重复/乱回。
    行号必须按 Name2Id 动态取: 写死 1 会让这道闸永远不命中。

    ignore_prefixes: 本机 bot 自己发出去的文本前缀(AI 回复、首次固定话术)。
    这些消息也是 real_sender_id=me, 但不代表"用户人工接话了" —— 不能因为
    快通道刚发过一句固定话术, 就把对方随后的追问也吞掉。"""
    peer = peer_rowid(conn, username) if username else None
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
    rows = conn.execute(
        f"SELECT message_content FROM '{table}' WHERE local_id > ? AND real_sender_id = ? "
        f"AND (local_type & 4294967295) = 1 ORDER BY local_id ASC LIMIT 50",
        (last_their, me)).fetchall()
    pres = tuple(str(p) for p in (ignore_prefixes or ()) if str(p or ''))
    for (raw,) in rows:
        txt = extract_text(raw)
        if pres and any(txt.startswith(p) for p in pres):
            continue                 # bot 自己发的, 不算人工插话
        return True
    return False


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


# ---------------- contact / remark ----------------
def get_remark(username):
    """查备注全名, 返回 (remark, remark8) 或 (None,None)"""
    global _contact_db
    if _contact_db is None:
        if not os.path.exists(config.CONTACT_DB):
            return None, None
        _contact_db = sqlite3.connect(config.CONTACT_DB)
        _contact_db.text_factory = str
    row = _contact_db.execute(
        "SELECT remark FROM contact WHERE username=?", (username,)).fetchone()
    if not row or not row[0]:
        return None, None
    remark = row[0].strip()
    m = re.match(r'^(1024\d{4})', remark)
    if not m:
        return None, None
    return remark, m.group(1)
