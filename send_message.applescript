on run {targetBuddy, messageText}
    tell application "Messages"
        # 确保 Messages 应用程序正在运行
        activate
        
        # 设置要发送消息的目标联系人
        set targetService to 1st service whose service type = iMessage
        set targetContact to buddy targetBuddy of targetService
        
        # 发送消息
        send messageText to targetContact
        
        return "消息发送成功"
    end tell
end run 