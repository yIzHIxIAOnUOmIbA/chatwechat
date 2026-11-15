# wxduo

Windows 上的微信双号自动化：**大号**替本人回消息，**小号**当桌面助手。纯 Python 标准库，
不需要装任何第三方包，ollama 负责本地模型，云端（GLM / DeepSeek）负责硬活。

> ⚠️ 本项目通过 UI 自动化操作微信客户端，属于非官方接口。使用前请自己确认微信服务条款
> 与当地法规，仅在自己拥有或已获授权的账号上使用，风险自负。

## 它能做什么

- **大号：拟人回复**。读本地消息库判断该不该回（白名单 / 静默窗口 / 回复频率），
  生成回复后走真实输入框发送，不是 hook。
- **小号：桌面助手**。收到指令后能读写文件、跑命令、看屏幕（OCR）、上网查资料，
  干完活把结论发回微信；支持多模型群聊（多个模型各出一版，再合成一条）。
- **云端多线路**：GLM 先上，DeepSeek 顶第二条，两条都不可用才落本地 ollama。
  单条线路内部还有"主模型 → 备用模型"两层。
- **控制台**：一个 GUI 管全部配置 —— 启停服务、挑模型、改 system prompt、改开场话术、
  维护白名单、开关云端线路。改完点保存，只写真正改过的键。

## 目录结构

```
wxduo/
├── wxduo_console.py        控制台（tkinter，单文件）
├── 控制台.bat              双击启动控制台
├── core/                   两个号共用
│   ├── cloud_llm.py        云端线路（GLM / DeepSeek / 协议转换）
│   ├── first_reply.py      开场话术模板
│   ├── acct.py             窗口 → 进程 → 微信账号目录 的识别
│   ├── pidfile.py          进程名片（谁是大号谁是小号）
│   ├── subwin.py / win.py  窗口与输入的基础操作
├── chains/chat/              大号：真人对话
│   ├── main.py             主循环
│   ├── ai_reply.py         决策 + 生成 + 发送
│   ├── config.py           所有配置项的读取与默认值
│   ├── sender.py / wx_db.py
├── chains/agent/              小号：桌面助手
│   ├── main.py / guard.py  主循环与看门狗
│   ├── agent.py            工具循环
│   ├── pc_tools.py / net_tools.py / ocr_proc.py
│   ├── group_chat.py       多模型群聊
│   ├── skills.py / subagent.py
├── autostart/              开机自启用的 vbs（隐藏窗口）
└── chains/*/config.json      你自己的配置（不进 git）
```

## 快速开始

1. 装 Python 3.11+ 和 [ollama](https://ollama.com/)，拉一个能用的本地模型：
   ```
   ollama pull qwen3:8b
   ```
2. 把 `chains/chat/config.example.json`、`chains/agent/config.example.json` 各复制一份成
   `config.json`，按注释改掉微信路径、账号目录等。
3. 大号需要一份白名单 `chains/chat/whitelist.json`：`{"示例会话": "备注名", ...}`
   （键是微信备注前 8 位数字，值是不带 ID 的纯名字）。
4. 双击 `控制台.bat`，在「服务」页启动小号/大号；或在 `autostart/` 里注册开机自启。
5. 云端要用的话，在控制台「云端线路」页填 API Key（也可以让它从 CodeBuddy 的
   `~/.codebuddy/models.json` 里读）。

## 云端线路是怎么排的

```
GLM 主模型 → GLM 备用模型 → DeepSeek 主模型 → DeepSeek 备用模型 → 本地 ollama
```

只有"这条线路此刻用不了"（余额/限流/连不上/超时）才会往下一格走；参数错误这类
换线路也没用的错会直接抛出来，不吞。每条线路的开关、模型、超时、地址都能在控制台改。

本地兜底**只配一个模型**：兜底是最后一道，它自己 404 一次再白等一次超时，等于没有兜底。

## 配置项速查

`chains/*/config.json` 里的键都用同一套小写命名，改完重启对应服务生效。

| 键 | 说明 |
| --- | --- |
| `llm_model` / `llm_primary` | 本地模型；小号的 `llm_primary` 可选 `local` / `cloud` |
| `llm_fallback_model` | 本地兜底只这一个（大号） |
| `llm_timeout` / `llm_num_ctx` / `llm_keep_alive` | 本地推理参数 |
| `cloud_providers.{glm,deepseek}` | 云端线路：`enabled / model / fallback_model / timeout / base_url / api_key` |
| `cloud_enable` / `cloud_model` | 老版本的单线路配置，仍兼容 |
| `system_prompt` | 留空用代码里的默认人设 |
| `first_reply.*` | 开场话术（模板 + 前缀 + 正文 + 后缀） |
| `whitelist.json` | 大号只回这些会话 |
| `poll_interval` | 轮询间隔（秒） |

API Key 只保存在本机 `config.json`，代码里没有硬编码任何 key。

## 控制台快捷键

- `Ctrl+S` 保存（只写改过的键，自动备份 `.bak-时间戳`）
- 白名单页：`Ctrl+F` 搜索、`Ctrl+N` 新增、`F2` 编辑、`Del` 删除、`Ctrl+C` 复制、
  「批量粘贴导入」一次贴一堆（每行 `ID 名字`，顺序和分隔符随意）

## 免责声明

仅供学习与个人自动化使用。因使用本工具导致的账号风险、数据丢失或违反平台规则，
作者不承担责任。

## License

MIT，见 [LICENSE](LICENSE)。
