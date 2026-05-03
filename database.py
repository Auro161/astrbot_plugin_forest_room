"""
Forest 插件数据库操作封装
使用 SQLite 存储打卡记录和学习主题
"""

import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Tuple


class ForestDB:
    """Forest 插件数据库操作类"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化数据库表"""
        with sqlite3.connect(self.db_path) as conn:
            # 打卡记录表（添加唯一约束）
            conn.execute("""CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_name TEXT,
                checkin_time DATETIME NOT NULL,
                checkin_date TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, group_id, checkin_date)
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_checkin_lookup
                ON checkins(user_id, group_id, checkin_date)""")

            # 学习主题表
            conn.execute("""CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                pushed_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_topic_pushed ON topics(pushed_at)""")

            conn.commit()

    # === 打卡相关 ===

    def has_checked_today(self, user_id: str, group_id: str) -> bool:
        """查询今日是否已打卡"""
        today = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """SELECT 1 FROM checkins
                   WHERE user_id = ? AND group_id = ? AND checkin_date = ?""",
                (user_id, group_id, today)
            )
            return cursor.fetchone() is not None

    def checkin(self, user_id: str, group_id: str, user_name: str = None) -> bool:
        """
        打卡，返回是否成功
        False 表示今日已打卡
        使用 INSERT OR IGNORE 配合唯一约束避免重复打卡
        """
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO checkins
                       (user_id, group_id, user_name, checkin_time, checkin_date)
                       VALUES (?, ?, ?, ?, ?)""",
                    (user_id, group_id, user_name, now, today)
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            return False

    def get_week_rank(self, group_id: str, top_n: int = 5) -> List[Tuple[str, str, int]]:
        """
        获取本周打卡排行
        返回 [(user_id, user_name, 打卡天数)]
        """
        today = datetime.now()
        week_start = today - timedelta(days=today.weekday())
        week_start_str = week_start.strftime("%Y-%m-%d")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """SELECT user_id, MAX(user_name) as user_name, COUNT(DISTINCT checkin_date) as days
                   FROM checkins
                   WHERE group_id = ? AND checkin_date >= ?
                   GROUP BY user_id
                   ORDER BY days DESC
                   LIMIT ?""",
                (group_id, week_start_str, top_n)
            )
            return cursor.fetchall()

    def get_user_week_days(self, user_id: str, group_id: str) -> int:
        """获取用户本周打卡天数"""
        today = datetime.now()
        week_start = today - timedelta(days=today.weekday())
        week_start_str = week_start.strftime("%Y-%m-%d")

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """SELECT COUNT(DISTINCT checkin_date)
                   FROM checkins
                   WHERE user_id = ? AND group_id = ? AND checkin_date >= ?""",
                (user_id, group_id, week_start_str)
            )
            result = cursor.fetchone()
            return result[0] if result else 0

    # === 学习主题相关 ===

    def add_topic(self, content: str) -> int:
        """添加新主题，返回主题ID"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """INSERT INTO topics (content) VALUES (?)""",
                    (content,)
                )
                conn.commit()
                return cursor.lastrowid
        except sqlite3.Error:
            return -1

    def get_unpushed_topic(self) -> Optional[Tuple[int, str]]:
        """
        获取一个未推送的主题
        返回 (id, content) 或 None
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """SELECT id, content FROM topics
                       WHERE pushed_at IS NULL
                       ORDER BY id ASC
                       LIMIT 1"""
                )
                result = cursor.fetchone()
                return (result[0], result[1]) if result else None
        except sqlite3.Error:
            return None

    def mark_topic_pushed(self, topic_id: int) -> bool:
        """标记主题已推送"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """UPDATE topics SET pushed_at = ? WHERE id = ?""",
                    (datetime.now(), topic_id)
                )
                conn.commit()
            return True
        except sqlite3.Error:
            return False

    def reset_all_topics(self) -> bool:
        """重置所有主题状态（循环重播）"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""UPDATE topics SET pushed_at = NULL""")
                conn.commit()
            return True
        except sqlite3.Error:
            return False

    def list_topics(self) -> List[Tuple[int, str, Optional[str]]]:
        """
        列出所有主题
        返回 [(id, content, pushed_at)]
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """SELECT id, content, pushed_at FROM topics ORDER BY id ASC"""
                )
                return cursor.fetchall()
        except sqlite3.Error:
            return []

    def delete_topic(self, topic_id: int) -> bool:
        """删除主题，返回是否成功"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """DELETE FROM topics WHERE id = ?""",
                    (topic_id,)
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error:
            return False

    def get_topic_count(self) -> int:
        """获取主题总数"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""SELECT COUNT(*) FROM topics""")
                return cursor.fetchone()[0]
        except sqlite3.Error:
            return 0
