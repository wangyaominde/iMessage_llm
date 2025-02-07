#!/usr/bin/env python3
import subprocess
import os

def send_imessage(contact, message):
    """
    发送iMessage消息给指定联系人
    
    Args:
        contact (str): 联系人的电话号码或Apple ID
        message (str): 要发送的消息内容
    """
    # 获取当前脚本所在目录
    current_dir = os.path.dirname(os.path.abspath(__file__))
    script_path = os.path.join(current_dir, 'send_message.applescript')
    
    # 执行AppleScript
    cmd = ['osascript', script_path, contact, message]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"消息已成功发送给 {contact}")
        else:
            print(f"发送失败: {result.stderr}")
    except Exception as e:
        print(f"发送出错: {str(e)}")

if __name__ == "__main__":
    # 示例使用
    contact = input("请输入联系人 (电话号码或 Apple ID): ")
    message = input("请输入要发送的消息: ")
    send_imessage(contact, message) 