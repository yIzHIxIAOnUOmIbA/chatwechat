# -*- coding: utf-8 -*-
"""子代理(subagent) —— 照 CodeBuddy 的 Task/subagent 机制搬过来的最小实现

为什么需要: 主助手是"一条消息一个循环", 上下文里塞着对方的原话、历史、22 条规则。
碰上"读三个文件再汇总""搜几轮再下结论"这种活, 这几轮工具往返会把主上下文搅浑,
而且主循环的护栏(交付门禁/进度播报)是给"最后要发给对方一句话"设计的, 不适合中间产物。

于是拆出一层: 主助手调 subagent(task="要独立查清的那件事"), 拿回来的只有**结论**,
它自己的中间过程和一堆工具输出不进主上下文。这和 CodeBuddy 的 Task 子代理同构:
  * 独立的 system 提示词(只做这一件事, 不许问问题, 不许编)
  * 独立的工具表(受控白名单, 见 config.json 的 subagent.tools)
  * 自己的小循环(最多 max_steps 轮), 有总时限
  * 只回一段结论文本

安全边界(为什么白名单里没有 wechat_send / shot_to_wechat / delete_file ...):
子代理没有"对方原话授权"这个上下文, agent._loop 里那几道越权闸(只认 user_text)也没法
套用 —— 那就干脆不给它对外动作和破坏动作。它只负责**查**, 动手永远回到主助手。

配置(config.json, 改完重启生效):
  "subagent": {"enabled": true, "max_steps": 4, "timeout": 90, "num_predict": 512,
               "max_result_len": 1200,
               "tools": ["read_file", "list_files", "web_search", "http_get", "use_skill"]}
"""
import json
import os
import time

import config

DEFAULTS = {
    'enabled': True,
    'max_steps': 4,
    'timeout': 90,          # 整条子任务的总预算(秒), 不是单次请求的超时
    'num_predict': 512,
    'max_result_len': 1200,  # 结论回给主助手的最大字数
    # 白名单: 只读 + 联网 + 技能。要放开别的工具自己加, 但别加对外动作。
    'tools': ['read_file', 'list_files', 'web_search', 'http_get', 'use_skill'],
}

# 永远不给子代理的工具: 它能递归派子代理(指数爆炸), 也不能对外发消息。
_NEVER = ('subagent', 'wechat_send', 'shot_to_wechat', 'send_keys', 'type_text',
          'paste_to_app', 'click', 'open_app', 'win_run', 'run_command',
          'delete_file', 'write_file', 'edit_file', 'run_py', 'run_java',
          'set_clipboard')

_PROMPT = (
    "你是主助手临时派出的子代理，只负责把交给你的一件事查清楚，然后交一段结论。\n"
    "规则：\n"
    "1) 只能用手上的工具去查证。查到的才说，查不到就说查不到，绝对不许编。"
    "同一个工具同一个参数不要重复调，结果不会变。\n"
    "2) 路径一律写成绝对路径（带盘符）。任务里给的是相对路径时，先拼到当前工作目录"
    "（{cwd}）后面再传；拿不准就先 list_files 看一眼再读。\n"
    "3) 没有人会回答你的问题：不要反问、不要等确认。信息不够就用现有条件作出最合理"
    "的判断，并把不确定的地方写清。\n"
    "4) 结论直接写在正文里，不超过 200 字，要点式，不要客套开场、不要贴大段代码、"
    "不要复述工具名和参数。\n"
    "5) 结论只给主助手看，它再决定怎么回对方 —— 所以写成陈述句，别写“我这就发给你”。\n"
)

# 本机 4B 的老毛病: 把工具调用写成正文文字(<tool_call>read_file ...</tool_call>),
# 而不是走 tool_calls 通道。这种"正文"不是结论, 当结论交回去等于把一串参数当答案 ——
# 实测主模型会拿它硬凑出一句"config 里是 true"发出去, 而它一个字都没读到。
_RAW_CALL_TAGS = ('<tool_call>', '</tool_call>', '<arg_key>', '<arg_value>',
                  '"function":', '"arguments":')


def settings():
    sc = (config.USER_CFG or {}).get('subagent')
    if not isinstance(sc, dict):
        sc = {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in sc:
            out[k] = sc[k]
    out['enabled'] = bool(out['enabled'])
    for k, lo, hi in (('max_steps', 1, 8), ('timeout', 15, 600),
                      ('num_predict', 128, 4096), ('max_result_len', 200, 4000)):
        try:
            out[k] = max(lo, min(hi, int(out[k])))
        except Exception:
            out[k] = DEFAULTS[k]
    names = [str(x) for x in (out.get('tools') or []) if str(x).strip()]
    out['tools'] = [x for x in names if x not in _NEVER]
    return out


def _trim(text, limit):
    s = str(text or '').strip()
    if len(s) > limit:
        s = s[:limit].rstrip() + '…'
    return s


def _fix_paths(args):
    """相对路径兜底。

    实测: 主模型把任务传成 "chains/agent/config.json"(它按仓库根想事情), 而子代理进程的
    cwd 就是 chains/agent, read_file 一拼就成了 chains/agent/chains/agent/config.json, 白烧两轮。
    这里只做一件无害的事: 相对路径在 cwd 下**不存在**时, 逐级往上找, 找到就换绝对路径。
    """
    if not isinstance(args, dict):
        return args
    out = dict(args)
    v = out.get('path')
    if not isinstance(v, str) or not v.strip() or os.path.isabs(v):
        return out
    p = v.strip()
    cwd = os.getcwd()
    if os.path.exists(os.path.join(cwd, p)):
        return out
    up = cwd
    for _i in range(4):
        up = os.path.dirname(up)
        if not up or up.endswith(os.sep):
            break
        cand = os.path.join(up, p)
        if os.path.exists(cand):
            out['path'] = cand
            print(f'[子代理] 路径改写 {p!r} -> {cand}')
            break
    return out


def _usage(trace):
    """把 trace 压成 '工具 read_file×1, web_search×2'。

    工具名必须原样出现: 主循环的 agent._read_ok 靠这行判断"文件内容到底读没读过",
    没有它, 子代理读完文件、主模型复述内容时会被当成"编造读过"推回去重写。
    """
    cnt = {}
    for t in trace or ():
        n = (t or {}).get('tool') or '?'
        cnt[n] = cnt.get(n, 0) + 1
    if not cnt:
        return '工具 没调过'
    return '工具 ' + ', '.join(f'{k}×{v}' for k, v in cnt.items())


def run(task, focus=''):
    """跑一次子代理, 返回给主模型看的文本结论(永远不抛异常)。"""
    s = settings()
    if not s['enabled']:
        return '子代理没有启用(config.json 里 subagent.enabled=false), 这件事自己做。'
    task = str(task or '').strip()
    if not task:
        return '没给任务: subagent 的 task 必须写清"要它独立查清的那一件事"。'
    if len(task) > 1200:
        task = task[:1200]

    import agent                       # 延迟 import: agent <-> pc_tools 互相引用
    import pc_tools

    wanted = set(s['tools']) - set(_NEVER)
    specs = [x for x in pc_tools.TOOL_SPECS
             if x['function']['name'] in wanted]
    if not specs:
        return '子代理没有可用工具(检查 config.json 的 subagent.tools), 这件事自己做。'

    deadline = time.time() + s['timeout']
    users = task if not focus else f'{task}\n(重点关注: {str(focus)[:200]})'
    msgs = [{'role': 'system', 'content': _PROMPT.replace('{cwd}', os.getcwd())},
            {'role': 'user', 'content': users}]
    trace = []
    cache = {}              # key -> 上次结果: 重复调用直接把结果再给它一份, 不是骂它一句
    banned = set()          # 重复调用过的工具名: 下一轮直接摘出工具表(主循环同款手法)
    t0 = time.time()
    print(f'[子代理] 开工: {task[:80]!r} (工具 {len(specs)} 个, 预算 {s["timeout"]}s)')

    step = 0
    while step < s['max_steps']:
        remain = deadline - time.time()
        if remain < 8:
            print(f'[子代理] 时间预算用完({time.time() - t0:.0f}s), 收工')
            break
        avail = [x for x in specs if x['function']['name'] not in banned]
        # 最后一轮(或工具被摘光)收走工具表: 和主循环同一个道理(见 agent._loop 末轮
        # 注释), 带着工具它只会继续调工具, 不会坐下来写结论。
        last = step == s['max_steps'] - 1 or not avail
        step += 1
        try:
            msg = agent._chat_once(msgs, tools=(None if last else avail),
                                   timeout=min(remain, 120),
                                   num_predict=s['num_predict'])
        except Exception as e:                              # noqa: BLE001
            print(f'[子代理] 模型调用失败: {type(e).__name__}: {e}')
            return (f'子代理调用模型失败({type(e).__name__}), 没有拿到结论。'
                    f'这件事你按现有信息自己做, 或者告诉对方这次没查成。')

        content = agent._strip_stray_think(msg.get('content') or '')
        calls = msg.get('tool_calls') or []
        if calls:
            msgs.append({
                'role': 'assistant', 'content': content,
                'tool_calls': [{'function': {
                    'name': c['function']['name'],
                    'arguments': c['function'].get('arguments') or {}}} for c in calls],
            })
            dup_names = set()
            for c in calls:
                name = c['function']['name']
                args = c['function'].get('arguments') or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                args = _fix_paths(args)      # 相对路径按仓库根写的情况, 见 _fix_paths
                # 判重直接用主循环那套身份口径(_call_key): 它把"路径/查询词"之外那些旋钮
                # (start/limit/count…)剔出去了。实测子代理会给同一个 read_file 加不同的小
                # 参数重发三次, 用全量 args 当 key 一次都拦不住。
                key = agent._call_key(name, args)
                if name not in wanted:
                    result = (f'子代理没有 {name} 这个工具(它只能查, 不能动手)。'
                              f'改用 {"/".join(sorted(wanted))} 里的工具, 或者直接给结论。')
                elif key in cache:
                    # 重复调用: 把上次的结果**原样再给它一份**, 并把该工具从下一轮的
                    # 工具表里摘掉。只回一句"调过了"没用 —— 实测它会连着十几轮重发同一个
                    # read_file(25 秒烧光、一个字结论都没有); 但"这个工具压根不在选项里"
                    # 是 4B 能懂的死路(主循环 _SEARCH_BUDGET 就是这么治搜索循环的)。
                    result = (cache[key] + '\n(上面这份就是刚才那次调用的结果, 原样再给你'
                              '一次。这个工具不能再用了, 现在直接下结论。)')
                    dup_names.add(name)
                else:
                    result = pc_tools.dispatch(name, args)
                    cache[key] = _trim(result, 2000)
                    trace.append({'tool': name, 'args': args, 'result': result})
                print(f'[子代理] 第{step}轮 用 {name} -> {str(result)[:100]!r}')
                msgs.append({'role': 'tool',
                             'content': _trim(result, 2000), 'tool_name': name})
            if dup_names:
                banned.update(dup_names)
                print(f'[子代理] 重复调用 {sorted(dup_names)}, 下一轮摘掉这些工具')
                msgs.append({'role': 'user', 'content':
                             '（' + '/'.join(sorted(dup_names)) + ' 已经调过且结果都在上面,'
                             '下一轮起不能再调它们了。直接用已有结果把结论写出来。）'})
            continue

        mark, body = agent._split_mark(content)
        body = body.strip()
        if body and any(t in body for t in _RAW_CALL_TAGS):
            # 它把工具调用写成了正文: 这不是结论, 推回去重来(不占结论机会)。
            print(f'[子代理] 第{step}轮 把工具调用写成正文, 推回重来')
            msgs.append({'role': 'assistant', 'content': content})
            msgs.append({'role': 'user', 'content':
                         '（你上一轮把工具调用写成了正文文字, 那不是结论, 也没有任何工具'
                         '被执行。要调工具就走工具调用通道; 已经查到结果就直接写结论。）'})
            continue
        if body and mark != 'continue':
            print(f'[子代理] 出结论 {time.time() - t0:.1f}s: {body[:100]!r}')
            return (f'子代理结论({_usage(trace)}, {time.time() - t0:.0f}s): '
                    + _trim(body, s['max_result_len']))
        if mark == 'continue':
            continue

    # 走到这里 = 步数/时间用尽还没交付。把做过的工具列出来, 主助手至少知道查到哪一步。
    done = _usage(trace)
    print(f'[子代理] 无结论收工 {time.time() - t0:.0f}s, 用过: {done}')
    return (f'子代理没给出结论(步数或时间用尽, 已用工具: {done})。'
            f'别把它的中间结果当答案, 你按自己手上的信息决定怎么回对方。')


TOOL_SPECS = [
    {
        'type': 'function',
        'function': {
            'name': 'subagent',
            'description': '派一个子代理去独立查清一件事, 只拿回它的结论(它的中间过程不进你的上下文)。'
                           '适合: 要读好几个文件、或要搜好几轮才能汇总的事。'
                           '不适合: 一步就能做完的事(自己直接调工具更快), 也不要拿它来代你跟对方聊天。'
                           '它只能查不能动手(不能发消息、不能改文件)。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'task': {'type': 'string',
                             'description': '要它独立查清的那一件事, 写清楚要回答什么'},
                    'focus': {'type': 'string',
                              'description': '可选: 特别要盯住的点, 例如某个路径/某个关键词'},
                },
                'required': ['task'],
            },
        },
    },
]

TOOL_FUNCS = {
    'subagent': run,
}
