# -*- coding: utf-8 -*-
"""助手大脑(imhere): ollama 原生工具调用 + 模型自主决策的单循环

一条对方消息的完整链路:

    ask(session, "帮我打开计算器")
      -> 先过快速通道: 不用动手的消息(闲聊/常识)不带工具单轮直答, 1~2s 出结果;
         它拿不准就回 FAST_NEED_TOOLS, 转下面的决策循环(所以是加速档, 不减能力)
      -> 决策循环(最多 AGENT_MAX_STEPS 轮), 每一轮模型自己判断三件事:
         进度话术  一句话给对方的"我先看一下" -> 事件通道, 不进历史
           工具调用  走 ollama 原生 tool_calls -> pc_tools 真执行, 结果回灌
           收尾标记  最后一行 <continue> = 还要继续; <finish> 或没写 = 交付
      -> 判断权在模型, 程序只做解析 + 执行 + 护栏:
           * 最后一轮收走工具, 用 DELIVER_NUDGE 逼它交付
           * 连续 AGENT_STALL_LIMIT 轮没进展 -> 强制交付
           * 循环始终没交出正文 -> 拿工具记录二次总结兜底(做过事就一定有回话)
      -> 返回正文, 交给 sender 发回微信

为什么这么设计(官方依据 + 本机实测):
  1) 官方同源: OpenAI GPT-5 的 tool preambles —— 工具调用前先给一句人话。
     Responses API 里它是独立的 message 对象(推理摘要 -> 前置说明 -> 工具调用),
     不是塞在工具调用里的。
  2) 实测(ollama 0.35.0 + qwen3:4b): 纯工具轮的 content 是空串, 想让它写进度话术
     必须在提示词里明确要求; 而且必须清洗(会带 </think> 残标签和大段内心独白)。
  3) 进度话术是"事件"不是"消息": 进了历史, 模型会把自己还没做的事当成已经做过的,
     所以走 on_note 回调用掉, 不写进 session.messages。

速度依据(本机 i7-9750H / 8G 内存 / GTX1650 4G, 显存只剩约 1G):
    2.5G 的 qwen3:4b 塞不进 4G 显存, 有 6~13% 的层掉在 CPU 上, 解码只有 8~10 tok/s。
    这个速度下写 250~400 字思维链要 30~50s, 一条消息 50~120s —— 慢的全部来源就是思维链。
    改用 config.LLM_MODEL = qwen3:4b-nothink(模板预填空 <think></think>, 见 config.py
    和 LLM_MODEL 处注释): 闲聊 1.5s, 一次工具往返(含截图落盘) 11.7s, 原生工具调用照旧。
    踩过的坑: think=False 单独用没用 —— ollama 官方 qwen3 模板末尾无条件注入 <think>,
    关掉只会让思维链漏进正文(还会吃掉 num_predict 预算)。
"""
import json
import os
import re
import threading
import time

import requests

import config
import cloud_llm
import pc_skills
import pc_tools

_SESSION_ID = 0


class Session:
    """一个会话 = 一条独立上下文(本实例固定只有一个)"""

    def __init__(self, name, history=None):
        self.name = name
        self.messages = [{'role': 'system', 'content': config.SYSTEM_PROMPT}]
        self.note = None          # 当次请求的临时附件, 见 user()/wire()
        if history:
            lines = '\n'.join(f'{"我" if who == "我" else "对方"}: {t}'
                              for who, t in history)
            self.messages.append({
                'role': 'system',
                'content': '下面是你们最近的聊天记录，仅作背景了解，'
                           '不要重复回答这些内容：\n' + lines,
            })

    def user(self, text, note=None):
        """note = 只给当次请求看的程序预检, 绝不落进历史。

        为什么必须分开: 预检是命令腔文本("回答的第一句话必须先点明"), 一旦留在
        历史里, 下一轮 4B 会把它当待办继续执行 —— 实测 14:53 问了成语, 14:54
        对方改问"现在几点了", 它却去搜"叶公好龙的出处"接着讲成语, 时间一个字没提。
        """
        self.messages.append({'role': 'user', 'content': text})
        self.note = note

    def wire(self):
        """真正发给模型的那份消息: 当次 note 挂到最后一条 user 上, 历史保持干净。

        多轮循环里每轮都重新拼一次, 所以模型每轮都看得到预检, 而它永远进不了历史。
        """
        msgs = [dict(m) for m in self.messages]
        if self.note and msgs and msgs[-1].get('role') == 'user':
            msgs[-1]['content'] = f"{msgs[-1]['content']}\n\n{self.note}"
        return msgs

    def assistant(self, text):
        self.messages.append({'role': 'assistant', 'content': text})

    def trim(self, keep=40):
        """只保留 system 前缀 + 最近 keep 条, 防上下文无限膨胀"""
        head = [m for m in self.messages if m['role'] == 'system']
        rest = [m for m in self.messages if m['role'] != 'system']
        self.messages = head + rest[-keep:]


def new_session(name, history=None):
    global _SESSION_ID
    _SESSION_ID += 1
    return Session(name, history)


# ============================================================
# ollama 调用
# ============================================================
def _post(path, payload, timeout=None):
    url = config.OLLAMA_URL.rstrip('/') + path
    r = requests.post(url, json=payload, timeout=timeout or config.LLM_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _options():
    return {
        'num_ctx': config.LLM_NUM_CTX,
        'num_predict': config.LLM_NUM_PREDICT,
        # 必须显式给: 不给就交给 ollama 按"加载时剩多少显存"估算, 显存被占时整模型
        # 退回 CPU, 解码从 17.7 掉到 0.9 tok/s 且 keep_alive=-1 让它一直坏着。
        # 详见 config.LLM_NUM_GPU 处的实测。
        'num_gpu': config.LLM_NUM_GPU,
        'temperature': 0.4,
        'repeat_penalty': 1.1,
    }


def _chat_cloud(messages, tools=None, timeout=None, num_predict=None, model=None):
    # model=None 保持老行为(用 cloud_llm 里配置的默认模型); 群聊才逐模型指定
    msg = cloud_llm.cloud_chat(messages, tools=tools, model=model,
                               num_predict=num_predict, timeout=timeout)
    msg['_via'] = 'cloud'
    return msg


def _chat_local(messages, tools=None, timeout=None, think=None, num_predict=None,
                model=None):
    opts = _options()
    if num_predict:
        opts['num_predict'] = num_predict
    payload = {
        'model': model or config.LLM_MODEL,
        'messages': messages,
        'stream': False,
        'options': opts,
        'keep_alive': config.LLM_KEEP_ALIVE,
    }
    if tools:
        payload['tools'] = tools
    want_think = config.LLM_THINK if think is None else think
    if want_think is not None:
        payload['think'] = bool(want_think)
    try:
        data = _post('/api/chat', payload, timeout)
    except requests.HTTPError:
        if 'think' in payload:      # 老版本 ollama 不认 think 字段, 去掉重试一次
            payload.pop('think')
            data = _post('/api/chat', payload, timeout)
        else:
            raise
    msg = data.get('message') or {}
    msg['_via'] = 'local'
    return msg


def _chat_once(messages, tools=None, timeout=None, think=None, num_predict=None,
               prefer=None):
    """一次 /api/chat, 按调用点决定走哪一侧(意图路由)。

    prefer='local'  闲聊侧: 快速通道的单轮直答直接走本地 ollama —— 闲聊不需要
                    推理深度, 本地 2~3s 就够, 也不占云端额度、不受限流影响;
    prefer='cloud'  决策侧: 要动手/要联网的判断走云端, 云端挂了才退本地;
    prefer=None     老行为(云端优先), 既有调用点行为一个字节都不变。

    两侧都留兜底: "优先"不是"唯一", 免得不该失联的时候失联。
    """
    if prefer == 'local':
        try:
            return _chat_local(messages, tools=tools, timeout=timeout,
                               think=think, num_predict=num_predict)
        except Exception as e:                                  # noqa: BLE001
            if not (config.CLOUD_ENABLE and config.CLOUD_FALLBACK_LOCAL):
                raise
            print('[agent] 闲聊本地失败(%s), 退云端'
                  % cloud_llm.err_text(e)[:120])
            return _chat_cloud(messages, tools=tools, timeout=timeout,
                               num_predict=num_predict)

    if config.CLOUD_ENABLE:
        try:
            return _chat_cloud(messages, tools=tools, timeout=timeout,
                               num_predict=num_predict)
        except Exception as e:
            if not config.CLOUD_FALLBACK_LOCAL:
                raise
            print('[agent] 云端不可用(%s), 本轮退回本地 ollama'
                  % cloud_llm.err_text(e)[:120])
    return _chat_local(messages, tools=tools, timeout=timeout,
                       think=think, num_predict=num_predict)


def list_models():
    try:
        r = requests.get(config.OLLAMA_URL.rstrip('/') + '/api/tags', timeout=5)
        return [m['name'] for m in r.json().get('models', [])]
    except Exception:
        return []


def warmup():
    """预热两条路: 云端先探活(顺带把 DNS/TLS 连接建好, 第一条消息不用现握手),
    本地 ollama 也提前加载进显存 —— 云端是主路径, 但兜底那条同样不能冷启动。

    本地这次**故意绕开 _chat_once** 直接打 ollama: 否则云端通着的时候本地永远
    不会被加载, 等云端真挂了才第一次冷启动, 兜底就变成了"等 60s"。
    """
    t_all = time.time()
    if config.CLOUD_ENABLE:
        try:
            dt = cloud_llm.ping()
            print(f'[agent] 云端 {config.CLOUD_MODEL} 就绪 ({dt:.1f}s)')
        except Exception as e:
            print(f'[agent] 云端预热失败({cloud_llm.err_text(e)[:100]}), '
                  f'主路径将退回本地 ollama')
    try:
        t0 = time.time()
        _post('/api/chat', {
            'model': config.LLM_MODEL,
            'messages': [{'role': 'user', 'content': '在'}],
            'stream': False,
            'options': _options(),
            'keep_alive': config.LLM_KEEP_ALIVE,
            'think': False,
        }, 300)
        print(f'[agent] 本地 {config.LLM_MODEL} 就绪 ({time.time() - t0:.1f}s)')
    except Exception as e:
        print(f'[agent] 本地模型预热失败: {e}')
    print(f'[agent] 预热合计 {time.time() - t_all:.1f}s')


# ============================================================
# 输出清洗
# ============================================================
_THINK_RE = re.compile(r'<(think|thinking|reasoning)>[\s\S]*?</\1>', re.I)
_FENCE_RE = re.compile(r'```[a-zA-Z]*\n?([\s\S]*?)```')
# 正文里的工具调用 JSON。键名放得很宽: 小模型一会儿写 {"name": ...},
# 一会儿写 {"function": "open_app", ...} —— 实测后一种旧版没接住, 结果那串 JSON
# 被当成交付发给了对方(见 config.JSON_NUDGE 的注释)。
_JSON_TOOL_RE = re.compile(r'\{[^{}]*"(?:tool|name|function)"\s*:\s*"([a-zA-Z_]+)"'
                           r'[^{}]*"(?:args|arguments|parameters)"\s*:\s*(\{[^{}]*\})[^{}]*\}',
                           re.S)
# OpenAI 那种嵌套写法: {"function": {"name": "open_app", "arguments": {...}}}
_JSON_TOOL_NESTED_RE = re.compile(
    r'"function"\s*:\s*\{[^{}]*"(?:name|tool)"\s*:\s*"([a-zA-Z_]+)"'
    r'[^{}]*"(?:args|arguments|parameters)"\s*:\s*(\{[^{}]*\})', re.S)


def _strip_stray_think(s):
    """收拾"思维链漏进正文"的残渣。

    关掉 think(快速通道就是这么干的)时, qwen3 偶尔仍会把思考写在正文里,
    但只有孤立的 </think> 标签, 成对的 <think>…</think> 已被 _THINK_RE 清掉。
    这种情况下标签之后的才是真正要说的话, 直接截断取后面。
    """
    low = s.lower()
    i = low.rfind('</think')
    if i != -1:
        j = s.find('>', i)
        if j != -1:
            s = s[j + 1:]
    return re.sub(r'</?think(ing)?>', '', s, flags=re.I)


def _clean(text):
    """去掉思维链/代码围栏/自称前缀/收尾标记, 只留要发给对方的正文"""
    s = str(text or '')
    s = _THINK_RE.sub('', s)
    s = _strip_stray_think(s)
    s = _strip_mark(s)          # 标记绝不能发给对方
    m = _FENCE_RE.search(s)
    if m:
        s = m.group(1) if len(_FENCE_RE.findall(s)) == 1 else _FENCE_RE.sub('', s)
    s = re.sub(r'^\s*(\[AI[^\]]*\]|AI[:：])\s*', '', s, flags=re.I)
    s = s.replace('**', '').strip()
    s = _strip_cn_punct(s)      # 中文标点一律换成空格(提示词交代过, 这里再兜一道)
    if len(s) > config.MAX_REPLY_LEN:
        s = s[:config.MAX_REPLY_LEN].rstrip() + '...'
    return s


def _text_tool_call(content):
    """兼容兜底: 有些小模型不走 API 的工具通道, 而是把 JSON 打在正文里。
    命中就当成一次工具调用。"""
    s = content or ''
    for rx in (_JSON_TOOL_RE, _JSON_TOOL_NESTED_RE):
        m = rx.search(s)
        if not m:
            continue
        try:
            args = json.loads(m.group(2))
        except Exception:
            continue
        return m.group(1), args
    # 还有更歪的写法: 工具名和参数平铺在同一级, 连 arguments 都不写 ——
    # 实测 10-07 18:42 模型第二次吐的就是 {"function": "open_app", "name": "运行框"}。
    # 这种直接整段解析: 认得出的工具名 + 剩下的键当参数, 总比当成答复发出去强。
    obj = _whole_json_object(s)
    if isinstance(obj, dict):
        name = src = None
        for k in ('tool', 'function', 'name'):
            v = obj.get(k)
            if isinstance(v, str) and v in _tool_names():
                name, src = v, k
                break
        if name:
            for k in ('args', 'arguments', 'parameters'):
                if isinstance(obj.get(k), dict):
                    return name, obj[k]
            rest = {k: v for k, v in obj.items()
                    if k not in ('tool', 'function', 'args', 'arguments',
                                 'parameters', 'tool_call', 'tool_calls')}
            if src == 'name':
                rest.pop('name', None)   # 这个 name 是工具名, 不是参数
            return name, rest
    return None


def _whole_json_object(s):
    """把整段正文当成一个 JSON 对象解析出来(容忍 ``` 围栏和前后杂字); 不行返回 None。"""
    t = str(s or '').strip().strip('`').strip()
    m = _FENCE_RE.search(t)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    i = t.find('{')
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(t)):
        if t[j] == '{':
            depth += 1
        elif t[j] == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[i:j + 1])
                except Exception:
                    return None
    return None


# 正文里出现这些键 = 它长得就是一段工具调用, 不是给人看的话。
_TOOLJSON_KEYS = ('"tool"', '"function"', '"tool_call"', '"tool_calls"')

# GLM(glm-4.5-flash)专属: 它在**拿不到工具表**的那些轮(末轮程序会故意摘掉工具表逼它
# 交付)会把想调的工具写成 XML 文本 —— <tool_call>run_py<arg_key>path</arg_key>...
# 实测 10-14 端到端: 前 5 轮原生 tool_calls 全对(探路/mkdir/write_file 一次写对),
# 第 6 轮(末轮无工具)交回来的正文就是这段 XML, 被原样发给了对方。
# JSON 那道闸只认 { / [ 开头, 对这种一个词都命中不了, 所以单独列一份。
_TOOLXML_RE = re.compile(r'<\s*/?\s*(?:tool_call|arg_key|arg_value|function_call'
                         r'|parameter|arg_name)', re.I)


def _looks_like_tool_json(text):
    """整条正文就是一段工具调用(JSON 或 XML) —— 这种绝不能当答复发出去。

    它是给程序看的, 发出去对方只会收到一串乱码似的 JSON/XML, 而且任务明明没做完
    却"交付"了, 后续也不会再有人管(实测 10-07 18:34 就是这样断掉的)。
    这里宁可判宽一点: JSON 那条只要以 { / [ 开头又带工具调用特征键, 就当它是;
    XML 那条不看开头, 带 <tool_call> / <arg_key> 这类标签就当它是。
    """
    s = str(text or '').strip().strip('`').strip()
    if not s:
        return False
    if _TOOLXML_RE.search(s):
        return True
    if s[0] not in '{[':
        return False
    if any(k in s for k in _TOOLJSON_KEYS):
        return True
    return '"arguments"' in s and ('"name"' in s or '"args"' in s)


# 整条正文就是一行代码(它想写进文件的东西) —— 这种也绝不能当答复发出去。
# 实测 10-07 22:58 真模型复现: 六轮没做成之后, 最后一轮它交的正文是 `print("静")`
# —— 那是它脑子里那行"输出静夜思"的代码, 不是要说给对方的话。_looks_like_tool_json
# 只管 JSON, 认不出这种, 于是这句原样发了出去: 对方收到 `print("静")`, 既看不出
# 任务没做成, 也看不出该说什么。
_CODE_LINE_RE = re.compile(r'^\s*(?:print|import|from|def|class|for|while|if|elif|'
                           r'else|return|try|except|with|lambda)\b', re.I)
_CODE_CALL_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_\.]*\s*\([^)]*\)\s*;?\s*$')


def _looks_like_code(text):
    """整条正文只是代码(单行, 或整段) —— 不是给人看的话。

    单行那条是原有判据(10-07 22:58 `print("静")` 实测)。**多行这条是 10-14 补的**:
    公平测试 D 组第 1 次跑, 假成功被拦后走"兜底二次总结", 模型交回来的**整段就是
    猜数字源码**(13 行, 里面还带着中文提示语"猜一个数字 1-100")。单行判据认不出,
    `_guarded` 也没有第二道闸, 那段 import random 原样发了出去 —— 对方收到一段代码,
    既看不出任务没做成, 也不知道该说什么。

    多行的判据是"按行投票": 非空行里命中强代码特征的**超过三分之二**才算。
    阈值取 2/3 而不是过半, 是卡着两类误判调的: "我写好了 内容是 / import os /
    print(os.getcwd())" 这种(一句人话带两行代码, 2/3 命中)必须放行, 而整段源码
    (实测那段 13 行里 10 行命中)必须抓住。中文**不作排除条件** —— 代码里的字符串/
    提示语照样可能是中文, 上面那段就是。
    """
    s = str(text or '').strip().strip('`').strip()
    if not s:
        return False
    if '\n' not in s:
        if _CODE_CALL_RE.match(s) and not re.match(r'[\u4e00-\u9fff]', s):
            return True
        return bool(_CODE_LINE_RE.match(s) and not re.search(r'[\u4e00-\u9fff]', s))
    lines = [ln for ln in s.splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    hits = 0
    for ln in lines:
        t = ln.strip()
        if (_CODE_LINE_RE.match(t)
                or _CODE_CALL_RE.match(t)
                or re.match(r'^[A-Za-z_][\w\.]*\s*=[^=]', t)     # 赋值 number = ...
                or re.match(r'^#', t)                           # 注释行
                or re.match(r'^[}\]\)]', t)):                    # 块收尾 } ) ]
            hits += 1
    return hits * 3 > len(lines) * 2


# ------------------------------------------------------------
# 每轮决策标记的解析(模型自己决定「继续」还是「交付」)
# 官方同源做法: OpenAI 的 tool preambles —— 工具调用前先说一句人话,
# Responses API 里它是独立的 message 对象, 不塞在工具调用里。
# ------------------------------------------------------------
_MARK_RE = re.compile(r'[<\[]\s*(continue|finish)\s*[>\]]', re.I)

# 程序写给**模型**看的控制文本片段 —— 只该出现在工具结果里, 一旦被复述进交付正文,
# 说明它把提示当成要给对方的话了。实测 10-14: write_file 新加的相对路径闸门回给它
# "真桌面是 ... 必须写完整绝对路径", 它下一轮原样转述给了对方, 任务一个都没做。
_ECHO_TEXT_RE = re.compile(r'真桌面是|相对路径会被拼到程序自己的目录|必须写完整绝对路径'
                           r'|立刻用真实路径重调一次|立刻按上面的参数表补齐'
                           r'|刚才已经用完全一样的参数调用过|换别的参数、换别的工具')

# 但关键词只认**原句**: 实测 10-14 模型把那段提示**意译成英文**复述给了对方
# ("I understand. When writing files, it's important to use absolute paths..."),
# _ECHO_TEXT_RE 一个词都没命中。所以再补一条按"语义骨架"认的:
# 一件成果都没有(_worked 为假), 通篇却在讲"路径该怎么写/目录结构是什么" —— 那是提示
# 文本的回声, 不是给对方的回话。判宽了会把"真做成了"的交付也拦掉, 所以拿 _worked 当门槛。
_LECTURE_RE = re.compile(r'(?:absolute|relative|full|complete)\s*path|file\s*path'
                         r'|directory\s*structure|绝对路径|相对路径|完整路径|文件路径'
                         r'|目录结构|路径要写全', re.I)
_LECTURE_OPEN_RE = re.compile(r'^\s*(?:I understand|Understood|Sure|Of course|Got it'
                              r'|Note that|Please (?:provide|make sure|use)'
                              r'|我明白|明白了|理解了|请注意|请提供|请使用)', re.I)


def _lecturing(text, trace):
    """啥也没干成, 交付却在讲"应该怎么做" —— 提示文本的回声, 不是给对方的回话。

    实测 10-14 端到端: write_file 的相对路径闸门把提示回给模型, 它下一轮把这段意译成
    英文复述给了对方, 任务一个都没做。共同特征就两条: 零成果 + 讲课腔。
    """
    if _worked(trace):
        return False
    s = str(text or '').strip()
    if not s:
        return False
    return bool(_LECTURE_OPEN_RE.match(s) or _LECTURE_RE.search(s))


def _split_mark(text):
    """取正文末尾的收尾标记, 返回 (标记 or None, 去掉标记后的正文)。

    只看末尾 48 字符里的最后一个标记, 且要求标记之后没有别的正文 ——
    否则正文里偶然写出的 <continue> 会被误判(小模型爱复读自己的提示词)。
    """
    s = str(text or '').strip()
    if not s:
        return None, ''
    tail = s[-48:]
    hits = list(_MARK_RE.finditer(tail))
    if not hits:
        return None, s
    h = hits[-1]
    if tail[h.end():].strip('*`_ \t。.，'):
        return None, s
    body = s[:len(s) - (len(tail) - h.start())].strip()
    return h.group(1).lower(), body.strip('*`_ \t').rstrip()


def _strip_mark(text):
    """只去标记, 不关心是哪种(给清洗后的正文兜底用)"""
    return _split_mark(text)[1]


def _extract_interim(content):
    """挑出本轮那句给对方的「进度话术」。

    临时回复走事件通道(打印/可选发送), 不进模型历史 ——
    否则模型会把自己"还没做"的进度话当成已经发过的话, 后续开始自我指涉。
    命中日志/JSON/标记行一律丢弃, 宁可不发进度也不发脏话。
    """
    for line in str(content or '').splitlines():
        t = line.strip().strip('*`_#> ').strip()
        if not t or _MARK_RE.fullmatch(t.strip()):
            continue
        if t[0] in '{[' or t.startswith(('工具', '结果', 'function', 'tool')):
            return None
        if len(t) > config.INTERIM_MAX_LEN:
            t = t[:config.INTERIM_MAX_LEN].rstrip() + '…'
        return t
    return None


# 工具名 -> 给对方的"正在…"。模型没写进度话术时程序拿它兜底 ——
# 实测 4B 经常一整轮只吐 tool_calls、正文一个字都没有, 于是最该有的
# "正在搜索…"就这么没了。
_TOOL_SAY = {
    'web_search': '正在搜索',
    'read_file': '正在读文件',
    'write_file': '正在写文件',
    'edit_file': '正在改文件',
    'run_command': '正在执行命令',
    'list_dir': '正在看目录',
    'open_app': '正在打开程序',
    'screenshot': '正在截图',
    'click': '正在点击',
    'type_text': '正在输入文字',
    'run_py': '正在跑一遍自检',
    'run_java': '正在编译并运行',
    'list_files': '正在看项目文件',
    'paste_to_app': '正在把内容送进窗口',
    'shot_to_wechat': '正在截图发回微信',
    'wechat_send': '正在给微信里的人发消息',
    'wechat_windows': '正在看已打开的聊天窗口',
    'press_key': '正在按键',
    'move_mouse': '正在移动鼠标',
    'list_skills': '正在看有哪些技能',
    'use_skill': '正在查操作流程',
    'subagent': '正在让子代理去查',
}


def _tool_say(name):
    return _TOOL_SAY.get(str(name or ''), f'正在执行 {name}')


# 这些工具的结果**不**往微信里播报"返回结果: …"。
# 技能正文和子代理结论都是写给模型看的内部材料(几行 Markdown/要点), 对方收到只会是
# 一串看不懂的流程标题; 而 use_skill 的抬头就是"技能 xxx 已加载", 播出去等于把机制
# 暴露给对方。执行前的"正在查操作流程"照旧播, 对方至少知道在干活。
_NO_RESULT_NOTE = ('use_skill', 'list_skills', 'subagent')


_SPACE_RE = re.compile(r'\s+')
_URL_IN_LINE_RE = re.compile(r'\(?（?https?://\S+\)?）?')


def _result_note(result):
    """把工具的真实结果压成一句"返回结果: …"(只取第一行 + 截断)。

    这句是**程序从 result 里读出来的**, 不是模型编的 —— 进度话术可以是"我打算做什么",
    但"做成了什么"必须对着执行结果说, 否则又回到"我已成功在桌面创建了文件夹"那种
    没有任何依据的通报上。

    首行的长 URL 必须剥掉: 搜索结果的抬头是
       搜索「今天北京天气」命中 8 条  (https://cn.bing.com/search?q=%E4%BB%8A...)
    那个 percent-encoded 查询串一条就把 40 字额度吃光, 对方看到的是一串百分号。
    """
    for line in str(result or '').splitlines():
        line = line.strip()
        if not line:
            continue
        line = _SPACE_RE.sub(' ', _URL_IN_LINE_RE.sub('', line)).strip(' ()（）、·-')
        if not line:
            continue
        if len(line) > config.INTERIM_MAX_LEN:
            line = line[:config.INTERIM_MAX_LEN].rstrip() + '…'
        return f'返回结果: {line}'
    return None


def _similar(a, b):
    """粗略判两句话是不是几乎一样(去空白比前 40 字), 用于卡住检测"""
    norm = lambda x: re.sub(r'\s+', '', str(x or ''))[:40]
    return bool(norm(a)) and norm(a) == norm(b)


# 过程话/预告的典型长相。本机 4B 实测会把进度话当成最终交付:
#   1) 问"帮我打开计算器" -> 回一句"我先看一下。"就结束了, 结果什么都没打开;
#   2) 问"你帮我打开计算器" -> 回"我先用 open_app 打开计算器。" —— 它把工具名
#      念进了要发给人的话里, 自己以为"说了就等于做了", 实际一个工具都没调。
# 这些话是"事件"(给对方的进度), 绝不能当答复 —— 要么逼它真动手, 要么用执行记录重写。
_PREAMBLE_RE = re.compile(
    r'^(?:好的?|嗯|我|先|那|来|帮|马上|这就)*'
    r'(?:看一下|看看|瞧瞧|试试|试一下|查一下|找一下|操作一下|处理一下|等一下|稍等|马上)')
# "打算做"的意图词 / 动作词 / "已完成"的交付词 —— 三者组合起来判定
_INTENT = ('我先', '我来', '我这就', '我马上', '让我', '我帮你', '我给你',
           '准备', '正在', '马上', '稍等', '等一下', '试试', '看一下', '看看')
_ACTION = ('打开', '启动', '关掉', '关闭', '截图', '截个图', '搜索', '搜一下',
           '查', '找', '操作', '处理', '执行', '运行', '发给')
_DONE = ('完成', '已经', '成功', '好了', '搞定', '弄好', '办好', '做完',
         '马上好', '快好', '就好', '搞定')
# "已经做完了"这种完成句。**关键: 这类话本身不是证据, 工具执行记录才是。**
# 实测(本机 4B): "帮我打开记事本" -> 它回"我已经打开了记事本。", 一个工具都没调,
# 窗口根本没出现 —— 而 _DONE 里含"已经", 于是这句谎话被当成真交付直接发出去了。
# 所以下面 _is_preamble 必须带 worked(有没有执行记录), 不能只看它嘴上怎么说。
_CLAIM_RE = re.compile(r'(?:打开|启动|运行|关闭|关掉|截图|执行|查|搜|完成|弄好)'
                       r'(?:了|好了|成功|完成)(?!吗|么|没|不)')
# 结果文本里出现这些 = 这次操作没成功(pc_tools 里逐条写死的措辞)
# 这张表必须跟着 pc_tools.dispatch 的返回口径走, 漏一个就等于把失败当成功:
# 实测 15:07 "工具 sleep 参数不对: ..." 不含下面任何一条, 于是 _worked() 认为
# "确实做成了", 六轮下来一个文件没建, 最后那句"我已成功创建"就被当成有证据的交付。
# 10-14 又漏一类: 语法闸门拦回时 pc_tools 回的是"**没写入**: xxx 编译不过, 拒绝落盘",
# 这几个词都不在表里, 于是"文件根本没落盘"被当成"写成功了"的证据 —— 假成功检测
# (_guarded/_is_preamble)全跟着失灵。写/改文件的三条拒绝措辞必须在这里有一份。
_BAD_HINT = ('失败', '没看到新窗口', '不是有效的程序名', '没找到', '不允许', '不确定',
             '参数不对', '执行出错', '没有这个工具', '被安全策略拒绝',
             '弹出错误框', '并没有被执行', '激活失败',
             '跑出错了', '跑不起来',
             '没写入', '没改', '拒绝落盘', '不接受相对路径')
# 只有"真动过电脑 / 真取回了信息"的工具才算干活证据 —— 用来把 _worked() 从
# "碰过任何工具"收窄到"确实产出了东西"。下面这七个是看和等, 不产生任何产出:
#   read_file / list_windows / focus_window / screenshot / ocr_screen / get_clipboard / sleep
_ACT_TOOLS = ('write_file', 'run_py', 'run_java', 'edit_file', 'run_command', 'open_app',
              'win_run',
              'send_keys', 'type_text', 'click', 'scroll', 'paste_to_app', 'set_clipboard',
              'shot_to_wechat', 'web_search', 'http_get')
# list_files 不在这里: 它只是"看", 不产出任何东西 —— 跟 read_file/screenshot 同类。

# ---- 搜索预算: 一条消息最多真搜几次 ----
# 实测 10-07 18:33 对方只说了句「搜索一下:J空间是什么」, 六轮里模型搜了 5 次:
# 第 1 次是对的, 之后两次换个说法重搜同一个问题, 最后两次越搜越远(去搜"codebuddy
# 的功能和使用方法"), 117 秒后交白卷。搜索引擎是**有代价的外部动作**(每次 2~6 秒,
# 还有被反爬盯上的风险), 不能由着它无限烧。
# 超预算就把搜索工具从工具表里摘掉(跟授权闸同一套做法: 它听不懂拒绝, 但"这个工具
# 压根不在选项里"是它能懂的死路), 逼它用手里已有的结果作答。
_SEARCH_TOOLS = ('web_search', 'http_get')
_SEARCH_BUDGET = 3

# ---- 搜偏判据 ----
# 同一轮里对方问的是"J空间是什么", 它第三、五次搜的却是"codebuddy 截图功能" /
# "codebuddy 的功能和使用方法" —— 那是上一轮对话的残影, 跟当前问题毫无关系。
# 这种情况去重闸拦不住(参数确实不一样), 只能在"搜索词跟原话有没有关系"上判。
# 只在**已经搜过一次之后**才启用: 第一次搜索一律放行, 免得误伤正常的同义扩展
# (实测那种情况交集依然很高, 例如「苹果股价」->「AAPL 股价」= 2/4)。
_OFFTOPIC_RATIO = 0.34
_OFFTOPIC_LIMIT = 2


def _hanzi(s):
    return {c for c in str(s or '') if '\u4e00' <= c <= '\u9fff'}


def _search_offtopic(query, user_text):
    """这条搜索词是不是跑到别的题目上去了。

    判据: 搜索词里的汉字, 只有不到三分之一出现在对方原话里 = 换题了。
    汉字为空的搜索词(如纯英文/纯符号)一律放行 —— 说不出它跑偏, 就别拦。
    """
    q = _hanzi(query)
    if not q:
        return False
    u = _hanzi(user_text)
    if not u:
        return False
    return len(q & u) / len(q) < _OFFTOPIC_RATIO


# ============================================================
# 对外动作授权闸: 没人让它发, 它却发出去了 —— 最高危的一类越权
# ============================================================
# 实测(10-07 19:21): 对方只说了一句「告诉我桌面上的static_thoughts.txt的内容」,
# 4B 进决策循环后第1轮 screenshot、第2轮 shot_to_wechat 往聊天窗口发了张截图、
# 第3轮又发一张(第三张才被"重复调用"闸挡掉), 117 秒后把"云南是个省份"当成那个
# 文件的内容回了过去。对方从头到尾一个字没提过微信, 桌面却真被截图、真发进了窗口。
#
# 这类动作跟 read_file 有本质区别: 它**在对方的真实世界里留下痕迹**, 发出去收不回。
# 所以不能跟只读工具一样交给 4B 自行判断 —— 必须由对方原话授权。
# 判据是"原话里有没有这个意图", 不是"模型觉不觉得该做"。
# 宁可判严: 拒了模型会看到拒绝文本并改道(最多多花一轮), 漏了就是真越权。
#
# 不拦 screenshot / ocr_screen / get_clipboard / focus_window / scroll / list_windows:
# 它们是"看", 不往外面发东西(截图只落盘, 配合 ocr_screen 读屏是正当用法)。
_OUTWARD = (
    ('shot_to_wechat', '截图发进聊天窗口',
     r'截图|截个图|发张图|发个图|发图|发给我|发我|发过来|发到|发给|传过来|'
     r'发微信|给你看|给你发'),
    # 给"另一个联系人"发消息是典型对外动作: 对方没明说要发, 就不许替他找人。
    # 实测教训同 paste_to_app —— 少一道闸, 4B 会在无关任务里顺手把消息发出去。
    ('wechat_send', '给微信里其他人发消息',
     r'发消息|发条消息|发个消息|发信息|发条信息|发微信|发给|发到|发送给|发送|'
     r'告诉他|告诉她|'
     r'通知他|通知她|提醒他|提醒她|跟他说|跟她说|问他|问她|问一下|问问|'
     r'联系他|联系她|转告|转发给|回复他|回复她|回他|回她'),
    ('paste_to_app', '把一段话发进别的程序',
     r'发给|发到|发送|发消息|发条消息|回他|回她|回一下|回复他|回复她|'
     r'贴到|粘到|粘贴到|粘贴进|说给|发微信|转告'),
    ('send_keys', '敲键盘',
     r'按|敲|输入|键入|回车|快捷键|ctrl|alt|tab|esc'),
    ('type_text', '打字',
     r'打字|输入|键入|填上|填入|写上|写在|打上|打到'),
    ('click', '点鼠标',
     r'点击|单击|点一下|点它|点那个|点这个|按一下|双击'),
    ('open_app', '打开一个程序',
     r'打开|启动|运行|开一下|开个|调起'),
    ('win_run', '用运行框启动东西',
     r'运行|启动|打开|win\s*\+\s*r|运行框'),
    # 命令 / 代码执行是 RCE 面, 必须对方**原话**明确授权才放行(网页 / 文件 / OCR
    # 里被投毒的"运行这段命令"不算授权 —— 它不在对方原话 user_text 里, 这道闸据此
    # 挡住提示注入驱动的任意命令执行)。注意: 程序内部自己的兜底调用走的是
    # pc_tools.run_command 直连, 不经过此闸(那是代码逻辑, 不是模型决策)。
    ('run_command', '在电脑上执行系统命令',
     r'运行|执行|跑一下|跑个|跑这|跑命令|命令|cmd|终端|terminal|powershell|'
     r'脚本|代码|python|java|编译|build|起服务|启动服务|看进程|查进程|任务|command'),
    ('run_py', '运行 python 脚本',
     r'运行|执行|跑一下|跑个|python|脚本|代码|\.py'),
    ('run_java', '运行 java 程序',
     r'运行|执行|跑一下|跑个|java|编译|build|程序'),
)

_OUTWARD_MAP = {n: (h, re.compile(p, re.I)) for n, h, p in _OUTWARD}


def _outward_ok(name, user_text):
    """这个动作, 对方原话里授权了吗。不在表里 = 只读/本地工具, 一律放行。"""
    hit = _OUTWARD_MAP.get(name)
    if not hit:
        return True
    return bool(hit[1].search(str(user_text or '')))


def _outward_refuse(name, user_text):
    """拒绝文本。必须带"不允许", _BAD_HINT 认这个词 -> _worked() 才不会把它
    当成一次"干成了活"的证据(否则拒绝本身反而成了谎报完成的素材)。"""
    human = _OUTWARD_MAP.get(name, ('', None))[0]
    return (f'不允许: 对方这条消息没有让你{human}, 你也没有被授权动他的窗口。'
            f'他说的原话是「{str(user_text or "")[:40]}」。'
            f'不要再调 {name}, 换个参数重试一样会被拒。'
            f'这只否掉 {name} 一个动作, 不代表整个任务不能做 —— 换别的工具继续'
            f'把他真正要的那件事做完, 再来回话; 要是确实需要{human}, '
            f'就在正文里用一句话问他同不同意, 然后等他回复。')


# ============================================================
# 破坏闸: 会真删掉磁盘东西的工具, 原话里没有删除意图就不许动
# ============================================================
# 实测(10-07 21:47 微信真链路): 对方说的是「帮我在桌面创建一个名为poem的文件夹
# libai的py类型 这个需要实现输出静夜思」—— 通篇一个字没提删东西, 4B 却连着两轮
# delete_file, 路径还是它自己编的 C:\Users\User\Documents\to_delete.txt(用户名都
# 是错的)。要建文件夹的任务跑去删文件, 接着 write_file 撞上 WinError 5, 最后回
# 一句"我这边没弄成"。越权闸管"发出去收不回", 这道闸管"删掉回不来" —— 交给 4B
# 自己判断就是迟早误删真文件。判据同样是原话里有没有这个意图, 不是模型觉不觉得该做。
# 宁可判严: 拒了模型会看到拒绝文本并改道(最多多花一轮), 漏了就是真删。
_DESTRUCTIVE = (
    ('delete_file', '删掉磁盘上的东西',
     r'删|清空|清除|清掉|清理|移除|去掉|不要了|扔掉|干掉|回收'),
)

_DANGER_MAP = {n: (h, re.compile(p, re.I)) for n, h, p in _DESTRUCTIVE}


def _danger_ok(name, user_text):
    """这个破坏性动作, 对方原话里授权了吗。不在表里 = 只读/本地工具, 一律放行。"""
    hit = _DANGER_MAP.get(name)
    if not hit:
        return True
    return bool(hit[1].search(str(user_text or '')))


def _danger_refuse(name, user_text):
    """拒绝文本。一样要带"不允许", 免得 _BAD_HINT / _worked() 把拒绝当成干成了活。

    还要明说"这只否掉了这一个动作": 实测越权闸的拒绝话术被 4B 读成"整件事都没授权",
    于是它连着两轮回"根据规则, 我不能直接在桌面上创建文件夹或文件", 活不干了。
    """
    human = _DANGER_MAP.get(name, ('', None))[0]
    return (f'不允许: 对方这条消息没有让你{human}, 不要动他磁盘上的任何东西。'
            f'他说的原话是「{str(user_text or "")[:60]}」。'
            f'注意这只否掉 {name} 一个动作, 不代表整个任务不能做: 看清楚他到底要什么, '
            f'换对应的工具继续做完; 真要删也得先问过他。')


# ============================================================
# 目标一致性闸: 写文件的**目标**和**内容**必须在对方原话里有出处
# ============================================================
# 实测 10-07 21:47: 对方要"桌面\poem 文件夹里的 libai(输出静夜思)", 4B 却发了
#   write_file(path="C:\Users\username\Documents\example.txt",
#              content="This is the content of the file that the user requested.")
# 用户名是编的、文件名是编的、内容是英文样例。_fix_path 会把编出来的用户名接回真
# 家目录, 于是这坨样例**真的落到了盘上**; 最后 _vague 兜底照着执行记录报"已经写好
# example.txt 了" —— 对方要 libai, 拿到的是一句英文样例。两个东西都要拦:
#   (1) 内容本身就是样例/占位(example/test/占位词), 没人会真要这个;
#   (2) 造一个新文件夹的任务, 文件却建在文件夹外。
# 只拦"内容长得就是样例"和"文件名是样例词且原话里根本没提过它"这两种 —— 不按
# "文件名必须原话里有" 一刀切: "写个脚本算 1到5的和" 这种好任务压根不会提文件名。
_WRITERS = ('write_file', 'edit_file')

_DEMO_NAMES = frozenset((
    'example', 'sample', 'demo', 'untitled', 'newfile', 'new_file', 'temp',
    'placeholder', 'output', 'file', 'test', 'foo', 'bar', 'yourfile',
))

_DEMO_CONTENT_RE = re.compile(
    r'the user requested|content of the file|this is the content|placeholder|'
    r'lorem ipsum|your content here|示例内容|这里写内容|待补充内容|TODO:',
    re.I)

# 对方点名要的是"带名字的产物"(文件/脚本/代码/程序)
_ASK_FILE_RE = re.compile(r'文件|脚本|代码|程序|\bpy\b|python', re.I)


def _target_ok(name, args, user_text):
    """这个写文件的目标, 像不像对方真要的东西。非写文件工具一律放行。"""
    if name not in _WRITERS:
        return True
    path = str((args or {}).get('path') or '')
    if not path:
        return True
    text = str(user_text or '')
    stem = os.path.splitext(os.path.basename(path))[0].strip().lower()
    # (1) 内容是样例/占位
    if _DEMO_CONTENT_RE.search(str((args or {}).get('content') or '')):
        return False
    # (2) 文件名是样例词, 而对方原话里压根没提过它
    if stem in _DEMO_NAMES and stem not in text.lower():
        return False
    # (3) 对方点了名要"新建一个 X 文件夹", 文件却建在这个文件夹外面
    dirs = pc_skills._new_dirs(text)
    if dirs and not any(d.lower() in path.lower() for d in dirs):
        return False
    # (4) 对方要的是带名字的产物, 却把目标写成了目录本身
    # 实测 10-07 21:47 复跑第 4 轮: 目标是 C:\Users\用户\Desktop\poem(文件夹本身,
    # 没有文件名), 写进去直接 Errno 13 Permission denied —— 那一轮又白烧掉了。
    if not os.path.splitext(os.path.basename(path))[1] and _ASK_FILE_RE.search(text):
        return False
    return True


def _target_refuse(name, args, user_text):
    """拒绝文本: 说清"对方要的是什么"并给出现在就该用的完整目标路径。"""
    text = str(user_text or '')
    paths = _wanted_paths(text)
    want = ('对方要的是这些文件:\n' + '\n'.join(f'  - {p}' for p in paths)) if paths \
        else '对方要的那个文件, 文件名和位置都在他的原话里'
    return (f'不允许: 你刚才给 {name} 的目标或内容是编的(样例名/样例内容/或者建在'
            f'对方点名的文件夹外面), 发出去对方拿到的是没用的东西。'
            f'他说的原话是「{text[:60]}」。{want}。'
            f'用上面那个完整路径、写对方真正要的内容, 立刻重调一次 {name}; '
            f'不要解释、不要写计划、不要放弃。')


# 去重的"身份参数": 只认这一个参数, 其余的都是旋钮(engine / count / timeout …),
# 改旋钮不等于换了活。实测 10-07 19:38: 第1轮 web_search{"query":"什么","engine":"bing"}
# 真搜了; 第2轮塞了个 count=8 就成了新 key, 同一句"什么"又搜一遍; 第3轮原样重发才
# 被拦下 —— 4B 拿到不满意的结果时不会换思路, 只会往参数里加个旋钮再试一次。
_ID_ARG = {'web_search': 'query', 'http_get': 'url', 'run_command': 'command',
           'run_py': 'path', 'run_java': 'path', 'list_files': 'path',
           'read_file': 'path', 'write_file': 'path',
           'edit_file': 'path', 'paste_to_app': 'app', 'open_app': 'name',
           'type_text': 'text',
           # 技能/子代理按"查什么"判重: 同一个技能/同一个任务再发一遍不会变
           'use_skill': 'name', 'list_skills': 'keyword', 'subagent': 'task'}

# 写文件类除了路径, 还要拿**正文**当身份的一部分。详见 _call_key。
_BODY_ARGS = {'write_file': ('content',), 'edit_file': ('old_str', 'new_str')}


def _call_key(name, args):
    """这一次调用的"身份" —— 用来判"是不是又在原地重试刚才那一下"。

    写文件类必须连正文一起算。只认路径的话, "同一个路径、把内容改好再写一次"会被
    当成重复调用拦掉 —— 而那恰恰是编译失败之后唯一该走的一步。实测 10-07 22:58
    真模型复现: 第2轮 write_file(poem 里的 libai.py, 抄来的 torch 样例代码)编译不过被拒,
    第5轮它把内容改好、重发同一个路径, 却撞上"刚才已经用完全一样的参数调用过
    write_file" —— 去重闸把"改对了再试一次"和"原地瞎重试"当成一回事, 唯一能成的
    那一步被堵死, 6 轮烧光只留下正文里那句 print("静")。
    """
    arg = _ID_ARG.get(name)
    if arg and isinstance(args, dict) and args.get(arg) is not None:
        ident = re.sub(r'\s+', '', str(args[arg])).lower()
        for k in _BODY_ARGS.get(name, ()):
            ident += '\x00' + str((args or {}).get(k) or '')
        return (name, ident)
    return (name, json.dumps(args, ensure_ascii=False, sort_keys=True))


# 授权类闸(越权/破坏)拒同一个工具到这个次数, 就把它从工具表里摘掉。
# 为什么必须摘: 实测 10-07 22:15(真模型复现对方那句"建 poem 文件夹 + libai的py")
# 模型连着 5 轮发 open_app, 越权闸挡 5 次, 每次都拿到同一句"换别的工具继续", 它一个字
# 没听进去 —— 6 轮全烧光, 最后回"我这边没弄成 你再说一遍我试试"。根因是**被挡的调用
# 不占"同参数只放行一次"的名额**(那名额是留给真调用的), 于是同一个注定被拒的动作可以
# 无限重发; 而 4B 拿到同一句话重复几遍也不会换路, 只会照着再发一遍。
# 判据按工具名而不是按参数: _outward_ok/_danger_ok 只看(工具名, 原话), 参数怎么改结果
# 都一样 —— 按参数计数的话它换个 name 就是一个新 key, 照样能无限试。
_REFUSE_LIMIT = 2


def _gate_refuse(name, refused, first, wanted=()):
    """授权类闸拒绝一次, 返回 (给模型看的拒绝文本, 要不要把这个工具摘掉)。

    第一次还是原话术; 到 _REFUSE_LIMIT 次就升级: 明说"工具已被摘掉, 再发一样被拒",
    并把对方真正要的那些路径摆在它面前 —— 光说"换别的工具"它不知道该换哪个, 给了
    具体路径才有下一步可走。
    """
    n = refused.get(name, 0) + 1
    refused[name] = n
    if n < _REFUSE_LIMIT:
        return first, False
    want = ('\n对方要的产物就是这些, 完整路径直接拿去用:\n'
            + '\n'.join(f'  - {p}' for p in wanted)) if wanted else ''
    return (f'{first}\n'
            f'【这是第 {n} 次】{name} 在对方这条消息里就是不许用的: 程序已经把它从你的'
            f'工具表里摘掉了, 再发一次拿到的还是这段一模一样的字。现在只剩一条路 —— '
            f'换别的工具把对方真正要的那件事做出来, 不要解释、不要写计划、'
            f'不要回"做不到"{want}'), True


# 程序自己造的控制文本 —— 是写给模型看的, 一个字都不许出现在对方那里。
_CTRL_STARTS = ('（', '不允许:')

# 工具层自己造的报错, 同样是写给模型看的: 权限被拒、参数不对、占位符、没有这个工具。
# 实测 10-07 21:47: 对方要"建 poem 文件夹 + 写 libai.py", write_file 撞上 WinError 5,
# _result_note 把"返回结果: 工具 write_file 被安全策略拒绝: [WinError 5] 拒绝访问。"
# 原样播进了微信 —— 对方收到一串他既看不懂、也帮不上的东西, 就是"答非所问"的观感。
_CTRL_MARKS = ('被安全策略拒绝', '参数不对', '占位符', '没有这个工具',
               '并没有被执行', '执行出错')


def _is_ctrl(result):
    """这条 result 是"程序的控制文本", 还是"工具的真产出"。

    实测 10-07 19:38: 对方只说了句"什么", 六轮里第 3 轮撞上去重闸, 那句
    "（刚才已经用完全一样的参数调用过 web_search 了…）"被 _result_note 原样播进了
    微信 —— 对方收到的是"返回结果: 刚才已经用完全一样的参数调用过 web_search 了"。
    这是内部机制的话, 对方看不懂, 也不该看到。
    """
    s = str(result or '').lstrip()
    return s.startswith(_CTRL_STARTS) or any(m in s for m in _CTRL_MARKS)


# 快答通道让模型"需要动手/联网时就只回一个标记词"。
# 实测本机 4B 会写成 need_tools(尖括号掉了), 而判定原来是严格找 '<need_tools>' ——
# 漏判的后果有两个: 这个标记词被当成最终答复发给对方(对方收到一个光秃秃的
# need_tools), 而且本该转进决策循环去联网/动手的活被挡在门外(现象=它说自己搜不了)。
_NEED_TOOLS_RE = re.compile(r'^[\s<>`\'"*_#\-]*need_tools[\s<>`\'"*_。.!！,，:：]*$', re.I)


def _need_tools(text):
    """整条回复就是那个标记词才算; 尖括号/引号/标点/空白都容错。"""
    return bool(_NEED_TOOLS_RE.match(str(text or '').strip()))


# 程序级标点兜底: 提示词里已经交代"不要用中文标点", 但 4B 遵守不稳,
# 交付前再扫一道 —— 中文标点一律换成空格(换行保留), 保证对方永远看不到中文句读。
_CN_PUNCT_RE = re.compile(r'[，。！？；：、”“‘’（）《》〈〉【】「」『』…—～·]')


def _strip_cn_punct(text):
    """把中文标点替成空格并压掉多余空行(提示词之外的第二道保险)"""
    s = str(text or '')
    if not s:
        return s
    s = _CN_PUNCT_RE.sub(' ', s)
    s = re.sub(r'[ \t]{2,}', ' ', s)
    s = re.sub(r'[ \t]+(\n)', r'\1', s)
    return re.sub(r'\n{3,}', '\n\n', s).strip()


def _tool_names():
    """真实工具名清单 —— 从工具表现取, 免得两边不同步

    联网工具(web_search/http_get)不用在这里另加: pc_tools 末尾已经把 net_tools
    的 spec 和函数合并进来了(pc_tools 里 TOOL_SPECS + / _TOOL_FUNCS.update),
    这里再拼一次只会让同一个工具在模型眼里出现两份。
    """
    try:
        return tuple(s['function']['name'] for s in pc_tools.TOOL_SPECS)
    except Exception:
        return ()


def _worked(trace):
    """执行记录里有没有"确实做成"的证据 —— 只有真动过电脑/真取回信息的工具才算。

    trace 为空 = 这条消息一次工具都没调过, 那模型嘴里所有"已经打开了/完成了"
    都是空口汇报(本机 4B 的固定病: 说了就等于做了)。
    判宽: 任何一条结果不带失败特征就算做成了 —— 免得把真做了的当谎话重写。

    只对动手类工具(_ACT_TOOLS)判宽。list_windows / focus_window / sleep / screenshot /
    ocr_screen / get_clipboard / read_file 是"看和等", 什么产出都没有, 拿它们当"我做好了"
    的证据就是漏洞。实测 10-07 18:07 "在桌面写一个加法计算器": 六轮只调了 list_windows +
    focus_window("微信"), 原来这里照样返回 True, 于是那句"在桌面的桌面上已经写好了加法
    计算器"被当成有证据的交付发了出去 —— 桌面上根本没有任何文件。
    """
    for t in trace or ():
        if (t or {}).get('tool') not in _ACT_TOOLS:
            continue
        result = str(t.get('result') or '')
        if not any(b in result for b in _BAD_HINT):
            return True
    return False


def _read_ok(trace):
    """trace 里有没有"真把某个文件读出来过"的成功记录。

    单独拉出来判, 是因为 web_search 也在 _ACT_TOOLS 里 —— 它确实"取回了信息",
    可"网上搜到的东西"根本不是"这个文件里写了什么"的证据。详见 _fake_read_claim。
    """
    for t in trace or ():
        tool = (t or {}).get('tool')
        r = str((t or {}).get('result') or '')
        if tool == 'read_file':
            if r and not any(b in r for b in _BAD_HINT):
                return True
        # 子代理读过文件也算: 文件内容进了子代理的上下文, 结论是它的产物, 主模型
        # 复述"文件里写的是…"就不是编的。判据是结论抬头里带 read_file 的用量
        # (subagent.py 会把用过哪些工具写进抬头), 不看结论正文。
        elif tool == 'subagent' and '子代理结论' in r and 'read_file' in r:
            return True
    return False


def _wrote_ok(result):
    """这次写文件/改文件是不是真落地了(只看 pc_tools 的成功抬头)"""
    r = str(result or '')
    return r.startswith('已写入') or r.startswith('已改')


def _selfcheck_pending(trace):
    """trace 里写过 .py/.java, 但之后没有一次成功的验证 -> 返回那个路径, 否则 None。

    为什么要在循环层当门禁, 而不是只靠提示词: "写完不跑"是本机 4B 的稳定行为。
    实测 10-07 17:37 "帮我在桌面写一个猜数字py" —— 它开记事本把 261 字符敲了一遍
    就说"我先用记事本看看有没有写错的地方"交付了, 文件根本不存在; 而那份代码里
    大小提示全是反的, 它也从没跑过。pc_tools._py_syntax_gate 只能拦语法错, 逻辑错
    只有真跑一遍才暴露 —— 所以"写过 .py 就得跑过一次且跑通"放在这里兜底, 模型
    漏掉这一步时把它推回去(只推一次, 免得原地打转)。

    Java 走同一个门禁, 但判定不同: 单文件没有"语法自检"这回事(类之间互相引用),
    只能全量 javac, 所以只认 run_java 的通过。而且**不比 basename**: run_java 编译的
    是整个目录, 跑一次就覆盖了全部类, 拿"跑的是 Main.java、欠自检的是 Book.java"
    去比会永远清不掉。
    """
    pending = None              # None / 'py:路径' / 'java:路径' 都靠后缀判, 这里只存路径
    for t in trace or ():
        name = t.get('tool')
        args = t.get('args') or {}
        result = str(t.get('result') or '')
        if name in ('write_file', 'edit_file'):
            path = str(args.get('path') or '')
            low = path.lower()
            if low.endswith(pc_tools.PY_EXTS) or low.endswith('.java'):
                pending = path if _wrote_ok(result) else None
        elif name in ('run_py', 'run_java'):
            if not pending:
                continue
            is_py = pending.lower().endswith(pc_tools.PY_EXTS)
            if is_py and name != 'run_py':
                continue
            if not is_py and name != 'run_java':
                continue
            if not any(k in result for k in pc_tools.RUNPY_PASS):
                continue
            if is_py:
                same = (os.path.basename(str(args.get('path') or ''))
                        == os.path.basename(pending))
                if not same:
                    continue
            pending = None              # 跑通过了, 不再欠自检
    return pending


def _auto_run(path):
    """程序代跑一次 .py / .java —— 模型叫不动这一步时, 由程序补上。

    为什么需要它: "写完不跑"是稳定的模型行为(本机 4B 从来想不起跑; GLM-4.5-Flash
    实测 10-14 把 gss.py 一次写对, 偏偏漏了 run_py, 整条任务就卡在最后一步)。
    SELFCHECK_NUDGE 只在前几轮推得动, 末轮程序摘了工具表, 它只能把想调的 run_py
    写成 XML 文本 —— 于是这一步永远没人做。

    先裸跑; 读到 EOF(交互式程序没有输入)就补一组顺序输入再跑一次 ——
    猜数字这类 input() 程序不喂输入只会 EOFError, 那不代表程序坏了。
    输入喂 **1..100** 而不是随便几个数: 猜数字要"猜中"才算自检通过(才肯打印
    恭喜/次数), 只喂 7 个大概率猜不中, 程序以退出码 1 结束, _run_ok 判不过 ——
    实测 10-14 就这么误过一次。
    """
    if str(path).lower().endswith('.java'):
        # Java: 编译走整个目录, gui=true 让窗口程序起得来(图书管理系统基本都有界面)。
        return pc_tools.dispatch('run_java',
                                 {'path': path, 'gui': True, 'timeout': 60})
    r = pc_tools.dispatch('run_py', {'path': path})
    if 'EOFError' in str(r):
        r2 = pc_tools.dispatch('run_py', {
            'path': path,
            'stdin_text': '\n'.join(str(i) for i in range(1, 101)) + '\n',
            'timeout': 30})
        if 'EOFError' not in str(r2):
            return r2
    return r


# 对方明说要"产出一份文件"(在桌面写/创建某个 .py、新建文件夹…), 但循环跑到交付时
# 一次写文件的工具都没调过 —— 本机 4B 会滑进"教对方怎么做"的模式, 把工具名当说明书
# 写成一整篇步骤。实测 10-07 18:07 "在桌面写一个加法计算器": 第1轮吐了 521 字
# "1. 打开记事本: 使用 open_app…", 六轮下来只 list_windows/focus_window, 文件一个没建。
# 与 _selfcheck_pending 是一对: 那个管"写完没跑", 这个管"该写却没写"。
_FILE_TASK_RE = re.compile(r'(写|创建|新建|生成|做|保存|导出)')
_FILE_HINT_RE = re.compile(r'(桌面|文件夹|目录|文件|脚本|程序|代码|游戏|计算器'
                           r'|\.py|\.txt|\.md|\.js|\.html|\.json|\.csv)')
_WRITE_TOOLS = ('write_file', 'edit_file')


def _file_task_pending(user_text, trace):
    """对方要的是文件, 但一次写文件的工具都没调过 -> True(该把它推去 write_file)。

    只认"动作词 + 文件线索"同时出现, 免得把"帮我打开计算器"(没有产出文件的意思)算进来。
    """
    s = str(user_text or '')
    if not (_FILE_TASK_RE.search(s) and _FILE_HINT_RE.search(s)):
        return False
    return not any((t or {}).get('tool') in _WRITE_TOOLS for t in trace or ())


# "我要先做 X, 然后我会做 Y" 这种多步骤计划腔。两条同时命中才算 ——
# 只认"我需要/我得/我要 + 先|去|在|把|创建"起步, 再跟上"然后/接着/最后 + 会|将|再"。
# 单看前半句会误杀闲聊("我要先睡了"), 加上后半句就只剩真正的计划书了。
_PLAN_RE = re.compile(r'我(?:需要|得|要|打算|准备|计划)\s*(?:先|去|来|在|用|把|创建|新建|写|生成|打开)')
_PLAN_NEXT_RE = re.compile(r'(?:然后|接着|之后|下一步|最后)\s*我?\s*(?:会|要|将|再|就|把|去)')


def _is_preamble(text, worked=False):
    """这句话是不是"只有过程话/预告/空口汇报, 没有实质答复"

    worked = 有没有执行记录做支撑(见 _worked)。**没有证据时, "我已经打开了记事本"
    这类完成句一律按过程话处理** —— 否则模型什么都不做也能骗过检测, 把谎话发给对方。

    只在决策循环里用(闲聊走快速通道, 不经过这里), 所以宁可判宽一点:
    误判的代价只是多重写一句, 漏判的代价是对方收到"我先看一下"然后干等。
    """
    s = re.sub(r'[\s。.!！~,，、?？:：;；]+', '', str(text or ''))
    if not s:
        return False

    # 过程话腔开头的一律先按过程话处理 —— 这条不能只躺在下面的短句分支里 ----------
    # 实测 10-07 18:58 「向我展示桌面static_thoughts.txt的内容」, 它交回的正文是
    # 「我先看一下桌面上的static_thoughts.txt文件内容」: 剥掉标点 31 字, 因为超过
    # 下面长句分支的 24 字上限, 连 _PREAMBLE_RE 都没机会跑, 直接掉进去被放行发了
    # 出去 —— 而它一个工具都没调、那个文件一个字都没读。文件名是 ASCII, 一句话里
    # 挂个 .txt 就足以把短句撑成长句, 所以"是不是过程话腔"必须跟句长脱钩。
    if _PREAMBLE_RE.match(s):
        # 唯一的例外仍是"后半句已经把答案给出了": 带数字的长句
        # ("我先看一下系统时间 现在是下午3点整")。
        if len(s) > 12 and any(c.isdigit() for c in s):
            return False
        return True

    if len(s) > 24:
        # 长句也可能是整段的计划, 不能一律放行。实测 76 字被快速通道当成答复发出去了:
        # "我需要先在桌面上创建一个名为bubble_sort.cpp的文件并编写冒泡排序的C++代码
        #  然后我会用文本编辑器打开这个文件输入代码并保存最后我将确认文件是否成功创建"
        # —— 文件一个字都没写。长句判不准, 所以只认最突出的两种特征:
        #   a) 正文里在念工具名(它把说明书当结果了)
        #   b) "我要先做X, 然后我会做Y" 的多步骤计划腔, 且没有任何已完成的证据
        #   c) 长句里的"完成声明"同样要验证据。实测 15:07 "帮我在桌面创建文件夹…",
        #      六轮下来工具全失败, 最后一轮交付 78 字的"我已成功在桌面创建了名为poem
        #      的文件夹 并在里面放置了a2fd381ed.py…" —— 没有工具名、也不是计划腔,
        #      就这么漏过去发出去了, 而对方桌面上根本没有那个文件夹。
        if worked:
            return False
        if any(n and n.lower() in s.lower() for n in _tool_names()):
            return True
        if _CLAIM_RE.search(s) or any(w in s for w in _DONE):
            return True
        return bool(_PLAN_RE.search(s) and _PLAN_NEXT_RE.search(s))
    # 正文里出现工具名 = 它在念说明书, 不是报结果(要发给人的话里不该有 open_app 这种词)
    if any(n and n.lower() in s.lower() for n in _tool_names()):
        return not any(w in s for w in _DONE)
    if _CLAIM_RE.search(s) or any(w in s for w in _DONE):
        return not worked          # 嘴上说做完了, 但记录里没证据 -> 空话
    if any(w in s for w in _INTENT) and (any(w in s for w in _ACTION) or len(s) <= 10):
        return True                               # "我这就去打开计算器 / 我马上来"
    return False


# ============================================================
# 主入口: 两段式
# ============================================================
def _loop(session, max_steps, on_note=None, user_text=''):
    """单循环: 每轮由模型自己判断「继续 / 调工具 / 交付」。

    user_text = 对方原话。只用来做程序侧门禁(_file_task_pending: 要产出文件却一直没写),
    不进模型历史。

    模型一轮的输出被拆成三块, 程序只解析和执行, 判断权始终在模型:
      1) 进度话术(可选) —— 事件通道: 走 on_note(打印/可选发送), 不进历史
      2) tool_calls(可选) —— 真执行, 结果回灌, 继续下一轮
      3) 末尾标记 —— <continue> 把本轮内容当下一轮提示词回灌继续;
                     <finish> 或没写标记 = 交付, 标记以上的正文就是要发的话

    护栏(不交给模型, 本机 4B 判断不了):
      * max_steps 到顶: 收走工具 + 用 DELIVER_NUDGE 逼它交付
      * 连续 AGENT_STALL_LIMIT 轮没有新工具结果、思考又和上一轮重复 -> 直接交付
    返回 (reply or None, trace, 实际轮数)
    """
    trace = []
    called = {}
    scaffold = []          # 脚手架消息下标, 结束时撤掉, 内心独白不进历史
    last_think = ''
    stall = 0
    reply = None
    step = 0
    nudged = False         # "只说不做"只提醒一次, 免得原地打转
    selfnudged = False     # "写了 .py 没跑过"也只推一次
    pending_rounds = 0     # 连续几轮欠自检(>=3 就由程序代跑, 见 _loop 里那段)
    dup_n = {}             # 每个工具被"重复调用"拦下的次数(>=3 摘工具)
    filenudged = False     # "对方要文件却一次都没写文件"也只推一次
    readnudged = False     # "没读过文件却敢报文件内容"也只推一次
    resultnudged = False   # "报了个记录里没有的结果"也只推一次
    quotenudged = False    # "引号里报了个记录里没有的结果"也只推一次
    cmdnudged = False      # "没回答对方问的那条命令的输出"也只推一次
    emptynudged = False    # "这一轮什么都没产出"也只在第一次给重来的机会
    jsondodged = False     # "正文是一段工具调用 JSON"也只推一次
    codenudged = False     # "正文是一行代码"也只推一次
    echonudged = False     # "正文在复读程序写给它的控制文本"也只推一次
    noted = 0              # 已经播报了几条进度(总上限 config.INTERIM_MAX_COUNT)
    last_seen = None       # 上一次**尝试**播报的文案(去重看它, 见 emit 里的说明)
    last_note_t = 0.0      # 上一条播报的时刻(时间闸, 见 config.INTERIM_MIN_GAP)
    last = False           # 当前是不是最后一轮(emit 会读它)
    refused = {}           # 授权类闸按"工具名"数的拒绝次数(见 _gate_refuse / _REFUSE_LIMIT)
    banned = set()         # 被授权类闸拒到上限的工具名 —— 后面几轮直接从工具表里摘掉
    search_n = 0           # 这条消息真搜了几次(见 _SEARCH_BUDGET)
    offtopic = 0           # 搜索跑偏被拦了几次(见 _search_offtopic / _OFFTOPIC_LIMIT)

    def emit(note):
        """播报一条进度。走 on_note 事件通道, 不进模型历史。

        为什么要计数上限: 每条进度都要完整走一遍 sender(激活微信窗口 + ctrl+F
        搜索会话 + OCR 复核目标 + 粘贴发送), 实测单条 6~8s。一条消息最多 6 轮
        工具循环, 每轮"正在…"+"返回结果…"两条 —— 不封顶就是十几条、一分半钟
        全花在发送上, 对方只看到刷屏。

        但光有上限挡不住长任务: 实测一轮 127s 的搜索任务把 6 条吃满、49.8s 全花在
        播报上(占端到端 43%), 而这 8s 是串行加在链路里的, 等于把答案往后拖。所以
        这里再按时间闸(INTERIM_MIN_GAP)+ 去重 + "最后一轮不播"三道收紧。
        """
        nonlocal noted, last_seen, last_note_t
        if not note or on_note is None or noted >= config.INTERIM_MAX_COUNT:
            return
        # 最后一轮: 它的下一步就是最终答案, 播了纯为多花 8s。
        if last:
            return
        # 连着两遍一样的话只播一次。注意去重比的是"上一次**尝试**播的文案", 不是
        # "上一次真播出去的": 每轮会试两次("正在搜索" / "返回结果…"), 后者常被时间闸
        # 挡掉 —— 若只记真正播出去的那条, 后面所有"正在搜索"都会被判成重复, 时间闸就
        # 退化成了"只播一次"(实测 145.9s 的任务全程只播了 1 条, 对方以为它死了)。
        if note == last_seen:
            return
        last_seen = note
        now = time.time()
        if last_note_t and (now - last_note_t) < config.INTERIM_MIN_GAP:
            return
        noted += 1
        last_note_t = now
        print(f'[agent] 播报进度 {noted}/{config.INTERIM_MAX_COUNT}: {note}')
        on_note(note)

    for step in range(1, max_steps + 1):
        last = step == max_steps
        if last:
            scaffold.append(len(session.messages))
            session.messages.append({'role': 'user', 'content': config.DELIVER_NUDGE})

        t0 = time.time()
        # 被授权类闸拒到上限的工具, 这一轮直接从工具表里摘掉 —— 拒绝话术它听不进去
        # (见 _REFUSE_LIMIT), 只有"这个工具压根不在选项里"才是它能懂的死路。全被摘光
        # 就退回 None(不给工具), 让它只能把话说完。
        #
        # 末轮必须摘掉工具表 —— 试过留着它(图省下 KV 缓存失效的钱), 不行, 已回退:
        # 带工具表时末轮只会吐一个"结束标记"(4/4 次都只回 2~4 个字; 端到端交付直接
        # 变成"结束标记"这四个字), 摘掉它才会正常写出要发给人话。大概是工具段把模型
        # 带进了"调工具"的语境, 而"标上结束标记"那句就只剩标记可写了。
        # 所以末轮这 5.8s prefill 是**正确交付的代价**, 不是浪费。留档数据(别再试):
        # 同一段 3.5k tok 上下文, 前缀不变 prefill 0.06s / 一变 9.6s(本机真实 prefill
        # 仅 ~370 tok/s, 0.06s 那组是缓存命中), 即末轮 8.6s 里有 5.8s 花在这。
        specs = None
        if not last:
            specs = [s for s in pc_tools.TOOL_SPECS
                     if s['function']['name'] not in banned] or None
        try:
            msg = _chat_once(session.wire(), tools=specs)
        except Exception as e:
            print(f'[agent] 模型调用失败: {e}')
            break

        content = _strip_stray_think(msg.get('content') or '')
        # ↑ 必须在这里就剥掉孤立的 </think>: 实测它漏进正文会把"我先用 open_app
        #   打开 powershell。"撑到 33 字, 撞上 _is_preamble 的 24 字上限 -> 过程话
        #   被当成交付放行(同一句话没残渣时却能正确拦住, 行为随残渣飘忽)。
        think = msg.get('thinking') or ''
        tool_calls = msg.get('tool_calls') or []
        print(f'[agent] 第{step}轮 {time.time() - t0:.1f}s ({msg.get("_via") or "?"}) '
              f'(思维链 {len(think)} 字 / 正文 {len(content)} 字 / '
              f'工具 {len(tool_calls)} 个)'
              + (f' 正文={content[:60]!r}' if content else ''))

        # 模型没用原生工具通道, 但正文里写了 JSON 调用 -> 手工接住(最后一轮不接)
        if not tool_calls and not last:
            guess = _text_tool_call(content)
            if guess:
                tool_calls = [{'function': {'name': guess[0], 'arguments': guess[1]}}]

        mark, body = _split_mark(content)

        # ---------- 情况一: 这一轮要动手 ----------
        if tool_calls:
            # 执行前: 优先用模型自己写的进度话术; 它没写就用工具名兜底,
            # 保证对方至少能看到"正在搜索…"这类反馈。
            # 但播报只给**会被批准**的动作: emit 走的是真实 sender(激活微信 + 粘贴
            # 发送), 给一个注定被拒的动作播一句"正在截图发给你", 就等于自己也发了
            # 条消息出去。全被拒时连模型自己写的过程话也不播(见 _OUTWARD)。
            sayable = [tc for tc in tool_calls
                       if _outward_ok(tc['function']['name'], user_text)
                       and tc['function']['name'] not in banned]
            if sayable:
                emit(_extract_interim(body or content)
                     or _tool_say(sayable[0]['function']['name']))
            session.messages.append({
                'role': 'assistant',
                'content': body,
                'tool_calls': [{'function': {'name': tc['function']['name'],
                                             'arguments': tc['function'].get('arguments') or {}}}
                               for tc in tool_calls],
            })
            for tc in tool_calls:
                name = tc['function']['name']
                args = tc['function'].get('arguments') or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                key = _call_key(name, args)
                # 越权闸放最前: 对方没让做的对外动作, 连试都不试。也不进 called ——
                # 别让它占掉"同参数只放行一次"的名额(那名额是留给真调用的)。
                blocked = not _outward_ok(name, user_text)
                if blocked:
                    result, dead = _gate_refuse(name, refused,
                                                _outward_refuse(name, user_text),
                                                _wanted_paths(user_text))
                    print(f'[agent] 越权闸拦下 {name}(第 {refused[name]} 次): '
                          f'原话里没有这个授权' + (' -> 摘掉这个工具' if dead else ''))
                    banned.update({name} if dead else ())
                # 破坏闸紧跟其后, 同样不占"同参数只放行一次"的名额。
                elif not _danger_ok(name, user_text):
                    result, dead = _gate_refuse(name, refused,
                                                _danger_refuse(name, user_text),
                                                _wanted_paths(user_text))
                    print(f'[agent] 破坏闸拦下 {name}(第 {refused[name]} 次): '
                          f'原话里没有删除意图' + (' -> 摘掉这个工具' if dead else ''))
                    banned.update({name} if dead else ())
                # 目标一致性闸: 写文件的路径/内容是编的(样例名/样例内容/建在点名的
                # 文件夹外)也不占名额 —— 拦下来是为了让它用对的目标**重调一次**。
                elif not _target_ok(name, args, user_text):
                    # 目标闸**不进 refused、也不升级话术**: 它跟上面两道性质不同 ——
                    # 授权类闸拒了就是"这工具不能用", 而目标闸拒了要的是"用**对的目标**
                    # 重调一次 write_file"。把 write_file 摘掉等于这活彻底没法做, 所以
                    # 这里只给 _target_refuse 那套带正确路径的指路话术, 不参与摘除。
                    print(f'[agent] 目标闸拦下 {name}: 目标或内容是编的'
                          f' path={str((args or {}).get("path"))[:70]!r}')
                    result = _target_refuse(name, args, user_text)
                # 搜索预算: 超过上限就摘掉搜索工具, 不再让它烧轮次。
                # 见 _SEARCH_BUDGET —— 这是"搜索循环"的解药: 实测它能在六轮里搜 5 次,
                # 还越搜越远, 每次 2~6 秒, 对方那边就是干等两分钟。
                elif name in _SEARCH_TOOLS and search_n >= _SEARCH_BUDGET:
                    result = (f'（这条消息已经搜过 {search_n} 次, 到上限了。'
                              f'现在只能用手上已有的搜索结果作答, 不许再搜; '
                              f'搜不到就如实告诉对方没搜到, 别再试。）')
                    banned.update(_SEARCH_TOOLS)
                    print(f'[agent] 搜索预算用完(已搜 {search_n} 次), 摘掉搜索工具')
                # 搜偏: 搜索词跟对方问的几乎不沾 -> 拦下, 连拦两次也摘工具。
                # 见 _search_offtopic —— 实测问"J空间是什么"时它去搜"codebuddy 的功能
                # 和使用方法", 那是上一轮对话的残影, 搜了也白搜。
                elif (name == 'web_search' and search_n >= 1
                      and _search_offtopic(args.get('query'), user_text)):
                    offtopic += 1
                    result = ('（这句搜索词跟对方问的不是同一件事, 程序没执行它, '
                              '搜了也是浪费。回到对方原话里那件事上, 换一句跟它明显'
                              '相关的说法; 或者直接用已有的结果作答。）')
                    print(f'[agent] 搜索跑偏拦下(第 {offtopic} 次): '
                          f'query={str((args or {}).get("query"))[:40]!r}')
                    if offtopic >= _OFFTOPIC_LIMIT:
                        banned.update(_SEARCH_TOOLS)
                        print(f'[agent] 连续 {offtopic} 次搜偏, 摘掉搜索工具')
                # 同一个调用只放行一次。实测 13:55 "帮我搜一下陈胜世家": 第1轮搜完,
                # 第2轮拿着同一份不理想的结果又搜了一遍一模一样的词, 白烧 6 秒 ——
                # 4B 拿到不满意的工具结果时不会换思路, 只会原地重试。
                # 判据是 _call_key(身份参数), 不是整串 args —— 否则加个 count 就绕开了。
                elif called.get(key, 0) >= 1:
                    # 同一个调用被去重拦下的次数。实测 17:5x: 模型对 run_py 用同一组
                    # 参数连撞 20+ 轮(每轮都只回一个字), 因为它拿不到"新结果"就不会
                    # 换思路。拦到第 3 次就直接把这个工具从工具表摘掉, 逼它交付;
                    # 交付门禁那边会由程序代跑补上自检。
                    dup_n[name] = dup_n.get(name, 0) + 1
                    if dup_n[name] >= 3 and name not in banned:
                        banned.add(name)
                        print(f'[agent] {name} 连续被去重 {dup_n[name]} 次, 摘掉该工具')
                    # 去重拦下之后必须给"下一步怎么换"的具体线索, 否则 4B 会继续
                    # 用同一组参数空转。
                    hint = ''
                    if name == 'run_py' and not (
                            isinstance(args, dict) and args.get('stdin_text')):
                        hint = ('；如果这是交互式程序(有 input()), 必须传 stdin_text 喂输入,'
                                '例如 stdin_text=\'50\\n\' 或 \'50\\n75\\n\'，换一组输入再跑')
                    elif name in ('run_command', 'run_py'):
                        hint = '；换一条命令/换一组输入，别重复同一条'
                    result = (f'（刚才已经用完全一样的参数调用过 {name} 了, 结果就在上面, '
                              f'再调一次不会变。换别的参数、换别的工具，或者直接作答{hint}）')
                else:
                    called[key] = called.get(key, 0) + 1
                    if name in _SEARCH_TOOLS:
                        search_n += 1
                    print(f'[agent] 用工具 {name} {json.dumps(args, ensure_ascii=False)}')
                    result = pc_tools.dispatch(name, args)
                print(f'[agent] 结果: {str(result)[:200]}')
                trace.append({'tool': name, 'args': args, 'result': result})
                # 执行后: 把真实结果播报一句。这句是程序从 result 生成的 ——
                # "正在搜索"可以是模型说的, "搜到了什么"必须对着执行结果说。
                # 但程序自造的控制文本(越权拒绝 / 去重拦截)一律不播: 那是写给模型看的,
                # 发出去对方只会看到"刚才已经用完全一样的参数调用过 web_search 了"。
                if (config.INTERIM_RESULT and not _is_ctrl(result)
                        and name not in _NO_RESULT_NOTE):
                    emit(_result_note(result))
                session.messages.append({'role': 'tool', 'content': result,
                                         'tool_name': name})
            # 对方明确要"造一个新文件", 可这一轮调的全是别的工具, 一次都没碰
            # write_file/edit_file -> 早点把它点回正轨, 别等它交付再说。
            # 实测五类回归 长任务: 第1轮 run_command 跑一条必败的 shell 一行流, 第2轮
            # delete_file 一个随机临时文件, 第3~5轮 list_windows / focus_window 微信 ——
            # 六轮打满一次没写过文件。而它唯一那次交付落在最后一轮, 那时已经没有轮次可推:
            # 门禁只能拒绝, 对方什么也没拿到。所以提醒必须**提前到还有轮次的时候**发。
            if (_make_file_intent(user_text)
                    and _file_task_pending(user_text, trace)
                    and not filenudged and not last):
                filenudged = True
                print(f'[agent] 第{step}轮没碰写文件工具, 点它去 write_file')
                scaffold.append(len(session.messages))
                session.messages.append(
                    {'role': 'user',
                     'content': config.WRITE_NUDGE.format(
                         path=_path_hint(user_text))})
                stall = 0
                continue
            stall = 0
            continue

        # ---------- 情况二: 模型自己说还要继续 ----------
        if mark == 'continue' and not last:
            print(f'[agent] 判断: 继续思考 -> {body[:60]!r}')
            stall = stall + 1 if _similar(think or body, last_think) else 0
            last_think = think or body
            scaffold.append(len(session.messages))
            session.messages.append({'role': 'assistant',
                                     'content': body or content or '（继续）'})
            scaffold.append(len(session.messages))
            session.messages.append({'role': 'user', 'content': config.CONTINUE_NUDGE})
            if stall >= config.AGENT_STALL_LIMIT:
                print(f'[agent] 连续 {stall} 轮没进展, 强制交付')
                reply = _clean(body or content)
                break
            continue

        # ---------- 情况三: 交付 ----------
        # 但"我先看一下"这类过程话不是答复。实测(本机 4B): 问"帮我打开计算器",
        # 它会回一句"我先看一下。"就交付, 结果程序一个没开、对方还收到一句废话。
        said = body or content
        # 这一轮什么都没产出: 既没正文, 也没工具调用。别把它当"交付"处理 ——
        # 交付是"说给对方的答案", 空的就是没有答案。实测五类回归 长任务 第2轮:
        # 40.4 秒 / 0 字正文 / 0 个工具, 整轮废掉; 老代码把它当交付往下走, 撞上写文件
        # 门禁被拒后就再也没下文了(对方拿到的是兜底那句"已经帮你打开资源管理器了")。
        # 给一次重来的机会, 比白白烧掉一整轮强。
        if not tool_calls and not str(said or '').strip():
            if not emptynudged and not last:
                emptynudged = True
                print('[agent] 这一轮什么都没产出, 推它重来一次')
                scaffold.append(len(session.messages))
                session.messages.append({'role': 'user', 'content': config.EMPTY_NUDGE})
                continue
            reply = None
            break
        # 门禁: 整条正文就是一段工具调用(JSON 或 XML) -> 绝不能发出去, 推它改用工具
        # 通道或用人话交代。这是"它把调用写进正文、正则又没兜住"时的最后一道闸;
        # 漏了它的后果实测很直接: 对方收到一串 JSON/XML, 活没干完, 后续也没人管了。
        if not tool_calls and _looks_like_tool_json(said):
            if not jsondodged and not last:
                jsondodged = True
                print(f'[agent] 交付的是工具调用(JSON/XML), 不发出去, 推它说人话: {said[:60]!r}')
                scaffold.append(len(session.messages))
                session.messages.append({'role': 'user', 'content': config.JSON_NUDGE})
                continue
            print(f'[agent] 还是工具调用(JSON/XML), 交回兜底总结: {said[:60]!r}')
            reply = None
            break
        # 门禁: 交付的正文只是代码(单行或整段) —— 那是它想写进文件的东西, 不是给对方的话。
        # 实测 10-07 22:58 真模型复现: 六轮没做成之后最后一轮交了 `print("静")`, 程序
        # 不认识这种交付, 原样发了出去 —— 对方收到一行代码, 既看不出没做成, 也不知道
        # 该说什么。跟 JSON 那条一样处理: 推它说人话, 最后一轮干脆交回兜底总结。
        # 10-14 公平测试 D 组又复现了多行版本(整段猜数字源码从兜底总结漏了出去),
        # 判据已扩到整段, 见 _looks_like_code。
        if not tool_calls and _looks_like_code(said):
            if not codenudged and not last:
                codenudged = True
                print(f'[agent] 交付的是代码不是话, 推它说人话: {said[:60]!r}')
                scaffold.append(len(session.messages))
                session.messages.append({'role': 'user', 'content': config.CODE_NUDGE})
                continue
            print(f'[agent] 还是代码不是话, 交回兜底总结: {said[:60]!r}')
            reply = None
            break
        # 门禁: 交付在复读**程序写给它的控制文本**(工具报错/参数提示/防重复提示)。
        # 实测 10-14: write_file 新加的相对路径闸门把"真桌面是 ... 必须写完整绝对路径"
        # 回给它, 它下一轮原样转述给了对方, 任务一个都没做。那些文本是写给模型看的,
        # 对方看到只会更糊涂。跟代码那条一样: 推它说人话, 最后一轮交回兜底总结。
        if not tool_calls and (_ECHO_TEXT_RE.search(str(said))
                               or _lecturing(said, trace)):
            if not echonudged and not last:
                echonudged = True
                print(f'[agent] 交付在复读控制文本, 推它说人话: {said[:60]!r}')
                scaffold.append(len(session.messages))
                session.messages.append({'role': 'user', 'content': config.ECHO_NUDGE})
                continue
            print(f'[agent] 还在复读控制文本, 交回兜底总结: {said[:60]!r}')
            reply = None
            break
        # 门禁: 写过 .py 却一次都没跑通 -> 这次不许交付, 先推回去自检。
        # 放在过程话检查之前: "写完了"这句话在 _is_preamble 眼里是有证据的长句
        # (write_file 成功算 worked), 会被直接放行 —— 只有这里能拦住它。
        pending = _selfcheck_pending(trace)
        pending_rounds = (pending_rounds + 1) if pending else 0
        if pending and not selfnudged and not last:
            selfnudged = True
            is_java = str(pending).lower().endswith('.java')
            print(f'[agent] {os.path.basename(pending)} 写完还没'
                  f'{"编译" if is_java else "跑"}过, 不许交付, 先自检')
            scaffold.append(len(session.messages))
            nudge = (config.JAVA_SELFCHECK_NUDGE if is_java
                     else config.SELFCHECK_NUDGE)
            session.messages.append({'role': 'user', 'content':
                                     nudge.format(path=pending)})
            continue
        # 推过一次还是没跑: 别再等末轮了。实测 17:46 猜数字: 模型连着 20+ 轮用
        # 同一组参数调 run_py, 全被去重挡住, 一直卡到第 30 轮才由程序代跑。
        # 现在从"欠自检的第 3 轮"起就由程序代跑 —— 正常的 3 步(建/写/跑)本来就该
        # 在第 3 轮结束, 超过就是模型在原地转。
        if pending and (last or pending_rounds >= 3):
            r = _auto_run(pending)
            tool_name = ('run_java' if str(pending).lower().endswith('.java')
                         else 'run_py')
            trace.append({'tool': tool_name, 'args': {'path': pending}, 'result': r})
            print(f'[agent] 欠自检第 {pending_rounds} 轮, 由程序代跑 '
                  f'{os.path.basename(pending)}: {str(r)[:160]}')
        elif pending and not last:
            # 还欠自检又没到代跑阈值: 这一轮不许交付, 推回去再试一次。
            continue
            if _run_ok(r):
                # 代跑出了真结果, 照执行记录重说一句 —— 原来那句往往只是"已经写好了"
                better = _report(trace)
                if better:
                    said = better
            else:
                # 代跑也没跑通: 照实说, 别让"写好了"听起来像成功
                err = str(r).replace('\n', ' ')[:120]
                said = f'{said} 不过我跑了一下没跑通 {err}'.strip()
        # 门禁: 对方要的是一份文件, 可到现在一次都没写成功过(或者写的是别的文件) ->
        # 别让它拿"1. 打开记事本…"这种步骤说明交差, 更别让它交付"已经建好了"。
        # 原来这里只有"推它一次"、没有拒绝支 —— 而且判据是 _file_task_pending(只看
        # "写过任何文件吗")。实测五类回归两条都漏了:
        #   py开发: 它真写了, 但写的是 桌面\guess.py(照抄了 WRITE_NUDGE 里的示例路径),
        #           交付却说"在 regress_5 文件夹里新建了一个 hello_5.py";
        #   长任务: 一次 write_file 都没调过, 交付却说"已经完成…建了 nums.txt 和 sum_5.py"。
        # 所以判据改成"交付点名的那个文件到底写过没有", 且最后一轮不许放行。
        # 两条判据都留着, 各catch一路:
        #   _file_task_pending —— 对方要文件, 可**压根没调过**写文件工具(不管交付说没说
        #     "建好了")。实测五类回归 长任务: 它只 open_app + 一条跑失败的 run_command,
        #     然后交了一段英文排错说明。那段话里没有任何"建好了"的字样, 只靠
        #     _fake_write_claim 会漏掉 —— 英文交付、或者干脆认输的交付, 都归这条管。
        #   _fake_write_claim —— 调是调了, 但写的**不是**对方要的那个文件, 交付却报它建好了。
        if said and not tool_calls and (_fake_write_claim(said, trace, user_text)
                                        or _file_task_pending(user_text, trace)):
            if not filenudged and not last:
                filenudged = True
                print(f'[agent] 还没写出对方要的文件, 推它去 write_file: {said[:60]!r}')
                scaffold.append(len(session.messages))
                session.messages.append(
                    {'role': 'user',
                     'content': config.WRITE_NUDGE.format(
                         path=_path_hint(user_text))})
                continue
            print(f'[agent] 没写出文件却报"建好了", 拒绝放行: {said[:60]!r}')
            reply = None
            break
        # 门禁: 交付在"念某个文件的内容", 可这一轮根本没成功读过任何文件 -> 编的。
        # 必须排在 _is_preamble 之前: web_search 成功也算 worked, 那句
        # "static_thoughts.txt 里的内容是 云南是中国的一个省份"在 _is_preamble 眼里是
        # "有证据的长句"(worked=True 直接 return False), 会被原样放行 —— 只有这里拦得住。
        if not tool_calls and _fake_read_claim(said, trace, user_text):
            if not readnudged and not last:
                readnudged = True
                print(f'[agent] 没读过文件却在报文件内容, 推它去 read_file: {said[:60]!r}')
                scaffold.append(len(session.messages))
                session.messages.append({'role': 'user', 'content': config.READ_NUDGE})
                continue
            print(f'[agent] 没读过文件却报文件内容, 拒绝放行: {said[:60]!r}')
            reply = None
            break
        # 门禁: 交付里报了个具体版本号/结果, 可这一轮跑命令的输出里根本没这个数 -> 编的。
        # 实测五类回归「运行 python --version，把版本号告诉我」: 4B 选了 win_run(把字
        # 丢进 Win+R 运行框), 回执是"已用 Win+R 执行: python --version" —— 听着像成了,
        # 其实一个字符的输出都没拿到。它随后交付"Python 版本是 3.9.7 finish", 而本机真
        # 版本是 3.13.7, 整条执行记录里都没有 3.9.7。放出去就是拿编的数糊弄对方。
        if not tool_calls and _fake_result_claim(said, trace):
            if not resultnudged and not last:
                resultnudged = True
                print(f'[agent] 报的结果在记录里找不到, 推它去 run_command: {said[:60]!r}')
                scaffold.append(len(session.messages))
                session.messages.append({'role': 'user', 'content': config.RESULT_NUDGE})
                continue
            print(f'[agent] 报的结果没有执行记录支撑, 拒绝放行: {said[:60]!r}')
            reply = None
            break
        # 门禁: 交付里用引号报了一段"跑出来的东西", 可执行记录里没有那段 -> 编的。
        # 实测 10-07 23:04 真链路复现: run_py 的真实输出是 `Hello, LibAI!`, 它交付的却是
        # "...输出结果为：'床前明月光，疑是地上霜...'"。文件是真的, 报的结果是假的。
        if not tool_calls and _fake_quote_claim(said, trace, user_text):
            if not quotenudged and not last:
                quotenudged = True
                print(f'[agent] 引号里报的结果记录里没有, 推它照实说: {said[:60]!r}')
                scaffold.append(len(session.messages))
                session.messages.append({'role': 'user', 'content': config.QUOTE_NUDGE})
                continue
            print(f'[agent] 引号里报的结果还是没有记录支撑, 拒绝放行: {said[:60]!r}')
            reply = None
            break
        # 门禁: 对方要的是某条命令的直接输出, 交付里却连那条命令都没提 -> 那不是他要的答案。
        # 实测五类回归 命令行操作: 第1轮就拿到了 3.13.7, 它却接着 sleep 两次、跑去搜
        # "codebuddy 的功能"、最后交付一整段 CodeBuddy 介绍。上面那条 _fake_result_claim
        # 拦不住, 因为这段回话里压根没有任何版本号。拒绝放行之后, 兜底会走 _rescue_run
        # 把同一条命令真跑一遍, 用它的真实输出作答。
        if not tool_calls and _cmd_out_pending(user_text, said):
            if not cmdnudged and not last:
                cmdnudged = True
                print(f'[agent] 没回答对方问的命令输出, 推它照实说: {said[:60]!r}')
                scaffold.append(len(session.messages))
                session.messages.append(
                    {'role': 'user',
                     'content': config.CMD_NUDGE.format(cmd=_cmd_asked(user_text))})
                continue
            print(f'[agent] 还是没回答对方问的命令输出, 拒绝放行: {said[:60]!r}')
            reply = None
            break
        if not tool_calls and _is_preamble(said, _worked(trace)):
            if not nudged and not last:
                nudged = True
                print(f'[agent] 交付的只是一句过程话, 提醒它真动手: {said[:60]!r}')
                scaffold.append(len(session.messages))
                session.messages.append({'role': 'user',
                                         'content': config.PREAMBLE_NUDGE})
                continue
            # 提醒过、或者已经是最后一轮 —— 这句没有证据的话绝不能放行。
            # 这里原来写的是 not nudged: 第一轮提醒过一次之后, 整个循环就再也不检查了,
            # 而最后一轮正是 4B 最爱报"已成功完成"的地方(实测 15:07 那句"我已成功在
            # 桌面创建了名为poem的文件夹"就是这么发出的, 对方桌面上根本没有那个文件夹)。
            # 交回 ask() 走真兜底: 要么按记录说实情, 要么如实说没弄成。
            print(f'[agent] 没有证据的交付, 拒绝放行: {said[:60]!r}')
            reply = None
            break
        why = ('最后一轮' if last else
               '标记 finish' if mark == 'finish' else '没写标记(默认交付)')
        print(f'[agent] 判断: 交付({why})')
        reply = _clean(body or content)
        break

    for i in reversed(scaffold):
        del session.messages[i]
    return (reply or None), trace, step


def _compose(session, user_text, trace):
    """兜底总结: 循环没能自主交付时(模型报错/只有 continue/交付轮空产出),
    只看「原话 + 执行记录」单独生成要发出去的正文 —— 保证有事做过就一定有回话。"""
    log = []
    for i, t in enumerate(trace, 1):
        log.append(f'{i}. 工具 {t["tool"]}  参数 '
                   f'{json.dumps(t["args"], ensure_ascii=False)}')
        log.append(f'   结果 {str(t["result"])[:800]}')
    scaffold = (f'{config.SUMMARY_PROMPT}\n\n'
                f'【对方原话】\n{user_text}\n\n'
                f'【工具执行记录】\n' + '\n'.join(log))

    session.messages.append({'role': 'user', 'content': scaffold})
    try:
        msg = _chat_once(session.messages,      # 不给工具: 逼它只能出正文
                         think=False, num_predict=config.FAST_NUM_PREDICT)
    except Exception as e:
        print(f'[agent] 总结阶段失败: {e}')
        return None
    finally:
        session.messages.pop()                  # 脚手架问句撤掉, 不进历史

    reply = _clean(msg.get('content') or '')
    if not reply:
        print('[agent] 总结阶段没产出正文')
        return None
    session.assistant(reply)
    return reply


def _recent_replies(session, n=3):
    """最近几条已经发出去过的助手正文 —— 用来识破"复述上一轮"""
    out = []
    for m in reversed(list(session.messages or ())):
        if m.get('role') == 'assistant':
            out.append(str(m.get('content') or ''))
            if len(out) >= n:
                break
    return out


def _repeat_of_history(reply, olds):
    """这条回复是不是把之前发过的话又抄了一遍

    实测: "帮我在控制台输出hello" 它把上一轮的 "echo hello 已经执行完了..." 原样
    发了出去 —— 看着像执行过, 其实这一轮啥也没干。用 _similar 逐字比太脆
    (冒号、空白差一点就漏), 这里去掉符号后按相似度比。
    """
    def norm(x):
        return re.sub(r'[\s:：,，.。;；!！?？"\'`*_\-]+', '', str(x or '')).lower()

    a = norm(reply)
    if len(a) < 12:
        return False
    import difflib
    for old in olds:
        b = norm(old)
        if b and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.9):
            return True
    return False


# ============================================================
# 错字预检(成语)
# ============================================================
# 为什么不让模型自己纠: 实测 6 组对照(脚本 _exp_correct.py) —— 问"王公好龙是什么意思",
# 4B 会现编一个不存在的出处("王公好龙指王孙好龙"); 明着问"这写法对吗", 它先说
# "不太对", 然后又把错词原样抄一遍当正确答案; 开 think 更糟(118s 后一个字没吐出来)。
# 可同一个模型问正确的"叶公好龙"却答得一字不差 —— 知识是有的, 缺的只是"错词 -> 正词"
# 这一步检索映射, 而这本来就是查表能定的事。所以交给程序: 错词查不到、改一个字就命中
# 词表, 就把正确写法直接摆到模型眼前, 不用它自己发现。
#
# 第二轮实锤(14:40 微信真链路): 光给"正确写法"只修好了字, 没修好义 —— 它把叶公好龙
# 解释成"对于自己不擅长或不了解的事物容易产生疑虑", 跟原义毫不相干。释义同样是查表
# 能定的事, 所以词表里连着权威释义一起读进来, 命中就直接抄给它。
_IDIOM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'dicts', 'idioms.tsv')
_IDIOMS = None        # 词表(set)
_IDIOM_DEF = None     # 词 -> 权威释义
_IDIOM_IDX = None     # (词长, 第几位, 去掉这一位剩下的字) -> 词
_CN_RUN_RE = re.compile(r'[\u4e00-\u9fa5]{3,}')
# 句子里出现这些字眼 = 对方在问成语的意思/出处, 这时候才附释义;
# 否则正常聊天里塞一段词典解释反而干扰。
_ASK_IDIOM_RE = re.compile(r'什么|啥|意思|含义|释义|解释|出处|出自|典故|来历|来源|成语|怎么说|怎么讲')


def _idioms():
    """惰性读成语表(3 万条 / 2.2MB, 只读一次), 并建"差一个字"的索引。

    索引键 = (词长, 第几位, 去掉这一位剩下的字), 值 = 原词。这样"某个词改一个字
    能不能变成表里的成语"就退化成 4 次字典查找 —— 否则按字逐个试要 4 x 4000 次。
    读不到表就静默关掉整个功能, 不影响其它流程。
    """
    global _IDIOMS, _IDIOM_DEF, _IDIOM_IDX
    if _IDIOMS is None:
        _IDIOMS, _IDIOM_DEF, _IDIOM_IDX = set(), {}, {}
        try:
            with open(_IDIOM_PATH, encoding='utf-8') as f:
                for line in f:
                    w, _, meaning = line.rstrip('\n').partition('\t')
                    w = w.strip()
                    if not w:
                        continue
                    _IDIOMS.add(w)
                    if meaning.strip():
                        _IDIOM_DEF[w] = meaning.strip()
            for w in _IDIOMS:
                for i in range(len(w)):
                    _IDIOM_IDX.setdefault((len(w), i, w[:i] + w[i + 1:]), w)
        except OSError as e:
            print(f'[agent] 成语表不可用({e}), 跳过错字预检')
            _IDIOMS, _IDIOM_DEF, _IDIOM_IDX = set(), {}, {}
    return _IDIOMS


def _edit1(word):
    """word 只改一个字就能变成表里的成语 -> 返回那个正确写法, 否则 None

    只认"替换"(长度不变): 笔误绝大多数是同长度的形近/音近字替换
    (王公好龙->叶公好龙、井底之鱼->井底之蛙)。删字/插字误报高得多, 不做。
    """
    for i in range(len(word)):
        good = _IDIOM_IDX.get((len(word), i, word[:i] + word[i + 1:]))
        if good and good != word:
            return good
    return None


def _idiom_def(word):
    """查成语的权威释义(表里没有就返回空串)"""
    return (_IDIOM_DEF or {}).get(word, '')


def _typo_hint(text):
    """给模型补一条它自己查不到的成语事实 -> 返回提示文本, 没有就 None

    两件事:
      a) 笔误: "王公好龙" 只改一个字就落进词表 -> 点明正确写法 (先解决"字")
      b) 释义: 修正后的那个词、以及对方明摆着在问的那个成语, 把权威释义抄给它
         (再解决"义" —— 实测 4B 自己解释叶公好龙会编成"对不擅长的事物容易产生疑虑")
    释义一律来自词表, 不让它现场编, 这是这个函数存在的全部理由。
    """
    tbl = _idioms()
    if not tbl:
        return None
    ask = bool(_ASK_IDIOM_RE.search(str(text or '')))
    fixed, correct = [], []
    for run in _CN_RUN_RE.findall(str(text or '')):
        for size in (4, 3):              # 成语以四字为主, 另有少量三字(莫须有)
            for i in range(len(run) - size + 1):
                w = run[i:i + size]
                if w in tbl:
                    if w not in correct:
                        correct.append(w)
                    continue             # 本身就在词表里, 不是错字
                good = _edit1(w)
                if good and (w, good) not in fixed:
                    fixed.append((w, good))
    if not fixed and not (ask and correct):
        return None
    lines = []
    if fixed:
        pairs = '、'.join(f'"{w}" 其实是 "{g}"' for w, g in fixed)
        # 抽象要求("先点明正确写法")4B 会直接跳过 —— 实测它上来就"叶公好龙的意思是...",
        # 对方根本看不出它发现了错字。给个例句它就照做了。
        example = f'你说的"{fixed[0][0]}"应该是"{fixed[0][1]}"'
        lines.append(f'[程序预检] 对方这句话里可能有笔误: {pairs}。'
                     f'回答的第一句话必须先点明, 例如: {example}。')
    targets = [g for _, g in fixed]                  # 修正后的词必须给释义
    if ask:
        targets += [w for w in correct if w not in targets]
    for w in targets[:2]:
        meaning = _idiom_def(w)
        if meaning:
            lines.append(f'"{w}" 的释义（用这一条，不要自己另编）: {meaning}')
    return ' '.join(lines)


# 用户嘴里那个具体文件名(带扩展名)。判据见 _hit_triggers。
_FILENAME_RE = re.compile(
    r'[\w\u4e00-\u9fa5-]{1,64}\.'
    r'(?:txt|md|py|pyw|json|log|csv|tsv|xml|ini|cfg|conf|yaml|yml|toml|'
    r'doc|docx|xls|xlsx|ppt|pptx|pdf|rtf|'
    r'png|jpe?g|gif|bmp|webp|svg|ico|'
    r'mp3|wav|flac|mp4|avi|mkv|mov|wmv|flv|'
    r'zip|rar|7z|tar|gz|iso|exe|msi|bat|cmd|ps1|vbs|sh|'
    r'c|h|cpp|hpp|cs|java|js|jsx|ts|tsx|vue|go|rs|rb|php|swift|kt|sql|db|sqlite)'
    # 结尾**不能**用 \b: Python3 的 re 默认 Unicode, 中文算 \w, 于是 "txt的内容" 里
    # txt 和 的 之间没有词边界, \b 永远匹配不上 —— 这个正则曾整体失效(search 全 None)。
    # 只要求后面不是 ASCII 词字符即可。
    r'(?![A-Za-z0-9_])',
    re.I)

# 交付里在"念某个文件的内容"的迹象。
_READ_CLAIM_RE = re.compile(r'(内容|原文|正文|写的是|写着|里面是|里面写|里面说|'
                            r'大概是|意思是|讲的是)')


def _fake_read_claim(reply, trace, user_text=''):
    """交付在"念某个文件的内容", 这一轮却没成功读过任何文件 -> True(编的)。

    实测 10-07 19:38: 对方只说了句"什么", 六轮调了 5 次 web_search(搜的就是"什么"这个
    问句本身), 最后交付"static_thoughts.txt 里的内容是 云南是中国的一个省份 不是城市" ——
    那个文件一个字都没读过。同一句"云南是个省份"在 19:21 那条也出现过。
    为什么 _is_preamble 拦不住: web_search 在 _ACT_TOOLS 里, _worked() 见它成功就返回
    True, 于是这句被当成"有证据"放行了。可"网上搜到的"不是"这个文件里写了什么"。
    所以这里单判一条: 话里点了文件名(或对方本来就是冲着某个文件来的), 又在讲"内容",
    就必须有成功的 read_file 垫底。
    """
    s = str(reply or '')
    if not _READ_CLAIM_RE.search(s):
        return False
    if not (_FILENAME_RE.search(s) or _FILENAME_RE.search(str(user_text or ''))):
        return False
    return not _read_ok(trace)


# "我建好了 / 写好了 / 已经完成" 这类完成腔调 —— 只认**造东西**的说法, 不认"已经读到
# 了""已经看过了"。宽一点最多多逼它真写一次, 漏了就是把没发生的事发出去。
_WRITE_CLAIM_RE = re.compile(
    r'已写入|成功写入|已保存|已生成|已创建|已新建|已经保存|保存好了|'
    r'新建了|创建了|生成了|建了|写了|保存了|写好了|建好了|做好了|完成了|完成')

_WRITTEN_TOOLS = ('write_file', 'edit_file')

# "libai的py类型" / "libai 的 python" —— 没写扩展名点, 但就是"要个 libai.py"。
# 定义在 pc_skills(意图门那边也要用它把这种句子挡在专线外), 这里直接引用同一份,
# 免得两处判据各自漂移 —— 一处认一处不认, 就又回到"文件夹建了、文件没写"的老路上。
_WANT_PY_RE = pc_skills.WANT_PY_RE


def _wanted_paths(user_text):
    """对方要的**所有**文件的完整路径(按原话出现顺序, 去重); 拼不出来返回 []。

    只为一个用途: 往提示里填**对方真正要的那些路径**。实测五类回归, WRITE_NUDGE 里
    原本写着"（比如 C:\\Users\\<用户名>\\Desktop\\guess.py）", 4B 把示例路径原样抄走了 ——
    让它建 桌面\\regress_5\\hello_5.py, 它建的是 桌面\\guess.py, 然后交付"在 regress_5
    文件夹里新建了一个 hello_5.py"。提示里绝不能再出现一个能被照抄的路径。
    要多个文件的任务(长任务: nums.txt + sum_5.py)也得把**每一个**都点名, 否则它建完
    第一个就以为交差了。
    """
    s = str(user_text or '')
    if '桌面' not in s:
        return []
    desk = os.path.join(os.path.expanduser('~'), 'Desktop')
    # 对方没写扩展名点, 但意图很明确: "libai的py类型" / "libai 的 python" = 要个 libai.py。
    # 实测 10-07 21:47: 原话正是"libai的py类型", _FILENAME_RE 找不到带扩展名的名字,
    # 于是这里返回 [], WRITE_NUDGE 那句"写到哪个文件"根本不触发, 6 轮全在瞎撞, 最后
    # 回一句"我这边没弄成"。推法保持保守: 名字必须紧贴在 py 前面, 猜不出就不猜。
    # 先问 pc_skills 要"新建的文件夹名"。旧正则 \S+文件夹 会把动词一起吞掉:
    # "创建一个名为poem的文件夹" 抽出的是 "创建一个名为poem" —— 于是路径变成
    # 桌面\创建一个名为poem\libai.py, 填进提示里就是让模型去建那么个怪文件夹。
    dirs = pc_skills._new_dirs(s)
    if dirs:
        d = os.path.join(desk, dirs[0])
    else:
        fm = re.search(r'桌面(?:上|上的|里的)?\s*([^\s文件夹目录]{1,32})\s*(?:文件夹|目录)', s)
        d = os.path.join(desk, fm.group(1).strip()) if fm else desk
    names = [m.group(0) for m in _FILENAME_RE.finditer(s)]
    names += [f'{m.group(1)}.py' for m in _WANT_PY_RE.finditer(s)]
    return [os.path.join(d, n) for n in dict.fromkeys(names)]


def _wanted_path(user_text):
    """对方要的第一个文件的完整路径; 拼不出来返回 ''。"""
    ps = _wanted_paths(user_text)
    return ps[0] if ps else ''


def _path_hint(user_text):
    """填进 WRITE_NUDGE 的路径清单。

    一个都拼不出来时给一句**不能照抄**的占位话 —— 提示里出现任何真实路径, 4B 都会
    原样抄走(见 _wanted_paths 的注释)。
    """
    ps = _wanted_paths(user_text)
    return '\n'.join(f'  - {p}' for p in ps) if ps else '  - 对方要的那个文件的完整绝对路径'


def _written_ok(trace, path=''):
    """这一轮真写成功过吗。给了 path 就只比那一个文件, 没给就问有没有写过任何文件。"""
    want = os.path.basename(str(path or '')).lower()
    for t in trace or ():
        if (t or {}).get('tool') not in _WRITTEN_TOOLS:
            continue
        r = str(t.get('result') or '')
        if not r or any(b in r for b in _BAD_HINT):
            continue
        if not want:
            return True
        got = os.path.basename(str((t.get('args') or {}).get('path') or '')).lower()
        if got == want:
            return True
    return False


def _fake_write_claim(reply, trace, user_text=''):
    """交付在说"某个文件建好了/写好了", 可那个文件这一轮从没被写成功过。

    和 _fake_read_claim 是一对: 那个管"没读过就说内容", 这个管"没写过就说建好了"。

    实测五类回归, 两种编法都被放过去了:
      py开发: 它真调了 write_file, 但 path 写成了 桌面\\guess.py(照抄了我自己提示里的
              示例路径), 内容是把工具清单原样抄了一遍; 跑完还交付"在 regress_5 文件夹里
              新建了一个 hello_5.py"。_wrote_ok() 只问"写过任何文件吗" —— 写了个不相干
              的 guess.py 也算过关, 所以这里必须比到**文件名**。
      长任务: 一次 write_file 都没调过(它去 open_app 打开文件夹、win_run 丢运行框),
              却交付"已经完成…建了 nums.txt 和 sum_5.py…得到所有数字的和是 15"。
    """
    s = str(reply or '')
    named = [m.group(0) for m in _FILENAME_RE.finditer(s)]
    if named:
        if _WRITE_CLAIM_RE.search(s) or _file_task_pending(user_text, trace):
            return any(not _written_ok(trace, n) for n in named)
        return False
    # 没点名文件, 但对方本来就是冲着文件来的 —— 那就是在含糊地报"完成了"
    return bool(_WRITE_CLAIM_RE.search(s)) and _file_task_pending(user_text, trace)


# 版本号形态(3.13.7 / 2.1.0)。这类数字极其具体, 不在执行记录里就基本是编的。
_CITE_RE = re.compile(r'\d+\.\d+(?:\.\d+)+')
_RUN_TOOLS = ('run_command', 'run_py', 'run_java', 'win_run')
# 交付里被引号括起来的那段话 —— 报"程序跑出来的东西"时, 引号里的原文必须真出现过。
_QUOTED_RE = re.compile(r'[「『“"]([^」』”"]{6,})[」』”"]')


def _fake_result_claim(reply, trace):
    """交付里报了个具体版本号, 可这一轮跑命令的输出里根本没有这个数 —— 编的。

    实测五类回归「运行 python --version，把版本号告诉我」: 4B 选了 win_run(把字丢进
    Win+R 运行框), 回执"已用 Win+R 执行: python --version"听着像成了, 其实一个字符的
    输出都没拿到。它随后交付"Python 版本是 3.9.7 finish", 而本机真版本是 3.13.7。
    """
    cited = _CITE_RE.findall(str(reply or ''))
    if not cited:
        return False
    ran = [t for t in trace or () if (t or {}).get('tool') in _RUN_TOOLS]
    if not ran:
        return False                      # 这一轮压根没碰命令, 不归这条管
    blob = ' '.join(str((t or {}).get('result') or '') for t in ran)
    return any(c not in blob for c in cited)


def _fake_quote_claim(reply, trace, user_text=''):
    """交付里用引号报了一段"跑出来的东西", 可执行记录里压根没有那段 —— 编的。

    为什么单拉一条: 实测 10-07 23:04 真链路复现(对方"建 poem 文件夹 + libai的py,
    这个需要实现输出静夜思"), 六轮跑完它交付的是"...已成功运行，输出结果为：
    '床前明月光，疑是地上霜...'" —— 而那次 run_py 的真实输出是 `Hello, LibAI!`。
    它把自己**想象中的**结果一字不差报给了对方: 文件是真的, 报的内容是假的,
    对方照着去跑只会看到 Hello, LibAI!。_fake_result_claim 只认版本号那种数字形态,
    认不出整段被编出来的输出, 所以这里按"引号里的原文"再兜一道。

    判据放宽是有意的: 引号里的话只要在这一轮任一工具的结果里出现过、或者在对方原话里
    出现过(引用对方的话是正当的), 就算真的。长度不足 6 个字的(「好的」这种)一律不判。
    """
    s = str(reply or '')
    quoted = [m.group(1) for m in _QUOTED_RE.finditer(s)]
    quoted = [re.sub(r'\s+', '', q) for q in quoted if len(q.strip()) >= 6]
    if not quoted:
        return False
    ran = [t for t in trace or () if (t or {}).get('tool') in _RUN_TOOLS]
    if not ran:
        return False                      # 这一轮没跑过东西, 不归这条管
    blob = re.sub(r'\s+', '', ''.join(str((t or {}).get('result') or '')
                                      for t in ran))
    said_by_user = re.sub(r'\s+', '', str(user_text or ''))
    return any(q and q not in blob and q not in said_by_user for q in quoted)


def _hit_triggers(text):
    """消息里出现"明摆着要动手"的词, 就别浪费一轮快速通道"""
    low = str(text or '').lower()
    if any(k.lower() in low for k in config.FAST_TRIGGERS):
        return True
    # 提到一个具体文件名 = 明摆着要对它做事, 不管配的是哪个动词 ------------------
    # 实测 10-07 18:58 「向我展示桌面static_thoughts.txt的内容」: 词表里只有
    # "读一下"、"看一下这个文件"这种整串, "展示"、"内容"、"桌面"一个都没收, 于是
    # 这句走了快速通道 —— 而那条路上没有 read_file, 4B 只能回一句
    # "我先看一下桌面上的static_thoughts.txt文件内容" 就交付了, 文件根本没读。
    # 靠一条条往词表里补动词是补不完的("展示"补完还有"贴出来"、"报一下"、"讲一下"),
    # 但"人不会无缘无故说一个具体文件名"是稳的 —— 说了就是想对它做事。
    return bool(_FILENAME_RE.search(low))


# 快速通道的回复里出现这两样 = 伪造(它手里没工具、也不联网):
#   _URL_RE  任何明文网址
#   _DUMP_RE web_search 的输出抬头("命中 N 条" / 必应搜索地址)
_URL_RE = re.compile(r'https?://|www\.[a-z0-9-]+\.', re.I)
_DUMP_RE = re.compile(r'命中\s*\d+\s*条|search\?q=', re.I)


def fast_reply(session, user_text, note=None):
    """快速通道: 单轮直答, 不带工具、关思维链。

    走通就是几秒出结果(完整循环要几十秒)。模型一旦回了 FAST_NEED_TOOLS,
    说明这活必须动手/联网, 返回 None 让 ask() 转完整循环。

    把 user_text 记进历史这件事由本函数负责(不管成不成),
    这样 ask() 不用关心走没走快速通道、也不会把同一句话记两遍。
    """
    session.user(user_text, note=note)
    session.trim()

    # 契约挂在 system 上, 绝不能作为第二条 user 消息追加 ——
    # 连着两条 user 会把 4B 绕晕(实测: 问"你好呀"它回了上一题的答案)
    msgs = session.wire()
    if msgs and msgs[0].get('role') == 'system':
        msgs[0]['content'] = msgs[0]['content'] + '\n' + config.FAST_CONTRACT
    else:
        msgs.insert(0, {'role': 'system', 'content': config.FAST_CONTRACT})

    t0 = time.time()
    try:
        # 闲聊侧走本地: 单轮直答不需要云端推理深度, 本地 2~3s 出话且不占额度
        msg = _chat_once(msgs, think=False,
                         num_predict=config.FAST_NUM_PREDICT,
                         timeout=config.FAST_TIMEOUT, prefer='local')
    except Exception as e:
        print(f'[agent] 快速通道失败({time.time() - t0:.1f}s), 转完整循环: {e}')
        return None

    content = msg.get('content') or ''
    if _need_tools(content) or config.FAST_NEED_TOOLS in content:
        print(f'[agent] 快速通道: 需要动手({time.time() - t0:.1f}s), 转完整循环')
        return None

    reply = _clean(content)
    if not reply:
        print(f'[agent] 快速通道空产出({time.time() - t0:.1f}s), 转完整循环')
        return None

    # 快速通道既没有工具也没干活, 它嘴里这两类话绝不能当答复发出去:
    #   1) 过程话预告 —— 实测 13:50 "帮我查找我电脑上python的位置" 它回了
    #      "我先看一下一下电脑上的文件夹结构" 就交付, 对方干等一个不会来的结果
    #   2) 复述历史 —— 实测 13:40 "帮我在控制台输出hello" 它把上一轮助手的
    #      "echo hello 已经执行完了" 原样抄了一遍, 看着像执行过其实啥也没干
    # 快速通道手里没有工具, 却吐出 <tool_call> XML —— 说明它想动手。这东西绝不能
    # 当正文发出去。实测 13:54 对方只发了个 "s", 它就把
    # '<tool_call>{"name": "focus_window", ...}</tool_call>' 原样发给了对方。
    if re.search(r'<\s*/?\s*tool_call', reply, re.I):
        print(f'[agent] 快速通道吐出了工具调用残渣({time.time() - t0:.1f}s), '
              f'转完整循环: {reply[:40]!r}')
        return None
    #   3) 伪造搜索结果 —— 快速通道手里没有 web_search、也不联网, 它吐出的
    #      网址必然是编的, 而且它会照抄历史里见过的"工具输出格式"往下编。
    #      实测 15:03 对方只发了个 "5G", 它直接编出一整页发给了对方:
    #        "搜索 5G 命中 8 条 (https://cn.bing.com/search?q=5G)
    #         1. 5G技术 - Wikipedia / https://zh.wikipedia.org/zh/wiki/5G
    #         2. 5G技术 - 知乎      / https://www.zhihu.com/question/349677170"
    #      —— 同一时刻日志里没有任何 web_search 调用; 而真去搜"5G", 必应返回的
    #      是百度百科/央视网/腾讯新闻, 整个页面里根本没有 wikipedia 这个域名。
    #      连 "命中 8 条" 这个抬头都是仿的。对方点开就是打不开的页面。
    if _URL_RE.search(reply) or _DUMP_RE.search(reply):
        print(f'[agent] 快速通道编造了搜索结果/网址({time.time() - t0:.1f}s), '
              f'转完整循环: {reply[:40]!r}')
        return None
    # 判过程话前先剥掉可能带的残渣: 带残渣的句子必然超过 _is_preamble 的 24 字
    # 上限, 会把"我先看一下..."这类过程话直接放行(13:54 就是这么漏的)。
    if _is_preamble(re.split(r'<\s*/?\s*tool_call', reply, flags=re.I)[0], False):
        print(f'[agent] 快速通道只说了过程话({time.time() - t0:.1f}s), '
              f'转完整循环: {reply[:40]!r}')
        return None
    if _repeat_of_history(reply, _recent_replies(session)):
        print(f'[agent] 快速通道在复述历史({time.time() - t0:.1f}s), '
              f'转完整循环: {reply[:40]!r}')
        return None

    print(f'[agent] 快速通道直答({time.time() - t0:.1f}s): {reply!r}')
    session.assistant(reply)
    return reply


def _rescue_open(user_text, trace):
    """"只说不做"的确定性兜底: 它嘴上说要开 X, 那就由程序真去开 X。

    实测(本机 4B): "帮我打开powershell" 连续两轮都只回
    "我先用 open_app 打开 powershell。", 一个工具都不调 —— 提醒也没用。
    这种明确到只有一个动作的请求, 不该让模型的态度决定做不做:
    从对方原话里抓出"打开/启动"后面的程序名, 直接 open_app。
    只做 open_app(开程序/文件/文件夹/网址), 不执行任何命令, 所以是安全的。
    成功返回要发的话, 失败返回 None(交给外层说实情)。
    """
    # 只认"打开/启动"这类开程序的说法: "运行"多半是在说命令(帮我运行ipconfig),
    # 硬当程序名去 open_app 只会开出一个错误框 + 回一句驴唇不对马嘴的话。
    m = re.search(r'(?:打开|启动|开一下|开个)\s*([^\s，。！？、,.!?]{2,24})',
                  str(user_text or ''))
    if not m:
        return None
    target = re.sub(r'^(一下|一个|个|那个|这个)', '', m.group(1))
    # 从句截断: "打开了三次电脑" 这种 打开+了 的叙述句里, 后面的"了三次电脑"不是程序名
    target = re.split(r'然后|再|并|顺便|以及|和|的|了', target)[0].strip()
    if not target:
        return None

    result = pc_tools.dispatch('open_app', {'name': target})
    print(f'[agent] 兜底真开一次 open_app({target!r}): {str(result)[:120]}')
    trace.append({'tool': 'open_app', 'args': {'name': target}, 'result': result})
    if '出现新窗口' in result:
        return f'{target} 已经打开了'
    return f'{target} 我这边没打开，可能不是程序名，你再说一次试试'


# "把话转交给电脑上那个开着的助手"的两种说法:
#   * 带转交动词: "让/叫/请/麻烦/帮我问一下 + codebuddy + 原话"
#   * 拿它开头直接下指令: "codebuddy 帮我写个脚本"
_CB = r'(?:code\s*buddy|编程助手|代码助手)'
_HANDOFF_RE = re.compile(
    r'(?:让|叫|请|麻烦|转给|转交|交给|发给|告诉|转告|通知|帮我|帮|问一下|问问|问)\s*'
    r'(?:一下\s*)?' + _CB + r'\s*[:：,，。]?\s*(.+)', re.I)
# 开头式必须跟一个动作词, 否则"codebuddy 现在开着吗""codebuddy 是什么意思"也会被当成
# 转交指令, 把一句问话粘进人家的输入框里去。
_CB_HEAD_RE = re.compile(
    r'^' + _CB + r'\s*[:：,，]?\s*(?:帮我|给我|请|帮忙)?\s*'
    r'(?:写|做|改|看|查|算|跑|生成|创建|新建|打开|优化|解释|分析|修复|找|总结|翻译|'
    r'整理|实现|回答|问)\s*(.+)', re.I)


def _handoff_body(user_text):
    """"让 codebuddy 干X"里那段该转交出去的原话; 不是转交请求就返回 ''。

    纯判断, 不动手 —— ask() 一进门就要用它决定"这话是不是给自己的"。
    """
    text = str(user_text or '').strip()
    m = _HANDOFF_RE.search(text)
    if m:
        body = (m.group(1) or '').strip()
    else:
        if not _CB_HEAD_RE.search(text):
            return ''
        # 开头式: 把"帮我"这类前缀连同动作词一起保留, 粘过去的是一句完整指令
        body = re.sub(r'^' + _CB + r'\s*[:：,，]?\s*', '', text, flags=re.I).strip()
    return body if len(body) >= 2 else ''      # "让codebuddy"后面没内容, 不算转交


def _rescue_codebuddy(user_text, trace):
    """"让 codebuddy 干 X"的确定性执行: 真把原话粘进 CodeBuddy 窗口并回车让它开工。

    实测(本机 4B, 10-07 18:0x): 让模型自己判断这件事, 它会去 list_windows、
    focus_window("Agents"), 然后**自己把代码写进给微信的回复**, 一句都没送进去;
    另一路是只回一句"好的，我这就去告诉 codebuddy"就交差, trace 里一个工具都没有 ——
    对方那边就是消息石沉大海。这类话的意图窄得很: 一个有名字的目标窗口 + 一段要转交
    的原话, 没有第二种解释, 所以 ask() 在进门时就直接调它, 不经过模型。
    没有明确转交意图时返回 None。
    """
    if any((t or {}).get('tool') == 'paste_to_app' for t in trace or ()):
        return None                       # 已经粘过了, 不重复粘
    body = _handoff_body(user_text)
    if not body:
        return None
    result = pc_tools.dispatch('paste_to_app', {'app': 'codebuddy', 'text': body,
                                               'submit': config.CB_SUBMIT})
    print(f'[agent] 兜底真粘一次 paste_to_app(codebuddy): {str(result)[:120]}')
    trace.append({'tool': 'paste_to_app', 'args': {'app': 'codebuddy', 'text': body},
                  'result': result})
    if '已把' not in result:
        return ('我这边没能把话送进 CodeBuddy（它可能没开着，或者窗口没抢到前台）'
                '，你等一下再发一次试试')
    # 粘进去只是把话放到它面前, "它到底干成什么样"得等它自己跑。回图绝不能在这里等:
    # main.py 是单线程循环, 阻塞几十秒就收不到对方的下一条消息, 所以丢给后台线程。
    _start_shot_watch()
    if config.CB_SUBMIT:
        return ('已经把话送进 CodeBuddy 并回车了，它应该开工了；'
                '跑完我把它的画面截图发回你这边')
    return '已经把话送进你电脑上的 CodeBuddy 了，去那边看结果就行'


def _start_shot_watch():
    """起后台线程: 等 CodeBuddy 跑完 -> 截一张 -> 发回微信。"""
    if not config.CB_SHOT_ON_DONE:
        print('[agent] 配置里关掉了"转交后回图"')
        return
    threading.Thread(target=_shot_watch_worker, name='cb-shot', daemon=True).start()
    print('[agent] 已起后台回图线程(等它跑完再截图发微信)')


def _shot_watch_worker():
    """后台: 等画面稳(≈跑完) -> 截图 -> 发回微信。异常一律吞掉。

    线程里绝不能把异常抛出去 —— 它不在 ask() 的 try 里, 抛了就是无声少一个线程,
    而对方那边永远等不到那张图, 还不知道为什么。
    """
    try:
        if not pc_tools.wait_codebuddy_idle('codebuddy'):
            print('[cb-shot] 等不到 CodeBuddy 窗口, 不回图')
            return
        path = pc_tools.screenshot_app('codebuddy', tag='codebuddy_done')
        if not path:
            print('[cb-shot] 截图是空的, 不回图')
            return
        import sender
        ok = sender.send_image_to(path)
        print(f'[cb-shot] 回图{"成功" if ok else "失败"}: {path}')
    except Exception as e:                                   # noqa: BLE001
        print(f'[cb-shot] 后台回图出错(已吞掉): {e!r}')


# 对方想看 CodeBuddy 现在什么样: 必须点名 codebuddy, 再配一个"看/图"的词。
# 只认"看"不认名字的话, "帮我看看桌面有什么文件"也会被当成截图发微信。
# 分强弱两档, 是因为这两句长得几乎一样:
#   "截图看看 codebuddy 现在怎么样了"  —— 要看画面, 该截图
#   "让 codebuddy 看一下这个报错"      —— 派活给它, 该 paste_to_app
# 只按"有没有'看'字"判是分不开的; 区别在于前者带"怎么样了/截图"这种明确想看结果的词。
_SHOT_STRONG_RE = re.compile(r'(截图|截屏|截个图|截一张|拍张|拍个照|拍一下|拍一张|'
                             r'怎么样了|怎么样|什么情况|什么样子|跑成什么样|'
                             r'在干嘛|干嘛呢|进度|状态|画面)')
_SHOT_WEAK_RE = re.compile(r'(看看|看一下|看一眼)')


def _shot_request(user_text):
    """"看看 codebuddy 怎么样了 / 截个图给我" -> 返回 'codebuddy'; 不是这意思返回 ''。

    纯判断, 不动手。带强信号(截图/怎么样了/进度…)就直接认; 只有弱信号(看看/看一下)
    且整句又是个转交句式("让 codebuddy 看一下 X")时让位给转交 —— 那是在派活, 不是要看图。
    """
    s = str(user_text or '')
    if not re.search(_CB, s, re.I):
        return ''
    strong = bool(_SHOT_STRONG_RE.search(s))
    if not strong:
        if not _SHOT_WEAK_RE.search(s):
            return ''
        if _handoff_body(s):          # "让 codebuddy 看一下这个报错" = 转交
            return ''
    return 'codebuddy'


def _rescue_shot(user_text, trace):
    """确定性完成"截 CodeBuddy 窗口 + 发回微信"; 没这个意图返回 None。"""
    app = _shot_request(user_text)
    if not app:
        return None
    if any((t or {}).get('tool') == 'shot_to_wechat' for t in trace or ()):
        return None                       # 这一轮已经截过了
    result = pc_tools.dispatch('shot_to_wechat', {'app': app})
    print(f'[agent] 兜底真截一次 shot_to_wechat({app}): {str(result)[:120]}')
    trace.append({'tool': 'shot_to_wechat', 'args': {'app': app}, 'result': result})
    if '已截' in result:
        return f'已经把 {app} 现在的画面截下来发到你微信了'
    return result                          # 失败时 result 本身就是实话


_CMD_POOL = None


def _cmd_pool():
    """拼错纠正用的命令名池: cmd 内置命令 + PATH 里的可执行文件名。

    懒加载缓存 —— 只有真要纠错时才扫一次 PATH, 不拖慢正常路径。
    """
    global _CMD_POOL
    if _CMD_POOL is not None:
        return _CMD_POOL
    pool = set(('echo dir cd copy del type mkdir rmdir ren cls set start call pause exit '
                'find findstr sort more tree ver vol date time title color path attrib '
                'move rd md tasklist taskkill ipconfig ping netstat curl where whoami '
                'systeminfo explorer notepad calc mspaint cmd powershell python pip git '
                'node npm code shutdown reg sc net schtasks wmic winget chcp timeout'
                ).split())
    for d in (os.environ.get('PATH') or '').split(os.pathsep):
        try:
            for f in os.listdir(d):
                if f.lower().endswith(('.exe', '.cmd', '.bat', '.com')):
                    pool.add(os.path.splitext(f)[0].lower())
        except Exception:                                   # noqa: BLE001
            continue
    _CMD_POOL = sorted(pool)
    return _CMD_POOL


def _fix_command(cmd, err):
    """命令名拼错时猜一个最接近的, 返回 (新命令, 猜到的名字); 拿不准返回 None。

    只在 Windows 明说 "'xxx' is not recognized" 时才动手 —— 这样不会把本来是对的、
    只是恰好执行失败的命令改坏。
    """
    m = re.search(r"'([^']{1,60})'\s+is not recognized", str(err or ''), re.I)
    if not m:
        return None
    bad = m.group(1).strip()
    if not bad or ' ' in bad:
        return None
    import difflib
    hit = difflib.get_close_matches(bad.lower(), _cmd_pool(), n=1, cutoff=0.7)
    if not hit or hit[0] == bad.lower():
        return None
    return re.sub(re.escape(bad), hit[0], cmd, count=1), hit[0]


def _run_ok(result_text):
    """run_command 的结果里带「退出码 N」, 只有 N=0 才算真跑成。

    必须看退出码: 实测 `帮我运行 echh hello` -> 返回 "退出码 1 + 'echh' is not
    recognized", 文本里既没有"失败"也没有"拒绝", 老代码据此走了成功分支,
    把一条报错的命令说成"已经执行完了" —— 等于替它谎报成功。
    拿不到退出码(超时/被安全策略拒绝/参数不对)一律算没跑成。
    """
    m = re.search(r'退出码\s*(-?\d+)', str(result_text or ''))
    return bool(m) and int(m.group(1)) == 0


def _run_once(cmd):
    """跑一条命令, 返回 run_command 的原始文本结果"""
    return str(pc_tools.dispatch('run_command', {'command': cmd}) or '')


def _cmd_asked(text):
    """对方**明确**要"运行/执行某条命令"时, 抠出那条命令; 不是这类请求返回 ''。

    安全性不靠这里把关: 抠出来的命令照样走 pc_tools.run_command, 它内部的 _guard 会按
    config.DANGEROUS_PATTERNS 直接拒绝危险命令。这里只是别乱认:
    否定/疑问/描述句("别运行"、"正在运行的进程"、"怎么运行")一律不碰。
    """
    text = str(text or '')
    if re.search(r'别|不要|不用|不必|停止|取消|正在|已经|是否|怎么|为什么|哪些|什么|能否',
                 text):
        return ''
    m = re.search(r'(?:运行|执行|run)\s*[:：]?\s*(.{2,200})', text)
    if not m:
        return ''
    cmd = m.group(1).strip().strip('`"\'')
    # 原话往往是"运行 python --version，把版本号告诉我" —— 上面那个 .{2,200} 会把动词
    # 后面**整句**都抓进来, 连中文带标点一起丢给命令行, 必失败。命令到标点为止。
    # 实测五类回归: 不截的话这条兜底跑的是 `python --version，把版本号告诉我`, 命令行回
    # "不是内部或外部命令", 于是它回对方一句"没跑成" —— 可原话里命令写得清清楚楚。
    cmd = re.split(r'[，。！？；：、\n]', cmd)[0].strip().strip('`"\'')
    if not cmd or cmd[0] in '的了吧呢吗。，,':
        return ''
    # 抠出来的必须是**真命令**: 命令行不认中文。原话里"运行"常常只是叙述, 比如
    # "新建一个 hello_5.py，运行后打印 Hello 5"抠出来是 `后打印 Hello 5`,
    # "最后运行 sum_5.py"抠出来是 `sum_5.py`。实测五类回归: 这两条都被真拿去跑了,
    # 都失败, 交付于是变成"没跑成" —— 比让循环自己兜底还差。
    if re.search(r'[\u4e00-\u9fff]', cmd):
        return ''
    # 原话讲的是"建文件/写文件"的活, 不是"运行某条现成命令" —— 别抢, 交给决策循环。
    if re.search(r'新建|创建|建个|建一个|写一?个|生成', str(text or '')):
        return ''
    return cmd


def _cmd_out_pending(user_text, reply):
    """对方问的是某条命令的直接输出, 可交付里连那条命令都没提 -> True。

    实测五类回归 命令行操作: 第1轮 run_command("python --version") 就拿到了 3.13.7,
    它却接着 sleep 两次、去搜"codebuddy 的功能和使用方法"、最后交付一整段 CodeBuddy
    介绍, 版本号一个字没提。_fake_result_claim 拦不住 —— 它只管"报了记录里没有的数",
    而这段回话里**压根没有任何版本号**。可对方问的就是那个数, 他必须拿到。
    判据故意松(只要回话里出现过命令的第一个词就算提过), 因为漏拦的代价是对方拿不到答案。
    """
    cmd = _cmd_asked(user_text)
    if not cmd:
        return False
    head = (cmd.split() or [cmd])[0].lower()
    return head not in str(reply or '').lower()


def _rescue_run(user_text, trace):
    """明确让"运行/执行某条命令"时, 模型只说不做(甚至空输出)的确定性兜底。

    实测(本机 4B): "帮我运行: echo hello" -> 它一个字都没产出(既没调工具也没正文),
    整条消息一条都不回, 对方看到的就是"发过去没反应"。
    这类请求意图单一(就是那条命令), 不该由模型的态度决定做不做。
    """
    cmd = _cmd_asked(user_text)
    if not cmd:
        return None

    out = _run_once(cmd)                       # 先按你说的原样跑一次
    fixed = None
    if not _run_ok(out):                       # 退出码非 0 才算失败
        fix = _fix_command(cmd, out)           # 命令名拼错(echh -> echo)时猜一个
        if fix:
            cmd2, good = fix
            out2 = _run_once(cmd2)
            if _run_ok(out2):
                fixed = (cmd.split()[0] if cmd.split() else cmd, good)
                cmd, out = cmd2, out2
    out = out.strip()
    print(f'[agent] 兜底真跑一次 run_command({cmd!r}): {out[:120]}')
    trace.append({'tool': 'run_command', 'args': {'command': cmd}, 'result': out})
    if fixed:
        return f'你打的 {fixed[0]} 我猜是 {fixed[1]} 按这个跑了 {cmd} 结果 {out[:200]}'
    if not _run_ok(out):
        return f'{cmd} 没跑成 {out[:200]}' if out else f'{cmd} 没跑成'
    return f'{cmd} 跑完了 {out[:200]}'


# ---------------------------------------------------------------
# 删文件的确定性兜底。
#
# 为什么不让模型碰: 实测 10-07 18:50「删除桌面上的guess.py」, 本机 4B **生成 30 个
# token 却一个可见字符都没有** —— 不给正文、不给工具调用, 白等 8.6s 后回了句
# "我这边没弄成 你再说一遍我试试"。事后用探针单轮复现(19:0x): 新提示词 / 旧提示词 /
# 去掉 delete_file 三种组合**全部哑火**(prompt_eval_count 2050, 远没到 num_ctx 4096,
# 所以不是上下文撑爆)。而 18:34、18:42 两轮有历史时它倒是会动手, 但调的是
# win_run(把"删除"当"打开那个文件")+ 正文里手写 {"function": "open_app", ...},
# 六轮全落空 —— 这就是"用运行框删文件"的来历。
#
# 结论: 提示词第 14 条早写了 del, 工具层也给了 delete_file, 两条路模型都不走。
# 意图窄(一个文件)、动作一步的活, 只能由程序直接做 —— 和 _handoff_body(转交
# CodeBuddy)、_shot_request(截图)、_direct_reply(时间日期)是同一个套路。
#
# 安全性: 目标必须是一个**写了扩展名的文件名**, 泛称("删掉那个文件")一律不接,
# 原样交回模型; 真删走 pc_tools.delete_file(进回收站, 还能还原)。
_DEL_WORD = r'(?:删除|删掉|删了|去掉|移除|清理掉|清掉|干掉|抹掉)'
_DEL_BAD = r'别|不要|不用|不必|停止|取消|是否|怎么|为什么|哪些|什么|能否|已经|正在|没有'
_EXT = (r'py|pyw|txt|md|json|csv|log|ini|bat|cmd|ps1|exe|zip|rar|7z|'
        r'jpg|jpeg|png|gif|bmp|webp|mp3|mp4|avi|mov|'
        r'doc|docx|xls|xlsx|ppt|pptx|pdf')
_DEL_FULL_RE = re.compile(r'([A-Za-z]:\\[^\s"\'，。！？、；]+)')
_DEL_FILE_RE = re.compile(r'([A-Za-z0-9_\-\u4e00-\u9fa5]+\.(?:' + _EXT + r'))', re.I)

# 「新建/建一个 xxx.py」= 对方明确要**造一个带扩展名的新文件**。和 pc_skills._CREATE_FILE_RE
# 是同一套意图, 只是这里判的是"该不该早点把它推去 write_file"。
# 注意 {0,24} 里排除了逗号句号 —— 否则"新建一个文件夹，然后复制 x.txt"也会命中。
_CREATE_FILE_RE = re.compile(
    r'(?:新建|创建|建个|建一个|写一个|生成)[^，。！？；]{0,24}?\.(?:' + _EXT + r')', re.I)


def _make_file_intent(user_text):
    """对方要"造一个新文件"吗 —— 比 _CREATE_FILE_RE 宽一档。

    实测 10-07 21:47 对方说的是「libai的py类型」(没有那个点), 于是 _CREATE_FILE_RE
    不命中, 这句"没碰写文件工具就点它去 write_file"的提醒整条不触发; 6 轮里没有一轮
    被推向 write_file, 全在瞎撞 open_app, 最后回"我这边没弄成"。
    所以判据跟 pc_skills 的意图门保持一致: 带扩展名的写法 + "名字紧贴 py"的写法都算。
    """
    s = str(user_text or '')
    return bool(_CREATE_FILE_RE.search(s) or _WANT_PY_RE.search(s))


def _delete_target(user_text):
    """从"删掉桌面上的 guess.py"里抽出要删的完整路径; 不是明确请求就返回 ''。

    只认两种句式, 宁可漏也不误伤:
        "把 X 删掉"     "删掉 X"
    抽不到就返回 '' —— 原样交回模型, 程序绝不猜着删。
    """
    text = str(user_text or '')
    if re.search(_DEL_BAD, text) or not re.search(_DEL_WORD, text):
        return ''
    m = re.search(r'把\s*(.{2,80}?)\s*' + _DEL_WORD, text)
    seg = m.group(1) if m else ''
    if not seg:
        m = re.search(_DEL_WORD + r'\s*(?:一下|掉|了)?\s*(.{2,80})', text)
        seg = m.group(1) if m else ''
    if not seg:
        return ''
    # "把 a.txt 的内容删掉"是要清空里面的东西, 不是删文件本身 —— 不接
    if re.search(r'(?:内容|里面|里的|中的|文字|代码|这几行)\s*$', seg):
        return ''
    full = _DEL_FULL_RE.search(seg)
    if full:
        return full.group(1)
    # 剥掉"桌面上的/我的/那个"这类指代, 只留文件名本身 —— 不剥的话
    # "桌面上的guess.py"会被整串当成文件名, 拼出 ...\Desktop\桌面上的guess.py
    seg = re.sub(r'桌面(?:上|里|中)?(?:的)?|我的|你的|那个|这个|一个', '', seg).strip()
    name = _DEL_FILE_RE.search(seg)
    if not name:
        return ''
    if '桌面' in text:
        return os.path.join(os.path.expanduser('~'), 'Desktop', name.group(1))
    return name.group(1)


def _rescue_delete(target):
    """程序直接删 —— 返回要发给对方的话(就是 delete_file 的原文, 已核实过措辞)。

    走 delete_file 而不是 run_command('del …'): del 是彻底抹掉, 这个进回收站,
    反悔还能捞回来; 而且中文/带空格的路径不用去拼 cmd 的引号。
    """
    result = str(pc_tools.dispatch('delete_file', {'path': target}) or '')
    print(f'[agent] 明确的删除请求, 程序直接做 delete_file({target!r}): {result[:120]}')
    return result or f'{target} 我这边没删成'


# ============================================================
# 操作电脑的专线: 选方法 -> 选参数 -> 程序拼命令
# ============================================================
# 为什么不让模型直接生成命令 —— 实测(10-07 19:00)它给的是 `copy [path] a.txt`,
# `[path]` 是它自己编的占位符。让它"挑"就稳得多。详见 pc_skills 顶部。
def _pc_ask(prompt, num_predict=6):
    """单轮极短选择题 —— 关思维链、预算压到个位数, 1~2 秒出结果。

    为什么不复用 _loop: 这条线上模型只做"分类", 不要工具通道, 也不需要历史。
    它一犯迷糊我们就直接放弃、交回决策循环, 不会卡住也不会瞎编。
    """
    msgs = [
        {'role': 'system',
         'content': '你在替一个 Windows 助手做选择题。只回编号数字，不要写任何别的字。'},
        {'role': 'user', 'content': prompt},
    ]
    try:
        msg = _chat_once(msgs, think=False, num_predict=num_predict, timeout=40)
    except Exception as e:                                   # noqa: BLE001
        print(f'[agent] 专线选择题调用失败({type(e).__name__}): {e}')
        return ''
    return str(msg.get('content') or '').strip()


def _pc_history(session, n=8):
    """最近几轮的纯文本 —— 专线只拿它找"这个文件"的线索。"""
    out = []
    for m in (session.messages or [])[-n:]:
        if m.get('role') in ('user', 'assistant'):
            c = str(m.get('content') or '').strip()
            if c:
                out.append(c)
    return '\n'.join(out)


def _pc_param(sk, pname, ptext, kind, user_text, hist):
    """选一个参数值。有候选就挑编号, 没候选就让它写 —— 写完都要过 verify。

    kind == 'text' 是关键词这类纯词, 给不出候选, 只能让它从原话里摘。
    """
    if kind == 'text':
        v = pc_skills.guess_kw(user_text)      # 原话里明摆着的词就别问模型了
        if not v:
            v = _pc_ask(f'对方说：{user_text}\n\n'
                        f'做「{sk["when"]}」要确定{pname}（{ptext}）。'
                        f'只输出这个词本身，不要标点、不要解释：', num_predict=16)
        return v.strip().strip('"').strip('。')

    cands = pc_skills.candidates(kind, user_text, hist, sid=sk['id'])
    if cands:
        body = '\n'.join(f'{i}. {c}' for i, c in enumerate(cands, 1))
        n_other = len(cands) + 1
        pick = _pc_ask(f'对方说：{user_text}\n\n'
                       f'做「{sk["when"]}」要确定{pname}（{ptext}）。候选：\n{body}\n'
                       f'{n_other}. 都不是上面这些\n\n只回编号：', num_predict=4)
        sidx = re.search(r'\d+', pick)
        if not sidx:
            return ''
        i = int(sidx.group())
        if 1 <= i <= len(cands):
            return cands[i - 1]
        if i != n_other:
            return ''

    # 候选都不合适: 让它自己写完整路径, 写完由 verify 把存在性这关
    v = _pc_ask(f'对方说：{user_text}\n\n'
                f'做「{sk["when"]}」要确定{pname}（{ptext}）。'
                f'请直接给出完整路径（形如 C:\\Users\\<用户名>\\Desktop\\a.txt），'
                f'只输出路径本身：', num_predict=48)
    v = v.strip().strip('"').strip('。')
    return v.splitlines()[0].strip() if v else ''


def _pc_output(res):
    """从 run_command 的返回里抠出真正的 stdout"""
    m = re.search(r'输出:\n(.*?)(?:\n错误:\n|\Z)', str(res or ''), re.S)
    return (m.group(1) if m else '').strip()


def _after_run(res):
    """从 run_py 的返回里抠出真正的 stdout。

    run_py 的返回是「跑完了(退出码 0): <stdout>」, 跟 run_command 的
    「退出码 0\\n输出:\\n<stdout>」不是一个形状, _pc_output 抠不到它。
    """
    m = re.search(r'退出码\s*-?\d+\s*\)\s*:?\s*(.*)', str(res or ''), re.S)
    return (m.group(1) if m else '').strip()


def _pc_clean_read(text):
    """把 read_file 的带行号输出还原成纯文件内容"""
    out = []
    for ln in str(text or '').splitlines():
        m = re.match(r'\s*\d+\| ?(.*)$', ln)
        if m:
            out.append(m.group(1))
    return '\n'.join(out)


def _pc_read(sid, params, user_text):
    """读文件类走 pc_tools.read_file, **不走 type 命令**。

    为什么: 实测 `type "static_thoughts.txt"` 交回来的是
    「搴婂墠鏄庢湀鍏夛紝鐤戞槸鍦颁笂闇溿」—— run_command 是按 gbk 解 stdout 的, 而文件
    是 utf-8。read_file 里的 _read_text 已经把 utf-8/gbk/mbcs 依次试过, 那才是对的。
    管道 findstr 更糟: 中文在两种编码之间比对, 基本匹配不上(实测找"风"退出码 1)。
    """
    if sid == 'list':
        # 列目录也走 Python: `dir /b` 的中文文件名跟着控制台代码页走, 代码页一变就乱,
        # 实测在 65001 的控制台里交回来一串「BLV-涓澶璐㈢粡澶у」。os.listdir 不吃这套。
        d = params['dir']
        try:
            items = sorted(os.listdir(d))
        except OSError:
            return None
        dirs = [n for n in items if os.path.isdir(os.path.join(d, n))]
        files = [n for n in items if not os.path.isdir(os.path.join(d, n))]
        if not dirs and not files:
            return f'{d} 是空的'
        def _brief(names):
            head = '、'.join(names[:20])
            return head + (f' …还有 {len(names) - 20} 个' if len(names) > 20 else '')

        out = [f'{d} 里有 {len(dirs)} 个文件夹、{len(files)} 个文件：']
        if dirs:
            out.append('文件夹：' + _brief(dirs))
        if files:
            out.append('文件：' + _brief(files))
        return '\n'.join(out)

    src = params['src']
    raw = str(pc_tools.dispatch('read_file', {'path': src, 'max_lines': 80}) or '')
    if raw.startswith(('文件不存在', '读取失败', 'read_file 没给路径')):
        return None
    body = _pc_clean_read(raw)
    if sid == 'read':
        return body or None
    if sid == 'count':
        return f'{src} 一共 {len(body.splitlines())} 行'
    kw = str(params.get('kw') or '').strip().strip('"').strip('。')
    if not kw or kw.lower() in ('kw', '关键词', '关键字', '词', 'word'):
        return None                       # 模型把占位符抄回来了, 交回决策循环
    hits = [f'{i}| {ln}' for i, ln in enumerate(body.splitlines(), 1)
            if kw.lower() in ln.lower()]
    if not hits:
        return f'{src} 里没有含"{kw}"的行'
    return '\n'.join(hits[:40])


def _pc_line(session, user_text):
    """操作电脑专线。接得住返回要发给对方的话, 接不住返回 None(交回决策循环)。

    六道闸, 过不去任何一道就放弃 —— 交回原路, 所以能力只增不减:
        ① 意图门(调用方) ② 选方法 ③ 选参数 ④ 校验 ⑤ 拼命令 ⑥ 看退出码
    """
    hist = _pc_history(session)
    # 句式一看就明白的先按规则定, 定不下来才让模型做选择题 ——
    # 实测 4B 分不清"文件夹里有什么"和"找词", 白问一轮还选错。
    sid = pc_skills.guess_skill(user_text)
    if sid:
        print(f'[agent] 专线: 规则定方法 -> {sid}')
    else:
        menu = '\n'.join(pc_skills.menu())
        pick = _pc_ask(f'对方在微信上说：{user_text}\n\n'
                       f'他想让助手在这台 Windows 电脑上做一件事。'
                       f'从下面挑一个最合适的做法，只回编号：\n{menu}', num_predict=4)
        sid = pc_skills.by_number(pick)
    sk = pc_skills.skill_by_id(sid)
    if not sk:
        print(f'[agent] 专线: 选方法没选中({sid!r})')
        return None
    print(f'[agent] 专线: 方法 {sid} ({sk["when"]})')

    params = {}
    for pname, ptext, kind in sk['params']:
        v = _pc_param(sk, pname, ptext, kind, user_text, hist)
        if not v:
            print(f'[agent] 专线: 参数 {pname} 没选出来')
            return None
        ok, why = pc_skills.verify(kind, v)
        if not ok:
            print(f'[agent] 专线: 参数 {pname}={v!r} 不可用({why})')
            return None
        params[pname] = v
    print(f'[agent] 专线: 选参数 -> {params}')

    # 删文件不走 del 命令 —— 那条是彻底抹掉。delete_file 进回收站, 反悔还能捞回来,
    # 返回的也正好是现成的人话(见 pc_tools.delete_file)。
    # 读类不走命令: type 会被 gbk 解码搞成乱码, 管道 findstr 的中文匹配也基本失效
    if sid in ('read', 'find', 'count', 'list'):
        out = _pc_read(sid, params, user_text)
        print(f'[agent] 专线: 读类 {sid} -> {str(out)[:120]!r}')
        return out

    if sid == 'del':
        res = str(pc_tools.dispatch('delete_file', {'path': params['src']}) or '')
        print(f'[agent] 专线: delete_file({params["src"]!r}) -> {res[:100]}')
        return res or None

    # 覆盖闸 --------------------------------------------------------------
    # 闸的位置比闸的松紧重要: 方法选错最多是"这活没干对", 再做一次就行;
    # 而覆盖是不可逆的 —— dst 一旦被盖掉, 原来的内容就没了。所以这一道跟
    # "谁选的方法"无关: 关键词猜的、模型挑的, 一律拦, 要覆盖得让人明说。
    if sid in ('copy', 'move'):
        dst = str(params.get('dst') or '')
        if os.path.isfile(dst) and not re.search(r'覆盖|盖掉|盖住|替换|取代', user_text):
            print(f'[agent] 专线: {dst} 已存在, 不覆盖')
            return (f'{dst} 已经有个同名文件了，我没敢盖掉它。'
                    f'要盖掉就说一声"覆盖"。')

    # pipe 闸 -------------------------------------------------------------
    # pipe 是模型自己写命令的逃生舱, 也是唯一能绕过上面所有 skill 的口子。
    # 详见 pc_skills._PIPE_BAD 的注释。
    if sid == 'pipe':
        ok, why = pc_skills.pipe_ok(params.get('cmd'))
        if not ok:
            print(f'[agent] 专线: pipe 被拦 —— {why}: {params.get("cmd")!r}')
            return None

    cmd = pc_skills.build(sid, params)
    if not cmd:
        print('[agent] 专线: 命令拼不出来')
        return None
    res = _run_once(cmd)
    print(f'[agent] 专线: {cmd!r} -> {res[:160]}')
    if not _run_ok(res):
        return None                      # 跑挂了不硬圆, 交回决策循环
    if sk.get('raw'):
        return _pc_output(res) or None
    done = {'copy': '复制好了', 'move': '挪好了', 'mkdir': '建好了'}.get(sid, '办好了')
    return f'{done}：{params.get("dst") or params.get("src") or ""}'


def _report(trace):
    """用执行记录拼一句确定的话 —— 不花模型时间, 也不会说走样。

    实测(本机 4B): 工具真把画图开起来了, 它交付的却是"已经可以回话了"、
    cmd 那次交付"已经可以用了。" —— 事是做成了, 但这话发出去对方不知道发生了什么。
    """
    said = []
    for t in trace or ():
        name, result = t.get('tool'), str(t.get('result') or '')
        args = t.get('args') or {}
        if name == 'open_app':
            if any(b in result for b in _BAD_HINT) or '出现新窗口' not in result:
                continue
            said.append(f'已经帮你打开{args.get("name")}了' if args.get('name')
                        else '已经帮你打开了')
        elif name == 'paste_to_app' and '已把' in result:
            # 转交也算做成的活, 但说法不能跟"打开了"混 —— 它是把话送进去, 不是开程序
            said.append(f'已经把话送进 {args.get("app") or "那个窗口"} 了')
        elif name in ('write_file', 'edit_file') and _wrote_ok(result):
            # 真写了文件也别干说"做好了": 带上文件名, 对方知道去哪儿看
            p = str(args.get('path') or '')
            said.append(f'已经写好 {os.path.basename(p)} 了' if p else '文件已经写好了')
        elif name == 'shot_to_wechat' and '已截' in result:
            # "已截 X 的窗口并发到微信了" —— 直接告诉对方图已经发出去了, 它去聊天里看
            said.append(f'已经把 {args.get("app") or "CodeBuddy"} 的画面截下来发你微信了')
        elif name in ('run_py', 'run_command') and _run_ok(result):
            # 真跑出来了就把输出原文带上 —— 对方要的往往就是这段输出, 光说"跑通了"
            # 他还是不知道结果是什么。实测 10-07 23:2x: 那次 run_py 真跑出来了(一串
            # 乱码), 交付却在问对方"你要我执行什么操作" —— 对方既不知道写没写成,
            # 也不知道跑出来是什么, 纯答非所问。
            out = (_pc_output(result) or _after_run(result)).splitlines()
            out = ' '.join(x.strip() for x in out if x.strip())[:80]
            said.append(f'跑出来是 {out}' if out else '跑通了(这次没有输出)')
    return '，'.join(said) if said else None


def _guarded(reply, trace):
    """给"二次总结"再上一道证据闸 —— 兜底总结也是模型写的, 一样会说谎。

    实测 15:09 "在桌面新建一个名为Poem的文件夹": 循环里它开了记事本就交付过程话,
    二次总结照着对方原话直接编出"我已成功在桌面创建了名为Poem的文件夹"。
    config.SUMMARY_PROMPT 里虽然写着"没成功就直说、别编", 但提示词管不住 4B,
    所以这里按记录验一遍: 没有任何成功证据时, 带完成腔调的总结一律作废。

    10-14 再补一道**代码闸**: 上面那种"没有证据"只挡谎言, 挡不住"交回来一段代码"。
    公平测试 D 组第 1 次跑, 假成功检测把谎报拦下后走这里, 二次总结交回来的整段就是
    猜数字源码, 而当时 `_worked(trace)` 为真(它确实调过工具), 于是原样放行 ——
    代码被当微信消息发了出去。代码不是给对方的话, 一律作废改走确定性拼句。
    """
    if _looks_like_code(reply):
        print(f'[agent] 二次总结交回来的是代码, 拒绝放行: {str(reply)[:60]!r}')
        return _report(trace) or config.PREAMBLE_GIVEUP
    if _worked(trace):
        return reply
    if _is_preamble(reply, False):
        print(f'[agent] 二次总结也没有证据, 拒绝放行: {str(reply)[:60]!r}')
        return _report(trace) or config.PREAMBLE_GIVEUP
    return reply


def _carries_result(text, trace):
    """交付里是否已经带上了某次成功运行的**结果**。

    实测五类回归 长任务: 对方说"最后运行 sum_5.py，把算出来的得数告诉我", 它交付的正文
    就是 `15` —— 答案完全正确, 却因为 _vague 眼里"既没提那个程序也没有动作词"被换成
    _report 那句"已经帮你打开资源管理器了，已经写好 nums.txt 了，已经写好 sum_5.py 了":
    动作清清楚楚, 唯独对方真正要的那个数没了。
    "答得短"和"答不清"是两回事, 这里就是拿来分开它们的。
    """
    s = str(text or '')
    if not s.strip():
        return False
    for t in trace or ():
        if (t or {}).get('tool') not in ('run_command', 'run_py'):
            continue
        r = str(t.get('result') or '')
        if not _run_ok(r):
            continue
        # 两位以上的数字才算"特定" —— 免得 '0'/''1' 这种到处都有的把任何回话都判成答对了
        if any(len(n) >= 2 and n in s for n in re.findall(r'\d+', r)):
            return True
    return False


# 交付在反问对方"接下来还要做什么" —— 这是本机 4B 的通用收尾腔, 跟这条消息做了什么
# 毫无关系。
# 反复漏判过两次, 所以别写太窄: 先是"请问接下来需要做什么呢"漏了(23:12), 补上后
# "请告诉我你需要执行什么操作 例如 是否需要我帮你处理这些文字"(23:2x)又漏了 ——
# 这类话的骨架就三种: 问对方要指令、问对方要不要帮、说没看懂对方要什么。
_CHATTY_RE = re.compile(r'请问|接下来(?:需要|要|该)|还(?:需要|要)我|需要我(?:帮|做|执行|处理)'
                        r'|是否(?:需要|要我)我?帮|你?(?:如果|要是)(?:有|需要)'
                        r'|有什么(?:需要|要|可以|能)帮|请随时(?:告诉|吩咐|联系)'
                        r'|(?:希望|想让我)做(?:什么|啥)|请(?:明确|告诉我|提供|说明|给出|指示)'
                        r'|(?:没|没有)明确(?:的)?(?:指令|要求|指示|问题)'
                        r'|你要我(?:做|干|执行)(?:什么|啥)')


def _vague(text, trace):
    """事情真做了, 但交付的话里没提做成了什么 -> 说不清, 该换。"""
    if _carries_result(text, trace):
        return False
    s = str(text or '')
    # 第一路: 交付就是一句客套收尾, 而这一轮真写过文件 —— 连文件名都没提。
    # 实测 10-07 23:12 真模型复现(对方"建 poem 文件夹 + libai的py, 这个需要实现输出
    # 静夜思"): 文件写了、程序也跑通了(输出 Hello, LibAI!), 交付的却是"看起来我已经
    # 完成了运行，退出码为0…请问接下来需要做什么呢" —— 对方要的那段输出一个字没有,
    # 连文件建在哪儿都不知道, 在他眼里就是答非所问。
    if _CHATTY_RE.search(s):
        made = [os.path.basename(str((t.get('args') or {}).get('path') or ''))
                for t in (trace or ()) if t.get('tool') in _WRITE_TOOLS]
        made = [m for m in made if m]
        if made and not any(m in s for m in made):
            return True
    # 第二路(原有): 只提了开程序那句话, 却没说清是哪个程序。
    names = [((t.get('args') or {}).get('name') or '')
             for t in (trace or ()) if t.get('tool') == 'open_app']
    names = [n for n in names if n]
    if not names:
        return False
    if any(n.lower() in s.lower() for n in names):
        return False
    return not any(w in s for w in ('打开', '启动', '运行'))


# 问时间/日期这种"答案就在本机"的句子 —— 程序直答, 一秒都不用花在模型上。
# 实测(本机 4B): 问"现在几点了", 快速通道其实答对了("系统显示现在是下午3点整"), 但
# 开头是"我先看一下"被判成过程话扔掉; 转完整循环后它先联网搜"现在几点了", 再想开浏览器
# 看时间, open_app 拿"浏览器/edge"当程序名失败、win_run 拿"edge"也失败, 35 秒后把问题
# 原样复述回来当答复。这类问法没有模型发挥的余地。
_TIME_ASK_RE = re.compile(
    r'^\s*(?:现在|当前|此刻|今天|帮我看看|告诉我)?\s*是?\s*'
    r'(?:几点|几点钟|几时|什么时间|时间是多少|现在时间)\s*[了呀啊吧呢]?\s*[?？。.!！]*$')
_DATE_ASK_RE = re.compile(
    r'^\s*(?:现在|当前|今天|帮我看看|告诉我)?\s*是?\s*'
    r'(?:几号|几日|几月几号|什么日子|星期几|周几|礼拜几|日期是多少)\s*'
    r'[了呀啊吧呢]?\s*[?？。.!！]*$')

# 组合问法: "今天几号，星期几？" / "现在几点、几号"。
# 上面两条正则都要求单一句式, 逗号一拼就整句不匹配, 于是一个本该 0.1s 答掉的
# 问题被送去联网搜(实测 55.8s, 走了 3 次 web_search/http_get)。
# 这里只做"拆分后逐段认", 不猜语义: 每段必须正好是已知问法, 否则返回 None。
_COMBINE_SPLIT = re.compile(r'[，,；;、和及\s]+')
_DATE_TOKENS = ('几号', '几日', '几月几号', '什么日子', '星期几', '周几',
                '礼拜几', '日期是多少')
_TIME_TOKENS = ('几点', '几点钟', '几时', '什么时间', '时间是多少', '现在时间')
_ASK_PREFIXES = ('现在', '当前', '此刻', '今天', '帮我看看', '告诉我', '请问')


def _combined_ask(s, now):
    parts = [p for p in _COMBINE_SPLIT.split(s) if p]
    if not parts or len(parts) > 3:
        return None
    want_date = want_time = False
    for p in parts:
        q = p
        for pre in _ASK_PREFIXES:
            if q.startswith(pre):
                q = q[len(pre):]
        q = q.strip('是 ')
        q = q.rstrip('了呀啊吧呢?？。.!！ ')
        if q in _DATE_TOKENS:
            want_date = True
        elif q in _TIME_TOKENS:
            want_time = True
        else:
            return None
    out = ''
    if want_date:
        out = '今天是 {}年{}月{}日 星期{}'.format(
            now.tm_year, now.tm_mon, now.tm_mday, '一二三四五六日'[now.tm_wday])
    if want_time:
        t = '现在是 {}点{:02d}分'.format(now.tm_hour, now.tm_min)
        out = f'{out}，{t}' if out else t
    return out or None


def _direct_reply(text):
    """时间/日期直答 -> 返回正文; 不是这类问法就返回 None"""
    s = str(text or '').strip()
    if not s:
        return None
    now = time.localtime()
    if _TIME_ASK_RE.match(s):
        return '现在是 {}点{:02d}分'.format(now.tm_hour, now.tm_min)
    if _DATE_ASK_RE.match(s):
        return '今天是 {}年{}月{}日 星期{}'.format(
            now.tm_year, now.tm_mon, now.tm_mday, '一二三四五六日'[now.tm_wday])
    return _combined_ask(s, now)


def ask(session, user_text, max_steps=None, on_note=None):
    """处理一条对方消息, 返回要发出去的正文; 失败返回 None

    先过快速通道(不用动手的消息单轮直答, 省几十秒), 再走完整决策循环:
    每一轮都由模型自己判断「继续思考 / 调用工具 / 交付」, 程序只解析它的输出、
    执行工具、并在它管不住自己时兜底(步数上限、卡住强制交付、工具记录二次总结)。
    on_note(进度话术) 是可选的临时回复回调, 走事件通道, 不进模型历史。
    """
    max_steps = max_steps or config.AGENT_MAX_STEPS
    # 错字预检: 命中就把"程序观察到的事实"挂进给模型看的输入。
    # 注意 _hit_triggers / _rescue_* 仍然只认对方原话, 免得提示文本里的词误触发流程。
    direct = _direct_reply(user_text)
    if direct:
        print(f'[agent] 时间/日期直答: {direct}')
        session.user(user_text)
        session.assistant(direct)
        return direct

    # 明确说"让 codebuddy 干X"的: 这句话不是给自己的活, 直接转交, 根本不问模型。
    # 实测(10-07 18:0x, 真模型跑这条): 交给它判断的结果是 —— 列窗口、激活 Agents、
    # 然后自己把猜数字的源码写进了给微信的回复里, 一句都没送进 CodeBuddy。
    # 意图明确 + 动作只有一步的活不该赌模型(和上面时间日期直答是同一个道理)。
    if _handoff_body(user_text):
        reply = _rescue_codebuddy(user_text, []) or config.PREAMBLE_GIVEUP
        print(f'[agent] 明确的转交请求, 程序直接做: {reply}')
        session.user(user_text)
        session.assistant(reply)
        return reply

    # "截个图看看 codebuddy 怎么样了"同理: 意图窄(一个窗口 + 一张图), 动作只有一步,
    # 丢给模型判断的话它会去 list_windows / screenshot, 然后照样不知道图怎么发出去。
    if _shot_request(user_text):
        reply = _rescue_shot(user_text, []) or config.PREAMBLE_GIVEUP
        print(f'[agent] 明确的截图请求, 程序直接做: {reply}')
        session.user(user_text)
        session.assistant(reply)
        return reply

    # "删掉桌面上的 guess.py"同理, 而且更该绕开模型: 实测(10-07 18:50)它对这句话
    # 直接哑火 —— 生成 30 个 token 却一个可见字符都没有, 白等 8.6s 后回一句
    # "我这边没弄成 你再说一遍我试试"。详见 _delete_target 上面的说明。
    del_target = _delete_target(user_text)
    if del_target:
        reply = _rescue_delete(del_target)
        session.user(user_text)
        session.assistant(reply)
        return reply

    # 操作电脑的活走"选择题专线": 先选方法, 再选参数, 命令由程序拼。
    # 接不住就返回 None, 原样落回下面的快速通道 / 决策循环 —— 纯增量, 能力不减。
    if pc_skills.looks_like_pc(user_text):
        reply = _pc_line(session, user_text)
        if reply:
            print(f'[agent] 专线交付: {reply[:120]}')
            session.user(user_text)
            session.assistant(reply)
            return reply
        print('[agent] 专线没接住, 交回决策循环')

    hint = _typo_hint(user_text)
    # text 只喂 _compose 的总结脚手架(那里要一次完整上下文);
    # 进历史的那份必须是对方原话 —— 预检改走 note 临时挂载, 见 Session.wire()。
    text = f'{user_text}\n\n{hint}' if hint else user_text
    if hint:
        print(f'[agent] 错字预检命中, 已附给模型(不进历史): {hint[:100]}')

    if config.FAST_REPLY:
        if _hit_triggers(user_text):
            session.user(user_text, note=hint)   # 一看就要动手, 不必白试一轮
            session.trim()
        else:
            reply = fast_reply(session, user_text, note=hint)   # 内部已记好原话
            if reply:
                return reply
    else:
        session.user(user_text, note=hint)
        session.trim()

    print(f'[agent] 进入决策循环(最多 {max_steps} 轮)')
    reply, trace, steps = _loop(session, max_steps, on_note, user_text)
    print(f'[agent] 循环结束: {steps} 轮 / {len(trace)} 次工具调用')

    if reply:
        # 交付的还是一句过程话, 绝不能就这么发出去让对方干等"我先看一下":
        #   干过活  -> 拿真实执行记录重写一句(实测:"计算器已经打开了。")
        #   啥也没干 -> 它嘴上说的那个程序, 由程序自己开一次; 开不了才说实情
        if _is_preamble(reply, _worked(trace)):
            if trace:
                print('[agent] 交付的只是过程话, 改用执行记录二次总结')
                # 先用确定性的话: _report 直接从记录拼, 没有模型发挥的余地, 编不出
                # "已完成"; 拼不出来才问模型, 而且问完还得过一遍证据闸。
                better = _report(trace) or _guarded(_compose(session, text, trace), trace)
                if not better or better == config.PREAMBLE_GIVEUP:
                    # 二次总结也拿不出证据: 明确的"转交给 codebuddy"请求由程序补做
                    better = _rescue_codebuddy(user_text, trace) or better
                reply = better or config.PREAMBLE_GIVEUP
            else:
                print('[agent] 提醒过还是只说不做, 由程序兜底真开一次')
                reply = (_rescue_codebuddy(user_text, trace)
                         or _rescue_open(user_text, trace)
                         or _rescue_run(user_text, trace)
                         or config.PREAMBLE_GIVEUP)
        elif trace and _vague(reply, trace):
            print(f'[agent] 交付的话没说清做了什么({reply!r}), 按执行记录换一句')
            # 执行记录里也拼不出一句能看的(那些动作全失败了) —— 那就照实说没弄成,
            # 别把原来那句说不清的话原样发出去。实测 10-07 22:58 这里原来是 `or reply`,
            # 于是 `print("静")` 又被捡回来发了一遍。
            reply = _report(trace) or config.PREAMBLE_GIVEUP
        session.assistant(reply)
        return reply

    # 兜底: 循环没交出正文 —— 绝不能静默。
    # 实测(本机 4B): "帮我运行: echo hello" 它一个字都没产出, 于是整条消息**一条都不回**;
    # 对方那边看到的就是"发过去没反应", 跟死掉没有区别(而且它其实还活着)。
    if not trace:
        print('[agent] 既没调用工具也没产出正文, 先看是不是明确的命令请求')
        reply = (_rescue_codebuddy(user_text, trace)
                 or _rescue_run(user_text, trace) or config.PREAMBLE_GIVEUP)
    else:
        print('[agent] 兜底: 拿工具执行记录二次总结')
        # _rescue_run 放在 _report 之前: 循环把一条"编出来的命令结果"拒掉之后会走到这里
        # (五类回归「运行 python --version，把版本号告诉我」就是这么被拒的 —— 它选了
        # win_run, 那条路拿不到输出, 于是编了个 3.9.7)。原话里那条命令是现成的, 由程序
        # 自己跑一次最靠得住; 跑不出来(或压根不是命令请求)再退回照记录说实情。
        reply = (_rescue_run(user_text, trace)
                 or _report(trace)
                 or _guarded(_compose(session, text, trace), trace)
                 or config.PREAMBLE_GIVEUP)
    session.assistant(reply)
    return reply
