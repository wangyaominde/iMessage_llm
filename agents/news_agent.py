"""
新闻代理模块，用于处理新闻查询请求
"""

import logging
from .search_agent import SearchAgent

# 配置日志
logger = logging.getLogger(__name__)

class NewsAgent:
    """新闻代理，处理新闻查询请求"""
    
    def __init__(self, config):
        """
        初始化新闻代理
        
        Args:
            config: 配置对象，包含API密钥等信息
        """
        self.config = config
        self.search_agent = SearchAgent(config)
    
    def get_news(self, category="科技"):
        """
        获取指定类别的新闻信息
        
        Args:
            category: 新闻类别，默认为科技
            
        Returns:
            新闻信息
        """
        try:
            # 构建搜索查询
            search_query = f"最新{category}新闻"
            logger.info(f"执行新闻任务，自动搜索: {search_query}")
            
            # 使用搜索代理执行搜索
            return self.search_agent.execute_search_with_llm(search_query)
            
        except Exception as e:
            logger.error(f"获取新闻信息时出错: {str(e)}")
            return f"获取新闻信息失败: {str(e)}" 