# -*- coding: utf-8 -*-
"""wxduo 控制台 —— 一个窗口管住两个机器人。

为什么单独做一个 GUI, 而不是继续手改 json:
  1. 两套配置都是"改完要重启才生效"的 json, 手改最容易写坏逗号和引号, 写坏的后果
     是服务起不来、微信里一条都不回, 而你还以为是没人发消息;
  2. 服务是隐藏窗口跑的(autostart/*.vbs), 想看它在不在跑、想关掉, 只能开任务管理器,
     再从上几十个 python 里猜哪个是它;
  3. 白名单分了两处(whitelist.json 主体 + config.json 里几条), 手动维护迟早对不上 ——
     这里统一按 whitelist.json 一处写。

只做三件事: 读 json -> 给你表单改(带校验) -> 先备份再写回, 按需重启服务。
写完不重启 = 不生效。

用法: 双击 控制台.bat, 或 python wxduo_console.py。
想管另一个目录的 wxduo, 设环境变量 WXDUO_ROOT 指过去。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox

ROOT = os.environ.get('WXDUO_ROOT') or os.path.dirname(os.path.abspath(__file__))
APPS = ('main', 'mini')
APP_DIR = {a: os.path.join(ROOT, 'apps', a) for a in APPS}
CFG_FILE = {a: os.path.join(APP_DIR[a], 'config.json') for a in APPS}
WL_FILE = os.path.join(APP_DIR['main'], 'whitelist.json')
AUTOSTART = os.path.join(ROOT, 'autostart')
# 服务自己写的"名片"(见 core/pidfile.py): 命令行里只有 main.py, 外面认不出大小号
PID_GUARD = {'mini': os.path.join(APP_DIR['mini'], 'guard.pid')}
PID_MAIN = {a: os.path.join(APP_DIR[a], 'main.pid') for a in APPS}
OLLAMA = 'http://127.0.0.1:11434'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
AUTO_ENTRIES = {'main': 'WxDuoMain', 'mini': 'WxDuoMini', 'ollama': 'WxDuoOllama'}
# ---- 云端线路 ----
# 顺序是策略, 不是偏好: GLM 先上, DeepSeek 顶第二条, 两条都挂才落本地。
# 控制台只负责开/关, 排序写死在这里(用户在 json 里排数组迟早排乱)。
CLOUD_IDS = ('glm', 'deepseek')
CLOUD_TITLES = {'glm': 'GLM（智谱）', 'deepseek': 'DeepSeek'}
CLOUD_MODELS = {
    'glm': ['glm-4.5-air', 'glm-4-flash', 'glm-4-air'],
    'deepseek': ['deepseek-v4-flash', 'deepseek-chat', 'deepseek-reasoner'],
}
CLOUD_PRESETS = {
    'glm': {'enabled': True,
            'base_url': 'https://open.bigmodel.cn/api/paas/v4',
            'model': 'glm-4.5-air', 'fallback_model': 'glm-4-flash',
            'timeout': 60},
    'deepseek': {'enabled': True,
                 'base_url': 'http://127.0.0.1:8789',
                 'model': 'deepseek-v4-flash', 'fallback_model': '',
                 'timeout': 120},
}
# CodeBuddy 自己的模型配置: 里面有 apiKey, 控制台从这儿"抄"一份进 wxduo,
# 代码里不存任何 key 字面量(发布版换台机器也能用同一个入口)。
CB_MODELS_JSON = os.path.join(os.path.expanduser('~'), '.codebuddy', 'models.json')
NO_WIN = 0x08000000 if os.name == 'nt' else 0
_MISSING = object()     # 区分"json 里没这个键"和"值是 null"

sys.path.insert(0, ROOT)
try:
    from core import pidfile
except Exception:                                            # noqa: BLE001
    pidfile = None

# 表单字段: (哪个号, json 路径, 类型)。路径支持 a.b.c, 缺的键自动补。
FIELDS = [
    # ---- 大号: 模型 ----
    ('main', 'llm_enabled', 'bool'),
    ('main', 'llm_model', 'str'),
    ('main', 'llm_fallback_model', 'str'),
    ('main', 'llm_timeout', 'int'),
    ('main', 'llm_warmup', 'bool'),
    ('main', 'llm_num_ctx', 'int'),
    ('main', 'llm_keep_alive', 'int'),
    # ---- 云端线路(两个号各一套开关; url/key 两号共用, 存在大号那份里) ----
    ('main', 'cloud_providers.glm.enabled', 'bool'),
    ('main', 'cloud_providers.glm.model', 'str'),
    ('main', 'cloud_providers.glm.fallback_model', 'str'),
    ('main', 'cloud_providers.glm.timeout', 'int'),
    ('main', 'cloud_providers.glm.base_url', 'str'),
    ('main', 'cloud_providers.glm.api_key', 'str'),
    ('main', 'cloud_providers.deepseek.enabled', 'bool'),
    ('main', 'cloud_providers.deepseek.model', 'str'),
    ('main', 'cloud_providers.deepseek.fallback_model', 'str'),
    ('main', 'cloud_providers.deepseek.timeout', 'int'),
    ('main', 'cloud_providers.deepseek.base_url', 'str'),
    ('main', 'cloud_providers.deepseek.api_key', 'str'),
    # ---- 大号: 运行 ----
    ('main', 'poll_interval', 'float'),
    ('main', 'allow_search', 'bool'),
    # ---- 小号: 模型 ----
    ('mini', 'llm_primary', 'str'),
    ('mini', 'llm_model', 'str'),
    ('mini', 'llm_timeout', 'int'),
    ('mini', 'llm_num_ctx', 'int'),
    ('mini', 'llm_num_gpu', 'int'),
    ('mini', 'llm_keep_alive', 'int'),
    ('mini', 'llm_warmup', 'bool'),
    ('mini', 'llm_think', 'bool'),
    ('mini', 'llm_num_predict', 'int'),
    ('mini', 'fast_reply', 'bool'),
    # 云端: 小号只留自己的开关, 线路参数(模型/url/key/超时)跟大号共用一份
    ('mini', 'cloud_providers.glm.enabled', 'bool'),
    ('mini', 'cloud_providers.deepseek.enabled', 'bool'),
    # ---- 小号: 多模型群聊 ----
    ('mini', 'multi_model.enabled', 'bool'),
    ('mini', 'multi_model.timeout', 'int'),
    ('mini', 'multi_model.num_predict', 'int'),
    # ---- 小号: 运行/杂项 ----
    ('mini', 'poll_interval', 'float'),
    ('mini', 'quiet_window', 'float'),
    ('mini', 'max_reply_len', 'int'),
    ('mini', 'send_interim', 'bool'),
    ('mini', 'send_verify', 'bool'),
    ('mini', 'skills.enabled', 'bool'),
    ('mini', 'subagent.enabled', 'bool'),
    # ---- 首次话术 ----
    ('main', 'first_reply.enabled', 'bool'),
    ('main', 'first_reply.prefix', 'str'),
    ('main', 'first_reply.text', 'str'),
    ('main', 'first_reply.suffix', 'str'),
    ('main', 'first_reply.template', 'str'),
    ('main', 'first_reply.users', 'list'),
    ('main', 'first_reply.reset_hours', 'float'),
    ('mini', 'first_reply.enabled', 'bool'),
    ('mini', 'first_reply.prefix', 'str'),
    ('mini', 'first_reply.text', 'str'),
    ('mini', 'first_reply.suffix', 'str'),
    ('mini', 'first_reply.template', 'str'),
    ('mini', 'first_reply.users', 'list'),
    ('mini', 'first_reply.reset_hours', 'float'),
]


# ---------------------------------------------------------------- 文件读写
def load_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return {} if default is None else default


def save_json(path, obj):
    """内容没变一个字节都不写(避免每次点保存都刷备份); 变了先备份再原子替换。"""
    txt = json.dumps(obj, ensure_ascii=False, indent=2) + '\n'
    old = None
    try:
        with open(path, encoding='utf-8') as f:
            old = f.read()
    except OSError:
        pass
    if old is not None and old.strip() == txt.strip():
        return False
    if old is not None:
        try:
            shutil.copy2(path, path + '.bak-' + time.strftime('%Y%m%d-%H%M%S'))
        except OSError:
            pass
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(txt)
    os.replace(tmp, path)
    return True


def jget(obj, path, default=None):
    cur = obj
    for part in path.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def jset(obj, path, value):
    parts = path.split('.')
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def cb_provider_keys():
    """从 CodeBuddy 的 models.json 里捞各家的 apiKey / base_url。

    只读本机这一个文件: 代码里不写死任何 key, 发布出去的版本不带谁的密钥;
    换台机器只要 CodeBuddy 里配过, 控制台同样能"抄"到。
    返回 {'glm': {'api_key': .., 'base_url': ..}, 'deepseek': {...}}
    """
    data = load_json(CB_MODELS_JSON, {}) or {}
    out = {}
    for m in (data.get('models') or []):
        if not isinstance(m, dict):
            continue
        mid = str(m.get('id') or '').lower()
        vendor = str(m.get('vendor') or '').lower()
        if 'zhipu' in vendor or mid.startswith('glm'):
            pid = 'glm'
        elif 'deepseek' in vendor or mid.startswith('deepseek'):
            pid = 'deepseek'
        else:
            continue
        rec = out.setdefault(pid, {})
        key = str(m.get('apiKey') or '').strip()
        if key and not rec.get('api_key'):
            rec['api_key'] = key
        base = str(m.get('url') or '').split('/chat/completions')[0].rstrip('/')
        if base.startswith('http') and not rec.get('base_url'):
            rec['base_url'] = base
    return out


def legacy_cloud_key(app):
    """apps/<app>/config.py 里那把"只在代码里"的默认云端 key。

    老配置的 key 是 config.py 的默认值(不在 json 里), 而 CodeBuddy 那把智谱 key
    直连 open.bigmodel.cn 会 401 —— 所以必须把真正在用的那把捞出来, 别覆盖成
    一把不能用的。单独起进程读, 免得两个同名 config 模块在控制台里互相顶掉。
    """
    code = 'import config;print(config.CLOUD_API_KEY or "")'
    rc, out = run_cmd([sys.executable, '-c', code], timeout=20,
                      cwd=APP_DIR[app])
    if rc != 0 or not out:
        return ''
    lines = [x.strip() for x in out.decode('utf-8', 'replace').splitlines() if x.strip()]
    return lines[-1] if lines else ''


def seed_cloud_cfg(cfg, keys, legacy=''):
    """补齐 cloud_providers: 只补缺的键, 已有的一律不动(用户改过就听用户的)。

    没有 key 的线路默认关掉 —— 开了也是一次必然失败的请求, 不如不排进队列。
    """
    cur = jget(cfg, 'cloud_providers', _MISSING)
    if not isinstance(cur, dict):
        jset(cfg, 'cloud_providers', {})
        cur = cfg['cloud_providers']
    added = []
    for pid in CLOUD_IDS:
        existed = isinstance(cur.get(pid), dict)
        p = cur.get(pid) if existed else {}
        cur[pid] = p
        src = keys.get(pid) or {}
        for k, v in CLOUD_PRESETS[pid].items():
            if k == 'enabled':
                continue
            if k not in p:
                p[k] = src.get(k) or v
                added.append('%s.%s' % (pid, k))
        if 'api_key' not in p:
            # GLM 优先用 wxduo 自己那份(实测: CodeBuddy 里的智谱 key 直连官方
            # 返回 401, 而老配置这把是通的, 不能被"导入"覆盖掉)
            old_key = legacy or str(jget(cfg, 'cloud_api_key', '') or '')
            p['api_key'] = ((old_key or src.get('api_key') or '') if pid == 'glm'
                            else (src.get('api_key') or old_key or ''))
            if p['api_key']:
                added.append('%s.api_key' % pid)
        if 'enabled' not in p:
            p['enabled'] = bool(pid == 'glm' or p.get('api_key'))
            added.append('%s.enabled' % pid)
    return added


def parse_wl_lines(text):
    """把粘进来的一堆文本解析成 [(id, 名字)], 返回 (行, 不合格行数)。

    认 "示例会话 张三" / "张三,示例会话" / 制表符 / 全角空格 / 顿号;
    ID = 6~12 位纯数字(微信备注前缀一般 8 位), 名字 = 这一行剩下的部分。
    """
    rows, bad = [], 0
    for line in (text or '').replace('\u3000', ' ').splitlines():
        s = line.strip().strip('\ufeff')
        if not s or s.startswith('#'):
            continue
        parts = [p for p in re.split(r'[\s,，、;；|\t]+', s) if p]
        ids = [p for p in parts if re.match(r'^\d{6,12}$', p)]
        if not ids:
            bad += 1
            continue
        name = ' '.join(p for p in parts if p not in ids).strip('()（）[]【】 ')
        if not name:
            bad += 1
            continue
        rows.append((ids[0], name))
    return rows, bad


def _mm_key(rows):
    """群聊条目的比较指纹: 只比内容, 不比键顺序。"""
    return json.dumps(rows, ensure_ascii=False, sort_keys=True)


def decode(data):
    """日志编码看服务当时用的: 控制台里是 utf-8, 重定向进 bat 时可能是 GBK。"""
    for enc in ('utf-8', 'mbcs', 'gbk'):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode('utf-8', 'replace')


def tail(path, lines=200):
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return '(还没有日志: %s)' % path
    rows = decode(data).splitlines()
    return '\n'.join(rows[-lines:]) if rows else '(空)'


def run_cmd(args, timeout=20, cwd=None):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, cwd=cwd,
                           creationflags=NO_WIN)
        return p.returncode, p.stdout
    except Exception as e:                                   # noqa: BLE001
        return -1, ('%s: %s' % (type(e).__name__, e)).encode('utf-8', 'replace')


def ollama_models():
    try:
        with urllib.request.urlopen(OLLAMA + '/api/tags', timeout=1.5) as r:
            data = json.loads(r.read().decode('utf-8'))
        return [m['name'] for m in data.get('models') or []]
    except Exception:                                        # noqa: BLE001
        return []


def wechat_running():
    _code, out = run_cmd(['tasklist', '/FI', 'IMAGENAME eq Weixin.exe', '/NH'])
    return b'Weixin.exe' in (out or b'')


def reg_autostart():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            got, i = set(), 0
            while True:
                try:
                    name, _v, _t = winreg.EnumValue(k, i)
                except OSError:
                    break
                got.add(name)
                i += 1
            return got
    except Exception:                                        # noqa: BLE001
        return set()


def reg_set(name, command):
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, command)
        return True
    except Exception:                                        # noqa: BLE001
        return False


def reg_del(name):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return True
    except Exception:                                        # noqa: BLE001
        return False


# ---------------------------------------------------------------- 服务起停
def pid_alive(pid):
    if pidfile is not None:
        return pidfile.alive(pid)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def service_pids(app):
    """(主进程, 工作进程)。小号的主进程是看门狗 guard.py。"""
    if app == 'main':
        boss = pidfile.live_pid(PID_MAIN['main']) if pidfile else None
    else:
        boss = pidfile.live_pid(PID_GUARD['mini']) if pidfile else None
    worker = pidfile.live_pid(PID_MAIN[app]) if pidfile else None
    return boss, worker


def kill_pid(pid):
    if pid:
        run_cmd(['taskkill', '/PID', str(pid), '/T', '/F'], timeout=15)


def start_app(app):
    vbs = os.path.join(AUTOSTART, 'run_%s.vbs' % app)
    if not os.path.exists(vbs):
        return False, '找不到启动脚本: %s' % vbs
    try:
        subprocess.Popen(['wscript.exe', vbs], cwd=ROOT, creationflags=NO_WIN)
        return True, ''
    except Exception as e:                                   # noqa: BLE001
        return False, '%s: %s' % (type(e).__name__, e)


def stop_app(app):
    boss, worker = service_pids(app)
    kill_pid(boss)
    if worker and worker != boss:
        kill_pid(worker)
    for p in (PID_GUARD.get(app), PID_MAIN.get(app)):
        if p:
            try:
                os.remove(p)
            except OSError:
                pass
    return bool(boss or worker)


def build_first_reply(cfg):
    """和 core/first_reply.build 同一套拼法, 只用于界面预览。"""
    fr = cfg or {}
    tpl = (fr.get('template') or '').strip()
    if tpl:
        try:
            return tpl.format(prefix=fr.get('prefix') or '',
                              text=fr.get('text') or '',
                              suffix=fr.get('suffix') or '')
        except Exception:                                    # noqa: BLE001
            return '(模板占位符写错了: 只能用 {prefix}/{text}/{suffix})'
    return '%s%s%s' % (fr.get('prefix') or '', fr.get('text') or '', fr.get('suffix') or '')
# ---------------------------------------------------------------- 界面
class Console(tk.Tk):
    # ---- 配色(唯一来源, 改这里等于改全站) ----
    BG = '#f4f5f7'
    BORDER = '#d9dce1'
    TEXT = '#1f2328'
    MUTED = '#6b7280'
    PRIMARY = '#2563eb'
    PRIMARY_DK = '#1d4ed8'
    DANGER = '#c0392b'

    def __init__(self):
        super().__init__()
        self.title('wxduo 控制台')
        # 窗口高度按屏幕裁剪: 之前固定 760, 在小屏/任务栏高的机器上底部那条
        # "保存"会被裁到屏幕外(用户反馈"找不到保存按键"), 现在保存按钮两处都有。
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(1080, max(900, sw - 160))
        h = min(800, max(600, sh - 130))
        self.geometry('%dx%d+%d+%d' % (w, h, max(0, (sw - w) // 2),
                                       max(0, (sh - h) // 2 - 24)))
        self.minsize(900, 620)
        self.cfg = {}
        self.wl = {}
        self.mm = []
        self.vars = {}
        self.initial = {}       # 每个控件载入时的值: 没动过就不往 json 里写
        self.field_types = {}
        self.prompt_text = {}
        self.first_hint = {}    # app -> 显示"已发过首发"的会话数
        self.models = []
        self.status = {}
        self._dirty = False
        self._loading = True
        self._load_state()
        self._init_style()
        self._build()
        self._fill()
        self._refresh_first_hint()
        if self._seed_added:
            # 第一次跑: 把 GLM/DeepSeek 的线路(含从 CodeBuddy 抄来的 key)落盘,
            # 这样服务重启后就能用, 不用先手动点保存。
            self.save(quiet=True)
            self.status_var.set('已写入云端线路: GLM%s + DeepSeek%s'
                                % ('(已导入 key)' if self._seed_keys.get('glm') else '',
                                   '(已导入 key)' if self._seed_keys.get('deepseek') else ''))
        self.after(60, self._tint_titlebar)
        self.after(200, self.refresh_status_async)

    def _tint_titlebar(self):
        """把系统标题栏刷成和界面同色(Windows 11 22H2+)。

        不改的话标题栏跟着系统强调色走, 默认是紫红 —— 一条紫杠压在浅灰界面上,
        比界面本身更抢眼。老系统/旧版本不支持这两个属性, 静默跳过即可。
        COLORREF 是 0x00BBGGRR, 所以 #f4f5f7 要写成 0x00F7F5F4。"""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            for attr, color in ((35, 0x00F7F5F4), (36, 0x0028231F)):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(ctypes.c_int(color)), 4)
        except Exception:                                    # noqa: BLE001
            pass

    # ---------- 数据 ----------
    def _load_state(self):
        self.cfg = {a: (load_json(CFG_FILE[a], {}) or {}) for a in APPS}
        # 云端线路: 缺什么补什么。key 从 CodeBuddy 的 models.json 抄, 代码里不存 key
        self._seed_keys = cb_provider_keys()
        self._seed_added = []
        for a in APPS:
            self._seed_added += seed_cloud_cfg(self.cfg[a], self._seed_keys,
                                               legacy_cloud_key(a))
        # config.json 里的 whitelist 是历史遗留"补充项": 展示时并进来, 保存时统一
        # 写回 whitelist.json 并把那边清空(一处维护, 不会再对不上)
        wl = dict(load_json(WL_FILE, {}) or {})
        for k, v in (self.cfg['main'].get('whitelist') or {}).items():
            wl.setdefault(str(k), v)
        self.wl = wl
        mm = jget(self.cfg['mini'], 'multi_model.models', _MISSING)
        self._mm_was = mm is not _MISSING       # 原来没有这个键, 没动过就别造出来
        self.mm = [dict(x) for x in mm if isinstance(x, dict)] if isinstance(mm, list) else []
        for i, m in enumerate(self.mm):
            m.setdefault('enabled', True)
            m.setdefault('name', '模型%d' % (i + 1))
            m.setdefault('via', 'local')
            m.setdefault('model', '')
        self._mm_orig = _mm_key(self._mm_rows())   # 载入时的基线, 用来判断"有没有动过"
        self.models = ollama_models()

    def _init_style(self):
        """统一视觉: clam 主题 + 一套配色, 全在样式层, 各 tab 的布局代码不用动。

        Windows 默认 vista 主题几乎不允许改颜色, 界面看起来就是一张灰表单;
        换 clam 之后把 底色/边框/主按钮/危险按钮/页签/输入框 一次定好, 以后
        调色只改这里。
        所有容器统一用同一个浅灰底(不做白卡): ttk 控件没有透明背景, Label 落
        在白卡上会露出灰方块, 与其逐处换 style, 不如靠 1px 边框 + 加粗标题 +
        行距来分层, 既干净又不会漏改。"""
        fam = 'Microsoft YaHei UI'
        self.option_add('*Font', (fam, 10))
        st = ttk.Style(self)
        try:
            st.theme_use('clam')
        except tk.TclError:
            pass
        self.configure(background=self.BG)
        st.configure('.', font=(fam, 10), background=self.BG,
                     foreground=self.TEXT, bordercolor=self.BORDER)
        # ---- 文字层级 ----
        st.configure('TLabel', background=self.BG, foreground=self.TEXT)
        st.configure('H1.TLabel', font=(fam, 15, 'bold'), background=self.BG)
        st.configure('Sub.TLabel', font=(fam, 9), foreground=self.MUTED,
                     background=self.BG)
        st.configure('Dirty.TLabel', foreground='#b45309',
                     font=(fam, 10, 'bold'), background=self.BG)
        st.configure('Ok.TLabel', foreground='#15803d', background=self.BG)
        st.configure('Info.TLabel', foreground=self.PRIMARY, background=self.BG)
        # ---- 分组框: 细边框 + 加粗标题, 就是这里的"卡片" ----
        st.configure('TLabelframe', background=self.BG, bordercolor=self.BORDER,
                     relief='solid', borderwidth=1)
        st.configure('TLabelframe.Label', background=self.BG,
                     foreground=self.TEXT, font=(fam, 10, 'bold'))
        # ---- 按钮: 普通白底描边, 主操作蓝底, 破坏性操作红字 ----
        st.configure('TButton', padding=(12, 5), background='#ffffff',
                     bordercolor=self.BORDER, lightcolor='#ffffff',
                     darkcolor='#ffffff', focuscolor=self.BG)
        st.map('TButton',
               background=[('disabled', '#f0f1f3'), ('pressed', '#e2e6ec'),
                           ('active', '#eef1f6')],
               foreground=[('disabled', '#9aa0a6')])
        st.configure('Accent.TButton', padding=(16, 6), background=self.PRIMARY,
                     foreground='#ffffff', font=(fam, 10, 'bold'),
                     bordercolor=self.PRIMARY, lightcolor=self.PRIMARY,
                     darkcolor=self.PRIMARY, focuscolor=self.PRIMARY)
        st.map('Accent.TButton',
               background=[('active', self.PRIMARY_DK), ('pressed', self.PRIMARY_DK)],
               foreground=[('disabled', '#c7d5f7')])
        st.configure('Danger.TButton', padding=(12, 5), background='#ffffff',
                     foreground=self.DANGER, bordercolor='#e6b3ae',
                     lightcolor='#ffffff', darkcolor='#ffffff')
        st.map('Danger.TButton',
               background=[('active', '#fdecea'), ('pressed', '#fbd9d5')])
        # ---- 页签 ----
        # clam 的 Notebook 客户端区会吃掉一层浅色底(tab 右侧那条白带就是它),
        # 三个颜色都压成同一个才跟窗口底色接得上
        st.configure('TNotebook', background=self.BG, lightcolor=self.BG,
                     darkcolor=self.BG, bordercolor=self.BORDER,
                     tabmargins=(2, 6, 2, 0))
        st.configure('TNotebook.Tab', padding=(18, 8), background='#e8eaee',
                     foreground=self.MUTED, bordercolor=self.BORDER,
                     font=(fam, 10))
        st.map('TNotebook.Tab',
               background=[('selected', '#ffffff'), ('active', '#f0f2f5')],
               foreground=[('selected', self.PRIMARY)],
               font=[('selected', (fam, 10, 'bold'))])
        # ---- 输入控件: 白底 + 内边距, 焦点给蓝框 ----
        st.configure('TEntry', fieldbackground='#ffffff', bordercolor=self.BORDER,
                     padding=(7, 5), insertcolor=self.TEXT)
        st.map('TEntry', bordercolor=[('focus', self.PRIMARY)],
               lightcolor=[('focus', self.PRIMARY)],
               darkcolor=[('focus', self.PRIMARY)])
        st.configure('TCombobox', fieldbackground='#ffffff', background='#ffffff',
                     bordercolor=self.BORDER, padding=(6, 4), arrowcolor=self.MUTED)
        st.configure('TSpinbox', fieldbackground='#ffffff', bordercolor=self.BORDER,
                     padding=(6, 4), arrowcolor=self.MUTED, arrowsize=12)
        st.configure('TCheckbutton', background=self.BG, foreground=self.TEXT,
                     focuscolor=self.BG)
        st.map('TCheckbutton', background=[('active', self.BG)])
        # ---- 表格 ----
        st.configure('Treeview', rowheight=27, background='#ffffff',
                     fieldbackground='#ffffff', bordercolor=self.BORDER,
                     font=(fam, 10))
        st.configure('Treeview.Heading', font=(fam, 10, 'bold'),
                     background='#f1f3f6', foreground=self.TEXT,
                     relief='flat', padding=(6, 5))
        st.map('Treeview', background=[('selected', '#dbe7ff')],
               foreground=[('selected', self.TEXT)])
        st.configure('Vertical.TScrollbar', background='#e6e8ec',
                     troughcolor=self.BG, bordercolor=self.BG,
                     arrowcolor=self.MUTED, relief='flat')
        st.configure('TSeparator', background=self.BORDER)

    def _mark_dirty(self, *_a):
        """界面上任何一处被改动 -> 右上角亮"未保存", 免得改完忘了点保存。"""
        if getattr(self, '_loading', False):
            return
        if not self._dirty:
            self._dirty = True
            self._refresh_dirty()

    def _refresh_dirty(self):
        try:
            self.dirty_var.set('● 有未保存的改动' if self._dirty else '')
        except Exception:                                    # noqa: BLE001
            pass

    def _var(self, app, path, typ):
        key = (app, path)
        if key not in self.vars:
            raw = jget(self.cfg[app], path)
            if typ == 'bool':
                v = tk.BooleanVar(value=bool(raw))
            elif typ == 'list':
                v = tk.StringVar(value=', '.join(str(x) for x in (raw or [])))
            else:
                v = tk.StringVar(value='' if raw is None else str(raw))
            self.vars[key] = v
            self.field_types[key] = typ
            self.initial[key] = v.get()
            v.trace_add('write', self._mark_dirty)
        return self.vars[key]

    def _fill(self):
        self._loading = True
        for (app, path), typ in self.field_types.items():
            raw = jget(self.cfg[app], path)
            v = self.vars[(app, path)]
            if typ == 'bool':
                v.set(bool(raw))
            elif typ == 'list':
                v.set(', '.join(str(x) for x in (raw or [])))
            else:
                v.set('' if raw is None else str(raw))
            self.initial[(app, path)] = v.get()
        for app, w in self.prompt_text.items():
            w.delete('1.0', 'end')
            w.insert('1.0', self.cfg[app].get('system_prompt') or '')
        self._fill_mm()
        self._fill_wl()
        self._refresh_previews()
        self._loading = False
        self._dirty = False
        self._refresh_dirty()

    # ---------- 骨架 ----------
    def _build(self):
        # 顶栏: 左边标题, 右边"未保存"提示 + 两个操作。上下各压一条分隔线,
        # 让标题区和页签区分开, 不然一片灰底看不出边界。
        top = ttk.Frame(self, padding=(18, 14, 18, 12))
        top.pack(fill='x')
        ttk.Label(top, text='wxduo 控制台', style='H1.TLabel').pack(side='left')
        ttk.Label(top, text='Ctrl+S 保存 · Ctrl+F 搜索白名单',
                  style='Sub.TLabel').pack(side='left', padx=(12, 0), pady=(6, 0))
        # 保存按钮放右上角(底部那条也留着): 小屏/任务栏高的机器上底部会被裁掉
        ttk.Button(top, text='保存并重启', command=self.save_and_restart).pack(
            side='right', padx=(10, 0))
        ttk.Button(top, text='保存', command=self.save,
                   style='Accent.TButton').pack(side='right')
        self.dirty_var = tk.StringVar(value='')
        ttk.Label(top, textvariable=self.dirty_var,
                  style='Dirty.TLabel').pack(side='right', padx=(0, 12))
        self.bind_all('<Control-s>', lambda e: self.save())
        ttk.Separator(self, orient='horizontal').pack(fill='x')

        nb = ttk.Notebook(self)
        self.nb = nb
        nb.pack(fill='both', expand=True, padx=14, pady=(12, 8))
        self._tab_service(nb)
        self._tab_model(nb)
        self._tab_cloud(nb)
        self._tab_prompt(nb)
        self._tab_first(nb)
        self._tab_whitelist(nb)
        self._tab_misc(nb)

        ttk.Separator(self, orient='horizontal').pack(fill='x')
        bar = ttk.Frame(self, padding=(18, 10, 18, 12))
        bar.pack(fill='x')
        ttk.Button(bar, text='保存', command=self.save,
                   style='Accent.TButton').pack(side='left')
        ttk.Button(bar, text='保存并重启',
                   command=self.save_and_restart).pack(side='left', padx=8)
        ttk.Button(bar, text='重新载入',
                   command=self.reload_all).pack(side='left')
        self.status_var = tk.StringVar(value='就绪')
        ttk.Label(bar, textvariable=self.status_var,
                  style='Ok.TLabel').pack(side='right')

    #
    # 小工具: 让每个 tab 的布局代码短一点
    #   pady 统一 5: 原来 3 太挤, 密集表单里每行贴着走, 眼睛找不到行
    #
    @staticmethod
    def _lab(parent, text, r, c=0, sticky='w', **kw):
        w = ttk.Label(parent, text=text, **kw)
        w.grid(row=r, column=c, sticky=sticky, padx=(0, 10), pady=5)
        return w

    def _entry(self, parent, app, path, r, c=1, width=26, typ='str'):
        w = ttk.Entry(parent, textvariable=self._var(app, path, typ), width=width)
        w.grid(row=r, column=c, sticky='w', pady=5)
        return w

    def _spin(self, parent, app, path, r, lo=0, hi=999999, step=1, c=1, width=8):
        typ = 'float' if isinstance(step, float) else 'int'
        w = ttk.Spinbox(parent, from_=lo, to=hi, increment=step, width=width,
                        textvariable=self._var(app, path, typ))
        w.grid(row=r, column=c, sticky='w', pady=5)
        return w

    def _check(self, parent, app, path, text, r, c=0, span=2):
        w = ttk.Checkbutton(parent, text=text, variable=self._var(app, path, 'bool'))
        w.grid(row=r, column=c, columnspan=span, sticky='w', pady=4)
        return w

    def _combo(self, parent, app, path, r, values, c=1, width=24):
        w = ttk.Combobox(parent, textvariable=self._var(app, path, 'str'),
                         values=values, width=width)
        w.grid(row=r, column=c, sticky='w', pady=5)
        return w

    #
    # Tab 1: 服务
    #
    def _tab_service(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text='服务')
        # 用 grid + 权重: pack 的 expand 在这里没把日志区拉到底(底部留了一大片
        # 灰底), 而日志是这一页最主要的信息, 必须吃掉剩余高度
        f.columnconfigure(0, weight=1)
        f.rowconfigure(3, weight=1)

        box = ttk.LabelFrame(f, text='当前状态', padding=10)
        box.grid(row=0, column=0, sticky='ew')
        self.lbl_main = ttk.Label(box, text='大号：检查中…')
        self.lbl_mini = ttk.Label(box, text='小号：检查中…')
        self.lbl_env = ttk.Label(box, text='环境：检查中…')
        for i, w in enumerate((self.lbl_main, self.lbl_mini, self.lbl_env)):
            w.grid(row=i, column=0, sticky='w', pady=3)

        # 启停按"对象"分组: 左边大号一对, 中间小号一对, 右边全局操作,
        # 七个按钮排成一行时最容易点错的就是"停止大号"和"停止小号"
        btns = ttk.Frame(f)
        btns.grid(row=1, column=0, sticky='ew', pady=12)
        ttk.Button(btns, text='启动大号', command=lambda: self._start('main')).pack(side='left')
        ttk.Button(btns, text='停止大号',
                   command=lambda: self._stop('main')).pack(side='left', padx=(6, 0))
        ttk.Separator(btns, orient='vertical').pack(side='left', fill='y', padx=14)
        ttk.Button(btns, text='启动小号', command=lambda: self._start('mini')).pack(side='left')
        ttk.Button(btns, text='停止小号',
                   command=lambda: self._stop('mini')).pack(side='left', padx=(6, 0))
        ttk.Separator(btns, orient='vertical').pack(side='left', fill='y', padx=14)
        ttk.Button(btns, text='全部重启', command=self._restart_all).pack(side='left')
        ttk.Button(btns, text='全部停止', command=self._stop_all).pack(side='left', padx=(6, 0))
        ttk.Button(btns, text='刷新状态',
                   command=self.refresh_status_async).pack(side='right')

        auto = ttk.LabelFrame(f, text='开机自启', padding=10)
        auto.grid(row=2, column=0, sticky='ew')
        ttk.Label(auto, text='登录 Windows 后在后台静默拉起，不弹窗；'
                             '写入 HKCU\\...\\Run，不需要管理员。',
                  style='Sub.TLabel').grid(row=0, column=0, columnspan=3,
                                           sticky='w', pady=(0, 8))
        self.auto_vars = {k: tk.BooleanVar(value=False) for k in AUTO_ENTRIES}
        self.auto_info = {k: self._auto_probe(k) for k in AUTO_ENTRIES}
        self.auto_state = {}
        for i, (key, name) in enumerate((('main', '大号'), ('mini', '小号'),
                                         ('ollama', 'ollama 服务')), start=1):
            ttk.Checkbutton(auto, text=name, variable=self.auto_vars[key],
                            command=lambda k=key: self._toggle_autostart(k)
                            ).grid(row=i, column=0, sticky='w', padx=(0, 16))
            ttk.Label(auto, text=self.auto_info[key][0],
                      style='Sub.TLabel').grid(row=i, column=1, sticky='w')
            lb = ttk.Label(auto, text='')
            lb.grid(row=i, column=2, sticky='w', padx=(18, 0))
            self.auto_state[key] = lb
        auto.columnconfigure(1, weight=1)
        self.after(0, self._refresh_auto_rows)

        lg = ttk.LabelFrame(f, text='日志', padding=8)
        lg.grid(row=3, column=0, sticky='nsew', pady=(12, 0))
        row = ttk.Frame(lg)
        row.pack(fill='x')
        self.log_name = tk.StringVar(value='小号 run.log')
        self.log_map = {
            '小号 run.log': os.path.join(APP_DIR['mini'], 'run.log'),
            '小号 guard.log': os.path.join(APP_DIR['mini'], 'guard.log'),
            '大号 run.log': os.path.join(APP_DIR['main'], 'run.log'),
        }
        ttk.Combobox(row, textvariable=self.log_name, values=list(self.log_map),
                     width=16, state='readonly').pack(side='left')
        ttk.Button(row, text='刷新', command=self._show_log).pack(side='left', padx=6)
        ttk.Button(row, text='打开文件夹', command=self._open_log_dir).pack(side='left')
        # tk.Text 不吃 ttk 样式, 得单独配色; 等宽字体才能让日志的时间戳对齐
        self.log_text = tk.Text(lg, height=16, wrap='none', undo=False,
                                font=('Consolas', 10), bg='#ffffff',
                                fg='#24292f', relief='flat', bd=0,
                                highlightthickness=1,
                                highlightbackground=self.BORDER,
                                highlightcolor=self.PRIMARY,
                                insertbackground=self.TEXT, padx=8, pady=6)
        sb = ttk.Scrollbar(lg, orient='vertical', command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side='left', fill='both', expand=True, pady=(6, 0))
        sb.pack(side='right', fill='y', pady=(6, 0))

    #
    # Tab 2: 模型
    #
    def _tab_model(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text='模型')

        m = ttk.LabelFrame(f, text='大号（真人对话）', padding=10)
        m.pack(fill='x')
        self._check(m, 'main', 'llm_enabled', '启用本地大模型（关掉=只用知识库+搜索）', 0, span=4)
        self._lab(m, '主模型', 1)
        self.cb_main_model = self._combo(m, 'main', 'llm_model', 1, ['(还没读到 ollama)'])
        self.cb_main_model._wx_model = True      # 标记: 这只下拉吃 ollama 模型列表
        ttk.Button(m, text='刷新模型列表', command=self._reload_models).grid(row=1, column=2, padx=8)
        self._lab(m, '本地兜底（只一个）', 2)
        self.cb_fb_model = self._combo(m, 'main', 'llm_fallback_model', 2,
                                       [''] + (self.models or
                                               ['(还没读到 ollama)']), width=30)
        ttk.Label(m, text='云端全挂才用它；清空就复用主模型',
                  style='Sub.TLabel').grid(row=2, column=2, columnspan=2, sticky='w', padx=8)
        self._lab(m, '超时(秒)', 3)
        self._spin(m, 'main', 'llm_timeout', 3, 10, 600)
        self._lab(m, '上下文', 3, c=2)
        self._spin(m, 'main', 'llm_num_ctx', 3, 1024, 65536, 1024, c=3)
        self._lab(m, '显存保活(秒)', 4)
        self._spin(m, 'main', 'llm_keep_alive', 4, -1, 3600, 60)
        self._check(m, 'main', 'llm_warmup', '启动时预热模型（第一条消息不用干等）', 4, c=2, span=2)

        s = ttk.LabelFrame(f, text='小号（桌面助手）', padding=10)
        s.pack(fill='x', pady=(10, 0))
        self._lab(s, '主用来源', 0)
        self._combo(s, 'mini', 'llm_primary', 0, ['ollama', 'cloud'])
        self._lab(s, '主模型', 1)
        self.cb_mini_model = self._combo(s, 'mini', 'llm_model', 1, ['(还没读到 ollama)'])
        self.cb_mini_model._wx_model = True
        self._lab(s, '上下文', 2)
        self._spin(s, 'mini', 'llm_num_ctx', 2, 1024, 65536, 1024)
        self._lab(s, '显存层数', 2, c=2)
        self._spin(s, 'mini', 'llm_num_gpu', 2, -1, 999, 1, c=3)
        self._lab(s, '保活(秒)', 3)
        self._spin(s, 'mini', 'llm_keep_alive', 3, -1, 3600, 60)
        self._lab(s, '单条上限', 3, c=2)
        self._spin(s, 'mini', 'llm_num_predict', 3, 64, 4096, 64, c=3)
        self._check(s, 'mini', 'llm_think', '开思考链（慢，但难题更稳）', 4, span=2)
        self._check(s, 'mini', 'llm_warmup', '启动时预热', 4, c=2, span=2)
        self._check(s, 'mini', 'fast_reply', '快答通道（短消息直接回，不走工具）', 5, span=4)

        g = ttk.LabelFrame(f, text='小号 · 多模型群聊（同一段聊天记录并行问多个模型，各自带前缀回）',
                           padding=10)
        g.pack(fill='both', expand=True, pady=(10, 0))
        self._check(g, 'mini', 'multi_model.enabled', '启用', 0, span=1)
        self._lab(g, '超时', 0, c=1)
        self._spin(g, 'mini', 'multi_model.timeout', 0, 5, 300, 1, c=2)
        self._lab(g, '单条上限', 0, c=3)
        self._spin(g, 'mini', 'multi_model.num_predict', 0, 64, 4096, 64, c=4)
        cols = ('on', 'name', 'prefix', 'via', 'model')
        self.mm_tree = ttk.Treeview(g, columns=cols, show='headings', height=5)
        for col, txt, w in (('on', '启用', 50), ('name', '名字', 120), ('prefix', '前缀', 110),
                            ('via', '来源', 70), ('model', '模型', 260)):
            self.mm_tree.heading(col, text=txt)
            self.mm_tree.column(col, width=w, anchor='w')
        self.mm_tree.grid(row=1, column=0, columnspan=5, sticky='nsew', pady=6)
        # minsize: 窗口矮的时候 Treeview 会被上面两块挤成 0 高, 整块表格凭空
        # 消失(看着像功能没了), 给它留一个最小高度
        g.rowconfigure(1, weight=1, minsize=110)
        g.columnconfigure(4, weight=1)
        self.mm_tree.bind('<Double-1>', lambda e: self._mm_edit())
        mb = ttk.Frame(g)
        mb.grid(row=2, column=0, columnspan=5, sticky='w')
        ttk.Button(mb, text='添加', command=self._mm_add).pack(side='left')
        ttk.Button(mb, text='编辑', command=self._mm_edit).pack(side='left', padx=4)
        ttk.Button(mb, text='删除', command=self._mm_del).pack(side='left')
        ttk.Button(mb, text='上移', command=lambda: self._mm_move(-1)).pack(side='left', padx=(12, 2))
        ttk.Button(mb, text='下移', command=lambda: self._mm_move(1)).pack(side='left')

    #
    # Tab 3: 云端线路(GLM / DeepSeek)
    #
    def _tab_cloud(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text='云端线路')
        ttk.Label(f, text='链路：GLM 先上 → DeepSeek 顶第二条 → 两条都不行才落本地兜底模型。'
                          '每条线路内部先试主模型，再试备用模型。',
                  style='Sub.TLabel').pack(anchor='w')
        bar = ttk.Frame(f)
        bar.pack(fill='x', pady=(8, 10))
        ttk.Button(bar, text='从 CodeBuddy 导入 key',
                   command=self._import_keys).pack(side='left')
        ttk.Button(bar, text='测连通', command=self._test_cloud).pack(side='left', padx=8)
        self.cloud_msg = tk.StringVar(value='')
        ttk.Label(bar, textvariable=self.cloud_msg,
                  style='Info.TLabel').pack(side='left', padx=6)

        for pid in CLOUD_IDS:
            box = ttk.LabelFrame(f, text=CLOUD_TITLES[pid], padding=10)
            box.pack(fill='x', pady=(0, 10))
            self._check(box, 'main', 'cloud_providers.%s.enabled' % pid,
                        '大号启用', 0, span=2)
            self._check(box, 'mini', 'cloud_providers.%s.enabled' % pid,
                        '小号启用', 0, c=2, span=2)
            self._lab(box, '主模型', 1)
            self._combo(box, 'main', 'cloud_providers.%s.model' % pid, 1,
                        CLOUD_MODELS[pid], width=22)
            self._lab(box, '备用模型', 1, c=2)
            self._combo(box, 'main', 'cloud_providers.%s.fallback_model' % pid, 1,
                        [''] + CLOUD_MODELS[pid], c=3, width=22)
            self._lab(box, '接口地址', 2)
            self._entry(box, 'main', 'cloud_providers.%s.base_url' % pid, 2,
                        width=42)
            self._lab(box, '超时(秒)', 2, c=2)
            self._spin(box, 'main', 'cloud_providers.%s.timeout' % pid, 2,
                       5, 600, 5, c=3)
            self._lab(box, 'API Key', 3)
            ent = ttk.Entry(box, width=52, show='*',
                            textvariable=self._var('main', 'cloud_providers.%s.api_key' % pid, 'str'))
            ent.grid(row=3, column=1, columnspan=2, sticky='w', pady=3)
            ttk.Button(box, text='显示', width=6,
                       command=lambda w=ent: w.configure(
                           show='' if w.cget('show') else '*')
                       ).grid(row=3, column=3, sticky='w', padx=6)

        ttk.Label(f, text='两号的线路参数共用一份（开关各自独立）；API Key 只存在本机 '
                          'config.json，控制台不会写进日志。',
                  style='Sub.TLabel').pack(anchor='w', pady=(4, 0))

    def _import_keys(self):
        """把 CodeBuddy models.json 里的 key 填进表单(还得点保存才落盘)。"""
        keys = cb_provider_keys()
        got, kept = [], []
        for pid in CLOUD_IDS:
            src = keys.get(pid) or {}
            if not src:
                continue
            var = self._var('main', 'cloud_providers.%s.api_key' % pid, 'str')
            if src.get('api_key') and not var.get().strip():
                var.set(src['api_key'])
                got.append(pid)
            elif src.get('api_key'):
                # 已经有 key 就不覆盖: CodeBuddy 里那把智谱 key 直连官方会 401,
                # 而这里原本那把是通的, 一键导入不该把它冲掉。
                kept.append(pid)
            if src.get('base_url'):
                self._var('main', 'cloud_providers.%s.base_url' % pid,
                          'str').set(src['base_url'])
        self._mark_dirty()
        if got:
            self.cloud_msg.set('已填入 %s 的 key，别忘了保存' % '、'.join(
                CLOUD_TITLES[p] for p in got))
        elif kept:
            self.cloud_msg.set('%s 已经有 key，没有覆盖（要换就手动粘贴）'
                               % '、'.join(CLOUD_TITLES[p] for p in kept))
        else:
            self.cloud_msg.set('CodeBuddy 的 models.json 里没找到 GLM / DeepSeek 的 key')

    def _test_cloud(self):
        """按表单里的值真发一句"在", 结果回到 cloud_msg(不落盘、不改配置)。"""
        jobs = []
        for pid in CLOUD_IDS:
            if not self.vars[('main', 'cloud_providers.%s.enabled' % pid)].get():
                continue
            jobs.append((pid,
                         self.vars[('main', 'cloud_providers.%s.base_url' % pid)].get().strip(),
                         self.vars[('main', 'cloud_providers.%s.api_key' % pid)].get().strip(),
                         self.vars[('main', 'cloud_providers.%s.model' % pid)].get().strip()))
        if not jobs:
            self.cloud_msg.set('没有启用的线路')
            return
        self.cloud_msg.set('测试中…')

        def work():
            out = []
            for pid, base, key, model in jobs:
                t0 = time.time()
                try:
                    req = urllib.request.Request(
                        base.rstrip('/') + '/chat/completions',
                        data=json.dumps({'model': model, 'max_tokens': 8,
                                         'messages': [{'role': 'user',
                                                       'content': '在'}]}).encode('utf-8'),
                        headers={'Content-Type': 'application/json',
                                 'Authorization': 'Bearer ' + key})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        json.loads(r.read().decode('utf-8'))
                    out.append('%s 通 %.1fs' % (CLOUD_TITLES[pid], time.time() - t0))
                except Exception as e:                         # noqa: BLE001
                    out.append('%s 失败(%s)' % (CLOUD_TITLES[pid], str(e)[:50]))
            msg = ' ｜ '.join(out)
            self.after(0, lambda: self.cloud_msg.set(msg))

        threading.Thread(target=work, daemon=True).start()

    #
    # Tab 4: 系统提示词
    #
    def _tab_prompt(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text='提示词')
        ttk.Label(f, text='留空 = 用代码里的默认人设（config.py 里那份）。'
                          '大号这里写的会替换默认的开头部分，末尾那几条硬规矩仍然追加。',
                  style='Sub.TLabel').pack(anchor='w', pady=(0, 6))
        sub = ttk.Notebook(f)
        sub.pack(fill='both', expand=True)
        for app, name, hint in (('main', '大号', '真人对话口吻；越短模型越听话'),
                                ('mini', '小号', '桌面助手的干活口吻；写清什么时候调工具')):
            page = ttk.Frame(sub, padding=8)
            sub.add(page, text='%s system_prompt' % name)
            ttk.Label(page, text=hint, style='Sub.TLabel').pack(anchor='w')
            txt = tk.Text(page, wrap='word', undo=True, height=18)
            sb = ttk.Scrollbar(page, orient='vertical', command=txt.yview)
            txt.configure(yscrollcommand=sb.set)
            txt.pack(side='left', fill='both', expand=True, pady=(6, 0))
            sb.pack(side='right', fill='y', pady=(6, 0))
            ttk.Button(page, text='清空(改用默认)',
                       command=lambda t=txt: (t.delete('1.0', 'end'))).pack(anchor='w', pady=(6, 0))
            self.prompt_text[app] = txt

    #
    # Tab 4: 固定话术
    #
    def _tab_first(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text='固定话术')
        ttk.Label(f, text='对方这条会话第一次来消息时，先回这一句（只回一次，之后正常对话）。'
                          '模板非空时优先用模板，{prefix}/{text}/{suffix} 是占位符。',
                  style='Sub.TLabel').pack(anchor='w', pady=(0, 6))
        self.preview_vars = {}
        for app, name in (('main', '大号'), ('mini', '小号')):
            box = ttk.LabelFrame(f, text=name, padding=10)
            box.pack(fill='x', pady=(0, 8))
            self._check(box, app, 'first_reply.enabled', '启用', 0, span=4)
            self._lab(box, '前缀', 1)
            self._entry(box, app, 'first_reply.prefix', 1, width=14)
            self._lab(box, '正文', 1, c=2)
            self._entry(box, app, 'first_reply.text', 1, width=46, c=3)
            self._lab(box, '后缀', 2)
            self._entry(box, app, 'first_reply.suffix', 2, width=14)
            self._lab(box, '模板(可空)', 2, c=2)
            self._entry(box, app, 'first_reply.template', 2, width=46, c=3)
            self._lab(box, '生效名单(逗号分隔,@名)', 3)
            self._entry(box, app, 'first_reply.users', 3, width=34, typ='list')
            self._lab(box, '过期(小时)', 3, c=2)
            self._spin(box, app, 'first_reply.reset_hours', 3, 0, 720, 0.5, c=3)
            pv = tk.StringVar(value='')
            ttk.Label(box, textvariable=pv, style='Info.TLabel').grid(
                row=4, column=0, columnspan=4, sticky='w', pady=(6, 0))
            self.preview_vars[app] = pv
            # 首发记录: 谁已经收过那句固定话术。想重测"首发到底多快"就得先清掉,
            # 否则同一个人再来消息会直接进 AI, 永远测不到首发那一条。
            hint = tk.StringVar(value='')
            self.first_hint[app] = hint
            ttk.Label(box, textvariable=hint, style='Sub.TLabel').grid(
                row=5, column=0, columnspan=2, sticky='w', pady=(6, 0))
            ttk.Button(box, text='重置首发记录',
                       command=lambda a=app: self._reset_first(a)).grid(
                row=5, column=3, sticky='e', pady=(6, 0))
            for path in ('prefix', 'text', 'suffix', 'template', 'users', 'enabled'):
                self._var(app, 'first_reply.' + path,
                          'list' if path == 'users' else
                          ('bool' if path == 'enabled' else 'str')
                          ).trace_add('write', lambda *_a: self._refresh_previews())

    # ---------- 首发记录: 重置后能重测"首次话术到底多快" ----------
    # 这台机器上现在同时跑着两份副本: 桌面的 wareply(大号) 和 wxduo/chains/chat(大号)。
    # 原来只按"有个 main.py 在跑"就认目录、不看是哪个号, 于是点小号重置也会去清
    # 大号那份 state.json —— 用户看到的正是"大号小号反了"。
    # 现在先认出这个目录登录的是谁(账号指纹), 只清同一个号的目录。
    def _cfg_of(self, d):
        try:
            return load_json(os.path.join(d, 'config.json'), {}) or {}
        except Exception:                                    # noqa: BLE001
            return {}

    @staticmethod
    def _wxid_of(cfg):
        m = re.search(r'(wxid_[A-Za-z0-9]+)', cfg.get('wx_root') or '')
        return m.group(1).lower() if m else ''

    def _fingerprint(self, d):
        """(wxid, 昵称) —— 这个目录登录的是哪个号。"""
        c = self._cfg_of(d)
        return self._wxid_of(c), (c.get('wechat_self_name') or '').strip()

    def _same_account(self, d1, d2):
        if os.path.normcase(os.path.abspath(d1)) == os.path.normcase(os.path.abspath(d2)):
            return True
        w1, n1 = self._fingerprint(d1)
        w2, n2 = self._fingerprint(d2)
        if w1 and w2 and w1 == w2:
            return True
        if n1 and n2 and n1 == n2:
            return True
        # 一边写了 wx_root、另一边只写了昵称时, 借"谁在跟谁聊"搭桥:
        # 某个号的 target_username/target_name 正好指向对方, 两者就是同一个号。
        for d in APP_DIR.values():
            c = self._cfg_of(d)
            tw = (c.get('target_username') or '').strip().lower()
            tn = (c.get('target_name') or '').strip()
            if w1 and n2 and tw == w1 and tn == n2:
                return True
            if w2 and n1 and tw == w2 and tn == n1:
                return True
        return False

    def _running_dirs(self):
        """所有正在跑的 main.py 所在目录(不区分是哪个号)。"""
        try:
            import psutil
        except ImportError:
            return []
        out = []
        try:
            for p in psutil.process_iter(['pid', 'name', 'cmdline']):
                if (p.info.get('name') or '').lower() not in ('python.exe', 'pythonw.exe'):
                    continue
                cmd = p.info.get('cmdline') or []
                if not any(str(c).lower().endswith('main.py') for c in cmd):
                    continue
                try:
                    d = p.cwd()
                except Exception:                            # noqa: BLE001
                    continue
                if d and os.path.exists(os.path.join(d, 'state.json')) and d not in out:
                    out.append(d)
        except Exception:                                    # noqa: BLE001
            pass
        return out

    def _targets(self, app):
        """要动的目录: 该号的默认目录 + 所有"同一个号"的运行副本。"""
        out = []
        for d in [APP_DIR[app]] + self._running_dirs():
            if d in out:
                continue
            if d == APP_DIR[app] or self._same_account(d, APP_DIR[app]):
                out.append(d)
        return out

    def _dir_tag(self, d):
        w, n = self._fingerprint(d)
        who = n or w or '未知账号'
        return '%s（%s）' % (d, who)

    def _read_first(self, app):
        rec = {}
        for d in self._targets(app):
            try:
                st = load_json(os.path.join(d, 'state.json'), {}) or {}
            except Exception:                                # noqa: BLE001
                continue
            r = st.get('_first_reply') or {}
            if isinstance(r, dict) and len(r) > len(rec):
                rec = r
        return rec

    def _refresh_first_hint(self):
        for app, var in (getattr(self, 'first_hint', None) or {}).items():
            n = len(self._read_first(app))
            ds = [d for d in self._targets(app)
                  if os.path.exists(os.path.join(d, 'state.json'))]
            d = ds[0] if ds else APP_DIR[app]
            who = self._fingerprint(d)[1] or self._fingerprint(d)[0] or os.path.basename(d)
            var.set('已收过首发的会话: %d 个　（目录: %s / %s）'
                    % (n, os.path.basename(d), who)
                    if n else
                    '暂无首发记录，谁发消息都会先收到固定话术　（目录: %s / %s）'
                    % (os.path.basename(d), who))

    def _reset_first(self, app):
        name = {'main': '大号', 'mini': '小号'}.get(app, app)
        rec = self._read_first(app)
        dirs = self._targets(app)
        if not rec:
            messagebox.showinfo('重置首发记录',
                                '%s 本来就没有首发记录，不用重置。' % name)
            return
        # 每个目录都带上账号指纹: 用户一眼就能看出有没有点错号
        detail = '\n'.join('  %s\n      记录 %d 条' % (
            self._dir_tag(d),
            len((load_json(os.path.join(d, 'state.json'), {}) or {})
                .get('_first_reply') or {}))
            for d in dirs)
        if not messagebox.askyesno(
                '重置首发记录',
                '%s 有 %d 个会话已经收过首发话术。\n'
                '清空后他们下次再发消息，会重新收到那条固定话术。\n\n'
                '要改的目录：\n%s\n\n'
                '服务不用重启：它看到标记文件就会自己清内存里的记录。\n\n继续？'
                % (name, len(rec), detail)):
            return
        done, fail = [], []
        for d in dirs:
            p = os.path.join(d, 'state.json')
            try:
                st = load_json(p, {}) or {}
                st.pop('_first_reply', None)
                with open(p, 'w', encoding='utf-8') as f:
                    json.dump(st, f, ensure_ascii=False, indent=2)
                # 标记文件: 服务在跑时内存里还留着旧记录, 下一轮会把整份 state
                # 写回 json, 把刚清掉的又冲回来。它看到标记就先清内存再落盘。
                with open(os.path.join(d, 'reset_first.flag'),
                          'w', encoding='utf-8') as f:
                    f.write(time.strftime('%Y-%m-%d %H:%M:%S'))
                done.append(d)
            except OSError as e:
                fail.append('%s (%s)' % (d, e))
        self._refresh_first_hint()
        if fail:
            messagebox.showerror('部分目录写入失败', '\n'.join(fail))
        try:
            self.status_var.set('%s 首发记录已重置（%d 条，%d 个目录），'
                                '可以让对方再发一条重测了'
                                % (name, len(rec), len(done)))
        except Exception:                                    # noqa: BLE001
            pass

    #
    # Tab 5: 白名单
    #
    def _tab_whitelist(self, nb):
        f = ttk.Frame(nb, padding=10)
        nb.add(f, text='白名单')
        ttk.Label(f, text='大号只回这些会话（键=备注前 8 位数字，值=纯名字，不带 ID）。'
                          '团体名字建议先确认，群里的人不该被单独回。',
                  style='Sub.TLabel').pack(anchor='w', pady=(0, 6))
        bar = ttk.Frame(f)
        bar.pack(fill='x')
        ttk.Label(bar, text='搜索').pack(side='left')
        self.wl_search = tk.StringVar()
        self.wl_entry = ttk.Entry(bar, textvariable=self.wl_search, width=20)
        self.wl_entry.pack(side='left', padx=6)
        self.wl_entry.bind('<KeyRelease>', lambda _e: self._fill_wl())
        self.wl_entry.bind('<Return>', lambda _e: self._wl_focus_first())
        self.wl_entry.bind('<Escape>', lambda _e: (self.wl_search.set(''),
                                                   self._fill_wl()))
        ttk.Button(bar, text='新增 Ctrl+N', command=self._wl_add).pack(side='left', padx=(12, 2))
        ttk.Button(bar, text='批量粘贴导入', command=self._wl_batch).pack(side='left', padx=2)
        ttk.Button(bar, text='删除选中 Del', command=self._wl_del).pack(side='left', padx=2)
        ttk.Button(bar, text='复制全部', command=self._wl_copy).pack(side='left', padx=2)
        ttk.Button(bar, text='导出 txt', command=self._wl_dump).pack(side='left', padx=2)
        self.wl_count = ttk.Label(bar, text='')
        self.wl_count.pack(side='right')

        ttk.Label(f, text='快捷键：Ctrl+F 搜索 · Ctrl+N 新增 · F2 编辑 · Del 删除 · '
                          'Ctrl+C 复制选中 · 双击编辑（可多选后一次删）',
                  style='Sub.TLabel').pack(anchor='w', pady=(4, 0))
        cols = ('id', 'name')
        self.wl_tree = ttk.Treeview(f, columns=cols, show='headings',
                                    selectmode='extended')
        self.wl_tree.heading('id', text='ID（备注前 8 位）')
        self.wl_tree.heading('name', text='备注')
        self.wl_tree.column('id', width=160, anchor='w')
        self.wl_tree.column('name', width=520, anchor='w')
        sb = ttk.Scrollbar(f, orient='vertical', command=self.wl_tree.yview)
        self.wl_tree.configure(yscrollcommand=sb.set)
        self.wl_tree.pack(side='left', fill='both', expand=True, pady=6)
        sb.pack(side='right', fill='y', pady=6)
        self.wl_tree.bind('<Double-1>', lambda _e: self._wl_edit())
        self.wl_tree.bind('<F2>', lambda _e: self._wl_edit())
        self.wl_tree.bind('<Return>', lambda _e: self._wl_edit())
        self.wl_tree.bind('<Delete>', lambda _e: self._wl_del())
        # 全局快捷键: 在别的页签按 Ctrl+F/Ctrl+N 也直接落到白名单上
        self.bind('<Control-f>', lambda _e: self._wl_focus())
        self.bind('<Control-n>', lambda _e: self._wl_add())

    def _wl_focus(self):
        for tab_id in self.nb.tabs():
            if self.nb.tab(tab_id, 'text') == '白名单':
                self.nb.select(tab_id)
                break
        self.wl_entry.focus_set()
        self.wl_entry.selection_range(0, 'end')
        return 'break'

    def _wl_focus_first(self):
        kids = self.wl_tree.get_children()
        if kids:
            self.wl_tree.selection_set(kids[0])
            self.wl_tree.focus(kids[0])
            self.wl_tree.see(kids[0])

    def _wl_copy(self):
        sel = self.wl_tree.selection() or self.wl_tree.get_children()
        txt = '\n'.join('%s\t%s' % (k, self.wl.get(k, '')) for k in sel)
        self.clipboard_clear()
        self.clipboard_append(txt)
        self.status_var.set('已复制 %d 条到剪贴板' % len(sel))

    def _wl_batch(self):
        """一次贴一堆: 每行 "ID 名字", 顺序/分隔符都随意, 只挑得出 6-12 位数字就当 ID。"""
        win = tk.Toplevel(self)
        win.title('批量导入白名单')
        win.transient(self)
        win.grab_set()
        ttk.Label(win, text='每行一条，例如：示例会话 张三\n'
                            'ID 在前或在后都行，空格 / 逗号 / 制表符都认；'
                            '已存在的 ID 会被新名字覆盖。',
                  justify='left', foreground='#555').pack(anchor='w', padx=10, pady=(10, 4))
        txt = tk.Text(win, width=56, height=14, wrap='none')
        txt.pack(fill='both', expand=True, padx=10)
        txt.focus_set()
        msg = tk.StringVar(value='')
        ttk.Label(win, textvariable=msg, foreground='#c60').pack(anchor='w', padx=10)

        def ok():
            rows, bad = parse_wl_lines(txt.get('1.0', 'end'))
            if not rows:
                msg.set('没读到有效行（%d 行不合格）' % bad)
                return
            new = overwrite = 0
            for k, v in rows:
                if k in self.wl:
                    overwrite += 1
                else:
                    new += 1
                self.wl[k] = v
            self._mark_dirty()
            self._fill_wl()
            win.destroy()
            self.status_var.set('批量导入: 新增 %d 条, 覆盖 %d 条%s'
                                % (new, overwrite,
                                   (', 跳过 %d 行' % bad) if bad else ''))

        bb = ttk.Frame(win)
        bb.pack(pady=8)
        ttk.Button(bb, text='导入', command=ok).pack(side='left', padx=6)
        ttk.Button(bb, text='取消', command=win.destroy).pack(side='left')
        win.wait_window()

    #
    # Tab 6: 其它
    #
    def _tab_misc(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text='其它')
        a = ttk.LabelFrame(f, text='大号', padding=10)
        a.pack(fill='x')
        self._check(a, 'main', 'allow_search', '允许联网搜索（关掉=只用本地知识）', 0, span=4)
        self._lab(a, '轮询间隔(秒)', 1)
        self._spin(a, 'main', 'poll_interval', 1, 0.2, 30, 0.2)

        b = ttk.LabelFrame(f, text='小号', padding=10)
        b.pack(fill='x', pady=(10, 0))
        self._check(b, 'mini', 'send_interim', '发送进度话术（“正在…”，会刷屏）', 0, span=4)
        self._check(b, 'mini', 'send_verify', '发送前二次核对窗口', 1, span=4)
        self._check(b, 'mini', 'skills.enabled', '启用技能（SKILL.md 按需加载）', 2, span=4)
        self._check(b, 'mini', 'subagent.enabled', '启用子代理（只读调查，结论回主模型）', 3, span=4)
        self._lab(b, '静止窗口(秒)', 4)
        self._spin(b, 'mini', 'quiet_window', 4, 0, 30, 0.5)
        self._lab(b, '回复长度上限', 4, c=2)
        self._spin(b, 'mini', 'max_reply_len', 4, 50, 2000, 10, c=3)
        self._lab(b, '轮询间隔(秒)', 5)
        self._spin(b, 'mini', 'poll_interval', 5, 0.2, 30, 0.2)

        ttk.Label(f, text='提示：这两页多数改动都要重启对应服务才生效；'
                          '只有轮询间隔这类每轮重读的才免重启。',
                  style='Sub.TLabel').pack(anchor='w', pady=(10, 0))

    # ---------- 模型列表 ----------
    def _reload_models(self):
        self.models = ollama_models()
        vals = self.models or ['(没连上 ollama)']
        self._walk_set_values(vals)
        fb = getattr(self, 'cb_fb_model', None)
        if fb is not None:
            # 兜底只挑一个: 列表里多一个空项 = "用主模型", 比让人手敲错模型名强
            fb.configure(values=[''] + (self.models or ['(没连上 ollama)']))
        self.status_var.set(('ollama 模型 %d 个' % len(self.models)) if self.models
                            else 'ollama 没连上(服务没开?)')

    def _walk_set_values(self, vals):
        """只改标了 _wx_model 的那两只下拉, 别把云端模型的下拉也刷成 qwen。"""
        def rec(w):
            if getattr(w, '_wx_model', False):
                w.configure(values=vals)
            for ch in w.winfo_children():
                rec(ch)
        rec(self)

    # ---------- 群聊表格 ----------
    def _fill_mm(self):
        if not hasattr(self, 'mm_tree'):
            return
        self.mm_tree.delete(*self.mm_tree.get_children())
        for i, m in enumerate(self.mm):
            self.mm_tree.insert('', 'end', iid=str(i), values=(
                '✓' if m.get('enabled') else '',
                m.get('name', ''), m.get('prefix', ''),
                m.get('via', 'local'), m.get('model', '')))

    def _mm_sel(self):
        sel = self.mm_tree.selection()
        return int(sel[0]) if sel else None

    def _mm_add(self):
        self.mm.append({'enabled': True, 'name': '模型%d' % (len(self.mm) + 1),
                        'prefix': '', 'via': 'local',
                        'model': self.models[0] if self.models else ''})
        self._fill_mm()

    def _mm_del(self):
        i = self._mm_sel()
        if i is None:
            return
        del self.mm[i]
        self._fill_mm()

    def _mm_move(self, d):
        i = self._mm_sel()
        if i is None:
            return
        j = i + d
        if 0 <= j < len(self.mm):
            self.mm[i], self.mm[j] = self.mm[j], self.mm[i]
            self._fill_mm()
            self.mm_tree.selection_set(str(j))

    def _mm_edit(self):
        i = self._mm_sel()
        if i is None:
            messagebox.showinfo('提示', '先选中一行')
            return
        m = self.mm[i]
        win = tk.Toplevel(self)
        win.title('编辑模型')
        win.transient(self)
        win.grab_set()
        on = tk.BooleanVar(value=bool(m.get('enabled')))
        name = tk.StringVar(value=m.get('name', ''))
        prefix = tk.StringVar(value=m.get('prefix', ''))
        via = tk.StringVar(value=m.get('via', 'local'))
        model = tk.StringVar(value=m.get('model', ''))
        rows = [('启用', ttk.Checkbutton(win, variable=on)),
                ('名字', ttk.Entry(win, textvariable=name, width=24)),
                ('回复前缀', ttk.Entry(win, textvariable=prefix, width=24)),
                ('来源', ttk.Combobox(win, textvariable=via, values=['local', 'cloud'],
                                     width=22, state='readonly')),
                ('模型', ttk.Combobox(win, textvariable=model,
                                     values=self.models or [''], width=22))]
        for r, (lab, w) in enumerate(rows):
            ttk.Label(win, text=lab).grid(row=r, column=0, sticky='w', padx=8, pady=5)
            w.grid(row=r, column=1, sticky='w', padx=8, pady=5)

        def ok():
            m.update({'enabled': bool(on.get()), 'name': name.get().strip(),
                      'prefix': prefix.get().strip(), 'via': via.get(),
                      'model': model.get().strip()})
            win.destroy()
            self._fill_mm()

        bb = ttk.Frame(win)
        bb.grid(row=len(rows), column=0, columnspan=2, pady=8)
        ttk.Button(bb, text='确定', command=ok).pack(side='left', padx=6)
        ttk.Button(bb, text='取消', command=win.destroy).pack(side='left')
        win.wait_window()

    # ---------- 白名单表格 ----------
    def _fill_wl(self):
        if not hasattr(self, 'wl_tree'):
            return
        kw = self.wl_search.get().strip().lower() if hasattr(self, 'wl_search') else ''
        self.wl_tree.delete(*self.wl_tree.get_children())
        n = 0
        for k in sorted(self.wl, key=lambda x: str(x)):
            v = str(self.wl[k])
            if kw and kw not in k.lower() and kw not in v.lower():
                continue
            self.wl_tree.insert('', 'end', iid=str(k), values=(k, v))
            n += 1
        self.wl_count.configure(text='共 %d 条（显示 %d）' % (len(self.wl), n))

    def _wl_add(self):
        self._wl_dialog(None)

    def _wl_edit(self):
        sel = self.wl_tree.selection()
        if sel:
            k = sel[0]
            self._wl_dialog(k)

    def _wl_dialog(self, key):
        win = tk.Toplevel(self)
        win.title('编辑白名单' if key else '新增白名单')
        win.transient(self)
        win.grab_set()
        kid = tk.StringVar(value=key or '')
        kname = tk.StringVar(value=str(self.wl.get(key, '')) if key else '')
        ttk.Label(win, text='ID（备注前 8 位）').grid(row=0, column=0, padx=8, pady=6, sticky='w')
        ttk.Entry(win, textvariable=kid, width=26).grid(row=0, column=1, padx=8, pady=6)
        ttk.Label(win, text='备注（纯名字）').grid(row=1, column=0, padx=8, pady=6, sticky='w')
        ttk.Entry(win, textvariable=kname, width=26).grid(row=1, column=1, padx=8, pady=6)

        def ok():
            new_id = kid.get().strip()
            new_name = kname.get().strip()
            if not re.match(r'^\d{6,12}$', new_id):
                messagebox.showwarning('格式不对', 'ID 应该是备注开头的数字（一般 8 位）', parent=win)
                return
            if not new_name:
                messagebox.showwarning('缺少备注', '备注别空着，发送时要靠它核对窗口', parent=win)
                return
            if key and key != new_id:
                self.wl.pop(key, None)
            self.wl[new_id] = new_name
            self._mark_dirty()
            win.destroy()
            self._fill_wl()

        bb = ttk.Frame(win)
        bb.grid(row=2, column=0, columnspan=2, pady=8)
        ttk.Button(bb, text='确定', command=ok).pack(side='left', padx=6)
        ttk.Button(bb, text='取消', command=win.destroy).pack(side='left')
        win.wait_window()

    def _wl_del(self):
        sel = self.wl_tree.selection()
        if not sel:
            return
        if not messagebox.askyesno('确认', '从白名单删掉这 %d 个会话？' % len(sel)):
            return
        for k in sel:
            self.wl.pop(k, None)
        self._mark_dirty()
        self._fill_wl()

    def _wl_dump(self):
        path = os.path.join(ROOT, 'whitelist_export.txt')
        with open(path, 'w', encoding='utf-8') as f:
            for k in sorted(self.wl, key=lambda x: str(x)):
                f.write('%s\t%s\n' % (k, self.wl[k]))
        self.status_var.set('已导出 %s' % path)
    # ---------- 收集 & 保存 ----------
    @staticmethod
    def _conv(raw, typ, old):
        if typ == 'bool':
            return bool(raw), None
        s = (raw or '').strip()
        if typ == 'list':
            return [x.strip() for x in re.split(r'[,，、\n]', s) if x.strip()], None
        if typ == 'int':
            try:
                return int(float(s)), None
            except ValueError:
                return old, '要填整数'
        if typ == 'float':
            try:
                v = float(s)
            except ValueError:
                return old, '要填数字'
            return (int(v) if v == int(v) else v), None    # 168 不写成 168.0, 少改一行是一行
        return raw, None

    def collect(self):
        """把界面上的东西写回内存里的 dict; 格式不对的字段保留原值并回报。"""
        warns = []
        for (app, path), typ in self.field_types.items():
            old = jget(self.cfg[app], path, _MISSING)
            raw = self.vars[(app, path)].get()
            val, warn = self._conv(raw, typ, None if old is _MISSING else old)
            if warn:
                warns.append('%s / %s: %s' % (app, path, warn))
            # json 里本来就没这个键、用户也没动它 -> 绝对不要凭空造一个默认值写进去。
            # 踩过: 把 llm_warmup 写成 false / cloud_model 写成 "" 之后, 服务起来就
            # 不走预热、云端模型名变空, 表面上看只是"不预热了", 实际很难查。
            if old is _MISSING and raw == self.initial.get((app, path)):
                continue
            jset(self.cfg[app], path, val)
        for app, w in self.prompt_text.items():
            s = w.get('1.0', 'end-1c').strip()
            if s:
                self.cfg[app]['system_prompt'] = s
            else:
                self.cfg[app].pop('system_prompt', None)
        rows = self._mm_rows()
        if self._mm_was or _mm_key(rows) != getattr(self, '_mm_orig', None):
            jset(self.cfg['mini'], 'multi_model.models', rows)
        # 白名单统一到 whitelist.json, config.json 那份清空 —— 否则两处迟早对不上
        mwl = self.cfg['main'].get('whitelist')
        if isinstance(mwl, dict):
            for k, v in mwl.items():
                self.wl.setdefault(str(k), v)
            if mwl:
                self.cfg['main']['whitelist'] = {}
        # 云端线路: 模型 / url / key / 超时 两号共用一份(小号只留自己的开关)。
        # 同一把 key 在两个 json 里各维护一次, 迟早一边好一边坏。
        for pid in CLOUD_IDS:
            for k in ('base_url', 'api_key', 'model', 'fallback_model', 'timeout'):
                v = jget(self.cfg['main'], 'cloud_providers.%s.%s' % (pid, k), _MISSING)
                if v is not _MISSING:
                    jset(self.cfg['mini'], 'cloud_providers.%s.%s' % (pid, k), v)
        # 老键跟着新开关走: 小号 agent 是看 config.CLOUD_ENABLE 决定"要不要试云端"
        # 的, 不同步的话把两条线路都关掉也白搭。
        for a in APPS:
            jset(self.cfg[a], 'cloud_enable',
                 any(bool(jget(self.cfg[a], 'cloud_providers.%s.enabled' % p))
                     for p in CLOUD_IDS))
            glm = jget(self.cfg[a], 'cloud_providers.glm', None) or {}
            jset(self.cfg[a], 'cloud_model', glm.get('model') or 'glm-4.5-air')
            jset(self.cfg[a], 'cloud_fallback_model',
                 glm.get('fallback_model') or '')
            jset(self.cfg[a], 'cloud_timeout', int(glm.get('timeout') or 60))
        return warns

    def _mm_rows(self):
        """群聊条目落盘用的形状: enabled=true 是默认值, 不写。"""
        rows = []
        for m in self.mm:
            row = dict(m)
            if row.get('enabled'):
                row.pop('enabled', None)
            rows.append(row)
        return rows

    def save(self, quiet=False):
        warns = self.collect()
        changed = []
        if save_json(WL_FILE, self.wl):
            changed.append('whitelist.json')
        for a in APPS:
            if save_json(CFG_FILE[a], self.cfg[a]):
                changed.append('%s/config.json' % a)
        msg = ('已保存: ' + ', '.join(changed)) if changed else '没有改动, 未写文件'
        if warns:
            msg += ' (%d 个字段格式不对, 已保留原值)' % len(warns)
        self.status_var.set(msg)
        self._dirty = False
        self._refresh_dirty()
        if warns and not quiet:
            messagebox.showwarning('部分字段没写进去', '\n'.join(warns[:10]))
        if changed:
            # 存完立刻按磁盘重读一遍: 这样"哪些键本来就在 json 里"和控件的基线
            # 都是最新的, 接着再改一次不会被误判成"没动过"。
            self._load_state()
            self._fill()
        return bool(changed)

    def reload_all(self):
        self._load_state()
        self._fill()
        self.status_var.set('已重新载入')

    def _refresh_previews(self):
        for app, var in getattr(self, 'preview_vars', {}).items():
            fr = {}
            for f in ('prefix', 'text', 'suffix', 'template'):
                v = self.vars.get((app, 'first_reply.' + f))
                fr[f] = v.get() if v else ''
            on = self.vars.get((app, 'first_reply.enabled'))
            users = self.vars.get((app, 'first_reply.users'))
            scope = '全部会话' if not (users and users.get().strip()) else '仅 ' + users.get().strip()
            var.set('%s%s    [%s]' % ('开: ' if (on and on.get()) else '关: ',
                                      build_first_reply(fr), scope))

    # ---------- 服务操作 ----------
    def _start(self, app):
        ok, err = start_app(app)
        self.status_var.set(('已发出启动: %s' % app) if ok else ('启动失败: ' + err))
        self.after(1500, self.refresh_status_async)

    def _stop(self, app, ask=True):
        name = '大号' if app == 'main' else '小号'
        if ask and not messagebox.askyesno('确认', '停掉%s？停了就不再自动回复。' % name):
            return
        self.status_var.set('正在停止%s…' % name)

        def work():
            stop_app(app)
            time.sleep(0.6)
            self.after(0, self.refresh_status_async)
            self.after(0, lambda: self.status_var.set('%s 已停止' % name))
        threading.Thread(target=work, daemon=True).start()

    def _stop_all(self):
        if not messagebox.askyesno('确认', '停掉大号和小号？'):
            return
        self._stop('main', ask=False)
        self._stop('mini', ask=False)

    def _restart_all(self):
        self.status_var.set('正在全部重启…')

        def work():
            for a in APPS:
                stop_app(a)
            time.sleep(1.2)
            for a in APPS:
                start_app(a)
            time.sleep(1.5)
            self.after(0, self.refresh_status_async)
            self.after(0, lambda: self.status_var.set('已重启大号和小号'))
        threading.Thread(target=work, daemon=True).start()

    def save_and_restart(self):
        running = [a for a in APPS if any(service_pids(a))]
        self.save(quiet=True)
        if not running:
            self.status_var.set('已保存; 当前没有服务在跑(去「服务」页点启动)')
            return
        self.status_var.set('已保存, 正在重启: ' + ', '.join(running))

        def work():
            for a in running:
                stop_app(a)
            time.sleep(1.2)
            for a in running:
                start_app(a)
            time.sleep(1.5)
            self.after(0, self.refresh_status_async)
            self.after(0, lambda: self.status_var.set('已保存并重启: ' + ', '.join(running)))
        threading.Thread(target=work, daemon=True).start()

    def _autostart_cmd(self, key):
        if key == 'ollama':
            exe = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Ollama',
                               'ollama app.exe')
            return '"%s"' % exe
        return 'wscript.exe "%s"' % os.path.join(AUTOSTART, 'run_%s.vbs' % key)

    def _auto_probe(self, key):
        """(命令摘要, 自检结论, 这条链路能不能跑)。

        勾选框只能证明"注册表里有个名字", 证明不了开机时它起得来 ——
        bat 被删了、python 换版本了、ollama 装了别处, 勾着也是白搭, 而且要到
        下次开机才发现。这里把实际会执行的命令和链路缺件直接摆在旁边。
        """
        if key == 'ollama':
            exe = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Ollama',
                               'ollama app.exe')
            return ('ollama app.exe',
                    '链路正常' if os.path.exists(exe) else '找不到 ollama app.exe', 
                    os.path.exists(exe))
        vbs = os.path.join(AUTOSTART, 'run_%s.vbs' % key)
        bat = os.path.join(AUTOSTART, 'run_%s.bat' % key)
        if not os.path.exists(vbs):
            return ('run_%s.vbs' % key, '缺 run_%s.vbs，开机起不来' % key, False)
        if not os.path.exists(bat):
            return ('run_%s.vbs' % key, '缺 run_%s.bat，开机起不来' % key, False)
        # bat 里写死的解释器路径最容易过期: python 升个小版本重装一遍,
        # 装到别的目录, 开机就静默失败 —— 注册表还勾着, 但什么都没起来。
        try:
            with open(bat, 'r', encoding='utf-8', errors='ignore') as f:
                body = f.read()
        except OSError:
            return ('run_%s.vbs' % key, '读不到 run_%s.bat' % key, False)
        for py in re.findall(r'"([^"]*?python[^"]*?\.exe)"', body, re.I):
            if not os.path.exists(py):
                return ('run_%s.vbs' % key,
                        '解释器不存在: %s' % os.path.basename(py), False)
        return ('run_%s.vbs' % key, '链路正常', True)

    def _refresh_auto_rows(self):
        """把"登没登记 + 链路通不通"合成一句话, 每个自启项一行。"""
        for k, lb in self.auto_state.items():
            _cmd, msg, ok = self.auto_info[k]
            if not ok:
                lb.configure(text=msg, foreground=self.DANGER)
            elif self.auto_vars[k].get():
                lb.configure(text='已登记 · ' + msg, foreground='#15803d')
            else:
                lb.configure(text='未登记', foreground=self.MUTED)

    def _toggle_autostart(self, key):
        name = AUTO_ENTRIES[key]
        if self.auto_vars[key].get():
            if not reg_set(name, self._autostart_cmd(key)):
                self.auto_vars[key].set(False)
                messagebox.showerror('写注册表失败', '开机自启没写进去(HKCU 也可能被策略拦了)')
        else:
            reg_del(name)
        self._refresh_auto_rows()
        self.refresh_status_async()

    # ---------- 状态 / 日志 ----------
    def refresh_status_async(self):
        def work():
            st = {'main': service_pids('main'), 'mini': service_pids('mini'),
                  'models': ollama_models(), 'wechat': wechat_running(),
                  'auto': reg_autostart()}
            self.status = st
            self.after(0, self._apply_status)
        threading.Thread(target=work, daemon=True).start()

    def _apply_status(self):
        st = self.status or {}
        bm, wm = st.get('main', (None, None))
        bg, wg = st.get('mini', (None, None))
        self.lbl_main.configure(
            text='大号：' + ('运行中  pid=%s' % bm if bm else '已停止'),
            foreground='#15803d' if bm else self.DANGER)
        self.lbl_mini.configure(
            text='小号：' + ('运行中  看门狗=%s 工作=%s' % (bg or '-', wg or '-')
                            if (bg or wg) else '已停止'),
            foreground='#15803d' if (bg or wg) else self.DANGER)
        models = st.get('models') or []
        env = ['ollama ' + ('在线(%d 个模型)' % len(models) if models else '没连上'),
               '微信' + ('在跑' if st.get('wechat') else '没在跑')]
        # ollama 没连上不代表坏了(云端顶上就行), 只有微信没在跑才是真问题,
        # 所以按微信状态上色, 免得整行飘红
        self.lbl_env.configure(text='环境：' + ' | '.join(env),
                              foreground=self.TEXT if st.get('wechat')
                              else self.DANGER)
        auto = st.get('auto') or set()
        for k, v in self.auto_vars.items():
            v.set(AUTO_ENTRIES[k] in auto)
        self._refresh_auto_rows()
        if not self.models and models:
            self.models = models
            self._walk_set_values(models)

    def _show_log(self):
        path = self.log_map.get(self.log_name.get())
        if not path:
            return
        self.log_text.delete('1.0', 'end')
        self.log_text.insert('1.0', tail(path, 300))
        self.log_text.see('end')

    def _open_log_dir(self):
        path = self.log_map.get(self.log_name.get()) or ROOT
        target = path if os.path.exists(path) else os.path.dirname(path)
        subprocess.Popen(['explorer', '/select,' + target.replace('/', '\\')],
                         creationflags=NO_WIN)


def main():
    import traceback
    try:
        Console().mainloop()
    except Exception:                                        # noqa: BLE001
        try:
            with open(os.path.join(ROOT, 'wxduo_console.err.log'), 'a',
                      encoding='utf-8') as f:
                f.write(time.strftime('%Y-%m-%d %H:%M:%S') + '\n')
                f.write(traceback.format_exc() + '\n')
        except Exception:                                    # noqa: BLE001
            pass
        raise


if __name__ == '__main__':
    main()



