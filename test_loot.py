"""Добыча, которую сервер разыграл после убийства, не теряется, если пилот успел уйти из локации.

Запуск: python -m unittest test_loot
"""
import unittest
from pathlib import Path

GAME = (Path(__file__).resolve().parent / "game.html").read_text(encoding="utf-8")


class LootAway(unittest.TestCase):
    def test_late_drops_go_to_bag(self):
        a = GAME.index("function mgResult(r){")
        body = GAME[a:GAME.index("\nfunction lootAway(d){", a)]
        self.assertIn("if(k.loc !== locKey){ if(lootAway(d))", body)     # раньше: continue — добыча пропадала
        self.assertNotIn("if(!k || k.loc !== locKey) continue;", body)

    def test_late_drops_keep_registry(self):
        a = GAME.index("function lootAway(d){")
        body = GAME[a:GAME.index("\n}", a)]
        self.assertIn("addItem(S.inv, id, n, INV_MAX, d.g, d.e, d.uid) || addItem(S.store, id, n, STORE_MAX, d.g, d.e, d.uid)", body)
        self.assertIn("if(d.reg && typeof regAddSph === \"function\") regAddSph(id, n);", body)


if __name__ == "__main__":
    unittest.main()
