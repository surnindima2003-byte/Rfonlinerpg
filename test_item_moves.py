"""Вещь с руной: надеть, снять, переложить на склад (по одной и разом) — руна остаётся в вещи.

Раньше вещь при переносе пересоздавалась только из id, грейда, заточки и номера: вставленная руна пропадала
навсегда (учтённая оставалась на сервере «в гнезде»). Такие руны возвращаются при входе по счёту сервера (sock).
Запуск: python -m unittest test_item_moves
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
ITEMS_SRC = (ROOT / "items.py").read_text(encoding="utf-8")


class RunesTravelWithItem(unittest.TestCase):
    def test_add_item_copies_socket(self):
        self.assertIn("function addItem(list, id, n, max, g, e, uid, src){", GAME)
        self.assertIn("if(uid) it.uid = uid; gearRunes(it, src); list.push(it);", GAME)

    def test_every_move_passes_the_item(self):
        self.assertIn("S.eq[it.slot] = gearRunes({id:x.id", GAME)                       # надеть
        self.assertIn("e.uid ? {uid:old.uid} : {})}, old));".replace("e.uid", "old.uid"), GAME)   # старая вещь — в сумку
        self.assertIn("addItem(S.inv, e.id, 1, INV_MAX, e.g, e.e, e.uid, e)", GAME)      # снять
        self.assertIn("addItem(to, x.id, k, max, x.g, x.e, x.uid, x)", GAME)             # склад по одной
        self.assertIn("addItem(to, x.id, x.n, max, x.g, x.e, x.uid, x)", GAME)           # склад разом

    def test_lost_registered_runes_come_back(self):
        self.assertIn('"sock": {k: sph.get(SOCKET_PREFIX + k, 0) for k in RUNES', ITEMS_SRC)
        self.assertIn("let miss = (+r.sock[id] || 0) - runeSocketedReg(id);", GAME)
        self.assertIn("runeUnsocketReg(id); back.push(RUNES[id].name);", GAME)


if __name__ == "__main__":
    unittest.main()
