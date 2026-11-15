# -*- coding: utf-8 -*-
"""操作电脑的确定性专线: 「选方法 → 选参数 → 程序拼命令」。

为什么这么干 —— 实测(10-07 19:00)本机 4B 面对「复制这个文件到桌面 新名字为a.txt」,
第 1 轮生成的是:

    run_command {"command": "copy [path] a.txt"}

`[path]` 是它自己编的占位符 —— 它**知道要用命令行**, 但**不知道"这个文件"是谁**,
于是把模板原样交了上去, 命令行回"系统找不到指定的文件"; 第 2、3 轮又反复去
delete_file guess.py(跟"复制"毫无关系), 参数一模一样被去重挡下, 白刷 6 条进度。
18:58 那条「向我展示桌面static_thoughts.txt的内容」更糟: 路径都给了, 它只回了句
"我先看一下桌面上的static_thoughts.txt文件内容" —— 一个字节都没读。

结论: 让 4B「生成」一条能跑的命令, 它一定瞎编路径; 但让它从**几个真实存在的候选
里挑一个**(分类), 它稳得多。所以这里把活拆成两道选择题:

    ① 选方法: 从 CMD_SKILLS 里挑一个(只回编号)
    ② 选参数: 每个参数给一串候选(桌面/原话/历史里真实存在的路径), 让它挑

挑完由程序套模板拼命令、执行 —— 模型全程没有机会编路径。

管道的处理: find / count 两个 skill 本身就是管道; 都不合适时还有 pipe 兜底,
让它直接写一条完整命令(这条最弱, 但总比没有强)。

本模块只放**数据 + 纯函数**, 不碰模型、不发微信 —— 问模型那步在 agent._pc_line。
"""

import os
import re

HOME = os.path.expanduser('~')
DESKTOP = os.path.join(HOME, 'Desktop')
DOWNLOADS = os.path.join(HOME, 'Downloads')

_EXT = (r'py|pyw|txt|md|json|csv|log|ini|bat|cmd|ps1|exe|zip|rar|7z|'
        r'jpg|jpeg|png|gif|bmp|webp|mp3|mp4|avi|mov|'
        r'doc|docx|xls|xlsx|ppt|pptx|pdf')

_FILE_RE = re.compile(r'([A-Za-z0-9_\-\u4e00-\u9fa5]+\.(?:' + _EXT + r'))', re.I)
_FULL_RE = re.compile(r'([A-Za-z]:\\[^\s"\'，。！？、；:]+)')
# 「新建/建一个 xxx.py」= 造一个带扩展名的新文件(不是新建文件夹)。
_CREATE_FILE_RE = re.compile(
    r'(?:新建|创建|建个|建一个|写一个|生成)[^，。！？；]{0,24}?\.(?:' + _EXT + r')', re.I)

# 「libai的py」/「libai的python」= 名字后面直接跟着 py/python(没写扩展名那个点),
# 意思就是"要个 libai.py"。**必须也当成"造新文件"**:
# 实测 10-07 21:47 对方说「…libai的py类型 这个需要实现输出静夜思」, 整句没有 ".py",
# 于是 _CREATE_FILE_RE 没命中、意图门判它"是专线能干的活", 被专线接走只建了个文件夹,
# libai.py 一个字没写 —— 对方拿到的是"建好了：C:\Users\user\Desktop\poem",
# 要的那个文件根本不存在(答非所问)。agent._WANT_PY_RE 是同一个判据(那边用来推路径)。
# 名字必须紧贴 py 前面(中间只允许一个"的"和空白), 所以"用 python 写个脚本"不会误命中。
WANT_PY_RE = re.compile(
    r'([A-Za-z][A-Za-z0-9_\-]{1,31})\s*的?\s*(?:py|python)(?![A-Za-z])', re.I)


# ============================================================
# 意图门: 这条消息是不是"要操作电脑"
# ============================================================
# 动词 + 宾语**都**得命中才认。宁可漏(漏了就回到原来的决策循环, 能力不减),
# 也不能误伤 —— 把"删除是啥意思"这种提问也拖进专线, 就等于答非所问。
_PC_VERB = (r'看|查|读|显示|展示|列出|列一下|念一下|复制|拷贝|移动|挪|'
            r'改名|重命名|删除|删掉|删了|去掉|移除|新建|创建|建个|打开|运行|执行|'
            r'找|搜|统计|排序|多少|几行|多大|都有啥|都有什么|有什么')
_PC_NOUN = (r'桌面|文件夹|目录|文件|回收站|命令行|cmd|资源管理器|'
            r'\.(?:' + _EXT + r')\b')
_PC_BAD = r'别|不要|不用|不必|为什么|怎么|如何|能否|是否|已经|正在|有没有'


def looks_like_pc(text):
    """是不是"明确要求动这台电脑"的话。"""
    t = str(text or '')
    if not t or re.search(_PC_BAD, t):
        return False
    # 「新建/建一个 xxx.py」是**往一个新文件里写内容**, 不是专线能干的活 ——
    # CMD_SKILLS 里全是 type/copy/dir/del 这类"对着已有的东西动手"的, 没有"造文件"。
    # 实测 10-07 五类回归: 「在桌面 regress_5 文件夹里新建一个 hello_5.py，运行后打印
    # Hello 5」被专线接住, guess_skill 掉到最后那句 `if has_file: return 'read'`,
    # 于是它去 type 一个**还不存在**的 hello_5.py, 回"参数 src 不可用(不存在)",
    # 白等 54 秒, 最后交付一句"新建了一个 hello_5.py" —— 文件根本没建出来。
    # 造文件的活在决策循环里, 用 write_file。这里就得放出去。
    # 必须排在下面那个"动词+名词"前面: 这句同时命中 _PC_VERB(新建/运行)和 _PC_NOUN
    # (文件夹/文件), 排后面就永远轮不到。
    if _CREATE_FILE_RE.search(t) or WANT_PY_RE.search(t):
        return False
    if re.search(_PC_VERB, t) and re.search(_PC_NOUN, t, re.I):
        return True
    # 提到一个具体文件名 = 要对它做事, 就算动词不在 _PC_VERB 里 -------------------
    # 实测 10-07 19:21 「告诉我桌面上的static_thoughts.txt的内容」: 动词是"告诉我",
    # 而 _PC_VERB 只有 看/查/读/显示/展示/列出… 一个都没命中, 整条就被挡在专线外。
    # 可 guess_skill 明明是认的(命中"内容"->read, 再不济 has_file 也兜到 read)。
    # 结果它掉进决策循环, 4B 连着截了两次图发微信, 117 秒后把"云南是个省份"当成
    # 那个文件的内容发给了对方 —— 全是幻觉。
    # 别让动词表把名词表已经看见的文件名拦死: 带扩展名的文件名比任何动词都硬。
    return bool(_FILE_RE.search(t) or _FULL_RE.search(t))


_KW_RE = re.compile(
    r'(?:找|搜|查|含|包含|出现|有)(?:一下|一找|找)?[：:为]?\s*["“\']?'
    r'([^\s"“”\'，。！？、的地得]{1,12}?)["”\']?\s*'
    r'(?:这个字|这两个字|这个词|这几个字|这俩字|这两个词|这个|的字|字|词|的行|的地方|地方|在哪|哪儿)')


def guess_kw(text):
    """从原话里直接抠要搜索的关键词, 抠不到返回 ''。

    实测(10-07 19:3x)问模型"找什么词", 它把「风」翻成了「wind」, 于是白找一场。
    这种词就在原话里躺着, 正则比模型靠谱。
    """
    m = _KW_RE.search(str(text or ''))
    return m.group(1).strip() if m else ''


def guess_skill(text):
    """按句式先猜一个 skill; 猜不中返回 '' 交给模型做选择题。

    为什么要有这一步: 实测(10-07 19:2x)「桌面上都有什么文件」被 4B 选成了
    "在一个文件里找含某个词的行" —— 它根本分不清"文件夹里有什么"和"找词"。
    这种句式一看就明白的活, 压根不该赌它。

    注意顺序: 从具体到笼统, catch-all 放最后。
    """
    t = str(text or '')
    has_file = bool(_names(t) or _full_paths(t))
    # 删除放最前 —— 不写这条的话「删掉桌面上的 a.txt」会掉到最后的 catch-all 上被判成
    # read(它有文件名), 于是回你一整屏文件内容。走 del 才是走 delete_file(回收站)。
    if re.search(r'删除|删掉|删了|去掉|移除|清理掉', t):
        return 'del'
    if re.search(r'有什么|都有啥|都有什么|列一下|列出|里面有(?:什么|啥|哪些)', t):
        return 'list'
    if re.search(r'多少行|几行|行数', t):
        return 'count'
    if re.search(r'多大|占.*(?:空间|地方)|体积|大小', t):
        return 'size'
    if re.search(r'目录树|树形|结构', t):
        return 'tree'
    if re.search(r'改名|重命名', t):
        return 'move'
    if re.search(r'复制|拷贝', t):
        return 'copy'
    if re.search(r'挪|移动到|移到|搬到', t):
        return 'move'
    if re.search(r'新建.*(?:文件夹|目录)|建个.*(?:文件夹|目录)', t):
        return 'mkdir'
    # 「新建/建一个 xxx.py」是**写**文件。这张表里没有写文件的方法(全是 type/copy/dir/del
    # 这种对着已有东西动手的), 所以返回空交给决策循环用 write_file —— 千万别掉到下面
    # 那句 `if has_file: return 'read'` 去 type 一个还不存在的文件(实测白等 54 秒)。
    # 注意放在 mkdir 后面: 「新建一个文件夹」得先被上面那条接走。
    if re.search(r'新建|创建|建个|建一个|写一?个|生成', t):
        return ''
    if has_file and re.search(r'找|搜|含|出现|在哪儿|哪里', t):
        return 'find'
    if re.search(r'内容|展示|显示|读一下|念一下|写的什么', t):
        return 'read'
    if has_file:
        return 'read'
    if re.search(r'看|查|列|有', t):
        return 'list'
    return ''


# ============================================================
# skills 表 —— 就是"方法选项"
# ============================================================
# 每条:
#   id     程序内部用的名字
#   when   给模型看的一句话(选择题的选项文字, 必须短: 本机 4B 上下文只有 4096)
#   params [(参数名, 给模型看的问题, 候选类型)]
#          候选类型: file=已存在的文件 dir=已存在的文件夹 save=要写出去的目标 text=从原话里抓
#   build  命令模板, 程序用参数套进去
#   raw    True = 输出本身就是答案(读文件/列目录), 原样发回去; False = 只回一句"办好了"
#
# 刻意不做的: rd /s、del /f /s、format、diskpart —— 都命中 config.DANGEROUS_PATTERNS,
# run_command 会直接拒。删文件夹请用 delete_file(回收站), 不走这里。
CMD_SKILLS = [
    dict(id='list', when='看一个文件夹里都有什么',
         params=[('dir', '哪个文件夹', 'dir')],
         build='dir /b "{dir}"', raw=True),

    dict(id='read', when='看一个文件的内容',
         params=[('src', '哪个文件', 'file')],
         build='type "{src}"', raw=True),

    dict(id='find', when='在一个文件里找含某个词的行(管道 findstr)',
         params=[('src', '哪个文件', 'file'), ('kw', '找什么词', 'text')],
         build='type "{src}" | findstr /n /i "{kw}"', raw=True),

    dict(id='count', when='数一个文件有多少行',
         params=[('src', '哪个文件', 'file')],
         build='type "{src}" | find /c /v ""', raw=True),

    dict(id='copy', when='把一个文件复制到别处 / 复制成新名字',
         params=[('src', '源文件是哪个', 'file'), ('dst', '复制到哪儿(可带新名字)', 'save')],
         build='copy /y "{src}" "{dst}"'),

    dict(id='move', when='把一个文件挪到别处 / 改名',
         params=[('src', '源文件是哪个', 'file'), ('dst', '挪到哪儿(可带新名字)', 'save')],
         build='move /y "{src}" "{dst}"'),

    dict(id='del', when='删掉一个文件',
         params=[('src', '删哪个文件', 'file')],
         build='del /f /q "{src}"'),

    dict(id='mkdir', when='新建一个文件夹',
         params=[('dst', '在哪儿建、叫什么', 'save')],
         build='mkdir "{dst}"'),

    dict(id='tree', when='看一个文件夹的完整结构',
         params=[('dir', '哪个文件夹', 'dir')],
         build='tree /f "{dir}"', raw=True),

    dict(id='size', when='看一个文件夹里各项占多大(管道 sort)',
         params=[('dir', '哪个文件夹', 'dir')],
         build='dir "{dir}" | findstr /v par | sort', raw=True),

    dict(id='pipe', when='上面都不合适, 自己想一条带管道的完整命令',
         params=[('cmd', '完整命令(要带管道就写完整的 cmd 命令)', 'text')],
         build='{cmd}', raw=True),
]

_BY_ID = {s['id']: s for s in CMD_SKILLS}


def skill_by_id(sid):
    return _BY_ID.get(str(sid or '').strip())


# pipe 是"模型自己写命令"的逃生舱, 也是唯一能绕过上面所有 skill 的口子。
#
# config.DANGEROUS_PATTERNS 只拦 rd /s、del /f /s、format 这种"看着就狠"的, 但
# `del "x.txt"`、`copy /y a b`、`echo x > f` 一样能把东西弄没了, 而且都不可回滚。
# 所以这里按"只读"来放行 —— 要动磁盘上的东西, 请走上面那些带候选和闸的 skill。
_PIPE_BAD = re.compile(
    r'>|\|\||&&|&|'                       # 重定向/串联: echo x > f 一条就能盖掉文件
    r'\b(?:del|erase|rd|rmdir|ren|rename|move|copy|xcopy|robocopy|md|mkdir|'
    r'taskkill|reg|sc|net|wmic|bcdedit|diskpart|cipher|vssadmin|attrib|icacls|takeown|'
    r'start|call|shutdown)\b',
    re.I)


def pipe_ok(cmd):
    """pipe 命令能不能放行。返回 (ok, 原因)。"""
    c = str(cmd or '').strip()
    if not c:
        return False, '空命令'
    m = _PIPE_BAD.search(c)
    if m:
        return False, f'里面出现了「{m.group()}」'
    return True, ''


# ============================================================
# 候选生成 —— 让模型"挑", 不让它"编"
# ============================================================
def _ls(d):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


def _files_in(*dirs):
    out = []
    for d in dirs:
        for n in _ls(d):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                out.append(p)
    return out


def _dirs_in(*dirs):
    out = []
    for d in dirs:
        for n in _ls(d):
            p = os.path.join(d, n)
            if os.path.isdir(p):
                out.append(p)
    return out


# "改名为 a.txt" / "叫做 a.txt" / "复制到 xx" 这类词先切开, 再抽文件名 ——
# 不切的话正则贪婪地连前缀一起吃掉: 实测「新名字为a.txt」被整个抽成了一个假路径。
_SPLIT_RE = re.compile(r'名字为|命名为|改名为|重命名为|叫做|叫|为|成|：|:|＝|=|→|->|到')


def _names(text):
    """从一段话里抓出所有"带扩展名的文件名"(已按"改名为/叫做/到"切分)。"""
    out = []
    for seg in _SPLIT_RE.split(str(text or '')):
        out.extend(_FILE_RE.findall(seg))
    return out


def _full_paths(text):
    return [p for p in _FULL_RE.findall(str(text or ''))]


# "新建一个名为 poem 的文件夹" / "建个 libai 目录" —— 这里的名字是**新造的**,
# 磁盘上当然没有, 所以上面那套"只收真实存在的"闸会把它整个丢掉。
# 实测 10-07 21:47 微信真链路: 对方说「帮我在桌面创建一个名为poem的文件夹」,
# 候选里只剩"桌面"这种目录, 模型就挑了 C:\Users\user\Desktop 当 dst,
# 拼出的命令是 mkdir "C:\Users\user\Desktop" —— 文件夹名 poem 凭空消失,
# 命令当然失败, 整条任务跟着垮掉。名字必须单独抽出来, 并允许它不存在。
_NEWDIR_RE = re.compile(
    r'(?:新建|创建|建个|建一个|加个|生成|要个|要一个)\s*(?:一个|个)?\s*'
    r'(?:名为|叫|叫做|命名为)?\s*([^\s，。、！？,.!?\\/]{1,32}?)\s*(?:的)?\s*'
    r'(?:文件夹|目录|档案夹)')


def _new_dirs(text):
    """抓"要新建的文件夹名"(去重、去掉空壳词)。"""
    out = []
    for n in _NEWDIR_RE.findall(str(text or '')):
        n = n.strip().strip('"\'')
        if n and n not in ('文件夹', '目录', '一个', '个') and n not in out:
            out.append(n)
    return out


def _locate(name):
    """把一个光名字落到真实路径上: 桌面 -> 下载 -> 家目录 -> 尾巴上的 ASCII 片段。

    最后那一步专治"中文粘连": 正则里中文和英文字母同属一个字符类, 所以
    「向我展示桌面static_thoughts.txt」会被整个抽成一个名字(实测就是这样)。
    这类串整体当然不成立, 于是从右往左退到第一段纯 ASCII 再试一次, 就落到真文件上了。
    """
    for d in (DESKTOP, DOWNLOADS, HOME):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    for m in reversed(list(re.finditer(r'[A-Za-z0-9_\-.]+', str(name or '')))):
        for d in (DESKTOP, DOWNLOADS, HOME):
            p = os.path.join(d, m.group())
            if os.path.exists(p):
                return p
    return os.path.join(DESKTOP, name)


def candidates(kind, user_text='', history_text='', limit=9, sid=''):
    """给一个参数凑候选, **真实的排前面**。返回去重后的字符串列表。

    这是整条专线的命门: 只要候选里全是真路径, 模型就只能挑对的;
    它要真想编, 也得走到最后的"其他"分支(那边程序会自己校验存在性)。
    """
    user_text, history_text = str(user_text or ''), str(history_text or '')
    out = []
    noise = ('desktop.ini', 'thumbs.db', 'ntuser.dat')

    def add(v):
        v = str(v or '').strip().strip('"')
        if v and os.path.basename(v).lower() not in noise and v not in out:
            out.append(v)

    def add_exist(v):
        """只收**真实存在**的 —— 这是防"假路径"的关键闸。

        实测正则把「新名字为a.txt」「向我展示桌面static_thoughts.txt」都抽成过文件名,
        它们一律不存在, 卡在这就进不了候选单。exists 这一刀比调正则便宜也可靠得多。
        """
        p = str(v or '').strip().strip('"')
        if p and os.path.exists(p):
            add(p)

    if kind == 'text':
        return []

    # save 是"要写出去的目标", 允许不存在(复制成新名字、新建文件夹都靠它);
    # 其余类型必须是磁盘上真有的东西。
    keep = add if kind == 'save' else add_exist

    # 1) 原话里写得明明白白的完整路径 —— 用户亲手给的, 最可信
    for p in _full_paths(user_text):
        keep(p)

    # 2) 原话里提到的文件名
    for n in _names(user_text):
        keep(n if os.path.isabs(n) else _locate(n))

    # 3) 历史里最近提到的文件名(治"复制这个文件"里的"这个")
    for n in _names(history_text):
        add_exist(_locate(n))

    # 3.5) 专线 mkdir: "新建一个名为 X 的文件夹" 里的 X 是新造的, 磁盘上不存在,
    #      上面那些 add_exist 闸会把它整个丢掉。不补这一条, 候选里就只剩"桌面"
    #      这种目录, 模型于是挑了桌面当 dst, 文件夹名凭空消失 —— 实测 10-07 21:47
    #      就是这么拼出 mkdir "C:\Users\user\Desktop" 的。
    #      放在通用目录候选**前面**, 免得被 limit 截掉。
    if sid == 'mkdir':
        for n in _new_dirs(user_text):
            add(os.path.join(DESKTOP, n))

    if kind == 'dir':
        # 目录候选: 先给常见根目录, 再给真实子目录
        for d in (DESKTOP, DOWNLOADS, HOME):
            add_exist(d)
        for d in _dirs_in(DESKTOP, DOWNLOADS):
            add_exist(d)
    elif kind == 'save':
        # 目标候选: 真实存在的文件(覆盖"改名"场景) + 桌面/下载 的目录形式
        for f in _files_in(DESKTOP, DOWNLOADS):
            add_exist(f)
        # 光写了新名字没写目录时, 补上"桌面\新名字"这一支
        for n in _names(user_text):
            add(os.path.join(DESKTOP, n))
        for d in (DESKTOP, DOWNLOADS):
            add(d)
    else:  # file
        for f in _files_in(DESKTOP, DOWNLOADS):
            add_exist(f)

    return out[:limit]


def verify(kind, value):
    """参数到底能不能用。返回 (是否可用, 说明)。

    只有 file / dir 要求**现在就得存在**; save 是"要写出去的目标", 允许不存在
    (复制成新名字、新建文件夹都靠它)。空值一律不行。
    """
    v = str(value or '').strip().strip('"')
    if not v:
        return False, '空值'
    if kind in ('file', 'dir'):
        if not os.path.exists(v):
            return False, f'{v} 不存在'
        if kind == 'file' and os.path.isdir(v):
            return False, f'{v} 是文件夹不是文件'
        if kind == 'dir' and not os.path.isdir(v):
            return False, f'{v} 是文件不是文件夹'
    return True, ''


def build(sid, params):
    """套模板拼命令。参数不齐就返回空串(调用方据此交回决策循环)。"""
    sk = skill_by_id(sid)
    if not sk:
        return ''
    need = [p[0] for p in sk['params']]
    vals = {k: str(params.get(k) or '').strip().strip('"') for k in need}
    if any(not v for v in vals.values()):
        return ''
    if sid == 'pipe':
        # 兜底项: 模型自己写的完整命令。这里只挡最危险的几个词,
        # 真正的闸在 pc_tools.run_command 的 _guard(config.DANGEROUS_PATTERNS)。
        return vals['cmd']
    return sk['build'].format(**vals)


def menu():
    """选择题的选项文字(不带模板, 省 token)"""
    return [f"{i}. {s['when']}" for i, s in enumerate(CMD_SKILLS, 1)]


def by_number(num_text):
    """把模型回的编号换成 skill id"""
    m = re.search(r'\d+', str(num_text or ''))
    if not m:
        return ''
    i = int(m.group())
    if 1 <= i <= len(CMD_SKILLS):
        return CMD_SKILLS[i - 1]['id']
    return ''
