"""
Agent模块包，包含各种智能代理功能
"""

from .agent_base import detect_and_execute_agent_task, detect_task_with_llm
from .weather_agent import WeatherAgent
from .news_agent import NewsAgent
from .search_agent import SearchAgent
from .calculation_agent import CalculationAgent
from .translation_agent import TranslationAgent
from .reminder_agent import ReminderAgent
from .time_parser import parse_time_with_llm, parse_time_from_message

__all__ = [
    'detect_and_execute_agent_task',
    'detect_task_with_llm',
    'WeatherAgent',
    'NewsAgent',
    'SearchAgent',
    'CalculationAgent',
    'TranslationAgent',
    'ReminderAgent',
    'parse_time_with_llm',
    'parse_time_from_message'
] 