#!/usr/bin/env python3
import sqlite3
import os
import re
from datetime import datetime
import pandas as pd
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import threading
import queue


def decode_attributed_body(blob):
    """从 iMessage 的 attributedBody(苹果 typedstream 归档)里解析出纯文本。
    macOS 15.4+ 之后很多消息的文字只写进 attributedBody，message.text 为空。
    返回解析出的文本；失败返回 ''。
    """
    if not blob:
        return ''
    if isinstance(blob, str):
        try:
            blob = blob.encode('utf-8', 'surrogateescape')
        except Exception:
            return ''

    # 主路径：定位 NSString 标记后的长度前缀 + UTF-8 文本
    try:
        if b'NSString' in blob:
            s = blob.split(b'NSString', 1)[1]
            s = s[5:]  # 跳过类型元数据(\x01\x94\x84\x01+ 之类)
            marker = s[:1]
            # Apple typedstream 变长长度前缀：0x81→uint16, 0x82→uint32, 0x83→uint64，其余为单字节长度
            if marker == b'\x81':          # 2 字节小端长度
                ln = int.from_bytes(s[1:3], 'little'); txt = s[3:3 + ln]
            elif marker == b'\x82':         # 4 字节小端长度
                ln = int.from_bytes(s[1:5], 'little'); txt = s[5:5 + ln]
            elif marker == b'\x83':         # 8 字节小端长度（极少见）
                ln = int.from_bytes(s[1:9], 'little'); txt = s[9:9 + ln]
            elif s[0] < 0x80:               # 单字节长度(<=127)
                ln = s[0]; txt = s[1:1 + ln]
            else:                           # 未知的高位标记：留空，交给下面的启发式兜底
                txt = b''
            decoded = txt.decode('utf-8', 'replace').strip('\x00')
            if decoded.strip():
                return decoded
    except Exception:
        pass

    # 兜底：抠出最长的一段可见 UTF-8 文本(处理结构不匹配的边角情况)
    # 续接字符类里也允许 UTF-8 lead 字节(\xc2-\xf4)，否则中文这种多字节文本会在每个字处断开，
    # 反而被 'NSString' 这类 ASCII 元数据串比下去。
    try:
        candidates = re.findall(rb'[\x20-\x7e\xc2-\xf4][\x80-\xbf\x20-\x7e\xc2-\xf4]{1,}', blob)
        best = b''
        for cand in candidates:
            if len(cand) > len(best):
                best = cand
        guess = best.decode('utf-8', 'replace').strip()
        # 去掉尾部残留的元数据关键词
        for junk in ('iI', 'NSDictionary', 'NSObject', 'NSNumber', 'streamtyped'):
            guess = guess.split(junk)[0]
        return guess.strip()
    except Exception:
        return ''

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
        # 未确认的消息（callback 返回 False 表示没接住，下次连同新消息一起再喂）
        self.pending_messages = []
        self.last_pending_emitted_at = 0.0
        
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
           OR attributedBody IS NOT NULL
           OR EXISTS (
               SELECT 1 FROM message_attachment_join
               JOIN attachment ON message_attachment_join.attachment_id = attachment.ROWID
               WHERE message_attachment_join.message_id = message.ROWID
                 AND attachment.mime_type LIKE 'image/%'
           )
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
        """检查新消息。逻辑：
        1) 有 pending（上轮 callback 没接住）→ 不查 DB，仅 re-emit pending + 拉一轮比游标更新的
        2) 无 pending → 正常查 DB，把新消息 append 到 pending
        3) 推进 last_message_date（只要查过 DB 就推，避免 pending 失败时丢新消息）
        4) callback ack=True 时清空 pending；ack=False 时保留 pending
        """
        try:
            if not self.connection:
                if not self.connect():
                    return

            # 拉一轮新消息并入 pending；游标按“实际取到的行”的最大时间推进：
            #  - 读不到最新时间 → 跳过本轮
            #  - 还没有基线(last is None) → 用当前最新“建立基线”，绝不回历史消息
            #    （关键：否则 last=None 会让下面 fetch 退化成 WHERE date>0 = 全部历史 → 逐条乱回）
            #  - 查询失败(None) → 不推进，下轮重试，避免消息静默丢失
            #  - 有新行 → 推进到已取行的 max(original_date)，消除 get_latest 与 fetch 之间的
            #            TOCTOU 重复投递（中途落库的消息也会被 fetch 到并一并推进游标）
            #  - 无新行 → 推进到 current_latest，避免被 is_from_me/已过滤的行卡住
            current_latest_date = self.get_latest_message_date()
            if not current_latest_date:
                pass  # 读不到最新时间（如 DB 被锁），本轮跳过
            elif self.last_message_date is None:
                self.last_message_date = current_latest_date  # 建立基线，只处理之后到达的消息
            elif current_latest_date > self.last_message_date:
                fresh = self._fetch_new_messages()
                if fresh is None:
                    pass  # 查询失败，保持游标不动
                elif fresh:
                    self.pending_messages.extend(fresh)
                    try:
                        max_orig = max(int(m['original_date']) for m in fresh if m.get('original_date') is not None)
                        self.last_message_date = max(self.last_message_date or 0, max_orig)
                    except Exception:
                        self.last_message_date = current_latest_date
                else:
                    self.last_message_date = current_latest_date

            # 3) 把 pending 喂给 callback，ack 才清空
            if self.callback and self.pending_messages:
                try:
                    accepted = self.callback(list(self.pending_messages))
                except Exception as cb_err:
                    print(f"callback 抛出异常: {cb_err}")
                    accepted = False

                if accepted:
                    self.pending_messages = []
                else:
                    if len(self.pending_messages) > 200:
                        # 极端兜底：太旧的消息直接丢掉
                        self.pending_messages = self.pending_messages[-100:]
                    print(f"callback 未接住，保留 {len(self.pending_messages)} 条待重发")

        except Exception as e:
            print(f"检查新消息时出错: {str(e)}")
            # 如果发生错误，尝试重新连接
            self.connection = None

    def _fetch_new_messages(self):
        """执行 SQL 查询并把结果转成消息 dict 列表（不入 pending）"""
        # 安全兜底：没有有效游标时绝不拉取（否则 WHERE date>0 会把全部历史消息拉出来 → 乱回）
        if not self.last_message_date:
            return []
        query = """
                SELECT
                    datetime(message.date/1000000000 + strftime('%s', '2001-01-01'), 'unixepoch', 'localtime') AS message_date,
                    message.text,
                    message.attributedBody,
                    handle.id as contact,
                    message.is_from_me,
                    message.cache_roomnames AS group_chat,
                    message.date AS original_date,
                    message.ROWID AS message_rowid,
                    (
                        SELECT GROUP_CONCAT(attachment.filename || '||' || attachment.mime_type, char(10))
                        FROM message_attachment_join
                        JOIN attachment ON message_attachment_join.attachment_id = attachment.ROWID
                        WHERE message_attachment_join.message_id = message.ROWID
                          AND attachment.filename IS NOT NULL
                          AND attachment.mime_type LIKE 'image/%'
                    ) AS attachments
                FROM message
                LEFT JOIN handle ON message.handle_id = handle.ROWID
                WHERE message.date > ?
                  AND (message.text IS NOT NULL OR message.attributedBody IS NOT NULL OR EXISTS (
                      SELECT 1 FROM message_attachment_join
                      JOIN attachment ON message_attachment_join.attachment_id = attachment.ROWID
                      WHERE message_attachment_join.message_id = message.ROWID
                        AND attachment.mime_type LIKE 'image/%'
                  ))
                ORDER BY message.date ASC
                """
        try:
            df = pd.read_sql_query(query, self.connection, params=(self.last_message_date if self.last_message_date else 0,))
        except Exception as e:
            print(f"查询新消息 SQL 出错: {e}")
            return None  # None 表示查询失败（区别于“无新消息”的 []），调用方据此不推进游标

        result = []
        for _, row in df.iterrows():
            att_list = []
            raw = row['attachments']
            if raw:
                for entry in raw.split('\n'):
                    if '||' in entry:
                        path, mime = entry.split('||', 1)
                        expanded = os.path.expanduser(path)
                        att_list.append({'path': expanded, 'mime_type': mime, 'exists': os.path.exists(expanded)})

            # text 为空(macOS 15.4+ 常见)则从 attributedBody 解析出文字
            text_val = row['text'] or ''
            if not text_val.strip():
                decoded = decode_attributed_body(row['attributedBody'])
                if decoded:
                    text_val = decoded

            msg = {
                'date': row['message_date'],
                'contact': row['contact'],
                'text': text_val,
                'is_from_me': bool(row['is_from_me']),
                'group_chat': row['group_chat'],
                'original_date': row['original_date'],
                'attachments': att_list
            }
            result.append(msg)

            sender = "我" if msg['is_from_me'] else msg['contact']
            group_info = f" (群聊: {msg['group_chat']})" if msg['group_chat'] else ""
            att_info = f" [+{len(att_list)}图]" if att_list else ""
            text_preview = (msg['text'][:30] + '...') if msg['text'] else '[图片消息]'
            print(f"[{msg['date']}] {sender}{group_info}{att_info}: {text_preview}")

        return result
            
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
        self._stop = threading.Event()  # monitor_messages 的退出信号
        
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

        self._stop.clear()  # 支持停止后重新启动
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
            while not self._stop.is_set():  # 收到停止信号即退出，线程不再泄漏
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n停止监控消息")
            self.stop()
            
    def stop(self):
        """停止监控"""
        print("正在停止 iMessage 监控...")
        self._stop.set()  # 让 monitor_messages 的循环退出
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
        return True  # 必须返回真值，否则 reader 会保留 pending 反复重发

    reader = iMessageReader()
    reader.monitor_messages(callback=on_new_message) 