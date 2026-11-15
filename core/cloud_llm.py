# -*- coding: utf-8 -*-
"""云端大脑 —— 多线路(GLM / DeepSeek), 本地 ollama 只做最后一道兜底。

链路: 启用的云端线路按固定顺序排队(GLM -> DeepSeek), 每条线路内部先试主模型、
再试本线路的备用模型; 全部因为"额度/限流/连不上"失败才往下走, 最后落到本地。

为什么需要多线路: 单一线路撞到 429/1113(余额或资源包)时, 以前只能掉到本地 4B,
而 4B 做不了长任务, 等于这一轮白给。多一条云线路顶上去, 比掉本地有用得多。

为什么固定顺序而不是配置文件排序: 顺序就是"谁先花钱/谁更稳"的策略, 写死在
ORDER 里比让人在 json 里排数组可靠 —— 控制台只负责开/关, 不负责排序。

协议: 两家都是 OpenAI 兼容的 /chat/completions, Bearer 鉴权。但和 ollama 有两处
必须抹平, 否则多轮工具循环会静默坏掉:
  1) 返回的 tool_calls[i].function.arguments 是 **JSON 字符串**(ollama 给 dict);
  2) 回灌时 assistant 消息要带 tool_calls, 工具结果走 role='tool' + tool_call_id
     (ollama 那套用 role='tool' + tool_name, 云端不认 tool_name)。

配置在 config.json(改完重启服务生效):
  "cloud_providers": {
    "glm":      {"enabled": true,  "model": "glm-4.5-air",
                 "fallback_model": "glm-4-flash", "timeout": 60,
                 "base_url": "https://open.bigmodel.cn/api/paas/v4",
                 "api_key": "..."},
    "deepseek": {"enabled": true,  "model": "deepseek-v4-flash",
                 "fallback_model": "", "timeout": 120,
                 "base_url": "http://127.0.0.1:8789", "api_key": "..."}
  }
没有 cloud_providers 的老配置会自动退回原来的 cloud_enable / cloud_model /
cloud_api_key / cloud_base_url / cloud_fallback_model 这一组键, 行为不变。
"""
import json
import urllib.error
import urllib.request

import config

# 内置预设: 新装的人不填 config 也能跑(除了 key)。控制台按这个结构落盘。
PRESETS = {
    'glm': {
        'name': 'GLM(智谱)',
        'base_url': 'https://open.bigmodel.cn/api/paas/v4',
        'model': 'glm-4.5-air',
        'fallback_model': 'glm-4-flash',
        'timeout': 60,
        'temperature': 0.7,
    },
    'deepseek': {
        'name': 'DeepSeek',
        'base_url': 'http://127.0.0.1:8789',
        'model': 'deepseek-v4-flash',
        'fallback_model': '',
        'timeout': 120,
        'temperature': 0.7,
    },
}
ORDER = ('glm', 'deepseek')          # 排队顺序: GLM 先, DeepSeek 顶第二条


class CloudDisabled(RuntimeError):
    """一条启用的云端线路都没有(全关了或都没有 key)。"""


def _user_cfg():
    return getattr(config, 'USER_CFG', {}) or {}


def providers():
    """所有云端线路(含未启用的), 顺序固定。每项都是合并后的最终配置。"""
    raw = _user_cfg().get('cloud_providers')
    raw = raw if isinstance(raw, dict) else None
    out = []
    for pid in ORDER:
        merged = dict(PRESETS[pid])
        merged['id'] = pid
        merged['enabled'] = False
        if raw is None:
            # 老配置: 原来的 cloud_* 就是 GLM 这一条
            if pid == 'glm':
                merged.update({
                    'enabled': bool(getattr(config, 'CLOUD_ENABLE', False)),
                    'api_key': str(getattr(config, 'CLOUD_API_KEY', '') or ''),
                    'base_url': str(getattr(config, 'CLOUD_BASE_URL', '')
                                    or merged['base_url']),
                    'model': str(getattr(config, 'CLOUD_MODEL', '')
                                 or merged['model']),
                    'fallback_model': str(
                        getattr(config, 'CLOUD_FALLBACK_MODEL', '')
                        or merged['fallback_model']),
                    'timeout': int(getattr(config, 'CLOUD_TIMEOUT', 0)
                                   or merged['timeout']),
                })
        else:
            src = raw.get(pid)
            if isinstance(src, dict):
                for k in ('enabled', 'api_key', 'base_url', 'model',
                          'fallback_model', 'timeout'):
                    if k in src and src[k] not in (None, ''):
                        merged[k] = src[k]
            if pid == 'glm' and not merged.get('api_key'):
                merged['api_key'] = str(getattr(config, 'CLOUD_API_KEY', '') or '')
        merged.setdefault('api_key', '')
        merged['enabled'] = bool(merged.get('enabled'))
        merged['base_url'] = str(merged.get('base_url') or '').rstrip('/')
        merged['model'] = str(merged.get('model') or '')
        merged['fallback_model'] = str(merged.get('fallback_model') or '')
        try:
            merged['timeout'] = int(float(merged.get('timeout') or 60))
        except (TypeError, ValueError):
            merged['timeout'] = 60
        out.append(merged)
    return out


def active():
    """当前启用的线路(有 url 有模型才算)。"""
    return [p for p in providers() if p['enabled'] and p['base_url'] and p['model']]


def describe():
    """给启动日志用的一行话: GLM(glm-4.5-air) -> DeepSeek(...) -> 本地。"""
    names = ['%s(%s)' % (p['name'], p['model']) for p in active()]
    return ' -> '.join(names + ['本地 %s' % getattr(config, 'LLM_MODEL', '')])


_egress_warned = False


def _post(p, path, payload, timeout):
    global _egress_warned
    if not _egress_warned:
        _egress_warned = True
        # 数据出境提示: 聊天内容会经网络发送给第三方云端 LLM。
        print('[cloud_llm] 注意: 已开启云端大模型, 本次及后续对话内容会经网络发送给 '
              '云端提供商(%s)的服务器, 请确认你接受聊天内容出境。'
              % p.get('name', '云端'))
    url = p['base_url'] + path
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + str(p.get('api_key') or '')})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _to_openai_msgs(msgs):
    """把 agent 里的 ollama 形态消息翻成 OpenAI 形态。

    tool_call_id 的配对: agent 构造 assistant.tool_calls 时**不带 id**(ollama 不
    需要), 而云端要求每个工具结果都挂回它对应的那次调用。这里按"同一工具名就近
    配对"生成 id —— 同一轮里同参数工具只会调一次(agent 有防重复), 不同工具名不会
    混, 所以按名匹配足够; 真有多参数同名调用时退化成顺序 id, 也不会串(结果文本
    本身还在, 模型能对上)。
    """
    out = []
    pending = {}
    for m in msgs or ():
        role = m.get('role')
        if role == 'tool':
            cid = pending.get(m.get('tool_name') or '') or 'call_%d' % len(out)
            out.append({'role': 'tool', 'tool_call_id': cid,
                        'content': str(m.get('content') or '')})
            continue
        tcs = m.get('tool_calls') if role == 'assistant' else None
        if tcs:
            calls = []
            for i, t in enumerate(tcs):
                cid = t.get('id') or 'call_%d_%d' % (len(out), i)
                fn = t.get('function') or {}
                pending[fn.get('name') or ''] = cid
                calls.append({'id': cid, 'type': 'function',
                              'function': {
                                  'name': fn.get('name') or '',
                                  'arguments': json.dumps(
                                      fn.get('arguments') or {},
                                      ensure_ascii=False)}})
            out.append({'role': 'assistant',
                        'content': m.get('content') or '',
                        'tool_calls': calls})
            continue
        out.append({'role': role, 'content': str(m.get('content') or '')})
    return out


def _switchable(txt):
    """这个错值不值得换一条线路/换一个模型再试。

    1113(余额不足/无资源包) / 429 / 1305(访问量过大) / 401(Key 过期或被撤) /
    连不上 / 超时 都是"这条线路此刻用不了", 换线路可能就好了;
    400/参数错换线路也没用, 直接抛。
    """
    return any(k in txt for k in ('401', '1113', '429', '1305', 'Too Many',
                                  '余额不足', '资源包', '身份验证失败',
                                  'Unauthorized', '连接', 'timed out',
                                  'Timeout', 'URLError', 'Connection'))


def err_text(e):
    """把异常压成一行好认的话(错误体在 HTTPError.read() 里)。"""
    body = ''
    try:
        if isinstance(e, urllib.error.HTTPError):
            body = e.read().decode('utf-8', 'replace')[:200]
    except Exception:                                          # noqa: BLE001
        pass
    return '%s: %s %s' % (type(e).__name__, str(e)[:80], body)


def cloud_chat(messages, tools=None, model=None, timeout=None, num_predict=None):
    """一次云端对话, 返回和 ollama _chat_once 同形状的消息字典。

    线路顺序: 指定的 model 落在哪条线路上, 哪条先上(群聊里点名的模型优先);
    其余按 ORDER。每条线路内部: 主模型 -> 本线路备用模型。
    全部失败时抛最后一个异常, 由调用方决定是否退回本地。
    """
    provs = active()
    if not provs:
        raise CloudDisabled('没有可用的云端线路(config.json 的 '
                            'cloud_providers 全关着, 或都没填 key)')
    if model:
        provs = sorted(provs, key=lambda p: 0 if model in (p['model'],
                                                           p['fallback_model'])
                       else 1)
    last = None
    for p in provs:
        cands = []
        if model and model in (p['model'], p['fallback_model']):
            cands.append(model)
        for m in (p['model'], p['fallback_model']):
            if m and m not in cands:
                cands.append(m)
        for m in cands:
            try:
                return _cloud_once(p, messages, tools, m, timeout, num_predict)
            except Exception as e:                             # noqa: BLE001
                last = e
                txt = err_text(e)
                if not _switchable(txt):
                    raise
                print('[cloud] %s/%s 不可用(%s), 换下一条'
                      % (p['name'], m, txt[:120]))
    raise last


def _cloud_once(p, messages, tools, model, timeout=None, num_predict=None):
    payload = {
        'model': model,
        'messages': _to_openai_msgs(messages),
        'temperature': float(p.get('temperature') or 0.7),
        'stream': False,
    }
    if tools:
        payload['tools'] = tools
    if num_predict:
        payload['max_tokens'] = num_predict
    data = _post(p, '/chat/completions', payload,
                 timeout or p.get('timeout') or 60)
    msg = ((data.get('choices') or [{}])[0]).get('message') or {}
    tcs = []
    for t in (msg.get('tool_calls') or []):
        fn = t.get('function') or {}
        args = fn.get('arguments')
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:                                  # noqa: BLE001
                args = {}
        if not isinstance(args, dict):
            args = {}
        tcs.append({'function': {'name': fn.get('name') or '',
                                 'arguments': args},
                    'id': t.get('id')})
    return {'content': msg.get('content') or '',
            'tool_calls': tcs,
            'thinking': '',
            # usage: OpenAI 兼容格式 {prompt_tokens, completion_tokens, ...}
            # 调用方据此在回复末尾另起一行打印 token 明细, 缺了就按 0 处理
            'usage': data.get('usage') or {}}


def ping(pid=None):
    """探活: 发一句"在", 通就返回耗时(秒), 不通抛异常。

    不传 pid 时按 ORDER 逐条试, 返回第一条通的线路耗时(保持老调用方的 float 语义)。
    """
    import time
    provs = active()
    if pid:
        provs = [p for p in provs if p['id'] == pid] or [p for p in providers()
                                                         if p['id'] == pid]
    if not provs:
        raise CloudDisabled('没有可用的云端线路')
    last = None
    for p in provs:
        t0 = time.time()
        try:
            _cloud_once(p, [{'role': 'user', 'content': '在'}], None,
                        p['model'], p.get('timeout'), 8)
            return time.time() - t0
        except Exception as e:                                 # noqa: BLE001
            last = e
    raise last
