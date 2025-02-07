# iMessage LLM Assistant

一个基于大语言模型的 iMessage 智能助手，可以自动回复 iMessage 消息。支持多种 LLM 模型，提供 Web 控制台进行管理。

## 功能特点

- 🤖 自动回复 iMessage 消息
- 💬 支持多种 LLM 模型（如 Deepseek、ChatGPT 等）
- 🌐 Web 控制台管理界面
- 📝 自定义系统提示词（System Prompt）
- 🔄 实时对话监控
- 📊 对话历史记录和统计
- 🗑️ 自动清理过期数据
- 🔒 安全的配置管理

## 系统要求

- macOS 系统（需要访问 iMessage 数据库）
- Python 3.7+
- 支持的 LLM API（如 Deepseek API）

## 安装步骤

1. 克隆仓库：
```bash
git clone https://github.com/wangyaominde/iMessage_llm.git
cd iMessage_llm
```

2. 创建并激活虚拟环境：
```bash
# 创建虚拟环境
python -m venv venv

# 在 macOS/Linux 上激活虚拟环境
source venv/bin/activate

# 在 Windows 上激活虚拟环境
# .\venv\Scripts\activate
```

3. 安装依赖：
```bash
pip install -r requirements.txt
```

4. 授予 Terminal（或 iTerm）访问权限：
   - 打开"系统设置"
   - 进入"隐私与安全性" -> "完全磁盘访问权限"
   - 点击"+"号添加你的终端应用（Terminal.app 或 iTerm）
   - 确保该应用的开关是打开的
   - 重启终端应用

## 配置说明

1. 首次运行程序：
```bash
python message_ai_service.py
```

2. 访问 Web 控制台：
```
http://localhost:8888
```

3. 在控制台配置 AI 参数：
   - API Key：你的 LLM API 密钥
   - API URL：API 端点地址
   - Model Name：模型名称
   - System Prompt：系统提示词
   - Temperature：温度参数（0.0-1.5）
   - 最大历史记录长度：每个对话保留的最大消息数

## 目录结构

```
iMessage_llm/
├── message_ai_service.py   # 主服务程序
├── imessage_reader.py      # iMessage 读取模块
├── imessage_sender.py      # iMessage 发送模块
├── requirements.txt        # 项目依赖
├── README.md              # 项目说明
├── venv/                  # Python 虚拟环境（安装后生成）
└── data/                  # 数据目录
    ├── config.json        # 配置文件
    ├── messages.db        # 消息数据库
    └── template.html      # Web 界面模板
```

## 数据管理

- 消息历史和调用记录存储在 SQLite 数据库中
- 自动清理 30 天前的历史数据
- 可以通过 Web 控制台手动清理历史记录
- 配置信息保存在 config.json 中

## API 支持

目前支持以下 API：
- Deepseek API
- 可扩展支持其他符合 OpenAI API 格式的服务

## 开发说明

如果要扩展或修改功能：

1. 添加新的 LLM 支持：
   - 修改 `get_ai_response()` 函数
   - 确保 API 响应格式处理正确

2. 自定义 Web 界面：
   - 修改 `data/template.html` 文件
   - 重启服务生效

3. 修改数据库结构：
   - 更新 `init_db()` 函数中的表结构
   - 注意处理数据迁移

## 常见问题

1. 无法访问 iMessage 数据库
   - 检查终端是否有完全磁盘访问权限
   - 重启终端后重试

2. API 连接失败
   - 验证 API 密钥是否正确
   - 检查网络连接
   - 确认 API 地址是否可访问

3. 消息未自动回复
   - 检查配置是否正确
   - 查看日志输出
   - 确认程序正在运行

4. 虚拟环境相关问题
   - 如果提示 "venv: command not found"，请确保已安装 Python3
   - 激活虚拟环境后命令行前面应该显示 (venv)
   - 如需退出虚拟环境，使用命令 `deactivate`
   - 如果安装依赖时出错，尝试先升级 pip：`pip install --upgrade pip`

## 许可证

MIT License

## 贡献指南

欢迎提交 Issue 和 Pull Request！

1. Fork 本仓库
2. 创建你的特性分支
3. 提交你的改动
4. 推送到你的分支
5. 创建 Pull Request

## 联系方式

如有问题，请提交 Issue 或联系维护者。

## 更新日志

### v1.0.0
- 初始版本发布
- 基本的消息自动回复功能
- Web 控制台管理界面
- 数据库存储和自动清理 