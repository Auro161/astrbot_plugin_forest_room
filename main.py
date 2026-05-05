"""
Forest 房间密钥提取插件
监测 Forest 专注森林房间邀请消息，自动提取房间密钥并回复
支持打卡系统、定时通知、学习目标推送、AI 查询打卡记录
"""

import re
import time
import asyncio
import json
import random
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image
from astrbot.api import FunctionTool, ToolSet

# 不要使用这样的from astrbot.core.agent.tool import FunctionTool, ToolSet，这个会报错，需要使用这样的from astrbot.api import FunctionTool, ToolSet

from .database import ForestDB


class TreeManager:
    """树种数据管理器"""

    def __init__(self, plugin_dir: Path):
        self.plugin_dir = plugin_dir
        self.trees_data: dict = {}
        self.trees_list: list = []
        self._load_tree_data()

    def _load_tree_data(self):
        """加载树种数据"""
        tree_file = self.plugin_dir / "tree" / "tree_names.json"
        if tree_file.exists():
            try:
                with open(tree_file, "r", encoding="utf-8") as f:
                    self.trees_data = json.load(f)
                    self.trees_list = list(self.trees_data.items())
                    logger.info(f"已加载 {len(self.trees_list)} 个树种数据")
            except Exception as e:
                logger.error(f"加载树种数据失败: {e}")

    def get_tree_info(self, tree_id: str) -> dict | None:
        """获取单个树种信息"""
        return self.trees_data.get(tree_id)

    def get_all_tree_ids(self) -> list:
        """获取所有树种ID列表"""
        return list(self.trees_data.keys())

    def search_trees(self, keyword: str) -> list:
        """搜索树种（按中文名或英文名）"""
        results = []
        keyword_lower = keyword.lower()
        for tree_id, info in self.trees_list:
            zh_name = info.get("zh", "").lower()
            en_name = info.get("en", "").lower()
            if keyword_lower in zh_name or keyword_lower in en_name:
                results.append((tree_id, info))
        return results

    def get_tree_image_path(self, tree_id: str) -> Path | None:
        """获取树种图片路径"""
        tree_info = self.trees_data.get(tree_id)
        if not tree_info:
            return None

        # 安全处理文件名，防止路径遍历攻击
        en_name = re.sub(r'[^\w\-]', '_', tree_info.get("en", ""))
        zh_name = re.sub(r'[^\w\-]', '_', tree_info.get("zh", ""))
        safe_tree_id = re.sub(r'[^\w\-]', '_', tree_id)
        image_name = f"{safe_tree_id}_{en_name}_{zh_name}.webp"
        image_path = self.plugin_dir / "tree" / "mature_trees" / image_name

        # 额外检查：确保路径在预期目录内
        try:
            image_path.resolve().relative_to(self.plugin_dir.resolve())
        except ValueError:
            logger.warning(f"检测到非法路径访问: {image_name}")
            return None

        if image_path.exists():
            return image_path
        return None


class ForestRoomPlugin(Star):
    """Forest 房间密钥提取插件"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 初始化所有配置项
        self._init_configs()

        # === 数据库初始化 ===
        data_dir = StarTools.get_data_dir()
        db_path = data_dir / "forest.db"
        self.db = ForestDB(db_path)

        # === 树种数据加载 ===
        plugin_dir = Path(__file__).parent
        self.tree_manager = TreeManager(plugin_dir)

        # === 定时任务状态 ===
        self._last_check_minute = -1
        self._schedule_task = None
        self._platform_id: str | None = None

        # === 今日树种缓存 ===
        self._today_tree_message: str | None = None
        self._today_tree_id: str | None = None
        self._tree_cache_lock = asyncio.Lock()  # 树种缓存锁
        self._tree_push_lock = asyncio.Lock()  # 树种推送操作锁

        # === AI 树种查询缓存 ===
        self._ai_queried_tree_ids: list[str] = []
        self._ai_tree_lock = asyncio.Lock()  # AI 树种查询缓存锁

        # Forest 房间密钥正则表达式
        self.key_pattern = re.compile(r"输入我的房间密钥：([A-Z0-9]+)，和我一起")

        logger.info(f"Forest 房间密钥提取插件已加载，启用状态: {self.enabled}")

    def _get_config(self, key: str, default):
        """
        安全获取配置值，确保 None 时返回默认值

        Args:
            key: 配置键名
            default: 默认值

        Returns:
            配置值（如果为 None 则返回默认值）
        """
        value = self.config.get(key)
        return value if value is not None else default

    def _validate_fixed_reply_rules(self, rules: list) -> list:
        """
        验证并过滤无效的固定回复规则

        Args:
            rules: 原始规则列表

        Returns:
            有效的规则列表
        """
        valid_rules = []
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                logger.warning(f"固定回复规则 {i} 不是字典类型，已跳过")
                continue
            trigger_words = rule.get("trigger_words")
            reply = rule.get("reply")
            if not isinstance(trigger_words, list) or not trigger_words:
                logger.warning(f"固定回复规则 {i} 缺少有效的 trigger_words，已跳过")
                continue
            if not isinstance(reply, str) or not reply:
                logger.warning(f"固定回复规则 {i} 缺少有效的 reply，已跳过")
                continue
            # 过滤空的触发词
            original_count = len(trigger_words)
            trigger_words = [t for t in trigger_words if t and isinstance(t, str)]
            if len(trigger_words) < original_count:
                logger.debug(f"固定回复规则 {i} 过滤了 {original_count - len(trigger_words)} 个无效触发词")
            if not trigger_words:
                logger.warning(f"固定回复规则 {i} 过滤后无有效触发词，已跳过")
                continue
            valid_rules.append({
                "trigger_words": trigger_words,
                "reply": reply
            })
        return valid_rules

    def _init_configs(self):
        """初始化所有配置项"""
        # === 基础配置 ===
        self.enabled = self._get_config("enabled", True)
        self.reply_format = self._get_config("reply_format", "{key}")
        self.whitelist = self._get_config("whitelist", [])
        self.blacklist = self._get_config("blacklist", [])

        # === 限流配置 ===
        self.rate_limit_enabled = self._get_config("rate_limit_enabled", True)
        self.rate_limit_window = self._get_config("rate_limit_window", 60)
        self.rate_limit_count = self._get_config("rate_limit_count", 10)
        self.group_timestamps: defaultdict[str, deque] = defaultdict(deque)
        self._rate_limit_lock = asyncio.Lock()  # 限流数据锁

        # === 通知配置 ===
        # 早安通知
        self.morning_notify_enabled = self._get_config("morning_notify_enabled", True)
        self.morning_notify_time = self._validate_time_config("morning_notify_time", "07:00")
        self.morning_notify_days = self._get_config("morning_notify_days", [1, 2, 3, 4, 5, 6, 0])
        self.morning_notify_text = self._get_config("morning_notify_text", "早上好！新的一天开始了，快来打卡种树吧！")

        # 晚安通知
        self.night_notify_enabled = self._get_config("night_notify_enabled", True)
        self.night_notify_time = self._validate_time_config("night_notify_time", "23:00")
        self.night_notify_days = self._get_config("night_notify_days", [0, 1, 2, 3, 4, 5, 6])
        self.night_notify_text = self._get_config("night_notify_text", "夜深了，该休息啦，晚安！明天继续种树~")

        # 周统计
        self.weekstat_enabled = self._get_config("weekstat_enabled", True)
        self.weekstat_day = self._get_config("weekstat_day", 1)
        self.weekstat_time = self._validate_time_config("weekstat_time", "07:00")
        self.rank_top_n = self._get_config("rank_top_n", 5)

        # 学习目标
        self.topic_notify_enabled = self._get_config("topic_notify_enabled", True)
        self.topic_notify_day = self._get_config("topic_notify_day", 1)
        self.topic_notify_time = self._validate_time_config("topic_notify_time", "08:00")

        # === 关键词唤起配置 ===
        self.keyword_reply_enabled = self._get_config("keyword_reply_enabled", True)
        self.keywords = self._get_config("keywords", ["果果"])
        # 过滤空字符串关键词，避免匹配任意内容
        self.keywords = [kw for kw in self.keywords if kw and kw.strip()]
        if self.keywords:
            pattern = "|".join(re.escape(kw) for kw in self.keywords)
            self.keyword_pattern = re.compile(f"({pattern})")
        else:
            self.keyword_pattern = None

        # 关键词独立限流
        self.keyword_rate_limit_enabled = self._get_config("keyword_rate_limit_enabled", True)
        self.keyword_rate_limit_window = self._get_config("keyword_rate_limit_window", 60)
        self.keyword_rate_limit_count = self._get_config("keyword_rate_limit_count", 5)
        self.keyword_timestamps: defaultdict[str, deque] = defaultdict(deque)
        self._keyword_rate_limit_lock = asyncio.Lock()  # 关键词限流数据锁

        # === 固定回复配置 ===
        self.fixed_reply_enabled = self._get_config("fixed_reply_enabled", True)
        self.fixed_reply_rules = self._validate_fixed_reply_rules(
            self._get_config("fixed_reply_rules", [])
        )

        # === 树种推送配置 ===
        self.tree_notify_enabled = self._get_config("tree_notify_enabled", True)

        # === 早安晚安自动回复配置 ===
        self.greeting_reply_enabled = self._get_config("greeting_reply_enabled", True)
        self.morning_greeting_start = self._validate_time_config("morning_greeting_start", "06:00")
        self.morning_greeting_end = self._validate_time_config("morning_greeting_end", "10:00")
        self.morning_greeting_replies = self._get_config("morning_greeting_replies", [])
        self.night_greeting_start = self._validate_time_config("night_greeting_start", "21:00")
        self.night_greeting_end = self._validate_time_config("night_greeting_end", "02:00")
        self.night_greeting_replies = self._get_config("night_greeting_replies", [])

        # === 晚安车报名配置 ===
        self.night_bus_enabled = self._get_config("night_bus_enabled", True)
        self.night_bus_start = self._validate_time_config("night_bus_start", "18:00")
        self.night_bus_end = self._validate_time_config("night_bus_end", "00:00")

        # === 晚安车发车通知配置 ===
        self.night_bus_notify_enabled = self._get_config("night_bus_notify_enabled", True)
        self.night_bus_notify_time = self._validate_time_config("night_bus_notify_time", "22:00")

    async def initialize(self) -> None:
        """插件激活时启动定时任务"""
        await self._start_schedule()
        
        # 恢复今日树种信息（从数据库）
        await self._restore_today_tree()
        
        logger.info("Forest 插件已初始化，定时任务已启动")

    async def _restore_today_tree(self):
        """从数据库恢复今日树种信息"""
        try:
            result = self.db.get_today_pushed_tree()
            if result:
                tree_id, pushed_at = result
                tree_info = self.tree_manager.get_tree_info(tree_id)
                if tree_info:
                    # 构建消息
                    zh_name = tree_info.get("zh", "未知树种")
                    en_name = tree_info.get("en", "")
                    tier = tree_info.get("tier", "")
                    description = tree_info.get("description", "")

                    message = f"今日树种：{zh_name}\n"
                    if en_name:
                        message += f"英文名：{en_name}\n"
                    if tier:
                        message += f"稀有度：{tier}\n"
                    if description:
                        message += description

                    # 使用锁保护缓存写入
                    async with self._tree_cache_lock:
                        self._today_tree_message = message
                        self._today_tree_id = tree_id
                    logger.info(f"已恢复今日树种: {zh_name} (ID: {tree_id})")
        except Exception as e:
            logger.error(f"恢复今日树种失败: {e}")

    async def terminate(self) -> None:
        """插件禁用时停止定时任务"""
        await self._stop_schedule()
        logger.info("Forest 插件已终止，定时任务已停止")

    async def _start_schedule(self):
        """启动定时任务"""
        self._schedule_task = asyncio.create_task(self._schedule_loop())
        logger.info("Forest 定时任务已启动")

    async def _stop_schedule(self):
        """停止定时任务"""
        if self._schedule_task:
            self._schedule_task.cancel()
            try:
                await self._schedule_task
            except asyncio.CancelledError:
                pass
            logger.info("Forest 定时任务已停止")

    async def _schedule_loop(self):
        """定时任务主循环"""
        while True:
            try:
                now = datetime.now()
                current_minute = now.hour * 60 + now.minute

                # 每分钟只检查一次，避免重复推送
                if current_minute != self._last_check_minute:
                    self._last_check_minute = current_minute
                    current_time = now.strftime("%H:%M")
                    current_weekday = now.weekday()  # 0=周一, 6=周日

                    # 早安通知
                    if (self.morning_notify_enabled and
                        current_time == self.morning_notify_time and
                        current_weekday in self.morning_notify_days):
                        await self._send_morning_notify()

                    # 晚安车发车通知（在晚安通知之前）
                    if (self.night_bus_notify_enabled and
                        current_time == self.night_bus_notify_time and
                        current_weekday in self.night_notify_days):  # 复用晚安通知的日期配置
                        await self._send_night_bus_notify()

                    # 晚安通知
                    if (self.night_notify_enabled and
                        current_time == self.night_notify_time and
                        current_weekday in self.night_notify_days):
                        await self._send_night_notify()

                    # 周统计
                    if (self.weekstat_enabled and
                        current_time == self.weekstat_time and
                        current_weekday == self.weekstat_day):
                        await self._send_weekstat()

                    # 学习目标
                    if (self.topic_notify_enabled and
                        current_time == self.topic_notify_time and
                        current_weekday == self.topic_notify_day):
                        await self._send_topic_notify()

                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Forest 定时任务异常: {e}")
                await asyncio.sleep(60)

    async def _send_to_whitelist_groups(self, message: str):
        """发送消息到所有白名单群"""
        if not self.whitelist:
            logger.debug("白名单为空，跳过推送")
            return

        if not self._platform_id:
            logger.warning("平台 ID 未初始化，跳过推送")
            return

        for group_id in self.whitelist:
            try:
                await self._send_group_message(group_id, message)
            except Exception as e:
                logger.error(f"发送消息到群 {group_id} 失败: {e}")

    async def _send_group_message(self, group_id: str, message: str):
        """发送消息到指定群"""
        if not self._platform_id:
            logger.warning("平台 ID 未初始化，无法发送消息")
            return

        session_str = f"{self._platform_id}:GroupMessage:{group_id}"

        try:
            message_chain = MessageChain([Plain(message)])
            success = await self.context.send_message(session_str, message_chain)
            if success:
                logger.info(f"已发送消息到群 {group_id}")
            else:
                logger.warning(f"发送消息到群 {group_id} 失败：未找到匹配的平台")
        except Exception as e:
            logger.error(f"发送消息到群 {group_id} 异常: {e}")

    async def _send_group_message_with_image(self, group_id: str, message: str, image_path: Path | None):
        """发送消息到指定群（带图片）"""
        if not self._platform_id:
            logger.warning("平台 ID 未初始化，无法发送消息")
            return

        session_str = f"{self._platform_id}:GroupMessage:{group_id}"

        try:
            components = [Plain(message)]
            if image_path and image_path.exists():
                components.append(Image(file=str(image_path)))

            message_chain = MessageChain(components)
            success = await self.context.send_message(session_str, message_chain)
            if success:
                logger.info(f"已发送消息到群 {group_id}")
            else:
                logger.warning(f"发送消息到群 {group_id} 失败：未找到匹配的平台")
        except Exception as e:
            logger.error(f"发送消息到群 {group_id} 异常: {e}")

    async def _send_morning_notify(self):
        """发送早安通知（附带每日树种和图片）"""
        logger.info("发送早安打卡通知")

        base_message = self.morning_notify_text
        tree_image_path = None

        if self.tree_notify_enabled and self.tree_manager.trees_data:
            tree_message, tree_id = await self._get_daily_tree_message()
            if tree_message:
                base_message = f"{base_message}\n\n{tree_message}"
                tree_image_path = self.tree_manager.get_tree_image_path(tree_id)

        # 发送到白名单群
        if not self.whitelist:
            logger.debug("白名单为空，跳过推送")
            return

        if not self._platform_id:
            logger.warning("平台 ID 未初始化，跳过推送")
            return

        for group_id in self.whitelist:
            try:
                await self._send_group_message_with_image(group_id, base_message, tree_image_path)
            except Exception as e:
                logger.error(f"发送消息到群 {group_id} 失败: {e}")

    async def _get_daily_tree_message(self) -> tuple[str, str] | tuple[None, None]:
        """获取今日推送的树种信息，返回 (消息, 树种ID)"""
        # 使用锁保护整个推送过程，防止竞态条件
        async with self._tree_push_lock:
            all_tree_ids = self.tree_manager.get_all_tree_ids()
            if not all_tree_ids:
                return None, None

            pushed_ids = self.db.get_pushed_tree_ids()
            unpushed_ids = [tid for tid in all_tree_ids if tid not in pushed_ids]

            # 如果全部推送完，重置
            if not unpushed_ids:
                logger.info("所有树种已推送完毕，重新循环")
                if not self.db.reset_all_trees():
                    logger.error("重置树种推送状态失败")
                    return None, None
                unpushed_ids = all_tree_ids

            # 随机选择一个未推送的树种
            tree_id = random.choice(unpushed_ids)
            tree_info = self.tree_manager.get_tree_info(tree_id)

            if not tree_info:
                return None, None

            # 标记已推送
            self.db.mark_tree_pushed(tree_id)

            # 构建消息
            zh_name = tree_info.get("zh", "未知树种")
            en_name = tree_info.get("en", "")
            tier = tree_info.get("tier", "")
            description = tree_info.get("description", "")

            message = f"今日树种：{zh_name}\n"
            if en_name:
                message += f"英文名：{en_name}\n"
            if tier:
                message += f"稀有度：{tier}\n"
            if description:
                message += description

            # 保存今日树种信息供查询（使用缓存锁保护）
            async with self._tree_cache_lock:
                self._today_tree_message = message
                self._today_tree_id = tree_id

            logger.info(f"今日推送树种: {zh_name} (ID: {tree_id})")
            return message, tree_id

    async def _send_night_notify(self):
        """发送晚安通知"""
        logger.info("发送晚安通知")

        if not self.whitelist:
            logger.debug("白名单为空，跳过推送")
            return

        if not self._platform_id:
            logger.warning("平台 ID 未初始化，跳过推送")
            return

        for group_id in self.whitelist:
            try:
                await self._send_group_message(group_id, self.night_notify_text)
            except Exception as e:
                logger.error(f"发送消息到群 {group_id} 失败: {e}")

    async def _send_night_bus_notify(self):
        """发送晚安车发车通知（附带报名人员名单）"""
        logger.info("发送晚安车发车通知")

        if not self.whitelist:
            logger.debug("白名单为空，跳过推送")
            return

        if not self._platform_id:
            logger.warning("平台 ID 未初始化，跳过推送")
            return

        for group_id in self.whitelist:
            signups = self.db.get_night_bus_signups(group_id)

            # 只有报名人数 > 2 才发送发车通知
            if len(signups) > 2:
                names = [name or uid for uid, name in signups]
                message = f"🚌 晚安车准备发车，请司机和各位乘客准备！\n\n今日乘客 {len(signups)} 人："
                message += "\n" + "、".join(names)

                try:
                    await self._send_group_message(group_id, message)
                except Exception as e:
                    logger.error(f"发送消息到群 {group_id} 失败: {e}")
            else:
                logger.debug(f"群 {group_id} 晚安车报名人数不足 3 人，不发送发车通知")

    async def _send_weekstat(self):
        """发送周统计排行"""
        logger.info("发送周统计排行")
        if not self.whitelist:
            return

        for group_id in self.whitelist:
            rank = self.db.get_week_rank(group_id, self.rank_top_n)
            if not rank:
                continue

            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            lines = ["📊 本周打卡排行榜"]
            for i, (user_id, user_name, days) in enumerate(rank):
                medal = medals[i] if i < len(medals) else f"{i+1}."
                display_name = user_name if user_name else f"用户{i+1}"
                lines.append(f"{medal} 第{i+1}名：{display_name} - {days}天")

            await self._send_group_message(group_id, "\n".join(lines))

    async def _send_topic_notify(self):
        """发送学习目标"""
        logger.info("发送学习目标")
        topic = self.db.get_unpushed_topic()

        if not topic:
            if self.db.get_topic_count() > 0:
                self.db.reset_all_topics()
                topic = self.db.get_unpushed_topic()
                logger.info("所有主题已推送完毕，重新循环")
            else:
                logger.info("没有学习主题，跳过推送")
                return

        if topic:
            topic_id, content = topic
            message = f"📚 本周学习目标讨论：\n{content}"
            await self._send_to_whitelist_groups(message)
            self.db.mark_topic_pushed(topic_id)
            logger.info(f"已推送学习主题: {content}")

    async def _check_rate_limit(self, group_id: str) -> bool:
        """检查是否触发限流，返回 True 表示允许响应"""
        if not self.rate_limit_enabled:
            return True

        async with self._rate_limit_lock:
            now = time.time()
            timestamps = self.group_timestamps[group_id]

            while timestamps and timestamps[0] < now - self.rate_limit_window:
                timestamps.popleft()

            if len(timestamps) >= self.rate_limit_count:
                logger.debug(f"群 {group_id} 触发限流，当前计数: {len(timestamps)}")
                return False

            timestamps.append(now)
            return True

    async def _check_keyword_rate_limit(self, group_id: str) -> bool:
        """检查关键词是否触发限流，返回 True 表示允许响应"""
        if not self.keyword_rate_limit_enabled:
            return True

        async with self._keyword_rate_limit_lock:
            now = time.time()
            timestamps = self.keyword_timestamps[group_id]

            while timestamps and timestamps[0] < now - self.keyword_rate_limit_window:
                timestamps.popleft()

            if len(timestamps) >= self.keyword_rate_limit_count:
                logger.debug(f"群 {group_id} 关键词触发限流，当前计数: {len(timestamps)}")
                return False

            timestamps.append(now)
            return True

    def _parse_time(self, time_str: str) -> int:
        """
        解析时间字符串为分钟数

        Args:
            time_str: 时间字符串，支持 "HH:MM" 或 "H:MM" 格式

        Returns:
            int: 从午夜开始的分钟数，解析失败返回 -1
        """
        try:
            parts = time_str.strip().split(":")
            if len(parts) != 2:
                return -1
            hours, minutes = int(parts[0]), int(parts[1])
            if not (0 <= hours <= 23 and 0 <= minutes <= 59):
                return -1
            return hours * 60 + minutes
        except (ValueError, AttributeError):
            return -1

    def _validate_time_config(self, key: str, default: str) -> str:
        """
        验证时间配置项格式，如果无效则返回默认值

        Args:
            key: 配置键名
            default: 默认值

        Returns:
            str: 有效的时间字符串
        """
        value = self._get_config(key, default)
        if self._parse_time(value) < 0:
            logger.warning(f"配置项 {key} 的值 '{value}' 格式无效，使用默认值 '{default}'")
            return default
        return value

    def _is_in_time_range(self, current_time: str, start_time: str, end_time: str) -> bool:
        """
        判断当前时间是否在指定时间段内
        支持跨天场景（如 21:00-02:00）

        Args:
            current_time: 当前时间 "HH:MM"
            start_time: 开始时间 "HH:MM"
            end_time: 结束时间 "HH:MM"

        Returns:
            bool: 是否在时间段内，解析失败返回 False
        """
        current = self._parse_time(current_time)
        start = self._parse_time(start_time)
        end = self._parse_time(end_time)

        # 任一时间解析失败，返回 False
        if current < 0 or start < 0 or end < 0:
            logger.warning(f"时间格式错误: current={current_time}, start={start_time}, end={end_time}")
            return False

        if start <= end:
            # 不跨天：如 06:00-10:00
            return start <= current <= end
        else:
            # 跨天：如 21:00-02:00
            return current >= start or current <= end

    def _match_fixed_reply(self, message_text: str) -> str | None:
        """匹配固定回复规则，返回回复内容或 None"""
        if not self.fixed_reply_enabled or not self.fixed_reply_rules:
            return None

        for rule in self.fixed_reply_rules:
            for trigger in rule.get("trigger_words", []):
                if trigger in message_text:
                    return rule.get("reply", "")
        return None

    def _build_checkin_tools(self, user_id: str, group_id: str) -> ToolSet:
        """构建打卡查询工具集"""

        async def check_today_checkin(context, **kwargs) -> str:
            """查询今日是否打卡"""
            checked = self.db.has_checked_today(user_id, group_id)
            return "今日已打卡" if checked else "今日未打卡"

        async def get_week_checkin_count(context, **kwargs) -> str:
            """查询本周打卡天数"""
            days = self.db.get_user_week_days(user_id, group_id)
            return f"本周已打卡 {days} 天"

        async def get_missed_checkin_days(context, **kwargs) -> str:
            """查询本周未打卡日期"""
            missed = self.db.get_user_missed_days(user_id, group_id)
            if not missed:
                return "本周全勤，没有缺打卡"
            return f"本周未打卡日期：{', '.join(missed)}"

        tools = ToolSet([
            FunctionTool(
                name="check_today_checkin",
                parameters={"type": "object", "properties": {}},
                description="查询用户今日是否已打卡",
                handler=check_today_checkin,
            ),
            FunctionTool(
                name="get_week_checkin_count",
                parameters={"type": "object", "properties": {}},
                description="查询用户本周打卡天数",
                handler=get_week_checkin_count,
            ),
            FunctionTool(
                name="get_missed_checkin_days",
                parameters={"type": "object", "properties": {}},
                description="查询用户本周哪些天没有打卡",
                handler=get_missed_checkin_days,
            ),
        ])
        return tools

    def _build_tree_tools(self) -> ToolSet:
        """构建树种查询工具集"""

        async def search_tree_by_name(context, name: str, **kwargs) -> str:
            """根据名称搜索树种"""
            results = self.tree_manager.search_trees(name)
            if not results:
                return f"未找到包含「{name}」的树种"

            # 记录查询到的树种 ID（用于后续发送图片，使用锁保护）
            async with self._ai_tree_lock:
                for tree_id, _ in results[:3]:  # 最多记录3个
                    if tree_id not in self._ai_queried_tree_ids:
                        self._ai_queried_tree_ids.append(tree_id)

            lines = [f"找到 {len(results)} 个匹配的树种："]
            for tree_id, info in results[:5]:
                zh = info.get("zh", "")
                en = info.get("en", "")
                tier = info.get("tier", "")
                desc = info.get("description", "")[:50]
                lines.append(f"• {zh} ({en}) [{tier}]\n  {desc}...")

            return "\n".join(lines)

        async def get_tree_detail(context, tree_id: str, **kwargs) -> str:
            """获取树种详细信息"""
            tree_info = self.tree_manager.get_tree_info(tree_id)
            if not tree_info:
                return f"未找到 ID 为 {tree_id} 的树种"

            # 记录查询到的树种 ID（使用锁保护）
            async with self._ai_tree_lock:
                if tree_id not in self._ai_queried_tree_ids:
                    self._ai_queried_tree_ids.append(tree_id)

            zh = tree_info.get("zh", "未知")
            en = tree_info.get("en", "未知")
            tier = tree_info.get("tier", "未知")
            desc = tree_info.get("description", "暂无描述")

            return f"""🌲 树种详情
名称：{zh}
英文名：{en}
稀有度：{tier}
描述：{desc}"""

        async def list_trees_by_tier(context, tier: str, **kwargs) -> str:
            """按稀有度列出树种"""
            valid_tiers = ["基础", "稀有", "史诗", "传说", "特殊"]
            if tier not in valid_tiers:
                return f"稀有度可选值：{', '.join(valid_tiers)}"

            results = [(tid, info) for tid, info in self.tree_manager.trees_list
                       if info.get("tier") == tier]

            if not results:
                return f"没有稀有度为「{tier}」的树种"

            lines = [f"稀有度「{tier}」的树种（共 {len(results)} 个）："]
            for tree_id, info in results[:10]:
                zh = info.get("zh", "")
                lines.append(f"• {zh} (ID: {tree_id})")

            if len(results) > 10:
                lines.append(f"... 还有 {len(results) - 10} 个")

            return "\n".join(lines)

        async def get_random_tree(context, **kwargs) -> str:
            """随机推荐一个树种"""
            if not self.tree_manager.trees_list:
                return "树种数据未加载"

            tree_id, info = random.choice(self.tree_manager.trees_list)

            # 记录查询到的树种 ID（使用锁保护）
            async with self._ai_tree_lock:
                if tree_id not in self._ai_queried_tree_ids:
                    self._ai_queried_tree_ids.append(tree_id)

            zh = info.get("zh", "")
            en = info.get("en", "")
            tier = info.get("tier", "")
            desc = info.get("description", "")

            return f"随机推荐：{zh} ({en}) [{tier}]\n{desc}"

        async def get_tree_stats(context, **kwargs) -> str:
            """获取树种统计信息"""
            total = len(self.tree_manager.trees_list)
            pushed = self.db.get_pushed_tree_count()
            remaining = total - pushed

            tier_counts = {}
            for _, info in self.tree_manager.trees_list:
                tier = info.get("tier", "未知")
                tier_counts[tier] = tier_counts.get(tier, 0) + 1

            lines = [f"树种统计", f"总数：{total}", f"已推送：{pushed}", f"剩余：{remaining}", ""]
            for tier, count in sorted(tier_counts.items()):
                lines.append(f"• {tier}：{count} 个")

            return "\n".join(lines)

        tools = ToolSet([
            FunctionTool(
                name="search_tree_by_name",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "树种名称（中文或英文）"}
                    },
                    "required": ["name"]
                },
                description="根据名称搜索树种信息",
                handler=search_tree_by_name,
            ),
            FunctionTool(
                name="get_tree_detail",
                parameters={
                    "type": "object",
                    "properties": {
                        "tree_id": {"type": "string", "description": "树种ID"}
                    },
                    "required": ["tree_id"]
                },
                description="根据ID获取树种详细信息",
                handler=get_tree_detail,
            ),
            FunctionTool(
                name="list_trees_by_tier",
                parameters={
                    "type": "object",
                    "properties": {
                        "tier": {"type": "string", "description": "稀有度：基础/稀有/史诗/传说/特殊"}
                    },
                    "required": ["tier"]
                },
                description="按稀有度列出树种",
                handler=list_trees_by_tier,
            ),
            FunctionTool(
                name="get_random_tree",
                parameters={"type": "object", "properties": {}},
                description="随机推荐一个树种",
                handler=get_random_tree,
            ),
            FunctionTool(
                name="get_tree_stats",
                parameters={"type": "object", "properties": {}},
                description="获取树种统计信息",
                handler=get_tree_stats,
            ),
        ])
        return tools

    def _build_night_bus_tools(self, user_id: str, group_id: str) -> ToolSet:
        """构建晚安车工具集"""

        async def signup_night_bus(context, **kwargs) -> str:
            """报名晚安车"""
            # 检查时间段
            current_time = datetime.now().strftime("%H:%M")
            if not self._is_in_time_range(current_time, self.night_bus_start, self.night_bus_end):
                return f"⚠️ 晚安车报名时间为 {self.night_bus_start}-{self.night_bus_end}，当前不在报名时间内"
            
            user_name = f"用户{user_id[-4:]}" if len(user_id) >= 4 else f"用户{user_id}"
            success = self.db.signup_night_bus(user_id, group_id, user_name)
            if success:
                count = self.db.get_night_bus_count(group_id)
                return f"✅ 报名成功！当前已报名 {count} 人"
            else:
                return "⚠️ 今日已报名，无需重复报名"

        async def cancel_night_bus(context, **kwargs) -> str:
            """取消晚安车报名"""
            success = self.db.cancel_night_bus(user_id, group_id)
            if success:
                count = self.db.get_night_bus_count(group_id)
                return f"❌ 已取消报名，当前剩余 {count} 人"
            else:
                return "⚠️ 今日尚未报名"

        async def query_night_bus_signups(context, **kwargs) -> str:
            """查询今日晚安车报名名单"""
            signups = self.db.get_night_bus_signups(group_id)
            if signups:
                names = [name or uid for uid, name in signups]
                lines = [f"🚌 今日晚安车已报名 {len(signups)} 人："]
                lines.extend([f"  {i+1}. {name}" for i, name in enumerate(names)])
                return "\n".join(lines)
            else:
                return "🚌 今日暂无人报名晚安车"

        async def get_user_night_bus_count(context, **kwargs) -> str:
            """查询个人累计参加晚安车次数"""
            count = self.db.get_user_night_bus_count(user_id, group_id)
            return f"🚌 累计参加晚安车 {count} 次"

        tools = ToolSet([
            FunctionTool(
                name="signup_night_bus",
                parameters={"type": "object", "properties": {}},
                description="报名参加晚安车",
                handler=signup_night_bus,
            ),
            FunctionTool(
                name="cancel_night_bus",
                parameters={"type": "object", "properties": {}},
                description="取消晚安车报名",
                handler=cancel_night_bus,
            ),
            FunctionTool(
                name="query_night_bus_signups",
                parameters={"type": "object", "properties": {}},
                description="查询今日晚安车报名名单和人数",
                handler=query_night_bus_signups,
            ),
            FunctionTool(
                name="get_user_night_bus_count",
                parameters={"type": "object", "properties": {}},
                description="查询用户累计参加晚安车的次数",
                handler=get_user_night_bus_count,
            ),
        ])
        return tools

    # === 消息处理 ===

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，检测 Forest 房间邀请"""
        if not self._platform_id:
            self._platform_id = event.get_platform_id()

        if not self.enabled:
            return

        message_text = event.message_str
        if not message_text:
            return

        match = self.key_pattern.search(message_text)
        if not match:
            return

        room_key = match.group(1)
        group_id = event.get_group_id()

        if group_id:
            if self.whitelist and group_id not in self.whitelist:
                logger.debug(f"群 {group_id} 不在白名单中，跳过")
                return
            if group_id in self.blacklist:
                logger.debug(f"群 {group_id} 在黑名单中，跳过")
                return
            if not await self._check_rate_limit(group_id):
                return

        logger.info(f"检测到 Forest 房间邀请，密钥: {room_key}, 群: {group_id or '私聊'}")
        reply_text = self.reply_format.format(key=room_key)
        await event.send(event.plain_result(reply_text))

    # === 基础命令 ===

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest开启")
    async def enable_forest(self, event: AstrMessageEvent):
        """开启 Forest 密钥提取功能"""
        self.enabled = True
        yield event.plain_result("✅ Forest 房间密钥提取功能已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest关闭")
    async def disable_forest(self, event: AstrMessageEvent):
        """关闭 Forest 密钥提取功能"""
        self.enabled = False
        yield event.plain_result("❌ Forest 房间密钥提取功能已关闭")

    @filter.command("forest状态")
    async def forest_status(self, event: AstrMessageEvent):
        """查看 Forest 功能状态"""
        status = "✅ 开启" if self.enabled else "❌ 关闭"
        whitelist_str = ", ".join(self.whitelist) if self.whitelist else "无"
        blacklist_str = ", ".join(self.blacklist) if self.blacklist else "无"
        rate_limit_status = "✅ 启用" if self.rate_limit_enabled else "❌ 禁用"
        morning_status = "✅ 启用" if self.morning_notify_enabled else "❌ 禁用"
        night_status = "✅ 启用" if self.night_notify_enabled else "❌ 禁用"

        result = f"""🌲 Forest 插件状态
状态: {status}
白名单群: {whitelist_str}
黑名单群: {blacklist_str}
回复格式: {self.reply_format}
限流: {rate_limit_status} ({self.rate_limit_count}次/{self.rate_limit_window}秒)
早安通知: {morning_status} ({self.morning_notify_time})
晚安通知: {night_status} ({self.night_notify_time})"""

        yield event.plain_result(result)

    # === 白名单管理 ===

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest添加白名单")
    async def add_whitelist(self, event: AstrMessageEvent, group_id: str):
        """添加群号到白名单"""
        if group_id in self.whitelist:
            yield event.plain_result(f"⚠️ 群 {group_id} 已在白名单中")
            return

        self.whitelist.append(group_id)
        self.config["whitelist"] = self.whitelist
        self.config.save_config()

        logger.info(f"已添加群 {group_id} 到白名单")
        yield event.plain_result(f"✅ 已添加群 {group_id} 到白名单")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest移除白名单")
    async def remove_whitelist(self, event: AstrMessageEvent, group_id: str):
        """从白名单移除群号"""
        if group_id not in self.whitelist:
            yield event.plain_result(f"⚠️ 群 {group_id} 不在白名单中")
            return

        self.whitelist.remove(group_id)
        self.config["whitelist"] = self.whitelist
        self.config.save_config()

        logger.info(f"已从白名单移除群 {group_id}")
        yield event.plain_result(f"✅ 已从白名单移除群 {group_id}")

    # === 固定回复 ===

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_fixed_reply_message(self, event: AstrMessageEvent):
        """监听群消息，检测固定回复触发词"""
        if not self._platform_id:
            self._platform_id = event.get_platform_id()

        if not self.enabled or not self.fixed_reply_enabled:
            return

        message_text = event.message_str
        fixed_reply = self._match_fixed_reply(message_text)
        if not fixed_reply:
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        # 白名单检查
        if self.whitelist and group_id not in self.whitelist:
            return

        # 黑名单检查
        if group_id in self.blacklist:
            return

        # 限流检查
        if not await self._check_keyword_rate_limit(group_id):
            return

        logger.info(f"触发固定回复: {message_text}")
        yield event.plain_result(fixed_reply)

    # === 早安晚安自动回复 ===

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_greeting_message(self, event: AstrMessageEvent):
        """监听群消息，检测早安晚安关键词"""
        if not self._platform_id:
            self._platform_id = event.get_platform_id()

        if not self.enabled or not self.greeting_reply_enabled:
            return

        message_text = event.message_str.strip()
        group_id = event.get_group_id()
        if not group_id:
            return

        # 白名单检查
        if self.whitelist and group_id not in self.whitelist:
            return

        # 黑名单检查
        if group_id in self.blacklist:
            return

        # 限流检查（复用关键词限流）
        if not await self._check_keyword_rate_limit(group_id):
            return

        current_time = datetime.now().strftime("%H:%M")
        reply = None

        # 早安关键词（按长度降序，优先匹配更长的关键词）
        morning_keywords = [
            "早安呀", "早上好", "早早早", "早呀", "早哟", "早啊",
            "早安"
        ]
        # 晚安关键词（不含"晚安车"，该词由其他处理器处理）
        night_keywords = [
            "晚安呀", "晚安哟", "晚上好", "早点睡",
            "晚安", "晚啦", "睡啦", "好梦"
        ]

        # 检测早安关键词（互斥检测）
        for kw in morning_keywords:
            if kw in message_text:
                if self._is_in_time_range(current_time, self.morning_greeting_start, self.morning_greeting_end):
                    if self.morning_greeting_replies:
                        reply = random.choice(self.morning_greeting_replies)
                        logger.info(f"检测到早安关键词: {kw}")
                break

        # 检测晚安关键词（仅在早安未匹配且不含"晚安车"时检测）
        if reply is None and "晚安车" not in message_text:
            for kw in night_keywords:
                if kw in message_text:
                    if self._is_in_time_range(current_time, self.night_greeting_start, self.night_greeting_end):
                        if self.night_greeting_replies:
                            reply = random.choice(self.night_greeting_replies)
                            logger.info(f"检测到晚安关键词: {kw}")
                    break

        if reply:
            yield event.plain_result(reply)

    # === 关键词唤起 AI 回复 ===

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_keyword_message(self, event: AstrMessageEvent):
        """监听群消息，检测关键词或@机器人触发回复"""
        if not self._platform_id:
            self._platform_id = event.get_platform_id()

        if not self.enabled or not self.keyword_reply_enabled:
            return

        message_text = event.message_str

        # 检查是否触发关键词
        is_keyword_trigger = self.keyword_pattern and self.keyword_pattern.search(message_text)

        # 如果没有触发关键词，检查是否是命令（跳过命令）
        if not is_keyword_trigger:
            if hasattr(event, 'is_wake_up') and event.is_wake_up:
                return

        # 检查是否触发 @机器人
        is_atme_trigger = event.is_at_or_wake_command

        if not is_keyword_trigger and not is_atme_trigger:
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        # 白名单检查
        if self.whitelist and group_id not in self.whitelist:
            return

        # 黑名单检查
        if group_id in self.blacklist:
            return

        # 关键词独立限流检查
        if not await self._check_keyword_rate_limit(group_id):
            return

        # 获取用户信息
        user_id = event.get_sender_id()

        # 清空 AI 树种查询缓存（使用锁保护）
        async with self._ai_tree_lock:
            self._ai_queried_tree_ids = []

        # 构建打卡查询工具集
        checkin_tools = self._build_checkin_tools(user_id, group_id)

        # 构建树种查询工具集
        tree_tools = self._build_tree_tools()

        # 构建晚安车工具集
        night_bus_tools = self._build_night_bus_tools(user_id, group_id)

        # 合并工具集
        all_tools = ToolSet(list(checkin_tools.tools) + list(tree_tools.tools) + list(night_bus_tools.tools))

        # 获取默认人设的系统提示词
        persona = await self.context.persona_manager.get_default_persona_v3(umo=event.unified_msg_origin)
        system_prompt = persona.get("prompt", "") if persona else ""
        system_prompt += """\n\n你可以使用工具查询用户的打卡记录、Forest 树种信息和晚安车报名情况。

当用户询问打卡、树种或晚安车相关问题时，请调用相应的工具。

重要：工具返回的结果已经是标准化、格式化的消息，请直接返回工具的结果，不要重新生成或修改。

晚安车相关操作必须使用专门的晚安车工具（signup_night_bus、cancel_night_bus、query_night_bus_signups、get_user_night_bus_count），不要使用文件搜索工具处理晚安车相关的问题。

晚安车相关操作包括：
- 报名：用户说"报名晚安车"、"我要报名"、"报名"等
- 取消：用户说"取消报名"、"取消晚安车报名"、"取消"等
- 查询：用户说"晚安车有谁"、"晚安车名单"、"有多少人报名"、"有谁"等
- 统计：用户说"我晚安车几次"、"我参加了几次"、"晚安车统计"等"""

        # 获取当前 chat provider
        provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)

        # 调用带工具的 AI
        trigger_type = "@机器人" if is_atme_trigger else "关键词"
        logger.info(f"检测到{trigger_type}触发: {message_text}")
        try:
            response = await self.context.tool_loop_agent(
                event=event,
                chat_provider_id=provider_id,
                prompt=message_text,
                tools=all_tools,
                system_prompt=system_prompt,
            )

            # 构建消息组件
            components = [Plain(response.completion_text)]

            # 如果 AI 查询了树种，附加图片（使用锁保护读取）
            async with self._ai_tree_lock:
                queried_ids = list(self._ai_queried_tree_ids[:3])  # 最多发送3张图片

            if queried_ids:
                for tree_id in queried_ids:
                    image_path = self.tree_manager.get_tree_image_path(tree_id)
                    if image_path and image_path.exists():
                        components.append(Image(file=str(image_path)))

            yield event.chain_result(components)

        except Exception as e:
            logger.error(f"AI 回复失败: {e}")
            yield event.plain_result("抱歉，处理您的请求时出现了问题。")

    # === 打卡功能 ===

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_checkin_message(self, event: AstrMessageEvent):
        """监听群消息，检测打卡（直接发送"打卡"即可）"""
        if not self._platform_id:
            self._platform_id = event.get_platform_id()

        if not self.enabled:
            return

        message_text = event.message_str.strip()

        # 处理"随机树种"
        if message_text == "随机树种":
            if not self.tree_manager.trees_list:
                await event.send(event.plain_result("树种数据未加载"))
                return

            # 随机选择一个树种
            tree_id, tree_info = random.choice(self.tree_manager.trees_list)

            # 构建消息
            zh_name = tree_info.get("zh", "未知树种")
            en_name = tree_info.get("en", "")
            tier = tree_info.get("tier", "")
            description = tree_info.get("description", "")

            message = f"随机树种：{zh_name}\n"
            if en_name:
                message += f"英文名：{en_name}\n"
            if tier:
                message += f"稀有度：{tier}\n"
            if description:
                message += description

            # 获取图片路径
            image_path = self.tree_manager.get_tree_image_path(tree_id)

            # 构建消息链
            components = [Plain(message)]
            if image_path and image_path.exists():
                components.append(Image(file=str(image_path)))

            await event.send(event.chain_result(components))
            return

        # 处理"我的打卡"
        if message_text == "我的打卡":
            group_id = event.get_group_id()
            if not group_id:
                return

            if self.whitelist and group_id not in self.whitelist:
                return

            user_id = event.get_sender_id()
            days = self.db.get_user_week_days(user_id, group_id)
            await event.send(event.plain_result(f"📅 本周已打卡 {days} 天"))
            return

        # 处理"打卡"
        if message_text != "打卡":
            return

        group_id = event.get_group_id()
        if not group_id:
            return

        if self.whitelist and group_id not in self.whitelist:
            return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name() or f"用户{user_id[-4:]}"

        if self.db.has_checked_today(user_id, group_id):
            days = self.db.get_user_week_days(user_id, group_id)
            await event.send(event.plain_result(f"⚠️ 今日已打卡，本周已打卡 {days} 天"))
            return

        if not self.db.checkin(user_id, group_id, user_name):
            await event.send(event.plain_result("⚠️ 打卡失败，请稍后重试"))
            return

        days = self.db.get_user_week_days(user_id, group_id)
        logger.info(f"用户 {user_id} 在群 {group_id} 打卡成功，本周第 {days} 天")

        await event.send(event.plain_result(f"✅ 打卡成功！本周已打卡 {days} 天"))

    # === 通知开关命令 ===

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest早安开启")
    async def enable_morning(self, event: AstrMessageEvent):
        """开启早安通知"""
        self.morning_notify_enabled = True
        self.config["morning_notify_enabled"] = True
        self.config.save_config()
        yield event.plain_result(f"✅ 早安通知已开启 ({self.morning_notify_time})")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest早安关闭")
    async def disable_morning(self, event: AstrMessageEvent):
        """关闭早安通知"""
        self.morning_notify_enabled = False
        self.config["morning_notify_enabled"] = False
        self.config.save_config()
        yield event.plain_result("❌ 早安通知已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest晚安开启")
    async def enable_night(self, event: AstrMessageEvent):
        """开启晚安通知"""
        self.night_notify_enabled = True
        self.config["night_notify_enabled"] = True
        self.config.save_config()
        yield event.plain_result(f"✅ 晚安通知已开启 ({self.night_notify_time})")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest晚安关闭")
    async def disable_night(self, event: AstrMessageEvent):
        """关闭晚安通知"""
        self.night_notify_enabled = False
        self.config["night_notify_enabled"] = False
        self.config.save_config()
        yield event.plain_result("❌ 晚安通知已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest问候开启")
    async def enable_greeting(self, event: AstrMessageEvent):
        """开启早安晚安自动回复"""
        self.greeting_reply_enabled = True
        self.config["greeting_reply_enabled"] = True
        self.config.save_config()
        yield event.plain_result("✅ 早安晚安自动回复已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest问候关闭")
    async def disable_greeting(self, event: AstrMessageEvent):
        """关闭早安晚安自动回复"""
        self.greeting_reply_enabled = False
        self.config["greeting_reply_enabled"] = False
        self.config.save_config()
        yield event.plain_result("❌ 早安晚安自动回复已关闭")

    # === 晚安车报名管理命令 ===

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest晚安车开启")
    async def enable_night_bus(self, event: AstrMessageEvent):
        """开启晚安车报名"""
        self.night_bus_enabled = True
        self.config["night_bus_enabled"] = True
        self.config.save_config()
        yield event.plain_result("✅ 晚安车报名已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest晚安车关闭")
    async def disable_night_bus(self, event: AstrMessageEvent):
        """关闭晚安车报名"""
        self.night_bus_enabled = False
        self.config["night_bus_enabled"] = False
        self.config.save_config()
        yield event.plain_result("❌ 晚安车报名已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest晚安车统计")
    async def night_bus_stats(self, event: AstrMessageEvent):
        """查看晚安车统计（管理员）"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 只能在群聊中使用此命令")
            return

        stats = self.db.get_group_night_bus_stats(group_id, days=7)

        msg = f"🚌 本周晚安车统计：\n"
        msg += f"  发车次数：{stats['bus_days']} 次\n"
        msg += f"  报名人次：{stats['total_signups']} 人次\n"

        if stats['top_passengers']:
            msg += f"  活跃乘客："
            names = [f"{name}({cnt}次)" for name, cnt in stats['top_passengers']]
            msg += "、".join(names)

        yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest晚安车通知开启")
    async def enable_night_bus_notify(self, event: AstrMessageEvent):
        """开启晚安车发车通知"""
        self.night_bus_notify_enabled = True
        self.config["night_bus_notify_enabled"] = True
        self.config.save_config()
        yield event.plain_result(f"✅ 晚安车发车通知已开启 ({self.night_bus_notify_time})")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest晚安车通知关闭")
    async def disable_night_bus_notify(self, event: AstrMessageEvent):
        """关闭晚安车发车通知"""
        self.night_bus_notify_enabled = False
        self.config["night_bus_notify_enabled"] = False
        self.config.save_config()
        yield event.plain_result("❌ 晚安车发车通知已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest统计开启")
    async def enable_weekstat(self, event: AstrMessageEvent):
        """开启周统计"""
        self.weekstat_enabled = True
        self.config["weekstat_enabled"] = True
        self.config.save_config()
        yield event.plain_result("✅ 周统计推送已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest统计关闭")
    async def disable_weekstat(self, event: AstrMessageEvent):
        """关闭周统计"""
        self.weekstat_enabled = False
        self.config["weekstat_enabled"] = False
        self.config.save_config()
        yield event.plain_result("❌ 周统计推送已关闭")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest主题开启")
    async def enable_topic(self, event: AstrMessageEvent):
        """开启学习目标推送"""
        self.topic_notify_enabled = True
        self.config["topic_notify_enabled"] = True
        self.config.save_config()
        yield event.plain_result("✅ 学习目标推送已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest主题关闭")
    async def disable_topic(self, event: AstrMessageEvent):
        """关闭学习目标推送"""
        self.topic_notify_enabled = False
        self.config["topic_notify_enabled"] = False
        self.config.save_config()
        yield event.plain_result("❌ 学习目标推送已关闭")

    # === 学习主题管理 ===

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest添加主题")
    async def add_topic(self, event: AstrMessageEvent, content: str):
        """添加学习主题"""
        topic_id = self.db.add_topic(content)
        logger.info(f"已添加学习主题: {content}")
        yield event.plain_result(f"✅ 已添加学习主题 (ID: {topic_id})")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest主题列表")
    async def list_topics(self, event: AstrMessageEvent):
        """查看所有学习主题"""
        topics = self.db.list_topics()
        if not topics:
            yield event.plain_result("📭 暂无学习主题")
            return

        lines = ["📚 学习主题列表:"]
        for topic_id, content, pushed_at in topics:
            status = "✅ 已推送" if pushed_at else "⏳ 待推送"
            lines.append(f"{topic_id}. {content} [{status}]")

        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest删除主题")
    async def delete_topic(self, event: AstrMessageEvent, topic_id: int):
        """删除学习主题"""
        if self.db.delete_topic(topic_id):
            logger.info(f"已删除学习主题 ID: {topic_id}")
            yield event.plain_result(f"✅ 已删除学习主题 {topic_id}")
        else:
            yield event.plain_result(f"⚠️ 主题 {topic_id} 不存在")

    # === 树种功能 ===

    @filter.command("今日树种")
    async def today_tree(self, event: AstrMessageEvent):
        """获取今日推送的树种"""
        # 使用锁保护缓存读取
        async with self._tree_cache_lock:
            today_message = self._today_tree_message
            today_tree_id = self._today_tree_id

        if not today_message:
            yield event.plain_result("今日还没有推送树种，请等待早安通知")
            return

        # 获取图片路径
        image_path = None
        if today_tree_id:
            image_path = self.tree_manager.get_tree_image_path(today_tree_id)

        # 构建消息链
        components = [Plain(today_message)]
        if image_path and image_path.exists():
            components.append(Image(file=str(image_path)))

        yield event.chain_result(components)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest树种状态")
    async def tree_status(self, event: AstrMessageEvent):
        """查看树种推送状态"""
        total = len(self.tree_manager.trees_list)
        pushed = self.db.get_pushed_tree_count()
        remaining = total - pushed

        yield event.plain_result(f"""🌲 树种推送状态
总数：{total}
已推送：{pushed}
剩余：{remaining}
进度：{pushed}/{total}""")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest重置树种")
    async def reset_trees(self, event: AstrMessageEvent):
        """重置树种推送状态"""
        if self.db.reset_all_trees():
            logger.info("已重置所有树种推送状态")
            yield event.plain_result("✅ 已重置所有树种推送状态")
        else:
            yield event.plain_result("❌ 重置失败")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("forest搜索树种")
    async def search_tree(self, event: AstrMessageEvent, keyword: str = ""):
        """搜索树种"""
        if not keyword:
            yield event.plain_result("请输入搜索关键词，如：forest搜索树种 樱花")
            return

        results = self.tree_manager.search_trees(keyword)
        if not results:
            yield event.plain_result(f"未找到包含「{keyword}」的树种")
            return

        lines = [f"找到 {len(results)} 个匹配的树种："]
        for tree_id, info in results[:10]:
            zh = info.get("zh", "")
            tier = info.get("tier", "")
            lines.append(f"• {zh} [{tier}] (ID: {tree_id})")

        if len(results) > 10:
            lines.append(f"... 还有 {len(results) - 10} 个")

        yield event.plain_result("\n".join(lines))
