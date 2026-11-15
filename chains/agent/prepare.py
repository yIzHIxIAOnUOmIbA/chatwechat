# -*- coding: utf-8 -*-
"""imhere 数据准备 / 自检

职责:
  1) 为新账号重新提取数据库密钥 (wxkeys.json), --rekey 强制重提
  2) 解密 contact.db -> contact_dec.db (通讯录, 用于解析目标会话)
  3) 自检: 目标会话在消息库里是否存在, 打印最近几条消息

用法:
  python prepare.py            # 缺啥补啥
  python prepare.py --rekey    # 强制重新提取密钥
"""
import io
import json
import os
import sqlite3
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace', line_buffering=True)
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import config
import wx_db
import wcdb_key_tool_windows as W

W._print = lambda *a, **kw: None


def extract_keys(force=False):
    """用 wcdb_key_tool 从运行中的微信进程提取密钥, 落到 config.KEYS_JSON"""
    if os.path.exists(config.KEYS_JSON) and not force:
        print(f'[keys] 已存在 {os.path.basename(config.KEYS_JSON)}, 跳过提取')
        return
    print(f'[keys] 正在为 {config.WX_ROOT} 提取密钥 (需微信已登录并运行)...')
    cmd = [sys.executable, os.path.join(BASE, 'wcdb_key_tool_windows.py'),
           'extract', '--db-dir', config.WX_ROOT, '--output', config.KEYS_JSON]
    r = subprocess.run(cmd, cwd=BASE, capture_output=True)
    out = (r.stdout or b'').decode('utf-8', 'ignore')
    tail = [l for l in out.splitlines() if 'salt' in l or 'keys' in l.lower()]
    print('[keys] ' + ' / '.join(tail[-2:]) if tail else '[keys] 提取结束')
    if not os.path.exists(config.KEYS_JSON):
        sys.exit('[keys] 提取失败: 未生成密钥文件')


def decrypt_contact(force=False):
    """解密 contact.db -> CONTACT_DB"""
    if os.path.exists(config.CONTACT_DB) and not force:
        print(f'[contact] 已存在 {os.path.basename(config.CONTACT_DB)}, 跳过解密')
        return
    keys = json.load(open(config.KEYS_JSON, encoding='utf-8'))
    entry = None
    for k, v in keys.items():
        if k.replace('/', '\\').lower().endswith('contact\\contact.db'):
            entry = v
    if not entry:
        sys.exit('[contact] 密钥文件中找不到 contact\\contact.db')
    src = os.path.join(config.WX_ROOT, 'db_storage', 'contact', 'contact.db')
    if os.path.exists(config.CONTACT_DB):
        os.remove(config.CONTACT_DB)
    ok = W._decrypt_database(src, config.CONTACT_DB, bytes.fromhex(entry['enc_key']))
    print(f'[contact] {"解密完成" if ok else "解密失败"}: '
          f'{os.path.basename(config.CONTACT_DB)} '
          f'({os.path.getsize(config.CONTACT_DB) if ok else 0} bytes)')
    if not ok:
        sys.exit('[contact] 解密失败')


def check_target():
    """自检目标会话: 通讯录昵称 + 消息表 + 最近消息"""
    conn = sqlite3.connect(config.CONTACT_DB)
    conn.text_factory = str
    row = conn.execute('SELECT username, alias, nick_name, remark FROM contact '
                       'WHERE username=?', (config.TARGET_USERNAME,)).fetchone()
    conn.close()
    print(f'[check] 目标联系人: {row}')
    if not row:
        sys.exit(f'[check] 通讯录里没有 {config.TARGET_USERNAME}')

    conn = wx_db.decrypt()
    table = wx_db.msg_table(config.TARGET_USERNAME)
    exists = conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone()
    if not exists:
        sys.exit(f'[check] 消息库里没有目标会话表 {table}')
    total = conn.execute(f"SELECT COUNT(*) FROM '{table}'").fetchone()[0]
    print(f'[check] 会话表 {table} 共 {total} 条消息')
    rows = conn.execute(
        f"SELECT local_id, real_sender_id, create_time, local_type, message_content "
        f"FROM '{table}' ORDER BY local_id DESC LIMIT 5").fetchall()
    conn.close()
    import time as _t
    for lid, sid, ct, lt, content in reversed(rows):
        text = wx_db.extract_text(content)
        ts = _t.strftime('%m-%d %H:%M', _t.localtime(ct))
        who = '我' if sid == 1 else '对方'
        print(f'   #{lid} {ts} {who} t={wx_db._msg_type(lt)}: {text[:60]!r}')


if __name__ == '__main__':
    force = '--rekey' in sys.argv
    print(f'[prepare] 账号数据目录: {config.WX_ROOT}')
    extract_keys(force=force or not os.path.exists(config.KEYS_JSON))
    decrypt_contact(force=force)
    check_target()
    print('[prepare] 全部就绪')
