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
    for block in ("RUNES", "DRONES"):
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
        for did in items.DRONES:
            self.assertIn(f"{did}:", GAME)

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
