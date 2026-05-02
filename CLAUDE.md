# Forest 房间密钥提取插件

## 项目概述

这是一个 AstrBot 插件，用于自动监测 Forest 专注森林应用的房间邀请消息，提取房间密钥并自动回复。

## 技术栈

- Python 3.8+
- AstrBot 插件框架
- aiocqhttp 平台适配器（QQ 消息平台）

## 项目结构

```
astrbot_plugin_forest_room/
├── main.py              # 插件主逻辑
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置项定义
├── README.md            # 用户文档
└── CLAUDE.md            # 开发指南
```

## 核心逻辑

### 消息匹配

插件使用正则表达式匹配 Forest 房间邀请格式：
```python
r"输入我的房间密钥：([A-Z0-9]+)，和我一起"
```

### 事件处理流程

1. 监听所有 aiocqhttp 平台消息
2. 检查插件启用状态
3. 匹配 Forest 房间邀请格式
4. 白名单/黑名单过滤
5. 提取密钥并格式化回复

### 配置系统

- `enabled`: 插件开关
- `reply_format`: 回复模板，支持 `{key}` 占位符
- `whitelist`: 群号白名单（空=全部生效）
- `blacklist`: 群号黑名单

## 开发指南

### 修改匹配规则

如需调整正则表达式，修改 `main.py` 中的 `self.key_pattern`。

### 添加新命令

使用 `@filter.command("命令名")` 装饰器添加新命令。

### 扩展平台支持

当前仅支持 aiocqhttp 平台。如需支持其他平台，移除或修改 `@filter.platform_adapter_type` 装饰器。

## 注意事项

- 白名单优先级高于黑名单
- 命令不受白名单/黑名单限制
- 日志级别可通过 AstrBot 配置调整
