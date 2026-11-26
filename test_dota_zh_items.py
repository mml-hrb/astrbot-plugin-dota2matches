import json
import unittest
from pathlib import Path

import dota_zh


class ItemNameMappingTests(unittest.TestCase):
    def test_recent_item_keys_have_static_chinese_names(self):
        items = dota_zh._static_table()[dota_zh.KIND_ITEM]
        expected = {
            "bloodslinger": "血腥投带",
            "blood_grenade": "血腥榴弹",
            "royale_with_cheese": "奶酪块",
            "angels_demise": "绝刃",
            "devastator": "圣斧",
            "skypiercer": "破天之矛",
            "splintmail": "片甲",
            "shawl": "披巾",
            "tiara_of_selemene": "赛莉蒙妮之冠",
            "recipe_bloodslinger": "血腥投带卷轴",
            "recipe_skypiercer": "破天之矛卷轴",
            "recipe_angels_demise": "绝刃卷轴",
            "recipe_devastator": "圣斧卷轴",
        }
        for key, name in expected.items():
            with self.subTest(key=key):
                self.assertEqual(items[key], name)

    def test_item_keys_are_unique_json_object_entries(self):
        text = Path("dota_zh_data.json").read_text(encoding="utf-8")
        pairs = json.loads(text, object_pairs_hook=lambda pairs: pairs)
        item_pairs = next(value for key, value in pairs if key == "items")
        keys = [key for key, _ in item_pairs]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
