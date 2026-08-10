# iMessage Agent

把 iMessage 变成一个**每用户独立的 AI Agent**：每个联系人拥有自己逻辑隔离的 agent
（独立对话历史、长期记忆、工具状态，互不阻塞并发），agent 通过自研的轻量 harness
自主调用工具。LLM 后端可在 **Anthropic Claude** 与 **OpenAI 兼容接口（DeepSeek / Qwen / GLM 等）**
之间切换。

## 功能

- 每用户独立 Agent：独立历史 + 独立长期记忆 + 独立工具状态，不同用户并发处理
- 双后端可切换：Anthropic 官方 API / 任意 OpenAI 兼容端点（配 base_url）
- 自研工具调用循环（harness），provider 无关
- 工具：
  - **联网搜索**：用大模型自带能力（Anthropic 原生 server tool / OpenAI 端点自带搜索）
  - **长期记忆**：跨会话记住用户偏好与事实
  - **定时提醒 / 任务**：到点主动给用户发 iMessage
  - **BT 资源搜索**：返回标题 / 大小 / 做种数 + 磁力链接
- Web 管理后台：配置后端与工具、查看每用户 agent 状态、看日志

## 系统要求

- macOS（需 iMessage）
- Python 3.9+
- 终端需「完全磁盘访问权限」以读取 iMessage 数据库

## 安装与使用

```bash
chmod +x run.sh
source run.sh
```

然后浏览器打开 http://localhost:8877：

1. 选择 Provider（Anthropic 或 OpenAI 兼容），填 API Key / Base URL / 模型
2. 勾选需要的工具，保存配置
3. 「测试连接」确认后端可用
4. 「启动服务」开始监听 iMessage

## 授予磁盘访问权限

系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 添加你的终端（Terminal / iTerm）并重启终端。

## 架构

```
imessage_reader.py   读 chat.db，检测新消息（回调契约：list→truthy ack）
app.py               Flask 路由 + 线程编排 + 回调分发
providers/           LLM 后端抽象（base / anthropic / openai）
agent/               harness（工具循环）/ session（单用户）/ manager（并发调度）
tools/               memory / reminder / torrent（联网搜索在 providers 层）
config.py            配置（config.json）
```

数据落在 `agent_state/`（每用户历史、记忆、提醒，已 gitignore）。

## 说明

- 每用户 agent 状态可在「查看用户 Agent」页面查看/清理
- 消息检测支持文件监控（实时，推荐）或定时查询
- 发送失败会进重试队列，后台线程周期重试
