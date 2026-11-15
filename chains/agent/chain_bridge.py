# -*- coding: utf-8 -*-
"""chain_bridge — 把 WeTrace/chain 的 agent loop 接进小号。

为什么单独一层：
  小号原来的 agent._loop 是"聊天 + 桌面操作"的循环，工具是 pc_tools 那套
  （开窗口、发消息、读写文件）。而指挥链那套是**授权 + 证据**的执行器：
  每个动作都要过 guard、落审计、带 artifact。两者目标不同，不能混进同一个循环。

  所以这里只做一座桥：主循环用 `chain_task` 这个工具把"一件受控的本地任务"
  外包给 chain.agent.loop，拿回结论和证据编号；chain 那边的判定/审计照旧。

安全：
  * 默认关闭（config.json 里 chain_agent.enabled=false），不改变现有行为。
  * 模型调用仍是小号自己的通道（agent._chat_once），本模块不发网络请求。
  * 工具集来自 chain/jobs.json，模型无法新增工具，也无法直接调 shell。
"""
import os
import sys

import config

DEFAULTS = {
    'enabled': False,          # 默认关：不配置就不改变小号现有行为
    'root': '',                # chain 工程根；空 = 自动按本文件位置推
    'max_steps': 6,
    'deadline_s': 120,
    'operator': 'wechat:mini',
    'max_result_len': 1500,
}

_CHAIN = None                  # 延迟导入的 chain 模块句柄
_ERR = ''


def settings():
    sc = (config.USER_CFG or {}).get('chain_agent')
    if not isinstance(sc, dict):
        sc = {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in sc:
            out[k] = sc[k]
    out['enabled'] = bool(out['enabled'])
    try:
        out['max_steps'] = max(1, min(12, int(out['max_steps'])))
    except Exception:
        out['max_steps'] = DEFAULTS['max_steps']
    try:
        out['deadline_s'] = max(15, min(600, float(out['deadline_s'])))
    except Exception:
        out['deadline_s'] = DEFAULTS['deadline_s']
    return out


def _root():
    s = settings()
    if s['root'] and os.path.isdir(s['root']):
        return s['root']
    # chains/agent/chain_bridge.py -> chains/agent -> apps -> wxduo -> Desktop
    here = os.path.dirname(os.path.abspath(__file__))
    desktop = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    cand = os.path.join(desktop, 'WeTrace', 'chain')
    return cand if os.path.isdir(cand) else ''


def _load():
    """导入 chain 包（只导入、不执行任何任务）。失败返回 None 并记原因。"""
    global _CHAIN, _ERR
    if _CHAIN is not None:
        return _CHAIN
    root = _root()
    if not root:
        _ERR = '找不到 chain 工程根（config.json 的 chain_agent.root 没配且默认路径不存在）'
        return None
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from chain.agent import loop as agent_loop      # noqa: F401
        from chain.jobs import load_jobs
        _CHAIN = {'loop': agent_loop, 'load_jobs': load_jobs, 'root': root}
    except Exception as e:                              # noqa: BLE001
        _ERR = f'导入 chain 失败: {type(e).__name__}: {e}'
        return None
    return _CHAIN


def available():
    s = settings()
    if not s['enabled']:
        return False, 'chain_agent 未启用（config.json -> chain_agent.enabled）'
    if _load() is None:
        return False, _ERR
    return True, ''


def _chat(messages, tools=None):
    """把消息交给小号自己的模型通道。tools 为 None 时表示末轮不给工具。"""
    import agent                       # 延迟导入：避免与 agent <-> pc_tools 循环
    return agent._chat_once(messages, tools=tools)


def _trace_text(result):
    if not result.trace:
        return '工具 没调过'
    cnt = {}
    for t in result.trace:
        n = t.get('tool') or '?'
        cnt[n] = cnt.get(n, 0) + 1
    return '工具 ' + ', '.join(f'{k}×{v}' for k, v in cnt.items())


def run_task(task, focus=''):
    """跑一次 chain agent loop，返回给主模型看的文本。永远不抛异常。"""
    ok, why = available()
    if not ok:
        return f'chain_task 不可用：{why}'
    task = str(task or '').strip()
    if not task:
        return 'chain_task 需要 task：写清要让链式代理做的**一件**事。'
    if len(task) > 1200:
        task = task[:1200]
    if focus:
        task += f'\n(重点关注: {str(focus)[:200]})'

    s = settings()
    m = _load()
    cat = m['load_jobs']()
    if cat is None or not cat.valid:
        return 'chain_task 不可用：jobs.json 无效（链式代理没有工具可调）。'

    print(f'[chain] 开工: {task[:80]!r} (步数上限 {s["max_steps"]}, 预算 {s["deadline_s"]}s)')
    try:
        r = m['loop'].run(task, _chat, operator=s['operator'], catalog=cat,
                          max_steps=s['max_steps'], deadline_s=s['deadline_s'])
    except Exception as e:                              # noqa: BLE001
        return f'chain_task 执行异常({type(e).__name__}: {e})，这件事按现有信息自己做。'

    body = (r.final or '').strip()
    head = f'链式代理结论({_trace_text(r)}, {r.elapsed:.0f}s)'
    if r.unverified_claims:
        head += f'[未验证断言: {"/".join(r.unverified_claims)}]'
    if not body:
        return (f'链式代理没给出结论（{r.reason}，{_trace_text(r)}）。'
                f'别把中间结果当答案，按现有信息自己决定怎么回。')
    limit = int(s.get('max_result_len') or 1500)
    if len(body) > limit:
        body = body[:limit].rstrip() + '…'
    print(f'[chain] 完成: {r.summary()}')
    return f'{head}: {body}'


TOOL_SPECS = [
    {
        'type': 'function',
        'function': {
            'name': 'chain_task',
            'description': '把一件**受控的本地任务**交给链式代理执行（它只能调用被授权的具名工具，'
                           '每个动作都会过闸门并落审计）。适合：要读本地源码/证据、要核对版本、'
                           '要做需要留证据链的只读核查。不适合：聊天、发消息、开窗口（用别的工具）。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'task': {'type': 'string',
                             'description': '要它做的那一件事，写清要回答什么'},
                    'focus': {'type': 'string',
                              'description': '可选：特别要盯住的点（路径/关键词）'},
                },
                'required': ['task'],
            },
        },
    },
]

TOOL_FUNCS = {
    'chain_task': run_task,
}
