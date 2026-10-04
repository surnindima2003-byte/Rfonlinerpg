"""Крафт рун и дронов идёт на сервере: рецепты сервера должны совпадать с игрой,
а проверка по сохранению — списывать ресурсы и не пускать без них."""
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# items.py тянет aiohttp и базу; для проверки рецептов они не нужны — подменяем, если их нет
for name in ("aiohttp", "sqlalchemy", "db", "models", "metrics", "mobguard", "db_atomic"):
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = mock.MagicMock()

import items  # noqa: E402

GAME = (Path(__file__).resolve().parent / "game.html").read_text(encoding="utf-8")


def client_recipes():
    out = {}
    for block in ("RUNES", "DRONES", "ARTS", "WINGS", "CLOAKS"):
        body = re.search(rf"const {block} = \{{(.*?)\n\}};", GAME, re.S).group(1)
        for m in re.finditer(r'(\w+):\s*\{name:"[^"]*",\s*src:"craft", lvl:(\d+)[^\n]*?(?:\n[^\n]*?)?need:\{([^}]*)\}', body):
            need = {k: int(v) for k, v in re.findall(r"(\w+):(\d+)", m.group(3))}
            out[m.group(1)] = (int(m.group(2)), need)
    return out


class CraftContract(unittest.TestCase):
    def test_server_matches_client(self):
        self.assertEqual(client_recipes(), items.CRAFT)

    def test_all_tradeable_ids_known_to_client(self):
        for rid in items.RUNES:
            self.assertIn(f"{rid}:", GAME)
        for did in items.DRONES + items.ARTIFACTS + items.WINGS + items.CLOAKS:
            self.assertIn(f"{did}:", GAME)

    def test_ids_fit_balance_column(self):
        # таблица балансов хранит id в String(12): и сами вещи, и руны в гнёздах («s:<id>»)
        for rid in items.REG_IDS:
            self.assertLessEqual(len(rid), 12, rid)
        for rid in items.RUNES:
            self.assertLessEqual(len(items.SOCKET_PREFIX + rid), 12, rid)

    def test_artifact_packs_minted_by_server(self):
        for pid in ("ar_crown", "ar_eye", "ar_heart", "ar_relic"):
            self.assertIn(pid, items.PACK_ITEMS)
            self.assertIn(items.PACK_ITEMS[pid][0][1], items.ARTIFACTS)

    def test_wing_packs_minted_by_server(self):
        # паки крыльев: есть цена в gram.py, сервер выдаёт крылья, и в игре тот же набор
        gram_src = (Path(__file__).resolve().parent / "gram.py").read_text(encoding="utf-8")
        for pid in ("wn_seraph", "wn_void", "wn_phoenix", "wn_storm"):
            self.assertIn(pid, items.PACK_ITEMS)
            self.assertIn(items.PACK_ITEMS[pid][0][1], items.WINGS)
            self.assertRegex(gram_src, rf'"{pid}":\s*\d+')
        body = re.search(r"const WINGS = \{(.*?)\n\};", GAME, re.S).group(1)
        self.assertEqual(set(re.findall(r"^\s*(wg_\w+):", body, re.M)), set(items.WINGS))

    def test_cloak_packs_minted_by_server(self):
        gram_src = (Path(__file__).resolve().parent / "gram.py").read_text(encoding="utf-8")
        for pid in ("cl_royal", "cl_night", "cl_ember", "cl_aurora"):
            self.assertIn(pid, items.PACK_ITEMS)
            self.assertIn(items.PACK_ITEMS[pid][0][1], items.CLOAKS)
            self.assertRegex(gram_src, rf'"{pid}":\s*\d+')
        body = re.search(r"const CLOAKS = \{(.*?)\n\};", GAME, re.S).group(1)
        self.assertEqual(set(re.findall(r"^\s*(ck_\w+):", body, re.M)), set(items.CLOAKS))

    def test_craft_spends_and_adds(self):
        S = {"level": 12, "scrap": 9000, "cores": 40, "inv": [{"id": "chip", "n": 5}]}
        self.assertIsNone(items.craft_in_save(S, "r_atk", 12))
        self.assertEqual((S["scrap"], S["cores"]), (1000, 10))
        self.assertEqual(S["inv"], [{"id": "chip", "n": 1}, {"id": "r_atk", "n": 1}])

    def test_craft_refuses_without_resources_or_level(self):
        S = {"scrap": 100, "cores": 40, "inv": [{"id": "chip", "n": 5}]}
        self.assertIsNotNone(items.craft_in_save(S, "r_atk", 12))
        self.assertEqual(S["scrap"], 100)
        S = {"scrap": 99999, "cores": 999, "inv": [{"id": "chip", "n": 50}]}
        self.assertIsNotNone(items.craft_in_save(S, "r_atk", 5))

    def test_drone_only_once(self):
        S = {"scrap": 99999, "cores": 999, "inv": [{"id": "wire", "n": 50}], "store": [{"id": "d_spark", "n": 1}]}
        self.assertEqual(items.craft_in_save(S, "d_spark", 10), "Этот дрон уже есть")


if __name__ == "__main__":
    unittest.main()
