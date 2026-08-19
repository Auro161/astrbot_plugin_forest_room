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
            if name in zh or nl in en.lower():
                return tree_id

    return None
