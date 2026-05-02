# Forest 房间密钥提取插件

一个用于 AstrBot 的插件，自动监测 Forest 专注森林房间邀请消息，提取房间密钥并回复。

## 功能特性

- 自动检测 Forest 房间邀请消息格式 `输入我的房间密钥：XXXXXX，和我一起`
- 自动提取房间密钥并发送回复
- 支持自定义回复格式
- 支持群聊白名单/黑名单过滤
- 支持运行时开关控制

## 安装

将本插件克隆到 AstrBot 的 `addons/plugins/` 目录下：

```bash
cd addons/plugins/
git clone https://github.com/openclaw/astrbot_plugin_forest_room.git
```

重启 AstrBot 或在管理面板中重载插件。

## 配置

在 AstrBot 管理面板中配置以下选项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | `true` | 是否启用插件 |
| `reply_format` | string | `{key}` | 回复格式，`{key}` 为密钥占位符 |
| `whitelist` | list | `[]` | 白名单群号，为空则全部群生效 |
| `blacklist` | list | `[]` | 黑名单群号 |

### 配置示例

```json
{
    "enabled": true,
    "reply_format": "房间密钥: {key}",
    "whitelist": ["123456789", "987654321"],
    "blacklist": ["111111111"]
}
```

## 命令

| 命令 | 说明 |
|------|------|
| `forest开启` | 开启密钥提取功能 |
| `forest关闭` | 关闭密钥提取功能 |
| `forest状态` | 查看当前功能状态 |

## 使用场景

当群成员分享 Forest 房间邀请时，消息通常包含如下格式：

```
输入我的房间密钥：ABC123，和我一起种树吧！
```

插件会自动提取密钥 `ABC123` 并回复，方便其他用户快速复制。

## 技术要求

- AstrBot >= 3.0.0
- Python >= 3.8
- 仅支持 aiocqhttp 平台（如 QQ）

## 许可证

MIT License

## 作者

火花花
