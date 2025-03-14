#!/usr/bin/env python3
import sqlite3
import os
from datetime import datetime
import pandas as pd
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import threading
import queue

class iMessageDatabaseHandler(FileSystemEventHandler):
    def __init__(self, event_queue):
        self.event_queue = event_queue
        self.last_event_time = 0
        self.cooldown = 0.5

    def on_modified(self, event):
        if event.src_path.endswith('chat.db'):
            current_time = time.time()
            if current_time - self.last_event_time >= self.cooldown:
                self.last_event_time = current_time
                self.event_queue.put('database_changed')

class DatabaseThread(threading.Thread):
    def __init__(self, db_path, event_queue, callback=None):
        super().__init__()
        self.db_path = db_path
        self.event_queue = event_queue
        self.callback = callback
        self.connection = None
        self.last_message_date = None
        self.running = True
        
    def connect(self):
        """连接到数据库"""
        try:
            self.connection = sqlite3.connect(self.db_path, timeout=5)
            return True
        except Exception as e:
            print(f"连接数据库时出错: {str(e)}")
            return False
            
    def get_latest_message_date(self):
        """获取最新消息的时间戳"""
        if not self.connection:
            if not self.connect():
                return None
                
        query = """
        SELECT MAX(date) AS latest_date
        FROM message 
        WHERE text IS NOT NULL
        """
        
        try:
            cursor = self.connection.cursor()
            cursor.execute(query)
            result = cursor.fetchone()
            return result[0] if result[0] else None
        except Exception as e:
            print(f"获取最新消息时间时出错: {str(e)}")
            return None
            
    def check_new_messages(self):
        """检查新消息"""
        try:
            if not self.connection:
                if not self.connect():
                    return

            current_latest_date = self.get_latest_message_date()
            
            if current_latest_date and (self.last_message_date is None or current_latest_date > self.last_message_date):
                query = """
                SELECT 
                    datetime(message.date/1000000000 + strftime('%s', '2001-01-01'), 'unixepoch', 'localtime') AS message_date,
                    message.text,
                    handle.id as contact,
                    message.is_from_me,
                    message.cache_roomnames,
                    message.date AS original_date
                FROM message 
                LEFT JOIN handle ON message.handle_id = handle.ROWID
                WHERE message.text IS NOT NULL
                AND message.date > ?
                ORDER BY message.date ASC
                """
                
                df = pd.read_sql_query(query, self.connection, params=(self.last_message_date if self.last_message_date else 0,))
                
                new_messages = []
                for _, row in df.iterrows():
                    msg = {
                        'date': row['message_date'],
                        'contact': row['contact'],
                        'text': row['text'],
                        'is_from_me': bool(row['is_from_me']),
                        'group_chat': row['cache_roomnames'],
                        'original_date': row['original_date']
                    }
                    new_messages.append(msg)
                    
                    # 打印新消息
                    sender = "我" if msg['is_from_me'] else msg['contact']
                    group_info = f" (群聊: {msg['group_chat']})" if msg['group_chat'] else ""
                    print(f"[{msg['date']}] {sender}{group_info}: {msg['text']}")
                
                if self.callback and new_messages:
                    self.callback(new_messages)
                
                self.last_message_date = current_latest_date
                
        except Exception as e:
            print(f"检查新消息时出错: {str(e)}")
            # 如果发生错误，尝试重新连接
            self.connection = None
            
    def run(self):
        """运行数据库线程"""
        print("数据库监控线程启动...")
        
        # 初始连接
        if not self.connect():
            print("无法连接到数据库，线程退出")
            return
            
        self.last_message_date = self.get_latest_message_date()
        print(f"初始化完成，最后消息时间戳: {self.last_message_date}")
        
        while self.running:
            try:
                # 等待文件系统事件，最多等待1秒
                try:
                    event = self.event_queue.get(timeout=1)
                    if event == 'database_changed':
                        self.check_new_messages()
                except queue.Empty:
                    # 即使没有事件，也定期检查一次
                    self.check_new_messages()
            except Exception as e:
                print(f"处理事件时出错: {str(e)}")
                time.sleep(1)
                
        # 关闭连接
        if self.connection:
            self.connection.close()
            
    def stop(self):
        """停止线程"""
        self.running = False

class iMessageReader:
    def __init__(self):
        self.db_path = os.path.expanduser("~/Library/Messages/chat.db")
        self.observer = None  # 添加 observer 属性
        self.db_thread = None  # 添加 db_thread 属性
        
    def check_db_access(self):
        """检查数据库文件是否存在且可访问"""
        if not os.path.exists(self.db_path):
            print(f"错误: 找不到数据库文件 {self.db_path}")
            print("请确保你使用的是 macOS 系统，并且有 iMessage 的聊天记录")
            return False
            
        if not os.access(self.db_path, os.R_OK):
            print(f"错误: 无法读取数据库文件 {self.db_path}")
            print("请按照以下步骤授予权限：")
            print("1. 打开'系统设置'")
            print("2. 进入'隐私与安全性' -> '完全磁盘访问权限'")
            print("3. 点击'+'号添加你的终端应用（Terminal.app 或 iTerm）")
            print("4. 确保该应用的开关是打开的")
            print("5. 重启终端应用")
            return False
        return True

    def monitor_messages(self, callback=None):
        """
        使用文件系统事件监控新消息
        
        Args:
            callback (callable): 收到新消息时的回调函数，接收消息列表作为参数
        """
        if not self.check_db_access():
            print("无法访问 iMessage 数据库，请确保已授予权限")
            return
            
        # 创建事件队列
        event_queue = queue.Queue()
        
        # 创建并启动数据库线程
        print("创建数据库线程...")
        self.db_thread = DatabaseThread(self.db_path, event_queue, callback)
        self.db_thread.start()
        
        # 创建文件系统观察者
        print("创建文件系统观察者...")
        self.observer = Observer()
        handler = iMessageDatabaseHandler(event_queue)
        
        # 获取数据库所在目录
        db_dir = os.path.dirname(self.db_path)
        print(f"准备监控目录: {db_dir}")
        self.observer.schedule(handler, db_dir, recursive=False)
        
        print("开始监控新消息...")
        print(f"监控数据库文件: {self.db_path}")
        
        try:
            self.observer.start()
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n停止监控消息")
            self.stop()
            
    def stop(self):
        """停止监控"""
        print("正在停止 iMessage 监控...")
        if self.observer:
            print("停止文件系统观察者...")
            self.observer.stop()
            self.observer.join()
            self.observer = None
            
        if self.db_thread:
            print("停止数据库线程...")
            self.db_thread.stop()
            self.db_thread.join()
            self.db_thread = None
        
        print("iMessage 监控已完全停止")

if __name__ == "__main__":
    # 使用示例
    def on_new_message(messages):
        print(f"收到 {len(messages)} 条新消息！")
    
    reader = iMessageReader()
    reader.monitor_messages(callback=on_new_message) 