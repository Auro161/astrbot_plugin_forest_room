# 房间邀请回复附带树种图片 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 检测到 Forest 房间邀请时，若消息含树种名，则在密钥回复中附带该树种图片。

**Architecture:** 复用 `focus_patterns` 现有解析出的 `tree_name` / `tree_name_en`，新增独立纯函数模块 `tree_matching.py`（不依赖 astrbot，可独立单测）负责树名→tree_id 匹配；插件侧新增 `_match_tree_image()` 包装为图片路径，并新增 `_send_room_key_reply()` 重构发送段以支持附带图片。新增 `reply_with_tree_image` 配置开关（默认开）。不修改 `focus_patterns` 正则、不改数据库、不破坏撤回同步。

**Tech Stack:** Python 3.8+，pytest（本机 8.4.2，astrbot 在 Python 3.13 环境不可导入，故纯逻辑须独立模块），AstrBot `MessageChain`/`Plain`/`Image`。

## Global Constraints

- 不修改 `focus_patterns` 正则（同时用于专注记录入库）。
- 不修改数据库结构、不破坏 `room_key_mappings` 撤回同步逻辑。
- 匹配逻辑必须是纯函数、可脱离 astrbot 单测。
- 树名→图片匹配：精确匹配优先于子串匹配；无命中或图片缺失时静默返回 `None`（不带图），绝不影响密钥提取主功能。
- 导入路径遵循现有 `from astrbot.api import ...` 写法。
- `reply_with_tree_image` 配置默认 `true`。

---

### Task 1: 纯匹配函数 `tree_matching.py`（TDD）

**Files:**
- Create: `tree_matching.py`（插件根目录）
- Create: `tests/test_tree_matching.py`

**Interfaces:**
- Produces: `match_tree_id(tree_data: dict, tree_name: str | None, tree_name_en: str | None) -> str | None` — 按顺序对 中文名/英文名 先精确后子串匹配，返回第一个命中的 tree_id，无命中返回 `None`。Task 3 依赖此函数。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_tree_matching.py`：

```python
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tree_matching import match_tree_id

TREE_DATA = {
    "43": {"en": "Yellow Oak Tree", "zh": "黄色橡树"},
    "1": {"en": "Wisteria", "zh": "紫藤"},
    "2": {"en": "Cherry Tree", "zh": "樱花树"},
}


def test_exact_zh():
    assert match_tree_id(TREE_DATA, "黄色橡树", None) == "43"


def test_exact_en():
    assert match_tree_id(TREE_DATA, None, "Wisteria") == "1"


def test_exact_en_case_insensitive():
    assert match_tree_id(TREE_DATA, None, "wisteria") == "1"


def test_trailing_punctuation_zh():
    assert match_tree_id(TREE_DATA, "黄色橡树！", None) == "43"


def test_substring_zh():
    assert match_tree_id(TREE_DATA, "橡树", None) == "43"


def test_unknown_tree():
    assert match_tree_id(TREE_DATA, "不存在的树", None) is None


def test_empty_inputs():
    assert match_tree_id(TREE_DATA, None, None) is None
    assert match_tree_id(TREE_DATA, "", "") is None


def test_prefers_zh_name_first():
    assert match_tree_id(TREE_DATA, "黄色橡树", "Wisteria") == "43"


def test_reference_invite_message():
    """回归：用真实邀请消息验证正则提取 + 匹配链路。"""
    msg = ("【是时候放下手机专心做事啰！输入我的房间密钥：3AMHPERSC，"
           "和我一起种棵 120 分钟的 黄色橡树 吧！\n"
           "或是点击连结直接加入房间：https://www.forestapp.cc/join-room?token=3AMHPERSC】")
    zh = re.search(r'和我一起种棵\s*(\d+)\s*分钟的\s*(\S+)', msg)
    assert zh.group(2) == "黄色橡树"
    assert match_tree_id(TREE_DATA, zh.group(2), None) == "43"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_tree_matching.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'tree_matching'`。

- [ ] **Step 3: 实现 `match_tree_id`**

创建 `tree_matching.py`：

```python
"""树种名称→tree_id 的纯匹配逻辑（无 astrbot 依赖，可独立单测）。"""
import re

_TRAILING_PUNCT_RE = re.compile(r'[\s。！？!?.,，；;…、]+$')


def _normalize_name(name: str) -> str:
    """去掉首尾空白与尾部标点。"""
    return _TRAILING_PUNCT_RE.sub('', name).strip()


def match_tree_id(tree_data: dict, tree_name: str | None, tree_name_en: str | None) -> str | None:
    """根据邀请消息中的树种名匹配 tree_id。

    依次对 中文名/英文名 做：精确匹配 → 子串匹配。
    返回第一个命中的 tree_id，无命中返回 None。
    """
    names = []
    for raw in (tree_name, tree_name_en):
        if raw:
            n = _normalize_name(raw)
            if n:
                names.append(n)
    if not names:
        return None

    for name in names:
        nl = name.lower()
        for tree_id, info in tree_data.items():
            zh = info.get("zh", "")
            en = info.get("en", "")
            if zh == name or en.lower() == nl:
                return tree_id

    for name in names:
        nl = name.lower()
        for tree_id, info in tree_data.items():
            zh = info.get("zh", "")
            en = info.get("en", "")
            if (name and name in zh) or (nl and nl in en.lower()):
                return tree_id

    return None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_tree_matching.py -v`
Expected: 9 个测试全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add tree_matching.py tests/test_tree_matching.py
git commit -m "feat(tree): 新增树名→tree_id 纯匹配函数及单元测试"
```

---

### Task 2: 配置项 `reply_with_tree_image`

**Files:**
- Modify: `_conf_schema.json`（在 `reply_format` 配置块之后插入）
- Modify: `main.py:225`（`_init_configs` 中 `self.reply_format` 之后）

**Interfaces:**
- Consumes: 无
- Produces: 插件实例属性 `self.reply_with_tree_image: bool`（默认 `True`）。Task 3 读取它作为图片开关。

- [ ] **Step 1: 在 `_conf_schema.json` 添加配置项**

在 `_conf_schema.json` 中 `reply_format` 块（第 13 行 `},`）之后、`whitelist` 块之前插入：

```json
    "reply_with_tree_image": {
        "description": "房间邀请回复附带树种图片",
        "type": "bool",
        "hint": "检测到房间邀请且消息含树种名时，回复附带该树种图片",
        "default": true
    },
```

- [ ] **Step 2: 在 `_init_configs` 读取配置**

在 `main.py:225` `self.reply_format = self._get_config("reply_format", "{key}")` 之后新增一行：

```python
        self.reply_with_tree_image = self._get_config("reply_with_tree_image", True)
```

- [ ] **Step 3: 验证**

Run: `python -m json.tool _conf_schema.json` — 应输出合法 JSON 无报错。
Run: `python -m py_compile main.py` — 应无输出（编译通过）。

- [ ] **Step 4: 提交**

```bash
git add _conf_schema.json main.py
git commit -m "feat(config): 新增 reply_with_tree_image 配置项"
```

---

### Task 3: 集成发送图片

**Files:**
- Modify: `main.py:33`（新增导入）
- Modify: `main.py`（`# === 消息处理 ===` 之前新增 `_match_tree_image` 与 `_send_room_key_reply` 两个方法）
- Modify: `main.py:1543-1569`（`on_message` 发送段）

**Interfaces:**
- Consumes:
  - `match_tree_id`（Task 1）
  - `self.tree_manager.trees_data` / `self.tree_manager.get_tree_image_path(tree_id)`（现有 TreeManager）
  - `self.reply_with_tree_image`（Task 2）
  - `_get_message_id(event)`（现有模块级函数）
- Produces:
  - `_match_tree_image(tree_name: str | None, tree_name_en: str | None) -> Path | None`
  - `_send_room_key_reply(event, reply_text: str, room_key: str, tree_image_path: Path | None, group_id: str | None) -> None`

- [ ] **Step 1: 新增导入**

在 `main.py:33` `from .database import ForestDB` 之后新增：

```python
from .tree_matching import match_tree_id
```

- [ ] **Step 2: 新增 `_match_tree_image` 与 `_send_room_key_reply` 方法**

在 `main.py` 中 `# === 消息处理 ===` 注释（约 1336 行）之前、`on_message` 定义前插入：

```python
    def _match_tree_image(self, tree_name: str | None, tree_name_en: str | None) -> Path | None:
        """匹配邀请消息中的树种名到图片路径；无命中或图片缺失返回 None。"""
        tree_id = match_tree_id(self.tree_manager.trees_data, tree_name, tree_name_en)
        if tree_id is None:
            return None
        return self.tree_manager.get_tree_image_path(tree_id)

    async def _send_room_key_reply(self, event: AstrMessageEvent, reply_text: str,
                                   room_key: str, tree_image_path: Path | None,
                                   group_id: str | None):
        """发送房间密钥回复，可附带树种图片；撤回同步逻辑保持不变。"""
        original_msg_id = _get_message_id(event)

        def build_send():
            components = [Plain(reply_text)]
            if tree_image_path and tree_image_path.exists():
                components.append(Image(file=str(tree_image_path)))
            return event.chain_result(components)

        # 撤回同步路径：手动发送以捕获 message_id
        if (self.auto_recall_on_delete and group_id and original_msg_id
                and hasattr(event, 'bot')):
            try:
                message: object = reply_text
                if tree_image_path and tree_image_path.exists():
                    message = [
                        {"type": "text", "data": {"text": reply_text}},
                        {"type": "image", "data": {"file": str(tree_image_path)}},
                    ]
                result = await event.bot.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=message,
                )
                reply_msg_id = result.get("message_id")
                if reply_msg_id:
                    user_id = event.get_sender_id()
                    self.db.save_room_key_mapping(
                        group_id, original_msg_id, reply_msg_id,
                        room_key, user_id
                    )
                    logger.info(f"已保存房间密钥映射: original={original_msg_id}, reply={reply_msg_id}")
            except Exception as e:
                logger.error(f"发送房间密钥回复失败: {e}")
                await event.send(build_send())
        else:
            await event.send(build_send())
```

- [ ] **Step 3: 改造 `on_message` 发送段**

把 `main.py:1543-1569` 的发送段（从 `logger.info(f"检测到 Forest 房间邀请...` 到 `await event.send(event.plain_result(reply_text))` 结束）整体替换为：

```python
        logger.info(f"检测到 Forest 房间邀请，密钥: {room_key}, 群: {group_id or '私聊'}")
        reply_text = self.reply_format.format(key=room_key)

        # 附带树种图片（可配置开关；匹配失败静默降级为纯文本）
        tree_image_path = None
        if self.reply_with_tree_image:
            try:
                tree_image_path = self._match_tree_image(tree_name, tree_name_en)
                if tree_image_path:
                    logger.info(f"房间邀请附带树种图片: {tree_image_path.name}")
            except Exception as e:
                logger.warning(f"匹配树种图片失败: {e}")

        await self._send_room_key_reply(event, reply_text, room_key, tree_image_path, group_id)
```

注意：`tree_name` / `tree_name_en` 在 `on_message` 中 1502-1504 行已初始化为 `None`，若 `focus_patterns` 未命中则保持 `None`，`_match_tree_image` 返回 `None`（不带图），行为正确。

- [ ] **Step 4: 验证**

Run: `python -m py_compile main.py tree_matching.py` — 应无输出（编译通过）。

Run 集成回归（模拟真实邀请消息的提取 + 匹配 + 图片路径解析，不导入 main.py）：

```bash
python -X utf8 -c "
import re, sys, json
from pathlib import Path
sys.path.insert(0, r'G:\claudecode\astrbot_plugin_forest_room')
from tree_matching import match_tree_id
data = json.load(open(r'G:\claudecode\astrbot_plugin_forest_room\tree\tree_names.json', encoding='utf-8'))
msg = ('【是时候放下手机专心做事啰！输入我的房间密钥：3AMHPERSC，和我一起种棵 120 分钟的 黄色橡树 吧！'
       '\n或是点击连结直接加入房间：https://www.forestapp.cc/join-room?token=3AMHPERSC】')
zh = re.search(r'和我一起种棵\s*(\d+)\s*分钟的\s*(\S+)', msg)
tid = match_tree_id(data, zh.group(2), None)
print('tree_id =', tid)
info = data[tid]
img = Path(r'G:\claudecode\astrbot_plugin_forest_room\tree\mature_trees') / f'{tid}_{info[\"en\"]}_{info[\"zh\"]}.png'
print('image exists =', img.exists())
assert tid == '43' and img.exists()
print('OK')
"
```

Expected: 输出 `tree_id = 43`、`image exists = True`、`OK`。

- [ ] **Step 5: 提交**

```bash
git add main.py
git commit -m "feat(room): 房间邀请回复附带对应树种图片"
```

---

## 手动验收（真实机器人环境）

在 QQ 群内验证（需要 AstrBot 运行环境，当前 Python 3.13 无 astrbot 无法本地执行）：

1. 发参考邀请 → 期望回复密钥 + `黄色橡树` 图片。
2. 发纯链接 / 纯 room code 邀请 → 期望只回密钥文本，无图。
3. 管理面板关闭 `reply_with_tree_image` → 期望所有邀请回复均无图。
4. 开启撤回同步时撤回原邀请 → 期望机器人同步撤回带图回复，且映射记录仍生效。

## 自审

- **Spec 覆盖**：开关（Task 2）、匹配与标准化（Task 1）、无树名/未知树静默降级（Task 1 返回 None + Task 3 集成）、双发送路径带图与回退保留图片（Task 3）、schema 条目（Task 2）——均覆盖。
- **占位符**：无 TBD/TODO。
- **类型一致**：`match_tree_id` 在 Task 1 定义、Task 3 消费，签名一致；`_match_tree_image` / `_send_room_key_reply` 在 Task 3 定义并唯一调用；`self.reply_with_tree_image` 在 Task 2 定义、Task 3 读取。
