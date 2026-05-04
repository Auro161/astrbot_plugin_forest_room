# Forest 房间密钥提取插件

## 项目概述

这是一个 AstrBot 多功能辅助插件，主要功能包括：
- Forest 专注森林房间密钥自动提取
- 每日打卡系统与周统计
- 定时通知（早安/晚安/周统计/学习目标）
- 关键词唤起 AI 智能回复
- 关键词固定回复规则

## 技术栈

- Python 3.8+
- AstrBot 插件框架
- aiocqhttp 平台适配器（QQ 消息平台）
- SQLite 数据库

## 项目结构

```
astrbot_plugin_forest_room/
├── main.py              # 插件主逻辑
├── database.py          # 数据库操作封装
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置项定义
├── README.md            # 用户文档
└── CLAUDE.md            # 开发指南
```

## 核心模块

### 1. 房间密钥提取

消息匹配使用正则表达式：
```python
r"输入我的房间密钥：([A-Z0-9]+)，和我一起"
```

事件处理流程：
1. 监听 aiocqhttp 平台消息
2. 检查插件启用状态
3. 匹配 Forest 房间邀请格式
4. 白名单/黑名单过滤
5. 限流检查
6. 提取密钥并格式化回复

### 2. 打卡系统

- **数据表**: `checkins` - 存储用户打卡记录
- **唯一约束**: `(user_id, group_id, checkin_date)` 防止重复打卡
- **周统计**: 从周一计算本周打卡天数
- **排行榜**: 按打卡天数降序排列

关键方法：
- `ForestDB.checkin()` - 打卡记录写入
- `ForestDB.get_user_week_days()` - 获取本周打卡天数
- `ForestDB.get_week_rank()` - 获取群排行榜

### 3. 定时通知

使用 `asyncio.create_task` 运行后台定时任务：

```python
async def _schedule_loop(self):
    while True:
        # 每分钟检查一次
        if current_minute != self._last_check_minute:
            # 检查早安/晚安/周统计/学习目标
            ...
        await asyncio.sleep(30)
```

通知类型：
- 早安通知 - 推送到白名单群
- 晚安通知 - 推送到白名单群
- 周统计 - 每群单独生成排行榜
- 学习目标 - 从数据库轮询未推送主题

### 4. 关键词 AI 回复

使用 AstrBot 的 `tool_loop_agent` 实现带工具调用的 AI 回复：

```python
tools = self._build_checkin_tools(user_id, group_id)
response = await self.context.tool_loop_agent(
    event=event,
    chat_provider_id=provider_id,
    prompt=message_text,
    tools=tools,
    system_prompt=system_prompt,
)
```

可用工具：
- `check_today_checkin` - 查询今日是否打卡
- `get_week_checkin_count` - 查询本周打卡天数
- `get_missed_checkin_days` - 查询未打卡日期

### 5. 固定回复

配置驱动的关键词匹配：

```python
def _match_fixed_reply(self, message_text: str) -> str | None:
    for rule in self.fixed_reply_rules:
        for trigger in rule.get("trigger_words", []):
            if trigger in message_text:
                return rule.get("reply", "")
    return None
```

## 配置系统

配置项定义在 `_conf_schema.json`，通过 AstrBot 管理面板配置。

配置保存方式：
```python
self.config["key"] = value
self.config.save_config()
```

## 命令注册

使用 AstrBot 装饰器注册命令：

```python
@filter.command("命令名")
async def handler(self, event: AstrMessageEvent):
    yield event.plain_result("回复内容")
```

管理员命令添加权限检查：
```python
@filter.permission_type(filter.PermissionType.ADMIN)
@filter.command("管理命令")
async def admin_handler(self, event: AstrMessageEvent):
    ...
```

## 开发注意事项

### 消息处理优先级

- `on_message` - 处理房间密钥提取
- `on_fixed_reply_message` - 处理固定回复（优先级高于 AI 回复）
- `on_keyword_message` - 处理关键词 AI 回复
- `on_checkin_message` - 处理打卡

### 限流机制

使用 `deque` 存储时间戳实现滑动窗口限流：

```python
def _check_rate_limit(self, group_id: str) -> bool:
    now = time.time()
    timestamps = self.group_timestamps[group_id]
    # 清理过期时间戳
    while timestamps and timestamps[0] < now - self.rate_limit_window:
        timestamps.popleft()
    # 检查是否超限
    if len(timestamps) >= self.rate_limit_count:
        return False
    timestamps.append(now)
    return True
```

### 平台 ID 缓存

首次消息时缓存平台 ID 用于主动发送消息：

```python
if not self._platform_id:
    self._platform_id = event.get_platform_id()
```

### 白名单/黑名单优先级

- 白名单不为空时，仅白名单群生效
- 白名单为空时，全部群生效（黑名单除外）
- 黑名单群始终不响应

### 数据库路径

使用 `StarTools.get_data_dir()` 获取数据目录：

```python
data_dir = StarTools.get_data_dir()
db_path = data_dir / "forest.db"
```

## 扩展开发

### 添加新的定时任务

1. 在 `__init__` 中添加配置项
2. 在 `_schedule_loop` 中添加检查逻辑
3. 实现推送方法
4. 在 `_conf_schema.json` 中添加配置定义

### 添加新的 AI 工具

在 `_build_checkin_tools` 方法中添加新的 `FunctionTool`：

```python
async def new_tool(context, **kwargs) -> str:
    # 实现工具逻辑
    return "结果"

tools.append(FunctionTool(
    name="tool_name",
    parameters={"type": "object", "properties": {}},
    description="工具描述",
    handler=new_tool,
))
```

### 添加新命令

使用装饰器注册命令：

```python
@filter.command("新命令")
async def new_command(self, event: AstrMessageEvent, arg: str = None):
    yield event.plain_result("回复内容")
```
