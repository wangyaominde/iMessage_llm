"""
天气代理模块，用于处理天气查询请求
"""

import logging
from .search_agent import SearchAgent

# 配置日志
logger = logging.getLogger(__name__)

class WeatherAgent:
    """天气代理，处理天气查询请求"""
    
    def __init__(self, config):
        """
        初始化天气代理
        
        Args:
            config: 配置对象，包含API密钥等信息
        """
        self.config = config
        self.search_agent = SearchAgent(config)
    
    def get_weather(self, city="上海"):
        """
        获取指定城市的天气信息
        
        Args:
            city: 城市名称，默认为北京
            
        Returns:
            天气信息
        """
        try:
            # 构建搜索查询
            search_query = f"{city}天气预报"
            logger.info(f"执行天气任务，自动搜索: {search_query}")
            
            # 使用搜索代理执行搜索
            return self.search_agent.execute_search_with_llm(search_query)
            
        except Exception as e:
            logger.error(f"获取天气信息时出错: {str(e)}")
            return f"获取天气信息失败: {str(e)}" 