"""
翻译代理模块，用于处理翻译请求
"""

import logging
import requests

# 配置日志
logger = logging.getLogger(__name__)

class TranslationAgent:
    """翻译代理，处理各种翻译请求"""
    
    def __init__(self, config):
        """
        初始化翻译代理
        
        Args:
            config: 配置对象，包含API密钥等信息
        """
        self.config = config
    
    def translate(self, text, target_language="英语"):
        """
        执行翻译
        
        Args:
            text: 要翻译的文本
            target_language: 目标语言，默认为英语
            
        Returns:
            翻译结果
        """
        try:
            # 获取基础系统提示词
            base_system_prompt = self.config.system_prompt
            logger.info(f"[TranslationAgent] 使用基础系统提示词: '{base_system_prompt}'")
            
            # 构建提示
            system_prompt = f"""{base_system_prompt}

你是一个翻译助手。请将用户提供的文本翻译成{target_language}。
只返回翻译结果，不要包含任何解释或其他内容。
保持原文的格式和语气。"""
            
            user_prompt = f"请翻译: {text}"
            
            logger.info(f"[TranslationAgent] 完整系统提示词: '{system_prompt}'")
            
            # 调用AI模型
            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": self.config.model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.3
            }
            
            response = requests.post(self.config.get_full_api_url(), json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            
            # 解析响应
            translation_result = response.json()["choices"][0]["message"]["content"]
            return f"翻译结果：\n{translation_result}"
            
        except Exception as e:
            logger.error(f"执行翻译时出错: {str(e)}")
            return f"翻译失败: {str(e)}" 