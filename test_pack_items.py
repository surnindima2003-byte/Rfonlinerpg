"""Снаряжение и сферы из паков магазина GRAM: сервер учитывает ровно то, что пак даёт в игре.

Запуск: python -m unittest test_pack_items
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")
ITEMS = (ROOT / "items.py").read_text(encoding="utf-8")


def server_items():
    a = ITEMS.index("PACK_ITEMS = {")
    b = ITEMS.index("\n\n\nasync def mint_pack")
    ns = {}
    exec(ITEMS[a:b], ns)
    return ns["PACK_ITEMS"]


def client_items():
    blk = GAME[GAME.index("const PACKS = [\n"):]
    blk = blk[:blk.index("\n];")]
    out = {}
    for line in blk.split("\n"):
        m = re.search(r'SP\("(\w+)"', line)
        if not m:
            continue
        got = []
        for t, g, e in re.findall(r'\{id:"gset", t:(\d+), g:(\d+)(?:, e:(\d+))?\}', line):
            got.append(("gset", int(t), int(g), int(e or 0)))
        for t, g, e in re.findall(r'\{id:"wcls", t:(\d+), n:1, g:(\d+)(?:, e:(\d+))?\}', line):
            got.append(("wcls", int(t), int(g), int(e or 0)))
        for sid in ("sph_ti", "sph_cu"):
            for n in re.findall(r'\{id:"%s", n:(\d+)\}' % sid, line):
                got.append(("sph", sid, int(n)))
        if got:
            out[m.group(1)] = got
    return out


class PackItemsContract(unittest.TestCase):
    def test_gear_and_spheres_match_game(self):
        srv = server_items()
        cli = client_items()
        self.assertTrue(cli, "не нашли паки в game.html")
        for pid, want in cli.items():
            got = [e for e in srv.get(pid, []) if e[0] in ("gset", "wcls") or e[1] in ("sph_ti", "sph_cu")]
            self.assertEqual(sorted(got), sorted(want), pid)

    def test_no_dead_keys(self):
        prices = set(re.findall(r'"(\w+)":', (ROOT / "gram.py").read_text(encoding="utf-8")))
        for pid in server_items():
            self.assertIn(pid, prices, f"пак {pid} есть в PACK_ITEMS, но его нельзя купить")

    def test_client_skips_server_minted_rows(self):
        self.assertIn("m.rw === r.id", GAME)


if __name__ == "__main__":
    unittest.main()
