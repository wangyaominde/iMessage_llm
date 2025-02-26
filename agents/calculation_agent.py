"""
计算代理模块，用于处理计算请求
"""

import logging
import requests
import re

# 配置日志
logger = logging.getLogger(__name__)

class CalculationAgent:
    """计算代理，处理各种计算请求"""
    
    def __init__(self, config):
        """
        初始化计算代理
        
        Args:
            config: 配置对象，包含API密钥等信息
        """
        self.config = config
    
    def calculate(self, expression):
        """
        执行计算
        
        Args:
            expression: 计算表达式
            
        Returns:
            计算结果
        """
        try:
            # 检查是否是简单的数学表达式
            if self.is_simple_math_expression(expression):
                # 使用macOS的计算功能
                result = self.calculate_with_macos(expression)
                if result is not None:
                    return f"计算结果: {result}"
            
            # 使用大模型进行计算
            return self.calculate_with_llm(expression)
            
        except Exception as e:
            logger.error(f"执行计算时出错: {str(e)}")
            return f"计算失败: {str(e)}"
    
    def is_simple_math_expression(self, expression):
        """
        判断是否为简单的数学表达式
        
        Args:
            expression: 表达式字符串
            
        Returns:
            是否为简单数学表达式
        """
        # 移除空格
        expr = expression.replace(" ", "")
        
        # 检查是否只包含数字、基本运算符和括号
        valid_chars = set("0123456789+-*/().^%<>=")
        return all(c in valid_chars for c in expr) and any(c in "0123456789" for c in expr)
    
    def calculate_with_macos(self, expression):
        """
        使用macOS的计算功能进行计算
        
        Args:
            expression: 计算表达式
            
        Returns:
            计算结果或None（如果计算失败）
        """
        try:
            # 替换^为**以支持幂运算
            expression = expression.replace("^", "**")
            
            # 使用Python的eval进行计算
            # 注意：在生产环境中应该更加谨慎地使用eval
            result = eval(expression)
            
            return result
        except Exception as e:
            logger.error(f"使用macOS计算时出错: {str(e)}")
            return None
    
    def calculate_with_llm(self, expression):
        """
        使用大模型执行计算
        
        Args:
            expression: 计算表达式
            
        Returns:
            计算结果
        """
        try:
            # 获取基础系统提示词
            base_system_prompt = self.config.system_prompt
            logger.info(f"[CalculationAgent] 使用基础系统提示词: '{base_system_prompt}'")
            
            # 构建提示
            system_prompt = f"""{base_system_prompt}

你是一个计算助手。用户会提供一个计算表达式或单位转换请求，你需要计算结果。
请确保计算准确，并清晰地展示计算过程和最终结果。
对于单位转换，请说明转换关系。
如果是复杂的实时数据计算（如货币汇率转换），请建议用户先使用"/g 查询汇率"获取最新汇率，然后再进行计算。"""
            
            user_prompt = f"计算: {expression}"
            
            logger.info(f"[CalculationAgent] 完整系统提示词: '{system_prompt}'")
            
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
            calculation_result = response.json()["choices"][0]["message"]["content"]
            return f"计算结果：\n{calculation_result}"
            
        except Exception as e:
            logger.error(f"使用LLM执行计算时出错: {str(e)}")
            return f"计算失败: {str(e)}" 