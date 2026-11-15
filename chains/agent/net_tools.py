# -*- coding: utf-8 -*-
"""联网工具(imhere): 极简搜索 / 原始 HTTP 抓取

等价于在命令行敲:

    curl "https://cn.bing.com/search?q=关键词"

区别只是: 这里把结果页解析成模型能直接读的「标题 / 链接 / 摘要」纯文本。
真要跑 curl 命令行本体, 用 pc_tools.run_command('curl ...') 即可。

对外暴露两个工具(与 pc_tools 同一套 schema 格式, 见 TOOL_SPECS):
  * web_search(query) —— 搜索, 默认必应(国内可直连)
  * http_get(url)     —— 原始抓取(相当于 curl, 自动剥离 HTML 标签)
"""
import html
import re
import socket
import time
import urllib.parse

import requests

import config

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')
HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
}

# 一次正则把 script/style 整块和普通标签都去掉
_TAG_RE = re.compile(r'<(script|style)[^>]*>[\s\S]*?</\1>|<[^>]+>', re.I)
# HTML 注释要单独先删: 百度会把 <!--s-data:{"summaryDetail"...}--> 这种注解嵌在正文里,
# 而 `_TAG_RE` 的 <[^>]+> 遇到注释内部的 '>' 会只啃掉半截, 把 JSON 渣留在摘要里。
_COMMENT_RE = re.compile(r'<!--[\s\S]*?-->')
# 宽容版去标签: `<[^>]*>?` 允许标签**没有闭合的 '>'**。必需的 —— 百度会往标签属性里塞
# 极长的内联 JSON, 切片时经常只切到半个标签, 于是摘要里会漏出 `<div class="cos-col` 这种
# 残渣(实测踩过)。`_TAG_RE` 要求必须有 '>', 处理不了这种情况。
_TAG_LOOSE_RE = re.compile(r'<(script|style)[^>]*>[\s\S]*?</\1>|<[^>]*>?', re.I)
_ITEM_RE = re.compile(r'<li class="b_algo"[\s\S]*?</li>', re.I)
_LINK_RE = re.compile(
    r'<h2[^>]*>[\s\S]*?<a[^>]*href="(https?://[^"]+)"[^>]*>([\s\S]*?)</a>', re.I)
_SNIP_RE = re.compile(r'<p[^>]*>([\s\S]*?)</p>', re.I)
_ANY_LINK_RE = re.compile(
    r'<a[^>]+href="(https?://[^"]+)"[^>]*>([\s\S]{10,200}?)</a>', re.I)

# ---- 百度(结构与必应完全不是一套, 见 _parse_baidu 的说明) ----
# 用零宽前瞻切块, 这样容器 div 本身(以及它身上的 mu 属性)留在块里
_BD_SPLIT_RE = re.compile(r'(?=<div[^>]+class="[^"]*\bc-container\b)', re.I)
_BD_TITLE_RE = re.compile(
    r'<h3[^>]*>[\s\S]{0,400}?<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', re.I)
_BD_MU_RE = re.compile(r'\bmu="(https?://[^"]+)"', re.I)
# 广告块: 实测广告链接是 baidu.php?url=(推广跳转), 容器 class 带 ec-pc- 前缀, 也可能挂
# data-tuiguang。不滤的话"AI生图 / 立即获取答案"这类推广会被当成结果递给模型。
_BD_AD_RE = re.compile(r'\bdata-tuiguang\b|class="[^"]*\bec-[a-z]', re.I)
# 摘要锚点。实测(「冒泡排序 python 代码」页)每个结果块里都有两个 cos-line-clamp:
#   class="tts-b-hl cos-line-clamp-1"                -> 是**标题的重复**, 不是摘要;
#   class="cos-line-clamp-2/3 click-area-exp-block"  -> 这才是摘要正文。
# 所以只认 click-area-exp-block, 或行数 N>=2 的 clamp; c-abstract 只当兜底。
_BD_EXP_RE = re.compile(r'class="[^"]*click-area-exp-block[^"]*"', re.I)
_BD_CLAMP_N_RE = re.compile(r'class="[^"]*cos-line-clamp-([2-9]\d*)[^"]*"', re.I)
_BD_ABS_RE = re.compile(r'class="[^"]*c-abstract[^"]*"', re.I)


def _plain(s):
    """去标签 + 去注释 + 反转义 + 压空白"""
    s = _COMMENT_RE.sub(' ', s or '')
    s = _TAG_RE.sub(' ', s)
    s = html.unescape(s)
    return re.sub(r'\s+', ' ', s).strip()


def _safe_host(url):
    """解析主机名并校验全部 IP 均为公网。返回校验通过的 IP(str) 或 None。

    只做"校验"; 真正连接由 _get 用该 IP 直连, 杜绝校验-连接之间的 DNS 重绑(TOCTOU)。
    """
    import socket, ipaddress
    from urllib.parse import urlparse
    try:
        host = urlparse(str(url)).hostname
        if not host:
            return None
        try:
            ips = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                ips = [ipaddress.ip_address(a[4][0]) for a in
                       socket.getaddrinfo(host, None)]
            except Exception:
                return None
        for ip in ips:
            if not (ip.is_global and not ip.is_private and not ip.is_loopback
                    and not ip.is_link_local and not ip.is_reserved
                    and not ip.is_multicast):
                return None
        return str(ips[0])
    except Exception:
        return None


def _get(url, timeout=None):
    """抓取 URL, 带 SSRF 防护并防 DNS 重绑。

    旧实现: _safe_host 校验 IP 后, 仍把原 hostname 交给 requests 重新解析连接 ——
    攻击者可控 DNS 时能在"校验"时回公网 IP、"连接"时回 127.0.0.1 / 169.254.169.254
    (云元数据), 守卫形同虚设。
    新实现: 校验通过后, 临时把该 host 的 DNS 解析钉到校验过的 IP, 既保留 Host/SNI
    供 TLS 证书校验, 又让连接真正落到校验过的 IP(防 DNS 重绑 TOCTOU)。
    """
    import socket as _sock
    from urllib.parse import urlparse
    ip = _safe_host(url)
    if not ip:
        raise ValueError('SSRF 防护: 拒绝访问非公网地址 %s' % url)
    p = urlparse(str(url))
    host = p.hostname
    if not host or host == ip:
        return requests.get(url, headers=HEADERS,
                           timeout=timeout or config.SEARCH_TIMEOUT)
    fam = _sock.AF_INET6 if ':' in ip else _sock.AF_INET
    orig = _sock.getaddrinfo

    def _patched(*args, **kwargs):
        if args and args[0] == host:
            port = args[1] if len(args) > 1 else 0
            return [(fam, _sock.SOCK_STREAM, 6, '', (ip, port))]
        return orig(*args, **kwargs)
    _sock.getaddrinfo = _patched
    try:
        return requests.get(url, headers=HEADERS,
                           timeout=timeout or config.SEARCH_TIMEOUT)
    finally:
        _sock.getaddrinfo = orig


# ---- 百度反爬: 缺 Referer 就直接给你「安全验证」页 -----------------------------
# 实测(同一台机器、同一个 URL 连抓三次):
#   * 裸抓        -> HTTP 200, 但只有 1488 字符, 正文是"百度安全验证 网络不给力";
#   * 等 25 秒再抓 -> 还是被拦(所以这不是频率/超时问题, 等没用);
#   * 加 Referer: https://www.baidu.com/ -> 立刻 110 万字符、106 个结果块;
#   * 先访问百度首页拿到 Cookie 再用同一会话搜 -> 也行。
# 所以两样都做(带 Referer + 预热 Cookie), 并且**必须识别出验证页如实上报** ——
# 否则模型会把"被拦了"当成"搜不到", 凭空下结论。
_BD_HOME = 'https://www.baidu.com/'
_BD_BLOCKED_MARKS = ('百度安全验证', 'wappass', '网络不给力')
_BD_SESSION = None
_BD_WARMED = False


def _baidu_session():
    global _BD_SESSION, _BD_WARMED
    if _BD_SESSION is None:
        _BD_SESSION = requests.Session()
        _BD_SESSION.headers.update(HEADERS)
    if not _BD_WARMED:
        _BD_WARMED = True                     # 无论成败都只预热一次
        try:
            _BD_SESSION.get(_BD_HOME, timeout=config.SEARCH_TIMEOUT)
        except Exception:
            pass
    return _BD_SESSION


def _fetch_page(url, engine):
    """按引擎取搜索页。百度走"预热会话 + Referer", 否则会被反爬拦成验证页。"""
    if (engine or '').lower() == 'baidu':
        return _baidu_session().get(
            url, headers={'Referer': _BD_HOME}, timeout=config.SEARCH_TIMEOUT)
    return _get(url)


def _resp_text(r):
    """解码响应体。百度验证页不声明 charset, requests 会默认 ISO-8859-1, 中文全乱码。"""
    enc = (r.encoding or '').lower()
    if not enc or enc in ('iso-8859-1', 'latin-1'):
        enc = 'utf-8'
    try:
        return r.content.decode(enc, 'replace')
    except LookupError:
        return r.text


# 反爬拦截页判据: 页面很短 + 带验证/限流字样。
# 百度那三个字样是实测抓到的; 必应也会给 "One last step" 这类限流页, 一并认。
_BLOCK_MARKS = _BD_BLOCKED_MARKS + (
    'one last step', 'verify you are human', 'are you a robot',
    'unusual traffic', 'too many requests', '请稍候', '访问异常')


def _is_blocked(text):
    """反爬拦截页判据: 页面很短 + 带验证/限流字样(两个引擎共用)。"""
    t = str(text or '')
    return len(t) < 8000 and any(k in t.lower() for k in _BLOCK_MARKS)


# 被拦之后别再硬打: 实测(连搜 4 次、每次都换全新 Session+新 Cookie)全部照拦,
# 说明这是按出口 IP 判的, 换会话/换 Cookie 都逃不掉; 静置约 45 秒后同 IP 又恢复正常。
# 所以被拦后进冷却, 期间直接返回提示、不发请求 —— 免得把封禁越拖越长。
# 必应同样会限流(实测连续快抓时会给 429 / "One last step"), 但恢复比百度快, 冷却短些。
_BLOCK_COOLDOWN_S = {'baidu': 60, 'bing': 30}
_block_until = {'baidu': 0.0, 'bing': 0.0}


def _on_cooldown(eng):
    return time.time() < _block_until.get(eng, 0.0)


def _mark_blocked(eng):
    _block_until[eng] = time.time() + _BLOCK_COOLDOWN_S.get(eng, 60)


def _block_msg(eng, url, cached=False):
    """两个引擎共用的"被反爬拦了"口径: 说清被拦, 且**不代表没有相关信息**。"""
    name = {'baidu': '百度', 'bing': '必应'}.get(eng, eng)
    other = 'baidu' if eng == 'bing' else 'bing'
    wait = int(max(0, _block_until.get(eng, 0.0) - time.time())) if cached else 0
    when = f'(还在冷却期, 约 {wait} 秒内不再向它发请求)' if cached and wait else ''
    return (f'搜索失败: {url}\n'
            f'{name} 返回的是「安全验证/限流」页(按 IP 反爬), 没拿到真实结果{when}。'
            f'注意这不代表"没有相关信息", 只是这次被拦了 —— '
            f'可以改用 engine={other} 再搜一次, 或者如实告诉对方这次没搜到。')


# ============================================================
# 网络异常分类 —— "搜不到"和"没网了"必须分开说
# ============================================================
# 为什么非做这个: 原先所有抓取异常都被压成一句
#    搜索失败(ConnectionError): HTTPSConnectionPool(host=...) Max retries exceeded
# 4B 拿到这句话的反应是"换个说法再搜一次"(它以为是自己搜索词没写对), 于是六轮
# 全烧在重复搜索上, 对方那边只看到一串"正在搜索"。而下面这四种情况的处理方式
# 完全不同, 必须分类如实告诉模型:
#   * 没网     -> 换搜索词、换引擎都没用, 只能如实说"现在连不上网"
#   * 被墙/超时 -> 可以换引擎(必应 <-> 百度)再试一次
#   * 被反爬拦 -> 冷却后再试, 被拦 != 没有相关信息
#   * 代理/证书 -> 是这台机器的配置问题, 同样别赖搜索词
def _exc_kind(e):
    """把 requests 抛出的异常翻译成 (类别, 人话)。

    类别: no_net / dns / timeout / proxy / ssl / refused / other
    匹配的是真实报错特征(Windows + requests 2.x 的实测口径):
      ProxyError / SSLError
      ConnectionError: [WinError 10051] 网络不可达 / [WinError 10060] 连接超时
      ConnectionError: Max retries exceeded ... NameResolutionError ... getaddrinfo failed
      ConnectTimeout / ReadTimeout
    """
    s = f'{type(e).__name__}: {e}'.lower()
    if 'proxy' in s:
        return 'proxy', '本机代理连不上'
    if 'ssl' in s or 'certificate' in s:
        return 'ssl', 'SSL/证书握手失败'
    if 'getaddrinfo' in s or 'nameresolution' in s or 'name or service not known' in s:
        return 'dns', '域名解析失败(DNS 查不到)'
    if '10051' in s or 'network is unreachable' in s or 'no route to host' in s:
        return 'no_net', '本机网络不可达'
    if 'timeout' in s or 'timed out' in s:
        return 'timeout', '连接超时(网络慢或被墙)'
    if 'refused' in s or '10060' in s or '10061' in s:
        return 'refused', '目标服务器拒绝连接'
    if 'connection' in s:
        return 'no_net', '网络连接中断'
    return 'other', type(e).__name__


# 断网时的快速失败: 没有这一段, 每次搜索都要干等满 SEARCH_TIMEOUT(15s) 才报错,
# 六轮循环能把两分钟全耗在等超时上, 而对方那边只看到"正在搜索"。
# 用一条 TCP 连接 0.8 秒就能问清"这台机器整体还有没有网", 结论缓存 20 秒。
_PROBE_HOSTS = (('www.bing.com', 443), ('www.baidu.com', 443))
_NET_DOWN_COOLDOWN_S = 20
_net_down_until = 0.0


def _probe_net(timeout=0.8):
    """TCP 连一下门户站点: 连得上 = 至少还有网(不属于本机整体断网)。"""
    for host, port in _PROBE_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            continue
    return False


def _mark_net_down():
    global _net_down_until
    _net_down_until = time.time() + _NET_DOWN_COOLDOWN_S


def _net_down_msg(extra=''):
    return ('搜索失败: 这台电脑现在没有网络连接(断网, 或网卡/代理没起来)。'
            '换搜索词、换引擎都没用, 也不要再反复重试。'
            f'请直接如实告诉对方"这边现在连不上网", 不要再搜。{extra}').strip()


# ============================================================
# 同 query 结果缓存
# ============================================================
# 两个作用:
#   1) 直接掐死"模型换个小参数再搜同一句"的循环 —— agent 那层的去重闸只认参数,
#      模型给 count 加个 1 就是一个新 key, 照样能再打一次网络;
#   2) 少打一次就少一分被反爬盯上的机会。
_CACHE = {}
_CACHE_TTL = 120


def _cache_get(eng, query):
    """命中返回 (text, hits); 没命中返回 (None, None)。"""
    it = _CACHE.get((eng, query))
    if it and time.time() - it[0] < _CACHE_TTL:
        return it[1], it[2]
    return None, None


def _cache_put(eng, query, text, hits):
    _CACHE[(eng, query)] = (time.time(), text, hits or [])
    if len(_CACHE) > 64:                    # 只留最近 32 条, 免得常驻进程里无限涨
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:len(_CACHE) - 32]:
            _CACHE.pop(k, None)


# 缓存命中的抬头。**故意以「（」开头**: agent 的 _is_ctrl 会把它认成"程序自己造的
# 控制文本", 于是只回灌给模型、不播报进微信 —— 模型拿到"别重复搜"的明确指令,
# 对方那边也不会看到"返回结果: 这句查询刚刚已经搜过一次"这种内部机制的话。
_CACHE_HINT = ('（注意: 这句查询刚刚已经搜过一次, 下面就是那次的原结果, 一个字都没变。'
               '再搜同一句不会有新东西。要么就用它作答, 要么换一句真的不同的说法。）\n')


# ============================================================
# 搜索
# ============================================================
def search_url(query, engine=None):
    """拼出搜索地址(就是你在命令行 curl 的那个 URL)"""
    q = urllib.parse.quote_plus(str(query))
    engine = (engine or config.SEARCH_ENGINE).lower()
    if engine == 'baidu':
        return f'https://www.baidu.com/s?wd={q}'
    if engine == 'google':
        return f'https://www.google.com/search?q={q}'
    return f'https://cn.bing.com/search?q={q}'


def _parse_bing(page, count):
    hits = []
    for block in _ITEM_RE.findall(page):
        m = _LINK_RE.search(block)
        if not m:
            continue
        title = _plain(m.group(2))
        if not title:
            continue
        s = _SNIP_RE.search(block)
        hits.append((title, m.group(1), _plain(s.group(1)) if s else ''))
        if len(hits) >= count:
            break
    return hits


def _bd_same(a, b):
    """去掉空白/标点后判是不是同一句(用来排掉"摘要其实是标题的重复")。"""
    na = re.sub(r'[\W_]+', '', a or '')
    nb = re.sub(r'[\W_]+', '', b or '')
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _bd_clean(raw):
    """把一段 HTML 片段变成可读文字; 连**没闭合的标签**一起去掉。"""
    s = _COMMENT_RE.sub(' ', raw)
    s = _TAG_LOOSE_RE.sub(' ', s)
    s = html.unescape(s)
    return re.sub(r'\s+', ' ', s).strip()


def _bd_ok_text(s, title):
    """判一段候选摘要像不像人话(不像就宁可不要)。"""
    if len(s) < 15 or _bd_same(s, title):
        return False
    # 内联 JSON / CSS 残渣的特征
    if any(k in s for k in ('class=', 'style=', 'box-sizing', '":', '{"', '</')):
        return False
    return len(re.findall(r'[\u4e00-\u9fffA-Za-z]', s)) >= 8


def _bd_snippet(block, title):
    """在块里定位摘要正文。

    三个坑都是实测踩出来的:
      1) 不能"非贪婪匹配到最近的 </span>"—— 摘要元素里还套着别的 span, 会在第一个子
         span 处就截断。改成: 找到 class 所在标签的 '>' 之后, 直接切一段原文去标签。
      2) 不能见到 cos-line-clamp 就取 —— 块里第一个 clamp 是 `tts-b-hl cos-line-clamp-1`,
         那是**标题的重复**(实测 8 条里 6 条的摘要都在复读标题)。真摘要是
         `cos-line-clamp-2/3 click-area-exp-block`。
      3) **也不能只试第一个候选**: 一个块里可能有好几个 click-area-exp-block / clamp,
         排在前面的那个里面裹着巨大的内联 JSON, 切出来是 `<div class="cos-col` 这种残渣。
         所以把所有候选都试一遍, 用 _bd_ok_text 挑第一个"像人话"的。
    一个都不像就返回空 —— 宁缺, 也不拿标题或标签渣凑数当摘要。
    """
    cands = list(_BD_EXP_RE.finditer(block)) + list(_BD_CLAMP_N_RE.finditer(block))
    a = _BD_ABS_RE.search(block)
    if a:
        cands.append(a)
    for mt in cands:
        gt = block.find('>', mt.end())
        if gt < 0:
            continue
        s = _bd_clean(block[gt + 1: gt + 2600])[:220]
        if _bd_ok_text(s, title):
            return s
    return ''


def _parse_baidu(page, count):
    """解析百度结果页。

    下面每一条都是照着一页真实结果(带 Referer 抓下来的 110 万字符页)数出来的, 不是照记忆写的:
      * 结果项容器是 class 里带 c-container 的 div(实测整页 11~12 个)。**不能**拿
        class="result" 去匹配 —— 页面上还有 result-molecule / result-op 这类 UI 组件,
        一匹配就把"问AI / AI生图"那种输入框组件当成结果抓回来。
      * 实测三类块: `result-op c-container new-pmd`(AI/组件卡片, 没有 h3>a, 被标题正则
        自然跳过) / `...ec-pc-...`(广告) / `result c-container xpath-log new-pmd`(真结果)。
      * 标题在 <h3 ...><a href="...">标题</a> 里; <a> 会把 href **重复写两遍**, 取第一个。
      * href 基本都是 www.baidu.com/link?url=… 的中转链接(落地靠 JS 不是 302, http_get
        打不开)。真地址挂在容器 div 的 mu="https://…" 上, 有 mu 且不是百度自家域名时换掉。
      * **广告必须滤掉**: 实测广告块链接是 baidu.php?url=(推广跳转), 容器 class 带 ec-pc-
        前缀。不滤的话「静夜思 全诗」会把"属于什么体裁-立即获取答案""WorkBuddy-AI生图"
        排在最前面当结果 —— 它自己说了"不能全信任", 那更不该把推广递过去。
    """
    hits, seen = [], set()
    for block in _BD_SPLIT_RE.split(page)[1:]:
        block = block[:20000]          # 有些卡片块几十万字符, 先截断再上正则
        m = _BD_TITLE_RE.search(block)
        if not m:
            continue
        title = _plain(m.group(2))
        if not title or title in seen:
            continue
        link = m.group(1)
        if 'baidu.php' in link or _BD_AD_RE.search(block[:600]):
            continue
        mu = _BD_MU_RE.search(block)
        if mu:
            real = html.unescape(mu.group(1))
            if 'baidu.com' not in real:
                link = real
        seen.add(title)
        hits.append((title, link, _bd_snippet(block, title)))
        if len(hits) >= count:
            break
    return hits


def _parse(page, count, engine='bing'):
    """先按当前引擎的结果结构解析; 都解析不到再退化成"正文里所有像链接的条目"。"""
    hits = (_parse_baidu(page, count) if (engine or '').lower() == 'baidu'
            else _parse_bing(page, count))
    if hits:
        return hits

    skip = ('bing.com', 'baidu.com', 'google.com', 'microsoft.com/zh-cn')
    seen = set()
    for m in _ANY_LINK_RE.finditer(page):
        link, title = m.group(1), _plain(m.group(2))
        if link in seen or len(title) < 10 or any(k in link for k in skip):
            continue
        seen.add(link)
        hits.append((title, link, ''))
        if len(hits) >= count:
            break
    return hits


# 相关性判定要先剔掉的"疑问字/虚字"。
# 这些字在中文里出现频率极高 —— 尤其"的是什么"三个, 几乎任何中文页面都带。
# 实测代价: 查询"J空间是什么"被判成"结果相关", 而放行它的是第 7 条
# "英语字母 j 和国际音标 [j] 为什么读音完全不同"("为什么"凑齐了 什/么/是 三字
# = 3/5 ≥ 0.5); 查询的实词"空间"一条都没沾。模型于是放开手, 拿第 8 条
# "晋江文学城"替对方下了定义("J空间是网络文学平台, 成立于2003年8月")。
_STOP_CHARS = set('的是了么吗呢吧啊呀哦嘛什么怎样哪为有没在和与及或者就都也很太'
                  '我你他她它请帮下')


def _has_cjk(s):
    """串里有没有汉字(纯英文查询如 "Python"/"5G" 不该被当疑问词丢掉)。"""
    return any('\u4e00' <= c <= '\u9fff' for c in str(s or ''))


def _content_chars(q):
    """查询里的"实词汉字" = 全部汉字 - 疑问字/虚字。"""
    return {c for c in str(q or '') if '\u4e00' <= c <= '\u9fff'} - _STOP_CHARS


def _relevant(hits, query):
    """命中条目里有多少条真的沾了查询词。

    实测必应对某些查询会返回完全无关的一页 —— 搜"保德路的位置"给回来的是万智牌/
    炉石/旅法师营地(连发三次分别 1/8/8 条, 交集 0 条, 就是在轮换着给垃圾), 标题里
    连查询词的边都摸不到。不做这个检查的话模型会把它当资料转述: 日志里它就对着那页
    万智牌替对方编了句"保德路可能和国家自然科学基金有关"。

    判据两条, 沾一条就算:
      1) 查询词(按分隔符切开的词)整词出现在标题/摘要/链接里;
      2) 查询里的**实词**汉字被这条结果覆盖到一半以上, 且至少沾 2 个字。
    为什么要第 2 条: 中文查询常常不带分隔符, "今天北京天气"会被当成一整个词, 拿整串
    去标题里找必然找不到 —— 只看第 1 条会把正常的天气结果误判成"无关", 反而是在教
    模型别信好结果。

    为什么第 2 条要先剔疑问字、还要求 ≥2 个字(2026-10-08 加的):
      只沾 1 个汉字太容易是巧合。上面那个"J空间是什么"被一页字母 J 的资料放行,
      就是"为什么"贡献了 什/么/是 —— 去掉"是/什/么"后实词只剩 {空,间},
      8 条垃圾结果一条都凑不出 2 个, 于是如实判"无关"。反过来, 真正的天气结果
      同时带"北京""天气", 4 个实词字全覆盖, 照样通过。纯疑问词("是什么")也不再
      当查询词参与第 1 条匹配 —— 否则标题里随便一句"…是什么"就能骗过判据。
    """
    q = str(query or '')
    toks = [t for t in re.split(r'[\s,，。、：:；;？?！!/|]+', q) if len(t) >= 2]
    # 纯疑问字组成的"词"(是什么/怎么样)不算查询词; 带非汉字的词(Python/5G)原样保留。
    toks = [t for t in toks if _content_chars(t) or not _has_cjk(t)]
    chars = _content_chars(q)
    n = 0
    for title, link, snip in hits:
        blob = f'{title} {snip} {link}'.lower()
        if toks and any(t.lower() in blob for t in toks):
            n += 1
            continue
        hit_chars = chars & set(blob)
        if chars and len(hit_chars) / len(chars) >= 0.5 \
                and len(hit_chars) >= min(2, len(chars)):
            n += 1
    return n


def _hit_lines(hits, query):
    """结果排版 + "这页没用"的警告(两个引擎共用一套口径)。"""
    lines = []
    if hits and not _relevant(hits, query):
        # 明确告诉模型"这页没用", 否则它照样会拿这些条目编答案。
        lines.append('⚠ 这些结果里没有一条包含查询词, 大概率是搜索引擎返回的无关内容, '
                     '不要拿它当资料/答案。换一句**明显不同**的说法可以再试一次; '
                     '同一句话重复搜不会有新结果。')
    for i, (title, link, snip) in enumerate(hits, 1):
        lines.append(f'{i}. {title}\n   {link}')
        if snip:
            lines.append(f'   {snip}')
    return lines


def _overlap(a, b):
    """两个引擎互相印证的条目数(标题前 10 个字一样就算同一条)。"""
    return len({t[:10] for t, _, _ in a} & {t[:10] for t, _, _ in b})


def _one_line(text, limit=100):
    """取一段结果文本的第一行(去掉 URL, 只留人能读的一句)。

    用来在双源对照里转述"另一边为什么没结果" —— 直接把整段失败说明贴进去
    (里面有 URL、有建议)会把对照表撑乱。
    """
    for line in str(text or '').splitlines():
        line = re.sub(r'\(?https?://\S*\)?', '', line).strip()
        if line:
            return line[:limit]
    return '没拿到结果'


def _search_one(query, eng, n):
    """搜一次, 返回 (ok, text, hits)。

    ok=False = 这一次没拿到**可用**结果(异常 / 被反爬 / 没解析出条目 / 条目全无关),
    调用方据此决定"要不要换个引擎再试一次"。
    text 里一律如实说清失败属于哪一类 —— 断网、被墙、被反爬、代理错, 说法完全不同,
    模型对它们的正确反应也完全不同(断网就该收手, 被墙才值得换引擎)。
    """
    url = search_url(query, eng)
    if _on_cooldown(eng):
        return False, _block_msg(eng, url, cached=True), []
    if time.time() < _net_down_until:
        return False, _net_down_msg(), []
    try:
        r = _fetch_page(url, eng)
    except Exception as e:
        kind, why = _exc_kind(e)
        # 连接类报错要再确认一次"是不是整机断网": 断网必须直说, 不能让模型以为
        # 换个词就能搜到 —— 那正是六轮全烧在搜索上的来源。
        if kind in ('no_net', 'dns') and not _probe_net():
            _mark_net_down()
            return False, _net_down_msg(f'(报错详情: {why})'), []
        if kind == 'proxy':
            return False, (f'搜索失败: {url}\n'
                           f'这台电脑配了代理但连不上({why}) —— 是本机的网络配置问题, '
                           f'换搜索词没用。请如实告诉对方现在搜不了。'), []
        tip = {'timeout': '网络慢或被墙, 可以换个引擎再试',
               'dns': '也可能是本机 DNS/网络问题, 可以换个引擎再试',
               'ssl': '多为网络中间设备干扰, 可以换个引擎再试',
               'refused': '目标站点拒绝连接, 可以换个引擎再试',
               'no_net': '可以换个引擎再试'}.get(kind, '可稍后重试')
        return False, f'搜索失败({why}): {url}\n{tip}。', []
    if r.status_code in (403, 429):
        _mark_blocked(eng)
        return False, _block_msg(eng, url), []
    if r.status_code != 200:
        return False, f'搜索失败: HTTP {r.status_code}  {url}', []
    page = _resp_text(r)
    if _is_blocked(page):
        _mark_blocked(eng)
        return False, _block_msg(eng, url), []
    hits = _parse(page, n, eng)
    head = f'搜索「{query}」命中 {len(hits)} 条  ({url})'
    if not hits:
        return False, (head + '\n结果页结构没解析出条目, 下面是页面正文片段:\n'
                       + _plain(page)[:1200]), []
    text = '\n'.join([head] + _hit_lines(hits, query))
    # 条目一条都不沾查询词 = 这页没用, 同样算"没拿到可用结果", 让调用方换引擎再来。
    return bool(_relevant(hits, query)), text, hits


def _search_both(query, n):
    """必应 + 百度各搜一次, 分开列出来对照。

    为什么要两源: 百度对中文内容的榜单跟必应不是一套, 有些中文的事只有它那儿有;
    但它同样会给无关页(实测也有), 所以两边都列、都标, 谁也不是唯一真相 ——
    互相印证的条目才值得优先采信。
    """
    out = [f'搜索「{query}」(必应/百度 双源对照, 两边都不能全信)']
    got = {}
    for eng, name in (('bing', '必应'), ('baidu', '百度')):
        text, hits = _cache_get(eng, query)
        if text is None:
            ok, text, hits = _search_one(query, eng, n)
            if ok:
                _cache_put(eng, query, text, hits)
        got[eng] = hits or []
        out.append(f'── {name} 命中 {len(got[eng])} 条 ──')
        if got[eng]:
            out.extend(_hit_lines(got[eng], query))
        else:
            out.append(f'  {_one_line(text)}')
    b, d = got.get('bing', []), got.get('baidu', [])
    if b and d:
        same = _overlap(b, d)
        if same:
            out.append(f'※ 两个引擎都给出的条目有 {same} 条, 这些相对更可信; '
                       f'只有一边给的, 用之前先自己判断。')
        else:
            out.append('※ 两个引擎给的条目没有重合, 说明这份结果不稳, 别当定论。')
    else:
        out.append('※ 这次只拿到一个引擎的结果(另一边被拦或没解析出来), 属于单方来源, '
                   '别当定论; 想对照可以稍后重试。')
    return '\n'.join(out)


def web_search(query, count=8, engine=None):
    """联网搜索, 返回可直接阅读的结果列表。

    engine: bing(默认) / baidu / both

    失败一律**分类说清**(断网 / 被墙超时 / 代理错 / 被反爬拦 / 结果全无关), 而且
    主引擎拿不到可用结果时**自动换另一个引擎再试一次** —— 两个都不行才算真失败。
    为什么值得自动换: 必应给无关页、百度被反爬拦, 这两件事实测都常发生, 而原因
    互不相干, 换过去往往就好了; 旧代码只会把失败原样递给模型, 4B 拿到以后既不换
    引擎也不换说法, 只会原地重搜, 六轮全烧在搜索上。
    """
    query = str(query or '').strip()
    if not query:
        return '搜索词为空'
    eng = (engine or config.SEARCH_ENGINE or 'bing').lower()
    n = max(1, min(int(count or 8), 20))
    if eng == 'both':
        return _search_both(query, n)

    text, _ = _cache_get(eng, query)
    if text:
        return _CACHE_HINT + text

    ok, text, hits = _search_one(query, eng, n)
    if ok:
        _cache_put(eng, query, text, hits)
        return text

    other = 'bing' if eng == 'baidu' else 'baidu'
    ot, _h = _cache_get(other, query)
    if ot:
        return (text + f'\n\n※ 上面这条没拿到可用结果, 而 {other} 那边刚搜过同一句, '
                       f'直接给你它的结果(不再重复请求):\n' + ot)
    ok2, text2, hits2 = _search_one(query, other, n)
    if ok2:
        _cache_put(other, query, text2, hits2)
        return (text + f'\n\n※ 上面这条没拿到可用结果, 已自动改用 {other} 重搜, '
                       f'下面是它的结果:\n' + text2)
    return (text + f'\n\n※ 换了 {other} 也一样没拿到 ({_one_line(text2)})。'
                   f'两个引擎都试过了, 就用上面这些如实作答, 别再重复搜同一句话。')


# ============================================================
# 原始抓取(等价 curl)
# ============================================================
def http_get(url, max_len=2000):
    """抓一个 URL 的正文。html 会自动剥标签, 只留可读文字。"""
    url = str(url or '').strip()
    if not url:
        return 'url 为空'
    if not re.match(r'^https?://', url, re.I):
        url = 'https://' + url
    try:
        r = _get(url)
    except Exception as e:
        # 跟 web_search 同一套口径: 断网要明说"是这台机器没网了", 别让模型以为
        # 换个网址就能抓到。
        kind, why = _exc_kind(e)
        if kind in ('no_net', 'dns') and not _probe_net():
            _mark_net_down()
            return ('抓取失败: 这台电脑现在没有网络连接(断网, 或网卡/代理没起来)。'
                    f'换网址没用。(报错详情: {why})')
        return f'抓取失败({why}): {url}'
    if not r.encoding:
        r.encoding = r.apparent_encoding or 'utf-8'
    ctype = (r.headers.get('Content-Type') or '').lower()
    body = _plain(r.text) if 'html' in ctype else r.text.strip()
    n = max(200, min(int(max_len or 2000), 20000))
    tail = '' if len(body) <= n else f'\n…(已截断, 正文共 {len(body)} 字符)'
    return f'HTTP {r.status_code}  {r.url}\n{body[:n]}{tail}'


# ============================================================
# 工具清单(与 pc_tools.TOOL_SPECS 同格式)
# ============================================================
TOOL_SPECS = [
    {
        'type': 'function',
        'function': {
            'name': 'web_search',
            'description': '联网搜索最新信息(默认必应)。需要查资料、查现在的情况、'
                           '确认某个事实时用它, 不要凭记忆编。'
                           '中文的人名/地名/机构/俗语这类, 换 engine=baidu 或 '
                           'engine=both 再搜一次, 两边都有的条目更可信。'
                           '同一句话不要重复搜, 那不会有新结果; 要换就换一句明显不同的说法。'
                           '拿不到结果时返回里会写清是哪一类原因 —— 被反爬拦了(不代表'
                           '没有相关信息, 可以换引擎)、还是这台电脑没网了(那就别再搜, '
                           '直接如实告诉对方这边连不上网)。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': '搜索词'},
                    'count': {'type': 'integer', 'description': '返回条数, 默认 8'},
                    'engine': {'type': 'string',
                               'description': 'bing(默认) / baidu / both(双源对照)'},
                },
                'required': ['query'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'http_get',
            'description': '抓取一个网址的正文(相当于 curl), HTML 会自动转成纯文字。'
                           '搜索摘要不够时用它打开具体页面。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {'type': 'string', 'description': 'http/https 网址'},
                    'max_len': {'type': 'integer', 'description': '最多返回多少字符, 默认 2000'},
                },
                'required': ['url'],
            },
        },
    },
]

TOOL_FUNCS = {
    'web_search': web_search,
    'http_get': http_get,
}
