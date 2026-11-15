# -*- coding: utf-8 -*-
"""首次话术: 某人第一次来消息时, 先回一段固定文案, 追问了才交给 AI。

为什么这段走代码而不是交给模型: 预约类的第一句话必须稳定 —— 等多久、怎么继续,
得一句话说死。让模型自由发挥就会变成"应该很快吧""我帮你问问"这种没信息量的
话, 而对方要的就是一个明确的等待期。所以固定文案不进模型, 判定也不进模型。

"第一次"的判定看 state 里记的"是否已发过", 与消息库无关: 微信重启、库被重建、
历史被清都不会让它重发一遍。reset_hours > 0 时按时间窗复位(隔太久再来算新一轮)。

配置在两端 config.json 的 first_reply 里(改完重启服务生效):
    "first_reply": {
      "enabled": true,
      "prefix": "[自动回复]",
      "text": "下次预约等待一星期",
      "suffix": "追问以进入AI回复",
      "template": "",
      "users": [],
      "reset_hours": 0
    }

template 非空时优先用它, 可写 {prefix} {text} {suffix} 三个占位符, 想换行写 \\n。
users 为空 = 所有被监听的会话; 填备注全名/备注前8位/wxid 就只对这些人生效。
"""
import time

STATE_KEY = '_first_reply'      # 与两端的 state 结构共存: 大号键是 wxid, 小号是 last_id_xxx

DEFAULTS = {
    'enabled': False,
    'prefix': '[自动回复]',
    'text': '',
    'suffix': '',
    'template': '',
    'users': [],
    'reset_hours': 0,
}


def settings(user_cfg):
    """从 config.USER_CFG 里取 first_reply, 缺项回落 DEFAULTS。"""
    fr = (user_cfg or {}).get('first_reply')
    if not isinstance(fr, dict):
        fr = {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in fr:
            out[k] = fr[k]
    out['enabled'] = bool(out['enabled'])
    out['prefix'] = str(out['prefix'] or '')
    out['text'] = str(out['text'] or '')
    out['suffix'] = str(out['suffix'] or '')
    out['template'] = str(out['template'] or '')
    out['users'] = [str(x).strip() for x in (out['users'] or []) if str(x).strip()]
    try:
        out['reset_hours'] = float(out['reset_hours'] or 0)
    except (TypeError, ValueError):
        out['reset_hours'] = 0.0
    return out


def build(user_cfg):
    """拼出要发的那段话(不含任何"AI生成"前缀, 前缀由 prefix 字段自己带)。

    默认排版: '[自动回复] 下次预约等待一星期，追问以进入AI回复'
    """
    s = settings(user_cfg)
    if s['template']:
        try:
            return s['template'].format(prefix=s['prefix'], text=s['text'],
                                        suffix=s['suffix'])
        except (KeyError, IndexError, ValueError):
            pass                    # 占位符写错了就退回默认排版, 不能让话术发不出去
    body = f'{s["prefix"]} {s["text"]}'.strip() if s['prefix'] else s['text']
    if s['suffix']:
        # suffix 以换行开头 = 想单独另起一行, 这时不能在前面塞中文逗号
        # (否则发出去是 "…我会及时查看，" 换行, 逗号孤零零挂在行尾)
        if body and not s['suffix'].lstrip('\r').startswith('\n'):
            body = f'{body}，{s["suffix"]}'
        else:
            body = f'{body}{s["suffix"]}' if body else s['suffix']
    return body


def enabled_for(user_cfg, un='', remark='', remark8=''):
    """这个会话该不该走首次话术。users 为空表示所有会话都走。"""
    s = settings(user_cfg)
    if not s['enabled'] or not build(user_cfg):
        return False
    if not s['users']:
        return True
    have = {str(x).strip() for x in (un, remark, remark8) if str(x or '').strip()}
    return bool(have & set(s['users']))


def should_send(state, key, user_cfg, now=None):
    """还没发过 -> True; 超过 reset_hours 没发过 -> 也算新的一轮。"""
    s = settings(user_cfg)
    rec = state.get(STATE_KEY) or {}
    ts = rec.get(key)
    if not ts:
        return True
    if s['reset_hours'] <= 0:
        return False
    return (now or time.time()) - float(ts) >= s['reset_hours'] * 3600


def mark(state, key, now=None):
    """记下"已给这个人发过首次话术"。调用方负责落盘(state)。"""
    rec = state.get(STATE_KEY)
    if not isinstance(rec, dict):
        rec = {}
        state[STATE_KEY] = rec
    rec[key] = now or time.time()
