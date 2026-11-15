# -*- coding: utf-8 -*-
"""技能机制(SKILL.md) —— 照 CodeBuddy 那套搬过来的最小实现

CodeBuddy 的 skill = 一个目录里的 SKILL.md:

    ---
    name: python-error-triage
    description: 什么时候该用我(给模型看的裁决依据)
    ---
    # 标题
    ## 第 0 步: 先判断接不接
    ## 第 1 步: 取证
    ...
    细化资料放 references/, 需要时再读

机制只有三条, 全部由**模型自己裁决**(和 CodeBuddy 一致):
  1) 只把 name + description 摆进上下文(便宜) —— 对应本模块的 list_skills
  2) 模型觉得该用, 才把正文整段读进来 —— 对应 use_skill
  3) 正文里让"读 references/xxx.md"时, 它用现成的 read_file 去读那个绝对路径

为什么值得做: 本机模型(4B/GLM)每次都在重新发明步骤, 而技能是一份固定的操作流程,
读进来照做比现场推理稳得多 —— 这和 pc_skills.py 的思路同源(把"生成"换成"照着做")。

配置(config.json, 改完重启生效):
  "skills": {"enabled": true, "roots": [], "max_chars": 3000, "refresh_sec": 120}

roots 为空时扫描下列位置(顺序即优先级, 同名技能先到先得):
  ~/.codebuddy/skills                      本机自建技能
  ~/.codebuddy/plugins/marketplaces        插件市场带的技能(目录更深, 递归找)
  <本目录>/.codebuddy/skills               项目内技能
"""
import os
import re
import threading
import time

import config

HOME = os.path.expanduser('~')
_HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    'enabled': True,
    # 单次塞给模型的正文上限。为什么不是"全给": 本地 ollama 的 num_ctx 只有 4096,
    # 一份 6k 字的流程会把整个上下文挤爆, 模型连对方原话都看不到。
    'max_chars': 3000,
    # 技能目录扫描结果的缓存时间。扫描要遍历插件市场那棵树, 不能每条消息都扫。
    'refresh_sec': 120,
    'roots': [],
}

_DEFAULT_ROOTS = [
    os.path.join(HOME, '.codebuddy', 'skills'),
    os.path.join(HOME, '.codebuddy', 'plugins', 'marketplaces'),
    os.path.join(_HERE, '.codebuddy', 'skills'),
]

# 扫描剪枝: 这些目录里不可能藏着技能入口, 进去只是白花时间
_PRUNE = {'node_modules', '.git', '__pycache__', 'dist', 'build', '.venv', 'venv',
          'site-packages', '.next', 'target', 'bin', 'obj'}
_MAX_DEPTH = 6

_FM_RE = re.compile(r'^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?', re.S)
_KV_RE = re.compile(r'^([A-Za-z_][\w\-]*)\s*:\s*(.*)$')
_TITLE_RE = re.compile(r'^\s*#\s+(.+)$', re.M)

_CACHE = {'ts': 0.0, 'items': []}
_LOCK = threading.Lock()


# ============================================================
# 配置
# ============================================================
def settings():
    sc = (config.USER_CFG or {}).get('skills')
    if not isinstance(sc, dict):
        sc = {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in sc:
            out[k] = sc[k]
    out['enabled'] = bool(out['enabled'])
    try:
        out['max_chars'] = max(400, int(out['max_chars'] or 3000))
    except Exception:
        out['max_chars'] = 3000
    try:
        out['refresh_sec'] = max(5, int(out['refresh_sec'] or 120))
    except Exception:
        out['refresh_sec'] = 120
    roots = [str(r) for r in (out.get('roots') or []) if str(r).strip()]
    out['roots'] = roots or list(_DEFAULT_ROOTS)
    return out


# ============================================================
# 解析
# ============================================================
def _parse_front(text):
    """拆 YAML frontmatter, 返回 (meta, 正文)。

    只认最简单的 `key: value` 和折叠块(`description: >` 后面跟缩进行) —— 够用了:
    技能文件的头部就 name / description / description_zh 这几个字段, 嵌套的
    metadata: 块读不准也无所谓, 不影响"这技能是干什么的"。
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta, key, buf = {}, None, []

    def flush():
        if key:
            val = ' '.join(x.strip() for x in buf if x.strip()).strip()
            meta[key] = val.strip().strip('"').strip("'")

    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        h = _KV_RE.match(line)
        if h:
            flush()
            key = h.group(1).strip().lower()
            val = h.group(2).strip()
            buf = [] if val in ('>', '|', '>-', '|-', '>+', '|+') else [val]
            continue
        if key and (line[:1] in (' ', '\t') or line.startswith('- ')):
            buf.append(line)          # 折叠块 / 列表的续行
    flush()
    return meta, text[m.end():]


def _one_skill(path):
    """读一个 SKILL.md, 成功返回 dict, 失败返回 None(坏文件不该拖垮整张表)"""
    d = os.path.dirname(path)
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            raw = f.read()
    except Exception:
        return None
    meta, body = _parse_front(raw)
    name = str(meta.get('name') or '').strip() or os.path.basename(d)
    desc = (str(meta.get('description_zh') or '').strip()
            or str(meta.get('description') or '').strip())
    if not desc:
        t = _TITLE_RE.search(body)
        desc = t.group(1).strip() if t else ''
    return {'name': name, 'desc': desc, 'dir': d, 'path': path,
            'body': body.strip(), 'meta': meta}


def _walk(root, max_depth=_MAX_DEPTH):
    """在 root 下找所有 SKILL.md(深度受限 + 剪枝, 免得把插件市场整个吞下去)"""
    out = []
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == '.' else rel.count(os.sep) + 1
        dirnames[:] = [x for x in dirnames
                       if x.lower() not in _PRUNE and not x.startswith('.git')]
        if 'SKILL.md' in filenames:
            out.append(os.path.join(dirpath, 'SKILL.md'))
        if depth >= max_depth:
            dirnames[:] = []
    return out


def skills(force=False):
    """全部可用技能, 按 roots 顺序去重(同名只留第一个)。结果缓存 refresh_sec 秒。"""
    s = settings()
    now = time.time()
    with _LOCK:
        if (not force and _CACHE['items']
                and now - _CACHE['ts'] < s['refresh_sec']):
            return list(_CACHE['items'])
        seen, items = set(), []
        for rank, root in enumerate(s['roots']):
            for path in _walk(root):
                it = _one_skill(path)
                if not it:
                    continue
                key = it['name'].lower()
                if key in seen:
                    continue
                seen.add(key)
                it['rank'] = rank            # 越靠前的 root 越优先(本机自建 > 插件市场)
                items.append(it)
        # 排序带 rank: list_skills 封顶 25 条时, 先被列出来的必须是本机自建那些技能,
        # 而不是按字母序把 apple-notes / airflow 之类推到最前面。
        items.sort(key=lambda x: (x.get('rank', 99), x['name'].lower()))
        _CACHE.update(ts=now, items=items)
        return list(items)


def find(name):
    """按名字找技能: 精确 -> 前缀 -> 包含。返回 (命中的技能 or None, 候选名列表)。

    模糊一档是必要的: 模型会把 python-error-triage 记成 python-triage。
    但只要有歧义就**不猜**, 把候选摆回去让它重选 —— 猜错技能等于照错流程干活。
    """
    q = str(name or '').strip().lower()
    items = skills()
    if not q:
        return None, [it['name'] for it in items]
    for it in items:
        if it['name'].lower() == q:
            return it, []
    hits = [it for it in items if it['name'].lower().startswith(q)]
    if len(hits) == 1:
        return hits[0], []
    if not hits:
        hits = [it for it in items if q in it['name'].lower()]
    if len(hits) == 1:
        return hits[0], []
    # hits 为空 = 压根没这个技能, 别把 552 个名字当成"候选"甩回去误导它
    return None, [it['name'] for it in hits]


def _refs(d, limit=12):
    """技能目录里的附属资料(references/scripts/...), 给绝对路径让模型自己去读"""
    rows = []
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames[:] = [x for x in dirnames if x.lower() not in _PRUNE]
        for fn in filenames:
            if fn == 'SKILL.md':
                continue
            p = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(p)
            except Exception:
                size = 0
            rows.append((os.path.relpath(p, d), p, size))
            if len(rows) >= limit:
                return rows
    return rows


# ============================================================
# 工具
# ============================================================
_LIST_CAP = 25


def list_skills(keyword=''):
    """列出技能(只有名字 + 一句用途, 便宜)。

    本机插件市场里装着 500+ 个技能, 全列出来 5 万多字, 直接把 num_ctx=4096 挤爆 ——
    所以这里必须**封顶**, 并优先列本机自建技能(roots 顺序靠前的), 再列插件带的。
    关键词过滤是主用法: 模型拿不准时先想一个词(如 python / 前端 / 安全)再查。
    """
    s = settings()
    if not s['enabled']:
        return '没找到技能: 技能机制在 config.json 的 skills.enabled 里被关掉了'
    items = skills()
    if not items:
        return f'没找到技能: 扫过这些目录都没有 SKILL.md -> {", ".join(s["roots"])}'
    total = len(items)
    kw = str(keyword or '').strip().lower()
    if kw:
        items = [it for it in items
                 if kw in it['name'].lower() or kw in it['desc'].lower()]
        if not items:
            return (f'没找到技能: 名字和用途里都不含 {keyword!r} 的(共 {total} 个)。'
                    f'换个词再查, 或者直接按常规做法把活干完')
    head = [f'- {it["name"]}: {(it["desc"] or "(无说明)")[:80]}'
            for it in items[:_LIST_CAP]]
    tail = ''
    if len(items) > _LIST_CAP:
        tail = (f'\n(共 {len(items)} 个, 只列了前 {_LIST_CAP} 个; '
                f'要找特定的就带 keyword 再调一次 list_skills)')
    return ('可用技能(name: 用途), 要用哪个就调 use_skill(name=...):\n'
            + '\n'.join(head) + tail)


def use_skill(name):
    """把某个技能的 SKILL.md 正文读进来"""
    s = settings()
    if not s['enabled']:
        return '没找到技能: 技能机制在 config.json 的 skills.enabled 里被关掉了'
    it, cands = find(name)
    if not it:
        if not cands:
            return (f'没找到技能 {name!r}(本机没这个技能)。先调 list_skills(带一个关键词)'
                    f'拿到准确名字, 或者就不用技能、按常规做法把活干完; 不要自己编技能名。')
        return (f'{name!r} 对不上唯一一个技能, 可能是这些之一: '
                + ', '.join(cands[:12])
                + '。用上面列出的准确名字重新调一次 use_skill; 不要自己编技能名。')
    body = it['body'] or ''
    cap = s['max_chars']
    cut = ''
    if len(body) > cap:
        body = body[:cap].rstrip()
        cut = f'\n…(正文太长, 只给了前 {cap} 字; 剩下的用 read_file 读 {it["path"]})'
    refs = _refs(it['dir'])
    ref_txt = ''
    if refs:
        ref_txt = ('\n\n该技能目录下的资料(要细节就用 read_file 读它的绝对路径):\n'
                   + '\n'.join(f'- {rel}  ({p})' for rel, p, _z in refs))
    return (f'技能 {it["name"]} 已加载。以下是它的操作流程, 照着做, '
            f'不要把它念给对方、也不要贴进正文。\n'
            f'技能目录: {it["dir"]}\n\n{body}{cut}{ref_txt}')


TOOL_SPECS = [
    {
        'type': 'function',
        'function': {
            'name': 'list_skills',
            'description': '列出本机装好的技能(技能 = 一份写好的操作流程文档, '
                           '如修 Python 报错、写前端页面、做安全审计)。'
                           '对方要你做的是一件"有固定套路"的活、你又拿不准步骤时, '
                           '先用它看看有没有现成的流程。只返回名字和一句用途。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'keyword': {'type': 'string',
                                'description': '按关键词过滤, 可省略'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'use_skill',
            'description': '把某个技能的完整流程读进来, 然后照着它一步步做。'
                           'name 必须是 list_skills 里出现过的名字。'
                           '读进来的内容只给自己看 —— 不要复述、不要贴进给对方的正文。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string', 'description': '技能名, 例如 python-error-triage'},
                },
                'required': ['name'],
            },
        },
    },
]

TOOL_FUNCS = {
    'list_skills': list_skills,
    'use_skill': use_skill,
}


if __name__ == '__main__':
    for s in settings()['roots']:
        print(f'[扫描] {s}')
    rows = skills()
    print(f'共 {len(rows)} 个技能')
    for r in rows:
        print(f'  {r["name"]:<32} {r["desc"][:50]}')
    if rows:
        one = use_skill(rows[0]['name'])
        print('\n--- use_skill 预览 ---')
        print(one[:600])
