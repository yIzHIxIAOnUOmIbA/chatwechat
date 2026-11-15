# -*- coding: utf-8 -*-
"""imhere —— 微信「一对一桌面助手」配置

与参考实现 replyIsTooEasy 的关系: 仅作技术参考, 本目录是独立实现。
三处根本差异:
  1) 数据源: 只读账号 your_wechat_id 的本地库 (wxid_example_acct_0f40),
     旧账号 (wxid_example_target_7ea9) 的任何库/密钥都不引用。
  2) 会话: 只服务一个会话 (TARGET_USERNAME = 示例联系人), 独立上下文。
  3) 大脑: 不是人设代聊, 而是能调用电脑工具 (键鼠模拟 / Win+R / 执行命令) 的助手。

可编辑项都在同目录 config.json, 未提供的项自动回落下列默认值。
"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_JSON = os.path.join(BASE, 'config.json')


def _load_user_cfg():
    try:
        with open(CONFIG_JSON, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


USER_CFG = _load_user_cfg()

# ============================================================
# 1. 微信数据源 —— 账号 your_wechat_id (只读, 与旧账号完全隔离)
# ============================================================
WX_ACCOUNT = USER_CFG.get('wx_account', 'your_wechat_id')
WX_ROOT = USER_CFG.get(
    'wx_root',
    r'C:\Users\user\Documents\xwechat_files\wxid_example_acct_0f40')
MSG_DB = os.path.join(WX_ROOT, 'db_storage', 'message', 'message_0.db')
KEYS_JSON = os.path.join(BASE, 'wxkeys.json')      # 本账号专属密钥(随目录自包含)
CONTACT_DB = os.path.join(BASE, 'contact_dec.db')  # 解密后的通讯录
MSG_DEC = os.path.join(BASE, 'message0_live.db')   # 每轮解密的临时消息库

# ============================================================
# 2. 目标会话 —— 只服务这一个, 独立上下文
# ============================================================
TARGET_USERNAME = USER_CFG.get('target_username', 'wxid_example_target')
TARGET_NAME = USER_CFG.get('target_name', '示例联系人')   # 窗口识别候选词, 见下
# ctrl+F 的搜索词 —— 和 TARGET_NAME 分开, 因为这两件事的要求不一样:
#   TARGET_NAME 要"能认出目标窗口"(OCR 用), 必须是人能看见的那个备注;
#   搜索词要"能把目标会话捞到最前", 而微信的搜索框按昵称/备注/wxid 混匹配,
#   拿中文备注搜不一定命中。
# 用户实测(10-14): 搜 ' 比搜"示例联系人"稳, 于是固定成它。进错会话有后面两道
# 闸兜底 —— 发送前的 OCR 复核(config.SEND_VERIFY)认不出目标会话就放弃本轮。
SEARCH_WORD = USER_CFG.get('search_word', "'")
OCR_MARKER = USER_CFG.get('ocr_marker', '示例联系人')    # 窗口识别特征词
STATE = os.path.join(BASE, 'state.json')
# 控制台"重置首发记录"的信号文件(见 chains/chat/config.py 同名注释)。
RESET_FIRST_FLAG = os.path.join(BASE, 'reset_first.flag')
SESSIONS_DIR = os.path.join(BASE, 'sessions')

# ============================================================
# 3. 监听参数
# ============================================================
# 轮询间隔: 每轮只做 3 次 os.stat(实测 1.2ms), 所以它根本不影响响应速度 ——
# 真正决定"多久开始回复"的是下面的 QUIET_WINDOW。给 0.1 是白捡的(CPU 开销约等于零,
# 10Hz 下约 1.2% 单核), 但别指望从这里省出秒级时间。
# 更正(性能轮实测): 早先这里写"一次 decrypt 实测 1.1s"是错测 —— 那 1.1s 不是 AES,
# 是**另一个进程**占着 MSG_DEC 导致 os.replace 失败、又被静默重试 3 次的假象
# (3×0.3s 睡眠)。真实解密 152KB 只要 38ms, 见 wx_db.decrypt()。
# 发送前复核(进入会话后再 OCR 一次, 确认没发错会话/账号)。
# 这是"发错人"的最后一道闸, 但每发一条要多花 1~3s OCR。
# 本机双开微信 -> 必须 True; 只想单账号 + 要速度 -> False。
SEND_VERIFY = USER_CFG.get('send_verify', True)
# 复核判据的宽容度: OCR 在微信 4.x 上常缺笔画(实测把"文件传输助手"认成
# "三文件骺蝓助手"), 所以用"特征词字符覆盖率"而不是严格包含 —— 6 个字认对 4 个
# 也算命中。真换了会话时覆盖率会掉到 0, 照样拦得住。
# 0.6 -> 0.5: 实测同一张图上"示例联系人"最稳的识别结果是
# '0一一 只 小 - 吧 三文件传助手'(覆盖率 67%), 而"文件传输助手"这类别的会话是 0%,
# 中间没有样本; 阈值贴到 0.6 等于不给抖动留余量, 一旦少认对一个字就掉到 50% 被拦。
VERIFY_MATCH_RATIO = USER_CFG.get('verify_match_ratio', 0.5)
# 复核"测不出来"时的策略: 截屏拿不到内容 / OCR 文本为空, 这是截屏抖动,
# 不是"发错人"的证据。旧版把它和"确认发错"一样当成拦下的理由, 又不打日志,
# 于是 16:57 之后连着的 4 条回复被静默吞掉。True=放行(只打告警), False=拦。
VERIFY_UNKNOWN_AS_PASS = USER_CFG.get('verify_unknown_as_pass', True)
# 发送失败后最多补发几次(main.py 的待重发队列用), 超了就记 unsent.log 并告警。
SEND_RETRY_MAX = USER_CFG.get('send_retry_max', 3)

# 模型只回了一句过程话却没动手时, 用来推它一把。本机 4B 实测会这样:
# 问"帮我打开计算器", 它回"我先看一下。"就交付了, 程序一个没开。
PREAMBLE_NUDGE = USER_CFG.get('preamble_nudge',
                              '你刚才只说了要做，但什么都没做，对方还没拿到任何结果。'
                              '这件事如果确实需要操作电脑或联网，就现在调用工具，'
                              '不要只是说明；如果不需要，就直接给出最终答复，'
                              '不要再回"我先看一下"这类过程话。')

# 提醒过一次, 它还是只回过程话(什么都没做)时的兜底答复。
# 宁可说"没弄成", 也不能把"我先看一下"发出去让对方干等。
PREAMBLE_GIVEUP = USER_CFG.get('preamble_giveup',
                               '我这边没弄成 你再说一遍我试试')

# 模型把工具调用写成了正文里的一段 JSON(没走工具通道), 被程序逮住时推它一把。
# 实测 10-07 18:34 "删除桌面上的guess.py": 第2轮它吐的是
# {"function": "open_app", "arguments": {"name": "运行框"}} —— 旧正则不认这种写法,
# 这串 JSON 就被当成交付原样发给了对方, 活没干完, 后面也再没下文。
JSON_NUDGE = USER_CFG.get('json_nudge',
                          '你上一句给程序看的是一段工具调用（JSON 或 <tool_call> 这类 XML），'
                          '那不是给对方的话。要动手就用工具通道真的把它调出来；'
                          '不打算动手就用人话说清现在做到哪了、成没成。'
                          '别把工具调用打进正文。')

# 模型把"想写进文件的那行代码"当成正文交上来 —— 那也不是给对方的话, 推它说人话。
# 实测 10-07 22:58 真模型复现: 六轮没做成之后, 最后一轮交的正文就是 `print("静")`
# (它脑子里"输出静夜思"的那行代码), 旧代码认不出来, 原样发给了对方。
CODE_NUDGE = USER_CFG.get('code_nudge',
                          '你刚才交上来的是一行代码，那是要写进文件里的东西，'
                          '不是要发给对方的话。事情做到哪一步了、成没成，用人话一句'
                          '说清楚；要是还没动手，就先用工具把它做出来。')

# 模型把**程序回给它的控制文本**(工具报错/提示)当成要给对方的话, 原样转述出去。
# 实测 10-14: write_file 新加的"相对路径"闸门把提示回给它(真桌面是 ... 必须写完整
# 绝对路径), 它下一轮把这段转述给了对方, 任务却一个都没做。那些文本是写给模型看的,
# 对方看到只会更糊涂。判据见 agent._ECHO_TEXT_RE。
ECHO_NUDGE = USER_CFG.get('echo_nudge',
                          '你刚才那段是程序回给你的提示文本，不是要发给对方的话。'
                          '对方看不懂那些路径说明，也不关心。要么重新用工具把事做出来，'
                          '要么用一句人话说清现在做到哪一步、成没成。别复读提示。')
POLL_INTERVAL = USER_CFG.get('poll_interval', 0.05)
# 解密节流: 指纹一变就解密的话, 微信在活跃期是持续写库的(会话列表、已读回执、它自己的
# 状态), 于是几乎每轮都在解密 —— 一次解密 = 38ms(实测) + 重写 152KB 临时库 + 一次
# os.replace + 开关一次 sqlite 连接。按 10Hz 算就是每秒 1.5MB 的磁盘写和白开几十个连接,
# 纯属白烧。而解一次密读的是整个库, 早解晚解都不丢消息(水位 last_id 保证只读新的),
# 只是晚一点发现, 所以把窗口内的多次变化合并成一次解密即可。
# 取值必须 <= QUIET_WINDOW(1.2s): 否则"发现消息"本身会变成回复链上的瓶颈。
# (原来定义了却从没被引用过, 主循环是"指纹一变就解密"。)
# 注: 这条闸门最早的理由是"一次解密 1.1s 的同步开销" —— 那是错测(见上面的更正)。
# 38ms 也不该每轮都做, 但它省下来的是"白烧", 不是秒级延迟。
MIN_DECRYPT_INTERVAL = USER_CFG.get('min_decrypt_interval', 0.8)

# 对账: 与指纹无关的定时兜底, 不管指纹怎么抖, 每这么久一定完整读一次库。
# 指纹(size/mtime)只是我们对"微信写了库"的**推断** —— 一旦这个推断失效(微信换版本改了
# 文件布局、写入不再改动 size/mtime、或者写入走 mmap 回写), 指纹就永远不变, 于是从此
# 一条消息都读不出来, 而界面上不会有任何异常。这和"水位失效"是同一类故障(那次的教训见
# main.py 的 table_top 检查)。定期无条件读一次, 把"永久漏"降级成"最多晚这么久",
# 顺带每隔这么久在日志里留一行"探测链路还活着"的凭据 —— 静默失效才是最难查的。
RECONCILE_INTERVAL = USER_CFG.get('reconcile_interval', 120)
# 对方安静这么久才算"说完", 然后才交给大脑。这是纯等待: 从收到最后一条到开始
# 思考要等满它, 是回复链里第二大的一笔固定开销(实测 1.8s)。
# 降到 1.2s: 连发场景仍是"最后一条之后再等 1.2s", 合并逻辑不受影响。
QUIET_WINDOW = USER_CFG.get('quiet_window', 1.2)  # 对方安静这么久才算说完
FIRST_REPLY_MAX = USER_CFG.get('first_reply_max', 0)  # 启动首扫回补历史条数(0=只处理新消息)
MAX_HISTORY = USER_CFG.get('max_history', 6)      # 注入会话的最近历史条数
# 历史最多回溯多久(秒)。只按条数取会出事: 实测 23:43 那条回复照抄了 21:36 的跑偏回复
# ("从屏幕上看到 Agents 窗口…"), 因为两条间隔 2 小时仍被当成同一个连续话题喂进模型,
# 模型便把它当成"我刚才在做的事"继续复述。真实使用中对方隔半小时以上再开口就是新
# 话题, 旧上下文只会带偏。0 = 不限制(退回按条数取的老行为)。
HISTORY_MAX_AGE = int(USER_CFG.get('history_max_age', 1800) or 0)
MAX_REPLY_LEN = USER_CFG.get('max_reply_len', 300)

# ============================================================
# 4. 大模型 (ollama) —— 本机 GTX1650 4G / 8G 内存下的最优选
# ============================================================
OLLAMA_URL = USER_CFG.get('ollama_url', 'http://127.0.0.1:11434')
# qwen3:4b-nothink = qwen3:4b 的权重 + 一份"不给它思考余地"的模板, 本目录自制。
# 为什么必须换(本机 GTX1650 4G / 8G 内存实测):
#   显存只剩约 1G, 3.0G 的模型有 6~13% 的层掉在 CPU 上, 解码只有 8~10 tok/s;
#   原版 qwen3:4b 每轮先写 250~400 字思维链, 单轮 30~50s, 一条消息 50~120s。
#   换掉之后: 一句"你好" 37s -> 1.4s, 一次工具调用 2.6s, 工具调用照旧走原生 tool_calls。
# 关键点: 光换成 fast-fix(模板只是不发 <think>) 还不够 —— 模型自己会写 <think>,
#   于是"想不想"全凭运气, 同一个提示词有时 1s 有时 40s。
#   真正管用的是在模板末尾预填一个空的思考块 <think>\n\n</think>\n\n,
#   生成被迫从 </think> 之后开始, 想思考也没地方写了。
# 重建命令: ollama create qwen3:4b-nothink -f <Modelfile>
#   (Modelfile = FROM qwen3:4b-fast-fix + ollama show --modelfile qwen3:4b 的官方模板,
#    末尾的 <think> 换成空的 <think>\n\n</think>\n\n; 可用
#    ollama show --modelfile qwen3:4b-nothink 取回完整定义)
LLM_MODEL = USER_CFG.get('llm_model', 'qwen3:4b-nothink')
LLM_TIMEOUT = USER_CFG.get('llm_timeout', 120)
# 8192 会让更多层掉到 CPU(实测 87% 上显存, 4096 时是 94%), 反正现在没有思维链, 上下文够用
LLM_NUM_CTX = USER_CFG.get('llm_num_ctx', 4096)
# 强制全部层上 GPU。这是本轮最大的一处性能修复 —— 同一台机(GTX 1650 4G)、同一
# qwen3:4b-nothink、同一 ctx=4096, 只差这一个字段:
#   不给 num_gpu(交给 ollama 自己估算): 0.9 ~ 4.8 tok/s, **不稳定**
#   显式 num_gpu=999                    : 15.8 / 18.0 / 19.2 tok/s (3 次均值 17.7)
# 慢的那次不是抖动: 估算看的是"加载那一刻还剩多少显存", VRAM 被别的实例或桌面占着时
# 它会整模型退回 CPU —— 实测到的 0.9 tok/s 与 num_gpu=0(纯 CPU) 那一组一模一样, 而
# LLM_KEEP_ALIVE=-1 会让这个坏状态一直留着。外部表现就是"某天起每条消息要等两分钟",
# 重启 ollama 才好, 还找不到原因。写死 999 就没有这个自由。
# 999 = 全部层。2.7G 的模型 + 4096 KV 实测只占 2986/4096 MiB, 有 1.1G 余量, 不会挤爆。
LLM_NUM_GPU = USER_CFG.get('llm_num_gpu', 999)
LLM_KEEP_ALIVE = USER_CFG.get('llm_keep_alive', -1)  # 常驻显存, 避免每条消息冷启动
LLM_WARMUP = USER_CFG.get('llm_warmup', True)
# 思维链开关。新模型没给它思考的余地, 这里 False 即可。
# 留档: 原版 qwen3:4b 上 think=True 是唯一正确解法 ——
#   ollama 的官方 qwen3 模板在末尾无条件注入 <think>, 所以 think=False 时
#   提示词已经帮模型开好了思考, 思维链只能漏进正文(还会吃掉 num_predict 预算);
#   /no_think 软开关也被那行盖掉, 同样无效。
# 设 null 表示完全不传该字段(给不支持 think 的老版本 ollama 用)
LLM_THINK = USER_CFG.get('llm_think', False)
# 单轮最多生成多少 token。现在不写思维链了, 这点预算全是给正文的, 绰绰有余。
# (以前思维链和正文共用预算, 1536 用满就是 70s。)
LLM_NUM_PREDICT = USER_CFG.get('llm_num_predict', 1024)

# ============================================================
# 4c. 云端大模型 (智谱 GLM) —— 主路径, 本地 ollama 只做兜底
# ============================================================
# 为什么换: 本机 4B 的多步任务能力有硬上限。公平测试四组对照(文本协议 /
# 原生 tool_calls / 全工具集 / 加护栏)同一道题全是 0 分, 失败集中在"路径漂移、
# 内容漂移、同工具死循环、过早放弃"这几类, 与接口无关(证据见 imhere 目录
# _fair_*.txt)。而智谱清言的 glm-4.5-flash 免费且原生支持工具调用, 实测
# (10-14) 多轮回灌(assistant tool_calls + role=tool)格式兼容。
#
# API Key 从本机 config.json 读取(默认空字符串), 代码里不硬编码任何密钥:
#   智谱清言 / open.bigmodel.cn 开放平台 -> API keys, 形如 <id>.<secret>
CLOUD_ENABLE = USER_CFG.get('cloud_enable', True)
CLOUD_API_KEY = USER_CFG.get(
    'cloud_api_key', '')
CLOUD_BASE_URL = USER_CFG.get('cloud_base_url',
                              'https://open.bigmodel.cn/api/paas/v4')
# 免费额度内实测可用的: glm-4.5-air(主) / glm-4-flash(备)
# 10-26 实测: glm-4.5-flash 也打不动了, 返回 429 code=1113「余额不足或无可用资源包」;
# 同批探测里 glm-4.5-air、glm-4-flash 直接可调且原生支持 tool_calls。
# 别把 glm-4.6 / glm-4.5 / glm-4-air / glm-4-plus 写进来 —— 全都返回 1113(要充值)。
# 也不用 glm-4.1v-thinking-flash: 它能调通但只会"描述要调什么工具", 不会真的发起
# tool_calls, 长任务会变成一路写计划。
CLOUD_MODEL = USER_CFG.get('cloud_model', 'glm-4.5-air')
CLOUD_FALLBACK_MODEL = USER_CFG.get('cloud_fallback_model', 'glm-4-flash')
CLOUD_TIMEOUT = USER_CFG.get('cloud_timeout', 60)
# 云端连不上/报错/限流时, 自动退回本地 ollama(原 imhere 那条链路), 保证不断线
CLOUD_FALLBACK_LOCAL = USER_CFG.get('cloud_fallback_local', True)
# 云端温度。本地仍用 _options() 里的 0.4(4B 需要它发散一点才肯动手),
# 云端用 0.3: 工具调用要的是稳, 不是花样。
CLOUD_TEMPERATURE = USER_CFG.get('cloud_temperature', 0.3)
# 主模型标记, 只为日志里好区分这一轮是谁答的: 'cloud' / 'ollama'
LLM_PRIMARY = USER_CFG.get('llm_primary', 'cloud')

# ============================================================
# 4b. 快速通道 —— 不用动手的消息单轮直答
# ============================================================
# 一句话不带工具、提示词更短: 闲聊 1~2s, 而完整循环光工具定义就一千多个 token。
# 一旦这活必须动手或必须联网, 模型只回 FAST_NEED_TOOLS, 程序立刻转回完整循环 ——
# 所以它是"加速档"而不是"阉割版", 能力一点不减。
FAST_REPLY = USER_CFG.get('fast_reply', True)
FAST_NUM_PREDICT = USER_CFG.get('fast_num_predict', 320)
FAST_TIMEOUT = USER_CFG.get('fast_timeout', 45)
FAST_NEED_TOOLS = USER_CFG.get('fast_need_tools', '<need_tools>')
FAST_CONTRACT = USER_CFG.get('fast_contract', (
    "【快答模式】这条消息不需要操作电脑，也不要联网。"
    "直接给出要发给对方的最终答复，简短口语，一到两句。\n"
    "语气：像朋友聊天。对方说心情、吐槽、闲聊时先接住情绪（安慰、共情、顺着他的话聊），"
    "别冷冰冰、别说教。\n"
    "标点：不要用中文标点，断句用空格或换行，需要标点时用英文半角 , . ! ?\n"
    "别硬编：拿不准就说拿不准，别编事实、别编数据、别编出处；"
    "对方把成语写错了，先一句点出正确写法再答。\n"
    "不要写“我先看一下”这类过程话，不要写任何标记。\n"
    "只要出现下面任一情况，你就绝对不要自己编，只回 "
    f"{FAST_NEED_TOOLS} 这一个词（尖括号也原样带上），别的什么都不写：\n"
    "  1) 必须操作电脑（开关程序、点鼠标、看屏幕、执行命令、生成文件）；\n"
    "  2) 必须联网查最新信息；\n"
    "  3) 涉及具体事实（人名、时间、数字、作品、事件）而你没有十足把握 —— "
    "你是个小模型，记错不如不答。"
))
# 命中这些词 = 明摆着要动手, 直接跳过快速通道(省掉白试的一轮)。
# 宁可漏判: 漏了模型自己会在快速通道里喊 FAST_NEED_TOOLS 转回来。
FAST_TRIGGERS = list(USER_CFG.get('fast_triggers') or [
    '打开', '启动', '运行', '关闭', '关掉', '退出', '点击', '双击', '右键',
    '输入', '打字', '按下', '按键', '快捷键', '鼠标', '键盘',
    '截图', '截屏', '屏幕', '界面',
    '命令', 'cmd', 'powershell', '终端', '进程', '任务管理器', '资源管理',
    '文件', '文件夹', '目录', '保存', '另存', '安装', '卸载', '下载', '上传',
    # 生成文件也是动手: 实测 "在桌面创建一个冒泡排序的cpp脚本" 不含任何旧触发词,
    # 走了快速通道, 结果模型回了 76 字的计划书当答复, 文件没写。
    '创建', '新建', '生成', '建个', '写个', '写一个', '写一段',
    # 改/读已有文件同样是动手。词表原来只有"写"没有"改", 于是"把桌面 a.txt 第二行
    # 改一下"不含任何触发词, 走了快速通道 —— 而那里没有 read_file/edit_file, 模型
    # 只能凭记忆编一句"改好了"。加了这组词它才会进完整循环, 真去读、真去改。
    '改', '修改', '编辑', '替换', '换成', '删除', '删掉', '去掉', '加上', '追加',
    '读一下', '读给我', '读取', '看一下这个文件',
    # 10-07 18:58 漏的一组: 「向我展示桌面static_thoughts.txt的内容」整句不含任何旧
    # 触发词, 走了快速通道, 而那边没有 read_file, 它只回了句"我先看一下…"就交付了。
    # _hit_triggers 里新加的"提到文件名"判据能兜住带文件名的大多数, 这组词是给
    # "展示一下今天的热点"这类不带文件名的说法兜的。
    '展示', '显示', '看一下', '看看', '列出', '列一下', '念一下', '念给我', '打印',
    '音量', '静音', '播放', '暂停', '关机', '重启', '睡眠', '锁屏',
    '计算器', '记事本', '浏览器', '画图', '微信窗口', '粘贴',
    # 需要联网实时信息的一律别走快速通道 —— 模型凭记忆答这种题只会编。
    # 实测: "搜索一下:今天上海的天气" 它 1.5 秒回了"多云 气温在25℃左右"(纯编的,
    # 1.5 秒根本不可能联网); "搜索一下:白海豚" 也压根没搜。
    '搜', '查找', '查询', '查一下', '帮我查', '找一下', '帮我找',
    '天气', '新闻', '最新', '股价', '汇率',
    '路径', '在哪', 'where',
    # 知识类: 实测 4B 答成语全是编的 —— "井底之鱼"出处编成《庄子·八iale》、
    # "王公好龙"整段释义错, 走搜索反而准。
    '成语', '典故',
    # 转交给 CodeBuddy 是"动手"(找窗口 + 切前台 + 粘贴), 快速通道里没有这个工具,
    # 模型只会回一句"我这就去告诉 codebuddy" —— 对方在微信那头什么都没等到。
    'codebuddy', 'code buddy', '编程助手', '代码助手',
    ])

# ============================================================
# 5. 智能体(工具调用)参数
# ============================================================
AGENT_MAX_STEPS = USER_CFG.get('agent_max_steps', 30)     # 一条消息最多几轮工具调用
# 6 轮只够"写一个文件 + 跑一次"; Java 项目要写 N 个类、编译、按报错改、再编译,
# 6 轮必然在半路被截断(模型只能把想调的工具写成 XML 文本交付)。默认抬到 30,
# 兜底靠 AGENT_STALL_LIMIT: 真的原地打转时照样会被强制收尾。
AGENT_TOOL_TIMEOUT = USER_CFG.get('agent_tool_timeout', 30)
SHOT_DIR = os.path.join(BASE, 'shots')                    # 截图落盘目录
# ---- 转交 CodeBuddy 的收尾: 回车让它开工, 跑完把画面截图发回微信 ----
CB_SUBMIT = USER_CFG.get('cb_submit', True)               # 转交时替对方回车, 让它立刻开工
CB_SHOT_ON_DONE = USER_CFG.get('cb_shot_on_done', True)   # 转交后自动回一张结果截图
CB_SHOT_POLL = USER_CFG.get('cb_shot_poll', 3.0)          # 每隔几秒看一眼画面变没变
CB_SHOT_IDLE = USER_CFG.get('cb_shot_idle', 6.0)          # 画面连续稳定这么久 = 它跑完了
CB_SHOT_START = USER_CFG.get('cb_shot_start', 12.0)       # 先等这么久再开始判断(让它先动起来)
CB_SHOT_MAX_WAIT = USER_CFG.get('cb_shot_max_wait', 240)  # 最多等这么久(秒)就截一张收工
CB_SHOT_SETTLE = USER_CFG.get('cb_shot_settle', 2.0)      # 判稳后再等这么久截最终图
# CodeBuddy 一般摆在左边那块竖屏上。它的画面在后台时未必刷新, 抓图前要先在下面这个
# 固定点上按一下把它叫醒, 否则 PrintWindow 抓回来的可能是旧画面。
#
# 这两个点是"死"坐标, 不做任何界面元素识别(去认输入框控件得迁就各家 UI, 太脆)。
# 支持两种写法:
#   [x, y]             整块虚拟桌面的绝对坐标(和鼠标同口径, 左屏是负数)
#   {"rel": [dx, dy]}  相对窗口左上角的偏移 —— 窗口拖到哪块屏都跟得住, 推荐
# 设成 None / 空就关掉这个动作。窗口矩形由系统直接给, 不算识别控件。
_RAW_LEFT_CLICK = USER_CFG.get('cb_left_click', {'rel': [1042, 1703]})
CB_LEFT_CLICK = _RAW_LEFT_CLICK or None
CB_LEFT_CLICK_WAIT = USER_CFG.get('cb_left_click_wait', 0.6)   # 点完等这么久再抓图
# 往 CodeBuddy 送话(粘贴)之前, 先点一下它输入框的位置。只把窗口切到前台保证不了
# 键盘焦点落在输入框上 —— 不点这一下, ctrl+V 很可能贴不进输入框, 后面回车也白按。
# 写法同上(绝对坐标 或 {"rel":[dx,dy]})。
_RAW_INPUT_CLICK = USER_CFG.get('cb_input_click', {'rel': [576, 1808]})
CB_INPUT_CLICK = _RAW_INPUT_CLICK or None
CB_INPUT_CLICK_WAIT = USER_CFG.get('cb_input_click_wait', 0.4)   # 点完等这么久再粘贴
# 危险命令护栏: 命中即拒绝执行(助手是给自己用的, 但防手滑/防被对话诱导)。
# 这是"尽力而为"的最后一道本地防线, 不能替代"降权 / 沙箱"运行 —— 任何能拿到
# shell 的助手都应尽量在非管理员、受限账户下启动。
# 下面这份默认表偏严格(覆盖常见 LOLBin / 系统破坏 / 编码混淆), 可在 config.json
# 的 dangerous_patterns 里按需增删(程序启动时合并进此表)。
DANGEROUS_PATTERNS = list(USER_CFG.get('dangerous_patterns') or [
    # 磁盘 / 分区破坏
    'format', 'diskpart', 'defrag', 'vssadmin', 'mountvol', 'chkdsk /f',
    # 递归 / 强制删除
    'rd /s', 'rmdir /s', 'rmdir', 'del /f /s', 'del /q', 'rm -rf', 'rmdir /q',
    # 关机 / 重启
    'shutdown', 'restart-computer', 'stop-computer',
    # 注册表
    'reg delete', 'reg add', 'reg import', 'reg save', 'reg load',
    # 账户 / 权限 / 共享
    'net user', 'net localgroup', 'net group', 'net share', 'net session',
    'wmic useraccount', 'takeown', 'icacls', 'cacls',
    # 下载执行类 LOLBin(最容易被网页 / 文件投毒诱导)
    'certutil', 'bitsadmin', 'mshta', 'regsvr32', 'rundll32', 'wscript',
    'cscript', 'schtasks', 'at.exe', 'sc create', 'sc config', 'sc delete',
    'net start', 'net stop', 'taskkill /f',
    # 编码混淆 / 远端拉取(绕过检测常用)
    'powershell -e', 'powershell -enc', 'powershell -encodedcommand',
    'powershell -nop -w', 'powershell -w hidden', 'cmd /c', 'cmd /k',
    'iex ', 'invoke-expression', 'downloadstring', 'downloadfile',
    'bypass -e', ' -enc ',
    # 系统 / 安全
    'bcdedit', 'cipher /w', 'sfc /scannow', 'dism', 'wevtutil', 'netsh',
    'disable-realtimescanner', 'set-mppreference',
])
# write_file 的落点黑名单: 系统目录一律拒绝(助手是自己人用, 但防手滑/防被对话诱导)
WRITE_DENY = [p.rstrip('\\').lower() for p in (USER_CFG.get('write_deny') or [
    'C:\\Windows', 'C:\\Program Files', 'C:\\Program Files (x86)',
    'C:\\ProgramData', 'C:\\$Recycle.Bin', 'C:\\System Volume Information',
])]

# ============================================================
# 6. 微信窗口识别
# ============================================================
# 主窗口标题识别词(新版 4.x 标题可能是账号昵称, 实测本机为 '微信')
WECHAT_TITLE_MARKERS = list(USER_CFG.get('wechat_title_markers')
                            or ['微信', TARGET_NAME])
WECHAT_MAIN_CLASSES = ['WeChatMainWndForPC', 'WeChatMainWindowForPC',
                       'WeChatLoginWndForPC']
WECHAT_QT_CLASSES = list(USER_CFG.get('wechat_qt_classes') or ['Qt51514QWindowIcon'])
WECHAT_KNOWN_CLASSES = WECHAT_MAIN_CLASSES + WECHAT_QT_CLASSES
WECHAT_EXCLUDE_CLASSES = [
    'WeChatCallWndForPC', 'WeChatVideoWndForPC', 'WeChatVoiceWndForPC',
    'WeChatVideoVoiceCallWndForPC', 'WeChatAudioCallWndForPC',
    'WeChatMiniProgramWndForPC', 'WeChatFileManagerWndForPC',
    'WeChatPreviewWndForPC', 'WeChatMsgVideoWndForPC',
    'ConsoleWindowClass', 'CASCADIA_HOSTING_WINDOW_CLASS',
    'Windows.UI.Core.CoreWindow',
]
# OCR 置信特征词: 只有 OCR 在左侧聊天列表里认出它, 才认定这是目标微信窗口。
# 本机同时登录了两个账号, 两个窗口类名/标题都一样, 唯一可靠区分点就是
# 目标账号的聊天列表里常驻着这个联系人。
WECHAT_MAIN_MARKERS = list(USER_CFG.get('wechat_main_markers') or [OCR_MARKER])
WECHAT_EXE = USER_CFG.get('wechat_exe', 'Weixin.exe')
# 双开时该用哪个账号的主窗口(0 = 自动判定; 判不出来链路直接放弃, 绝不猜)。
# 现在默认走**进程账号指纹**(见 core/acct.py): 该窗口所属进程打开着哪个
# xwechat_files\wxid_xxx 目录, 与标题/尺寸/头像全都无关, 微信重启换了句柄也照样命中。
# 下面的 hwnd 只在指纹判不出时兜底(0 = 不写)。
WECHAT_ACCOUNT_HWND = int(USER_CFG.get('wechat_account_hwnd', 0) or 0)
# 头像模板(旧路径, 已被进程指纹取代): 本机两个号把昵称/头像设成了同一张图, 实测
# 两个窗口同时命中(1.0000 / 0.9991), 没有区分力, 已降级为交叉验证用。
WECHAT_ACCOUNT_TMPL = str(USER_CFG.get('wechat_account_tmpl', '') or '')

# ============================================================
# 6b. 程序别名 —— open_app 的翻译表
# ============================================================
# 模型习惯说中文名("打开记事本"), 但 Windows 的 start / 运行框只认程序名:
# 实测把"记事本"填进运行框回车 = Windows 找不到该程序 —— 这就是"说打开了其实没打开"的真因。
# open_app 先查这张表, 查不到就把原名原样交给 start(所以漏了也还有救, 只是可能失败)。
# 想加自己的程序: config.json 里写 "app_aliases": {"我的工具": "D:\\tools\\x.exe"}
APP_ALIASES = {
    '记事本': 'notepad', 'notepad': 'notepad',
    '计算器': 'calc', 'calc': 'calc', 'calculator': 'calc',
    '画图': 'mspaint', 'mspaint': 'mspaint', 'paint': 'mspaint',
    '命令提示符': 'cmd', 'cmd': 'cmd', 'powershell': 'powershell',
    '任务管理器': 'taskmgr', 'taskmgr': 'taskmgr',
    '资源管理器': 'explorer', '文件管理器': 'explorer', 'explorer': 'explorer',
    '控制面板': 'control', '注册表': 'regedit', 'regedit': 'regedit',
    '浏览器': 'msedge', 'edge': 'msedge', '微软浏览器': 'msedge',
    'chrome': 'chrome', '谷歌浏览器': 'chrome',
    '微信': WECHAT_EXE,
}
APP_ALIASES.update(USER_CFG.get('app_aliases') or {})

# ============================================================
# 7. 发送
# ============================================================
AI_PREFIX = USER_CFG.get('ai_prefix', '[AI] ')
# 项目目录: agent 产出的文件统一放这里, 不再直接堆在桌面根目录。
# 用户 2026-11-11 指定"给定桌面子文件夹为项目文件夹"。
WORK_DIR = USER_CFG.get('work_dir') or os.path.join(
    os.path.join(os.path.expanduser('~'), 'Desktop'), 'wxduo_work')
try:
    os.makedirs(WORK_DIR, exist_ok=True)
except Exception:                                              # noqa: BLE001
    pass
# 对端"机器人发言"的前缀。
# 2026-11-15 改: 小号必须能看见**大号**发来的话(大号默认前缀 [AI生成], 首句是
# [自动回复]) —— 旧表把这两个也当机器人忽略, 结果大号发什么都进不来, 小号永远不回。
# 只保留小号自己回显的前缀, 避免自问自答。
BOT_PREFIXES = tuple(
    str(x).strip() for x in (USER_CFG.get('bot_prefixes')
                             or ('[AI]', '[AI回复]'))
    if str(x).strip()
) or ('[AI]', '[AI回复]')
SEND_DELAY = 1.0

# 静默发送: 填目标会话的头像模板 key(assets/tmpl/<key>_avatar.png), 空=不启用。
# 启用后 send_to 会先用模板反查是哪个微信窗口(双开时标题/进程都分不出来, 只能
# 靠列表头像), 再点头像切会话、投字符发消息, 全程不激活微信、不碰剪贴板。
# 代价: 绕过了 OCR 复核这道"发错账号"的闸门, 所以只在模板命中时才走; 没命中就
# 自动回退到原来的激活 + 剪贴板 + OCR 复核流程, 行为与不配置时完全一致。
# 模板用 core/win.py 的 capture_avatar(hwnd, key, y_center) 现抓。
QUIET_TMPL = USER_CFG.get('quiet_tmpl', '')

# ============================================================
# 8. 助手系统提示词(不套人设, 工具型助手) + 每轮决策标记
# ============================================================
# 每轮决策标记: 让模型自己决定「继续」还是「交付」, 程序只做解析和护栏。
#   CONTINUE_MARK -> 这轮不算完, 本轮内容当作下一轮的提示词继续
#   FINISH_MARK   -> 可以交付, 标记以上的正文才是发给对方的话
# 铁律: 没写标记一律按「交付」处理。默认「继续」在本机 4B 模型上必然死循环。
CONTINUE_MARK = USER_CFG.get('continue_mark', '<continue>')
FINISH_MARK = USER_CFG.get('finish_mark', '<finish>')
# 护栏(不交给模型): 连续这么多轮「没有新工具结果 + 思考与上轮高度相似」就强制交付
AGENT_STALL_LIMIT = USER_CFG.get('agent_stall_limit', 2)

SYSTEM_PROMPT = USER_CFG.get('system_prompt', (
    "你是运行在这台 Windows 电脑上的桌面助手，通过微信和「示例联系人」一对一对话。"
    "你只服务这一个会话，回复对象永远是「示例联系人」。\n"
    "你拥有真正操作这台电脑的能力（工具）：执行命令、打开程序、模拟按键、"
    "点击鼠标、输入文字、截屏、识别屏幕文字，还能联网搜索。\n"
    "工作方式：\n"
    "1) 对方让你做事时，先调用工具去做，拿到真实结果后再回答；"
    "不要没做就说“已完成”。\n"
    "2) 一步做不成就分几步做，每步看结果再决定下一步；必要时用截图/OCR 确认界面状态。"
    "要生成代码、脚本、文档这类文件时，用 write_file 一次把整个文件写完，"
    "再用 run_command 跑一下验证它真能用；不要用 run_command 拼 echo/here-string 写文件。\n"
    "3) 只能用工具实际能做的事，做不到就直说做不到。\n"
    "4) 回复要短、口语、直接，像微信聊天。只说是给对方的正文："
    "不要输出工具名、参数、JSON、XML、代码块、执行日志或“我正在调用…”这类过程说明。"
    "工具调用只能走工具通道，正文里绝不要出现 <tool_call> <arg_key> <arg_value> "
    "这类字样 —— 写在正文里既不会被执行，还会原样发给对方。\n"
    "5) 对方只是闲聊/问问题时，不用调用工具，正常回答即可。\n"
    "6) 不确定的不要编；需要外部信息或最新情况时，先联网搜一下（web_search）再回答。\n"
    "7) 每一轮的回复，最后一行必须单独写一个收尾标记，用来说明这轮算不算完：\n"
    f"   · 还要继续（还想再想一步、还想再看一眼结果）——最后一行写 {CONTINUE_MARK}。"
    "这个标记不会发给对方，程序会把你这轮写的内容原样交还给你，让你接着往下想；\n"
    f"   · 已经可以回话了——最后一行写 {FINISH_MARK}，标记上面的内容就是发给对方的话。\n"
    "   标记必须写、且只能出现在最后一行；正文里不要出现标记字样。\n"
    "8) 需要动手时，进度话和工具调用必须在同一次回答里一起给出："
    "正文写一句给对方的进度话（如“我先看一下”），同时给出工具调用。"
    "只写进度话、不给工具调用，等于什么都没做，这是最严重的错误。\n"
    "9) 最后交付的话必须是“已经发生的结果”，不能是“打算做”的预告："
    "只报这一轮工具真正做出来的事，别报你打算做的事，也别把工具名说给对方听"
    "（那等于还没做，而且对方也不关心）。\n"
    "10) 自检：开口前先问自己一句“这件事我真的做了吗？”"
    "没做就去调用工具，做了就说结果。\n"
    "11) 标点：发给对方的话里不要出现中文标点 —— 逗号、句号、顿号、问号、叹号、"
    "分号、冒号、引号、书名号、省略号这些一个都不要。断句只用空格或换行；"
    "确实需要标点时用英文半角 , . ! ?\n"
    "12) 语气：对方是朋友，不是你的上级，你回的是聊天而不是工作汇报。"
    "对方说心情、吐槽、闲聊时，先接住情绪（安慰、共情、顺着他的话聊），再谈事情；"
    "可以关心、可以开玩笑，但别浮夸、别客套、别说教。\n"
    "13) 别硬编：拿不准的事就说拿不准。对方把成语、人名、地名写错了，"
    "先用一句指出正确写法再回答，绝对不要顺着错的一路编下去 —— 编错了比说不知道更糟。\n"
    "14) 电脑里的事分两种：一种是专属工具能干的（写作文件 write_file、看文件 read_file、"
    "改文件 edit_file、开程序 open_app、上网 web_search），另一种没有专属工具，"
    "就得用 run_command 打命令。这台机器的 run_command 走的是 cmd.exe，命令按 cmd 写：\n"
    "   列目录 dir   、看内容 type 文件名   、复制 copy 源 目标   、改名/移动 move 源 目标\n"
    "   删除 del 文件名   、建目录 mkdir 目录名   、路径带空格或中文一律加双引号\n"
    "   例：copy \"源文件的全路径\" \"目标的全路径\"（两个都写全，目标也要带文件名）\n"
    "   对方说“复制这个文件”“改个名”“挪到别处”时，就是一条 run_command 的事 —— "
    "没有 write_file 那种专属工具也得动手，绝不许卡住不干。\n"
    "   而“删了它”“把桌面那个 guess.py 删掉”有专属工具：delete_file(path=完整路径)，"
    "一步删完（进回收站，反悔能还原）。删文件**绝对不要**去开运行框（win_run）—— "
    "运行框是打开程序用的，拿它删文件只会弹错误框、文件一个都不会少"
    "（实测连试 6 轮全落空，还白等了好几分钟）。\n"
    "   bash 那套在这儿一句都不认：ls、cp、mv、rm、cat、grep、mkdir -p、&& 全都别用。\n"
    "15) 非要用 PowerShell 时必须喊全名：powershell -NoProfile -Command \"<命令>\"。"
    "基础对应关系：dir↔Get-ChildItem、type↔Get-Content、copy↔Copy-Item、"
    "move↔Move-Item、del↔Remove-Item、mkdir↔New-Item -ItemType Directory。"
    "但 cmd 能干的一律用 cmd —— run_command 只按 GBK 解输出，pwsh 的中文回显会乱码。\n"
    "16) 生成文件只有一条正路：write_file 一次把完整内容写到磁盘，路径写全"
    "（桌面就是 C:\\Users\\<用户名>\\Desktop\\文件名）。绝对不要开记事本再用 type_text "
    "往里敲 —— 那不叫写文件，磁盘上什么都不会有，而且那段文本会粘进微信的聊天输入框，"
    "跟着下一条消息一起发给对方（实测踩过：261 个字符的代码就这么被发出去了一条）。\n"
    "17) 写完 .py 必须用 run_py 真跑一遍，跑通才准说完成。语法对不对 write_file 自己"
    "就会编译一遍，逻辑对不对只有跑一遍才知道 —— 实测把猜数字的大小提示写反了"
    "（猜的数比答案小，该说“太小了”，它输出了“太大了”），整段逻辑全反，"
    "而它一次都没跑过，说句“我先看看有没有写错的地方”就交差了。"
    "带 input() 的程序用 stdin_text 按顺序喂输入，再给 expect 下断言，"
    "例如 run_py(path=\"…\\guess.py\", stdin_text=\"1\\n\", expect=\"太小了\")。"
    "自检失败就 edit_file 改掉错的那处再跑，直到通过；通过后把实测输出一起交付。\n"
    "18) 对方说\"让 codebuddy 干X\"\"问一下 codebuddy Y\"时，那是要你把这句原话转交"
    "给电脑上开着的 CodeBuddy，不是让自己去做，也不是原样把话发回微信。做法只有一步："
    "paste_to_app(app=\"codebuddy\", text=\"要转交的原话\", submit=true)—— 它会自己按进程名"
    "找到 CodeBuddy 窗口、切到前台、把话粘进输入框并回车，让 CodeBuddy 立刻开工。"
    "CodeBuddy 的窗口标题是 \"Agents\" 之类，拿 codebuddy 去匹配标题找不到，"
    "所以别用 focus_window 手动找，交给 paste_to_app。"
    "不做这个转移、只回一句\"我这就去告诉 codebuddy\"是最糟的：对方在微信那头什么都"
    "看不到。（在微信窗口里打字是禁止的，往聊天框粘贴等于直接发给对方，所以只能用"
    "这个工具把内容送进目标窗口。）\n"
    "19) 对方要\"看看 codebuddy 现在怎么样了\"\"截个图\"\"把画面发我\"时，用 "
    "shot_to_wechat(app=\"codebuddy\")—— 它一次完成\"截 CodeBuddy 窗口 + 发到微信\"。"
    "不要自己 screenshot 完却不知道图怎么发出去；也不要拿全屏截图敷衍：CodeBuddy 常常"
    "被微信盖着，全屏图抓到的全是微信。\n"
    "20) 写 Java 项目（尤其是带窗口的桌面程序）按这套走，一步都别跳：\n"
    "    ① 先定一个项目目录，例如 C:\\Users\\<用户名>\\Desktop\\bookmgr，所有 .java "
    "都放在这个目录里。文件名必须和 public 类名完全一致（Book.java 里才能有 "
    "public class Book），大小写也要一致 —— 这条错一次整包就编译不过。\n"
    "    ② 用 write_file 一个类一个文件地写，一次写完整的文件。不要用 run_command 拼 "
    "echo 写代码，也不要开记事本敲。注释和界面上的中文可以直接写，落盘一律是 UTF-8。\n"
    "    ③ 这台机器只有 JDK 17，没有 maven、没有 gradle，也没有任何第三方 jar"
    "（连数据库驱动都没有）。所以：数据用内存集合或自己写文件读写，界面只用 "
    "javax.swing，不许 import 任何不属于 java.* / javax.* 的包。\n"
    "    ④ 所有类写完后，用 run_java(path=项目目录或任一 .java, gui=true) 一次性全量编译"
    "并运行。绝对不要一个类一个 javac —— 共享类在两次编译之间会报找不到符号，"
    "然后你会去改本来就对的代码。\n"
    "    ⑤ 编译报错就按报错里的文件名和行号用 edit_file 改那几行，改完再 run_java，"
    "一直循环到编译通过。这不是可选项：Java 几乎不可能一次编译通过。\n"
    "    ⑥ 跑起来后用 list_files 核对一遍文件数量和文件名，再交付。\n"
    "    ⑦ 交付只说是哪几个类、什么界面、怎么点，不要把代码贴进正文。\n"
    "21) 聊天就只是聊天：屏幕类工具（screenshot、ocr_screen、focus_window、"
    "list_windows）只在对方明确要你看屏幕、要截图、或明确让你在电脑上做事时才用。"
    "对方只说“确认一下”“看一下”“怎么样了”，却没给出具体对象（哪件事、哪个文件、"
    "哪个窗口）时，绝对不要拿屏幕上的东西当答案 —— 那是拿别人的窗口给自己找话编。"
    "这时正确做法是用一句话问清楚他指的是什么。"
    "实测踩过：对方发“E05 最后确认一下”，它去截屏、翻 CodeBuddy 的 Agents 窗口，"
    "最后交付“从屏幕上看到 Agents 窗口里有关于 E05 的信息 包括一些任务管理器…”，"
    "而 E05 在聊天记录里根本没有出处，整段话都是从屏幕上借来的。\n"
    "22) 有些活是有现成套路的（修 Python 报错、写前端页面、做安全审计、润色文案…）。"
    "拿不准步骤时先调 list_skills 看看本机有没有对应的技能，再用 use_skill(name) 把"
    "它的流程读进来，然后照着那套流程做 —— 不要自己现编步骤。读进来的技能正文是给你"
    "看的内部资料，绝不要复述给对方、也不要贴进正文。\n"
    "23) 一件事要读好几个文件、或者要搜好几轮才能查清时，可以派子代理："
    "subagent(task=\"要它独立查清的那一件事\")，它自己跑几轮，只交回一段结论。"
    "一步就能做完的事别派 —— 那只是白等一轮。子代理只能查不能动手。"
))

# 循环里的两句脚手架(不进最终历史, 用完即撤):
#  * 模型写了 CONTINUE_MARK, 用这句把它推回下一轮
#  * 步数用尽/卡住, 用这句逼它只能交付
CONTINUE_NUDGE = USER_CFG.get('continue_nudge',
                              '（继续。接着往下想，想清楚了就把最终要发给对方的话写在最后，'
                              '并在最后一行标上标记。）')
DELIVER_NUDGE = USER_CFG.get('deliver_nudge',
                             '（时间到了，现在只做一件事：把最终要发给对方的话写出来，'
                             '并在最后一行标上结束标记。）')
# 写过 .py 却没跑过就直接交付 -> agent._loop 用这句把它推回去(只推一次)。
# 为什么要有这句: 提示词里"写完必须跑一遍"是软约束, 4B 模型经常整条跳过; 这句是在
# 它正要交付的那一刻插进去的, 比提示词靠前得多。{path} 由调用方填真实路径。
SELFCHECK_NUDGE = USER_CFG.get('selfcheck_nudge',
                               '（你刚写的 {path} 还没真跑过一遍，现在不许说完成。'
                               '先用 run_py 跑它：带 input() 的程序把每行输入按顺序写进 '
                               'stdin_text，再给 expect 一句"输出里必须出现的话"当断言。'
                               '自检通过就接着交付，并在话里带上实测输出；'
                               '自检失败就用 edit_file 改掉出错的那处再跑。）')
# 写过 .java 却一次没编译过就想交付 -> 同一道门禁, 换一句 Java 版的话。
# Java 和 Python 的区别: 单文件根本没有"语法自检"这回事(类之间互相引用), 只能全量
# javac。所以这里不许它一个个编译, 只认 run_java。
JAVA_SELFCHECK_NUDGE = USER_CFG.get('java_selfcheck_nudge',
                                    '（你刚写的 {path} 还一次都没编译过，现在不许说完成。'
                                    'Java 必须全量编译：调用 run_java(path=项目目录, '
                                    'gui=true) 一次性编译并运行所有类。'
                                    '报错就按文件名+行号用 edit_file 改，再 run_java，'
                                    '直到编译通过、程序真的跑起来；'
                                    '然后把实测结果一起交付。）')
# 对方明说要产出一份文件, 但一直没调写文件的工具 -> agent._loop 用这句点名推它(只推一次)。
# 为什么要有这句: 实测 10-07 18:07 "在桌面写一个加法计算器", 4B 第1轮吐了 521 字
# "1. 打开记事本: 使用 open_app…" —— 它把工具名当说明书写, 六轮下来一个文件没建,
# 最后还报"已经写好了"。提示词管不住这个, 得在它要交付的那一刻点名插一句。
# 注意这是**格式模板**, 必须 .format(path=...) 用 —— 里面的 {path} 会换成对方真正要的
# 那个路径。原先这里写死了"（比如 C:\Users\user\Desktop\guess.py）", 实测五类回归:
# 让它建 桌面\regress_5\hello_5.py, 4B 把示例路径原样抄走, 真去建了个 桌面\guess.py,
# 内容还是把工具清单抄了一遍 —— 然后交付"在 regress_5 文件夹里新建了一个 hello_5.py"。
# 提示里绝不能再出现一个会被照抄的路径。
WRITE_NUDGE = USER_CFG.get('write_nudge',
                           '（对方要的是一份能用的文件，不是做法说明。现在直接调用 '
                           'write_file，path 就写下面这个（这是对方要的文件，'
                           '别改名、别换目录、别写别的文件）：\n{path}\n'
                           '要建几个文件就调几次 write_file，一次只建一个，'
                           '别用 run_command 拼 shell 去凑。content 里放完整内容；'
                           '建完用 run_py 真跑一遍自检。'
                           '不要输出"第一步打开记事本"这类步骤，也不要念工具名。）')

# 交付里报了个具体结果(版本号之类), 但这一轮跑命令的记录里根本没有这个数 -> 用这句推它
# 改用 run_command 真跑一遍(只推一次)。
# 为什么要有这句: 实测五类回归「运行 python --version，把版本号告诉我」, 4B 选了
# win_run(把字丢进 Win+R 运行框), 那条路**拿不到任何输出**, 它却交付"Python 版本是
# 3.9.7"(本机真版本 3.13.7)。
RESULT_NUDGE = USER_CFG.get('result_nudge',
                            '（你报的这个结果在执行记录里找不到，是你自己想的。'
                            '别用 win_run —— Win+R 运行框不会把输出给我，我拿不到一个字。'
                            '要拿命令的输出必须用 run_command：command 写完整命令，'
                            '然后照它真实返回的内容回答。拿不到就如实说拿不到。）')

QUOTE_NUDGE = USER_CFG.get('quote_nudge',
                           '（你引号里报的那段结果，在这一轮真跑出来的输出里找不到，'
                           '是你自己想的。照 run_py / run_command 真实返回的原文回答 ——'
                           '一字不差地抄真实输出；跑出来的是别的东西、没对上对方要的那段，'
                           '就如实说，别把他想象中的结果当成真结果报给他。）')

# 对方要的就是某条命令的输出, 可交付里连那条命令都没提 -> 用这句把它拉回来(只推一次)。
# 同样 .format(cmd=...) 用, {cmd} 换成对方要的那条命令。
# 为什么要有这句: 实测五类回归「运行 python --version，把版本号告诉我」, 第1轮就拿到了
# 3.13.7, 它却接着 sleep 两次、跑去搜"codebuddy 的功能和使用方法"、最后交付一整段
# CodeBuddy 介绍 —— 对方问的那个版本号一个字没提。
# 一轮下来既没正文也没有工具调用 —— 这轮废了, 用这句让它重来一次(只推一次)。
# 为什么要有这句: 实测五类回归 长任务 第2轮 40.4 秒 / 0 字正文 / 0 个工具, 老代码把
# 这个空轮当成"交付"处理, 撞上写文件门禁被拒后就再没下文了 —— 一整轮白烧。
EMPTY_NUDGE = USER_CFG.get('empty_nudge',
                           '（你这一轮什么都没产出，对方要的东西还没拿到。'
                           '现在就用一句话、或者一次工具调用接着做，别再沉默。）')

CMD_NUDGE = USER_CFG.get('cmd_nudge',
                         '（对方问的就是 `{cmd}` 这条命令的输出，你没答。'
                         '把你已经跑出来的真实输出直接报给他，一句话说完就结束，'
                         '不要再 sleep、不要再搜网页、不用再调别的工具。'
                         '要是还没跑成，就直说没跑成。）')

# 交付里在念"某个文件的内容", 但这一轮根本没读过那个文件 -> agent._loop 用这句推它(只推一次)。
# 为什么要有这句: 实测 10-07 19:38 对方只说了句"什么", 4B 六轮调了 5 次 web_search,
# 最后交付"static_thoughts.txt 里的内容是 云南是中国的一个省份" —— 文件一个字没读。
# 它拿"网上搜到的"当"这个文件里写的"了, 而 web_search 成功又让 _worked() 判成有证据。
READ_NUDGE = USER_CFG.get('read_nudge',
                          '（你还没读过那个文件，不许说它的内容是什么。现在直接调用 '
                          'read_file：path 写成完整绝对路径，先把文件真读出来，'
                          '再照着读到的原文回答。绝不能用网上搜到的说法冒充文件内容。）')

# 进度话术(临时回复)是否真的发到微信:
#   False(默认) -> 只打印在日志里(事件通道), 不碰微信窗口, 不打断工具操作
#   True        -> 交给 sender 发出去, 对方能看到"我先看一下"
SEND_INTERIM = USER_CFG.get('send_interim', False)
INTERIM_MAX_LEN = USER_CFG.get('interim_max_len', 40)
# 一条消息最多播报几条进度。每条都要完整走一遍 sender(激活窗口 + ctrl+F 搜索会话 +
# OCR 复核目标 + 粘贴发送), 实测单条 6~8s。不设上限的话 "6 轮 × 2 条 × 8s" 能把端到端
# 拖到两分钟以上, 对方看到的是一串刷屏而不是进度。
INTERIM_MAX_COUNT = USER_CFG.get('interim_max_count', 6)
# 两条播报之间的最短间隔(秒)。只有"条数上限"是不够的: 上限 6 在长任务里会被吃满 ——
# 实测一轮 127.3s 的搜索任务, "助手耗时 117.1s" 里有 6 条播报 × 8.2s = 49.8s, 占掉
# 端到端的 43%; 而且这 8s 是**串行**加在链路上的(播完才继续下一轮), 所以它不只是刷屏,
# 是实打实地把答案往后拖。真正的闸门是时间: 距上一条不足这个秒数就不播。
# 30s 这个值有实测依据(2026-10-08, 真模型跑联网任务):
#   58.3s 的任务播 2 条 —— t=11.4s"正在搜索"、t=42.4s"正在执行 list_windows", 间隔 31.0s,
#   两条文案不同、各自都有信息量。生产里这两条要串行花 2×8.2s=16.4s(占端到端 22%)。
# 试过放宽到 45s: 第 2 条会被挡掉(42.4-11.4=31 < 45), 58s 的任务只剩开头 11s 有动静,
# 后面 47s 是干等 —— 对方又会以为它死了, 省下的 8.2s 不值。所以定 30s。
INTERIM_MIN_GAP = USER_CFG.get('interim_min_gap', 30)
# 工具跑完要不要把结果也播报一句("返回结果: 命中 8 条…")。
# 这句是**程序按真实结果生成**的, 不是模型编的 —— 模型的活只说"正在搜索…"那种预告。
INTERIM_RESULT = USER_CFG.get('interim_result', True)

# ============================================================
# 9. 非文本消息固定话术
# ============================================================
MEDIA_REPLIES = dict(USER_CFG.get('media_replies', {
    'image': '图片我这边看不了 你直接发文字吧',
    'voice': '语音我这边听不了 发文字吧',
    'video': '视频我这边看不了 发文字吧',
    'media': '这个我这边看不了 发文字吧',
}))

# ============================================================
# 10. 联网搜索 —— 简单搜索, 等价 curl "https://cn.bing.com/search?q=..."
# ============================================================
SEARCH_ENGINE = USER_CFG.get('search_engine', 'bing')   # bing / baidu / both(双源对照)
SEARCH_TIMEOUT = USER_CFG.get('search_timeout', 15)

# ============================================================
# 11. 第二段(二次思考)的总结提示词
# 工具跑完后单独走一趟, 这一趟不给工具, 它只能产出要发出去的正文。
# ============================================================
SUMMARY_PROMPT = USER_CFG.get('summary_prompt', (
    "【第二轮：总结回复】工具已经执行完了。只看下面的执行记录，"
    "把结果总结成一条要发给「示例联系人」的微信消息。\n"
    "要求：\n"
    "1) 直接说结果，口语、简短；\n"
    "2) 不要提工具名、参数、JSON、日志，也不要说「我正在…」；\n"
    "3) 不要照抄执行记录的原文，只给人话结论；\n"
    "4) 没成功就直说没成功、卡在哪，别编；\n"
    "5) 别用中文标点，断句用空格或换行，需要标点时用英文半角 , . ! ? 。"
))
