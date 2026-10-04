"""Рейд в чате (/explore): исход боя и проверки обработчиков — без aiogram и базы.

Запуск: python -m unittest test_bot_explore
"""
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPLORE = (ROOT / "explore_handlers.py").read_text(encoding="utf-8")
START = (ROOT / "start_handlers.py").read_text(encoding="utf-8")
PROFILE = (ROOT / "profile_handlers.py").read_text(encoding="utf-8")


def fight(attack, defense, hp, npc):
    ns = {"Player": object}                     # аннотация типа в def
    exec(EXPLORE[EXPLORE.index("def simulate_fight"):], ns)
    p = types.SimpleNamespace(attack=attack, defense=defense, hp=hp)
    return ns["simulate_fight"](p, npc)


class FightOutcome(unittest.TestCase):
    def test_win(self):
        self.assertIs(fight(10, 2, 50, {"name": "x", "hp": 20, "attack": 3})[3], True)

    def test_loss(self):
        self.assertIs(fight(1, 0, 5, {"name": "x", "hp": 500, "attack": 9})[3], False)

    def test_turn_limit_is_not_a_win(self):
        # 20 раундов по 1 урона по мобу с 1000 HP, моб почти не ранит — раньше это считалось победой
        self.assertIsNone(fight(1, 50, 50, {"name": "x", "hp": 1000, "attack": 1})[3])


class HandlersContract(unittest.TestCase):
    def test_loss_repairs_robot(self):
        loss = EXPLORE[EXPLORE.index("            else:\n                # раньше робот оставался с 1 HP"):]
        self.assertIn("player.hp = player.max_hp", loss[:400])
        self.assertNotIn("player.hp = 1\n", EXPLORE)

    def test_callback_data_validated(self):
        self.assertIn("ZONES.get(zone_key)", EXPLORE)
        self.assertNotIn("ZONES[zone_key]", EXPLORE)
        self.assertIn("if faction_key not in FACTIONS", START)
        self.assertIn("FACTIONS.get(player.faction", PROFILE)

    def test_no_raw_edit_text(self):
        for src in (EXPLORE, START):
            self.assertNotIn("callback.message.edit_text", src)


if __name__ == "__main__":
    unittest.main()
