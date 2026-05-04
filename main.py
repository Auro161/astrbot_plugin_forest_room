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

        en_name = tree_info.get("en", "").replace("/", "_")
        zh_name = tree_info.get("zh", "").replace("/", "_")
        image_name = f"{tree_id}_{en_name}_{zh_name}.webp"
        image_path = self.plugin_dir / "tree" / "mature_trees" / image_name

        if image_path.exists():
            return image_path
        return None


class ForestRoomPlugin(Star):
    """Forest 房间密钥提取插件"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # === 基础配置 ===
        self.enabled = self.config.get("enabled", True)
        self.reply_format = self.config.get("reply_format", "{key}")
        self.whitelist = self.config.get("whitelist", [])
        self.blacklist = self.config.get("blacklist", [])

        # === 限流配置 ===
        self.rate_limit_enabled = self.config.get("rate_limit_enabled", True)
        self.rate_limit_window = self.config.get("rate_limit_window", 60)
        self.rate_limit_count = self.config.get("rate_limit_count", 10)
        self.group_timestamps: defaultdict[str, deque] = defaultdict(deque)

        # === 通知配置 ===
        # 早安通知
        self.morning_notify_enabled = self.config.get("morning_notify_enabled", True)
        self.morning_notify_time = self.config.get("morning_notify_time", "07:00")
        self.morning_notify_days = self.config.get("morning_notify_days", [1, 2, 3, 4, 5, 6, 0])
        self.morning_notify_text = self.config.get("morning_notify_text", "🌞 早上好！新的一天开始了，快来打卡种树吧！")

        # 晚安通知
        self.night_notify_enabled = self.config.get("night_notify_enabled", True)
        self.night_notify_time = self.config.get("night_notify_time", "22:00")
        self.night_notify_days = self.config.get("night_notify_days", [0, 1, 2, 3, 4, 5, 6])
        self.night_notify_text = self.config.get("night_notify_text", "🌙 夜深了，该休息啦，晚安！明天继续种树~")

        # 周统计
        self.weekstat_enabled = self.config.get("weekstat_enabled", True)
        self.weekstat_day = self.config.get("weekstat_day", 1)
        self.weekstat_time = self.config.get("weekstat_time", "07:00")
        self.rank_top_n = self.config.get("rank_top_n", 5)

        # 学习目标
        self.topic_notify_enabled = self.config.get("topic_notify_enabled", True)
        self.topic_notify_day = self.config.get("topic_notify_day", 1)
        self.topic_notify_time = self.config.get("topic_notify_time", "08:00")

        # === 关键词唤起配置 ===
        self.keyword_reply_enabled = self.config.get("keyword_reply_enabled", True)
        self.keywords = self.config.get("keywords", ["果果"])
        if self.keywords:
            pattern = "|".join(re.escape(kw) for kw in self.keywords)
            self.keyword_pattern = re.compile(f"({pattern})")
        else:
            self.keyword_pattern = None

        # 关键词独立限流
        self.keyword_rate_limit_enabled = self.config.get("keyword_rate_limit_enabled", True)
        self.keyword_rate_limit_window = self.config.get("keyword_rate_limit_window", 60)
        self.keyword_rate_limit_count = self.config.get("keyword_rate_limit_count", 5)
        self.keyword_timestamps: defaultdict[str, deque] = defaultdict(deque)

        # === 固定回复配置 ===
        self.fixed_reply_enabled = self.config.get("fixed_reply_enabled", True)
        self.fixed_reply_rules = self.config.get("fixed_reply_rules", [])

        # === 树种推送配置 ===
        self.tree_notify_enabled = self.config.get("tree_notify_enabled", True)

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

        # === AI 树种查询缓存 ===
        self._ai_queried_tree_ids: list[str] = []

        # Forest 房间密钥正则表达式
        self.key_pattern = re.compile(r"输入我的房间密钥：([A-Z0-9]+)，和我一起")

        logger.info(f"Forest 房间密钥提取插件已加载，启用状态: {self.enabled}")

    async def initialize(self) -> None:
        """插件激活时启动定时任务"""
        await self._start_schedule()
        logger.info("Forest 插件已初始化，定时任务已启动")

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
        all_tree_ids = self.tree_manager.get_all_tree_ids()
        if not all_tree_ids:
            return None, None

        pushed_ids = self.db.get_pushed_tree_ids()
        unpushed_ids = [tid for tid in all_tree_ids if tid not in pushed_ids]

        # 如果全部推送完，重置
        if not unpushed_ids:
            logger.info("所有树种已推送完毕，重新循环")
            self.db.reset_all_trees()
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

        # 保存今日树种信息供查询
        self._today_tree_message = message
        self._today_tree_id = tree_id

        logger.info(f"今日推送树种: {zh_name} (ID: {tree_id})")
        return message, tree_id

    async def _send_night_notify(self):
        """发送晚安通知"""
        logger.info("发送晚安通知")
        await self._send_to_whitelist_groups(self.night_notify_text)

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

    def _check_rate_limit(self, group_id: str) -> bool:
        """检查是否触发限流，返回 True 表示允许响应"""
        if not self.rate_limit_enabled:
            return True

        now = time.time()
        timestamps = self.group_timestamps[group_id]

        while timestamps and timestamps[0] < now - self.rate_limit_window:
            timestamps.popleft()

        if len(timestamps) >= self.rate_limit_count:
            logger.debug(f"群 {group_id} 触发限流，当前计数: {len(timestamps)}")
            return False

        timestamps.append(now)
        return True

    def _check_keyword_rate_limit(self, group_id: str) -> bool:
        """检查关键词是否触发限流，返回 True 表示允许响应"""
        if not self.keyword_rate_limit_enabled:
            return True

        now = time.time()
        timestamps = self.keyword_timestamps[group_id]

        while timestamps and timestamps[0] < now - self.keyword_rate_limit_window:
            timestamps.popleft()

        if len(timestamps) >= self.keyword_rate_limit_count:
            logger.debug(f"群 {group_id} 关键词触发限流，当前计数: {len(timestamps)}")
            return False

        timestamps.append(now)
        return True

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

            # 记录查询到的树种 ID（用于后续发送图片）
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

            # 记录查询到的树种 ID
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

            # 记录查询到的树种 ID
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
            if not self._check_rate_limit(group_id):
                return

        logger.info(f"检测到 Forest 房间邀请，密钥: {room_key}, 群: {group_id or '私聊'}")
        reply_text = self.reply_format.format(key=room_key)
        await event.send(event.plain_result(reply_text))

    # === 基础命令 ===

    @filter.command("forest开启")
    async def enable_forest(self, event: AstrMessageEvent):
        """开启 Forest 密钥提取功能"""
        self.enabled = True
        yield event.plain_result("✅ Forest 房间密钥提取功能已开启")

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
        if not self._check_keyword_rate_limit(group_id):
            return

        logger.info(f"触发固定回复: {message_text}")
        yield event.plain_result(fixed_reply)

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

        # 检查是否触发：关键词 或 @机器人
        is_keyword_trigger = self.keyword_pattern and self.keyword_pattern.search(message_text)
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
        if not self._check_keyword_rate_limit(group_id):
            return

        # 获取用户信息
        user_id = event.get_sender_id()

        # 清空 AI 树种查询缓存
        self._ai_queried_tree_ids = []

        # 构建打卡查询工具集
        checkin_tools = self._build_checkin_tools(user_id, group_id)

        # 构建树种查询工具集
        tree_tools = self._build_tree_tools()

        # 合并工具集
        all_tools = ToolSet(list(checkin_tools.tools) + list(tree_tools.tools))

        # 获取默认人设的系统提示词
        persona = await self.context.persona_manager.get_default_persona_v3(umo=event.unified_msg_origin)
        system_prompt = persona.get("prompt", "") if persona else ""
        system_prompt += "\n\n你可以使用工具查询用户的打卡记录和 Forest 树种信息。当用户询问打卡或树种相关问题时，请调用相应的工具。"

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

            # 如果 AI 查询了树种，附加图片
            if self._ai_queried_tree_ids:
                for tree_id in self._ai_queried_tree_ids[:3]:  # 最多发送3张图片
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

    @filter.command("打卡")
    async def checkin(self, event: AstrMessageEvent):
        """每日打卡（命令方式，需要前缀或@）"""
        if not self._platform_id:
            self._platform_id = event.get_platform_id()

        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 仅支持群聊打卡")
            return

        if self.whitelist and group_id not in self.whitelist:
            yield event.plain_result("⚠️ 本群未开启打卡功能")
            return

        user_id = event.get_sender_id()
        user_name = event.get_sender_name() or f"用户{user_id[-4:]}"

        if self.db.has_checked_today(user_id, group_id):
            days = self.db.get_user_week_days(user_id, group_id)
            yield event.plain_result(f"⚠️ 今日已打卡，本周已打卡 {days} 天")
            return

        if not self.db.checkin(user_id, group_id, user_name):
            yield event.plain_result("⚠️ 打卡失败，请稍后重试")
            return

        days = self.db.get_user_week_days(user_id, group_id)
        logger.info(f"用户 {user_id} 在群 {group_id} 打卡成功，本周第 {days} 天")

        yield event.plain_result(f"✅ 打卡成功！本周已打卡 {days} 天")

    @filter.command("我的打卡")
    async def my_checkin(self, event: AstrMessageEvent):
        """查看我的打卡记录"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("⚠️ 仅支持群聊查询")
            return

        user_id = event.get_sender_id()
        days = self.db.get_user_week_days(user_id, group_id)

        yield event.plain_result(f"📅 本周已打卡 {days} 天")

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
        if not self._today_tree_message:
            yield event.plain_result("今日还没有推送树种，请等待早安通知")
            return

        # 获取图片路径
        image_path = None
        if self._today_tree_id:
            image_path = self.tree_manager.get_tree_image_path(self._today_tree_id)

        # 构建消息链
        components = [Plain(self._today_tree_message)]
        if image_path and image_path.exists():
            components.append(Image(file=str(image_path)))

        yield event.chain_result(components)

    @filter.command("随机树种")
    async def random_tree(self, event: AstrMessageEvent):
        """随机抽取一个树种介绍
        注意：此命令不返回结果，由 AI 来生成回复
        """
        # 此命令不返回结果，由 on_keyword_message 函数中的 AI 来生成回复
        return

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
