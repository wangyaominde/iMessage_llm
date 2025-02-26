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
import re
from dateutil import parser
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ThreadPoolExecutor

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
    
    # 创建定时任务表
    c.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact TEXT NOT NULL,
            message TEXT NOT NULL,
            scheduled_time DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            executed BOOLEAN DEFAULT 0,
            job_id TEXT,
            is_recurring BOOLEAN DEFAULT 0,
            recurring_type TEXT,
            recurring_value INTEGER,
            next_run_time DATETIME,
            task_type TEXT,
            task_params TEXT
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
    
    # 检查是否需要添加新列
    try:
        c.execute('SELECT task_type FROM scheduled_tasks LIMIT 1')
    except sqlite3.OperationalError:
        # 列不存在，添加新列
        logger.info("正在更新数据库结构，添加任务类型和参数字段...")
        c.execute('ALTER TABLE scheduled_tasks ADD COLUMN task_type TEXT')
        c.execute('ALTER TABLE scheduled_tasks ADD COLUMN task_params TEXT')
        logger.info("数据库结构更新完成")
    
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
            cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            c.execute('DELETE FROM messages WHERE timestamp < ?', (cutoff_date,))
            c.execute('DELETE FROM call_history WHERE timestamp < ?', (cutoff_date,))
            c.execute('DELETE FROM scheduled_tasks WHERE executed = 1 AND scheduled_time < ?', (cutoff_date,))
    
    def add_scheduled_task(self, contact, message, scheduled_time, job_id=None, is_recurring=False, recurring_type=None, recurring_value=None, next_run_time=None, task_type=None, task_params=None):
        """添加定时任务"""
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                'INSERT INTO scheduled_tasks (contact, message, scheduled_time, job_id, is_recurring, recurring_type, recurring_value, next_run_time, task_type, task_params) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (contact, message, scheduled_time, job_id, is_recurring, recurring_type, recurring_value, next_run_time, task_type, task_params)
            )
            return c.lastrowid
    
    def get_scheduled_tasks(self, contact=None, include_executed=False, only_recurring=False):
        """获取定时任务"""
        with self.get_connection() as conn:
            c = conn.cursor()
            query_parts = []
            params = []
            
            if contact:
                query_parts.append("contact = ?")
                params.append(contact)
            
            if not include_executed:
                query_parts.append("executed = 0")
            
            if only_recurring:
                query_parts.append("is_recurring = 1")
            
            where_clause = " AND ".join(query_parts) if query_parts else ""
            if where_clause:
                where_clause = "WHERE " + where_clause
            
            query = f'''
                SELECT id, contact, message, scheduled_time, created_at, executed, job_id, 
                       is_recurring, recurring_type, recurring_value, next_run_time 
                FROM scheduled_tasks 
                {where_clause} 
                ORDER BY scheduled_time
            '''
            
            c.execute(query, params)
            return c.fetchall()
    
    def mark_task_executed(self, task_id):
        """标记任务已执行"""
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                'UPDATE scheduled_tasks SET executed = 1 WHERE id = ?',
                (task_id,)
            )
    
    def update_task_job_id(self, task_id, job_id):
        """更新任务的job_id"""
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                'UPDATE scheduled_tasks SET job_id = ? WHERE id = ?',
                (job_id, task_id)
            )
    
    def delete_task(self, task_id):
        """删除任务"""
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute(
                'DELETE FROM scheduled_tasks WHERE id = ?',
                (task_id,)
            )

# 创建数据库实例
db = MessageDB()

# 初始化任务调度器
jobstores = {
    'default': MemoryJobStore()
}
executors = {
    'default': ThreadPoolExecutor(20)
}
job_defaults = {
    'coalesce': False,
    'max_instances': 3
}
scheduler = BackgroundScheduler(jobstores=jobstores, executors=executors, job_defaults=job_defaults)

# 任务执行函数
def execute_scheduled_task(task_id, contact, message):
    """执行定时任务"""
    try:
        logger.info(f"开始执行定时任务 {task_id}: 向 {contact} 发送消息: {message}")
        logger.info(f"[DEBUG] 任务内容分析: 消息类型={type(message)}, 消息内容={message}")
        
        # 获取任务详情
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute('''
                SELECT id, contact, message, scheduled_time, is_recurring, recurring_type, recurring_value, next_run_time, task_type, task_params 
                FROM scheduled_tasks WHERE id = ?
            ''', (task_id,))
            task = c.fetchone()
            if task:
                logger.info(f"任务详情: {task}")
            else:
                logger.warning(f"未找到ID为 {task_id} 的任务")
                return
        
        # 解析任务详情
        if len(task) >= 10:  # 确保任务包含所有字段
            task_id, contact, message, scheduled_time, is_recurring, recurring_type, recurring_value, next_run_time, task_type, task_params = task
            logger.info(f"[DEBUG] 任务类型={task_type}, 任务参数={task_params}")
        else:
            # 兼容旧版本数据
            task_id, contact, message, scheduled_time, is_recurring, recurring_type, recurring_value, next_run_time = task
            task_type = None
            task_params = None
            logger.info(f"[DEBUG] 旧版本任务数据，无任务类型和参数")
        
        # 处理自动化任务
        response_message = message
        if task_type:
            logger.info(f"执行自动化任务: {task_type}, 参数: {task_params}")
            try:
                # 解析任务参数
                params = json.loads(task_params) if task_params else {}
                
                # 执行自动化任务
                from agents.agent_base import execute_agent_task
                response_message = execute_agent_task(task_type, params, contact, config)
                logger.info(f"自动化任务执行结果: {response_message}")
            except Exception as e:
                logger.error(f"执行自动化任务时出错: {str(e)}")
                response_message = f"执行自动化任务时出错: {str(e)}"
        else:
            # 尝试检测消息中是否包含需要执行的任务
            logger.info(f"[DEBUG] 尝试检测消息中是否包含需要执行的任务: {message}")
            from agents.agent_base import detect_and_execute_agent_task
            task_response = detect_and_execute_agent_task(message, contact, config)
            if task_response:
                logger.info(f"[DEBUG] 检测到任务并执行，结果: {task_response}")
                response_message = task_response
            else:
                logger.info(f"[DEBUG] 未检测到任务，将直接发送原始消息")
        
        # 发送消息
        send_imessage(contact, response_message)
        logger.info(f"消息已发送给 {contact}")
        
        # 处理循环任务
        if is_recurring and recurring_type and recurring_value:
            logger.info(f"这是一个循环任务，类型: {recurring_type}，值: {recurring_value}")
            
            # 计算下一次执行时间
            current_time = datetime.now()
            if recurring_type == 'daily':
                next_time = current_time + timedelta(days=recurring_value)
            elif recurring_type == 'weekly':
                next_time = current_time + timedelta(weeks=recurring_value)
            elif recurring_type == 'monthly':
                # 简单处理，不考虑月份天数不同的情况
                next_time = current_time + timedelta(days=30 * recurring_value)
            elif recurring_type == 'hourly':
                next_time = current_time + timedelta(hours=recurring_value)
            elif recurring_type == 'minutely':
                next_time = current_time + timedelta(minutes=recurring_value)
            else:
                logger.warning(f"未知的循环类型: {recurring_type}")
                next_time = None
            
            if next_time:
                # 更新任务的下一次执行时间
                with db.get_connection() as conn:
                    c = conn.cursor()
                    c.execute(
                        'UPDATE scheduled_tasks SET next_run_time = ? WHERE id = ?',
                        (next_time.strftime("%Y-%m-%d %H:%M:%S"), task_id)
                    )
                
                # 添加新的调度任务
                job = scheduler.add_job(
                    execute_scheduled_task,
                    'date',
                    run_date=next_time,
                    args=[task_id, contact, message],
                    id=f"task_{task_id}_{next_time.strftime('%Y%m%d%H%M%S')}"
                )
                
                logger.info(f"已为循环任务 {task_id} 安排下一次执行时间: {next_time}")
        else:
            # 非循环任务，标记为已执行
            db.mark_task_executed(task_id)
            logger.info(f"非循环任务 {task_id} 已标记为已执行")
        
        # 发送任务执行通知
        socketio.emit('task_executed', {
            'task_id': task_id,
            'contact': contact,
            'message': response_message,
            'executed_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'is_recurring': bool(is_recurring),
            'next_run_time': next_time.strftime("%Y-%m-%d %H:%M:%S") if is_recurring and 'next_time' in locals() and next_time else None,
            'task_type': task_type
        })
        logger.info(f"已发送任务执行通知")
    except Exception as e:
        logger.error(f"执行定时任务 {task_id} 时出错: {str(e)}")

# 加载未执行的定时任务
def load_scheduled_tasks():
    """从数据库加载未执行的定时任务"""
    tasks = db.get_scheduled_tasks()
    loaded_count = 0
    recurring_count = 0
    
    for task in tasks:
        if len(task) >= 11:  # 确保任务包含所有字段
            task_id, contact, message, scheduled_time, _, executed, _, is_recurring, recurring_type, recurring_value, next_run_time = task
        else:
            # 兼容旧版本数据
            task_id, contact, message, scheduled_time, _, executed, _ = task
            is_recurring = False
            recurring_type = None
            recurring_value = None
            next_run_time = None
        
        # 确定运行时间
        if is_recurring and next_run_time:
            run_time = parser.parse(next_run_time)
            logger.info(f"加载循环任务 {task_id}，下一次执行时间: {run_time}")
        else:
            run_time = parser.parse(scheduled_time)
            logger.info(f"加载一次性任务 {task_id}，执行时间: {run_time}")
        
        # 只加载未来的任务
        if run_time > datetime.now():
            job = scheduler.add_job(
                execute_scheduled_task,
                'date',
                run_date=run_time,
                args=[task_id, contact, message],
                id=f"task_{task_id}" if not is_recurring else f"task_{task_id}_{run_time.strftime('%Y%m%d%H%M%S')}"
            )
            db.update_task_job_id(task_id, job.id)
            
            if is_recurring:
                recurring_count += 1
            else:
                loaded_count += 1
                
            logger.info(f"已加载任务 {task_id}: 将在 {run_time} 向 {contact} 发送消息")
    
    logger.info(f"共加载了 {loaded_count} 个一次性任务和 {recurring_count} 个循环任务")

# 启动调度器
scheduler.start()
load_scheduled_tasks()

# 自动清理数据的函数
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
        valid = bool(self.api_key and self.api_url and self.model_name)
        if not valid:
            logger.error(f"配置验证失败 - API Key: {bool(self.api_key)}, API URL: {bool(self.api_url)}, Model Name: {bool(self.model_name)}")
        return valid

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
                logger.info(f"开始加载配置文件: {CONFIG_FILE}")
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                logger.info(f"配置文件内容: {json.dumps(data, ensure_ascii=False)}")
                self.from_dict(data)
                logger.info("配置已加载")
            else:
                logger.error(f"配置文件不存在: {CONFIG_FILE}")
        except Exception as e:
            logger.error(f"加载配置失败: {str(e)}")

config = Config()

# 存储每个联系人的消息历史和调用记录
message_history = defaultdict(list)
call_history = defaultdict(list)
MAX_RETRIES = 3  # 最大重试次数

def get_ai_response(messages_context, contact):
    """
    从AI模型获取响应
    """
    # 检查配置是否有效
    if not config.is_valid():
        error_msg = "请先在控制台配置API密钥和相关参数"
        logger.error(error_msg)
        call_data = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "success": False,
            "error": error_msg
        }
        call_history[contact].append(call_data)
        socketio.emit('call_update', {'contact': contact, **call_data})
        return error_msg

    for attempt in range(MAX_RETRIES):
        try:
            headers = {
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json"
            }
            
            # 添加system prompt
            full_messages = [{"role": "system", "content": config.system_prompt}] + messages_context
            
            payload = {
                "model": config.model_name,
                "messages": full_messages,
                "temperature": config.temperature
            }
            
            response = requests.post(config.get_full_api_url(), json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            
            # 记录调用历史并通知前端
            call_data = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "success": True
            }
            call_history[contact].append(call_data)
            socketio.emit('call_update', {'contact': contact, **call_data})
            
            return response.json()["choices"][0]["message"]["content"]
        except requests.exceptions.Timeout:
            logger.warning(f"请求超时，尝试重试 {attempt + 1}/{MAX_RETRIES}")
            if attempt == MAX_RETRIES - 1:
                call_data = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "success": False,
                    "error": "超时"
                }
                call_history[contact].append(call_data)
                socketio.emit('call_update', {'contact': contact, **call_data})
                return "抱歉，我和我的服务器联系不上了，一会儿再发一条给我试试。"
            time.sleep(2)
        except Exception as e:
            logger.error(f"获取AI响应时出错: {str(e)}")
            call_data = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "success": False,
                "error": str(e)
            }
            call_history[contact].append(call_data)
            socketio.emit('call_update', {'contact': contact, **call_data})
            return "抱歉，我的服务器设置崩了，联系服务的维护者解决。"

def process_message(message):
    """
    处理单条消息
    """
    try:
        if not message['is_from_me'] and message['contact']:
            contact = message['contact']
            user_message = message['text']
            
            logger.info(f"收到新消息: {user_message} 来自: {contact}")
            
            # 保存用户消息
            db.add_message(contact, "user", user_message)
            
            # 通知前端新消息
            socketio.emit('new_message', {
                'contact': contact,
                'role': 'user',
                'content': user_message
            })
            
            # 检查是否是定时任务请求
            is_reminder_request = False
            reminder_keywords = ['提醒我', '定时提醒', '闹钟', '定时发送', '定时', '提醒']
            time_keywords = ['分钟后', '小时后', '天后', '今天', '明天', '后天', '点', '点钟', '：', ':', '以后', '之后', '过后', '个小时', '个分钟']
            
            # 检查是否包含提醒关键词和时间关键词
            has_reminder_keyword = any(keyword in user_message for keyword in reminder_keywords)
            has_time_keyword = any(keyword in user_message for keyword in time_keywords)
            
            # 添加更详细的日志
            logger.info(f"[DEBUG] 提醒关键词检测: {has_reminder_keyword}, 匹配关键词: {[kw for kw in reminder_keywords if kw in user_message]}")
            logger.info(f"[DEBUG] 时间关键词检测: {has_time_keyword}, 匹配关键词: {[kw for kw in time_keywords if kw in user_message]}")
            
            if has_reminder_keyword and has_time_keyword:
                is_reminder_request = True
                logger.info(f"检测到定时任务请求: {user_message}")
            
            # 如果是定时任务请求，尝试解析时间
            scheduled_time = None
            if is_reminder_request:
                # 先尝试使用正则表达式解析
                scheduled_time = parse_time_from_message(user_message)
                logger.info(f"[DEBUG] 正则表达式解析时间结果: {scheduled_time}")
                
                # 如果正则表达式解析失败，尝试使用大模型解析
                if not scheduled_time:
                    # 获取最近的对话历史作为上下文
                    recent_messages = db.get_messages(contact, 5)
                    context = "\n".join([f"{msg['role']}: {msg['content']}" for msg in recent_messages])
                    logger.info(f"[DEBUG] 尝试使用大模型解析时间，上下文长度: {len(context)}")
                    
                    # 导入时间解析模块
                    from agents.time_parser import parse_time_with_llm
                    scheduled_time = parse_time_with_llm(user_message, context, config)
                    
                    # 记录使用了大模型解析
                    if scheduled_time:
                        logger.info(f"使用大模型成功解析时间: {scheduled_time}")
                    else:
                        logger.warning(f"[DEBUG] 大模型解析时间失败，无法创建定时任务")
            
            # 如果成功解析到时间，创建定时任务
            if scheduled_time and scheduled_time > datetime.now():
                # 提取提醒内容
                # 使用大模型提取提醒内容
                reminder_content = extract_reminder_content_with_llm(user_message, scheduled_time)
                logger.info(f"[DEBUG] 提取的提醒内容: {reminder_content}")
                
                if not reminder_content:
                    # 如果大模型提取失败，回退到正则表达式
                    content_pattern = r'提醒我(.+?)(?:在|到|于|到了)'
                    content_match = re.search(content_pattern, user_message)
                    reminder_content = content_match.group(1).strip() if content_match else user_message
                    logger.info(f"[DEBUG] 使用正则表达式提取的提醒内容: {reminder_content}")
                
                # 检查提醒内容是否包含需要执行的任务
                task_type = None
                task_params = None
                
                logger.info(f"[DEBUG] 分析提醒内容是否包含任务: {reminder_content}")
                
                # 检查是否是天气查询
                weather_pattern = r'(查询|查看|获取|告诉我).*?([\u4e00-\u9fa5]{2,}市?|[\u4e00-\u9fa5]{2,}县).*?(天气|气温|温度)'
                weather_match = re.search(weather_pattern, reminder_content)
                
                # 更宽松的天气查询模式
                simple_weather_pattern = r'([\u4e00-\u9fa5]{2,}市?|[\u4e00-\u9fa5]{2,}县).*?(天气|气温|温度)'
                simple_weather_match = re.search(simple_weather_pattern, reminder_content)
                
                # 直接检查是否包含"天气"和城市名
                has_weather = '天气' in reminder_content or '气温' in reminder_content or '温度' in reminder_content
                
                if weather_match or simple_weather_match or has_weather:
                    task_type = "weather"
                    # 提取城市
                    city = None
                    
                    # 尝试从正则表达式匹配中提取城市
                    if weather_match:
                        city = weather_match.group(2)
                    elif simple_weather_match:
                        city = simple_weather_match.group(1)
                    
                    # 检查常见城市名称
                    common_cities = ['北京', '上海', '广州', '深圳', '杭州', '南京', '成都', '重庆', '武汉', '西安']
                    for common_city in common_cities:
                        if common_city in reminder_content:
                            city = common_city
                            break
                    
                    # 如果仍然没有找到城市，默认为北京
                    if not city:
                        city = "北京"
                        
                    task_params = json.dumps({"city": city})
                    logger.info(f"[DEBUG] 检测到天气查询任务，城市: {city}")
                
                # 检查是否是新闻查询
                news_pattern = r'(查询|查看|获取|告诉我).*?(新闻|资讯|热点)'
                news_match = re.search(news_pattern, reminder_content)
                
                # 更宽松的新闻查询模式
                simple_news_pattern = r'([\u4e00-\u9fa5]{2,})?(新闻|资讯|热点)'
                simple_news_match = re.search(simple_news_pattern, reminder_content)
                
                if news_match or simple_news_match or '新闻' in reminder_content:
                    task_type = "news"
                    # 提取类别
                    category = "综合"
                    
                    # 尝试从正则表达式匹配中提取类别
                    if news_match and len(news_match.groups()) > 1:
                        category_match = re.search(r'([\u4e00-\u9fa5]{2,})(?:新闻|资讯|热点)', reminder_content)
                        if category_match:
                            category = category_match.group(1)
                    elif simple_news_match and simple_news_match.group(1):
                        category = simple_news_match.group(1)
                    
                    # 检查常见新闻类别
                    common_categories = ['科技', '财经', '体育', '娱乐', '国际', '国内', '社会', '军事']
                    for common_category in common_categories:
                        if common_category in reminder_content:
                            category = common_category
                            break
                    
                    task_params = json.dumps({"category": category})
                    logger.info(f"[DEBUG] 检测到新闻查询任务，类别: {category}")
                
                # 如果无法通过规则识别，尝试使用大模型识别
                if not task_type and len(reminder_content) > 5:  # 内容足够长，可能包含任务
                    try:
                        from agents.agent_base import detect_task_with_llm
                        task_info = detect_task_with_llm(reminder_content, contact, config)
                        if task_info:
                            task_type = task_info.get("task_type")
                            params = task_info.get("params", {})
                            if task_type:
                                task_params = json.dumps(params)
                                logger.info(f"[DEBUG] 使用大模型检测到任务，类型: {task_type}, 参数: {params}")
                    except Exception as e:
                        logger.error(f"使用大模型识别任务时出错: {str(e)}")
                
                # 创建定时任务
                task_id, scheduled_time = create_scheduled_task(
                    contact, 
                    reminder_content, 
                    scheduled_time,
                    task_type=task_type,
                    task_params=task_params
                )
                
                logger.info(f"[DEBUG] 成功创建定时任务: ID={task_id}, 时间={scheduled_time}, 内容={reminder_content}")
                
                # 回复用户
                formatted_time = scheduled_time.strftime("%Y年%m月%d日 %H:%M")
                ai_response = f"好的，我会在{formatted_time}提醒您：{reminder_content}"
                
                # 保存AI响应
                db.add_message(contact, "assistant", ai_response)
                
                # 通知前端AI响应
                socketio.emit('new_message', {
                    'contact': contact,
                    'role': 'assistant',
                    'content': ai_response
                })
                
                # 发送回复
                send_imessage(contact, ai_response)
                logger.info(f"已设置定时任务 {task_id} 并回复消息: {ai_response} 给: {contact}")
            else:
                # 尝试检测并执行自动任务
                agent_response = detect_and_execute_agent_task(user_message, contact)
                
                if agent_response:
                    # 保存AI响应
                    db.add_message(contact, "assistant", agent_response)
                    
                    # 通知前端AI响应
                    socketio.emit('new_message', {
                        'contact': contact,
                        'role': 'assistant',
                        'content': agent_response
                    })
                    
                    # 发送回复
                    send_imessage(contact, agent_response)
                    logger.info(f"已执行自动任务并回复消息: {agent_response} 给: {contact}")
                else:
                    # 获取AI响应
                    ai_response = get_ai_response(db.get_messages(contact, config.max_history_length), contact)
                    
                    # 保存AI响应
                    db.add_message(contact, "assistant", ai_response)
                    
                    # 通知前端AI响应
                    socketio.emit('new_message', {
                        'contact': contact,
                        'role': 'assistant',
                        'content': ai_response
                    })
                    
                    # 发送回复
                    send_imessage(contact, ai_response)
                    logger.info(f"已回复消息: {ai_response} 给: {contact}")
            
    except Exception as e:
        logger.error(f"处理消息时出错: {str(e)}")

# 使用大模型提取提醒内容
def extract_reminder_content_with_llm(message, scheduled_time):
    """
    使用大模型从消息中提取提醒内容
    
    Args:
        message: 用户消息
        scheduled_time: 已解析的时间
        
    Returns:
        提取的提醒内容或None
    """
    try:
        # 格式化时间为易读格式
        formatted_time = scheduled_time.strftime("%Y年%m月%d日 %H:%M")
        
        # 构建提示
        system_prompt = """你是一个专门提取提醒内容的AI助手。
用户的消息中包含了一个提醒请求和时间信息。
我已经确定提醒时间是: {formatted_time}
请从用户消息中提取出用户想要被提醒的具体内容或事项。
如果消息中包含天气查询、新闻查询等特定任务，请确保完整提取这些信息。
例如，如果用户说"一分钟后告诉我上海天气"，应提取"上海天气"而不仅仅是"天气"。
如果用户说"一个小时以后提醒我取快递，取件码是123456"，应提取"取快递，取件码是123456"。
只返回提取出的内容，不要包含任何其他解释或格式。
如果无法确定具体内容，请返回null。"""
        
        user_prompt = f"从以下消息中提取提醒内容: {message}"
        
        # 添加日志记录
        logger.info(f"[DEBUG] 尝试使用LLM提取提醒内容: {message}")
        
        # 调用AI模型
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": config.model_name,
            "messages": [
                {"role": "system", "content": system_prompt.format(formatted_time=formatted_time)},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.2
        }
        
        response = requests.post(config.get_full_api_url(), json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        
        # 解析响应
        content = response.json()["choices"][0]["message"]["content"].strip()
        
        # 如果返回null或空字符串，则返回None
        if content.lower() == "null" or not content:
            logger.warning(f"[DEBUG] LLM提取提醒内容返回空或null")
            return None
            
        logger.info(f"成功提取提醒内容: {content}")
        return content
        
    except Exception as e:
        logger.error(f"使用LLM提取提醒内容时出错: {str(e)}")
        return None

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
    测试AI响应
    """
    try:
        data = request.json
        # 使用配置中的值，但允许请求中的值覆盖
        temp_config = Config()
        temp_config.from_dict(data)
        
        # 使用简单的测试提示
        prompt = data.get('prompt', '你好，这是一个测试消息。请简短回复。')
        
        if not temp_config.is_valid():
            return jsonify({
                "status": "error",
                "message": "API配置无效，请检查API密钥、URL和模型名称"
            })
        
        # 构建测试消息上下文
        test_context = [{"role": "user", "content": prompt}]
        
        # 构建请求
        headers = {
            "Authorization": f"Bearer {temp_config.api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": temp_config.model_name,
            "messages": [{"role": "system", "content": temp_config.system_prompt}] + test_context,
                         "temperature": temp_config.temperature
        }
        
        # 发送请求
        response = requests.post(temp_config.get_full_api_url(), json=payload, headers=headers, timeout=15)
        response.raise_for_status()
        
        # 解析响应
        ai_response = response.json()["choices"][0]["message"]["content"]
        
        return jsonify({
            "status": "success",
            "message": "连接测试成功！",
            "response": ai_response
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

@app.route('/scheduled_tasks', methods=['GET'])
def get_tasks():
    """
    获取定时任务列表
    """
    try:
        contact = request.args.get('contact')
        include_executed = request.args.get('include_executed', 'false').lower() == 'true'
        only_recurring = request.args.get('only_recurring', 'false').lower() == 'true'
        
        tasks = db.get_scheduled_tasks(contact, include_executed, only_recurring)
        result = []
        
        for task in tasks:
            if len(task) >= 11:  # 确保任务包含所有字段
                task_id, contact, message, scheduled_time, created_at, executed, job_id, is_recurring, recurring_type, recurring_value, next_run_time = task
            else:
                # 兼容旧版本数据
                task_id, contact, message, scheduled_time, created_at, executed, job_id = task
                is_recurring = False
                recurring_type = None
                recurring_value = None
                next_run_time = None
            
            # 确定显示的执行时间
            display_time = next_run_time if is_recurring and next_run_time else scheduled_time
            
            task_info = {
                "id": task_id,
                "contact": contact,
                "message": message,
                "scheduled_time": scheduled_time,
                "display_time": display_time,
                "created_at": created_at,
                "executed": bool(executed),
                "is_recurring": bool(is_recurring),
                "recurring_type": recurring_type,
                "recurring_value": recurring_value,
                "next_run_time": next_run_time
            }
            result.append(task_info)
        
        return jsonify({
            "status": "success",
            "tasks": result
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"获取任务失败: {str(e)}"
        })

@app.route('/scheduled_tasks/<int:task_id>', methods=['DELETE'])
def delete_task(task_id):
    """
    删除定时任务
    """
    try:
        # 获取任务信息
        tasks = db.get_scheduled_tasks()
        task = None
        for t in tasks:
            if t[0] == task_id:
                task = t
                break
        
        if not task:
            return jsonify({
                "status": "error",
                "message": f"任务 {task_id} 不存在"
            })
        
        # 从调度器中移除任务
        job_id = task[6]
        if job_id:
            try:
                scheduler.remove_job(job_id)
            except Exception as e:
                logger.warning(f"从调度器中移除任务 {task_id} 时出错: {str(e)}")
        
        # 从数据库中删除任务
        db.delete_task(task_id)
        
        return jsonify({
            "status": "success",
            "message": f"任务 {task_id} 已删除"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"删除任务失败: {str(e)}"
        })

@app.route('/scheduled_tasks', methods=['POST'])
def create_task():
    """
    手动创建定时任务
    """
    try:
        data = request.json
        contact = data.get('contact')
        message = data.get('message')
        scheduled_time_str = data.get('scheduled_time')
        
        # 循环任务参数
        is_recurring = data.get('is_recurring', False)
        recurring_type = data.get('recurring_type')
        recurring_value = data.get('recurring_value')
        
        # 自动化任务参数
        task_type = data.get('task_type')
        task_params = data.get('task_params')
        
        if not contact or not scheduled_time_str:
            return jsonify({
                "status": "error",
                "message": "缺少必要参数"
            })
        
        # 如果没有消息但有任务类型，则允许创建
        if not message and not task_type:
            return jsonify({
                "status": "error",
                "message": "必须提供消息内容或任务类型"
            })
        
        try:
            scheduled_time = parser.parse(scheduled_time_str)
        except Exception:
            return jsonify({
                "status": "error",
                "message": "无效的时间格式"
            })
        
        if scheduled_time <= datetime.now():
            return jsonify({
                "status": "error",
                "message": "定时时间必须在未来"
            })
        
        # 验证循环任务参数
        if is_recurring:
            if not recurring_type or not recurring_value:
                return jsonify({
                    "status": "error",
                    "message": "循环任务必须指定循环类型和值"
                })
            
            if recurring_type not in ['minutely', 'hourly', 'daily', 'weekly', 'monthly']:
                return jsonify({
                    "status": "error",
                    "message": "无效的循环类型，支持的类型：minutely, hourly, daily, weekly, monthly"
                })
            
            try:
                recurring_value = int(recurring_value)
                if recurring_value <= 0:
                    raise ValueError("循环值必须大于0")
            except ValueError:
                return jsonify({
                    "status": "error",
                    "message": "循环值必须是正整数"
                })
        
        # 验证任务类型
        if task_type and task_type not in ['weather', 'news', 'reminder', 'search', 'calculate', 'translate']:
            return jsonify({
                "status": "error",
                "message": "无效的任务类型，支持的类型：weather, news, reminder, search, calculate, translate"
            })
        
        # 如果是天气任务，但没有提供参数，则添加默认参数
        if task_type == 'weather' and not task_params:
            task_params = json.dumps({"city": "北京"})
        
        # 如果是新闻任务，但没有提供参数，则添加默认参数
        if task_type == 'news' and not task_params:
            task_params = json.dumps({"category": "科技"})
        
        # 创建定时任务
        task_id, scheduled_time = create_scheduled_task(
            contact, 
            message or "自动化任务", 
            scheduled_time,
            is_recurring=is_recurring,
            recurring_type=recurring_type,
            recurring_value=recurring_value,
            task_type=task_type,
            task_params=task_params
        )
        
        return jsonify({
            "status": "success",
            "task_id": task_id,
            "scheduled_time": scheduled_time.strftime("%Y-%m-%d %H:%M:%S"),
            "is_recurring": is_recurring,
            "recurring_type": recurring_type,
            "recurring_value": recurring_value,
            "task_type": task_type,
            "task_params": task_params
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"创建任务失败: {str(e)}"
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

# 时间解析和定时任务创建
def parse_time_from_message(message):
    """
    从消息中解析时间
    支持的格式：
    - 今天/明天/后天 + 时间（如：今天下午3点，明天晚上8点半）
    - 具体日期 + 时间（如：2023年1月1日上午10点，1月15日下午2点30分）
    - 相对时间（如：5分钟后，2小时后，3天后）
    """
    now = datetime.now()
    
    # 相对时间模式
    relative_pattern = r'(\d+)\s*(分钟|小时|天)后'
    relative_match = re.search(relative_pattern, message)
    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2)
        if unit == '分钟':
            return now + timedelta(minutes=amount)
        elif unit == '小时':
            return now + timedelta(hours=amount)
        elif unit == '天':
            return now + timedelta(days=amount)
    
    # 更宽松的相对时间模式（如"一分钟后"、"两小时后"等）
    chinese_num_map = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    chinese_relative_pattern = r'([一二两三四五六七八九十])\s*(分钟|小时|天)后'
    chinese_relative_match = re.search(chinese_relative_pattern, message)
    if chinese_relative_match:
        amount = chinese_num_map.get(chinese_relative_match.group(1), 1)
        unit = chinese_relative_match.group(2)
        if unit == '分钟':
            return now + timedelta(minutes=amount)
        elif unit == '小时':
            return now + timedelta(hours=amount)
        elif unit == '天':
            return now + timedelta(days=amount)
    
    # 今天/明天/后天模式
    day_pattern = r'(今天|明天|后天)'
    day_match = re.search(day_pattern, message)
    if day_match:
        day_offset = {'今天': 0, '明天': 1, '后天': 2}[day_match.group(1)]
        target_date = now + timedelta(days=day_offset)
        
        # 提取时间
        time_pattern = r'(上午|中午|下午|晚上)?\s*(\d+)(?:点|时)(?:(\d+)分?)?'
        time_match = re.search(time_pattern, message)
        if time_match:
            period = time_match.group(1) or ''
            hour = int(time_match.group(2))
            minute = int(time_match.group(3) or 0)
            
            # 调整小时
            if period == '下午' or period == '晚上':
                if hour < 12:
                    hour += 12
            elif period == '上午' and hour == 12:
                hour = 0
                
            return target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    
    # 具体日期模式
    date_pattern = r'(\d+)月(\d+)日'
    date_match = re.search(date_pattern, message)
    if date_match:
        month = int(date_match.group(1))
        day = int(date_match.group(2))
        year = now.year
        
        # 如果指定的月份已经过去，假设是明年
        if month < now.month or (month == now.month and day < now.day):
            year += 1
            
        # 提取时间
        time_pattern = r'(上午|中午|下午|晚上)?\s*(\d+)(?:点|时)(?:(\d+)分?)?'
        time_match = re.search(time_pattern, message)
        if time_match:
            period = time_match.group(1) or ''
            hour = int(time_match.group(2))
            minute = int(time_match.group(3) or 0)
            
            # 调整小时
            if period == '下午' or period == '晚上':
                if hour < 12:
                    hour += 12
            elif period == '上午' and hour == 12:
                hour = 0
                
            try:
                return datetime(year, month, day, hour, minute, 0)
            except ValueError:
                # 处理无效日期
                return None
    
    return None

def create_scheduled_task(contact, message, scheduled_time, reminder_message=None, is_recurring=False, recurring_type=None, recurring_value=None, task_type=None, task_params=None):
    """
    创建定时任务
    
    Args:
        contact: 联系人
        message: 消息内容
        scheduled_time: 计划执行时间
        reminder_message: 提醒消息（可选）
        is_recurring: 是否是循环任务
        recurring_type: 循环类型（minutely, hourly, daily, weekly, monthly）
        recurring_value: 循环值
        task_type: 任务类型（weather, news, reminder, search, calculate, translate）
        task_params: 任务参数（JSON字符串）
    
    Returns:
        (task_id, scheduled_time): 任务ID和计划执行时间
    """
    if not reminder_message:
        reminder_message = "这是您之前设置的提醒消息：" + message
    
    # 如果是循环任务，设置下一次执行时间为初始执行时间
    next_run_time = scheduled_time.strftime("%Y-%m-%d %H:%M:%S") if is_recurring else None
    
    # 如果有任务类型，记录日志
    if task_type:
        logger.info(f"创建自动化任务: {task_type}, 参数: {task_params}")
    
    # 保存到数据库
    task_id = db.add_scheduled_task(
        contact, 
        reminder_message, 
        scheduled_time.strftime("%Y-%m-%d %H:%M:%S"),
        is_recurring=is_recurring,
        recurring_type=recurring_type,
        recurring_value=recurring_value,
        next_run_time=next_run_time,
        task_type=task_type,
        task_params=task_params
    )
    
    # 添加到调度器
    job = scheduler.add_job(
        execute_scheduled_task,
        'date',
        run_date=scheduled_time,
        args=[task_id, contact, reminder_message],
        id=f"task_{task_id}" if not is_recurring else f"task_{task_id}_{scheduled_time.strftime('%Y%m%d%H%M%S')}"
    )
    
    # 更新任务的job_id
    db.update_task_job_id(task_id, job.id)
    
    logger.info(f"已创建{'循环' if is_recurring else '一次性'}任务 {task_id}，执行时间: {scheduled_time}")
    if is_recurring:
        logger.info(f"循环类型: {recurring_type}，值: {recurring_value}")
    
    return task_id, scheduled_time

# 自动任务执行
def execute_agent_task(task_type, params, contact):
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
    logger.info(f"[message_ai_service] 开始执行任务: {task_type}, 参数: {params}, 联系人: {contact}")
    
    # 将create_scheduled_task函数添加到config对象中，以便ReminderAgent可以使用
    config.create_scheduled_task = create_scheduled_task
    logger.info(f"[message_ai_service] 已将create_scheduled_task函数添加到config对象中")
    
    # 使用agents模块中的execute_agent_task函数
    from agents.agent_base import execute_agent_task as agent_execute_task
    logger.info(f"[message_ai_service] 已导入agents.agent_base中的execute_agent_task函数")
    
    result = agent_execute_task(task_type, params, contact, config)
    logger.info(f"[message_ai_service] 任务执行结果: {result}")
    return result

# 识别并执行自动任务
def detect_and_execute_agent_task(message, contact):
    """
    识别并执行自动任务
    """
    logger.info(f"[message_ai_service] 开始识别并执行自动任务: {message}, 联系人: {contact}")
    
    # 将create_scheduled_task函数添加到config对象中，以便ReminderAgent可以使用
    config.create_scheduled_task = create_scheduled_task
    logger.info(f"[message_ai_service] 已将create_scheduled_task函数添加到config对象中")
    
    # 使用agents模块中的detect_and_execute_agent_task函数
    from agents.agent_base import detect_and_execute_agent_task as agent_detect_and_execute
    logger.info(f"[message_ai_service] 已导入agents.agent_base中的detect_and_execute_agent_task函数")
    
    result = agent_detect_and_execute(message, contact, config)
    logger.info(f"[message_ai_service] 任务执行结果: {result}")
    return result

if __name__ == '__main__':
    # 启动消息监控线程
    monitor_thread = threading.Thread(target=start_message_monitor, daemon=True)
    monitor_thread.start()
    
    # 启动自动清理数据线程
    cleanup_thread = threading.Thread(target=auto_cleanup_data, daemon=True)
    cleanup_thread.start()
    
    try:
        # 启动WebSocket服务
        logger.info("启动Web服务...")
        socketio.run(app, host='0.0.0.0', port=8888, debug=False)
    except KeyboardInterrupt:
        logger.info("正在关闭服务...")
    finally:
        # 关闭调度器
        scheduler.shutdown()
        logger.info("调度器已关闭") 