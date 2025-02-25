"""
提醒代理模块，用于处理提醒请求
"""

import logging
from datetime import datetime
from .time_parser import parse_time_from_message, parse_time_with_llm

# 配置日志
logger = logging.getLogger(__name__)

class ReminderAgent:
    """提醒代理，处理各种提醒请求"""
    
    def __init__(self, config):
        """
        初始化提醒代理
        
        Args:
            config: 配置对象，包含API密钥等信息
        """
        self.config = config
    
    def set_reminder(self, contact, content, time_str):
        """
        设置提醒
        
        Args:
            contact: 联系人
            content: 提醒内容
            time_str: 提醒时间字符串
            
        Returns:
            设置结果
        """
        try:
            if not content or not time_str:
                return "设置提醒失败，缺少内容或时间"
                
            try:
                scheduled_time = parse_time_from_message(time_str)
                if not scheduled_time:
                    scheduled_time = parse_time_with_llm(time_str, config=self.config)
            except:
                scheduled_time = parse_time_with_llm(time_str, config=self.config)
                
            if not scheduled_time or scheduled_time <= datetime.now():
                return "无法识别提醒时间，或提醒时间已过"
            
            # 这里需要调用外部的create_scheduled_task函数
            # 由于这个函数在message_ai_service.py中，我们需要通过回调或其他方式处理
            # 这里假设我们有一个回调函数
            if hasattr(self.config, 'create_scheduled_task'):
                task_id, _ = self.config.create_scheduled_task(contact, content, scheduled_time)
                formatted_time = scheduled_time.strftime("%Y年%m月%d日 %H:%M")
                return f"已设置提醒：将在{formatted_time}提醒您 - {content}"
            else:
                logger.error("缺少create_scheduled_task回调函数")
                return "设置提醒失败，系统错误"
                
        except Exception as e:
            logger.error(f"设置提醒时出错: {str(e)}")
            return f"设置提醒失败: {str(e)}" 