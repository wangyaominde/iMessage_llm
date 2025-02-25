"""
Agent基础模块，包含智能代理的核心功能
"""

import re
import json
import logging
from datetime import datetime
import requests

# 配置日志
logger = logging.getLogger(__name__)

def execute_agent_task(task_type, params, contact, config):
    """
    执行自动化任务
    
    支持的任务类型：
    - weather: 获取天气信息
    - news: 获取新闻摘要
    - reminder: 设置提醒
    - search: 信息搜索
    - calculate: 计算或转换
    - translate: 翻译
    """
    from .weather_agent import WeatherAgent
    from .news_agent import NewsAgent
    from .search_agent import SearchAgent
    from .calculation_agent import CalculationAgent
    from .translation_agent import TranslationAgent
    from .reminder_agent import ReminderAgent
    
    try:
        if task_type == "weather":
            agent = WeatherAgent(config)
            city = params.get("city", "北京")
            return agent.get_weather(city)
            
        elif task_type == "news":
            agent = NewsAgent(config)
            category = params.get("category", "科技")
            return agent.get_news(category)
            
        elif task_type == "reminder":
            agent = ReminderAgent(config)
            content = params.get("content", "")
            time_str = params.get("time", "")
            return agent.set_reminder(contact, content, time_str)
            
        elif task_type == "search":
            agent = SearchAgent(config)
            query = params.get("query", "")
            return agent.search(query)
            
        elif task_type == "calculate":
            agent = CalculationAgent(config)
            expression = params.get("expression", "")
            return agent.calculate(expression)
            
        elif task_type == "translate":
            agent = TranslationAgent(config)
            text = params.get("text", "")
            target_language = params.get("target_language", "英语")
            return agent.translate(text, target_language)
            
        else:
            return f"不支持的任务类型：{task_type}"
        
    except Exception as e:
        logger.error(f"执行自动任务时出错: {str(e)}")
        return f"执行任务时出错: {str(e)}"

def detect_compound_task(message):
    """
    检测复合任务，如先搜索后计算
    
    Args:
        message: 用户消息
        
    Returns:
        复合任务信息或None
    """
    # 汇率转换模式
    currency_pattern = r'([\d.]+)\s*([^\d\s]+)\s*(?:兑换|转换|换成|等于|是多少)\s*([^\d\s]+)'
    currency_match = re.search(currency_pattern, message)
    
    if currency_match:
        amount = float(currency_match.group(1))
        from_currency = currency_match.group(2)
        to_currency = currency_match.group(3)
        
        # 标准化货币名称
        from_currency = normalize_currency(from_currency)
        to_currency = normalize_currency(to_currency)
        
        if from_currency and to_currency:
            return {
                "type": "currency_conversion",
                "amount": amount,
                "from_currency": from_currency,
                "to_currency": to_currency
            }
    
    return None

def normalize_currency(currency_name):
    """
    标准化货币名称
    
    Args:
        currency_name: 货币名称
        
    Returns:
        标准化后的货币名称
    """
    # 货币名称映射
    currency_map = {
        "人民币": "CNY",
        "rmb": "CNY",
        "cny": "CNY",
        "元": "CNY",
        "美元": "USD",
        "美金": "USD",
        "usd": "USD",
        "刀": "USD",
        "欧元": "EUR",
        "eur": "EUR",
        "英镑": "GBP",
        "gbp": "GBP",
        "日元": "JPY",
        "jpy": "JPY",
        "韩元": "KRW",
        "krw": "KRW",
        "港币": "HKD",
        "港元": "HKD",
        "hkd": "HKD",
        "澳元": "AUD",
        "aud": "AUD",
        "加元": "CAD",
        "cad": "CAD",
        "新加坡元": "SGD",
        "sgd": "SGD",
        "瑞士法郎": "CHF",
        "chf": "CHF",
        "泰铢": "THB",
        "thb": "THB"
    }
    
    # 转为小写并去除空格
    normalized = currency_name.lower().strip()
    
    # 查找映射
    return currency_map.get(normalized, normalized)

def execute_compound_task(task_info, contact, config):
    """
    执行复合任务
    
    Args:
        task_info: 任务信息
        contact: 联系人
        
    Returns:
        执行结果
    """
    from .search_agent import SearchAgent
    from .calculation_agent import CalculationAgent
    
    try:
        if task_info["type"] == "currency_conversion":
            # 汇率转换
            amount = task_info["amount"]
            from_currency = task_info["from_currency"]
            to_currency = task_info["to_currency"]
            
            # 先搜索汇率
            search_agent = SearchAgent(config)
            search_query = f"{from_currency}兑{to_currency}汇率"
            search_result = search_agent.search(search_query)
            
            # 然后使用计算功能
            calc_agent = CalculationAgent(config)
            calc_prompt = f"根据以下信息，计算{amount}{from_currency}等于多少{to_currency}：\n{search_result}"
            return calc_agent.calculate_with_llm(calc_prompt)
        
        return "不支持的复合任务类型"
        
    except Exception as e:
        logger.error(f"执行复合任务时出错: {str(e)}")
        return f"执行复合任务时出错: {str(e)}"

def detect_and_execute_agent_task(message, contact, config):
    """
    识别并执行自动任务
    """
    logger.info(f"[agent_base] 开始识别并执行自动任务: {message}, 联系人: {contact}")
    
    # 检查是否是复合任务（先搜索后计算）
    compound_task = detect_compound_task(message)
    if compound_task:
        logger.info(f"[agent_base] 检测到复合任务: {compound_task}")
        return execute_compound_task(compound_task, contact, config)
    
    # 检查是否是网络搜索请求
    if message.startswith("/g "):
        search_content = message[3:].strip()
        logger.info(f"[agent_base] 检测到网络搜索请求: '{search_content}'")
        
        # 检查搜索内容是否为空
        if not search_content:
            logger.warning("[agent_base] 搜索内容为空")
            return "搜索内容不能为空，请在/g后输入要搜索的内容"
            
        # 创建搜索代理并执行搜索
        try:
            logger.info(f"[agent_base] 创建SearchAgent实例并执行搜索: '{search_content}'")
            from .search_agent import SearchAgent
            search_agent = SearchAgent(config)
            # 使用search方法，保持与SearchAgent中的处理逻辑一致
            return search_agent.search(message)
        except Exception as e:
            logger.error(f"[agent_base] 执行搜索时出错: {str(e)}")
            return f"搜索失败: {str(e)}"
    
    # 检查是否是实时性强的查询
    # 天气查询模式
    weather_pattern = r'(查询|查看|获取|告诉我|今天|明天|后天).*?(?:天气|气温|温度|下雨|下雪)'
    if re.search(weather_pattern, message):
        # 提取城市
        city_pattern = r'([\u4e00-\u9fa5]{2,}市?|[\u4e00-\u9fa5]{2,}县).*?(?:天气|气温|温度)'
        city_match = re.search(city_pattern, message)
        city = city_match.group(1) if city_match else "北京"
        
        logger.info(f"[agent_base] 检测到天气查询请求，城市: {city}")
        # 构建搜索查询并直接执行
        from .weather_agent import WeatherAgent
        weather_agent = WeatherAgent(config)
        return weather_agent.get_weather(city)
    
    # 新闻查询模式
    news_pattern = r'(查询|查看|获取|告诉我|最新|今日|今天).*?(?:新闻|资讯|热点)'
    if re.search(news_pattern, message):
        # 提取类别
        category_pattern = r'([\u4e00-\u9fa5]{2,})(?:新闻|资讯|热点)'
        category_match = re.search(category_pattern, message)
        category = category_match.group(1) if category_match else "综合"
        
        # 构建搜索查询并直接执行
        from .news_agent import NewsAgent
        news_agent = NewsAgent(config)
        return news_agent.get_news(category)
    
    # 股票和汇率查询模式
    financial_pattern = r'(股票|股价|汇率|比特币|加密货币|数字货币).*?(价格|行情|走势|多少|是多少)'
    if re.search(financial_pattern, message):
        # 直接执行搜索
        from .search_agent import SearchAgent
        search_agent = SearchAgent(config)
        return search_agent.search(message)
    
    # 检查是否是简单计算表达式
    from .calculation_agent import CalculationAgent
    calc_agent = CalculationAgent(config)
    if calc_agent.is_simple_math_expression(message):
        return calc_agent.calculate(message)
    
    # 如果规则无法识别，尝试使用大模型识别任务
    task_info = detect_task_with_llm(message, contact, config)
    if task_info:
        task_type = task_info.get("task_type")
        params = task_info.get("params", {})
        
        if task_type:
            return execute_agent_task(task_type, params, contact, config)
    
    # 提醒设置模式已在process_message中处理
    return None

def detect_task_with_llm(message, contact, config):
    """
    使用大模型识别用户消息中的任务
    
    Args:
        message: 用户消息
        contact: 联系人
        config: 配置对象
        
    Returns:
        任务信息字典或None
    """
    try:
        logger.info(f"[agent_base] 使用LLM识别任务: {message}")
        # 先检查是否是实时性强的查询
        realtime_patterns = [
            (r'(今天|明天|后天|最近|现在).*?(天气|气温|温度|下雨|下雪)', "weather"),
            (r'(最新|今日|今天|实时).*?(新闻|资讯|热点)', "news"),
            (r'(股票|股价|汇率|比特币|加密货币|数字货币).*?(价格|行情|走势|多少|是多少)', "search"),
            (r'(疫情|病例|确诊).*?(数据|情况|统计)', "search"),
            (r'(交通|路况|拥堵|堵车).*?(情况|状态)', "search"),
            (r'(航班|火车|高铁).*?(状态|延误|取消)', "search"),
            (r'(体育|比赛|足球|篮球|赛事).*?(比分|结果|赛程)', "search"),
            (r'(电影|电视剧|综艺).*?(评分|上映|播出)', "search"),
            (r'(餐厅|美食|饭店).*?(推荐|评价|地址)', "search"),
            (r'(地址|位置|怎么走|路线).*?(在哪|如何到达)', "search")
        ]
        
        for pattern, task_type in realtime_patterns:
            if re.search(pattern, message):
                logger.info(f"[agent_base] 检测到实时性查询: {message} -> {task_type}")
                
                # 对于天气查询，提取城市
                if task_type == "weather":
                    city_pattern = r'([\u4e00-\u9fa5]{2,}市?|[\u4e00-\u9fa5]{2,}县).*?(?:天气|气温|温度)'
                    city_match = re.search(city_pattern, message)
                    city = city_match.group(1) if city_match else "北京"
                    return {"task_type": task_type, "params": {"city": city}, "confidence": 0.9}
                
                # 对于新闻查询，提取类别
                elif task_type == "news":
                    category_pattern = r'([\u4e00-\u9fa5]{2,})(?:新闻|资讯|热点)'
                    category_match = re.search(category_pattern, message)
                    category = category_match.group(1) if category_match else "综合"
                    return {"task_type": task_type, "params": {"category": category}, "confidence": 0.9}
                
                # 对于其他实时查询，直接作为搜索处理
                else:
                    return {"task_type": "search", "params": {"query": message}, "confidence": 0.9}
        
        # 检查是否包含常见的搜索词
        search_keywords = ["搜索", "查询", "查找", "了解", "知道", "告诉我", "是什么", "怎么样", "如何", "多少"]
        if any(keyword in message for keyword in search_keywords):
            logger.info(f"检测到可能的搜索查询: {message}")
            return {"task_type": "search", "params": {"query": message}, "confidence": 0.8}
        
        # 构建提示
        system_prompt = """你是一个智能助手，能够识别用户消息中的任务请求。
请分析用户消息，判断是否包含以下类型的任务请求：
1. weather: 天气查询
2. news: 新闻获取
3. reminder: 设置提醒
4. search: 信息搜索
5. calculate: 计算或转换
6. translate: 翻译

对于实时性强的查询（如天气、股价、新闻等），应优先识别为相应的任务类型。
对于任何需要最新信息的查询，应优先识别为search任务类型。

如果识别到任务，请返回一个JSON格式的响应，包含以下字段：
1. task_type: 任务类型（上述类型之一）
2. params: 任务参数对象，根据不同任务类型包含不同字段
   - weather任务: {"city": "城市名"}
   - news任务: {"category": "新闻类别"}
   - reminder任务: {"content": "提醒内容", "time": "提醒时间"}
   - search任务: {"query": "搜索查询"}
   - calculate任务: {"expression": "计算表达式"}
   - translate任务: {"text": "要翻译的文本", "target_language": "目标语言"}
3. confidence: 你对识别结果的置信度（0-1之间的小数）

如果无法识别任何任务，请返回 {"task_type": null, "confidence": 0}"""
        
        user_prompt = f"请识别以下消息中的任务请求：{message}"
        
        # 调用AI模型
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": config.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.3
        }
        
        response = requests.post(config.get_full_api_url(), json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        
        # 解析响应
        ai_response = response.json()["choices"][0]["message"]["content"]
        logger.info(f"LLM任务识别响应: {ai_response}")
        
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
            
            # 检查置信度和任务类型
            if result.get("confidence", 0) < 0.6 or not result.get("task_type"):
                logger.info(f"未识别到任务或置信度低: {result}")
                return None
                
            logger.info(f"成功识别任务: {result}")
            return result
                
        except json.JSONDecodeError:
            logger.error(f"无法从AI响应中解析JSON: {ai_response}")
            return None
            
    except Exception as e:
        logger.error(f"使用LLM识别任务时出错: {str(e)}")
        return None 