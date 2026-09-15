"""配置：唯一的配置来源。app.py 从这里 import config / load_config / save_config。

config.json 被 gitignore；旧的 Dify 键即使残留也无害（load 时保留，不再使用）。
"""
import json
import os

CONFIG_FILE = 'config.json'

DEFAULT_CONFIG = {
    # ---- 服务运行 ----
    'is_running': False,
    'check_interval': 10,          # 轮询兜底间隔（秒）
    'use_file_watcher': True,      # 用文件监控代替纯轮询
    'use_system_watcher': True,
    'force_check_interval': 60,
    'last_message_id': 0,

    # ---- LLM provider（两套都存，provider 选当前生效的那套）----
    'provider': '',                # 'anthropic' | 'openai'，空 = 未配置
    'anthropic_api_key': '',
    'anthropic_model': 'claude-opus-5',
    'anthropic_base_url': '',
    'openai_api_key': '',
    'openai_base_url': '',          # 如 https://api.deepseek.com/v1
    'openai_model': '',             # 如 deepseek-chat
    # 端点开启联网搜索的参数名；默认空 = 不注入（DeepSeek/OpenAI 等不认识该字段会 400）。
    # 只有明确支持的端点（如 Qwen/DashScope 的 enable_search）才填。
    'openai_search_param': '',

    # ---- Agent ----
    'system_prompt': '',            # 附加系统提示词
    'max_tokens': 4096,
    'max_iters': 8,                 # harness 工具循环上限
    'history_limit': 60,            # 每用户硬上限：最近 N 条消息
    'history_hard_char_limit': 40000,  # 每用户硬上限：history 总字符数

    # ---- 安全 ----
    'bind_host': '127.0.0.1',       # 后台监听地址；非回环必须配 admin_token
    'admin_token': '',              # 后台鉴权令牌；空 = 不校验（仅建议本机）
    'reply_in_groups': False,       # 是否在群聊里回复（默认关：否则会给群里每个发言人发私信）

    # ---- 工具开关 ----
    'enable_web_search': True,      # 联网搜索总开关
    # 客户端搜索后端（非 Anthropic 后端使用）：留空=免 key 的 DuckDuckGo；
    # 也可填 serper / tavily / brave 并配上 search_api_key 提升质量。
    'search_backend': '',
    'search_api_key': '',
    'enable_memory': True,
    'enable_reminder': True,
    'enable_torrent': True,

    # ---- RAG + 空闲自动压缩（每用户隔离）----
    'enable_rag': True,
    'rag_top_k': 4,
    'rag_min_query_chars': 4,       # 短于此时不注入 RAG 召回（滚动摘要仍注入）
    'enable_auto_compress': True,
    'history_keep_recent': 8,       # 压缩后保留的近期条数
    'history_soft_limit': 20,       # 标记待压缩的条数阈值
    'history_char_budget': 12000,   # 标记待压缩的字符预算
    'compress_idle_seconds': 90,    # 距 last_active 多久算空闲
    'compress_tick_seconds': 30,    # 调度线程轮询间隔
    'compress_scan_every_ticks': 20,  # 每隔多少 tick 轻量扫一次未缓存会话
}

# 全局 config 字典（模块级单例，app.py 直接引用同一个对象）
config = DEFAULT_CONFIG.copy()


def load_config():
    """从 config.json 加载并合并进全局 config。文件不存在则写出默认值。"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config.update(json.load(f))
        except Exception as e:
            print(f"加载配置失败，使用默认配置: {e}")
    else:
        save_config()
    return config


def save_config():
    """持久化全局 config 到 config.json。"""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")


def provider_configured() -> bool:
    """当前 provider 是否已配置到可用。"""
    p = (config.get('provider') or '').strip().lower()
    if p == 'anthropic':
        return bool((config.get('anthropic_api_key') or '').strip())
    if p in ('openai', 'openai_compatible', 'openai-compatible'):
        return bool((config.get('openai_model') or '').strip())
    return False
