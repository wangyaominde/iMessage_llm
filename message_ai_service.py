import requests
from flask import Flask, request, jsonify, render_template_string
from flask_socketio import SocketIO, emit
from imessage_sender import send_imessage
from imessage_reader import iMessageReader
import logging
import threading
import time
from datetime import datetime, timedelta
from collections import defaultdict
import json
import os
import sqlite3
import base64

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
socketio = SocketIO(app)

# 数据文件路径
DATA_DIR = "data"
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DB_FILE = os.path.join(DATA_DIR, "messages.db")
TEMPLATE_FILE = os.path.join(DATA_DIR, "template.html")

# 确保数据目录存在
os.makedirs(DATA_DIR, exist_ok=True)

# 数据库初始化
def init_db():
    """初始化SQLite数据库"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # 创建消息历史表
    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 创建调用历史表
    c.execute('''
        CREATE TABLE IF NOT EXISTS call_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact TEXT NOT NULL,
            success BOOLEAN NOT NULL,
            error TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

class MessageDB:
    def __init__(self):
        self.db_file = DB_FILE
        
    def get_connection(self):
        return sqlite3.connect(self.db_file)
    
    def add_message(self, contact, role, content):
        """添加新消息"""
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                'INSERT INTO messages (contact, role, content) VALUES (?, ?, ?)',
                (contact, role, content)
            )
    
    def add_call_record(self, contact, success, error=None):
        """添加调用记录"""
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                'INSERT INTO call_history (contact, success, error) VALUES (?, ?, ?)',
                (contact, success, error)
            )
    
    def get_messages(self, contact, limit=None):
        """获取指定联系人的消息历史"""
        with self.get_connection() as conn:
            c = conn.cursor()
            if limit:
                c.execute(
                    'SELECT role, content FROM messages WHERE contact = ? ORDER BY timestamp DESC LIMIT ?',
                    (contact, limit)
                )
            else:
                c.execute(
                    'SELECT role, content FROM messages WHERE contact = ? ORDER BY timestamp DESC',
                    (contact,)
                )
            return [{"role": role, "content": content} for role, content in c.fetchall()][::-1]
    
    def get_all_contacts_with_stats(self):
        """获取所有联系人及其统计信息"""
        with self.get_connection() as conn:
            c = conn.cursor()
            # 获取每个联系人的消息数和最后通话记录
            c.execute('''
                SELECT m.contact,
                       COUNT(m.id) as message_count,
                       MAX(ch.timestamp) as last_call,
                       ch.success as last_call_success,
                       ch.error as last_call_error
                FROM messages m
                LEFT JOIN call_history ch ON m.contact = ch.contact
                GROUP BY m.contact
            ''')
            return c.fetchall()
    
    def clear_history(self, contact=None):
        """清除历史记录"""
        with self.get_connection() as conn:
            c = conn.cursor()
            if contact:
                c.execute('DELETE FROM messages WHERE contact = ?', (contact,))
                c.execute('DELETE FROM call_history WHERE contact = ?', (contact,))
            else:
                c.execute('DELETE FROM messages')
                c.execute('DELETE FROM call_history')
    
    def cleanup_old_data(self, days=30):
        """清理指定天数之前的数据"""
        with self.get_connection() as conn:
            c = conn.cursor()
            threshold = datetime.now() - timedelta(days=days)
            c.execute('DELETE FROM messages WHERE timestamp < ?', (threshold,))
            c.execute('DELETE FROM call_history WHERE timestamp < ?', (threshold,))
            return c.rowcount

# 创建数据库实例
db = MessageDB()

# 定期清理数据的函数
def auto_cleanup_data():
    while True:
        try:
            # 每天清理一次30天前的数据
            cleaned = db.cleanup_old_data(days=30)
            if cleaned > 0:
                logger.info(f"已清理 {cleaned} 条旧数据")
        except Exception as e:
            logger.error(f"清理数据时出错: {str(e)}")
        time.sleep(86400)  # 24小时

# 启动自动清理线程
cleanup_thread = threading.Thread(target=auto_cleanup_data, daemon=True)
cleanup_thread.start()

# 默认HTML模板
DEFAULT_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>iMessage AI 控制台</title>
    <meta charset="utf-8">
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; }
        .section { margin-bottom: 30px; padding: 20px; border: 1px solid #ddd; border-radius: 5px; background-color: white; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .config-item { margin: 15px 0; }
        .config-item label { display: block; margin-bottom: 5px; color: #666; }
        .contact-item { margin: 20px 0; padding: 15px; background: #f8f9fa; border-radius: 5px; }
        .message-item { margin: 8px 0; padding: 8px; background: white; border-left: 3px solid #007bff; }
        .message-item.user { border-left-color: #28a745; }
        .message-item.assistant { border-left-color: #007bff; }
        button { padding: 8px 15px; margin: 5px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }
        button:hover { background: #0056b3; }
        input[type="text"], textarea { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; margin-top: 5px; }
        textarea { height: 100px; }
        .success { color: #28a745; padding: 10px; }
        .error { color: #dc3545; padding: 10px; }
        .status-bar { background: #343a40; color: white; padding: 10px; position: fixed; bottom: 0; left: 0; right: 0; }
        .live-indicator { display: inline-block; width: 10px; height: 10px; background: #28a745; border-radius: 50%; margin-right: 5px; }
        .call-status { margin: 5px 0; padding: 5px; border-radius: 3px; }
        .call-status.success { background: #d4edda; }
        .call-status.error { background: #f8d7da; }
        .button-group { margin: 15px 0; }
        .button-group button { margin-right: 10px; }
        #testStatus { margin-top: 10px; padding: 10px; border-radius: 4px; }
        #testStatus.testing { background: #fff3cd; color: #856404; }
        #testStatus.success { background: #d4edda; color: #155724; }
        #testStatus.error { background: #f8d7da; color: #721c24; }
    </style>
</head>
<body>
    <div class="container">
        <h1>iMessage AI 控制台</h1>
        
        <div class="section">
            <h2>系统配置</h2>
            <div class="config-item">
                <label>API Key:</label>
                <input type="text" id="apiKey" value="{{ config.api_key }}">
            </div>
            <div class="config-item">
                <label>API URL:</label>
                <input type="text" id="apiUrl" value="{{ config.api_url }}">
            </div>
            <div class="config-item">
                <label>Model Name:</label>
                <input type="text" id="modelName" value="{{ config.model_name }}">
            </div>
            <div class="config-item">
                <label>System Prompt:</label>
                <textarea id="systemPrompt">{{ config.system_prompt }}</textarea>
            </div>
            <div class="config-item">
                <label>Temperature (0.0-1.5):</label>
                <input type="number" id="temperature" value="{{ config.temperature }}" min="0" max="1.5" step="0.1">
            </div>
            <div class="config-item">
                <label>最大历史记录长度:</label>
                <input type="number" id="maxHistory" value="{{ config.max_history_length }}" min="1" max="50">
            </div>
            <div class="button-group">
                <button onclick="updateConfig()">更新配置</button>
                <button onclick="testAI()">测试连接</button>
            </div>
            <div id="configStatus"></div>
            <div id="testStatus"></div>
        </div>

        <div class="section" id="conversationsSection">
            <h2>实时对话</h2>
            <div id="conversations">
                {% for contact, messages in message_history.items() %}
                <div class="contact-item" id="contact-{{ contact }}">
                    <h3>联系人: {{ contact }}</h3>
                    <button onclick="clearHistory('{{ contact }}')">清除此联系人历史</button>
                    <div class="messages">
                        {% for msg in messages %}
                        <div class="message-item {{ msg.role }}">
                            <strong>{{ msg.role }}:</strong> {{ msg.content }}
                        </div>
                        {% endfor %}
                    </div>
                </div>
                {% endfor %}
            </div>
            <button onclick="clearHistory()">清除所有历史</button>
        </div>

        <div class="section">
            <h2>调用统计</h2>
            <div id="callStats">
                {% for contact, calls in call_history.items() %}
                <div class="contact-item">
                    <h3>联系人: {{ contact }}</h3>
                    <p>总调用次数: {{ calls|length }}</p>
                    {% if calls %}
                    <p>最后调用时间: {{ calls[-1].time }}</p>
                    <div class="call-status {{ 'success' if calls[-1].success else 'error' }}">
                        状态: {{ '成功' if calls[-1].success else '失败 - ' + calls[-1].error }}
                    </div>
                    {% endif %}
                </div>
                {% endfor %}
            </div>
        </div>
    </div>

    <div class="status-bar">
        <span class="live-indicator"></span>
        <span id="connectionStatus">已连接</span>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <script>
        const socket = io();
        
        socket.on('connect', () => {
            document.getElementById('connectionStatus').textContent = '已连接';
            document.querySelector('.live-indicator').style.background = '#28a745';
        });
        
        socket.on('disconnect', () => {
            document.getElementById('connectionStatus').textContent = '已断开';
            document.querySelector('.live-indicator').style.background = '#dc3545';
        });
        
        socket.on('new_message', (data) => {
            let contactDiv = document.getElementById(`contact-${data.contact}`);
            if (!contactDiv) {
                const conversations = document.getElementById('conversations');
                contactDiv = document.createElement('div');
                contactDiv.id = `contact-${data.contact}`;
                contactDiv.className = 'contact-item';
                contactDiv.innerHTML = `
                    <h3>联系人: ${data.contact}</h3>
                    <button onclick="clearHistory('${data.contact}')">清除此联系人历史</button>
                    <div class="messages"></div>
                `;
                conversations.appendChild(contactDiv);
            }
            
            const messagesDiv = contactDiv.querySelector('.messages');
            const messageDiv = document.createElement('div');
            messageDiv.className = `message-item ${data.role}`;
            messageDiv.innerHTML = `<strong>${data.role}:</strong> ${data.content}`;
            messagesDiv.appendChild(messageDiv);
            messageDiv.scrollIntoView({ behavior: 'smooth' });
        });
        
        socket.on('call_update', (data) => {
            // 更新调用统计
            location.reload();  // 简单起见，直接刷新页面
        });

        function updateConfig() {
            const data = {
                api_key: document.getElementById('apiKey').value,
                api_url: document.getElementById('apiUrl').value,
                model_name: document.getElementById('modelName').value,
                system_prompt: document.getElementById('systemPrompt').value,
                temperature: parseFloat(document.getElementById('temperature').value),
                max_history_length: parseInt(document.getElementById('maxHistory').value)
            };
            
            fetch('/config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            })
            .then(response => response.json())
            .then(data => {
                const status = document.getElementById('configStatus');
                status.textContent = data.message;
                status.className = data.status;
            });
        }

        function clearHistory(contact = null) {
            fetch('/clear_history', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({contact: contact})
            })
            .then(response => response.json())
            .then(data => {
                if (data.status === 'success') {
                    if (contact) {
                        const contactDiv = document.getElementById(`contact-${contact}`);
                        if (contactDiv) {
                            contactDiv.querySelector('.messages').innerHTML = '';
                        }
                    } else {
                        document.getElementById('conversations').innerHTML = '';
                    }
                }
            });
        }

        function testAI() {
            const testStatus = document.getElementById('testStatus');
            testStatus.className = 'testing';
            testStatus.textContent = '正在测试连接...';
            
            const data = {
                api_key: document.getElementById('apiKey').value,
                api_url: document.getElementById('apiUrl').value,
                model_name: document.getElementById('modelName').value,
                system_prompt: document.getElementById('systemPrompt').value,
                temperature: parseFloat(document.getElementById('temperature').value),
                max_history_length: parseInt(document.getElementById('maxHistory').value)
            };
            
            fetch('/test_ai', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            })
            .then(response => response.json())
            .then(data => {
                testStatus.className = data.status;
                if (data.status === 'success') {
                    testStatus.innerHTML = `${data.message}<br>AI回复: ${data.response}`;
                } else {
                    testStatus.textContent = data.message;
                }
            })
            .catch(error => {
                testStatus.className = 'error';
                testStatus.textContent = '测试请求失败: ' + error;
            });
        }
    </script>
</body>
</html>
"""

# 保存默认模板
if not os.path.exists(TEMPLATE_FILE):
    try:
        with open(TEMPLATE_FILE, 'w', encoding='utf-8') as f:
            f.write(DEFAULT_TEMPLATE)
        logger.info("已创建默认模板文件")
    except Exception as e:
        logger.error(f"创建模板文件失败: {str(e)}")

def get_template():
    """获取HTML模板"""
    try:
        if os.path.exists(TEMPLATE_FILE):
            with open(TEMPLATE_FILE, 'r', encoding='utf-8') as f:
                return f.read()
        return DEFAULT_TEMPLATE
    except Exception as e:
        logger.error(f"读取模板文件失败: {str(e)}")
        return DEFAULT_TEMPLATE

# AI配置
class Config:
    def __init__(self):
        self.api_key = ""  # 移除默认key
        self.api_url = "https://api.deepseek.com"  # 只保留基础URL
        self.model_name = "deepseek-chat"
        self.system_prompt = "你是一个友好的AI助手，可以帮助用户解答问题。"
        self.temperature = 1.3
        self.max_history_length = 10
        self.load_config()

    def is_valid(self):
        """检查配置是否有效"""
        return bool(self.api_key and self.api_url and self.model_name)

    def get_full_api_url(self):
        """获取完整的API URL"""
        return f"{self.api_url.rstrip('/')}/v1/chat/completions"

    def validate_temperature(self, temp):
        """验证并规范化temperature值"""
        try:
            temp = float(temp)
            return max(0.0, min(1.5, temp))  # 限制在0-1.5范围内
        except (TypeError, ValueError):
            return 1.3  # 默认值

    def to_dict(self):
        return {
            "api_key": self.api_key,
            "api_url": self.api_url,
            "model_name": self.model_name,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "max_history_length": self.max_history_length
        }

    def from_dict(self, data):
        self.api_key = data.get("api_key", self.api_key)
        self.api_url = data.get("api_url", self.api_url).rstrip('/')  # 移除尾部的斜杠
        self.model_name = data.get("model_name", self.model_name)
        self.system_prompt = data.get("system_prompt", self.system_prompt)
        self.temperature = self.validate_temperature(data.get("temperature", self.temperature))
        self.max_history_length = int(data.get("max_history_length", self.max_history_length))

    def save_config(self):
        """保存配置到文件"""
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
            logger.info("配置已保存")
        except Exception as e:
            logger.error(f"保存配置失败: {str(e)}")

    def load_config(self):
        """从文件加载配置"""
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.from_dict(data)
                logger.info("配置已加载")
        except Exception as e:
            logger.error(f"加载配置失败: {str(e)}")

config = Config()

# 存储每个联系人的消息历史和调用记录
message_history = defaultdict(list)
call_history = defaultdict(list)
MAX_RETRIES = 3  # 最大重试次数

def encode_image_base64(image_path):
    """将图片编码为 base64 格式"""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def get_ai_response(messages_context, contact):
    """获取 AI 响应"""
    try:
        if not config.load_config() or not config.is_valid():
            logger.error("配置无效")
            return None

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}"
        }

        # 检查是否有图片消息
        has_image = any(msg.get('attachment') is not None for msg in messages_context if msg['role'] == 'user')
        
        if has_image:
            # 使用 GPT-4 Vision API
            api_url = "https://api.openai.com/v1/chat/completions"
            model = "gpt-4-vision-preview"
            max_tokens = 4096
        else:
            # 使用普通的 API
            api_url = config.get_full_api_url()
            model = config.model_name
            max_tokens = 2048

        formatted_messages = []
        # 添加系统提示
        if config.system_prompt:
            formatted_messages.append({
                "role": "system",
                "content": config.system_prompt
            })

        # 处理消息历史
        for msg in messages_context:
            if msg['role'] in ['user', 'assistant']:
                content = msg['content']
                
                # 如果是用户消息且包含图片
                if msg['role'] == 'user' and msg.get('attachment'):
                    image_path = msg['attachment']['path']
                    base64_image = encode_image_base64(image_path)
                    
                    # 构建多模态消息
                    content = [
                        {"type": "text", "text": content} if content else {"type": "text", "text": "请描述这张图片"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}",
                                "detail": "high"
                            }
                        }
                    ]
                
                formatted_messages.append({
                    "role": msg['role'],
                    "content": content
                })

        payload = {
            "model": model,
            "messages": formatted_messages,
            "temperature": config.temperature,
            "max_tokens": max_tokens
        }

        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()

        result = response.json()
        if 'choices' in result and len(result['choices']) > 0:
            content = result['choices'][0]['message']['content']
            
            # 记录到数据库
            db.add_message(contact, 'assistant', content)
            db.add_call_record(contact, True)
            
            return content
        else:
            logger.error(f"API 响应格式错误: {result}")
            db.add_call_record(contact, False, "API 响应格式错误")
            return None

    except Exception as e:
        error_msg = str(e)
        logger.error(f"调用 AI API 时出错: {error_msg}")
        db.add_call_record(contact, False, error_msg)
        return None

def process_message(message):
    """处理新消息"""
    try:
        contact = message['contact']
        content = message['text']
        is_from_me = message['is_from_me']
        attachment = message.get('attachment')

        # 忽略自己发送的消息
        if is_from_me:
            return

        # 记录用户消息到数据库
        if content or attachment:
            db.add_message(contact, 'user', content or '[图片]')

        # 获取历史消息
        history = db.get_messages(contact, limit=config.max_history_length)
        
        # 如果有图片，将图片信息添加到最后一条用户消息中
        if attachment and history:
            for msg in reversed(history):
                if msg['role'] == 'user':
                    msg['attachment'] = attachment
                    break

        # 获取 AI 响应
        response = get_ai_response(history, contact)
        if response:
            # 发送回复
            send_imessage(contact, response)
            
            # 发送到 WebSocket
            socketio.emit('new_message', {
                'contact': contact,
                'message': {
                    'role': 'assistant',
                    'content': response
                }
            })

    except Exception as e:
        logger.error(f"处理消息时出错: {str(e)}")

def on_new_messages(messages):
    """
    处理新消息的回调函数
    """
    for message in messages:
        process_message(message)

@app.route('/')
def index():
    """
    主页（控制台）
    """
    template = get_template()
    contacts_data = db.get_all_contacts_with_stats()
    
    # 构建模板数据
    message_history = {}
    call_history = defaultdict(list)
    
    for contact, msg_count, last_call, success, error in contacts_data:
        message_history[contact] = db.get_messages(contact)
        if last_call:
            call_history[contact].append({
                "time": last_call,
                "success": success,
                "error": error
            })
    
    return render_template_string(template, 
                                config=config,
                                message_history=message_history,
                                call_history=call_history)

@app.route('/config', methods=['POST'])
def update_config():
    """
    更新配置
    """
    try:
        data = request.json
        config.from_dict(data)
        config.save_config()
        return jsonify({"status": "success", "message": "配置更新成功"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"配置更新失败: {str(e)}"})

@app.route('/clear_history', methods=['POST'])
def clear_history():
    """
    清除历史记录
    """
    try:
        contact = request.json.get('contact')
        db.clear_history(contact)
        return jsonify({"status": "success", "message": "历史记录已清除"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"清除历史记录失败: {str(e)}"})

@app.route('/test_ai', methods=['POST'])
def test_ai():
    """
    测试AI连接
    使用提交的配置进行测试，而不是已保存的配置
    """
    try:
        # 获取当前输入的配置
        data = request.json or {}
        api_key = data.get('api_key', '')
        api_url = data.get('api_url', '').rstrip('/')  # 移除尾部的斜杠
        model_name = data.get('model_name', '')

        # 验证必要的配置是否存在
        if not all([api_key, api_url, model_name]):
            return jsonify({
                "status": "error",
                "message": "请填写完整的API配置信息"
            })

        # 构建完整的API URL
        full_api_url = f"{api_url}/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        test_message = "你好，这是一条测试消息，请回复'连接测试成功'"
        
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": "这是一个连接测试，请简短回复。"},
                {"role": "user", "content": test_message}
            ],
            "temperature": 0.1  # 使用较低的temperature以获得稳定的回复
        }
        
        response = requests.post(full_api_url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        
        ai_response = response.json()["choices"][0]["message"]["content"]
        
        return jsonify({
            "status": "success",
            "message": "AI连接测试成功",
            "response": ai_response
        })
        
    except requests.exceptions.Timeout:
        return jsonify({
            "status": "error",
            "message": "连接超时，请检查网络或API地址"
        })
    except requests.exceptions.RequestException as e:
        return jsonify({
            "status": "error",
            "message": f"API请求失败: {str(e)}"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"测试失败: {str(e)}"
        })

def start_message_monitor():
    """
    启动消息监控
    """
    reader = iMessageReader()
    if reader.check_db_access():
        logger.info("开始监控 iMessage...")
        reader.monitor_messages(callback=on_new_messages)
    else:
        logger.error("无法访问 iMessage 数据库，请确保已授予权限")

if __name__ == '__main__':
    # 启动消息监控线程
    monitor_thread = threading.Thread(target=start_message_monitor, daemon=True)
    monitor_thread.start()
    
    # 启动WebSocket服务
    logger.info("启动Web服务...")
    socketio.run(app, host='0.0.0.0', port=8888, debug=False) 