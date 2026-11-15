# -*- coding: utf-8 -*-
"""多模型群聊: 同一段聊天记录同时喂给多个模型, 各自回复、各自前缀发出。

为什么单独一层, 不塞进 agent.ask():
- ask() 里的 _loop 握着工具执行权。多个模型同时拿到工具就会重复动手(同一件事
  写两次文件、发两次消息), 这是不可接受的副作用。群聊这层只做"纯对话并行",
  一律不挂 tools; 要动手的活仍然交给单模型的 ask()。
- 每个模型必须独立超时、独立失败: 一个模型挂了不能让整轮没有回复, 所以走线程池
  并且各自兜底, 失败的只在日志里留一行。

配置在 config.json(改完重启服务生效):
  "multi_model": {
    "enabled": false,
    "timeout": 45,
    "num_predict": 320,
    "models": [
      {"name": "qwen3-本地", "prefix": "[Qwen] ", "via": "local",
       "model": "qwen3:4b-nothink"},
      {"name": "GLM-云端", "prefix": "[GLM] ", "via": "cloud",
       "model": "glm-4.5-air"}
    ]
  }

发送顺序严格按 models 里的排列, 不按谁先返回 —— 否则同一个问题每次回复顺序都
不一样, 对方读起来像抽风。
"""
import concurrent.futures
import time

import agent
import config

DEFAULTS = {
    'enabled': False,
    'timeout': 45,
    'num_predict': 320,
    'models': [],
}


def settings():
    """从 config.USER_CFG 取 multi_model, 缺项回落默认。"""
    mc = (config.USER_CFG or {}).get('multi_model')
    if not isinstance(mc, dict):
        mc = {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in mc:
            out[k] = mc[k]
    out['enabled'] = bool(out['enabled'])
    out['models'] = [m for m in (out['models'] or []) if isinstance(m, dict)]
    return out


def models():
    """解析成调用清单, 跳过 enabled=false 和没填模型的条目。"""
    out = []
    for i, m in enumerate(settings()['models']):
        if m.get('enabled') is False:
            continue
        name = str(m.get('name') or f'model{i + 1}')
        via = 'cloud' if str(m.get('via', 'local')).lower() == 'cloud' else 'local'
        model = str(m.get('model') or (config.CLOUD_MODEL if via == 'cloud'
                                       else config.LLM_MODEL))
        prefix = str(m.get('prefix') if m.get('prefix') is not None
                     else f'[{name}] ')
        out.append({'name': name, 'prefix': prefix, 'via': via, 'model': model})
    return out


def enabled():
    return bool(settings()['enabled'] and models())


def _one(spec, messages, timeout, num_predict):
    """跑单个模型, 任何异常都就地吃掉并记下来, 不拖垮其他人。"""
    t0 = time.time()
    try:
        if spec['via'] == 'cloud':
            msg = agent._chat_cloud(messages, timeout=timeout,
                                    num_predict=num_predict, model=spec['model'])
        else:
            msg = agent._chat_local(messages, timeout=timeout,
                                    num_predict=num_predict, model=spec['model'])
    except Exception as e:                                  # noqa: BLE001
        return {'name': spec['name'], 'ok': False, 'err':
                f'{type(e).__name__}: {e}'[:140], 'ms': (time.time() - t0) * 1000}
    text = agent._clean(msg.get('content') or '')
    return {'name': spec['name'], 'ok': bool(text), 'text': text,
            'ms': (time.time() - t0) * 1000}


def collect(messages, timeout=None):
    """并行问所有模型, 返回 [(prefix, text, name), ...]。

    messages 一般是 session.wire() 的快照; 这里只读, 绝不回写会话历史 —— 群聊
    的多份回复不该全都塞进主模型的上下文, 否则下一轮它会被自己另外几个分身带偏。
    """
    specs = models()
    if not specs:
        return []
    s = settings()
    timeout = timeout or s['timeout'] or 45
    num_predict = s['num_predict'] or config.FAST_NUM_PREDICT
    limit = max(config.MAX_REPLY_LEN, 300)

    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(specs), 4)) as ex:
        futs = [ex.submit(_one, sp, messages, timeout, num_predict) for sp in specs]
        results = [f.result() for f in futs]
    print(f'[群聊] {len(specs)} 个模型并行完成, 耗时 {time.time() - t0:.1f}s')

    order = {sp['name']: i for i, sp in enumerate(specs)}
    results.sort(key=lambda r: order.get(r['name'], 99))   # 发送顺序恒定

    out, seen = [], set()
    for r in results:
        if not r['ok']:
            print(f"  [群聊] {r['name']} 失败({r['ms']:.0f}ms): {r['err']}")
            continue
        txt = (r['text'] or '').strip()
        if not txt:
            print(f"  [群聊] {r['name']} 回了空内容, 跳过")
            continue
        if txt in seen:
            print(f"  [群聊] {r['name']} 与前面某条完全相同, 跳过")
            continue
        seen.add(txt)
        if len(txt) > limit:
            txt = txt[:limit].rstrip()
        out.append((specs[order[r['name']]]['prefix'], txt, r['name']))
        print(f"  [群聊] {r['name']} ok {r['ms']:.0f}ms: {txt[:60]!r}")
    return out
