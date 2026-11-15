# -*- coding: utf-8 -*-
"""看门狗: 盯着 main.py, 它一退出就 3 秒内拉起来。

为什么需要它:
    本机 winrt 的 OCR 会触发段错误(0xc0000005), 那种死法**没有 traceback**,
    run_err.log 一片空白, 你只会觉得"干完一件事它就没了", 然后所有消息都没人回。
    段错误已经用子进程隔离掉了(见 ocr_proc.py), 这里再兜最后一道:
    不管 main.py 因为什么退出, 都自动重启, 保证服务不会停在"死掉"状态。

用法: python -u guard.py     (日志: guard.log + 控制台; 按 Ctrl+C 退出)
"""
import os, sys, subprocess, time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, 'guard.log')

# core/ 是大小号共用的底层, 与 chains/agent 差两级目录(和 main.py 同一套找法)
_ROOT = os.path.dirname(os.path.dirname(HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from core import pidfile                                          # noqa: E402


def _log(msg):
    line = f'{time.strftime("%m-%d %H:%M:%S")} {msg}'
    print(f'[guard] {msg}', flush=True)
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:                                       # noqa: BLE001
        pass


def main():
    # 控制台靠这张名片判断"小号在不在跑"(命令行里只有 guard.py, 分不清哪个服务)
    pidfile.claim(os.path.join(HERE, 'guard.pid'))
    while True:
        _log('启动 main.py')
        proc = None
        try:
            # -u: 日志不缓冲 —— 再死一次时, run_out.log 能停在真实位置,
            #     不会再出现"日志看着断在半路"那种误导(块缓冲会吞掉尾部)
            proc = subprocess.Popen([sys.executable, '-u', 'main.py'] + sys.argv[1:],
                                    cwd=HERE)
            code = proc.wait()
        except KeyboardInterrupt:
            _log('收到 Ctrl+C, 看门狗退出')
            if proc and proc.poll() is None:
                proc.terminate()
            return 0
        except Exception as e:                              # noqa: BLE001
            _log(f'启动 main.py 失败: {type(e).__name__}: {e}')
            code = -1
        _log(f'main.py 退出 code={code} (0x{code & 0xffffffff:08X}), 3 秒后重启')
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            _log('收到 Ctrl+C, 看门狗退出')
            return 0


if __name__ == '__main__':
    sys.exit(main())
