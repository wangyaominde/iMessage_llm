"""
时间解析模块，用于解析自然语言时间表达式
"""

import re
import json
import logging
from datetime import datetime, timedelta
import requests
from dateutil import parser

# 配置日志
logger = logging.getLogger(__name__)

def parse_time_from_message(time_str):
    """
    从消息中解析时间表达式
    
    Args:
        time_str: 时间字符串
        
    Returns:
        datetime对象或None（如果无法解析）
    """
    try:
        # 当前时间
        now = datetime.now()
        
        # 尝试直接解析
        try:
            return parser.parse(time_str, fuzzy=True)
        except:
            pass
        
        # 常见时间表达式模式
        patterns = [
            # 今天/明天/后天 + 时间
            (r'今天\s*(\d{1,2})[点:：](\d{1,2})?', lambda m: now.replace(hour=int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0)),
            (r'明天\s*(\d{1,2})[点:：](\d{1,2})?', lambda m: (now + timedelta(days=1)).replace(hour=int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0)),
            (r'后天\s*(\d{1,2})[点:：](\d{1,2})?', lambda m: (now + timedelta(days=2)).replace(hour=int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0)),
            
            # X分钟/小时/天后
            (r'(\d+)\s*分钟后', lambda m: now + timedelta(minutes=int(m.group(1)))),
            (r'(\d+)\s*小时后', lambda m: now + timedelta(hours=int(m.group(1)))),
            (r'(\d+)\s*天后', lambda m: now + timedelta(days=int(m.group(1)))),
            
            # 下午/晚上 + 时间
            (r'今天\s*下午\s*(\d{1,2})[点:：](\d{1,2})?', lambda m: now.replace(hour=int(m.group(1)) + 12 if int(m.group(1)) < 12 else int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0)),
            (r'明天\s*下午\s*(\d{1,2})[点:：](\d{1,2})?', lambda m: (now + timedelta(days=1)).replace(hour=int(m.group(1)) + 12 if int(m.group(1)) < 12 else int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0)),
            
            # 简单时间格式
            (r'(\d{1,2})[点:：](\d{1,2})?', lambda m: now.replace(hour=int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0)),
            (r'下午\s*(\d{1,2})[点:：](\d{1,2})?', lambda m: now.replace(hour=int(m.group(1)) + 12 if int(m.group(1)) < 12 else int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0)),
            (r'晚上\s*(\d{1,2})[点:：](\d{1,2})?', lambda m: now.replace(hour=int(m.group(1)) + 12 if int(m.group(1)) < 12 else int(m.group(1)), minute=int(m.group(2) or 0), second=0, microsecond=0)),
            
            # 日期格式
            (r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?\s*(\d{1,2})[点:：](\d{1,2})?', 
             lambda m: datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5) or 0)))
        ]
        
        # 尝试匹配模式
        for pattern, time_func in patterns:
            match = re.search(pattern, time_str)
            if match:
                parsed_time = time_func(match)
                
                # 如果解析出的时间已经过去，且是今天的时间，则可能是指明天
                if parsed_time < now and "今天" not in time_str and "明天" not in time_str and "后天" not in time_str and (now - parsed_time).days < 1:
                    parsed_time += timedelta(days=1)
                
                return parsed_time
        
        return None
        
    except Exception as e:
        logger.error(f"解析时间表达式时出错: {str(e)}")
        return None

def parse_time_with_llm(time_expression, context=None, config=None):
    """
    使用大模型解析时间表达式
    
    Args:
        time_expression: 用户输入的时间表达式
        context: 可选的上下文信息，如对话历史
        config: 配置信息
        
    Returns:
        datetime对象或None（如果无法解析）
    """
    try:
        # 当前时间作为参考
        now = datetime.now()
        current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        
        # 构建提示
        system_prompt = """你是一个专门解析时间表达式的AI助手。
你的任务是将用户输入的自然语言时间表达式转换为标准的时间格式。
请分析输入的时间表达式，并返回一个JSON格式的响应，包含以下字段：
1. parsed_time: ISO格式的时间字符串（YYYY-MM-DDTHH:MM:SS）
2. confidence: 你对解析结果的置信度（0-1之间的小数）
3. reasoning: 简短解释你是如何理解这个时间表达式的

如果无法解析，请将parsed_time设为null，并在reasoning中解释原因。
当前时间是: {current_time}"""
        
        user_prompt = f"请解析以下时间表达式：{time_expression}"
        
        # 添加上下文（如果有）
        if context:
            user_prompt += f"\n上下文信息：{context}"
        
        # 调用AI模型
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": config.model_name,
            "messages": [
                {"role": "system", "content": system_prompt.format(current_time=current_time_str)},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2  # 使用较低的温度以获得更确定的回答
        }
        
        response = requests.post(config.get_full_api_url(), json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        
        # 解析响应
        ai_response = response.json()["choices"][0]["message"]["content"]
        logger.info(f"LLM时间解析响应: {ai_response}")
        
        # 尝试从响应中提取JSON
        try:
            # 查找JSON部分
            json_match = re.search(r'```json\s*(.*?)\s*```', ai_response, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                # 尝试直接解析整个响应
                json_str = ai_response
                
            result = json.loads(json_str)
            
            # 检查置信度
            if result.get("confidence", 0) < 0.5:
                logger.warning(f"时间解析置信度低: {result.get('confidence')}, 原因: {result.get('reasoning')}")
                return None
                
            # 解析ISO格式时间
            if result.get("parsed_time"):
                parsed_time = parser.parse(result["parsed_time"])
                logger.info(f"成功解析时间表达式: {time_expression} -> {parsed_time}, 置信度: {result.get('confidence')}")
                return parsed_time
            else:
                logger.warning(f"无法解析时间表达式: {time_expression}, 原因: {result.get('reasoning')}")
                return None
                
        except json.JSONDecodeError:
            logger.error(f"无法从AI响应中解析JSON: {ai_response}")
            
            # 尝试直接从文本中提取时间
            try:
                # 查找可能的日期时间格式
                dt_match = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', ai_response)
                if dt_match:
                    parsed_time = parser.parse(dt_match.group(0))
                    logger.info(f"从文本中提取到时间: {parsed_time}")
                    return parsed_time
            except:
                pass
                
            return None
            
    except Exception as e:
        logger.error(f"使用LLM解析时间时出错: {str(e)}")
        return None 