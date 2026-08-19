# 房间邀请回复附带对应树种图片

日期：2026-08-19

## 背景

当前插件检测到 Forest 房间邀请后，只回复 `reply_format` 格式化后的纯文本密钥（默认只回密钥本身），即使邀请消息里带有树种信息（如「和我一起种棵 120 分钟的 黄色橡树」）也不附带该树种的图片。用户希望回复时附带邀请中所对应树种的图片。

## 需求

- 检测到房间邀请并解析出树种名时，密钥回复附带该树种的图片。
- 邀请消息未解析出树种名（如纯链接 / 纯 room code）时，**不带图片**，维持现状（只回密钥文本）。
- 解析出的树种名无法匹配到本地树种库时，静默降级为不带图片，不影响密钥提取主功能。
- 带图功能可配置开关，默认开启。

## 约束

- 不修改 `focus_patterns` 正则（它同时用于专注记录入库，改动有回归风险）。
- 不修改数据库结构。
- 不破坏既有撤回同步逻辑（`room_key_mappings` 映射与同步撤回）。

## 设计决策

采用「方案 A」：复用 `focus_patterns` 现有解析结果（`tree_name` / `tree_name_en`），新增树名→图片匹配助手，并改造发送路径以支持附带图片。

## 改动范围

| 文件 | 改动 |
|------|------|
| `main.py` | 新增配置 `reply_with_tree_image` 读取；新增 `_match_tree_image()`；新增 `_send_room_key_reply()`；改造 `on_message` 发送段 |
| `_conf_schema.json` | 新增 `reply_with_tree_image` 配置项（bool，默认 true） |

## 数据流

```
匹配到 room_key
  → 解析专注信息（现有逻辑，得到 tree_name / tree_name_en）
  → 若 reply_with_tree_image 为 False → tree_image_path = None（跳过匹配）
  → _match_tree_image(tree_name, tree_name_en) → Path | None
      ├ 名字标准化：去首尾空白、去尾部标点（。！？!?.,，；;…、）
      ├ 依次：中文名精确匹配 → 英文名精确匹配 → 子串匹配（name in zh / name in en）
      ├ 命中第一个 tree_id → tree_manager.get_tree_image_path(tree_id)
      └ 无命中或图片不存在 → None
  → _send_room_key_reply(event, reply_text, room_key, tree_image_path, group_id, original_msg_id)
      ├ 密钥文本与树种图片拆为两条独立消息
      │   ├ 无图 → 仅发一条密钥文本消息
      │   └ 有图 → 先发文本消息，再发纯图片消息（仅图片，无文字）
      ├ 撤回路径：send_group_msg 分别发送文本/图片，捕获两个 message_id 存入 room_key_mappings（reply_msg_id + image_msg_id）
      └ 正常路径：event.send(chain_result([Plain(text)]))，再 event.send(chain_result([Image(file=...)]))
```

### `_match_tree_image(tree_name, tree_name_en) -> Path | None`

输入为空（两个名字都没有）→ 返回 `None`。

匹配顺序（对每个输入名字，先中文后英文）：
1. 精确匹配：`info["zh"] == name or info["en"].lower() == name.lower()`
2. 子串匹配：`name in info["zh"] or name.lower() in info["en"].lower()`
3. 取第一个命中的 `tree_id`，调用 `get_tree_image_path(tree_id)`；图片文件不存在返回 `None`。

名字标准化：`re.sub(r'[\s。！？!?.,，；;…、]+$', '', name).strip()`。

### `_send_room_key_reply(event, reply_text, room_key, tree_image_path, group_id, original_msg_id)`

封装当前 `on_message` 的两条发送路径（main.py:1345-1389），密钥文本与树种图片拆为两条独立消息：

- **撤回同步路径**（`auto_recall_on_delete` + 群消息 + 有 `event.bot`）：先 `send_group_msg` 发文本捕获 `reply_msg_id`，再（有图时）`send_group_msg` 发图片捕获 `image_msg_id`；随后 `save_room_key_mapping(group_id, original_msg_id, reply_msg_id, room_key, user_id, image_msg_id)`。原邀请被撤回时 `_handle_group_recall` 同时撤回文本与图片。
  - 图片文件字段用 `tree_image_path.as_uri()`（`file:///` URI，OneBot v11 本地文件约定）。
  - 文本发送失败：整体回退普通发送（文本未发出，无重复）。
  - 图片发送失败：记录日志、保留文本映射（image_msg_id=None），不重发文本。
- **正常路径**：先 `event.send(chain_result([Plain(reply_text)]))`，再（有图时）`event.send(chain_result([Image(file=str(path))]))`。

## 错误处理

- 图片匹配失败 / 图片文件不存在 → 静默降级为纯文本，不报错、不影响密钥回复。
- `_match_tree_image` 内部任何异常 → 捕获并返回 `None`。
- 文本发送异常 → 整体回退普通发送；图片发送异常 → 仅记日志、文本映射仍入库，避免重复文本。

## 配置

`_conf_schema.json` 新增：

```json
"reply_with_tree_image": {
    "description": "房间邀请回复附带树种图片",
    "type": "bool",
    "hint": "检测到房间邀请且消息含树种名时，回复附带该树种图片",
    "default": true
}
```

## 测试

### 单元测试（`_match_tree_image`）

- 中文精确命中（如 `黄色橡树` → tree_id 43）
- 英文精确命中（如 `Yellow Oak Tree` → tree_id 43）
- 子串命中
- 带尾部标点的名字（`黄色橡树！` → 标准化后命中）
- 未知树名 → `None`
- 空输入 → `None`
- 图片文件缺失 → `None`

### 手动验证

用参考邀请消息：

> 【是时候放下手机专心做事啰！输入我的房间密钥：3AMHPERSC，和我一起种棵 120 分钟的 黄色橡树 吧！\n或是点击连结直接加入房间：https://www.forestapp.cc/join-room?token=3AMHPERSC】

预期：密钥回复 `3AMHPERSC`（按 `reply_format`）+ 图片 `43_Yellow Oak Tree_黄色橡树.png`。

- 发纯链接 / 纯 room code 邀请 → 只回密钥文本，不带图。
- 开启 / 关闭 `reply_with_tree_image` 配置验证开关生效。
- 回归：密钥文本回复正常；撤回同步仍能触发。

## 变更记录（2026-08-19）

按用户要求，密钥文本与树种图片**拆分为两条独立消息**：

- 发送：先发密钥文本消息，再发纯图片消息（仅图片，无文字）。
- 撤回同步：`room_key_mappings` 表新增 `image_msg_id` 列（含旧表 `ALTER` 迁移，`database.py` 已兼容）；原邀请被撤回时同步撤回文本与图片两条消息。
- 正常路径：两次 `event.send`；撤回路径：两次 `send_group_msg` 分别捕获两个 message_id。
- 无图时仍只发一条密钥文本消息，行为与拆分前一致。


## 验证记录（参考邀请）

对上述参考邀请模拟完整链路：

```
key[link] -> 3AMHPERSC          （链接优先命中）
tree_name -> '黄色橡树'          （zh 正则精确捕获，无尾部标点）
tree_name_en -> None
match tree_id -> 43             （精确命中）
图片 43_Yellow Oak Tree_黄色橡树.png 存在
```
