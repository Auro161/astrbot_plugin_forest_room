"""
Forest 房间密钥提取插件
监测 Forest 专注森林房间邀请消息，自动提取房间密钥并回复
"""

import re
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.event_message_type import EventMessageType


class ForestRoomPlugin(Star):
    """Forest 房间密钥提取插件"""
    
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        
        # 默认配置
        self.enabled = self.config.get("enabled", True)
        self.reply_format = self.config.get("reply_format", "{key}")
        self.whitelist = self.config.get("whitelist", [])  # 白名单群号，为空则全部群生效
        self.blacklist = self.config.get("blacklist", [])  # 黑名单群号
        
        # Forest 房间密钥正则表达式
        # 匹配格式：输入我的房间密钥：XXXXXX，和我一起
        self.key_pattern = re.compile(r"输入我的房间密钥：([A-Z0-9]+)，和我一起")
        
        logger.info(f"Forest 房间密钥提取插件已加载，启用状态: {self.enabled}")
    
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AiocqhttpMessageEvent):
        """
        监听所有消息，检测 Forest 房间邀请
        """
        # 检查插件是否启用
        if not self.enabled:
            return
        
        # 获取消息文本
        message_text = event.message_str
        if not message_text:
            return
        
        # 检查是否匹配 Forest 房间邀请格式
        match = self.key_pattern.search(message_text)
        if not match:
            return
        
        # 提取房间密钥
        room_key = match.group(1)
        
        # 获取群号（如果是群聊）
        group_id = event.get_group_id()
        
        # 检查白名单/黑名单
        if group_id:
            # 如果有白名单且当前群不在白名单中，跳过
            if self.whitelist and group_id not in self.whitelist:
                logger.debug(f"群 {group_id} 不在白名单中，跳过")
                return
            # 如果当前群在黑名单中，跳过
            if group_id in self.blacklist:
                logger.debug(f"群 {group_id} 在黑名单中，跳过")
                return
        
        # 记录日志
        logger.info(f"检测到 Forest 房间邀请，密钥: {room_key}, 群: {group_id or '私聊'}")
        
        # 生成回复
        reply_text = self.reply_format.format(key=room_key)
        
        # 发送回复
        await event.send(event.plain_result(reply_text))
    
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
        """查看 Forest 密钥提取功能状态"""
        status = "✅ 开启" if self.enabled else "❌ 关闭"
        whitelist_str = ", ".join(self.whitelist) if self.whitelist else "无"
        blacklist_str = ", ".join(self.blacklist) if self.blacklist else "无"
        
        result = f"""🌲 Forest 房间密钥提取状态
状态: {status}
白名单群: {whitelist_str}
黑名单群: {blacklist_str}
回复格式: {self.reply_format}"""
        
        yield event.plain_result(result)
