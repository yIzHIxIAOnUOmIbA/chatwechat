# -*- coding: utf-8 -*-
"""兼容壳: 实现已搬到 wxduo/core/cloud_llm.py(大小号共用同一份)。

以前这里是一份完整拷贝, 改一处漏一处(GLM/DeepSeek 双线路就是这么漏出来的)。
小号进程的 sys.path[0] 是 chains/agent, 里面的 `config` 就是小号的 config.json,
所以 core 那份里的 `import config` 拿到的仍然是本进程的配置, 调用方不用改。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.cloud_llm import (                                   # noqa: E402,F401
    ORDER, PRESETS, CloudDisabled, active, cloud_chat, describe, err_text,
    ping, providers,
)
