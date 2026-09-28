"""Таблица VIP на сервере (бонус к дропу) должна совпадать с таблицей в игре."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class VipTableTest(unittest.TestCase):
    def test_same_as_client(self):
        game = (ROOT / "game.html").read_text(encoding="utf-8")
        blk = game[game.index("const VIP = [\n"):game.index("];", game.index("const VIP = [\n"))]
        client = [(int(a), float(b), float(c), float(d)) for a, b, c, d in
                  re.findall(r"need:(\d+), xp:([\d.]+), scrap:([\d.]+), drop:([\d.]+)", blk)]
        src = (ROOT / "vip.py").read_text(encoding="utf-8")
        ns = {}
        exec(src[src.index("LEVELS"):src.index("NANO")], ns)
        self.assertEqual(client, [tuple(x) for x in ns["LEVELS"]])

    def test_levels(self):
        src = (ROOT / "vip.py").read_text(encoding="utf-8")
        ns = {}
        exec(src[src.index("LEVELS"):src.index("NANO")], ns)
        self.assertEqual(ns["LEVELS"][-1][1:], (2.3, 2.3, 2.3))


if __name__ == "__main__":
    unittest.main()
