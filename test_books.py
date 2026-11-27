"""Книги навыков (решение владельца 28.11): учитываются сервером, книги чужого класса продаются на маркете за GRAM
и разбираются на ресурсы. Свои книги учатся, как раньше.

Запуск: python -m unittest test_books
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for name in ("aiohttp", "aiohttp.web", "sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio", "sqlalchemy.orm",
             "sqlalchemy.orm.attributes", "db", "models", "db_atomic"):
    try:
        __import__(name)
    except Exception:
        sys.modules[name] = mock.MagicMock()
import items  # noqa: E402

ROOT = Path(__file__).resolve().parent
GAME = (ROOT / "game.html").read_text(encoding="utf-8")


def game_books():
    ids = ["bk_" + x for x in re.findall(r'\{id:"([a-z]+)", lore:', GAME)]
    pas = ["bp_" + x for x in re.findall(r'\{id:"([a-z]+)",\s*cls:', GAME)]
    return set(ids), set(pas)


class Registry(unittest.TestCase):
    def test_books_are_registered(self):
        for b in items.BOOKS:
            self.assertIn(b, items.REG_IDS)
        self.assertEqual(len(set(items.BOOKS)), 49)

    def test_same_books_as_game(self):
        sk, pas = game_books()
        self.assertEqual(set(items.BOOKS_SKILL), sk)
        self.assertEqual(set(items.BOOKS_PAS), pas)
        self.assertIn("...BOOK_IDS]", GAME[GAME.index("const REG_IDS = ["):GAME.index("\n", GAME.index("const REG_IDS = ["))])

    def test_long_id_has_short_key(self):
        self.assertEqual(items._col("bp_selfrepair"), "bp_selfrep")
        self.assertEqual(items._uncol("bp_selfrep"), "bp_selfrepair")
        self.assertEqual(items._col("sph_cu"), "sph_cu")

    def test_drops_are_registered(self):
        a = items.__file__
        src = Path(a).read_text(encoding="utf-8")
        self.assertIn('if d["id"] in BOOKS:', src)
        self.assertIn('item = await add_spheres(s, uid, d["id"], d["n"])', src[src.index('if d["id"] in BOOKS:'):])

    def test_salvage_numbers_same_as_game(self):
        m = re.search(r"const BOOK_SALVAGE = \{scrap:(\d+), mat:([0-9.]+), mats:(\[[^\]]*\]), max:(\d+)\};", GAME)
        self.assertEqual((int(m.group(1)), float(m.group(2)), tuple(json.loads(m.group(3))), int(m.group(4))),
                         (items.BOOK_SALVAGE_SCRAP, items.BOOK_SALVAGE_MAT, items.SALVAGE_MATS, items.SALVAGE_MAX))


class Salvage(unittest.TestCase):
    def S(self):
        return {"scrap": 1000, "inv": [{"id": "bk_volley", "n": 3}, {"id": "wire", "n": 5}], "store": [{"id": "bk_volley", "n": 2}]}

    def test_scrap_and_materials(self):
        S = self.S()
        got, err = items.salvage_in_save(S, "bk_volley", 4, rnd=iter([0.1, 0.9, 0.1, 0.5]).__next__, pick=lambda xs: "wire")
        self.assertIsNone(err)
        self.assertEqual(got, {"scrap": 400, "wire": 2})
        self.assertEqual(S["scrap"], 1400)
        self.assertEqual(items._have_any(S, "bk_volley"), 1)          # 3 из сумки и 1 со склада
        self.assertEqual(items._inv_count(S, "bk_volley"), 0)
        self.assertEqual(items._inv_count(S, "wire"), 7)

    def test_refuses_without_books_or_not_a_book(self):
        S = self.S()
        self.assertIsNotNone(items.salvage_in_save(S, "bk_volley", 6)[1])
        self.assertIsNotNone(items.salvage_in_save(S, "wire", 1)[1])
        self.assertEqual(S, self.S())                                  # при отказе сохранение не меняется

    def test_bag_full(self):
        S = {"scrap": 0, "inv": [{"id": "x%d" % i, "n": 1} for i in range(59)] + [{"id": "bk_volley", "n": 2}], "store": []}
        got, err = items.salvage_in_save(S, "bk_volley", 1, rnd=lambda: 0.0, pick=lambda xs: "chip")
        self.assertIsNotNone(err)                                      # материал некуда положить
        got, err = items.salvage_in_save(S, "bk_volley", 2, rnd=lambda: 0.0, pick=lambda xs: "chip")
        self.assertIsNone(err)                                         # стопка книг ушла целиком — место освободилось


class Client(unittest.TestCase):
    def test_only_foreign_books_tradeable_and_salvageable(self):
        self.assertIn('const bookForeign = id => { const it = ITEMS[id]; return !!(it && it.type === "book" && it.cls && it.cls !== S.cls); };', GAME)
        a = GAME.index("function regTradable(x){")
        self.assertIn("!bookForeign(x.id)) return 0;", GAME[a:a + 200])
        self.assertIn('if(it.type === "book" && bookForeign(x.id))', GAME)

    def test_learning_spends_unregistered_first(self):
        self.assertEqual(GAME.count("regUseSph(c.book, c.books); removeItem(S.inv, c.book, c.books);"), 2)


if __name__ == "__main__":
    unittest.main()
