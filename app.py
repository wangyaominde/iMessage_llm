import os
import json
import sqlite3
import subprocess
import time
import threading
import requests
import logging
import hashlib
import signal
from collections import deque
from flask import Flask, render_template, request, jsonify, redirect, url_for
from datetime import datetime
import re
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
# 导入imessage_reader模块
from imessage_reader import iMessageReader

# 禁用Flask的默认日志
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)
# 禁用Flask的请求日志
app.logger.disabled = True
# 设置日志级别为ERROR，只显示错误信息
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# 配置文件路径
CONFIG_FILE = 'config.json'
USER_SESSIONS_FILE = 'user_sessions.json'

# 默认配置
DEFAULT_CONFIG = {
    'dify_url': '',
    'dify_api_key': '',
    'check_interval': 10,  # 检查新消息的间隔（秒）
    'last_message_id': 0,  # 上次检查的最后一条消息ID
    'is_running': False,   # 是否正在运行检查
    'dify_response_mode': 'blocking',  # 响应模式：blocking或streaming
    'enable_image_detection': True,    # 是否启用图片链接检测
    'use_file_watcher': True,          # 是否使用文件监控代替轮询
    'force_check_interval': 60,        # 强制检查间隔（秒），即使使用文件监控也会定期检查
    'auto_user_sessions': True,        # 是否自动为每个用户创建会话（始终为true）
    'use_system_watcher': True         # 是否使用系统级文件监控（更快速）
}

# 全局变量
config = DEFAULT_CONFIG.copy()
check_thread = None
stop_event = threading.Event()
message_reader = None
message_reader_thread = None
processing_lock = threading.Lock()  # 防止并发处理同一批消息

# 日志记录
log_entries = deque(maxlen=100)  # 最多保存100条日志

def add_log(message, level='info'):
    """添加日志"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = {
        'timestamp': timestamp,
        'message': message,
        'level': level
    }
    log_entries.append(log_entry)
    
    # 只输出消息处理和回复相关的日志
    if '成功回复消息' in message:
        print(f"成功: {message}")
    elif '处理消息' in message and level == 'success':
        print(f"处理: {message}")
    elif '检测到' in message and '条新消息' in message and level == 'success':
        print(f"检测: {message}")
    elif level == 'error' and ('处理消息错误' in message or '发送消息失败' in message):
        print(f"错误: {message}")

class UserSessionManager:
    """管理iMessage用户的会话信息"""
    
    def __init__(self, sessions_file):
        self.sessions_file = sessions_file
        self.sessions = {}
        self.load_sessions()
    
    def load_sessions(self):
        """加载用户会话信息"""
        if os.path.exists(self.sessions_file):
            try:
                with open(self.sessions_file, 'r') as f:
                    self.sessions = json.load(f)
                add_log(f"已加载 {len(self.sessions)} 个用户会话", 'info')
            except Exception as e:
                add_log(f"加载用户会话失败: {str(e)}", 'error')
                self.sessions = {}
    
    def save_sessions(self):
        """保存用户会话信息"""
        try:
            with open(self.sessions_file, 'w') as f:
                json.dump(self.sessions, f, indent=4)
        except Exception as e:
            add_log(f"保存用户会话失败: {str(e)}", 'error')
    
    def get_user_session(self, phone_number):
        """获取用户会话信息，如果不存在则创建"""
        if phone_number not in self.sessions:
            # 为新用户创建会话信息
            user_id = f"imessage-{self._generate_user_id(phone_number)}"
            self.sessions[phone_number] = {
                'user_id': user_id,
                'conversation_id': '',
                'last_active': datetime.now().isoformat()
            }
            add_log(f"为用户 {phone_number} 创建新会话，用户ID: {user_id}", 'info')
            self.save_sessions()
        else:
            # 更新最后活动时间
            self.sessions[phone_number]['last_active'] = datetime.now().isoformat()
        
        return self.sessions[phone_number]
    
    def update_conversation_id(self, phone_number, conversation_id):
        """更新用户的会话ID"""
        if phone_number in self.sessions:
            self.sessions[phone_number]['conversation_id'] = conversation_id
            self.sessions[phone_number]['last_active'] = datetime.now().isoformat()
            self.save_sessions()
            add_log(f"更新用户 {phone_number} 的会话ID: {conversation_id}", 'info')
    
    def clear_all_sessions(self):
        """清空所有用户会话数据"""
        old_count = len(self.sessions)
        old_sessions = self.sessions.copy()  # 保存一份副本用于日志
        
        # 记录详细日志
        add_log(f"准备清空所有用户会话数据，当前共有 {old_count} 个会话", 'info')
        for phone, session in old_sessions.items():
            add_log(f"将删除用户 {phone} 的会话数据，用户ID: {session['user_id']}, 会话ID: {session.get('conversation_id', '无')}", 'info')
        
        self.sessions = {}
        self.save_sessions()
        add_log(f"已清空所有用户会话数据，共 {old_count} 个会话", 'success')
        return old_count
    
    def delete_user_session(self, phone_number):
        """完全删除指定用户的会话数据"""
        if phone_number in self.sessions:
            session = self.sessions[phone_number]
            add_log(f"准备删除用户 {phone_number} 的会话数据，用户ID: {session['user_id']}, 会话ID: {session.get('conversation_id', '无')}", 'info')
            del self.sessions[phone_number]
            self.save_sessions()
            add_log(f"已成功删除用户 {phone_number} 的会话数据", 'success')
            return True
        else:
            add_log(f"尝试删除不存在的用户 {phone_number} 的会话数据", 'warning')
            return False
    
    def _generate_user_id(self, phone_number):
        """生成唯一的用户ID"""
        # 使用电话号码的哈希值作为用户ID的一部分
        hash_obj = hashlib.md5(phone_number.encode())
        hash_hex = hash_obj.hexdigest()[:8]
        return f"user-{hash_hex}"
    
    def get_all_sessions(self):
        """获取所有用户会话信息"""
        return self.sessions

# 初始化用户会话管理器
user_session_manager = UserSessionManager(USER_SESSIONS_FILE)

def load_config():
    """加载配置文件"""
    global config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config.update(json.load(f))
    else:
        save_config()

def save_config():
    """保存配置文件"""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

def test_dify_connection():
    """测试Dify API连接"""
    if not config['dify_url'] or not config['dify_api_key']:
        return False, "URL或API密钥未设置"
    
    # 验证URL格式
    url = config['dify_url'].strip()
    if not url.startswith(('http://', 'https://')):
        return False, "URL格式错误：必须以 http:// 或 https:// 开头"
    
    try:
        headers = {
            "Authorization": f"Bearer {config['dify_api_key']}",
            "Content-Type": "application/json"
        }
        # 尝试获取应用信息，这是一个简单的API调用来测试连接
        response = requests.get(
            f"{url.rstrip('/')}/applications/current",
            headers=headers
        )
        if response.status_code == 200:
            return True, "连接成功"
        else:
            return False, f"连接失败: {response.status_code} - {response.text}"
    except Exception as e:
        return False, f"连接错误: {str(e)}"

def get_imessage_db_path():
    """获取iMessage数据库路径"""
    return os.path.expanduser("~/Library/Messages/chat.db")

def send_imessage(phone_number, message):
    """使用AppleScript发送iMessage"""
    try:
        # 确保消息不包含多余的空行
        message = process_reply_text(message)
        
        result = subprocess.run(
            ['osascript', 'send_message.applescript', phone_number, message],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip()
    except Exception as e:
        return False, str(e)

# 处理新消息的回调函数
def on_new_messages(messages):
    """处理从imessage_reader接收到的新消息"""
    print(f"收到 {len(messages)} 条新消息")
    
    # 使用锁防止并发处理
    if processing_lock.acquire(blocking=False):
        try:
            process_messages(messages)
        finally:
            processing_lock.release()
    else:
        print("已有消息处理任务在进行中，跳过本次处理")

def process_messages(messages):
    """处理消息列表"""
    try:
        if messages:
            add_log(f"检测到 {len(messages)} 条新消息", 'success')
            
        for message in messages:
            # 跳过自己发送的消息
            if message['is_from_me']:
                continue
                
            # 记录详细的消息信息
            group_info = f" (群聊: {message['group_chat']})" if message.get('group_chat') else ""
            print(f"处理消息: 收到消息: 来自 {message['contact']}{group_info}, 内容: '{message['text'][:30]}...'")
            add_log(f"收到消息: 来自 {message['contact']}{group_info}, 内容: '{message['text'][:30]}...'", 'success')
            
            # 使用Dify处理消息
            print(f"处理消息: 开始处理消息")
            reply = process_with_dify(message['text'], message['contact'])
            
            # 只有当成功获取到回复时才发送
            if reply:
                print(f"处理消息: 获取到回复，准备发送")
                success, result = send_imessage(message['contact'], reply)
                if not success:
                    print(f"处理消息: 发送失败: {result}")
                    add_log(f"发送消息失败: {result}", 'error')
                else:
                    print(f"处理消息: 成功发送回复给 {message['contact']}")
                    add_log(f"成功回复消息给 {message['contact']}", 'success')
            else:
                print(f"处理消息: 处理消息时出错，跳过发送")
                add_log(f"跳过发送回复，因为处理消息时出错", 'warning')
    except Exception as e:
        print(f"处理消息: 错误: {str(e)}")
        add_log(f"处理新消息错误: {str(e)}", 'error')

def process_with_dify(message_text, phone_number=None):
    """使用Dify处理消息并获取回复"""
    if not config['dify_url'] or not config['dify_api_key']:
        add_log("Dify未配置，无法处理消息", 'error')
        return None
    
    # 验证URL格式
    url = config['dify_url'].strip()
    if not url.startswith(('http://', 'https://')):
        add_log("Dify URL格式错误：必须以 http:// 或 https:// 开头", 'error')
        return None
    
    try:
        headers = {
            "Authorization": f"Bearer {config['dify_api_key']}",
            "Content-Type": "application/json"
        }
        
        # 准备请求数据
        request_data = {
            "inputs": {},
            "query": message_text,
            "response_mode": config['dify_response_mode']
        }
        
        # 处理用户ID和会话ID
        if phone_number:
            # 使用用户特定的会话信息
            user_session = user_session_manager.get_user_session(phone_number)
            request_data["user"] = user_session['user_id']
            
            # 如果有会话ID，添加到请求中
            if user_session['conversation_id']:
                request_data["conversation_id"] = user_session['conversation_id']
        else:
            # 如果没有电话号码，使用临时用户ID
            request_data["user"] = f"imessage-temp-{int(time.time())}"
        
        # 如果启用了图片链接检测，检查消息中的图片链接
        if config['enable_image_detection']:
            image_url_pattern = r'https?://\S+\.(jpg|jpeg|png|gif|webp)'
            image_urls = re.findall(image_url_pattern, message_text)
            
            if image_urls:
                files = []
                for url in image_urls[:5]:  # 限制最多5个图片
                    files.append({
                        "type": "image",
                        "transfer_method": "remote_url",
                        "url": url
                    })
                if files:
                    request_data["files"] = files
        
        # 如果使用流式响应模式，需要特殊处理
        if config['dify_response_mode'] == 'streaming':
            # 流式响应模式下，我们需要收集所有的响应片段
            response = requests.post(
                f"{url.rstrip('/')}/chat-messages",
                headers=headers,
                json=request_data,
                timeout=60,  # 流式响应可能需要更长的超时时间
                stream=True
            )
            
            if response.status_code == 200:
                # 收集所有的响应片段
                full_answer = ""
                conversation_id = None
                
                for line in response.iter_lines():
                    if line:
                        try:
                            # 解析SSE格式的响应
                            line_text = line.decode('utf-8')
                            if line_text.startswith('data:'):
                                data_json = json.loads(line_text[5:])
                                if 'answer' in data_json:
                                    full_answer += data_json['answer']
                                # 提取会话ID
                                if 'conversation_id' in data_json and not conversation_id:
                                    conversation_id = data_json['conversation_id']
                        except Exception as e:
                            add_log(f"解析流式响应错误: {str(e)}", 'error')
                
                # 如果获取到了会话ID，更新用户会话
                if conversation_id and phone_number:
                    user_session_manager.update_conversation_id(phone_number, conversation_id)
                
                if full_answer:
                    # 处理回复，去除多余的空行
                    full_answer = process_reply_text(full_answer)
                    add_log(f"成功处理消息: '{message_text[:30]}...'", 'success')
                    return full_answer
                else:
                    add_log("无法从流式响应中获取有效回复", 'error')
                    return None
            else:
                add_log(f"Dify API错误: {response.status_code}", 'error')
                return None
        else:
            # 阻塞模式，直接获取完整响应
            response = requests.post(
                f"{url.rstrip('/')}/chat-messages",
                headers=headers,
                json=request_data,
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                # 从Dify响应中提取回复文本
                if 'answer' in data:
                    # 如果获取到了会话ID，更新用户会话
                    if 'conversation_id' in data and phone_number:
                        user_session_manager.update_conversation_id(phone_number, data['conversation_id'])
                    
                    # 处理回复，去除多余的空行
                    answer = process_reply_text(data['answer'])
                    add_log(f"成功处理消息: '{message_text[:30]}...'", 'success')
                    return answer
                else:
                    add_log(f"无法从Dify获取有效回复，响应内容: {data}", 'error')
                    return None
            else:
                add_log(f"Dify API错误: {response.status_code} - {response.text}", 'error')
                return None
    
    except requests.exceptions.Timeout:
        add_log("Dify API请求超时", 'error')
        return None
    except requests.exceptions.ConnectionError:
        add_log(f"无法连接到Dify API: {url}", 'error')
        return None
    except Exception as e:
        add_log(f"处理消息错误: {str(e)}", 'error')
        return None

def process_reply_text(text):
    """处理回复文本，去除多余的空行"""
    if not text:
        return text
    
    # 去除开头和结尾的空白字符
    text = text.strip()
    
    # 将多个连续空行替换为单个空行
    text = re.sub(r'\n\s*\n', '\n\n', text)
    
    # 确保消息末尾没有多余的换行符
    text = text.rstrip('\n')
    
    return text

class iMessageDBHandler(FileSystemEventHandler):
    """处理iMessage数据库文件变化的事件处理器"""
    
    def __init__(self):
        super().__init__()
        self.last_event_time = 0
        self.cooldown = 0.5  # 冷却时间，防止短时间内多次触发
    
    def on_any_event(self, event):
        """当任何文件事件发生时调用"""
        # 检查是否是iMessage数据库文件
        db_path = get_imessage_db_path()
        
        # 只处理数据库文件的事件
        if event.src_path.endswith('chat.db'):
            current_time = time.time()
            
            # 防止短时间内多次触发，使用冷却时间
            if current_time - self.last_event_time < self.cooldown:
                return
                
            self.last_event_time = current_time
            print(f"文件监控: 检测到数据库文件变化: {event.src_path}")
            add_log(f"检测到数据库文件变化: {event.src_path}", 'success')
            
            # 如果服务未运行或未使用文件监控，则不处理
            if not config['is_running'] or not config['use_file_watcher']:
                print("文件监控: 服务未运行或未使用文件监控，不处理")
                return
                
            # 触发消息检查
            print("文件监控: 触发消息检查")
            self._check_messages()
    
    def on_modified(self, event):
        """当文件被修改时调用"""
        # 此方法会被on_any_event调用，但我们保留它以确保兼容性
        # 特别针对chat.db文件的修改事件
        if event.src_path.endswith('chat.db'):
            current_time = time.time()
            
            # 防止短时间内多次触发，使用冷却时间
            if current_time - self.last_event_time < self.cooldown:
                return
                
            self.last_event_time = current_time
            print(f"文件监控: 检测到数据库文件修改: {event.src_path}")
            add_log(f"检测到数据库文件修改: {event.src_path}", 'success')
            
            # 如果服务未运行或未使用文件监控，则不处理
            if not config['is_running'] or not config['use_file_watcher']:
                print("文件监控: 服务未运行或未使用文件监控，不处理")
                return
                
            # 触发消息检查
            print("文件监控: 触发消息检查")
            self._check_messages()
    
    def _check_messages(self):
        """检查新消息（带节流控制）"""
        global last_modified_time
        current_time = time.time()
        
        # 防止短时间内多次触发，使用更短的间隔
        if current_time - last_modified_time < 0.5:  # 减少到0.5秒
            print("文件监控: 触发过于频繁，跳过")
            return
            
        last_modified_time = current_time
        
        # 使用锁防止并发处理
        if processing_lock.acquire(blocking=False):
            try:
                print("文件监控: 开始处理新消息")
                # 不再调用process_new_messages函数
                # 现在使用imessage_reader来处理消息
            finally:
                processing_lock.release()
        else:
            print("文件监控: 已有消息处理任务在进行中")
            # 不记录锁冲突的日志，减少日志量
            pass

def start_message_reader():
    """启动iMessage消息读取器"""
    global message_reader, message_reader_thread
    
    add_log("尝试启动iMessage消息监控...", 'info')
    
    if message_reader_thread is not None:
        add_log("检测到已存在的消息监控线程，尝试停止...", 'warning')
        stop_message_reader()
    
    try:
        # 创建iMessageReader实例
        add_log("创建iMessageReader实例...", 'info')
        message_reader = iMessageReader()
        
        # 检查数据库访问权限
        add_log("检查数据库访问权限...", 'info')
        if not message_reader.check_db_access():
            add_log("无法访问iMessage数据库，请确保已授予权限", 'error')
            return False
        
        # 创建并启动监控线程
        add_log("创建并启动监控线程...", 'info')
        message_reader_thread = threading.Thread(
            target=message_reader.monitor_messages,
            args=(on_new_messages,),
            daemon=True
        )
        message_reader_thread.start()
        
        add_log("已启动iMessage消息监控", 'success')
        return True
    except Exception as e:
        add_log(f"启动iMessage消息监控失败: {str(e)}", 'error')
        message_reader = None
        message_reader_thread = None
        return False

def stop_message_reader():
    """停止iMessage消息读取器"""
    global message_reader, message_reader_thread
    
    if message_reader_thread is None:
        return
    
    try:
        # 停止监控线程
        if message_reader:
            # 使用新添加的 stop 方法停止监控
            add_log("正在停止iMessage消息监控...", 'info')
            message_reader.stop()
        
        message_reader = None
        message_reader_thread = None
        add_log("已停止iMessage消息监控", 'warning')
    except Exception as e:
        add_log(f"停止iMessage消息监控失败: {str(e)}", 'error')

def message_checker():
    """后台线程，定期检查新消息"""
    last_force_check_time = 0
    last_log_time = 0
    last_reader_retry_time = 0
    reader_retry_interval = 300  # 5分钟重试一次消息监控
    log_interval = 3600  # 每小时最多记录一次日志，大幅减少日志量
    
    while not stop_event.is_set():
        current_time = time.time()
        
        if config['is_running']:
            # 如果消息读取器未运行，尝试重新启动
            if message_reader_thread is None and (current_time - last_reader_retry_time) > reader_retry_interval:
                print("后台检查: 尝试重新启动消息监控")
                
                # 确保先停止任何可能仍在运行的监控器
                stop_message_reader()
                
                # 等待一段时间，确保旧的监控器完全停止
                time.sleep(1)
                
                if start_message_reader():
                    print("后台检查: 消息监控重启成功")
                else:
                    print("后台检查: 消息监控重启失败")
                last_reader_retry_time = current_time
        
        # 等待指定的间隔时间
        wait_time = min(5, config['check_interval'])  # 最长等待5秒
        stop_event.wait(wait_time)

@app.route('/')
def index():
    """主页"""
    return render_template('index.html', config=config)

@app.route('/save_config', methods=['POST'])
def save_config_route():
    """保存配置"""
    dify_url = request.form.get('dify_url', '').strip()
    dify_api_key = request.form.get('dify_api_key', '').strip()
    check_interval = int(request.form.get('check_interval', 10))
    force_check_interval = int(request.form.get('force_check_interval', 60))
    
    # 获取检查模式
    check_mode = request.form.get('check_mode', 'polling')
    use_file_watcher = (check_mode == 'file_watcher')
    
    # 获取高级选项
    dify_response_mode = request.form.get('dify_response_mode', 'blocking')
    enable_image_detection = 'enable_image_detection' in request.form
    use_system_watcher = 'use_system_watcher' in request.form
    
    # 自动用户会话始终启用
    auto_user_sessions = True
    
    # 验证URL格式
    if dify_url and not (dify_url.startswith('http://') or dify_url.startswith('https://')):
        add_log("保存配置失败：Dify URL格式错误，必须以 http:// 或 https:// 开头", 'error')
        return redirect(url_for('index'))
    
    # 检查文件监控模式是否改变
    file_watcher_changed = config['use_file_watcher'] != use_file_watcher
    system_watcher_changed = config.get('use_system_watcher', True) != use_system_watcher
    
    # 更新配置
    config['dify_url'] = dify_url
    config['dify_api_key'] = dify_api_key
    config['check_interval'] = check_interval
    config['force_check_interval'] = force_check_interval
    config['dify_response_mode'] = dify_response_mode
    config['enable_image_detection'] = enable_image_detection
    config['auto_user_sessions'] = auto_user_sessions
    config['use_file_watcher'] = use_file_watcher
    config['use_system_watcher'] = use_system_watcher
    
    save_config()
    
    # 如果文件监控模式改变，需要重新启动或停止监控
    if (file_watcher_changed or system_watcher_changed) and config['is_running']:
        stop_file_watcher()  # 先停止所有监控
        if use_file_watcher:
            start_file_watcher()  # 重新启动监控
    
    add_log("配置已保存", 'success')
    return redirect(url_for('index'))

@app.route('/user_sessions')
def user_sessions():
    """显示所有用户会话信息"""
    sessions = user_session_manager.get_all_sessions()
    return render_template('user_sessions.html', sessions=sessions)

@app.route('/reset_user_session/<phone_number>', methods=['POST'])
def reset_user_session(phone_number):
    """重置指定用户的会话ID"""
    if phone_number in user_session_manager.sessions:
        user_session_manager.update_conversation_id(phone_number, '')
        add_log(f"已重置用户 {phone_number} 的会话ID", 'success')
    return redirect(url_for('user_sessions'))

@app.route('/delete_user_session/<phone_number>', methods=['POST'])
def delete_user_session(phone_number):
    """删除指定用户的会话数据"""
    add_log(f"收到删除用户 {phone_number} 会话数据的请求", 'info')
    success = user_session_manager.delete_user_session(phone_number)
    if success:
        add_log(f"已删除用户 {phone_number} 的会话数据", 'success')
    else:
        add_log(f"删除用户 {phone_number} 的会话数据失败，可能不存在", 'warning')
    return redirect(url_for('user_sessions'))

@app.route('/clear_all_sessions', methods=['POST'])
def clear_all_sessions():
    """清空所有用户会话数据"""
    add_log(f"收到清空所有用户会话数据的请求", 'info')
    count = user_session_manager.clear_all_sessions()
    add_log(f"已清空所有用户会话数据，共 {count} 个会话", 'success')
    return redirect(url_for('user_sessions'))

@app.route('/test_connection', methods=['POST'])
def test_connection():
    """测试Dify连接"""
    success, message = test_dify_connection()
    return jsonify({'success': success, 'message': message})

@app.route('/get_logs', methods=['GET'])
def get_logs():
    """获取日志"""
    return jsonify(list(log_entries))

@app.route('/clear_logs', methods=['POST'])
def clear_logs():
    """清除日志"""
    log_entries.clear()
    return jsonify({'success': True})

@app.route('/toggle_service', methods=['POST'])
def toggle_service():
    """启动或停止服务"""
    config['is_running'] = not config['is_running']
    
    if config['is_running']:
        # 启动消息监控
        if start_message_reader():
            add_log("服务已启动（使用imessage_reader监控）", 'success')
        else:
            add_log("服务已启动但消息监控启动失败，请检查权限设置", 'warning')
    else:
        # 停止消息监控
        stop_message_reader()
        add_log("服务已停止", 'warning')
    
    save_config()
    return jsonify({'is_running': config['is_running']})

@app.route('/get_status', methods=['GET'])
def get_status():
    """获取当前状态"""
    return jsonify({
        'is_running': config['is_running'],
        'last_message_id': config['last_message_id']
    })

@app.route('/reset_last_message', methods=['POST'])
def reset_last_message():
    """重置最后处理的消息ID"""
    reset_type = request.form.get('reset_type', 'zero')
    
    if reset_type == 'latest':
        # 重置为最新消息ID
        try:
            db_path = get_imessage_db_path()
            if not os.path.exists(db_path):
                return jsonify({'success': False, 'message': 'iMessage数据库不存在'})
            
            conn = sqlite3.connect(db_path, timeout=5)
            cursor = conn.cursor()
            
            # 获取最新消息ID
            cursor.execute("SELECT MAX(ROWID) as max_id FROM message")
            max_id_result = cursor.fetchone()
            
            if max_id_result and max_id_result[0]:
                old_id = config['last_message_id']
                config['last_message_id'] = max_id_result[0]
                save_config()
                
                add_log(f"手动重置最后消息ID: {old_id} -> {max_id_result[0]}", 'success')
                add_log(f"只会处理ID > {max_id_result[0]} 的新消息", 'success')
                
                return jsonify({
                    'success': True, 
                    'message': f'已重置为最新消息ID: {max_id_result[0]}',
                    'last_message_id': max_id_result[0]
                })
            else:
                return jsonify({'success': False, 'message': '无法获取最新消息ID'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'重置失败: {str(e)}'})
    else:
        # 重置为0（处理所有历史消息）
        old_id = config['last_message_id']
        config['last_message_id'] = 0
        save_config()
        
        add_log(f"手动重置最后消息ID: {old_id} -> 0（将处理所有历史消息）", 'warning')
        
        return jsonify({
            'success': True, 
            'message': '已重置为0，将处理所有历史消息',
            'last_message_id': 0
        })

@app.route('/force_check', methods=['POST'])
def force_check():
    """强制检查新消息"""
    if not config['is_running']:
        return jsonify({'success': False, 'message': '服务未运行'})
    
    try:
        # 这个功能在使用imessage_reader时不再需要
        # 因为imessage_reader会自动检测新消息
        return jsonify({'success': True, 'message': '使用imessage_reader自动检测新消息，无需手动检查'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'检查失败: {str(e)}'})

def start_app():
    """启动应用程序"""
    # 加载配置
    load_config()
    
    # 确保消息监控是停止状态
    global stop_event, check_thread
    
    # 启动消息检查线程
    stop_event = threading.Event()
    check_thread = threading.Thread(target=message_checker)
    check_thread.daemon = True
    check_thread.start()
    
    # 如果服务已启用，启动消息监控
    if config['is_running']:
        if not start_message_reader():
            add_log("启动消息监控失败，请检查权限设置", 'error')
    
    # 启动Flask应用，禁用日志输出
    print("iMessage-Dify 服务已启动，访问 http://127.0.0.1:8888 进行配置")
    app.run(host='0.0.0.0', port=8888, debug=False, use_reloader=False)

def cleanup():
    """清理资源"""
    stop_event.set()
    if check_thread:
        check_thread.join(timeout=1)
    stop_message_reader()

if __name__ == '__main__':
    try:
        start_app()
    finally:
        cleanup() 