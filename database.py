"""
Forest 插件数据库操作封装
使用 SQLite 存储打卡记录和学习主题
"""

import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)


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

            # 树种推送记录表
            conn.execute("""CREATE TABLE IF NOT EXISTS pushed_trees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tree_id TEXT NOT NULL UNIQUE,
                pushed_at DATETIME NOT NULL
            )""")

            # 晚安车报名表
            conn.execute("""CREATE TABLE IF NOT EXISTS night_bus_signups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_name TEXT,
                signup_time DATETIME NOT NULL,
                signup_date TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, group_id, signup_date)
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_night_bus_lookup
                ON night_bus_signups(group_id, signup_date)""")

            # 兼容旧表：添加 prefered_time 和 prefered_tree 字段（已有则跳过）
            try:
                conn.execute("ALTER TABLE night_bus_signups ADD COLUMN preferred_time TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE night_bus_signups ADD COLUMN preferred_tree TEXT")
            except sqlite3.OperationalError:
                pass

            # 房间密钥映射表（用于撤回同步）
            conn.execute("""CREATE TABLE IF NOT EXISTS room_key_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                original_msg_id INTEGER NOT NULL,
                reply_msg_id INTEGER NOT NULL,
                room_key TEXT NOT NULL,
                user_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_room_key_lookup
                ON room_key_mappings(group_id, original_msg_id)""")

            # 专注统计表
            conn.execute("""CREATE TABLE IF NOT EXISTS focus_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT,
                room_key TEXT NOT NULL,
                duration_minutes INTEGER,
                tree_name TEXT,
                tree_name_en TEXT,
                focused_at DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_focus_user
                ON focus_sessions(user_id, group_id)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_focus_date
                ON focus_sessions(focused_at)""")

            # 兼容旧表：添加 original_msg_id 字段（已有则跳过）
            try:
                conn.execute("ALTER TABLE focus_sessions ADD COLUMN original_msg_id INTEGER")
            except sqlite3.OperationalError:
                pass

            conn.execute("""CREATE INDEX IF NOT EXISTS idx_focus_msg_id
                ON focus_sessions(group_id, original_msg_id)""")
            conn.execute("""CREATE INDEX IF NOT EXISTS idx_focus_room_key
                ON focus_sessions(group_id, room_key)""")

            # 倒计时事件表
            conn.execute("""CREATE TABLE IF NOT EXISTS countdown_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target_date TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            # 插入默认高考倒计时（仅当表为空时）
            cursor = conn.execute("SELECT COUNT(*) FROM countdown_events")
            if cursor.fetchone()[0] == 0:
                conn.execute(
                    "INSERT INTO countdown_events (name, target_date) VALUES (?, ?)",
                    ("高考", "06-07")
                )

            conn.commit()

    # === 打卡相关 ===

    def has_checked_today(self, user_id: str, group_id: str) -> bool:
        """查询今日是否已打卡"""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """SELECT 1 FROM checkins
                       WHERE user_id = ? AND group_id = ? AND checkin_date = ?""",
                    (user_id, group_id, today)
                )
                return cursor.fetchone() is not None
        except sqlite3.Error as e:
            logger.error(f"查询打卡状态失败: {e}")
            return False

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
        try:
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
        except sqlite3.Error:
            return []

    def get_user_week_days(self, user_id: str, group_id: str) -> int:
        """获取用户本周打卡天数"""
        today = datetime.now()
        week_start = today - timedelta(days=today.weekday())
        week_start_str = week_start.strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """SELECT COUNT(DISTINCT checkin_date)
                       FROM checkins
                       WHERE user_id = ? AND group_id = ? AND checkin_date >= ?""",
                    (user_id, group_id, week_start_str)
                )
                result = cursor.fetchone()
                return result[0] if result else 0
        except sqlite3.Error:
            return 0

    def get_user_checkin_dates(self, user_id: str, group_id: str) -> List[str]:
        """获取用户本周打卡日期列表"""
        today = datetime.now()
        week_start = today - timedelta(days=today.weekday())
        week_start_str = week_start.strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """SELECT DISTINCT checkin_date
                       FROM checkins
                       WHERE user_id = ? AND group_id = ? AND checkin_date >= ?
                       ORDER BY checkin_date ASC""",
                    (user_id, group_id, week_start_str)
                )
                return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error:
            return []

    def get_user_missed_days(self, user_id: str, group_id: str) -> List[str]:
        """获取用户本周未打卡日期列表（截至今天）"""
        today = datetime.now()
        week_start = today - timedelta(days=today.weekday())

        # 本周所有日期（从周一到今天）
        all_dates = []
        current = week_start
        while current <= today:
            all_dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)

        # 已打卡日期
        checked_dates = set(self.get_user_checkin_dates(user_id, group_id))

        # 未打卡日期
        missed = [d for d in all_dates if d not in checked_dates]
        return missed

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
        except sqlite3.Error as e:
            logger.error(f"标记主题推送状态失败: {e}")
            return False

    def reset_all_topics(self) -> bool:
        """重置所有主题状态（循环重播）"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""UPDATE topics SET pushed_at = NULL""")
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"重置主题状态失败: {e}")
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
        except sqlite3.Error as e:
            logger.error(f"删除主题失败: {e}")
            return False

    def get_topic_count(self) -> int:
        """获取主题总数"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""SELECT COUNT(*) FROM topics""")
                return cursor.fetchone()[0]
        except sqlite3.Error:
            return 0

    # === 树种推送相关 ===

    def mark_tree_pushed(self, tree_id: str) -> bool:
        """标记树种已推送"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO pushed_trees (tree_id, pushed_at)
                       VALUES (?, ?)""",
                    (tree_id, datetime.now())
                )
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"标记树种推送状态失败: {e}")
            return False

    def get_pushed_tree_ids(self) -> set:
        """获取已推送的树种ID集合"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""SELECT tree_id FROM pushed_trees""")
                return {row[0] for row in cursor.fetchall()}
        except sqlite3.Error:
            return set()

    def reset_all_trees(self) -> bool:
        """重置所有树种推送状态"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""DELETE FROM pushed_trees""")
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"重置树种推送状态失败: {e}")
            return False

    def get_pushed_tree_count(self) -> int:
        """获取已推送树种数量"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""SELECT COUNT(*) FROM pushed_trees""")
                return cursor.fetchone()[0]
        except sqlite3.Error:
            return 0

    def get_today_pushed_tree(self) -> Optional[Tuple[str, str]]:
        """
        获取今天推送的树种
        返回 (tree_id, pushed_at) 或 None
        """
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """SELECT tree_id, pushed_at FROM pushed_trees 
                       WHERE DATE(pushed_at) = ?
                       ORDER BY pushed_at DESC
                       LIMIT 1""",
                    (today,)
                )
                result = cursor.fetchone()
                return (result[0], result[1]) if result else None
        except sqlite3.Error:
            return None

    # === 晚安车报名相关 ===

    def signup_night_bus(
        self, user_id: str, group_id: str, user_name: str = None,
        preferred_time: str = None, preferred_tree: str = None
    ) -> bool:
        """报名晚安车，返回是否成功（False 表示已报名）
        每次报名都会更新用户昵称和时间/树种偏好
        """
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """INSERT INTO night_bus_signups
                       (user_id, group_id, user_name, signup_time, signup_date, preferred_time, preferred_tree)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, group_id, signup_date)
                       DO UPDATE SET
                         user_name = excluded.user_name,
                         signup_time = excluded.signup_time,
                         preferred_time = excluded.preferred_time,
                         preferred_tree = excluded.preferred_tree""",
                    (user_id, group_id, user_name, now, today, preferred_time, preferred_tree)
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"报名晚安车失败: {e}")
            return False

    def cancel_night_bus(self, user_id: str, group_id: str) -> bool:
        """取消晚安车报名，返回是否成功"""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """DELETE FROM night_bus_signups
                       WHERE user_id = ? AND group_id = ? AND signup_date = ?""",
                    (user_id, group_id, today)
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"取消晚安车报名失败: {e}")
            return False

    def get_night_bus_signups(
        self, group_id: str
    ) -> List[Tuple[str, str, str, str]]:
        """获取今日晚安车报名列表，返回 [(user_id, user_name, preferred_time, preferred_tree)]"""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """SELECT user_id, user_name, preferred_time, preferred_tree
                       FROM night_bus_signups
                       WHERE group_id = ? AND signup_date = ?
                       ORDER BY signup_time ASC""",
                    (group_id, today)
                )
                return cursor.fetchall()
        except sqlite3.Error:
            return []

    def get_night_bus_count(self, group_id: str) -> int:
        """获取今日晚安车报名人数"""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """SELECT COUNT(*) FROM night_bus_signups
                       WHERE group_id = ? AND signup_date = ?""",
                    (group_id, today)
                )
                return cursor.fetchone()[0]
        except sqlite3.Error:
            return 0

    def get_user_night_bus_count(self, user_id: str, group_id: str) -> int:
        """获取用户累计参加晚安车次数"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """SELECT COUNT(DISTINCT signup_date) FROM night_bus_signups
                       WHERE user_id = ? AND group_id = ?""",
                    (user_id, group_id)
                )
                return cursor.fetchone()[0]
        except sqlite3.Error:
            return 0

    def get_group_night_bus_stats(self, group_id: str, days: int = 7) -> dict:
        """获取群晚安车统计（最近 N 天）"""
        date_limit = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                # 总发车次数（报名人数 > 2 的天数）
                cursor = conn.execute(
                    """SELECT signup_date, COUNT(*) as cnt FROM night_bus_signups
                       WHERE group_id = ? AND signup_date >= ?
                       GROUP BY signup_date
                       HAVING cnt > 2""",
                    (group_id, date_limit)
                )
                bus_days = cursor.fetchall()
                # 总报名人次
                cursor = conn.execute(
                    """SELECT COUNT(*) FROM night_bus_signups
                       WHERE group_id = ? AND signup_date >= ?""",
                    (group_id, date_limit)
                )
                total_signups = cursor.fetchone()[0]
                # 最活跃乘客
                cursor = conn.execute(
                    """SELECT user_name, COUNT(*) as cnt FROM night_bus_signups
                       WHERE group_id = ? AND signup_date >= ?
                       GROUP BY user_id
                       ORDER BY cnt DESC
                       LIMIT 3""",
                    (group_id, date_limit)
                )
                top_passengers = cursor.fetchall()

            return {
                "bus_days": len(bus_days),
                "total_signups": total_signups,
                "top_passengers": [(name or "未知", cnt) for name, cnt in top_passengers]
            }
        except sqlite3.Error:
            return {
                "bus_days": 0,
                "total_signups": 0,
                "top_passengers": []
            }

    # === 房间密钥映射（撤回同步） ===

    def save_room_key_mapping(self, group_id: str, original_msg_id: int,
                               reply_msg_id: int, room_key: str,
                               user_id: str = None) -> bool:
        """保存房间密钥消息映射"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO room_key_mappings
                       (group_id, original_msg_id, reply_msg_id, room_key, user_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (group_id, original_msg_id, reply_msg_id, room_key, user_id)
                )
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error(f"保存房间密钥映射失败: {e}")
            return False

    def find_room_key_mapping(self, group_id: str, original_msg_id: int) -> Optional[dict]:
        """查找房间密钥映射"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """SELECT id, group_id, original_msg_id, reply_msg_id, room_key, user_id, created_at
                       FROM room_key_mappings
                       WHERE group_id = ? AND original_msg_id = ?""",
                    (group_id, original_msg_id)
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "id": row[0],
                        "group_id": row[1],
                        "original_msg_id": row[2],
                        "reply_msg_id": row[3],
                        "room_key": row[4],
                        "user_id": row[5],
                        "created_at": row[6]
                    }
                return None
        except sqlite3.Error as e:
            logger.error(f"查找房间密钥映射失败: {e}")
            return None

    def delete_room_key_mapping(self, group_id: str, original_msg_id: int) -> bool:
        """删除房间密钥映射"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """DELETE FROM room_key_mappings
                       WHERE group_id = ? AND original_msg_id = ?""",
                    (group_id, original_msg_id)
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"删除房间密钥映射失败: {e}")
            return False

    # === 专注统计相关 ===

    def save_focus_session(self, user_id: str, group_id: str, room_key: str,
                            duration_minutes: int = None, tree_name: str = None,
                            tree_name_en: str = None,
                            original_msg_id: int = None) -> bool:
        """保存一条专注记录（自动去重：同群同房间密钥不重复保存）"""
        now = datetime.now()
        try:
            with sqlite3.connect(self.db_path) as conn:
                # 去重检查 1：同群已存过相同房间密钥（且未被删除）→ 跳过
                if group_id:
                    cursor = conn.execute(
                        """SELECT 1 FROM focus_sessions
                           WHERE group_id = ? AND room_key = ?""",
                        (group_id, room_key)
                    )
                    if cursor.fetchone():
                        logger.debug(f"专注记录已存在（同群同密钥），跳过: group={group_id}, key={room_key}")
                        return True

                # 去重检查 2：同群同消息ID已存在 → 跳过（防重复处理）
                if original_msg_id is not None and group_id:
                    cursor = conn.execute(
                        """SELECT 1 FROM focus_sessions
                           WHERE group_id = ? AND original_msg_id = ?""",
                        (group_id, original_msg_id)
                    )
                    if cursor.fetchone():
                        logger.debug(f"专注记录已存在（同消息ID），跳过: group={group_id}, msg_id={original_msg_id}")
                        return True

                conn.execute(
                    """INSERT INTO focus_sessions
                       (user_id, group_id, room_key, duration_minutes,
                        tree_name, tree_name_en, focused_at, original_msg_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, group_id, room_key, duration_minutes,
                     tree_name, tree_name_en, now, original_msg_id)
                )
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"保存专注记录失败: {e}")
            return False

    def get_user_focus_stats(self, user_id: str, group_id: str = None,
                              days: int = None) -> dict:
        """
        获取用户专注统计
        返回: {total_sessions, total_minutes, avg_minutes, favorite_tree, recent_sessions}
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                # 基础筛选条件
                conditions = ["user_id = ?"]
                params = [user_id]
                if group_id:
                    conditions.append("group_id = ?")
                    params.append(group_id)
                if days is not None:
                    date_limit = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                    conditions.append("focused_at >= ?")
                    params.append(date_limit)

                where_clause = " AND ".join(conditions)

                # 总次数和总时长
                cursor = conn.execute(
                    f"""SELECT COUNT(*), COALESCE(SUM(duration_minutes), 0)
                       FROM focus_sessions WHERE {where_clause}""",
                    params
                )
                total_sessions, total_minutes = cursor.fetchone()

                # 最常用的树种
                cursor = conn.execute(
                    f"""SELECT COALESCE(tree_name, tree_name_en) as tree, COUNT(*) as cnt
                       FROM focus_sessions
                       WHERE {where_clause} AND (tree_name IS NOT NULL OR tree_name_en IS NOT NULL)
                       GROUP BY tree
                       ORDER BY cnt DESC
                       LIMIT 1""",
                    params
                )
                fav_row = cursor.fetchone()
                favorite_tree = fav_row[0] if fav_row else "未知"

                # 最近 5 条记录
                cursor = conn.execute(
                    f"""SELECT room_key, duration_minutes, tree_name, tree_name_en, focused_at
                       FROM focus_sessions
                       WHERE {where_clause}
                       ORDER BY focused_at DESC
                       LIMIT 5""",
                    params
                )
                recent = []
                for row in cursor.fetchall():
                    recent.append({
                        "room_key": row[0],
                        "duration": row[1],
                        "tree_name": row[2],
                        "tree_name_en": row[3],
                        "focused_at": row[4],
                    })

            avg_minutes = round(total_minutes / total_sessions, 1) if total_sessions > 0 else 0

            return {
                "total_sessions": total_sessions,
                "total_minutes": total_minutes,
                "avg_minutes": avg_minutes,
                "favorite_tree": favorite_tree,
                "recent_sessions": recent,
            }
        except sqlite3.Error as e:
            logger.error(f"查询专注统计失败: {e}")
            return {
                "total_sessions": 0,
                "total_minutes": 0,
                "avg_minutes": 0,
                "favorite_tree": "未知",
                "recent_sessions": [],
            }

    # === 今日专注总时长 ===

    def get_today_group_focus_minutes(self, group_id: str) -> int:
        """获取今日某群的专注总时长（分钟）"""
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """SELECT COALESCE(SUM(duration_minutes), 0)
                       FROM focus_sessions
                       WHERE group_id = ? AND focused_at >= ?""",
                    (group_id, today)
                )
                result = cursor.fetchone()
                return result[0] if result else 0
        except sqlite3.Error as e:
            logger.error(f"查询今日专注总时长失败: {e}")
            return 0

    def delete_focus_session_by_msg_id(self, group_id: str, original_msg_id: int) -> bool:
        """根据消息ID删除对应的专注记录（消息撤回时使用）"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """DELETE FROM focus_sessions
                       WHERE group_id = ? AND original_msg_id = ?""",
                    (group_id, original_msg_id)
                )
                conn.commit()
                if cursor.rowcount > 0:
                    logger.info(f"已删除撤回消息的专注记录: group={group_id}, msg_id={original_msg_id}")
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"删除专注记录失败: {e}")
            return False

    def cleanup_old_room_key_mappings(self, hours: int = 48) -> int:
        """清理超过指定小时的旧映射记录，返回删除条数"""
        try:
            cut_time = datetime.now() - timedelta(hours=hours)
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """DELETE FROM room_key_mappings WHERE created_at < ?""",
                    (cut_time,)
                )
                conn.commit()
                deleted = cursor.rowcount
                if deleted:
                    logger.info(f"已清理 {deleted} 条旧的房间密钥映射")
                return deleted
        except sqlite3.Error as e:
            logger.error(f"清理旧映射失败: {e}")
            return 0

    # === 倒计时事件 ===

    def add_countdown_event(self, name: str, target_date: str) -> bool:
        """添加倒计时事件
        Args:
            name: 事件名称（如"高考"）
            target_date: 目标日期（如"06-07"表示每年6月7日，或"2026-12-25"表示具体日期）
        Returns:
            是否成功
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO countdown_events (name, target_date) VALUES (?, ?)",
                    (name, target_date)
                )
                conn.commit()
                logger.info(f"已添加倒计时事件: {name} -> {target_date}")
                return True
        except sqlite3.Error as e:
            logger.error(f"添加倒计时事件失败: {e}")
            return False

    def remove_countdown_event(self, event_id: int) -> bool:
        """删除倒计时事件"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "DELETE FROM countdown_events WHERE id = ?", (event_id,)
                )
                conn.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"删除倒计时事件失败: {e}")
            return False

    def list_countdown_events(self) -> list[tuple[int, str, str]]:
        """列出所有倒计时事件"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT id, name, target_date FROM countdown_events ORDER BY id"
                )
                return cursor.fetchall()
        except sqlite3.Error as e:
            logger.error(f"查询倒计时事件失败: {e}")
            return []

    def get_countdown_texts(self) -> tuple[list[str], list[str]]:
        """获取所有倒计时事件的计算结果文本列表

        Returns:
            tuple[list[str], list[str]]: (文本列表, 今天的倒计时事件名称列表)
        """
        events = self.list_countdown_events()
        if not events:
            return [], []

        texts = []
        today_events = []
        now = datetime.now()
        current_year = now.year

        for event_id, name, target_date in events:
            try:
                # 解析目标日期
                parts = target_date.split("-")
                if len(parts) == 2:
                    # 每年重复：MM-DD 格式
                    month, day = int(parts[0]), int(parts[1])
                    event_date = datetime(current_year, month, day)
                    # 如果已过，跳到下一年
                    if now.date() > event_date.date():
                        event_date = datetime(current_year + 1, month, day)
                elif len(parts) == 3:
                    # 具体日期：YYYY-MM-DD 格式
                    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                    event_date = datetime(year, month, day)
                    # 已过，不再显示
                    if now.date() > event_date.date():
                        continue
                else:
                    continue

                days = (event_date.date() - now.date()).days

                if days == 0:
                    texts.append(f"🎯 今天{name}！")
                    today_events.append(name)
                elif days == 1:
                    texts.append(f"🎯 距离{name}还有1天（明天！加油！💪)")
                else:
                    texts.append(f"🎯 距离{name}还有{days}天")
            except (ValueError, IndexError):
                logger.warning(f"解析倒计时日期失败: {target_date}")
                continue

        return texts, today_events
