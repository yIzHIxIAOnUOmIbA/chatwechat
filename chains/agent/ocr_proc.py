# -*- coding: utf-8 -*-
"""OCR 帮工进程(一次性): 从 stdin 收一张 PNG, 识别出的文字以 JSON 打到 stdout。

为什么要单独开进程跑 OCR:
    本机 winrt 的 _winrt_windows_globalization 模块会在 OCR 时**段错误**
    (Exception code 0xc0000005, 系统日志 Application Error 1000 可查)。
    段错误捕不住、也没有 traceback —— 它直接干掉整个解释器, 现象就是
    "干完一件事进程就没了": 日志断在半路、run_err.log 一个字都没有。
    段错误没法 try/except, 唯一可靠的办法就是把它关进子进程:
    崩了只崩这个帮工, 主进程活着, 拿不到文字就按"OCR 不可用"处理。

接口:
    stdin  : PNG 字节
    stdout : {"ok": true, "text": "..."}  成功
             {"ok": false, "err": "..."}  识别失败
    退出码 : 0 成功 / 2 没收到图 / 3 识别报错 (非 0 一律当不可用)
    除 stdout 的 JSON 外, 其它输出一律走 stderr, 免得污染协议。
"""
import sys, io, os, json, asyncio


def _init_apartment():
    """WinRT 要求调用线程先声明公寓类型。

    本机崩溃点就在 globalization 模块, 而 Globalization 的语言/用户配置接口
    (OcrEngine.try_create_from_user_profile_languages 走的就是它)最挑公寓状态,
    所以开跑前先初始化好。

    实测接口: init_apartment **必须显式传 ApartmentType**(不传直接 TypeError,
    不是可选的), 枚举值 MULTI_THREADED=0 / SINGLE_THREADED=2; OCR 走 async, 用 MTA。
    初始化失败不影响主流程(顶多回到"不初始化"的老样子), 所以只提示不抛。
    """
    try:
        from winrt.runtime import init_apartment, ApartmentType
        init_apartment(ApartmentType.MULTI_THREADED)
        return True
    except Exception as e:                                  # noqa: BLE001
        print(f'[ocr_proc] init_apartment 失败(忽略): {type(e).__name__}: {e}',
              file=sys.stderr)
        return False


def _ocr(png_bytes):
    """识别 PNG 里的文字(预处理与原实现完全一致, 保证识别效果不退化)"""
    from PIL import Image, ImageOps
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat

    img = Image.open(io.BytesIO(png_bytes))

    async def run():
        gray = img.convert('L')
        if gray.width > 1400:                  # 防超大图, 先压到合理宽度
            k = 1400 / gray.width
            gray = gray.resize((1400, int(gray.height * k)), Image.LANCZOS)
        gray = gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS)
        # cutoff=1 会按分位数裁掉最暗/最亮的 1% 像素再拉伸。微信窗口截图恰好是
        # "大面积深色底 + 少量亮字": 区域一大, 那点文字像素就落进被裁掉的 1% 里,
        # 于是整块被拉平、OCR 返回空。实测同一张图:
        #   (64,45,429,267) 高 222 -> '0=文件传助手'   ← 认得出
        #   (64,52,429,475) 高 423 -> ''              ← 原复核区, 被裁没了
        #   (36,45,300,700) 高 655 -> ''
        # 所以默认不裁(cutoff=0)。要退回旧行为设环境变量 OCR_CUTOFF=1。
        gray = ImageOps.autocontrast(gray, cutoff=int(os.environ.get('OCR_CUTOFF', '0')))
        bgra = gray.convert('RGBA').tobytes('raw', 'BGRA')
        sb = SoftwareBitmap.create_copy_from_buffer(
            bgra, BitmapPixelFormat.BGRA8, gray.width, gray.height)
        engine = OcrEngine.try_create_from_language(Language('zh-CN'))
        if engine is None:
            engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            return ''
        result = await engine.recognize_async(sb)
        return '\n'.join(l.text for l in result.lines)

    return asyncio.run(run())


def main():
    _init_apartment()
    try:
        data = sys.stdin.buffer.read()
    except Exception as e:                                  # noqa: BLE001
        print(f'[ocr_proc] 读 stdin 失败: {e}', file=sys.stderr)
        return 2
    if not data:
        print(json.dumps({'ok': False, 'err': '没收到图片'}, ensure_ascii=False))
        return 2
    try:
        text = _ocr(data)
    except Exception as e:                                  # noqa: BLE001
        print(json.dumps({'ok': False,
                          'err': f'{type(e).__name__}: {e}'}, ensure_ascii=False))
        return 3
    print(json.dumps({'ok': True, 'text': text or ''}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
