# -*- coding: utf-8 -*-
"""本地知识库: 诗词 + 固定问答, 确定性回答不依赖大模型
数据文件 kb.json 可手动增删(重启生效)"""
import json, os, re, time

BASE = os.path.dirname(os.path.abspath(__file__))
KB_JSON = os.path.join(BASE, 'kb.json')
_KB = None


def _load():
    global _KB
    if _KB is None:
        try:
            with open(KB_JSON, encoding='utf-8') as f:
                data = json.load(f)
            _KB = data if isinstance(data, dict) else {}
        except Exception as e:
            print(f'[kb] 知识库加载失败: {e}')
            _KB = {}
    return _KB


def query_qa(text):
    """固定问答: 正则命中直接给答案; 未命中返回 None"""
    for q in _load().get('qa', []):
        try:
            if re.search(q['pattern'], text):
                return q['answer']
        except Exception:
            continue
    return None


def _norm(s):
    """归一化: 去空白/间隔符/标点. 让"沁园春·雪"能被"沁园春雪"命中"""
    for ch in ' \t\r\n·．。，、！？；：""''（）【】[]':
        s = s.replace(ch, '')
    return s


def query_poem(text):
    """诗词: 标题/别名命中即给全文; 返回"标题（作者）\n全文"或 None.
    标点不敏感: "背诵沁园春雪" 也能命中标题为"沁园春·雪"的词"""
    nt = _norm(text)
    best = None
    for p in _load().get('poems', []):
        score = 0
        if _norm(p['title']) in nt:
            score += 3
        for k in p.get('keywords', []):
            if k and _norm(k) in nt:
                score += 1
        if score and (best is None or score > best[0]):
            best = (score, p)
    if best:
        p = best[1]
        return f"{p['title']}（{p['author']}）\n{p['full']}"
    return None


def today_holiday():
    """今天的节日名(公历固定节日, kb.json 的 holidays 表), 无则返回 None.
    只收公历固定日期; 农历节日(春节/中秋等)每年日期不同, 暂未纳入"""
    return _load().get('holidays', {}).get(time.strftime('%m-%d'))
