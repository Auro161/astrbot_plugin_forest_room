"""
Forest 房间密钥提取插件
监测 Forest 专注森林房间邀请消息，自动提取房间密钥并回复
支持打卡系统、定时通知、学习目标推送
"""

import re
import time
import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from .database import ForestDB


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

        # === 数据库初始化 ===
        data_dir = StarTools.get_data_dir()
        db_path = data_dir / "forest.db"
        self.db = ForestDB(db_path)

        # === 定时任务状态 ===
        self._last_check_minute = -1
        self._schedule_task = None
        self._platform_id: str | None = None

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

    async def _send_morning_notify(self):
        """发送早安通知"""
        logger.info("发送早安打卡通知")
        await self._send_to_whitelist_groups(self.morning_notify_text)

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

    # === 打卡功能 ===

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(EventMessageType.GROUP)
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
