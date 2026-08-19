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


def test_substring_en():
    """英文子串命中：'Oak' 是 'Yellow Oak Tree' 的子串。"""
    assert match_tree_id(TREE_DATA, None, "Oak") == "43"


def test_zh_miss_then_en_fallback():
    """中文未命中时回退到英文精确命中。"""
    assert match_tree_id(TREE_DATA, "未知", "Wisteria") == "1"
