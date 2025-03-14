# iMessage-Dify 集成

这个项目将 iMessage 与 Dify AI 平台集成，允许自动处理和回复 iMessage 消息。

## 功能特点

- 使用 Flask 构建的简单 Web 界面
- 配置 Dify API URL 和 API 密钥
- 自动检测新的 iMessage 消息
- 使用 Dify AI 处理消息内容
- 通过 AppleScript 自动回复消息
- 可配置的消息检查间隔

## 系统要求

- macOS 系统（需要 iMessage 支持）
- Python 3.6+
- 终端应用需要完全磁盘访问权限（用于读取 iMessage 数据库）

## 安装和使用

1. 克隆或下载此仓库
2. 运行启动脚本：

```bash
chmod +x run.sh
source run.sh
```

3. 在浏览器中访问 http://localhost:8888
4. 配置 Dify API URL 和 API 密钥
5. 点击"测试连接"确保配置正确
6. 点击"启动服务"开始监听新消息

## 授予磁盘访问权限

要读取 iMessage 数据库，您需要授予终端应用完全磁盘访问权限：

1. 打开系统偏好设置
2. 前往安全性与隐私 > 隐私 > 完全磁盘访问权限
3. 点击锁图标并输入密码以进行更改
4. 点击"+"按钮并添加您的终端应用（Terminal 或 iTerm）
5. 重启终端应用

## 配置 Dify

1. 在 [Dify](https://dify.ai) 创建一个应用
2. 获取 API 密钥（选择"对话"类型的应用）
3. 将 API URL 和密钥配置到本应用中

## 工作原理

1. 应用定期检查 iMessage 数据库中的新消息
2. 当检测到新消息时，将消息内容发送到 Dify API 进行处理
3. 获取 Dify 的回复
4. 使用 AppleScript 通过 iMessage 发送回复

## 注意事项

- 此应用仅在 macOS 上运行
- 需要完全磁盘访问权限才能读取 iMessage 数据库
- 首次运行时，可能需要重置消息 ID 以处理现有消息

## 故障排除

- 如果无法访问 iMessage 数据库，请确保已授予终端应用完全磁盘访问权限
- 如果 Dify 连接测试失败，请检查 API URL 和密钥是否正确
- 如果消息未被处理，尝试点击"重置消息 ID"按钮 