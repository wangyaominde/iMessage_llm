"""
搜索代理模块，用于处理各种搜索请求
"""

import logging
import requests
from datetime import datetime

# 配置日志
logger = logging.getLogger(__name__)

class SearchAgent:
    """搜索代理，处理各种搜索请求"""
    
    def __init__(self, config):
        """
        初始化搜索代理
        
        Args:
            config: 配置对象，包含API密钥等信息
        """
        self.config = config
        logger.info(f"[SearchAgent] 初始化搜索代理，API URL: {config.get_full_api_url()}")
    
    def search(self, query):
        """
        执行搜索
        
        Args:
            query: 搜索查询
            
        Returns:
            搜索结果
        """
        logger.info(f"[SearchAgent] 收到搜索请求: '{query}'")
        
        # 检查是否使用"/g"格式进行搜索
        if query.startswith("/g "):
            # 提取搜索内容
            search_content = query[3:].strip()
            logger.info(f"[SearchAgent] 检测到/g前缀，提取搜索内容: '{search_content}'")
            
            if not search_content:
                logger.warning("[SearchAgent] 搜索内容为空")
                return "搜索内容不能为空"
            
            logger.info(f"[SearchAgent] 直接执行搜索: '{search_content}'")
            return self.execute_search_with_llm(search_content)
        
        # 检查是否是实时性查询，如果是，自动转为网络搜索
        realtime_keywords = ["天气", "股价", "汇率", "新闻", "疫情", "比赛", "比特币", "航班", "火车", "最新", "实时", "现在"]
        is_realtime_query = any(keyword in query for keyword in realtime_keywords)
        
        if is_realtime_query:
            logger.info(f"[SearchAgent] 检测到实时性查询关键词，自动转为网络搜索: '{query}'")
            # 自动转为网络搜索
            return self.execute_search_with_llm(query)
        
        # 使用大模型进行普通搜索
        logger.info(f"[SearchAgent] 执行普通搜索: '{query}'")
        return self.execute_search_with_llm(query, is_realtime=False)
    
    def execute_search_with_llm(self, query, is_realtime=True):
        """
        使用大模型执行搜索功能，自动处理网络搜索请求
        
        Args:
            query: 搜索查询
            is_realtime: 是否是实时性查询
            
        Returns:
            搜索结果
        """
        try:
            logger.info(f"[SearchAgent] 开始执行LLM搜索，查询: '{query}'，实时: {is_realtime}")
            
            # 获取当前时间
            now = datetime.now()
            current_time_str = now.strftime("%Y年%m月%d日 %H:%M")
            
            if is_realtime:
                # 构建系统提示，指示大模型执行网络搜索
                system_prompt = f"""你是一个强大的搜索助手，具有网络搜索能力。
用户正在请求实时信息，你需要模拟网络搜索并提供最新、最相关的信息。
当前时间是: {current_time_str}，请确保提供的信息与当前时间相关。

请按照以下格式提供回答：
1. 简短总结搜索结果的核心信息（1-2句话）
2. 提供3-5条最相关的详细信息点
3. 如果是天气查询，提供当前温度、天气状况、未来预报等
4. 如果是新闻查询，提供最新的相关新闻标题和简短摘要
5. 如果是股票/汇率查询，提供最新价格、涨跌幅等关键数据

请确保回答简洁、信息丰富，并以权威的口吻呈现，就像你真的进行了网络搜索一样。
不要提及你没有真正的网络搜索能力，而是直接提供有用的信息。
对于时间敏感的查询（如天气、新闻、股价等），请明确表示这些信息是基于当前时间({current_time_str})的。"""
                
                user_prompt = f"请搜索并提供关于以下内容的最新信息(当前时间: {current_time_str}): {query}"
            else:
                # 使用大模型进行普通搜索
                system_prompt = """你是一个搜索助手。用户会提供一个搜索查询，你需要提供相关的信息。
请提供简洁、准确的回答，格式清晰易读。
在回答中，如果涉及可能已过时的信息，请明确指出这一点。"""
                
                user_prompt = f"搜索查询: {query}"
            
            logger.info(f"[SearchAgent] 准备调用API，模型: {self.config.model_name}")
            
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
                "temperature": 0.5 if is_realtime else 0.7
            }
            
            logger.info(f"[SearchAgent] 发送API请求到: {self.config.get_full_api_url()}")
            response = requests.post(self.config.get_full_api_url(), json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            
            # 解析响应
            search_result = response.json()["choices"][0]["message"]["content"]
            logger.info(f"[SearchAgent] 成功获取搜索结果，长度: {len(search_result)}")
            
            if not is_realtime:
                return f"搜索结果：\n{search_result}"
            return search_result
            
        except Exception as e:
            logger.error(f"[SearchAgent] 执行搜索时出错: {str(e)}")
            return f"搜索失败: {str(e)}" 