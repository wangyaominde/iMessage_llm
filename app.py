import os
import subprocess
import time
import threading
import logging
from collections import deque
from datetime import datetime

from flask import Flask, render_template, request, jsonify, redirect, url_for

from imessage_reader import iMessageReader
from config import config, load_config, save_config, provider_configured
from text_format import clean_reply
from providers.base import build_provider
from providers.base import Message as ProviderMessage
from agent.manager import AgentManager
from tools.base import ToolRegistry

# 禁用 Flask 默认日志
logging.getLogger('werkzeug').setLevel(logging.ERROR)
app = Flask(__name__)
app.logger.disabled = True

STATE_DIR = 'agent_state'

# ---- 全局状态 ----
check_thread = None
stop_event = threading.Event()
message_reader = None
message_reader_thread = None
agent_manager = None
reminder_thread = None
_send_lock = threading.Lock()   # 全局发送锁：任何时刻只有一个 osascript 在发消息

# ---- 失败发送重试队列 ----
retry_queue = deque()
RETRY_MAX_ATTEMPTS = 5
RETRY_INTERVAL_SECONDS = 30
_last_retry_run = 0.0
_retry_lock = threading.Lock()

# ---- 日志 ----
log_entries = deque(maxlen=100)


def add_log(message, level='info'):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entries.append({'timestamp': timestamp, 'message': message, 'level': level})
    if level in ('success', 'error', 'warning'):
        print(f"[{level}] {message}")


def enqueue_retry(phone, message, last_error=""):
    with _retry_lock:
        retry_queue.append({'phone': phone, 'message': message, 'attempts': 0,
                            'added_at': time.time(), 'last_error': last_error})
    add_log(f"入重试队列: {phone}（队列长度 {len(retry_queue)}）", 'warning')


def retry_pending_messages():
    global _last_retry_run
    now = time.time()
    if now - _last_retry_run < RETRY_INTERVAL_SECONDS:
        return
    _last_retry_run = now
    with _retry_lock:
        snapshot = list(retry_queue)[:3]
    if not snapshot:
        return
    for item in snapshot:
        try:
            ok, err = send_imessage(item['phone'], item['message'])
            if ok:
                with _retry_lock:
                    try:
                        retry_queue.remove(item)
                    except ValueError:
                        pass
                add_log(f"重试发送成功: {item['phone']}", 'success')
            else:
                item['attempts'] += 1
                item['last_error'] = err
                if item['attempts'] >= RETRY_MAX_ATTEMPTS:
                    with _retry_lock:
                        try:
                            retry_queue.remove(item)
                        except ValueError:
                            pass
                    add_log(f"重试{RETRY_MAX_ATTEMPTS}次后放弃: {item['phone']}（{err[:80]}）", 'error')
        except Exception as e:
            item['attempts'] += 1
            add_log(f"重试异常: {item['phone']} -> {e}", 'error')


# ---- 回复文本清理 ----
def process_reply_text(text):
    """出站清洗：剥思维链 + 把 Markdown 降级成纯文本（iMessage 不渲染 Markdown）。"""
    return clean_reply(text)


# ---- iMessage 发送 ----
def send_imessage(phone_number, message):
    """用 AppleScript 发 iMessage。全局串行，避免超时 pkill 误杀并发中的另一次发送。"""
    message = process_reply_text(message)
    with _send_lock:
        try:
            result = subprocess.run(
                ['osascript', 'send_message.applescript', phone_number, message],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
            return False, result.stderr.strip()
        except subprocess.TimeoutExpired:
            add_log("Messages 发送超时，强制重启 Messages.app", 'error')
            for cmd in (['pkill', '-9', '-f', 'send_message.applescript'],
                        ['osascript', '-e', 'tell application "Messages" to quit']):
                try:
                    subprocess.run(cmd, timeout=5)
                except Exception:
                    pass
            time.sleep(1)
            try:
                subprocess.run(['open', '-a', 'Messages'], timeout=5)
            except Exception:
                pass
            return False, "send timeout (30s)"
        except Exception as e:
            return False, str(e)


def deliver(phone, text):
    """发送 + 失败入重试队列。供 AgentManager 回调。"""
    ok, res = send_imessage(phone, text)
    if not ok:
        add_log(f"发送失败: {res}（已入重试队列）", 'error')
        enqueue_retry(phone, text, last_error=res)


# ---- 工具注册表（按配置构造；memory/reminder/torrent 在后续阶段接入）----
def build_registry():
    reg = ToolRegistry()
    # 联网搜索的三种情况：
    #  1) Anthropic：有原生 server tool，交给 provider 层，这里不注册
    #  2) OpenAI 兼容端点且填了「联网搜索参数名」（如 MiniMax 的 web_search_linkup）：
    #     用端点自带搜索，这里也不注册，免得两套搜索打架
    #  3) 其余（多数 OpenAI 兼容端点没有内置搜索）：注册客户端工具，换模型也不丢联网能力
    try:
        if config.get('enable_web_search'):
            provider = (config.get('provider') or '').lower()
            has_native = (provider == 'anthropic') or bool((config.get('openai_search_param') or '').strip())
            if not has_native:
                from tools.web import make_web_tools
                for t in make_web_tools():
                    reg.register(t)
    except Exception as e:
        print(f"注册 web 工具失败: {e}")
    try:
        if config.get('enable_memory'):
            from tools.memory import make_memory_tools
            for t in make_memory_tools():
                reg.register(t)
    except Exception as e:
        print(f"注册 memory 工具失败: {e}")
    try:
        if config.get('enable_reminder'):
            from tools.reminder import make_reminder_tools
            for t in make_reminder_tools():
                reg.register(t)
    except Exception as e:
        print(f"注册 reminder 工具失败: {e}")
    try:
        if config.get('enable_torrent'):
            from tools.torrent import make_torrent_tools
            for t in make_torrent_tools():
                reg.register(t)
    except Exception as e:
        print(f"注册 torrent 工具失败: {e}")
    return reg


def get_manager():
    global agent_manager
    if agent_manager is None:
        agent_manager = AgentManager(STATE_DIR, config, build_registry, deliver, add_log)
        agent_manager.services['send'] = send_imessage
    return agent_manager


# ---- reader 回调：把一批新消息交给 AgentManager（非阻塞入池）----
def on_new_messages(messages):
    try:
        return get_manager().dispatch_batch(messages)
    except Exception as e:
        add_log(f"分发消息出错: {e}", 'error')
        return False


# ---- 消息监控启停 ----
def start_message_reader():
    global message_reader, message_reader_thread
    if message_reader_thread is not None:
        stop_message_reader()
    try:
        message_reader = iMessageReader()
        if not message_reader.check_db_access():
            add_log("无法访问 iMessage 数据库，请确保已授予完全磁盘访问权限", 'error')
            return False
        message_reader_thread = threading.Thread(
            target=message_reader.monitor_messages, args=(on_new_messages,), daemon=True)
        message_reader_thread.start()
        add_log("已启动 iMessage 消息监控", 'success')
        return True
    except Exception as e:
        add_log(f"启动消息监控失败: {e}", 'error')
        message_reader = None
        message_reader_thread = None
        return False


def stop_message_reader():
    global message_reader, message_reader_thread
    if message_reader_thread is None:
        return
    try:
        if message_reader:
            message_reader.stop()
    except Exception as e:
        add_log(f"停止消息监控失败: {e}", 'error')
    message_reader = None
    message_reader_thread = None
    add_log("已停止 iMessage 消息监控", 'warning')


def message_checker():
    """后台线程：监控存活自愈 + 周期重试发送失败的消息。"""
    last_reader_retry = 0
    reader_retry_interval = 300
    while not stop_event.is_set():
        now = time.time()
        if config['is_running']:
            # 线程为 None 或已死（reader 自己崩了）都要自愈重启
            reader_dead = message_reader_thread is None or not message_reader_thread.is_alive()
            if reader_dead and (now - last_reader_retry) > reader_retry_interval:
                stop_message_reader()
                time.sleep(1)
                start_message_reader()
                last_reader_retry = now
            retry_pending_messages()
        stop_event.wait(min(5, config.get('check_interval', 10)))


# ---- 提醒调度线程（Phase 4 接入具体逻辑）----
def start_reminder_scheduler():
    global reminder_thread
    try:
        from tools.reminder import scheduler_tick
    except Exception:
        return  # reminder 工具还没接入
    def _loop():
        while not stop_event.is_set():
            # 只在服务运行且提醒工具开启时触发；否则线程存活但不发消息，
            # 这样后台点「停止服务」或关掉提醒开关能即时生效。
            if config.get('is_running') and config.get('enable_reminder'):
                try:
                    scheduler_tick(get_manager())
                except Exception as e:
                    print(f"reminder 调度出错: {e}")
            stop_event.wait(20)
    reminder_thread = threading.Thread(target=_loop, daemon=True)
    reminder_thread.start()


# ================= 路由 =================
@app.route('/')
def index():
    return render_template('index.html', config=config)


@app.route('/save_config', methods=['POST'])
def save_config_route():
    f = request.form

    def upd(key, default=None, cast=None):
        if key in f:
            val = f.get(key)
            if cast:
                try:
                    val = cast(val)
                except Exception:
                    return
            config[key] = val

    # provider
    upd('provider')
    upd('anthropic_api_key'); upd('anthropic_model'); upd('anthropic_base_url')
    upd('openai_api_key'); upd('openai_base_url'); upd('openai_model'); upd('openai_search_param')
    upd('search_backend'); upd('search_api_key')
    # agent
    upd('system_prompt')
    upd('max_tokens', cast=int); upd('max_iters', cast=int); upd('history_limit', cast=int)
    # 服务
    upd('check_interval', cast=int); upd('force_check_interval', cast=int)
    check_mode = f.get('check_mode')
    if check_mode is not None:
        config['use_file_watcher'] = (check_mode == 'file_watcher')

    # 复选框（表单里没勾就是关）——只有当表单确实提交了配置页时才据此更新
    if 'config_submitted' in f:
        config['enable_web_search'] = 'enable_web_search' in f
        config['enable_memory'] = 'enable_memory' in f
        config['enable_reminder'] = 'enable_reminder' in f
        config['enable_torrent'] = 'enable_torrent' in f
        config['use_system_watcher'] = 'use_system_watcher' in f
        config['reply_in_groups'] = 'reply_in_groups' in f

    save_config()
    add_log("配置已保存", 'success')
    return redirect(url_for('index'))


@app.route('/user_sessions')
def user_sessions():
    sessions = get_manager().list_sessions()
    return render_template('user_sessions.html', sessions=sessions)


@app.route('/reset_user_session/<phone_number>', methods=['POST'])
def reset_user_session(phone_number):
    get_manager().reset_session(phone_number)
    add_log(f"已清空用户 {phone_number} 的对话历史（保留长期记忆）", 'success')
    return redirect(url_for('user_sessions'))


@app.route('/delete_user_session/<phone_number>', methods=['POST'])
def delete_user_session(phone_number):
    ok = get_manager().delete_session(phone_number)
    add_log(f"删除用户 {phone_number} 会话{'成功' if ok else '失败'}", 'success' if ok else 'warning')
    return redirect(url_for('user_sessions'))


@app.route('/clear_all_sessions', methods=['POST'])
def clear_all_sessions():
    n = get_manager().clear_all()
    add_log(f"已清空全部 {n} 个用户会话", 'success')
    return redirect(url_for('user_sessions'))


@app.route('/test_connection', methods=['POST'])
def test_connection():
    if not provider_configured():
        return jsonify({'success': False, 'message': '当前 provider 未配置好'})
    try:
        provider = build_provider(config)
        resp = provider.chat([ProviderMessage(role='user', content='回复“ok”两个字即可')], None)
        txt = (resp.text or '').strip()
        return jsonify({'success': True, 'message': f"连接成功，模型回复：{txt[:40]}"})
    except Exception as e:
        return jsonify({'success': False, 'message': f"连接失败: {e}"})


@app.route('/get_logs', methods=['GET'])
def get_logs():
    return jsonify(list(log_entries))


@app.route('/clear_logs', methods=['POST'])
def clear_logs():
    log_entries.clear()
    return jsonify({'success': True})


@app.route('/toggle_service', methods=['POST'])
def toggle_service():
    config['is_running'] = not config['is_running']
    if config['is_running']:
        if start_message_reader():
            add_log("服务已启动", 'success')
        else:
            add_log("服务已启动但消息监控失败，请检查权限", 'warning')
    else:
        stop_message_reader()
        add_log("服务已停止", 'warning')
    save_config()
    return jsonify({'is_running': config['is_running']})


@app.route('/get_status', methods=['GET'])
def get_status():
    return jsonify({
        'is_running': config['is_running'],
        'provider': config.get('provider', ''),
        'provider_configured': provider_configured(),
    })


@app.route('/reset_last_message', methods=['POST'])
def reset_last_message():
    """兼容旧按钮：重置内部游标基准（重启监控使其从最新开始）。"""
    reset_type = request.form.get('reset_type', 'zero')
    if reset_type == 'latest':
        config['last_message_id'] = int(time.time())
    else:
        config['last_message_id'] = 0
    save_config()
    if config['is_running']:
        start_message_reader()  # 重启后 reader 会以当前最新时间为游标
    add_log("已重置消息游标基准", 'success')
    return jsonify({'success': True, 'message': '已重置', 'last_message_id': config['last_message_id']})


@app.route('/force_check', methods=['POST'])
def force_check():
    if not config['is_running']:
        return jsonify({'success': False, 'message': '服务未运行'})
    return jsonify({'success': True, 'message': 'reader 会自动检测新消息'})


def start_app():
    global stop_event, check_thread
    load_config()
    get_manager()  # 预热（触发旧会话迁移）
    stop_event = threading.Event()
    check_thread = threading.Thread(target=message_checker, daemon=True)
    check_thread.start()
    start_reminder_scheduler()
    if config['is_running']:
        if not start_message_reader():
            add_log("启动消息监控失败，请检查权限", 'error')
    print("iMessage Agent 服务已启动，访问 http://127.0.0.1:8877 进行配置")
    app.run(host='0.0.0.0', port=8877, debug=False, use_reloader=False)


def cleanup():
    stop_event.set()
    if check_thread:
        check_thread.join(timeout=1)
    stop_message_reader()


if __name__ == '__main__':
    try:
        start_app()
    finally:
        cleanup()
