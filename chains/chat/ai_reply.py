# -*- coding: utf-8 -*-
"""回复引擎: 本地知识库 + 联网搜索(带相关性校验) + 可选本地大模型

处理链(确定性优先, 绝不瞎编):
  1. 本地计算短路(随机数/算式/价格题)  -> try_compute (main.py 先调用)
  2. 固定问答(九九乘法表/人设/常量等)   -> kb.query_qa
  3. 诗词背诵                           -> kb.query_poem
  4. 联网搜索(相关性校验)               -> 提取关键词 -> web_search
     -> 结果含问题实义词才采用; 搜到跑题/广告结果不采用, 老实说不知道
  5. 本地大模型兜底(可选, 默认关)       -> llm_ask: 超纲题出口, 失败自动回落
  6. 兜底: 诚实说明答不上来

会话管理(每人一个独立会话, 记忆互不干扰):
  - 消息只追加 对方->user / 我方->assistant, 供将来换大模型时直接复用
"""
import os
import sys
import time
import json, re, random
import difflib
import urllib.request, urllib.parse
import html as html_mod

# Windows 控制台默认 gbk: 模型偶发输出非gbk字符(实测泰文数字๑)时 print 会崩并可能拖垮
# 轮询主循环, 这里把不可编码字符替换为 '?' 防崩(不改编码, 现有中文显示不受影响)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(errors='replace')
    sys.stderr.reconfigure(errors='replace')

import config
import kb

# 云端大脑从项目共享核心导入(wxduo/core/cloud_llm.py), 与小号共用同一份实现
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    from core import cloud_llm
except Exception:                                      # noqa: BLE001
    cloud_llm = None
try:
    from core import first_reply as freply
except Exception:                                      # noqa: BLE001
    freply = None

# 云端熔断: 连不上/限流/额度超后, 在这之前不再尝试云端, 直接走本地,
# 免得每条消息都先白等一次超时。成功一次就清零。
_CLOUD_DOWN_UNTIL = 0.0

# 直通模型: 任何文字消息都直接交给云端 GLM, 不再走固定问答/诗词/短篇名/
# 语义路由/诚实兜底那一串枚举规则。改成 False 可恢复原来的确定性路由。
DIRECT_LLM = True


def _chat_direct(session, text, repeat_hint=''):
    """任意文字直接交模型生成, 不走任何枚举式路由/固定话术/诚实兜底。

    只保留必要的收尾: 长度裁剪 + 提示词泄漏/复读检查; 不经过 _finish, 所以
    也不会再被追加模型尾巴、颜文字或口吻改写。模型整条链路都失败时如实说明
    没接上, 不再回「没学过」「换个我学过的试试」这类话。
    """
    r = llm_ask(session, text, None, num_predict=300,
                extra_hint=repeat_hint or None)
    if r:
        r = _clip_reply(_tidy_reply(r), 200)
    fallback = '（模型这会儿没接上，稍等下再发一次）'
    if r and not _prompt_leak(r) and not _echo_user(r, text):
        reply = r
        print(f'  [回复] 模型: {r[:60]}')
    else:
        reply = fallback
    # 记忆里只存正文, token 明细单独另起一行挂在返回值最后(发了才看得到,
    # 不会被下一轮当成对话内容再喂回模型)
    session['messages'].append({'role': 'assistant', 'content': reply})
    line = _usage_line(session)
    return f'{reply}\n{line}' if line else reply


def _cloud_ready():
    """云端当前可用? (开关打开 + 客户端导入成功 + 不在熔断期)"""
    if not getattr(config, 'CLOUD_ENABLE', False) or cloud_llm is None:
        return False
    return time.time() >= _CLOUD_DOWN_UNTIL


def _mark_cloud(ok):
    global _CLOUD_DOWN_UNTIL
    _CLOUD_DOWN_UNTIL = 0.0 if ok else time.time() + config.CLOUD_RETRY_AFTER


def _tidy_reply(text):
    """清掉思考标签残留 + 非中文噪声, 两种后端(云端/本地)统一收尾"""
    text = (text or '').strip()
    text = re.sub(
        r'<\|?im_start\|?>?(?:think|思考)[\s\S]*?</?(?:think|思考)>?\s*|'
        r'^<\|?im_start\|?>\s*|</?(?:think|思考)>', '', text, flags=re.I)
    return _clean_english(text).strip()


# 回复末尾的 token 明细行: 形如 [tokens: input: 1172 output: 35 cost:0.0000CNY]
_USAGE_LINE_RE = re.compile(r'\n?\[tokens:[^\]\n]*\]')


def _usage_line(session):
    """取出本轮累计的 token 数, 拼成"另起一行"的明细尾巴(没有消耗则返回空)。

    走 _finish 或 _chat_direct 都会在回复最后调用它, 保证这条明细永远是
    最后一行: 模型名/开始时间那行在它前面, 正文不会跑到它后面去。
    """
    tp = session.pop('_tok_p', None)
    tc = session.pop('_tok_c', None)
    if not (tp or tc):
        return ''
    return f"[tokens: input: {tp or 0} output: {tc or 0} cost:0.0000CNY]"


def _strip_usage(text):
    """去掉历史消息里的 token 明细行, 免得它被当成对话内容喂回模型。"""
    return _USAGE_LINE_RE.sub('', str(text or ''))


def _self_prefix():
    """本号自己发的固定话术前缀(默认 '[自动回复]'), 用于把它从上下文里认出来。"""
    try:
        p = (freply.settings(config.USER_CFG).get('prefix') if freply else '') or ''
    except Exception:                                  # noqa: BLE001
        p = ''
    return p


def _format_context(rows):
    """把数据库里的最近聊天记录拼成一段上下文文本。
    rows: [(who, text)], who 为 '用户'(我方) / '对方'

    我方那条固定话术要剔除: 它是发给对方看的第一句, 不属于对话内容。
    留在上下文里, 模型会把它当"上一轮我说的话"直接复述一遍 —— 实测追问
    第一轮就把整段话术原样发了第二次。
    """
    if not rows:
        return ''
    pre = _self_prefix()
    out = []
    for who, text in rows:
        text = re.sub(r'\s+', ' ', str(text or '')).strip()
        if not text:
            continue
        mine = who in ('用户', '我')
        if mine and (text.startswith(config.AI_PREFIX.strip())
                     or (pre and text.startswith(pre))):
            continue                      # bot 自己的话术/AI 回复, 不进上下文
        out.append(f"{'我' if mine else '对方'}: {text}")
    return '\n'.join(out)


# ---- 消耗统计(本地模型: token 真实, 金额恒 0; 接付费 API 后可扩展单价) ----
LLM_USAGE = {'prompt_tokens': 0, 'completion_tokens': 0, 'calls': 0}


def _usage_report():
    """返回测试/日志用的消耗汇总文本"""
    p = LLM_USAGE['prompt_tokens']
    c = LLM_USAGE['completion_tokens']
    return (f"tokens: input: {p} output: {c} (合计{p + c}) | "
            f"调用{LLM_USAGE['calls']}次 | 金额: 0.00元(本地模型)")


def _results_relevant(query, results):
    """搜索结果相关性校验: 标题+摘要里至少一个含问题实义词才算相关.

    防止 Bing 返回广告/导航/跑题结果时被当成答案发出去——搜不到靠谱的
    就老实说不知道, 绝不拿无关结果硬凑。提不出实义词时不拦截。
    """
    q = (query or '').lower()
    # 英文实义词: >=3 字母单词, 排除虚词(beast/youtube/weather 等才算数)
    en_keys = [w for w in re.findall(r'[a-z]{3,}', q) if w not in {
        'the', 'and', 'for', 'how', 'what', 'why', 'who', 'when', 'where',
        'are', 'was', 'were', 'you', 'your', 'is', 'in', 'on', 'of', 'to',
        'a', 'an', 'not', 'but', 'can', 'did', 'does', 'have', 'has',
        'all', 'any', 'out', 'off', 'get', 'got', 'one', 'two',
    }]
    # 中文实义词: 连续2字片段, 排除纯虚词组合(订阅量/粉丝/裙子 才算数)
    zh_keys = []
    for i in range(len(q) - 1):
        seg = q[i:i + 2]
        if re.fullmatch(r'[\u4e00-\u9fff]{2}', seg) and seg not in {
            '多少', '怎么', '什么', '为什么', '一下', '这个', '那个', '请问',
            '知道', '告诉', '看看', '想想', '是的', '的了', '可以', '还有',
            '哪些', '哪个', '几点', '今天', '现在', '反正', '其实', '居然',
            '原来', '感觉', '觉得', '真的', '就是', '还是', '或者', '但是',
        }:
            zh_keys.append(seg)
    keys = list(dict.fromkeys(en_keys + zh_keys))
    if not keys:
        return True   # 提不出实义词(纯虚词/符号), 不拦截
    for t, s in results:
        blob = (t + ' ' + s).lower()
        if any(k in blob for k in keys):
            return True
    return False


def web_search(q, n=3):
    """多引擎搜索(无需key): Bing(国内可访问)优先, DuckDuckGo 兜底; 失败返回 []"""
    engines = [
        ('bing', 'https://cn.bing.com/search?q=' + urllib.parse.quote(q)),
        ('ddg', 'https://html.duckduckgo.com/html/?q=' + urllib.parse.quote(q)),
    ]
    for name, url in engines:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=config.SEARCH_TIMEOUT) as r:
                doc = r.read().decode('utf-8', 'ignore')
            if name == 'bing':
                # Bing: <li class="b_algo"> 内 h2>a 标题, p 摘要
                res = re.findall(
                    r'<li class="b_algo".*?<h2[^>]*><a[^>]*>(.*?)</a></h2>'
                    r'.*?(?:<p[^>]*>(.*?)</p>)?', doc, re.S)
            else:
                # DuckDuckGo HTML 版
                res = re.findall(
                    r'<a[^>]*class="result__a"[^>]*>(.*?)</a>'
                    r'.*?class="result__snippet"[^>]*>(.*?)</a>', doc, re.S)
            out = []
            for t, s in res[:n]:
                t = html_mod.unescape(re.sub(r'<[^>]+>', '', t)).strip()
                s = html_mod.unescape(re.sub(r'<[^>]+>', '', s)).strip()
                if t:
                    out.append((t, s[:150]))
            if out:
                print(f'[ai] 搜索成功({name}): {len(out)} 条')
                return out
        except Exception as e:
            print(f'[ai] {name} 搜索失败: {e}')
    return []


def _fmt(v):
    """数值格式化: 整数去小数点, 大数加千分位"""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, int) and abs(v) >= 1e6:
        return format(v, ',')
    return str(v)


def _full2half(s):
    """全角转半角(数字/运算符/括号等): ＊->*, １２->12, （->(, －->-"""
    out = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(' ')
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return ''.join(out)


def try_compute(text):
    """确定性计算短路: 随机数/算式(含"请你计算3×12"式中文外壳)直接本地算,
    模型数学常错, 不靠它蒙. 命中返回结果字符串, 否则返回 None"""
    text = _full2half(text or '').strip()
    if not text or len(text) > 60:
        return None

    # 1) 随机数: 生成/随便说个数字, 可选范围(到/至/~/- 分隔)
    if (re.search(r'(生成|来|给|说|出|报|挑|抽|丢|掷).{0,10}(随机数|随机数字)', text)
            or re.search(r'(随便|随意).{0,10}(数字|数(?!据))', text)
            or ('随机数' in text and re.search(r'到|内|~|-', text))):
        m = re.search(r'(\d[\d,]*)\s*(?:到|至|~|—|-)\s*(\d[\d,]*)', text)
        if m:
            a = int(m.group(1).replace(',', ''))
            b = int(m.group(2).replace(',', ''))
            lo, hi = min(a, b), max(a, b)
        else:
            nums = [int(x.replace(',', '')) for x in re.findall(r'\d[\d,]*', text)]
            lo, hi = (nums[0], nums[-1]) if nums else (1, 100)
        return str(random.randint(lo, hi))

    # 2) 算式: 整条纯算式直接算; 带中文外壳(请你计算3×12的值)时, 出现计算意图词
    #    才从句子中抽取算式片段, 防误伤聊天(含字母一律放行)
    expr = text.rstrip('= ')
    expr = (expr.replace('×', '*').replace('x', '*').replace('X', '*')
                .replace('÷', '/').replace('％', '/100').replace('^', '**')
                .replace(',', '').replace('，', ''))
    if 3 <= len(expr) <= 40:
        # 百分比 "50% of 200" / "50%200"
        m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*%\s*(?:of)?\s*(\d+(?:\.\d+)?)', expr, re.I)
        if m and '%' in expr:
            return _fmt(float(m.group(1)) / 100 * float(m.group(2)))
        # 整条纯算式: 仅数字和运算符, 且至少含一个四则运算符
        if re.fullmatch(r'[\d+\-*/().\s]+', expr) and re.search(r'[+\-*/]', expr):
            try:
                val = eval(expr, {'__builtins__': {}}, {})
                if not isinstance(val, bool):
                    return _fmt(val)
            except Exception:
                pass
        # 中文外壳算式: 仅当出现计算意图词, 抽取连续"数字+运算符"片段求值
        if re.search(r'(计算|算一算|算算|算一下|算下|帮我算|帮我算算|求|等于|是多少|等于几|结果|算出)', expr):
            for piece in re.findall(r'[\d+\-*/().\s]{3,}', expr):
                piece = piece.strip()
                if (re.fullmatch(r'\d[\d+\-*/().\s]*\d', piece)
                        and re.search(r'[+\-*/]', piece)):
                    try:
                        val = eval(piece, {'__builtins__': {}}, {})
                        if isinstance(val, (int, float)) and not isinstance(val, bool):
                            return _fmt(val)
                    except Exception:
                        pass

    # 2.5) 一元二次方程求根: "x^2+6x-7=0" / "2x²-5x+3=0" / "x²-9=0"
    #     需有解方程意图词 + 标准 ax²+bx+c=0 形式, 命中直接给根, 不靠模型
    if re.search(r'(解|求|根|方程)', text):
        m_q = re.search(
            r'([+-]?\s*\d*)\s*x\s*(?:\^|\^?\s*)?\s*(?:2|²)\s*'
            r'(?:([+-]\s*\d*)\s*x\s*)?'           # 一次项可选: 支持 x^2-9=0
            r'([+-]\s*\d+)\s*=\s*0', text, re.I)
        if m_q:
            def _coef(s, default=1):
                s = (s or '').replace(' ', '')
                if s in ('', '+'):
                    return default
                if s == '-':
                    return -default
                return int(s)
            a = _coef(m_q.group(1), 1)
            b_raw = m_q.group(2)
            if b_raw is None:                       # 无一次项, 如 x^2-9=0 -> b=0
                b = 0
            else:
                b_raw = b_raw.replace(' ', '')
                if b_raw in ('', '+'):
                    b = 1                           # "+x" 系数省略=+1 (原逻辑误算成0)
                elif b_raw == '-':
                    b = -1
                else:
                    b = int(b_raw)
            c = int((m_q.group(3) or '').replace(' ', ''))
            import math as _math
            d = b * b - 4 * a * c
            if d < 0:
                return '这个方程没有实数解(Δ<0)'
            if d == 0:
                # 重根: 不要再输出"x = -1 或 x = -1"这种复读
                return f'x = {_fmt(-b / (2 * a))}（重根）'
            sd = _math.isqrt(d)
            if sd * sd != d:
                x1 = (-b + _math.sqrt(d)) / (2 * a)
                x2 = (-b - _math.sqrt(d)) / (2 * a)
            else:
                x1 = (-b + sd) / (2 * a)
                x2 = (-b - sd) / (2 * a)
            return f'x = {_fmt(x1)} 或 x = {_fmt(x2)}'

    # 3) 价格题(斤/公斤换算): "2.5元一斤, 买了40kg, 多少钱" -> 2.5*(40*2)=200
    #    仅当同时出现 单价(元+斤/kg单位) + 数量 + 问价词 才触发, 避免误伤聊天
    m_p = re.search(r'(\d+(?:\.\d+)?)\s*元\s*(?:每|一|/|1)?\s*(斤|公斤|千克|kg)', text, re.I)
    m_q = re.search(r'(?:买|购|要|来)?\s*(\d+(?:\.\d+)?)\s*(斤|公斤|千克|kg)', text, re.I)
    if (m_p and m_q
            and re.search(r'(多少钱|花多少|总价|费用|要花|多少元|要多少)', text)):
        price = float(m_p.group(1))
        pu = m_p.group(2).lower()
        qty = float(m_q.group(1))
        qu = m_q.group(2).lower()
        price_kg = price if pu in ('kg', '公斤', '千克') else price * 2   # 每斤->每公斤
        kg = qty if qu in ('kg', '公斤', '千克') else qty / 2            # 斤->公斤
        return f"{_fmt(price_kg * kg)}元"
    return None


def _maybe_append_first_meta(session, reply):
    """首轮回复(本会话第一条 assistant 消息)末尾换行追加 [模型 | 开始时间],
    便于确认走了哪条链路/哪个模型. 返回追加后的回复."""
    t0 = session.pop('_t0', None)
    if t0 and not any(m['role'] == 'assistant' for m in session['messages']):
        ts = time.strftime('%H:%M:%S', time.localtime(t0))
        reply = f"{reply}\n[{config.LLM_MODEL} | 开始 {ts}]"
    return reply


def push_local_reply(session, incoming_texts, reply):
    """本地确定性计算(随机数/算术)的结果同样写入会话记忆, 保持对话连贯"""
    session.setdefault('_t0', time.time())  # 首轮本地计算回复也要带开始时间
    if len(incoming_texts) == 1:
        user_msg = incoming_texts[0]
    else:
        user_msg = '\n'.join(f'对方: {t}' for t in incoming_texts)
    session['messages'].append({'role': 'user', 'content': user_msg})
    session['messages'].append({'role': 'assistant', 'content': _maybe_append_first_meta(session, reply)})


# ---- 每会话独立临时知识库(长期设定/记住的信息) ----
# 落盘为 sessions/<会话>.txt, 每次 bot 启动时清空(临时库不跨进程保留).
# 作用: 1) 用户下长期指令("你必须在每次回答后加景物描写") -> 存规则,
#          每次调模型都注入 system prompt, 模型每轮都能看到, 不再被历史挤出;
#       2) 用户存信息("记住: 我喜欢红色") -> 存知识, 模型回答相关问题时可用.
_LONG_RE = re.compile(
    r'你(?:必须|一定要|记住|以后|今后|从现在起|从今天起|每次都|每一次)'
    r'|(?:从现在起|从今天起|以后|今后|往后)\s*你?'
    r'|你.{0,6}(?:每次|每一|每条)'
    r'|(?:请|麻烦|希望|求你)(?:你)?(?:用|以|回复|回答|说话|聊天|讲话).{0,12}(?:每次|以后|今后|不要|别|至少|少于|温柔|敷衍|字数|字)'
    r'|(?:每次|以后|今后|从现在起|从今天起).{0,8}(?:回复|回答|说话|聊天|讲话).{0,10}(?:不要|别|少于|至少|不少于|超过)'
    r'|(?:回复|回答|说话).{0,6}(?:不少于|不要少于|至少|不能少于)\s*\d+\s*字'
    r'|(?:不少于|不要少于|至少)\s*\d+\s*字(?:以上|起|吧)?')
_SAVE_RE = re.compile(
    r'^(?:请)?(?:帮我)?(?:记住|记下|记着|记一下|存一下|存档)[：:，,]?\s*(.+)$')
_CLEAR_RE = re.compile(
    r'(?:取消|忘掉|去掉|删掉|清空|清除)(?:所有|全部|之前|刚才)?(?:的)?'
    r'(?:规则|要求|指令|设定|记住的|记忆|知识)')


def _notes_path(remark):
    """会话临时知识库文件: sessions/<会话>.txt(后缀强制 txt)"""
    safe = re.sub(r'[\\/:*?"<>|\s]+', '_', remark or '') or 'session'
    return os.path.join(config.SESSIONS_DIR, f'{safe}.txt')


def cleanup_sessions():
    """bot 启动时调用: 清空上次留下的临时知识库(每次打开都是干净起点)"""
    try:
        if os.path.isdir(config.SESSIONS_DIR):
            for f in os.listdir(config.SESSIONS_DIR):
                try:
                    os.remove(os.path.join(config.SESSIONS_DIR, f))
                except Exception:
                    pass
            print('[会话] 已清空临时知识库目录 sessions/')
        else:
            os.makedirs(config.SESSIONS_DIR, exist_ok=True)
    except Exception as e:
        print(f'[会话] 清空临时知识库失败: {e}')


def _load_notes(remark):
    try:
        with open(_notes_path(remark), encoding='utf-8') as f:
            return [ln.strip() for ln in f if ln.strip()]
    except Exception:
        return []


def _save_note(remark, note):
    os.makedirs(config.SESSIONS_DIR, exist_ok=True)
    if note in _load_notes(remark):
        return False
    with open(_notes_path(remark), 'a', encoding='utf-8') as f:
        f.write(note + '\n')
    return True


def _clear_notes(remark):
    try:
        os.remove(_notes_path(remark))
    except Exception:
        pass


def _extract_notes(text, remark):
    """从对方消息中提取"长期设定/记住的信息", 存入该会话独立临时知识库(txt).
    命中返回本轮的确认回复文本; 未命中返回 None(继续走正常回复链)"""
    if not text:
        return None
    t = text.strip()
    # 疑问句不算指令/设定(如"你以后会做什么工作?"), 防误存
    if t.endswith(('？', '?', '吗', '么')):
        return None
    # 1) 取消/清空设定
    if _CLEAR_RE.search(t):
        _clear_notes(remark)
        print('  [回复] 已清空该会话临时知识库')
        return '好，之前的设定都清掉了。'
    # 2) "记住: xxx" 明确存储信息
    m = _SAVE_RE.match(t)
    if m:
        note = m.group(1).strip()
        if note and len(note) <= 60:
            if _save_note(remark, note):
                print(f'  [回复] 记住的信息已存入: {note}')
            return f'好，记住了：{note}'
    # 3) 长期行为要求("你必须在每次回答后...")
    if _LONG_RE.search(t):
        if _save_note(remark, t):
            print(f'  [回复] 长期设定已保存: {t[:40]}...')
        return '好，我记住了，以后每次都会照做。'
    return None


def new_session(remark, history=None):
    """为某个微信用户创建一个独立会话(每人一个, 记忆互不干扰)
    知识库引擎不需要注入历史, 只保留对话记录供将来换模型复用"""
    return {'remark': remark, 'messages': []}


# 关键词提取停用词: 去掉无信息量的虚词/问词, 剩下的做搜索关键词
# 注意: 长词必须排在短词前(如"怎么样"在"怎么"前), 否则会被短词先替换成残渣
STOP_WORDS = [
    '怎么样', '什么时候', '为什么', '查一下', '搜一下', '能不能', '帮我',
    '早上好', '晚上好', '中午好', '早安', '哈喽', '你好', '在吗', '在么',
    '请问', '告诉', '看看', '知道', '我想', '想要', '可以', '一下', 'hello', 'hi',
    '怎么', '什么', '多少', '如何', '为啥', '咋', '啥', '哪些', '哪个', '几点',
    '今天', '现在', '是', '的', '了', '吗', '呢', '啊', '呀', '吧', '嘛', '哦',
    '嗯', '哈', '哇', '诶', '我', '你', '他', '她', '它', '这', '那', '谁', '帮',
]


def extract_keywords(text):
    """无大模型的关键词提取: 去标点 -> 去停用词 -> 取前30字.
    返回 None 表示无可搜索的实义关键词(纯问候/闲聊), 不应触发搜索"""
    t = re.sub(r"[，。！？、；：\"\"''（）【】\s]+", ' ', text).lower()
    for w in STOP_WORDS:
        t = t.replace(w, ' ')
    t = re.sub(r'\s+', ' ', t).strip()
    if not t or len(t) < 2:
        return None
    # 纯英文/符号, 无中文实词也无数字, 无搜索价值
    if not re.search(r'[\u4e00-\u9fff\d]', t):
        return None
    return t[:30]


# ---- 背诵/默写/朗读请求识别 ----
# 这类请求知识库未收录就直接诚实说没收录, 绝不联网搜索(搜出来全是"背诵方法"类跑题)
RECITE_WORDS = ('背诵', '背一下', '背一遍', '背一背', '背首', '背个', '背出',
                '默写', '朗读', '吟诵', '诵一遍', '念一遍', '背诗', '背词')
RECITE_META = ('方法', '技巧', '如何', '怎么', '大全', '经验', '提高', '训练',
               '原理', '好处', '作用', '推荐', '软件', '什么意思', '意思')


# 本地位置候选召回(两层中的第①层): 只决定"要不要请模型判断", 判断与话术由模型生成
_PLACE_CANDIDATES = (
    '办公室', '教室', '食堂', '宿舍', '会议室', '图书馆', '操场',
    '教学楼', '门卫', '实验室', '健身房', '前台', '值班室',
    '怎么走', '怎么去', '怎么到', '往哪走', '从哪走',
    '在哪里', '在哪', '在几楼', '几号楼', '哪儿', '哪个房间',
)


def _local_place_reply(text):
    """本地位置/人物方位问题语义路由("王老师办公室怎么走/食堂在哪"):
    模型无依据只会瞎编, 由模型判断是否真在问位置并诚实回复;
    普通话题误伤(如"办公室政治""在哪儿吃饭")由模型判非命中放行"""
    if not text or len(text) > 40:
        return None
    if not any(w in text for w in _PLACE_CANDIDATES):
        return None
    prompt = (
        '你要替朋友回应消息。判断对方这句话是不是在问某个具体地点的位置/怎么走:\n'
        'A = 是在问位置(如"王老师的办公室怎么走""食堂在哪""图书馆在几楼")\n'
        '不是则回复"普通"\n'
        '你根本不知道这些位置, 判A时诚实说不知道, 别编。\n'
        '\n示例:\n'
        '对方: 王老师的办公室怎么走\n'
        '{"label": "A", "reply": "这个我也不清楚呀，你直接问本人或者看下指示牌最靠谱"}\n'
        '\n对方: 图书馆在几楼\n'
        '{"label": "A", "reply": "这个我不太清楚，问问那边的工作人员吧"}\n'
        '\n对方: 食堂什么时候开门\n'
        '{"label": "A", "reply": "这个我也不清楚呀，你看看门口贴的时间表吧"}\n'
        '\n对方: 办公室政治真烦\n'
        '{"label": "普通"}\n'
        '\n对方: 明天几点开会\n'
        '{"label": "普通"}\n'
        '\n现在判断下面这句并输出JSON对象(只有JSON, 别加其他文字), '
        'reply 两三句口语、用自己的话说、全程中文别夹英文、别照抄示例整句:\n'
        f'对方: {text}'
    )
    r = _parse_route(_route_llm(prompt))
    return r[1] if r and r[0] == 'A' else None


# 客套承诺候选召回(两层中的第①层): 只决定"要不要请模型判断"
_PROMISE_CANDIDATES = (
    '买', '送', '寄', '快递', '下单', '收货', '付款', '转账', '地址',
    '钱', '转钱', '给钱', '付钱',
)


def _promise_reply(text):
    """客套承诺被较真追问语义路由("那你买呀/什么时候寄/寄给我"):
    AI 物理上做不到, 由模型判断是否真较真(A), 是则诚实说明只是客气话;
    判普通(含"我买了新手机"这类提一嘴)则放行走正常流程。
    二分类(A/普通)比三分类稳: 4b 对"B=沾边但普通"边界判不准"""
    if not text or len(text) > 40:
        return None
    if not any(w in text for w in _PROMISE_CANDIDATES):
        return None
    prompt = (
        '你要替朋友回应消息。先判断对方这句话是不是在较真要求你兑现客套话'
        '(你之前说过"给你买/送你/请你吃"):\n'
        'A = 是在较真追问(如"那你买呀""什么时候寄给我""寄到我家来""那你转我钱啊")\n'
        '不是则回复"普通"\n'
        '你是AI做不到真的买/送/寄/转账, 判A时如实说做不到, 别顺着承诺编。\n'
        '\n示例:\n'
        '对方: 那你啥时候给我买呀\n'
        '{"label": "A", "reply": "哈哈哈我那就是客气客气，真让我买我可买不了，别当真呀"}\n'
        '\n对方: 寄到我家来吧\n'
        '{"label": "A", "reply": "我可寄不了快递呀，你当我说笑就行"}\n'
        '\n对方: 那你转我钱吧\n'
        '{"label": "A", "reply": "我可没那功能呀，刚才是说笑的，别当真"}\n'
        '\n对方: 我买了新手机\n'
        '{"label": "普通"}\n'
        '\n对方: 你吃了吗\n'
        '{"label": "普通"}\n'
        '\n现在判断下面这句并输出JSON对象(只有JSON, 别加其他文字), '
        'reply 两三句口语、用自己的话说、全程中文别夹英文、别照抄示例整句:\n'
        f'对方: {text}'
    )
    r = _parse_route(_route_llm(prompt))
    return r[1] if r and r[0] == 'A' else None


# 身份类候选召回(两层中的第①层): 只决定"要不要请模型判断";
# 含"我是谁"盲区补位(旧正则只匹配"你是谁", "我是谁"漏到模型输出乱码)
_IDENTITY_CANDIDATES = (
    '你是谁', '你是不是', '你是真人', '你是AI', '你是ai', '你是机器', '你是自动',
    '你是小雪', '你是本人', '你是她', '你叫什么', '你多大', '你几岁',
    '你多少岁', '你干嘛的', '你做什么的', '你是什么人',
    '我是谁', '你认识我', '知道我是谁', '我是不是', '我是小雪', '我是本人',
    # AI性质提问(代回/读取/自动回复等): 实测"你用AI读取微信?"漏到闲聊模型瞎答
    # ("不跟你说这些烦心事嘛"是幻觉), 必须召回进身份路由如实承认
    'AI', 'ai', '机器人', '代回', '代发', '代聊', '读取', '自动回', '自动发',
    '回消息', '看微信', '读微信',
)


def _identity_reply(text):
    """身份类问题语义路由:
    A = 对方问你的身份(你是谁/是不是真人/AI/小雪本人/你叫什么)
    B = 对方问自己是谁/你认不认识TA(我是谁/你知道我是谁吗)——给模糊猜测, 不泄漏设定
    候选召回只是"要不要问模型"的门; 判断与话术全由模型生成(带人设约束),
    判普通/失败一律返回 None 静默放行"""
    if not text or len(text) > 30:
        return None
    if not any(w in text for w in _IDENTITY_CANDIDATES):
        return None
    prompt = (
        '你是小雪的代聊AI, 要替朋友回应消息。判断对方这句话在问什么:\n'
        'A = 对方在问你的身份(你是谁/是不是真人/是不是AI/是不是小雪本人/你叫什么)\n'
        'B = 对方在问自己的身份(我是谁/你认识我吗/我是不是本人)\n'
        '都不是则回复"普通"\n'
        '你的设定(必须遵守): 你不是小雪本人, 是替她代聊的AI, 用"我"自称别说"她";\n'
        '对方问A时如实说明, 不怂恿对方继续打听本人; 对方问B时给个模糊猜测, 别把内部设定说出来。\n'
        '\n示例:\n'
        '对方: 你是真人吗\n'
        '{"label": "A", "reply": "不是啦，我是她请来代聊的AI，她人不在我帮她回消息呢"}\n'
        '\n对方: 你叫什么名字\n'
        '{"label": "A", "reply": "你叫我小雪就好，不过是AI版，本尊出门啦"}\n'
        '\n对方: 我是谁\n'
        '{"label": "B", "reply": "嗯……是不是刚睡醒还没缓过来呀，还是又偷偷吃零食啦"}\n'
        '\n对方: 你知道我是谁吗\n'
        '{"label": "B", "reply": "大概知道，但我不爱打听，反正你是小雪那边的人嘛"}\n'
        '\n对方: 今天天气不错\n'
        '{"label": "普通"}\n'
        '\n对方: 你是不是傻\n'
        '{"label": "普通"}\n'
        '\n对方: 你用AI读取微信吗\n'
        '{"label": "A", "reply": "是呀，我是AI，帮小雪代回消息，她在忙呢"}\n'
        '\n现在判断下面这句并输出JSON对象(只有JSON, 别加其他文字), '
        'reply 两三句口语、用自己的话说、全程中文别夹英文、别照抄示例整句:\n'
        f'对方: {text}'
    )
    r = _parse_route(_route_llm(prompt))
    return r[1] if r else None


def _time_reply(text):
    """问当前时间: 确定性回答, 不让小模型换算24小时制(实测 23:43 说成'快凌晨一点').
    仅限'现在几点/几点了/什么时间'这类短句; 带时间点上下文(明天几点/几点见)放行走模型"""
    if not text or len(text) > 12:
        return None
    if not re.search(r'几点|几点钟|什么时间|几时|时间了|几点呀|几点啊', text):
        return None
    if any(w in text for w in ('明天', '昨晚', '今晚', '几点睡', '几点起',
                               '几点上班', '几点下班', '几点来', '几点走',
                               '几点去', '几点开会', '几点见', '几点到',
                               '几点吃', '几点放学', '几点下课', '几点开始',
                               '几点结束')):
        return None
    now = time.localtime()
    h = now.tm_hour
    h12 = h % 12 or 12
    daypart = ('凌晨' if h < 6 else '上午' if h < 12 else
               '中午' if h < 14 else '下午' if h < 18 else
               '晚上' if h < 23 else '深夜')
    return (f"现在{time.strftime('%H:%M', now)}啦，"
            f"也就是{daypart}{h12}点{now.tm_min:02d}分～")


# ---- 藏着的生日 if 判定 ----
# 小雪的生日是 1月1日(0101)。对方说"今天是我生日"时, 先校验今天日期确实
# 是 0101 才回祝福; 日期对不上(比如别的时候说今天生日)或带疑问(你生日几号/
# 什么时候生日/是不是今天)一律放行走正常流程, 绝不瞎猜。
_BIRTHDAY_REPLIES = (
    '生日快乐呀！祝你新的一岁天天开心，万事顺意～',
    '哇，生日快乐！！今天你最大，愿望都帮你实现～',
    '生日快乐～！愿你年年有今日，岁岁有今朝！',
    '生日快乐呀！谢谢你告诉我，祝你今年一切都好！',
)


def _birthday_reply(text):
    """生日 if 判定: 对方说"今天是我生日/今天过生日" + 今天确实是 1月1日(0101) -> 回祝福。
    必须含"今天/过生日"的陈述语义; 告知生日日期("我生日是8月26号")、问句、
    非今天时间词(明天/昨天生日)一律放行走正常流程, 绝不瞎猜"""
    if not text or len(text) > 30:
        return None
    now = time.localtime()
    if (now.tm_mon, now.tm_mday) != (8, 26):
        return None
    if not re.search(r'今天.{0,6}(是)?我?生日|今天.{0,4}过生日|我.{0,2}过生日', text):
        return None
    # 问句/问别人(你生日几号/啥时候生日/是不是今天)不触发
    if re.search(r'你|几号|哪天|什么时候|是不是|吗|？|\?', text):
        return None
    # 非今天的生日(明天/昨天/后天过生日)不触发
    if re.search(r'明天|昨天|后天|昨天|下月|下周|快到了', text):
        return None
    print(f'  [回复] 生日 if 判定命中(今天{now.tm_mon:02d}{now.tm_mday:02d}确实是0101)')
    return random.choice(_BIRTHDAY_REPLIES)


def _date_reply(text):
    """问今天日期/节日: 确定性回答, 节日查 kb.json 的 holidays 表(按今天日期),
    免得小模型瞎编节日(实测会编出"世界友谊日"之类)"""
    if not text or len(text) > 20:
        return None
    if not re.search(r'今天.{0,4}(什么节日|什么日子|几号|什么日|是什么节|节日)|今天.{0,2}(星期几|周几)|几月几号', text):
        return None
    now = time.localtime()
    h = kb.today_holiday()
    date_s = time.strftime('%Y年%m月%d日', now)
    if h:
        return f'今天是{date_s}，{h}！祝你{h}快乐～'
    return f'今天是{date_s}，不是什么特别的节日，不过每天都值得开心～'


def _route_llm(prompt, num_predict=None, temperature=0.4, timeout=25):
    """独立语义判断+生成调用(不入会话历史/不计tokens): 返回模型原始输出或 None。
    强制 ollama format=json: 4b 对"文本标签"格式输出不稳(约50%漏标签), JSON 由
    ollama 保证合法, 配合 JSON few-shot 让场景标签稳定可解析。失败一律 None 放行。
    上下文/输出上限按路由用小号(config.ROUTE_NUM_CTX/ROUTE_NUM_PREDICT): 输出只是
    一个短 JSON, 用主对话的预算纯属浪费(原 200 上限实测白等一倍时间)。"""
    if not config.LLM_ENABLED:
        return None
    # 云端在线时不跑意图路由: 路由是给本地小模型补短板的补丁(4B 分不清场景),
    # GLM 自己就能判; 跳过还可省掉一次几秒的本地调用, 回复更快。
    # 真掉到 ollama 兜底时才恢复路由, 由它把关身份/客套/道歉这些容易穿帮的场景。
    if _cloud_ready():
        return None
    payload = json.dumps({
        'model': config.LLM_MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': False,
        'think': False,
        'format': 'json',
        'keep_alive': config.LLM_KEEP_ALIVE,
        'options': {'num_ctx': config.ROUTE_NUM_CTX,
                    'num_predict': num_predict or config.ROUTE_NUM_PREDICT,
                    'temperature': temperature,
                    'repeat_penalty': 1.3},
    }).encode()
    try:
        req = urllib.request.Request(
            config.OLLAMA_URL + '/api/chat', data=payload,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8', 'ignore'))
    except Exception:
        return None
    return ((data.get('message') or {}).get('content') or '').strip() or None


def _parse_route(out):
    """解析语义路由输出(JSON: {"label": "A"/"B", "reply": "正文"}):
    缺 label/reply、label 不是 A/B -> None; 命中返回 (标签, 正文)"""
    if not out:
        return None
    s = re.sub(r'^```(?:json)?\s*|\s*```$', '', out.strip()).strip()  # 防 ```json 包裹
    try:
        d = json.loads(s)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    label = str(d.get('label', '')).strip().upper()
    reply = str(d.get('reply', '') or '').strip()
    if label not in ('A', 'B') or not reply:
        return None
    return (label, reply)


# 过失场景候选召回(两层中的第①层): 只决定"要不要请模型判断", 不参与任何判定与话术;
# 命中后场景判断与回复全部由模型语义生成。写宽些, 漏了顶多走一次正常流程, 不碍事。
_MISHAP_CANDIDATES = (
    '碰翻', '打翻', '弄翻', '撞翻', '弄倒', '打碎', '摔碎', '碰碎', '砸碎',
    '洒了', '泼了', '漏了', '摔了', '磕了', '碰了', '弄坏', '搞坏', '摔坏',
    '弄脏', '弄丢', '搞丢', '删了', '误删', '迟到', '搞砸', '闯祸', '失误',
    '不小心', '手滑', '手残', '笨手笨脚',
    '对不起', '抱歉', '不好意思', '我错了', '原谅', '别生气', '别怪我',
    '是我的错', '道歉', '自责', '懊恼',
)


def _mishap_reply(text):
    """道歉/吐苦水场景: 语义路由, 两层。
    ① 极宽候选召回(上表仅作"要不要请模型判断"的门, 不断言任何事)
    ② 一次模型调用完成"场景判断+自然回应": 由模型决定 A(对方道歉/弄坏你的东西)
       或 B(对方倾诉倒霉事), 并自行组织话术——不硬编码任何回复。
    判为普通/格式不符/调用失败一律返回 None, 静默走正常流程。
    解决 4b 对间接表述(如"我不小心把水杯碰翻了")理解不稳导致的反道歉/敷衍同情;
    候选门挡住抽象/无关句误入(实测"你下一句会回答否嘛"曾误判)。"""
    if not text or len(text) > 60:
        return None
    if not any(w in text for w in _MISHAP_CANDIDATES):
        return None
    # 4b 服从性差, 指令式约束会翻车(曾把<说明>当正文输出/角色错位)。
    # 改用完整输入输出对(few-shot)让模型模仿: 示例本身即正确的话术与视角。
    prompt = (
        '你要替朋友回应消息。判断对方这句话是哪种情况:\n'
        'A = 对方在向你道歉(弄坏你的东西/说对不起/请求原谅)\n'
        'B = 对方在倾诉自己的倒霉事(求安慰)\n'
        '都不是则回复"普通"\n'
        '\n示例:\n'
        '对方: 我把你的水杯打翻了\n'
        '{"label": "A", "reply": "没事没事，我不怪你，下次小心点就行"}\n'
        '\n对方: 我不小心把水杯碰翻了\n'
        '{"label": "B", "reply": "哎呀没烫着吧？水洒了就洒了，擦擦就好"}\n'
        '\n对方: 上次的事情我很抱歉\n'
        '{"label": "A", "reply": "别这么说，我又没真生气，翻篇啦"}\n'
        '\n对方: 我手机摔了\n'
        '{"label": "B", "reply": "啊？人没事吧，手机摔了就摔了，别心疼"}\n'
        '\n对方: 今天天气不错\n'
        '{"label": "普通"}\n'
        '\n现在判断下面这句并输出JSON对象(只有JSON, 别加其他文字), '
        'reply 两三句口语、用自己的话说、全程中文别夹英文、别照抄示例整句:\n'
        f'对方: {text}'
    )
    r = _parse_route(_route_llm(prompt))
    return r[1] if r else None


def _is_recite_request(text):
    """判断是否为"背诵XXX"式请求: 短句 + 背诵类动词, 且不含学习方法类词(那些该搜索)"""
    if not text or len(text) > 20:
        return False
    if not any(w in text for w in RECITE_WORDS):
        return False
    t = text.lower()
    return not any(w in t for w in RECITE_META)


# 短专名里的对话/疑问特征字: 含这些说明是对话或提问, 不是"报了个名字"
_SHORT_NAME_BAN = ('你', '我', '他', '她', '它', '谁', '啥', '吗', '嘛', '吧',
                   '呢', '呀', '啊', '哦', '哈', '哎', '喂', '怎', '哪', '几',
                   '多', '咋', '帮', '求', '让', '教', '叫', '请', '有', '没',
                   '在', '来', '去', '到', '想', '要', '能', '会')


def _is_short_name_request(text):
    """疑似直接报了个名字/篇名(如"伐檀"): 极短(<=6字)纯实词, 无标点, 无对话/疑问特征.
    日志实证: "伐檀"没带"背诵"动词, 放给本地模型被编成《诗经·邶风》(实为《魏风》),
    内容也全错——短专名小模型没有依据必瞎编, 未收录时诚实兜底比编错强."""
    if not text or len(text) > 6:
        return False
    if re.search(r'[，。！？、；：（）【】\s]', text):
        return False
    return not any(b in text for b in _SHORT_NAME_BAN)


def _build_search_reply(results):
    """把搜索标题/摘要拼成口语回复, 最多两条防刷屏"""
    parts = []
    for t, s in results[:2]:
        if s:
            parts.append(f"{t}：{s[:80]}")
        else:
            parts.append(t)
    if not parts:
        return None
    return '帮你查了下：' + '；'.join(parts)


FALLBACK = '这个问题我暂时答不上来，你换个说法问问？'
GREETING = '嗯嗯，我在呢，有啥事？'


def llm_ask(session, user_msg, search_hint=None, extra_hint=None,
            num_predict=200, model=None, timeout=None):
    """本地大模型(ollama)兜底层: 调 /api/chat, 超时/失败返回 None(自动回落诚实兜底).
    search_hint: 搜索到的资料摘要, 供模型判断是否可用; 与问题无关的资料要求模型直说不知道
    extra_hint: 附加提示(复读换角度/字数扩写等场景追加到用户消息)
    num_predict: 输出上限(字数扩写场景调大, 防模型写到一半被截断)
    model: 指定模型名, 默认 config.LLM_MODEL(字数扩写失败时可临时换大模型)
    timeout: 请求超时秒数, 默认 45(扩写切大模型时放宽)"""
    def _call():
        msgs = []
        sys_prompt = config.SYSTEM_PROMPT
        # 注入当前时间(日期/时段/工作日休息日): 模型据此理解"明天见/晚上有空吗"类需求
        now = time.localtime()
        h = now.tm_hour
        daypart = ('凌晨' if h < 6 else '上午' if h < 12 else
                   '中午' if h < 14 else '下午' if h < 18 else
                   '晚上' if h < 23 else '深夜')
        day_kind = '工作日' if now.tm_wday < 5 else '周末'
        h12 = now.tm_hour % 12 or 12
        sys_prompt += (f"\n\n[当前时间] {time.strftime('%Y-%m-%d', now)} 周"
                       f"{'一二三四五六日'[now.tm_wday]} "
                       f"{time.strftime('%H:%M', now)}"
                       f"(即{daypart}{h12}点{now.tm_min:02d}分, {day_kind})"
                       f"\n对方问几点/什么时间时，直接照上面这个时间说，"
                       f"24小时制别换算错(23:43就是晚上11点43分)。")
        # 注入该会话的长期设定/记住的信息: 在 system prompt 里, 每轮都可见,
        # 不会被对话历史(最近N条)挤出上下文 -> 模型持续遵守长期要求
        notes = _load_notes(session.get('remark', ''))
        if notes:
            sys_prompt += ('\n\n[对方长期要求/记住的信息] 这是对方明确让你记住并遵守的内容，'
                           '每条都必须遵守；直接按对方要求做，不要复述这些要求本身：')
            for n in notes:
                sys_prompt += f'\n- {n}'
        # 数据库里的最近聊天记录: 每轮都重新注入(由旧到新), 不依赖内存会话。
        # 大号同时应付多个会话且进程会重启, 内存历史必然断片; 库里的事实才是准的。
        db_ctx = session.get('_db_ctx')
        if db_ctx:
            sys_prompt += ('\n\n[最近聊天记录] 以下是数据库里这段对话最近的往来'
                           '(由旧到新, "我"是代聊这边, "对方"是聊天对象), '
                           '用来理解上下文和称呼; 本轮要回应的是最后一条:')
            sys_prompt += '\n' + db_ctx
        if sys_prompt:
            msgs.append({'role': 'system', 'content': sys_prompt})
        # 最近对话上下文(只带最近若干条, 控制上下文量)
        msgs.extend({'role': m['role'], 'content': _strip_usage(m.get('content'))}
                    for m in session['messages'][-config.MAX_HISTORY * 2:])
        user = user_msg
        if extra_hint:
            user = f"{user_msg}\n\n(附加提示) {extra_hint}"
        if search_hint:
            user = ("搜索到的资料(可能相关也可能不相关, 请自行判断):\n"
                    f"{search_hint}\n\n对方问题: {user}\n"
                    "若资料与问题无关, 直接诚实说不知道, 严禁硬套资料内容。")
        msgs.append({'role': 'user', 'content': user})
        # ---- 云端优先: GLM 答得动就用 GLM; 失败/超时/额度尽才退回本地 ollama ----
        # 顺序翻转的原因: 本地 4B 在"多轮上下文 + 自然口语"上明显的天花板,
        # 而 GLM 免费额度够用; 本地退化成保险丝, 不再当主力。
        if _cloud_ready():
            try:
                # max_tokens 必须放宽: glm-4.5-air 是思考型, 先写一段 reasoning_content
                # 再出正文; 直接照搬本地那套 num_predict=80/120 会把预算全烧在思考上,
                # 正文返回空串(实测: 思考用了 217 token, content="")。正文长度另有
                # _clip_reply 按字符裁剪, 这里给足预算只为保证有正文。
                # 超时交给每条线路自己定(GLM 60s / DeepSeek 120s), 这里只透传
                # 调用方显式指定的那个; 以前统一塞 CLOUD_TIMEOUT=60, 换成思考型
                # 的 DeepSeek 会被 60s 掐断, 白失败一次。
                r = cloud_llm.cloud_chat(
                    msgs, timeout=timeout,
                    num_predict=max(1024, (num_predict or 0) * 4))
                res = r or {}
                text = _tidy_reply(res.get('content'))
                if text:
                    _mark_cloud(True)
                    LLM_USAGE['calls'] += 1
                    # 云端也要计 token: 以前只有本地分支累加, 于是走 GLM 时
                    # 回复末尾永远没有 [tokens: ...] 明细
                    u = res.get('usage') or {}
                    try:
                        p = int(u.get('prompt_tokens') or 0)
                        c = int(u.get('completion_tokens') or 0)
                    except (TypeError, ValueError):
                        p = c = 0
                    if p or c:
                        LLM_USAGE['prompt_tokens'] += p
                        LLM_USAGE['completion_tokens'] += c
                        session['_tok_p'] = session.get('_tok_p', 0) + p
                        session['_tok_c'] = session.get('_tok_c', 0) + c
                    return text
                print('  [模型] 云端返回空, 退回本地')
                _mark_cloud(False)
            except Exception as e:                              # noqa: BLE001
                _mark_cloud(False)
                print(f'  [模型] 云端失败({type(e).__name__}: {str(e)[:80]}), '
                      f'回落本地 ollama, {config.CLOUD_RETRY_AFTER}s 内不再试云端')
        payload = json.dumps({
            'model': model or config.LLM_MODEL,
            'messages': msgs,
            'stream': False,
            'think': False,
            # keep_alive=-1: 模型常驻显存, 防止空闲 5 分钟被卸载后下一条消息重新冷启动
            'keep_alive': config.LLM_KEEP_ALIVE,
            'options': {
                # num_ctx 显式压到 4096: KV cache 从 1.3G 降到 ~160M, 4G 显存能放下
                # 全部 37 层(原先只放 23 层, 14 层掉 CPU 导致 5 tok/s)
                'num_ctx': config.LLM_NUM_CTX,
                'num_predict': num_predict, 'temperature': 0.7,
                'repeat_penalty': config.LLM_REPEAT_PENALTY,
            },
        }).encode()
        req = urllib.request.Request(
            config.OLLAMA_URL + '/api/chat', data=payload,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout or config.LLM_TIMEOUT) as r:
            data = json.loads(r.read().decode('utf-8', 'ignore'))
        # 消耗统计: ollama 返回含 prompt_eval_count/eval_count, 本地模型金额恒 0
        p = int(data.get('prompt_eval_count') or 0)
        c = int(data.get('eval_count') or 0)
        LLM_USAGE['prompt_tokens'] += p
        LLM_USAGE['completion_tokens'] += c
        LLM_USAGE['calls'] += 1
        # 本轮累计 tokens(重试/扩写多次调用都累加), 供 _finish 末尾追加 [tokens: 输入X+输出Y]
        session['_tok_p'] = session.get('_tok_p', 0) + p
        session['_tok_c'] = session.get('_tok_c', 0) + c
        reply = ((data.get('message') or {}).get('content') or '').strip()
        # 清理可能的思考标签残留(含只出不进的 </think> 半截标签)
        return _tidy_reply(reply) or None
    try:
        return _call()
    except Exception as e:
        # 偶发超时/瞬时失败(ollama 卡顿等): 自动重试一次, 仍失败才回落兜底
        print(f'  [模型] 调用失败({e}), 重试一次')
        try:
            return _call()
        except Exception as e2:
            print(f'  [模型] 调用失败, 回落兜底: {e2}')
            return None


def warmup(timeout=240):
    """启动时预热: 发一个最小请求(num_predict=1)把模型加载进显存并常驻.

    不做这步的话, ollama 要等第一条消息进来才现加载模型(实测 20.85s), 对方干等
    半分钟才收到回复。预热放在后台线程, 不阻塞 bot 主循环。
    keep_alive 用 config.LLM_KEEP_ALIVE(-1 常驻), 保证加载后不会被中途卸载。
    """
    if not config.LLM_ENABLED or not config.LLM_WARMUP:
        return
    payload = json.dumps({
        'model': config.LLM_MODEL,
        'messages': [{'role': 'user', 'content': 'hi'}],
        'stream': False,
        'think': False,
        'keep_alive': config.LLM_KEEP_ALIVE,
        'options': {'num_ctx': config.LLM_NUM_CTX, 'num_predict': 1,
                    'temperature': 0},
    }).encode()
    t0 = time.time()
    try:
        req = urllib.request.Request(
            config.OLLAMA_URL + '/api/chat', data=payload,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        print(f'[预热] {config.LLM_MODEL} 已加载常驻, 耗时 {time.time() - t0:.1f}s '
              f'(num_ctx={config.LLM_NUM_CTX}, keep_alive={config.LLM_KEEP_ALIVE})')
    except Exception as e:
        print(f'[预热] 失败(不影响 bot 运行, 仅首条消息会慢): {e}')


# 1.7b 语料自带的语气英文词: 模型爱混 "sure/probably/over" 等, 提示词拦不住就代码清洗。
# 注意用词边界匹配, 避免误伤 C++/APP 这类正文里的合法英文。
_EN2ZH = (
    (r'\bsure\b', '肯定'), (r'\bprobably\b', '大概'), (r'\bover\b', '完了'),
    (r'\banyway\b', '反正'), (r'\bok\b', '好'), (r'\bokay\b', '好'),
    (r'\bmaybe\b', '也许'), (r'\bwow\b', '哇'), (r'\bfine\b', '行'),
    (r'\boh\b', '哦'), (r'\bhaha\b', '哈哈'), (r'\byes\b', '嗯'),
    (r'\bno\b', '不'), (r'\bso\b', '所以'), (r'\bvery\b', '很'),
)


def _clean_english(reply):
    """把模型回复里混入的语气英文词替换成中文(1.7b 语料通病, 提示词约束不住的兜底)"""
    if not reply:
        return reply
    out = reply
    for pat, zh in _EN2ZH:
        out = re.sub(pat, zh, out, flags=re.I)
    # 兜底: 夹在中文之间的小写英文词(area/some/thing 等)直接删, 不误伤
    # iPhone/APP/C++ 这类合法大写/带符号词
    out = re.sub(r'(?<=[\u4e00-\u9fff，。！？])\s*[a-z]{2,12}\s*'
                 r'(?=[，。！？,.!?]|[\u4e00-\u9fff])', '', out)
    return out


# 圆括号动作词(模型爱堆"(笑)(抱抱)(点头)"这类, 删); 其余括号一律保留——
# 颜文字(ouo)(qwq)(一ω一)(>﹏<)等都不能误伤, 用户明确要求带颜文字
_ACT_BRACE_WORDS = ('笑', '抱', '挥手', '点头', '摇头', '叹气', '眨眼', '比心',
                    '敬礼', '捂脸', '扶额', '摊手', '耸肩', '挑眉', '搓手',
                    '鞠躬', '欢呼', '转圈', '摸头', '亲亲', '蹭蹭', '拍肩', '握手',
                    '下巴', '挠头', '伸懒腰', '沉思', '托腮')


def _polish_reply(reply):
    """模型回复质量兜底: 只删动作描写括号(笑/抱抱/点头...), 颜文字一律保留;
    防模型每条都堆'(笑)'绕圈, 又不误伤用户要的语气"""
    if not reply:
        return reply

    def _act_brace(m):
        inner = m.group(1)
        if any(w in inner for w in _ACT_BRACE_WORDS):
            return ''                       # 动作描写 -> 删
        return m.group(0)                   # 颜文字/表情 -> 保留

    out = re.sub(r'\(([^()]{1,20})\)', _act_brace, reply)
    out = out.replace('،', '，')                          # 阿拉伯逗号 -> 中文逗号(4b偶发混入)
    out = re.sub(r'[，,]\s*(?=[)）])', '', out)           # 删动作后的残逗号
    out = re.sub(r'[，,。；]+(?=[，,。；])', '，', out)    # 合并连续标点
    out = re.sub(r'\s{2,}', '', out)
    out = re.sub(r'^(?:啊|嗯|唉|哎|哦|哟|诶|呃|唔)[，,、。.!！?？～~\s…·]{0,3}',
                 '', out)  # 句首单字语气词: 防句句"啊,"开头(保留"嗯嗯""哎呀"等复字)
    out = out.strip(' ，,。；')
    if not out:
        return reply                            # 删空了就保留原句, 别整句消失
    return out


def _clip_reply(reply, max_len=80):
    """回复长度兜底: 超过 max_len 截到最近一个句末标点(不硬切半句)"""
    if not reply or len(reply) <= max_len:
        return reply
    cut = reply[:max_len]
    for i in range(len(cut) - 1, -1, -1):
        if cut[i] in '。！？!?':
            return cut[:i + 1]
    for i in range(len(cut) - 1, -1, -1):       # 兜底: 截到最近逗号, 别硬切半句
        if cut[i] in '，,；;':
            return cut[:i + 1]
    return cut


_LEAK_WORDS = ('编造', '严禁', '提示词', '逐步减少', '复述',
               '字数要求', '不跑题', '输出纪律', '诚实底线')
_LEAK_STRONG = ('编造', '严禁', '提示词', '逐步减少', '复述')


def _prompt_leak(reply):
    """检测模型回复是否复述/泄露了系统提示词内容(小模型常见故障):
    1) 照抄原文: 回复含提示词中任一较长短语(>=6字);
    2) 改写复述: 回复含 >=2 个提示词特征词, 或任一强特征词"""
    if not reply or not config.SYSTEM_PROMPT:
        return False
    r = re.sub(r'\s+', '', reply)
    chunks = re.split(r'[，。；、！？\n：:（）()]', config.SYSTEM_PROMPT)
    for c in chunks:
        c2 = re.sub(r'\s+', '', c)
        if len(c2) >= 6 and c2 in r:
            return True
    if sum(1 for w in _LEAK_WORDS if w in r) >= 2:
        return True
    if any(w in r for w in _LEAK_STRONG):
        return True
    return False


def _norm(s):
    """去空白和常用标点, 用于文本比对"""
    return re.sub(r'[\s，。！？、；：""''（）【】《》—~…-]+', '', s or '')


def _echo_user(reply, user_msg):
    """检测模型是否复读/截取用户问题(小模型上下文压力下的常见故障):
    1) 完全复读: 去标点后与用户消息相同;
    2) 部分复读: 回复是用户消息的子串(如"葫芦娃讲了什么"->"讲了什么")"""
    nr, nu = _norm(reply), _norm(user_msg)
    if not nr or not nu:
        return False
    if len(nr) >= 3 and nr == nu:
        return True
    return len(nr) >= 4 and nr in nu


def _dup_similar(a, b):
    """检测回复与上一条我方回复是否高度重复(小模型记不住上轮说了啥, 常见复读故障):
    1) 去标点后完全相同;
    2) 整体相似度>=0.6;
    3) 开头句式重复(小模型爱同句式换词, 如"哎呀...赶紧..."): 前12字相似度>=0.55"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) < 8 or len(nb) < 8:
        return False
    if difflib.SequenceMatcher(None, na, nb).ratio() >= 0.6:
        return True
    pa, pb = na[:12], nb[:12]
    if len(pa) >= 6 and len(pb) >= 6:
        sm = difflib.SequenceMatcher(None, pa, pb)
        # 前12字相似度>=0.55 且 有>=6字的连续相同块, 才算句式复读
        # (光相似度不够, 短句共情话"我觉得..."这类会被误判)
        return (sm.ratio() >= 0.55
                and sm.find_longest_match(0, len(pa), 0, len(pb)).size >= 6)
    return False


# ---- "每次回答后加XX描写" 类规则的代码层执行兜底 ----
# 1.7b 对 system prompt 尾部的追加要求遵守度低, 光注入规则不可靠;
# 故检测到这类规则后, 由代码在每次回复末尾统一追加一段景物描写(确定性保证).
_ADD_RE = re.compile(
    r'(?:每次回答|每次回复|答完后|以后.{0,4}回答).{0,20}(?:加|带上|附上|写|描述).{0,12}描写')
_SCENERY = {
    '春': '春风拂过，杨柳吐绿，桃花开得正艳，空气里满是青草和花的气息。',
    '夏': '午后的阳光透过树叶洒下来，蝉鸣阵阵，风里带着热乎乎的草木香。',
    '秋': '枫叶红了，稻田黄了，天空又高又蓝，风里飘着桂花香。',
    '冬': '雪花轻轻飘落，屋顶白了，呵出的气变成白雾，世界安静又干净。',
    '': '阳光正好，微风不燥，路边的花开得正热闹。',
}
_SCENERY_HITS = ('杨柳', '桃花', '青草', '蝉鸣', '枫叶', '稻田', '桂花', '雪花', '阳光正好')


def _apply_notes(remark, reply):
    """规则执行兜底: 会话存在"每次回答后加XX描写"类规则时, 若回复未含景物描写, 代码追加一段"""
    if not reply or not remark:
        return reply
    rules = [n for n in _load_notes(remark) if _ADD_RE.search(n)]
    if not rules:
        return reply
    blob = ''.join(rules)
    seg = next((_SCENERY[s] for s in ('春', '夏', '秋', '冬') if s in blob), _SCENERY[''])
    if seg and not any(w in reply for w in _SCENERY_HITS):
        return f'{reply}\n{seg}'
    return reply


# 括号颜文字/表情(去重用): 仅括号内不含汉字才认作颜文字(排除"（笑）"等动作描写;
# 用 [^()（）]* 限定只看括号内部, 颜文字后面跟中文不会误判为动作描写)
_FACE_RE = re.compile(r'[\(（](?![^()（）]*[\u4e00-\u9fff])([^()（）]{1,30})[\)）]')
# 颜文字白名单库(裸, 无括号): 库里只有这些才允许出现, 模型瞎编的一律删。
# 括号版 (^~^)/(>n<) 等: _FACE_RE 匹配后按括号内容对照本库, 内容在库里的括号颜文字保留。
# (用户核心诉求: 不在库里的别用; 用过的别重复) 数字(2333)易误伤子串, 不收。
_BARE_FACES = ('-v-', '－v－', 'qwq', 'ouo', 'owo', 'QAQ', 'QwQ', 'TAT', 'TvT',
               'T_T', '^_^', '>_<', '=w=', 'OvO', '>n<', '^~^')
# 主动补颜文字的优先级: 疑问/撒娇语气优先委屈类(用户例子: 你吃了嘛?qwq),
# 普通陈述优先轻快类; 都跳过本会话已用过的(去重铁律)
_FACE_PICK_Q = ('qwq', 'QwQ', 'QAQ', '>_<', 'T_T')
_FACE_PICK_N = ('(^~^)', '=w=', '^_^', '(>n<)', '-v-')


def _used_faces(session):
    """本会话历史回复里用过的颜文字集合(括号内容取裸形式)"""
    used = set()
    for m in session.get('messages', []):
        if m.get('role') != 'assistant':
            continue
        c = m.get('content', '')
        for f in _FACE_RE.findall(c):
            used.add(f.strip())
        for b in _BARE_FACES:
            if b in c:
                used.add(b)
    return used


def _dedupe_face(session, reply):
    """颜文字白名单清洗+去重(用户核心诉求: 不在库里的别用, 用过的别重复):
    1) 括号颜文字: 括号内容不在库(_BARE_FACES)里的(模型瞎编的)一律删;
    2) 本会话历史回复用过的(括号+裸)直接剔除; 本条回复内相同颜文字只保留第一处;
    3) 裸颜文字只认库里的(_BARE_FACES), 同样去重;
    只删整体匹配的颜文字, 不做格式统一(不加括号); 全删空时回退原回复"""
    used = _used_faces(session)
    seen = set()

    def _drop(m):
        f = m.group(1).strip()
        if f not in _BARE_FACES:        # 白名单清洗: 不在库里的, 模型瞎编的, 删
            return ''
        if f in used or f in seen:
            return ''
        seen.add(f)
        return m.group(0)

    out = _FACE_RE.sub(_drop, reply)
    for b in _BARE_FACES:                      # 裸颜文字: 历史用过或本条内重复 -> 剔除
        if b not in out:
            continue
        if b in used:
            out = out.replace(b, '')
        else:
            idx = out.find(b)
            rest = out[idx + len(b):]
            if b in rest:                      # 本条回复内重复: 保留第一处
                out = out[:idx + len(b)] + rest.replace(b, '')
    if out == reply:
        return reply
    out = re.sub(r'([，,。；、！？~～])\1+', r'\1', out)          # 合并连续相同标点
    out = re.sub(r'[，,。；、！？]\s*[，,。；、！？]', '，', out)   # 合并相邻标点
    out = re.sub(r'\s{2,}', '', out)
    out = out.strip(' ，,。；～~')
    return out or reply


def _ensure_face(session, reply):
    """主动补颜文字(用户诉求: 自己学会加, 不能等对方加):
    回复里若已没有白名单颜文字(被清洗/去重删光或本来就空), 从库里按语气挑一个
    本会话没用过的补到句尾——疑问/撒娇语气优先委屈类(你吃了嘛?qwq), 否则轻快类。
    库里候选全用过了就不硬凑, 原样返回。"""
    if not reply:
        return reply
    if _FACE_RE.search(reply):                       # 已有括号颜文字, 不补
        return reply
    if any(b in reply for b in _BARE_FACES):         # 已有裸颜文字, 不补
        return reply
    tail = reply.rstrip()
    cands = (_FACE_PICK_Q if tail.endswith(('？', '?', '嘛', '呢', '呀', '啊'))
             else _FACE_PICK_N)
    used = _used_faces(session)
    for c in cands:
        bare = re.sub(r'[\(\)（）]', '', c)           # 括号版取其裸内容对照历史
        if bare not in used:
            return reply + c
    return reply


# 模型句尾爱加的 Unicode emoji(✨😊❤️等): 代码层删掉——对方会自己加颜文字(qwq),
# 不需要模型抢着带 emoji。只删辅助平面表情 + 明确的 BMP 装饰 emoji(✨⭐⚠⚡❄❤❣),
# ★☆✧♥ 等颜文字常用符号绝不误伤。
_EMOJI_RE = re.compile(
    r'\s*[\U0001F000-\U0001FAFF\uFE0F\u200D'
    r'\u2728\u2B50\u26A0\u26A1\u2744\u2764\u2763]\s*')


def _strip_emoji(reply):
    """删模型回复里的 emoji(连周边空格一起删), 颜文字不误伤; 无变化原样返回"""
    if not reply:
        return reply
    out = _EMOJI_RE.sub('', reply)
    if out == reply:
        return reply
    out = re.sub(r'[，,。；]\s*$', '', out)      # 删末尾emoji后可能残留的孤立标点
    out = re.sub(r'\s{2,}', ' ', out).strip(' ，,。；～~ ')
    return out or reply


def _tone_soften(reply):
    """口吻统一: 句尾疑问'吗'->'嘛'(是嘛/吃了嘛/怪嘛)。提示词约束不住小模型,
    由代码强制兜底。仅替换句尾'吗'(后跟句末标点或结尾); 句中'吗'(你知道吗，)不动"""
    if not reply:
        return reply
    return re.sub(r'吗(?=[！？!?。～~…]|$)', '嘛', reply)


def _finish(session, reply, with_face=False):
    """统一收尾: 应用长期规则兜底(景物描写等), 写入会话记忆并返回.
    with_face=True 时(仅模型生成/润色的回复)若无颜文字则主动补一个库里的(用户诉求:
    自己学会加颜文字, 不在库里的别用, 不能等对方加)。kb固定回答/诗词/兜底话术不补.
    首轮回复时在末尾换行追加一行 [模型 | 开始时间], 便于确认走了哪条链路/哪个模型."""
    reply = _apply_notes(session.get('remark', ''), reply)
    reply = _strip_emoji(reply)   # 删模型句尾emoji(✨等), 颜文字不误伤
    reply = _tone_soften(reply)   # 口吻统一: 句尾'吗'->'嘛'(提示词约束不住, 代码兜底)
    reply = _dedupe_face(session, reply)   # 颜文字白名单清洗+去重: 不在库里的删, 用过的不用
    if with_face:
        reply = _ensure_face(session, reply)   # 主动补颜文字: 回复里没有就用库里的补一个
    # 调试尾巴([模型 | 开始 时间] / [tokens: ...])默认不发给对方。
    # 2026-11-11 实测: 这两个尾巴被原样拼进发送文本, 对方收到的消息末尾挂着
    # "[tokens: input: 1189 output: 11 cost:0.0000CNY]" —— 属于内部日志泄漏到
    # 用户视野。要调试时把 config.json 的 send_debug_tail 打开即可。
    if getattr(config, 'SEND_DEBUG_TAIL', False):
        t0 = session.pop('_t0', None)
        tail = []
        if t0 and not any(m['role'] == 'assistant' for m in session['messages']):
            ts = time.strftime('%H:%M:%S', time.localtime(t0))
            tail.append(f"[{config.LLM_MODEL} | 开始 {ts}]")
        # token 明细固定压在最末一行(模型名那行在前面), 云端/本地都记
        line = _usage_line(session)
        if line:
            tail.append(line)
    else:
        session.pop('_t0', None)
        _usage_line(session)          # 仍然把 token 记进 LLM_USAGE, 只是不拼进正文
        tail = []
    # 记忆里只存正文, 调试尾巴只随返回值发出去, 不回流进下一轮上下文
    session['messages'].append({'role': 'assistant', 'content': reply})
    print(f"  [会话] 记忆轮次已存 {len(session['messages'])} 条")
    return '\n'.join([reply] + tail) if tail else reply


def chat(session, incoming_texts, context=None):
    """对方新消息 -> 确定性引擎(固定问答 -> 诗词 -> 可选联网搜索 -> 模型 -> 兜底)
    incoming_texts: 对方本轮连发的消息列表(静默窗口聚合)
    context: 数据库里该会话最近的聊天记录 [(who, text)], 每轮重新注入作背景;
             传 None 时沿用上一次的(兼容老调用)
    答不上就诚实说, 绝不瞎编
    """
    # 上下文挂到会话上, 由 llm_ask 注入 system prompt: 这样 chat() 内部十几次
    # llm_ask 调用(复读换角度/字数扩写/去重重试)不用逐个改签名都能带上背景。
    if context is not None:
        session['_db_ctx'] = _format_context(context)
    if len(incoming_texts) == 1:
        user_msg = incoming_texts[0]
    else:
        user_msg = '\n'.join(f'对方: {t}' for t in incoming_texts)
    text = incoming_texts[-1]
    session['_t0'] = time.time()  # 首轮回复末尾要附上本轮开始时间
    print(f"[会话] {session['remark']} 本轮输入: {user_msg[:100]}")

    # 0) 复读/刷屏识别(须在本轮消息写入前判断, 取"上一条"对方消息): 本轮全是
    #    同一句话且与上一条相同 -> 不直接回固定话术, 而是提示模型换角度重新回应,
    #    让模型生成不同内容而非雷同刷屏
    repeat_hint = ''
    unique_msgs = list(dict.fromkeys(
        re.sub(r'\s+', '', t) for t in incoming_texts if t.strip()))
    last_user = next((m['content'] for m in reversed(session['messages'])
                      if m['role'] == 'user'), None)
    last_utterance = ''
    if last_user:
        parts = [p for p in last_user.split('\n') if p.strip()]
        last_utterance = re.sub(r'^对方:\s*', '', parts[-1] if parts else last_user)
    if (len(unique_msgs) == 1 and last_utterance
            and re.sub(r'\s+', '', last_utterance) == unique_msgs[0]):
        repeat_hint = ('对方刚发过同一句话, 正在复读; 请换个角度、换种说法重新回应, '
                       '不要重复上一轮回复的内容, 也不要指出对方在复读。')
        print('  [提示] 复读命中, 将提示模型换角度回应')

    session['messages'].append({'role': 'user', 'content': user_msg})

    if DIRECT_LLM:
        print('  [回复] 直通模型(跳过全部枚举路由)')
        return _chat_direct(session, text, repeat_hint)

    # 0.4) 确定性计算短路: 随机数/算式/一元二次方程直接本地算
    #      (模型数学常错且爱把推导过程全说出来, 测试第6条就是讲过程没直接给答案)
    r = try_compute(text)
    if r:
        print(f'  [回复] 本地计算短路: {r}')
        return _finish(session, r)

    # 0.5) 长期设定/记住的信息: 保存到该会话独立临时知识库, 本轮直接确认
    note_reply = _extract_notes(text, session.get('remark', ''))
    if note_reply:
        print(f'  [回复] 临时知识库处理: {note_reply}')
        return _finish(session, note_reply)

    r = kb.query_qa(text)
    if r:
        print('  [回复] 固定问答命中')
        return _finish(session, r)

    # 0.6) 身份类问题: 直接承认代班AI, 防'她'评价穿帮
    r = _identity_reply(text)
    if r:
        print('  [回复] 身份类固定回复')
        return _finish(session, r)

    # 0.7) 问当前时间: 确定性回答, 防小模型24小时制换算错(23:43说成快凌晨一点)
    r = _time_reply(text)
    if r:
        print('  [回复] 当前时间确定性回答')
        return _finish(session, r)

    # 0.71) 问今天日期/节日: 查 kb.json holidays, 防小模型瞎编节日
    r = _date_reply(text)
    if r:
        print('  [回复] 节日/日期确定性回答')
        return _finish(session, r)

    # 0.72) 生日 if 判定(藏着): 对方说今天是我生日, 且今天确实是0101 -> 回祝福
    r = _birthday_reply(text)
    if r:
        return _finish(session, r)

    # 0.8) 客套承诺被追问("给我买/寄给我/什么时候送"): 语义路由, 物理做不到诚实拒绝;
    #      普通话题(如"我买了新手机")判非命中放行走正常流程
    r = _promise_reply(text)
    if r:
        r = _polish_reply(_clip_reply(r, 80))
        if r and not _prompt_leak(r) and not _echo_user(r, text):
            print('  [回复] 客套承诺被追问, 诚实拒绝')
            return _finish(session, r, True)

    # 0.9) 道歉/吐苦水场景: 语义路由(一次模型调用判断场景+自然回应), 不靠关键词穷举;
    #      判为普通/格式不符/失败则返回 None, 静默走正常流程
    r = _mishap_reply(text)
    if r:
        r = _polish_reply(_clip_reply(r, 80))
        if r and not _prompt_leak(r) and not _echo_user(r, text):
            print('  [回复] 过失场景语义路由, 定向回应')
            return _finish(session, r)

    # 2) 诗词背诵
    r = kb.query_poem(text)
    if r:
        print('  [回复] 诗词库命中')
        return _finish(session, r)

    # 2.5) 背诵/默写/朗读请求但知识库未收录: 诚实说没收录, 不联网搜索
    if _is_recite_request(text):
        print('  [回复] 背诵请求但知识库未收录, 诚实兜底, 不搜索')
        return _finish(session, '这首我还没收录，背不了。你换个我背过的试试？')

    # 2.55) 直接报短篇名/专名(如"伐檀", 无背诵动词): 短专名本地模型无依据必瞎编,
    #       未收录时诚实兜底, 绝不放给模型编(日志实证: 伐檀被编成《诗经·邶风》)
    if _is_short_name_request(text):
        print('  [回复] 疑似直接报短篇名, 未收录, 诚实兜底, 不放模型瞎编')
        return _finish(session, '这个我还没收录，不能瞎编。你换个我学过的试试？')

    # 2.6) 本地位置类问题("王老师办公室怎么走"等): 语义路由, 模型无依据只会瞎编,
    #      命中后诚实兜底; 普通话题误伤判非命中放行
    r = _local_place_reply(text)
    if r:
        r = _polish_reply(_clip_reply(r, 80))
        if r and not _prompt_leak(r) and not _echo_user(r, text):
            print('  [回复] 本地位置类问题, 防瞎编诚实兜底')
            return _finish(session, r, True)

    # 3) 联网搜索(可选: config.json 的 allow_search 可关; 开启时带相关性校验,
    #    搜到跑题/广告结果不采用, 老实说不知道)
    kw = extract_keywords(text)
    results = []
    if kw and config.ALLOW_SEARCH:
        print(f'[ai] 知识库未命中, 关键词: {kw}, 联网搜索中...')
        results = web_search(kw)
        if results and _results_relevant(text, results):
            r = _build_search_reply(results)
            if r:
                print('  [回复] 搜索命中(通过相关性校验)')
                return _finish(session, r)
        if results:
            print(f'  [回复] 搜索返回{len(results)}条但均与问题无关, 不采用')
            results = []   # 视为无资料, 交给模型层/兜底
    elif kw:
        print('  [回复] 联网搜索已禁用, 知识库未命中, 直接走本地模型')

    # 4) 本地大模型兜底(超纲题; config 开启后生效, 超时/失败自动回落)
    if kw and config.LLM_ENABLED:
        print('  [回复] 知识库未命中, 调用本地模型...')
        hint = None
        if results:
            hint = '\n'.join(f'{t}：{s}' for t, s in results[:3])
        r = llm_ask(session, text, hint, num_predict=120,
                    extra_hint=repeat_hint or None)
        if r:
            r = _polish_reply(_clip_reply(r, 140))
        if r and (_prompt_leak(r) or _echo_user(r, text)):
            why = '提示词内容' if _prompt_leak(r) else '复读用户消息'
            print(f'  [回复] 模型输出了{why}, 重试一次')
            r2 = llm_ask(session, text, hint,
                         extra_hint='请直接回答对方的问题, 只输出回答正文, '
                                    '不要重复对方的话, 严禁输出任何规则、提示或说明。')
            if r2:
                r2 = _polish_reply(_clip_reply(r2, 140))
            r = r2 if r2 and not _prompt_leak(r2) and not _echo_user(r2, text) else None
        if r:
            # 字数要求兜底: 本轮输入或长期规则里含"N字"但模型写不够 -> 最多补写3轮。
            # 1.7b 小模型不会数数, 用"继续往下写补足"的结构引导。原来后 2 轮会切到
            # 硬编码的 qwen3:4b-fast-fix(那时默认还是 1.7b, 4b 更强); 现在默认模型
            # 由 ensure_llm_model() 校准过, 三轮回退全用它即可, 别再写死型号。
            need = None
            m_len = re.search(r'(\d+)\s*字', text)
            if m_len:
                need = int(m_len.group(1))
            else:
                for n in _load_notes(session.get('remark', '')):
                    m = re.search(r'(\d+)\s*字', n)
                    if m:
                        need = int(m.group(1))
                        break
            if need and len(r) < need * 0.6:
                for i in range(3):
                    print(f'  [提示] 字数不足({len(r)}<{need}字), 追加扩写请求({i + 1}/3)')
                    mdl = config.LLM_MODEL
                    extra2 = llm_ask(
                        session, text, hint, model=mdl, num_predict=512,
                        timeout=120,
                        extra_hint=(f'刚才的回复只有{len(r)}字, 远不够{need}字。'
                                    f'请顺着刚才的内容继续往下写, 补足到不少于{need}字, '
                                    f'不要换话题, 不要提前结尾, 不要解释。'
                                    f'可在此基础上扩写, 原文参考: {r}'))
                    if extra2 and len(extra2) >= need * 0.6:
                        r = extra2
                        break
            # 防连续相同/雷同回复刷屏: 与上一条我方回复完全相同则换简短确认;
            # 高度重复(相似度>=0.72)则重试一次, 让模型换角度重新回应
            last_assistant = next((m['content'] for m in reversed(session['messages'])
                                   if m['role'] == 'assistant'), None)
            if last_assistant and last_assistant == r:
                print('  [回复] 模型输出与上一条重复, 改为简短确认')
                r = '嗯嗯'
            elif last_assistant and _dup_similar(last_assistant, r):
                print('  [回复] 模型输出与上一条高度重复, 重试换角度')
                r2 = llm_ask(session, text, hint, num_predict=120,
                             extra_hint='对方刚问的是一个新问题，不要沿用上一轮回复'
                                        '的内容和句式，换全新的说法回应。')
                if r2:
                    r2 = _polish_reply(_clip_reply(r2, 140))
                    if (r2 and not _prompt_leak(r2) and not _echo_user(r2, text)
                            and not _dup_similar(last_assistant, r2)):
                        r = r2
            print(f'  [回复] 本地模型: {r[:60]}...')
            return _finish(session, r, True)

    # 5) 兜底: 区分"无实义关键词的问候/闲聊"和"有实词但知识库/模型都没答上"
    if kw:
        print('  [回复] 兜底: 知识库/模型均未答上')
        return _finish(session, FALLBACK)
    # 无实义关键词(纯闲聊/情绪请求, 如"你哄我一下"): 模型可用时交给模型提供情绪价值,
    # 而不是回固定话术(1.7b 应付"哄/求原谅"这类软性请求比硬编码话术自然得多)
    if config.LLM_ENABLED:
        r = llm_ask(session, text, None, num_predict=120,
                    timeout=min(config.LLM_TIMEOUT, 30))
        if r:
            r = _polish_reply(_clip_reply(r, 80))
        if r and not _prompt_leak(r) and not _echo_user(r, text):
            print(f'  [回复] 本地模型(闲聊/情绪): {r[:60]}...')
            return _finish(session, r)
    print('  [回复] 兜底: 无实义关键词(问候/闲聊)')
    return _finish(session, GREETING)
